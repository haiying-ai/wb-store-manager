"""Auditable, currency-isolated running accounts; never performs platform writes.

Adapters provide historical monetary amounts and explicit source coverage.  This
module contains no seller rates, assumed exchange rates, or funding authority.
Positive expense metrics mean a charge; a later return posts negative historical
accruals on its own business date. Cash already spent is never reversed by a return.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping


CENT = Decimal("0.01")
ZERO = Decimal("0")
PROJECTION_VERSION = 2
METRICS = (
    "gross_sales", "seller_proceeds", "product_cost", "agency_accrued",
    "promotion_accrued", "shared_pool_accrued", "return_reserve_accrued", "logistics_cost", "other_cost",
    "cash_ad_spend", "promo_credit_spend", "agency_paid", "receipts_received",
    "withdrawal_requested", "withdrawal_completed", "withdrawal_cancelled",
    "shared_pool_return_cost", "separate_return_cost",
)
REVERSIBLE_METRICS = (
    "gross_sales", "seller_proceeds", "product_cost", "agency_accrued",
    "promotion_accrued", "shared_pool_accrued", "return_reserve_accrued",
)
DERIVED = {
    "profit": {"seller_proceeds": 1, "product_cost": -1, "agency_accrued": -1,
               "promotion_accrued": -1, "return_reserve_accrued": -1,
               "logistics_cost": -1, "other_cost": -1, "excess_return_loss": -1},
    "operating_contribution": {"seller_proceeds": 1, "product_cost": -1,
                               "agency_accrued": -1, "cash_ad_spend": -1,
                               "logistics_cost": -1, "other_cost": -1,
                               "shared_pool_return_cost": -1, "separate_return_cost": -1},
    "advertising_available": {"promotion_accrued": 1, "cash_ad_spend": -1,
                              "shared_pool_return_cost": -1},
    "agency_payable": {"agency_accrued": 1, "agency_paid": -1},
    "withdrawal_pending": {"withdrawal_requested": 1, "withdrawal_completed": -1,
                           "withdrawal_cancelled": -1},
    "withdrawal_available": {"receipts_received": 1, "withdrawal_requested": -1,
                             "withdrawal_cancelled": 1},
}


class RunningAccountError(ValueError):
    """The supplied event or account contract is invalid."""


class EventConflict(RunningAccountError):
    """An existing identity cannot silently be overwritten."""


def money(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError("nonfinite amount")
        return result.quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise RunningAccountError(f"Invalid monetary amount: {value!r}") from exc


def _date(value: str | date) -> str:
    return date.fromisoformat(str(value)).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _timestamp(value: str | None = None) -> str:
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else datetime.now(timezone.utc)
    if result.tzinfo is None:
        raise RunningAccountError("Evidence timestamps must contain a timezone")
    return result.astimezone(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class FinanceEvent:
    store_id: str
    event_id: str
    event_date: str
    kind: str
    currency: str = "CNY"
    amounts: Mapping[str, Decimal | str | int | None] = field(default_factory=dict)
    order_id: str | None = None
    original_sale_id: str | None = None
    refund_fraction: str | Decimal | None = None
    saleable_return: bool | None = None
    evidence: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        if not self.store_id or self.store_id == "ALL" or not self.event_id:
            raise RunningAccountError("A nonempty store-scoped event identity is required")
        if not self.currency or self.currency != self.currency.upper():
            raise RunningAccountError("Use an uppercase, explicit currency")
        invalid = set(self.amounts) - set(METRICS)
        if invalid:
            raise RunningAccountError(f"Unknown metrics: {sorted(invalid)}")
        if self.kind not in {"sale", "refund", "cash", "adjustment"}:
            raise RunningAccountError("Unsupported event kind")
        fraction = None
        if self.kind == "refund":
            if not self.original_sale_id:
                raise RunningAccountError("Refunds must identify the original same-store sale")
            if self.saleable_return is not None and not isinstance(self.saleable_return, bool):
                raise RunningAccountError("Return inventory status must be true, false, or unknown")
            try:
                fraction_value = Decimal(str(self.refund_fraction))
            except InvalidOperation as exc:
                raise RunningAccountError("Refund fraction must be a finite decimal") from exc
            if not fraction_value.is_finite() or not ZERO < fraction_value <= 1:
                raise RunningAccountError("Refund fraction must be greater than zero and at most one")
            fraction = str(fraction_value)
            if set(self.amounts) & set(REVERSIBLE_METRICS):
                raise RunningAccountError("Refund accruals are resolved from the original sale, not supplied anew")
            if set(self.amounts) - {"logistics_cost", "other_cost", "shared_pool_return_cost", "separate_return_cost"}:
                raise RunningAccountError("Refunds cannot reverse cash advertisements or cash transactions")
        values = {key: None if value is None else str(money(value)) for key, value in self.amounts.items()}
        if self.kind == "sale":
            for metric in REVERSIBLE_METRICS:
                values.setdefault(metric, None)
            if any(value is not None and Decimal(value) < ZERO for value in values.values()):
                raise RunningAccountError("Use a linked refund or an audited adjustment for negative sale amounts")
        if self.kind == "cash" and any(value is not None and Decimal(value) < ZERO for value in values.values()):
            raise RunningAccountError("Negative cash facts require an explicit audited adjustment")
        return {
            "store_id": self.store_id, "event_id": self.event_id,
            "event_date": _date(self.event_date), "kind": self.kind, "currency": self.currency,
            "amounts": values, "order_id": self.order_id, "original_sale_id": self.original_sale_id,
            "refund_fraction": fraction, "saleable_return": self.saleable_return,
            "evidence": sorted(set(self.evidence)), "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> FinanceEvent:
        return cls(**{**value, "evidence": tuple(value.get("evidence", ()))})


def sale_event(store_id: str, event_id: str, event_date: str, *,
               amounts: Mapping[str, Any], **kwargs: Any) -> FinanceEvent:
    return FinanceEvent(store_id, event_id, event_date, "sale", amounts=amounts, **kwargs)


def refund_event(store_id: str, event_id: str, event_date: str, *,
                 original_sale_id: str, refund_fraction: str = "1",
                 saleable_return: bool | None = None, **kwargs: Any) -> FinanceEvent:
    return FinanceEvent(store_id, event_id, event_date, "refund", original_sale_id=original_sale_id,
                        refund_fraction=refund_fraction, saleable_return=saleable_return, **kwargs)


def cash_event(store_id: str, event_id: str, event_date: str, *, metric: str,
               amount: Any, **kwargs: Any) -> FinanceEvent:
    if metric not in {"cash_ad_spend", "promo_credit_spend", "agency_accrued", "agency_paid", "receipts_received",
                       "withdrawal_requested", "withdrawal_completed", "withdrawal_cancelled",
                       "logistics_cost", "other_cost", "shared_pool_return_cost", "separate_return_cost"}:
        raise RunningAccountError("Cash events require an explicit supported cash or expense metric")
    return FinanceEvent(store_id, event_id, event_date, "cash", amounts={metric: amount}, **kwargs)


def materialize_events(events: Iterable[FinanceEvent]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve returns without mutating originals; final partial return absorbs cent drift."""
    items = [event.to_dict() for event in events]
    identities = [(item["store_id"], item["event_id"]) for item in items]
    if len(identities) != len(set(identities)):
        raise EventConflict("Materialization requires exactly one current revision per identity")
    sales = {(item["store_id"], item["event_id"]): item for item in items if item["kind"] == "sale"}
    fractions: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    restored: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    postings, issues = [], []
    for item in sorted(items, key=lambda row: (row["event_date"], row["store_id"], row["event_id"])):
        amounts = dict(item["amounts"])
        if item["kind"] == "refund":
            key = (item["store_id"], item["original_sale_id"])
            original = sales.get(key)
            fraction = Decimal(item["refund_fraction"])
            previous, target = fractions[key], fractions[key] + fraction
            error = None
            if original is None:
                error = "ORIGINAL_SALE_MISSING"
            elif original["currency"] != item["currency"]:
                error = "REFUND_CURRENCY_MISMATCH"
            elif original["event_date"] > item["event_date"]:
                error = "REFUND_PRECEDES_ORIGINAL_SALE"
            elif item.get("order_id") and original.get("order_id") and item["order_id"] != original["order_id"]:
                error = "REFUND_ORDER_MISMATCH"
            elif target > 1:
                error = "REFUND_EXCEEDS_ORIGINAL_QUANTITY"
            if error:
                amounts.update({metric: None for metric in REVERSIBLE_METRICS})
                issues.append({"code": error, "store_id": item["store_id"], "event_id": item["event_id"],
                               "event_date": item["event_date"], "currency": item["currency"]})
            else:
                fractions[key] = target
                for metric in REVERSIBLE_METRICS:
                    value = original["amounts"].get(metric)
                    if metric == "product_cost":
                        if item["saleable_return"] is None:
                            amounts[metric] = None
                            issues.append({"code": "RETURN_INVENTORY_STATUS_UNKNOWN", "store_id": item["store_id"],
                                           "event_id": item["event_id"], "event_date": item["event_date"],
                                           "currency": item["currency"]})
                            continue
                        if item["saleable_return"] is False:
                            amounts[metric] = "0.00"
                            continue
                        restored_before = restored[key]
                        restored[key] += fraction
                        amounts[metric] = (None if value is None else
                                           str(-(money(Decimal(value) * restored[key]) - money(Decimal(value) * restored_before))))
                    else:
                        amounts[metric] = (None if value is None else
                                           str(-(money(Decimal(value) * target) - money(Decimal(value) * previous))))
        postings.append({**item, "amounts": amounts})
    return postings, issues


def _metric(known: Decimal, complete: bool) -> dict[str, Any]:
    value = str(money(known))
    return {"amount": value if complete else None, "known_subtotal": value, "complete": complete}


_RETURN_EXCESS_COMPONENTS = {
    "shared_return_excess": ("shared_pool_return_cost", "shared_pool_accrued"),
    "separate_return_excess": ("separate_return_cost", "return_reserve_accrued"),
}


def _derive(values: dict[str, dict[str, Any]], *,
            excess_values: Mapping[str, dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    for name, (cost, reserve) in _RETURN_EXCESS_COMPONENTS.items():
        if excess_values is not None:
            values[name] = dict(excess_values[name])
        else:
            # Only costs outside the applicable original reserve are another
            # policy-profit deduction. Deficits never borrow another store's
            # budget or the promotion allocation of a separate-risk policy.
            excess = max(ZERO, Decimal(values[cost]["known_subtotal"])
                         - max(ZERO, Decimal(values[reserve]["known_subtotal"])))
            values[name] = _metric(excess, values[cost]["complete"] and values[reserve]["complete"])
    values["excess_return_loss"] = _metric(
        sum((Decimal(values[name]["known_subtotal"]) for name in _RETURN_EXCESS_COMPONENTS), ZERO),
        all(values[name]["complete"] for name in _RETURN_EXCESS_COMPONENTS),
    )
    for name, components in DERIVED.items():
        values[name] = _metric(sum((Decimal(values[key]["known_subtotal"]) * sign for key, sign in components.items()), ZERO),
                               all(values[key]["complete"] for key in components))
    available = values["advertising_available"]
    values["advertising_spendable"] = _metric(max(ZERO, Decimal(available["known_subtotal"])), available["complete"])
    return values


class RunningAccount:
    """Append-only revisions plus reusable daily projections in a dedicated SQLite DB.

    ``amount`` is null until source coverage is complete; ``known_subtotal`` is
    evidence-backed partial information, not an assertion that missing facts are zero.
    ``known_at`` is an optional UTC evidence-time cutoff, distinct from business date.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS ra_stores(store_id TEXT PRIMARY KEY, opened_on TEXT, currency TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ra_events(seq INTEGER PRIMARY KEY AUTOINCREMENT, store_id TEXT NOT NULL,
              event_id TEXT NOT NULL, revision INTEGER NOT NULL, recorded_at TEXT NOT NULL,
              payload TEXT NOT NULL, fingerprint TEXT NOT NULL, correction_reason TEXT,
              UNIQUE(store_id,event_id,revision));
            CREATE INDEX IF NOT EXISTS ra_events_current ON ra_events(store_id,event_id,revision);
            CREATE TABLE IF NOT EXISTS ra_coverage(seq INTEGER PRIMARY KEY AUTOINCREMENT, store_id TEXT NOT NULL,
              currency TEXT NOT NULL, metric TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
              complete INTEGER NOT NULL, evidence TEXT NOT NULL, recorded_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ra_daily(store_id TEXT NOT NULL, currency TEXT NOT NULL, business_date TEXT NOT NULL,
              metric TEXT NOT NULL, amount_cents INTEGER NOT NULL, unknown_count INTEGER NOT NULL,
              PRIMARY KEY(store_id,currency,business_date,metric));
            CREATE TABLE IF NOT EXISTS ra_projection(store_id TEXT PRIMARY KEY, event_seq INTEGER NOT NULL,
              issues TEXT NOT NULL, projection_version INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS ra_snapshots(report_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL);
        """)
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(ra_projection)")}
        if "projection_version" not in columns:
            self.connection.execute("ALTER TABLE ra_projection ADD COLUMN projection_version INTEGER NOT NULL DEFAULT 0")
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> RunningAccount:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def register_store(self, store_id: str, opened_on: str | None, currency: str = "CNY") -> None:
        if not store_id or store_id == "ALL":
            raise RunningAccountError("Store identity must be explicit; ALL is reserved for display")
        if not currency or currency != currency.upper():
            raise RunningAccountError("Use an uppercase, explicit reporting currency")
        start = _date(opened_on) if opened_on else None
        old = self.connection.execute("SELECT * FROM ra_stores WHERE store_id=?", (store_id,)).fetchone()
        if old and (old["opened_on"] != start or old["currency"] != currency):
            raise EventConflict("Store opening or reporting currency cannot silently change")
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO ra_stores VALUES(?,?,?)", (store_id, start, currency))

    def upsert_event(self, event: FinanceEvent, *, expected_revision: int | None = None,
                     correction_reason: str | None = None, recorded_at: str | None = None) -> dict[str, Any]:
        payload = _json(event.to_dict())
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        with self.connection:
            # Serialize the compare-and-append, including independent controller processes.
            self.connection.execute("BEGIN IMMEDIATE")
            old = self.connection.execute("SELECT * FROM ra_events WHERE store_id=? AND event_id=? ORDER BY revision DESC LIMIT 1",
                                          (event.store_id, event.event_id)).fetchone()
            if old and old["fingerprint"] == digest:
                return {"changed": False, "revision": old["revision"], "fingerprint": digest}
            if old and (expected_revision != old["revision"] or not correction_reason or not correction_reason.strip()):
                raise EventConflict("Changed evidence requires the current expected_revision and a correction_reason")
            if old and (json.loads(old["payload"])["kind"] != event.kind or
                        json.loads(old["payload"])["currency"] != event.currency):
                raise EventConflict("Event kind and currency are identity attributes; append a distinct adjusting event")
            if not old and expected_revision not in (None, 0):
                raise EventConflict("Expected revision does not exist")
            revision = old["revision"] + 1 if old else 1
            timestamp = _timestamp(recorded_at)
            if old and timestamp <= old["recorded_at"]:
                raise EventConflict("A revision must have a later evidence timestamp than its predecessor")
            self.connection.execute("INSERT INTO ra_events(store_id,event_id,revision,recorded_at,payload,fingerprint,correction_reason) VALUES(?,?,?,?,?,?,?)",
                                    (event.store_id, event.event_id, revision, timestamp, payload, digest, correction_reason))
        return {"changed": True, "revision": revision, "fingerprint": digest}

    def event_history(self, store_id: str, event_id: str) -> list[dict[str, Any]]:
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in self.connection.execute(
            "SELECT * FROM ra_events WHERE store_id=? AND event_id=? ORDER BY revision", (store_id, event_id))]

    def current_events(self, store_id: str | None = None, *, known_at: str | None = None) -> list[FinanceEvent]:
        conditions, params = [], []
        if store_id:
            conditions.append("store_id=?")
            params.append(store_id)
        if known_at:
            conditions.append("recorded_at<=?")
            params.append(_timestamp(known_at))
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        rows = self.connection.execute("SELECT payload FROM (SELECT payload,ROW_NUMBER() OVER(PARTITION BY store_id,event_id ORDER BY revision DESC) AS rank FROM ra_events" + where + ") WHERE rank=1", params)
        return [FinanceEvent.from_dict(json.loads(row["payload"])) for row in rows]

    def set_coverage(self, store_id: str, currency: str, metric: str, start: str, end: str,
                     complete: bool, evidence: Iterable[str] = (), *, recorded_at: str | None = None) -> None:
        if metric not in METRICS:
            raise RunningAccountError("Coverage must refer to a base metric")
        start, end = _date(start), _date(end)
        if end < start:
            raise RunningAccountError("Coverage end precedes start")
        evidence_text = _json(sorted(set(evidence)))
        old = self.connection.execute("SELECT * FROM ra_coverage WHERE store_id=? AND currency=? AND metric=? AND start_date=? AND end_date=? ORDER BY seq DESC LIMIT 1",
                                      (store_id, currency, metric, start, end)).fetchone()
        if old and old["complete"] == int(complete) and old["evidence"] == evidence_text:
            return
        with self.connection:
            self.connection.execute("INSERT INTO ra_coverage(store_id,currency,metric,start_date,end_date,complete,evidence,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                                    (store_id, currency, metric, start, end, int(complete), evidence_text, _timestamp(recorded_at)))

    def _covered(self, store_id: str, currency: str, metric: str, start: str | None, end: str, known_at: str | None) -> bool:
        if start is None:
            return False
        params: list[Any] = [store_id, currency, metric, start, end]
        sql = "SELECT * FROM ra_coverage WHERE store_id=? AND currency=? AND metric=? AND end_date>=? AND start_date<=?"
        if known_at:
            sql += " AND recorded_at<=?"
            params.append(_timestamp(known_at))
        rows = list(self.connection.execute(sql + " ORDER BY seq", params))
        # Boundary intervals avoid iterating every calendar day, even for long histories.
        left, right = date.fromisoformat(start).toordinal(), date.fromisoformat(end).toordinal() + 1
        boundaries = {left, right}
        for row in rows:
            boundaries.update((max(left, date.fromisoformat(row["start_date"]).toordinal()),
                               min(right, date.fromisoformat(row["end_date"]).toordinal() + 1)))
        for point in sorted(boundaries)[:-1]:
            candidates = [row for row in rows if date.fromisoformat(row["start_date"]).toordinal() <= point <= date.fromisoformat(row["end_date"]).toordinal()]
            if not candidates or not candidates[-1]["complete"]:
                return False
        return True

    def refresh(self, store_ids: Iterable[str] | None = None) -> dict[str, Any]:
        """Rebuild only stores with changed evidence; retries reuse daily projections."""
        stores = list(store_ids) if store_ids is not None else [row[0] for row in self.connection.execute("SELECT DISTINCT store_id FROM ra_events")]
        changed = []
        for store_id in stores:
            with self.connection:
                self.connection.execute("BEGIN IMMEDIATE")
                seq = self.connection.execute("SELECT COALESCE(MAX(seq),0) FROM ra_events WHERE store_id=?", (store_id,)).fetchone()[0]
                old = self.connection.execute("SELECT event_seq,projection_version FROM ra_projection WHERE store_id=?", (store_id,)).fetchone()
                if old and old[0] == seq and old[1] == PROJECTION_VERSION:
                    continue
                postings, issues = materialize_events(self.current_events(store_id))
                daily: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0])
                for event in postings:
                    for metric, value in event["amounts"].items():
                        target = daily[(event["currency"], event["event_date"], metric)]
                        if value is None:
                            target[1] += 1
                        else:
                            target[0] += int(money(value) * 100)
                self.connection.execute("DELETE FROM ra_daily WHERE store_id=?", (store_id,))
                self.connection.executemany("INSERT INTO ra_daily VALUES(?,?,?,?,?,?)", [(store_id, *key, *value) for key, value in sorted(daily.items())])
                self.connection.execute("INSERT OR REPLACE INTO ra_projection VALUES(?,?,?,?)", (store_id, seq, _json(issues), PROJECTION_VERSION))
                changed.append(store_id)
        return {"rebuilt_stores": changed, "changed": bool(changed)}

    def _view(self, store_id: str, currency: str, start: str | None, end: str,
              known_at: str | None, postings: list[dict[str, Any]] | None) -> dict[str, Any]:
        if start and start > end:
            return {"start": start, "end": end, "metrics": _derive({metric: _metric(ZERO, True) for metric in METRICS}),
                    "status": "FINAL"}
        totals: dict[str, list[Any]] = {metric: [ZERO, 0] for metric in METRICS}
        if postings is not None:
            for event in postings:
                if event["store_id"] != store_id or event["currency"] != currency or event["event_date"] > end or (start and event["event_date"] < start):
                    continue
                for metric, value in event["amounts"].items():
                    if value is None:
                        totals[metric][1] += 1
                    else:
                        totals[metric][0] += Decimal(value)
        else:
            sql = "SELECT metric,SUM(amount_cents) AS amount,SUM(unknown_count) AS unknown_count FROM ra_daily WHERE store_id=? AND currency=? AND business_date<=?"
            params: list[Any] = [store_id, currency, end]
            if start:
                sql += " AND business_date>=?"
                params.append(start)
            for row in self.connection.execute(sql + " GROUP BY metric", params):
                totals[row["metric"]] = [Decimal(row["amount"]) / 100, row["unknown_count"]]
        metrics = _derive({metric: _metric(value[0], not value[1] and self._covered(store_id, currency, metric, start, end, known_at)) for metric, value in totals.items()})
        return {"start": start, "end": end, "metrics": metrics,
                "status": "FINAL" if all(value["complete"] for value in metrics.values()) else "PENDING_DATA"}

    def report(self, start: str, end: str, store_ids: Iterable[str], *, currency: str = "CNY",
               known_at: str | None = None) -> dict[str, Any]:
        start, end = _date(start), _date(end)
        if end < start:
            raise RunningAccountError("Report end precedes start")
        stores = list(dict.fromkeys(store_ids))
        if not stores or "ALL" in stores:
            raise RunningAccountError("Supply the complete explicit store scope")
        postings = None
        issues: list[dict[str, Any]] = []
        if known_at:
            postings, issues = materialize_events(self.current_events(known_at=known_at))
            issues = [item for item in issues if item.get("event_date", end) <= end
                      and item.get("store_id") in stores and item.get("currency", currency) == currency]
        else:
            self.refresh(stores)
        result: dict[str, Any] = {"schema_version": 1, "currency": currency, "period_start": start,
                                  "through": end, "known_at": _timestamp(known_at) if known_at else None,
                                  "stores": {}, "issues": issues}
        for store_id in stores:
            registration = self.connection.execute("SELECT * FROM ra_stores WHERE store_id=?", (store_id,)).fetchone()
            opening = registration["opened_on"] if registration else None
            if registration and registration["currency"] != currency:
                raise RunningAccountError("Registered reporting currency differs; do not consolidate currencies")
            if not opening:
                result["issues"].append({"code": "STORE_OPENING_UNKNOWN", "store_id": store_id})
            if postings is None:
                earliest = self.connection.execute("SELECT MIN(business_date) FROM ra_daily WHERE store_id=? AND currency=? AND business_date<=?", (store_id, currency, end)).fetchone()[0]
            else:
                earliest = min((item["event_date"] for item in postings if item["store_id"] == store_id and item["currency"] == currency and item["event_date"] <= end), default=None)
            if earliest and opening and earliest < opening:
                result["issues"].append({"code": "EVENT_PRECEDES_STORE_OPENING", "store_id": store_id,
                                          "registered_opening": opening, "earliest_event": earliest})
                # Retain known earlier facts while refusing a false complete lifetime total.
                opening = None
            if not known_at:
                projection = self.connection.execute("SELECT issues FROM ra_projection WHERE store_id=?", (store_id,)).fetchone()
                if projection:
                    result["issues"].extend(item for item in json.loads(projection["issues"])
                                            if item.get("event_date", end) <= end and item.get("currency", currency) == currency)
            period_start = max(start, opening) if opening else start
            period_view = self._view(store_id, currency, period_start, end, known_at, postings)
            cumulative_view = self._view(store_id, currency, opening, end, known_at, postings)
            if period_start <= end:
                prior_day = (date.fromisoformat(period_start) - timedelta(days=1)).isoformat()
                prior_view = self._view(store_id, currency, opening, prior_day, known_at, postings)
                # Excess is nonlinear. Recognize its change during the period,
                # not max(period freight - period reserve, 0), so closed weeks
                # reconcile exactly to the cumulative result across refunds.
                excess_delta = {}
                for name in _RETURN_EXCESS_COMPONENTS:
                    current_metric = cumulative_view["metrics"][name]
                    prior_metric = prior_view["metrics"][name]
                    excess_delta[name] = _metric(
                        Decimal(current_metric["known_subtotal"]) - Decimal(prior_metric["known_subtotal"]),
                        current_metric["complete"] and prior_metric["complete"],
                    )
                period_view["metrics"] = _derive(
                    {metric: period_view["metrics"][metric] for metric in METRICS}, excess_values=excess_delta,
                )
                period_view["status"] = "FINAL" if all(value["complete"] for value in period_view["metrics"].values()) else "PENDING_DATA"
            result["stores"][store_id] = {"period": period_view, "cumulative": cumulative_view}
        combined = {}
        for view in ("period", "cumulative"):
            values = {}
            for metric in METRICS:
                entries = [result["stores"][store][view]["metrics"][metric] for store in stores]
                values[metric] = _metric(sum((Decimal(entry["known_subtotal"]) for entry in entries), ZERO), all(entry["complete"] for entry in entries))
            # Clamp spendable per store; a surplus in one store cannot fund another store.
            excess_sum = {}
            for name in _RETURN_EXCESS_COMPONENTS:
                entries = [result["stores"][store][view]["metrics"][name] for store in stores]
                excess_sum[name] = _metric(sum((Decimal(entry["known_subtotal"]) for entry in entries), ZERO),
                                          all(entry["complete"] for entry in entries))
            values = _derive(values, excess_values=excess_sum)
            spendable = [result["stores"][store][view]["metrics"]["advertising_spendable"] for store in stores]
            values["advertising_spendable"] = _metric(sum((Decimal(entry["known_subtotal"]) for entry in spendable), ZERO), all(entry["complete"] for entry in spendable))
            combined[view] = {"metrics": values, "status": "FINAL" if all(entry["complete"] for entry in values.values()) else "PENDING_DATA"}
        result["stores"]["ALL"] = combined
        result["status"] = "FINAL" if not result["issues"] and all(view["status"] == "FINAL" for store in result["stores"].values() for view in store.values()) else "PENDING_DATA"
        return result

    def save_report_snapshot(self, report: Mapping[str, Any]) -> dict[str, str]:
        payload = _json(report)
        report_id = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO ra_snapshots VALUES(?,?,?)", (report_id, _timestamp(), payload))
        return {"report_id": report_id, "status": str(report.get("status", "PENDING_DATA"))}

    def read_report_snapshot(self, report_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT payload FROM ra_snapshots WHERE report_id=?", (report_id,)).fetchone()
        if not row:
            raise RunningAccountError("Unknown report snapshot")
        return json.loads(row[0])
