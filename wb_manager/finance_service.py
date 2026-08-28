from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .client import WbClient
from .finance_calculations import calculate_financial_report
from .finance_costs import normalize_product_key, parse_cost_quote
from .finance_funds import (
    AdvertisingAccrual,
    DepositCommitment,
    build_funding_plan,
    preflight_deposit_execution,
    validate_advertising_deposit_plan,
)
from .finance_ledger import FinanceLedger, canonical_json, normalize_iso
from .finance_periods import (
    FinancePeriod,
    parse_report_period,
    report_maturity_blocker,
)
from .finance_policy import (
    ADVERTISING_DEPOSIT_CAP_RULE,
    load_active_phone_financial_policy,
)
from .finance_reporting import write_report_artifacts
from .finance_sources import (
    ADVERTISING_BASE_URL,
    SELLER_BALANCE_URL,
    StoreSpec,
    deposit_campaign_budget,
    load_store_specs,
    resolve_store_credentials,
)
from .finance_sync import (
    MOSCOW,
    PRIMARY_SOURCES,
    read_registry,
    register_enabled_stores,
    resolve_through,
    sync_enabled_stores,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = Path(r"E:\电商运营\03运营数据\WB财务\reports")
_ZERO = Decimal("0")
_CENT = Decimal("0.01")
_MAX_FUNDING_BALANCE_EVIDENCE_AGE = timedelta(hours=1)
_MAX_FUTURE_EVIDENCE_SKEW = timedelta(minutes=1)
_ADVERTISING_SOURCE_FIELDS = {
    "account": "balance",
    "balance": "net",
    "bonus": "bonus",
}
_FUNDABLE_CAMPAIGN_STATUSES = {4, 9, 11}
_LOCKING_ADVERTISING_ACTION_STATUSES = {
    "approved",
    "executing",
    "executed",
    "succeeded",
    "unknown",
}
_DELTA_MONEY_FIELDS = (
    "netTurnoverCny",
    "purchaseCostCny",
    "actualWbCommissionCny",
    "wbCommissionCny",
    "actualPaymentFeeCny",
    "paymentFeeCny",
    "forwardLogisticsCny",
    "storageAndOperationsCny",
    "otherPlatformCostCny",
    "forwardOperationsUsedCny",
    "managedOperationsCny",
    "combinedPoolCny",
    "returnLogisticsCny",
    "advertisingProvisionCny",
    "excessReturnLossCny",
    "ownerProfitCny",
)


class FinanceServiceError(ValueError):
    pass


class FinanceService:
    """End-to-end local controller for the multi-store WB finance ledger."""

    def __init__(
        self,
        *,
        db_path: Path = Path("data/finance/wb_finance.sqlite3"),
        registry_path: Path = Path("config/finance/stores.json"),
        output_root: Path | None = None,
        repo_root: Path = ROOT,
        now: datetime | None = None,
        source_factory=None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.registry_path = self._resolve_path(registry_path)
        self.output_root = (
            Path(output_root).resolve() if output_root is not None else DEFAULT_OUTPUT_ROOT
        )
        self.ledger = FinanceLedger(self._resolve_path(db_path))
        self.now = now
        self.source_factory = source_factory
        self.registry = read_registry(self.registry_path)
        self.policy = load_active_phone_financial_policy()
        self.store_aliases = register_enabled_stores(
            self.ledger,
            self.registry,
            registry_path=self.registry_path,
            repo_root=self.repo_root,
        )

    def close(self) -> None:
        self.ledger.close()

    def bootstrap(self, *, through: str | date = "yesterday") -> dict[str, Any]:
        summaries = sync_enabled_stores(
            self.ledger,
            registry_path=self.registry_path,
            repo_root=self.repo_root,
            through=through,
            bootstrap=True,
            source_factory=self.source_factory,
            now=self._now(),
        )
        report = self.report(kind="bootstrap", period="auto")
        return {
            "operation": "bootstrap",
            "stores": [summary.as_dict() for summary in summaries],
            "report": report,
        }

    def daily(self, *, through: str | date = "yesterday") -> dict[str, Any]:
        summaries = sync_enabled_stores(
            self.ledger,
            registry_path=self.registry_path,
            repo_root=self.repo_root,
            through=through,
            bootstrap=False,
            source_factory=self.source_factory,
            now=self._now(),
        )
        reports: list[dict[str, Any]] = []
        if not self.ledger.find_reports(report_type="bootstrap", report_status="FINAL"):
            reports.append(self.report(kind="bootstrap", period="auto"))
        reports.append(self.report(kind="weekly", period="auto"))
        reports.append(self.report(kind="monthly", period="auto"))
        current_keys = {str(item["reportKey"]) for item in reports}
        historical_reports: list[dict[str, Any]] = []
        historical_checked = 0
        if summaries:
            historical_reports, historical_checked = self._refresh_finalized_reports(
                exclude_report_keys=current_keys,
                affected_from=min(summary.requested_from for summary in summaries),
                affected_through=max(summary.requested_through for summary in summaries),
            )
        reports.extend(historical_reports)
        return {
            "operation": "daily",
            "through": resolve_through(through, now=self._now()).isoformat(),
            "stores": [summary.as_dict() for summary in summaries],
            "reports": reports,
            "historicalReportsChecked": historical_checked,
        }

    def cost_import(
        self,
        *,
        quote: Path,
        effective_from: str,
        approved_by: str,
        rub_per_cny: str | Decimal | None = None,
    ) -> dict[str, Any]:
        approver = str(approved_by or "").strip()
        if not approver:
            raise FinanceServiceError("--approved-by is required")
        rate = rub_per_cny
        if rate is None:
            rate = (
                self.policy.raw.get("costs", {})
                .get("purchaseCost", {})
                .get("templateExchangeRateRubPerCny")
            )
        if rate in (None, ""):
            raise FinanceServiceError("RUB/CNY rate is missing from both CLI and policy")
        rows = parse_cost_quote(
            quote,
            effective_from=effective_from,
            rub_per_cny=rate,
        )
        existing_approvals = self.ledger.connection.execute(
            f"SELECT cost_version_uid, approved_by, approved_at FROM cost_versions "
            f"WHERE cost_version_uid IN ({', '.join('?' for _ in rows)})",
            [row.version_id for row in rows],
        ).fetchall() if rows else []
        if existing_approvals:
            if len(existing_approvals) != len(rows):
                raise FinanceServiceError(
                    "The quote import is only partially present; manual ledger review is required"
                )
            if {str(item["approved_by"]) for item in existing_approvals} != {approver}:
                raise FinanceServiceError("The existing cost import has a different approver")
            approval_times = {str(item["approved_at"]) for item in existing_approvals}
            if len(approval_times) != 1:
                raise FinanceServiceError("The existing quote rows have inconsistent approval times")
            approved_at = approval_times.pop()
        else:
            approved_at = self._now().isoformat()
        records = []
        for row in rows:
            record = row.as_dict()
            record.update(
                {
                    "approvedBy": approver,
                    "approvedAt": approved_at,
                    "sourceCurrency": "RUB",
                    "skuAliases": sorted({row.product_key, row.specification}),
                }
            )
            records.append(record)
        internal_ids = self.ledger.import_cost_versions(records)
        manifest = {
            "schemaVersion": 1,
            "quote": str(Path(quote).resolve()),
            "quoteSha256": rows[0].source_hash if rows else None,
            "effectiveFrom": rows[0].effective_from.isoformat() if rows else None,
            "approvedBy": approver,
            "approvedAt": approved_at,
            "exchangeRateRubPerCny": str(rate),
            "costVersions": [
                {**row.as_dict(), "internalLedgerId": identifier}
                for row, identifier in zip(rows, internal_ids)
            ],
        }
        manifest_dir = self.ledger.path.parent / "cost-imports"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = manifest_dir / (
            f"{rows[0].source_hash[:16]}-{rows[0].effective_from.date().isoformat()}.json"
        )
        if manifest_path.exists():
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            # Approval time is audit metadata and may differ on an idempotent
            # retry; immutable cost content itself must remain identical.
            comparable_existing = dict(existing)
            comparable_manifest = dict(manifest)
            comparable_existing.pop("approvedAt", None)
            comparable_manifest.pop("approvedAt", None)
            if comparable_existing != comparable_manifest:
                raise FinanceServiceError(f"Cost import manifest differs: {manifest_path}")
        else:
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        return {
            "operation": "cost-import",
            "quote": str(Path(quote).resolve()),
            "quoteSha256": rows[0].source_hash if rows else None,
            "effectiveFrom": rows[0].effective_from.isoformat() if rows else None,
            "approvedBy": approver,
            "approvedAt": approved_at,
            "exchangeRateRubPerCny": str(rate),
            "rowCount": len(rows),
            "manifest": str(manifest_path),
            "costVersionIdSample": [row.version_id for row in rows[:5]],
        }

    def report(
        self,
        *,
        kind: str,
        period: str,
        reason: str | None = None,
        _selected_period: FinancePeriod | None = None,
    ) -> dict[str, Any]:
        if _selected_period is None:
            first_monthly = kind == "monthly" and not any(
                report
                for status in ("FINAL", "RESTATED")
                for report in self.ledger.find_reports(
                    report_type="monthly", report_status=status
                )
            )
            selected = parse_report_period(
                kind=kind,
                value=period,
                registry=self.registry,
                today=self._now().astimezone(MOSCOW).date(),
                first_monthly=first_monthly,
            )
        else:
            if _selected_period.kind != kind:
                raise FinanceServiceError("Selected report period kind does not match")
            selected = _selected_period
        selected = self._adjust_cumulative_start(selected)
        evidence = self._load_period_evidence(selected)
        calculated = calculate_financial_report(
            order_rows=evidence["order"],
            finance_rows=evidence["finance"],
            return_rows=evidence["return"],
            advertising_events=evidence["advertising"],
            cost_selector=self._select_cost,
            policy=self.policy,
        )
        full_calculation = calculated.to_dict()
        prior_calculation = self._prior_period_calculation(evidence, selected)
        blockers = list(full_calculation.get("blockers") or [])
        if prior_calculation is not None:
            blockers.extend(prior_calculation.get("blockers") or [])
        blockers.extend(self._source_blockers(selected, evidence))
        maturity = report_maturity_blocker(
            selected, today=self._now().astimezone(MOSCOW).date()
        )
        if maturity:
            blockers.append({"code": "SETTLEMENT_NOT_MATURE", "message": maturity})
        blockers = _dedupe_blockers(blockers)
        calculation = self._period_delta_calculation(
            full_calculation,
            selected,
            prior_calculation=prior_calculation,
        )
        snapshot_hash = hashlib.sha256(
            canonical_json(
                {
                    "period": selected.as_dict(),
                    "policy": {
                        "reporting": self.policy.reporting_policy_id,
                        "pricing": self.policy.pricing_policy_id,
                    },
                    "calculation": calculation,
                    "sourceBlockers": blockers,
                }
            ).encode("utf-8")
        ).hexdigest()
        stable = self._record_report_stability(selected, evidence, blockers)
        if selected.kind in {"weekly", "monthly"} and not stable:
            blockers.append(
                {
                    "code": "SOURCE_NOT_STABLE_TWICE",
                    "message": "Official finance and advertising evidence must be complete and unchanged in two consecutive captures.",
                }
            )
        blockers = _dedupe_blockers(blockers)
        status = "PENDING_DATA" if blockers else "FINAL"
        report_key = f"{kind}:{selected.start.isoformat()}:{selected.end.isoformat()}"
        previous = self._latest_final_report(report_key)
        if status == "FINAL" and previous is not None:
            previous_hash = str((previous.get("metadata") or {}).get("calculationHash") or "")
            if previous_hash == snapshot_hash:
                return self._existing_report_result(previous)
            status = "RESTATED"

        payload = self._report_payload(
            calculation,
            selected,
            status=status,
            blockers=blockers,
        )
        persisted_orders = self._persist_order_profits(
            full_calculation.get("orders") or []
        )
        version = self._next_report_version(
            report_key,
            status=status,
            snapshot_hash=snapshot_hash,
        )
        report_orders = []
        for order in payload["orders"]:
            key = (str(order.get("storeAlias")), str(order.get("orderKey")))
            profit_version = persisted_orders.get(key)
            if profit_version is None:
                continue
            report_orders.append(
                {
                    "store_alias": key[0],
                    "order_key": key[1],
                    "profit_version": profit_version,
                    "owner_profit_cny": order.get("ownerProfitCny"),
                    "snapshot": order,
                }
            )
        totals = payload["totals"]
        previous_id = previous.get("report_id") if status == "RESTATED" and previous else None
        report_id = self.ledger.create_report(
            report_key,
            version,
            report_type=kind,
            period_start=_period_start(selected.start),
            period_end=_period_end_exclusive(selected.end),
            policy_id=self.policy.reporting_policy_id,
            totals={
                "revenue_cny": totals["netTurnoverCny"],
                "cost_cny": totals["purchaseCostCny"],
                "profit_cny": totals["ownerProfitCny"],
                "managed_operations_cny": totals["managedOperationsCny"],
                "return_fee_cny": totals["returnLogisticsCny"],
                "advertising_cny": totals["advertisingProvisionCny"],
            },
            orders=report_orders,
            generated_at=self._now(),
            metadata={
                "calculationHash": snapshot_hash,
                "blockers": blockers,
                "period": selected.as_dict(),
                "pricingPolicyId": self.policy.pricing_policy_id,
                "cumulative": selected.cumulative,
            },
            report_status=status,
            previous_report_id=previous_id,
            correction_reason=(
                (reason or "WB late refund/correction or financial-policy restatement")
                if status == "RESTATED"
                else None
            ),
            correction_metadata=(
                {"previousCalculationHash": (previous.get("metadata") or {}).get("calculationHash")}
                if status == "RESTATED" and previous
                else None
            ),
        )
        payload["reportId"] = report_id
        payload["reportVersion"] = version
        payload["status"] = status
        artifact_paths: dict[str, Any] | None = None
        if status in {"FINAL", "RESTATED"}:
            artifact_paths = self._write_report(payload, selected)
        return {
            "reportId": report_id,
            "reportKey": report_key,
            "reportVersion": version,
            "status": status,
            "period": selected.as_dict(),
            "orderCount": len(payload["orders"]),
            "blockers": blockers,
            "artifacts": artifact_paths,
        }

    def funds_plan(self, *, report_id: str) -> dict[str, Any]:
        report = self.ledger.get_report(report_id)
        if report is None:
            raise FinanceServiceError(f"Unknown report: {report_id}")
        if report["report_type"] != "weekly" or report["report_status"] not in {
            "FINAL",
            "RESTATED",
        }:
            raise FinanceServiceError("Funds plans require a FINAL or RESTATED weekly report")
        latest_weekly = self._latest_weekly_final_report()
        if latest_weekly is None:
            raise FinanceServiceError("No finalized weekly report is available")
        if str(latest_weekly["report_id"]) != report_id:
            raise FinanceServiceError(
                "The requested weekly report has been superseded; use the latest "
                f"FINAL/RESTATED weekly report: {latest_weekly['report_id']}"
            )
        activation = str(self.registry.get("fundingActivationAt") or "")
        if not activation:
            raise FinanceServiceError("Store registry has no fundingActivationAt")
        plans = []
        for store_alias in self.store_aliases:
            balance = self._latest_withdrawable_balance(store_alias)
            if balance is None:
                raise FinanceServiceError(
                    f"No platform withdrawable-balance evidence for {store_alias}"
                )
            balance_evidence_at = self._require_fresh_balance_evidence(
                balance,
                store_alias=store_alias,
            )
            accruals = self._advertising_accruals(
                store_alias,
                formal_activation_at=activation,
            )
            commitments = [
                DepositCommitment(
                    action_id=str(item["action_id"]),
                    amount_cny=Decimal(str(item["requested_amount_cny"])),
                    status=str(item["lifecycle_status"]),
                    readback_at=(
                        datetime.fromisoformat(str(item["executed_at"]))
                        if item.get("lifecycle_status") == "readback_confirmed"
                        and item.get("executed_at")
                        else None
                    ),
                )
                for item in self.ledger.query_funding_actions(
                    store_alias=store_alias, action_type="advertising_top_up"
                )
                if self._funding_action_locks_reserve(item)
            ]
            plan = build_funding_plan(
                report_id=report_id,
                store=store_alias,
                platform_available_balance_cny=balance["withdrawable_balance_cny"],
                balance_evidence_at=balance_evidence_at,
                formal_activation_at=activation,
                advertising_accruals=accruals,
                deposit_commitments=commitments,
                historical_bootstrap=False,
            )
            plan_dict = plan.as_dict()
            plans.append(plan_dict)
            self.ledger.record_funding_action(
                f"withdrawal-plan:{plan.fingerprint}",
                store_alias,
                action_type="withdrawal",
                requested_amount_cny=plan.suggested_withdrawal_cny,
                report_id=report_id,
                status="proposed",
                evidence=plan_dict,
            )
        output = {
            "schemaVersion": 1,
            "planType": "weekly_funding",
            "reportId": report_id,
            "generatedAt": self._now().isoformat(),
            "withdrawalExecutionSupported": False,
            "stores": plans,
        }
        digest = hashlib.sha256(canonical_json(output).encode("utf-8")).hexdigest()[:16]
        path = self.output_root / "funds" / f"{report_id}-{digest}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing != output:
                raise FinanceServiceError(f"Funding plan artifact differs: {path}")
        else:
            path.write_text(
                json.dumps(output, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        return {**output, "path": str(path)}

    def ads_deposit(
        self,
        *,
        plan_path: Path,
        approved_by: str,
        confirm_live: bool,
    ) -> dict[str, Any]:
        payload = validate_advertising_deposit_plan(
            json.loads(Path(plan_path).read_text(encoding="utf-8-sig"))
        )
        if payload["approvedBy"] != str(approved_by).strip():
            raise FinanceServiceError("CLI approver must exactly match the immutable plan")
        if payload["store"] not in self.store_aliases:
            raise FinanceServiceError(f"Deposit plan store is not enabled: {payload['store']}")

        existing = self.ledger.query_funding_actions(
            store_alias=payload["store"],
            report_id=payload["reportId"],
            action_type="advertising_top_up",
        )
        matching = [
            item
            for item in existing
            if str(item["idempotency_key"]) == f"ads-deposit:{payload['fingerprint']}"
        ]
        if not confirm_live:
            self._require_current_deposit_report(payload["reportId"])
            self._require_governed_deposit_cap(
                payload,
                exclude_action_id=(str(matching[0]["action_id"]) if matching else None),
            )
            proposal_preflight = preflight_deposit_execution(
                payload,
                current_status="approved",
                now=self._now(),
            )
            if not proposal_preflight.allowed:
                raise FinanceServiceError(proposal_preflight.reason)
            if matching:
                action = matching[0]
                if str(action["lifecycle_status"]) != "proposed":
                    raise FinanceServiceError(
                        "This exact proposal already left proposed status; create a new plan"
                    )
                action_id = str(action["action_id"])
                idempotent = True
            else:
                action_id = self.ledger.record_funding_action(
                    f"ads-deposit:{payload['fingerprint']}",
                    payload["store"],
                    action_type="advertising_top_up",
                    requested_amount_cny=payload["amountCny"],
                    report_id=payload["reportId"],
                    status="proposed",
                    requested_at=payload["approvedAt"],
                    evidence=payload,
                )
                idempotent = False
            return {
                "status": "proposal_recorded",
                "live": False,
                "actionId": action_id,
                "idempotent": idempotent,
                "planFingerprint": payload["fingerprint"],
                "expiresAt": payload["expiresAt"],
                "automaticRetryAllowed": False,
                "nextStep": (
                    "Review the exact store, campaign, amount, currency, source balance, "
                    "policy cap, advertising reserve and expiry, then repeat with --confirm-live."
                ),
            }
        if not matching:
            raise FinanceServiceError(
                "A matching prior dry-run proposal is required before --confirm-live"
            )
        action = matching[0]
        if str(action["lifecycle_status"]) in {"proposed", "approved"}:
            self._require_current_deposit_report(payload["reportId"])
        owner = f"finance-{uuid.uuid4()}"
        lock_name = f"wb-advertising-funds:{payload['store']}"
        # The strict preflight performs four read calls, followed by one write
        # and one readback. Keep the shared money lock alive beyond the worst
        # supported request-timeout envelope so another writer cannot enter
        # while this operation is still resolving.
        if not self.ledger.acquire_lock(lock_name, owner, ttl_seconds=900, now=self._now()):
            raise FinanceServiceError("Another finance/advertising funding operation holds the store lock")
        try:
            action = next(
                item
                for item in self.ledger.query_funding_actions(
                    store_alias=payload["store"],
                    action_type="advertising_top_up",
                )
                if item["action_id"] == action["action_id"]
            )
            self._require_exact_proposal(action, payload)
            lifecycle_status = str(action["lifecycle_status"])
            if lifecycle_status in {"proposed", "approved"}:
                try:
                    self._require_governed_deposit_cap(
                        payload,
                        exclude_action_id=str(action["action_id"]),
                    )
                except Exception as exc:
                    self._cancel_pre_post_action(action, payload, str(exc))
                    raise
            try:
                credentials = self._promotion_credentials(payload["store"])
                client = WbClient(credentials.token)
            except Exception as exc:
                if lifecycle_status in {"proposed", "approved"}:
                    self._cancel_pre_post_action(action, payload, str(exc))
                raise
            if lifecycle_status in {"unknown", "executing", "executed", "succeeded"}:
                return self._resolve_deposit_readback_only(client, payload, action)
            check_status = "approved" if lifecycle_status == "proposed" else lifecycle_status
            preflight = preflight_deposit_execution(
                payload,
                current_status=check_status,
                readback_complete=lifecycle_status == "readback_confirmed",
                now=self._now(),
            )
            if not preflight.allowed:
                if lifecycle_status in {"proposed", "approved"}:
                    self._cancel_pre_post_action(action, payload, preflight.reason)
                raise FinanceServiceError(preflight.reason)

            try:
                live_preflight = self._live_advertising_deposit_preflight(
                    client,
                    payload,
                    exclude_action_id=str(action["action_id"]),
                )
                preflight = preflight_deposit_execution(
                    payload,
                    current_status="approved",
                    readback_complete=False,
                    now=self._now(),
                )
            except Exception as exc:
                self._cancel_pre_post_action(action, payload, str(exc))
                raise
            if not preflight.allowed:
                self._cancel_pre_post_action(action, payload, preflight.reason)
                raise FinanceServiceError(preflight.reason)
            if lifecycle_status == "proposed":
                self.ledger.update_funding_action_status(
                    action["action_id"],
                    "approved",
                    occurred_at=self._now(),
                    external_reference=payload["fingerprint"],
                    evidence={
                        "planFingerprint": payload["fingerprint"],
                        "approvedBy": approved_by,
                        "proposalActionId": action["action_id"],
                    },
                )
            self.ledger.update_funding_action_status(
                action["action_id"],
                "executing",
                evidence={
                    "planFingerprint": payload["fingerprint"],
                    "livePreflight": live_preflight,
                },
            )
            result = deposit_campaign_budget(
                client,
                campaign_id=int(payload["campaign"]),
                amount=int(Decimal(payload["amountCny"])),
                source=payload["source"],
                approved_by=approved_by,
                confirm_live=True,
            )
            final_status = result.state
            readback: dict[str, Any] | None = None
            if result.state in {"succeeded", "unknown"}:
                expected_total = (
                    Decimal(live_preflight["campaignBudgetBeforeCny"])
                    + Decimal(payload["amountCny"])
                ).quantize(_CENT)
                readback = self._campaign_budget_readback(
                    client,
                    payload,
                    expected_total=expected_total,
                )
                final_status = (
                    "readback_confirmed" if readback["matched"] else "unknown"
                )
            self.ledger.update_funding_action_status(
                action["action_id"],
                final_status,
                external_reference=payload["fingerprint"],
                evidence={
                    "httpStatus": result.status,
                    "requiresReadback": result.requires_readback,
                    "livePreflight": live_preflight,
                    "readback": readback,
                    "error": result.error,
                },
            )
            return {
                "actionId": action["action_id"],
                "status": final_status,
                "store": payload["store"],
                "campaign": payload["campaign"],
                "amountCny": payload["amountCny"],
                "readback": readback,
                "automaticRetryAllowed": False,
            }
        finally:
            self.ledger.release_lock(lock_name, owner)

    def _resolve_path(self, path: Path) -> Path:
        value = Path(path)
        return value if value.is_absolute() else self.repo_root / value

    def _now(self) -> datetime:
        value = self.now or datetime.now(SHANGHAI)
        return value if value.tzinfo is not None else value.replace(tzinfo=SHANGHAI)

    def _select_cost(self, sku: str, created_at: str, *, order_completed_at=None):
        normalized = normalize_product_key(sku)
        selected = self.ledger.select_cost_version(
            normalized,
            created_at,
            order_completed_at=order_completed_at,
        )
        return {
            "costVersionId": selected.cost_version_uid,
            "internalCostVersionId": selected.cost_version_id,
            "purchaseCostCny": selected.purchase_cost_cny,
            "effectiveFrom": selected.effective_from,
            "effectiveTo": selected.effective_to,
        }

    def _load_period_evidence(self, period: FinancePeriod) -> dict[str, list[dict[str, Any]]]:
        finance = self._payload_rows("raw_finance")
        orders = self._payload_rows("raw_sales")
        returns = self._payload_rows("raw_returns")
        combined = [
            *(dict(row, _source="finance") for row in finance),
            *(dict(row, _source="order") for row in orders),
            *(dict(row, _source="return") for row in returns),
        ]
        from .finance_calculations import build_order_groups

        grouping = build_order_groups(combined, source="evidence")
        selected_groups = []
        for group in grouping.groups:
            if any(_record_in_period(record.row, period) for record in group.records):
                selected_groups.append(group)
        output: dict[str, list[dict[str, Any]]] = {
            "finance": [],
            "order": [],
            "return": [],
            "advertising": [],
        }
        end_exclusive = _period_end_exclusive(period.end)
        for group in selected_groups:
            for record in group.records:
                if not _record_at_or_before(record.row, end_exclusive):
                    continue
                source = str(record.row.get("_source") or "order")
                row = {key: value for key, value in record.row.items() if key != "_source"}
                output[source].append(row)
        for row in self._payload_rows("raw_ad"):
            occurred = _parse_datetime(row.get("occurredAt") or row.get("updTime"))
            if occurred and period.start <= occurred.astimezone(MOSCOW).date() <= period.end:
                output["advertising"].append(row)
        return output

    def _payload_rows(self, table: str) -> list[dict[str, Any]]:
        if table not in {"raw_finance", "raw_sales", "raw_returns", "raw_ad"}:
            raise FinanceServiceError(f"Unsupported evidence table: {table}")
        rows = self.ledger.connection.execute(
            f"SELECT * FROM {table} ORDER BY store_alias"
        ).fetchall()
        result = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            payload.setdefault("storeAlias", row["store_alias"])
            if table == "raw_finance":
                if row["buyer_paid_cny"] is not None:
                    payload["buyerPaidCny"] = row["buyer_paid_cny"]
                if row["commission_cny"] is not None:
                    payload["commissionCny"] = row["commission_cny"]
                if row["acquiring_fee_cny"] is not None:
                    payload["paymentFeeCny"] = row["acquiring_fee_cny"]
                if payload.get("paidStorage") not in (None, ""):
                    payload["storageCny"] = payload["paidStorage"]
                if payload.get("paidAcceptance") not in (None, ""):
                    payload["acceptanceFeeCny"] = payload["paidAcceptance"]
                if payload.get("additionalPayment") not in (None, ""):
                    payload["otherPlatformFeeCny"] = str(
                        -_decimal(payload["additionalPayment"])
                    )
            result.append(payload)
        return result

    def _source_blockers(
        self,
        period: FinancePeriod,
        evidence: Mapping[str, Sequence[Mapping[str, Any]]],
    ) -> list[dict[str, str]]:
        blockers: list[dict[str, str]] = []
        end = normalize_iso(_period_end_inclusive(period.end))
        for alias in self.store_aliases:
            store_start = self._store_reporting_start(alias, period.start)
            if store_start > period.end:
                continue
            start = normalize_iso(_period_start(store_start))
            for source in PRIMARY_SOURCES:
                if source == "seller_balance":
                    if self._latest_withdrawable_balance(alias) is None:
                        blockers.append(
                            {
                                "code": "MISSING_BALANCE_EVIDENCE",
                                "message": f"{alias} has no seller-balance evidence.",
                                "storeAlias": alias,
                                "source": source,
                            }
                        )
                    continue
                row = self.ledger.connection.execute(
                    """
                    SELECT status FROM sync_runs
                    WHERE store_alias=? AND source=? AND requested_from<=? AND requested_through>=?
                    ORDER BY completed_at DESC LIMIT 1
                    """,
                    (alias, source, start, end),
                ).fetchone()
                if row is None or row["status"] != "complete":
                    blockers.append(
                        {
                            "code": "MISSING_SOURCE_COVERAGE",
                            "message": f"{alias}/{source} is not complete for the report period.",
                            "storeAlias": alias,
                            "source": source,
                        }
                    )
            for gap in self.ledger.list_open_gaps(alias):
                if str(gap["gap_from"]) < end and str(gap["gap_through"]) >= start:
                    blockers.append(
                        {
                            "code": "OPEN_SYNC_GAP",
                            "message": f"{alias}/{gap['source']} has an unresolved collection gap.",
                            "storeAlias": alias,
                            "source": gap["source"],
                        }
                    )
        return blockers

    def _record_report_stability(
        self,
        period: FinancePeriod,
        evidence: Mapping[str, Sequence[Mapping[str, Any]]],
        blockers: Sequence[Mapping[str, Any]],
    ) -> bool:
        end = _period_end_exclusive(period.end)
        all_stable = True
        for alias in self.store_aliases:
            store_start = self._store_reporting_start(alias, period.start)
            if store_start > period.end:
                continue
            start = _period_start(store_start)
            for source, key in (
                ("finance_detailed", "finance"),
                ("advertising_costs", "advertising"),
            ):
                rows = [row for row in evidence[key] if row.get("storeAlias") == alias]
                complete = not any(
                    str(item.get("storeAlias") or "") == alias
                    and str(item.get("source") or "") == source
                    and str(item.get("code") or "")
                    in {"MISSING_SOURCE_COVERAGE", "OPEN_SYNC_GAP"}
                    for item in blockers
                )
                digest = hashlib.sha256(canonical_json(rows).encode("utf-8")).hexdigest()
                self.ledger.record_source_coverage(
                    alias,
                    f"report:{source}",
                    start,
                    end,
                    coverage_status="complete" if complete else "missing",
                    row_count=len(rows),
                    snapshot_hash=digest if complete else None,
                    captured_at=self._now(),
                    metadata={"reportKind": period.kind},
                )
                if period.kind in {"weekly", "monthly"} and not self.ledger.has_consecutive_stable_snapshot(
                    alias,
                    f"report:{source}",
                    start,
                    end,
                    minimum_count=2,
                ):
                    all_stable = False
        return all_stable

    def _store_reporting_start(self, store_alias: str, requested: date) -> date:
        history_start = date.fromisoformat(
            str(self.registry.get("officialHistoryStart") or "2024-01-29")[:10]
        )
        store = next(
            (
                item
                for item in self.registry.get("stores", [])
                if isinstance(item, Mapping)
                and str(item.get("alias") or "") == store_alias
            ),
            None,
        )
        if store is not None and store.get("openedAt"):
            history_start = max(
                history_start,
                date.fromisoformat(str(store["openedAt"])[:10]),
            )
        return max(requested, history_start)

    def _period_delta_calculation(
        self,
        calculation: Mapping[str, Any],
        period: FinancePeriod,
        *,
        prior_calculation: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        result = deepcopy(dict(calculation))
        if period.cumulative or period.kind not in {"weekly", "monthly"}:
            return result

        prior_by_order = {
            (str(value.get("storeAlias") or ""), str(value.get("orderKey") or "")): dict(value)
            for value in (prior_calculation or {}).get("orders") or []
        }
        orders: list[dict[str, Any]] = []
        for value in result.get("orders") or []:
            current = dict(value)
            prior = prior_by_order.get(
                (
                    str(current.get("storeAlias") or ""),
                    str(current.get("orderKey") or ""),
                )
            )
            if prior is None:
                current["periodRecognition"] = "INITIAL"
                orders.append(current)
                continue
            orders.append(_order_period_delta(current, prior))

        issues = [dict(value) for value in result.get("issues") or []]
        aliases = sorted(
            {str(order.get("storeAlias") or "") for order in orders}
            | {
                str(issue.get("storeAlias") or "")
                for issue in issues
                if issue.get("storeAlias")
            }
        )
        stores = [
            _summarize_order_rows(
                alias,
                [order for order in orders if order.get("storeAlias") == alias],
                [issue for issue in issues if issue.get("storeAlias") == alias],
            )
            for alias in aliases
            if alias
        ]
        result["orders"] = orders
        result["stores"] = stores
        result["totals"] = _summarize_order_rows("ALL", orders, issues)
        return result

    def _prior_period_calculation(
        self,
        evidence: Mapping[str, Sequence[Mapping[str, Any]]],
        period: FinancePeriod,
    ) -> dict[str, Any] | None:
        if period.cumulative or period.kind not in {"weekly", "monthly"}:
            return None
        boundary = _period_start(period.start)
        prior = calculate_financial_report(
            order_rows=[
                row for row in evidence["order"] if _record_at_or_before(row, boundary)
            ],
            finance_rows=[
                row for row in evidence["finance"] if _record_at_or_before(row, boundary)
            ],
            return_rows=[
                row for row in evidence["return"] if _record_at_or_before(row, boundary)
            ],
            advertising_events=[],
            cost_selector=self._select_cost,
            policy=self.policy,
        )
        return prior.to_dict()

    def _report_payload(
        self,
        calculation: Mapping[str, Any],
        period: FinancePeriod,
        *,
        status: str,
        blockers: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        orders = []
        for value in calculation.get("orders") or []:
            row = dict(value)
            row["actualAdvertisingAllocatedCny"] = row.get("actualAdvertisingSpendCny")
            row["otherCostCny"] = row.get("otherPlatformCostCny")
            orders.append(row)
        stores = [dict(value) for value in calculation.get("stores") or []]
        present_aliases = {str(value.get("storeAlias") or "") for value in stores}
        for alias in self.store_aliases:
            if alias in present_aliases:
                continue
            if self._store_reporting_start(alias, period.start) > period.end:
                continue
            stores.append(_empty_store_summary(alias))
        stores.sort(key=lambda value: str(value.get("storeAlias") or ""))
        raw_totals = dict(calculation.get("totals") or {})
        platform = sum(
            (
                _decimal(raw_totals.get(key))
                for key in (
                    "wbCommissionCny",
                    "paymentFeeCny",
                    "forwardOperationsCny",
                    "otherPlatformCostCny",
                )
            ),
            _ZERO,
        )
        totals = {
            **raw_totals,
            "netTurnoverCny": _money(raw_totals.get("netTurnoverCny")),
            "purchaseCostCny": _money(raw_totals.get("purchaseCostCny")),
            "platformAndOperationsCostCny": _money(platform),
            "managedOperationsCny": _money(raw_totals.get("managedOperationsCny")),
            "returnLogisticsCny": _money(raw_totals.get("returnLogisticsCny")),
            "advertisingProvisionCny": _money(raw_totals.get("advertisingProvisionCny")),
            "ownerProfitCny": _money(raw_totals.get("ownerProfitCny")),
        }
        return {
            "reportId": "pending",
            "status": status,
            "kind": period.kind,
            "generatedAt": self._now().isoformat(),
            "period": period.as_dict(),
            "policy": {
                **dict(calculation.get("policy") or {}),
                "paymentFallbackRate": str(
                    self.policy.raw["costs"]["paymentAndWithdrawal"]["fallbackRate"]
                ),
                "fixedOperationsFallbackCny": str(
                    self.policy.raw["costs"]["fixedOperations"]["fallbackPerCompletedUnitCny"]
                ),
            },
            "orders": orders,
            "stores": stores,
            "totals": totals,
            "costVersions": self._used_cost_versions(orders),
            "funding": self._report_funding_rows(stores),
            "blockers": list(blockers),
            "issues": list(calculation.get("issues") or []),
        }

    def _persist_order_profits(self, orders: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str], str]:
        persisted: dict[tuple[str, str], str] = {}
        for order in orders:
            if order.get("evidenceStatus") != "COMPLETE" or order.get("ownerProfitCny") is None:
                continue
            selected = self.ledger.select_cost_version(
                normalize_product_key(order["sku"]), order["orderCreatedAt"]
            )
            version = self.ledger.upsert_order_profit(
                order["storeAlias"],
                order["orderKey"],
                order_created_at=order["orderCreatedAt"],
                completed_at=order.get("completedAt"),
                sku=normalize_product_key(order["sku"]),
                cost_version_id=selected.cost_version_id,
                policy_id=self.policy.reporting_policy_id,
                revenue_cny=order["netTurnoverCny"],
                purchase_cost_cny=order["purchaseCostCny"],
                commission_cny=order["wbCommissionCny"],
                managed_operations_cny=order["managedOperationsCny"],
                combined_pool_cny=order["combinedPoolCny"],
                actual_return_logistics_cny=order["returnLogisticsCny"],
                actual_ad_spend_reconciliation_cny=order.get("actualAdvertisingSpendCny"),
                advertising_provision_cny=order["advertisingProvisionCny"],
                excess_return_loss_cny=order["excessReturnLossCny"],
                forward_logistics_cny=order.get("forwardLogisticsCny") or 0,
                net_quantity=order["netQuantity"],
                order_result=order["outcome"],
                payment_cny=order["paymentFeeCny"],
                fixed_operations_cny=order["forwardOperationsUsedCny"],
                other_cost_cny=_decimal(order.get("storageAndOperationsCny"))
                + _decimal(order.get("otherPlatformCostCny")),
                owner_profit_cny=order["ownerProfitCny"],
                calculation_status=order["evidenceStatus"],
                evidence={
                    "evidenceIds": order.get("evidenceIds") or [],
                    "componentSources": order.get("componentSources") or {},
                    "pricingPolicyId": self.policy.pricing_policy_id,
                    "fullOrderSnapshot": dict(order),
                },
            )
            persisted[(str(order["storeAlias"]), str(order["orderKey"]))] = version
        return persisted

    def _used_cost_versions(self, orders: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        identifiers = {str(row.get("costVersionId")) for row in orders if row.get("costVersionId")}
        if not identifiers:
            return []
        rows = self.ledger.connection.execute(
            f"SELECT * FROM cost_versions WHERE cost_version_uid IN ({', '.join('?' for _ in identifiers)})",
            sorted(identifiers),
        ).fetchall()
        return [
            {
                "costVersionId": row["cost_version_uid"],
                "productKey": row["sku"],
                "costRub": row["purchase_cost_rub"],
                "exchangeRateRubPerCny": row["exchange_rate_rub_per_cny"],
                "costCny": row["purchase_cost_cny"],
                "effectiveFrom": row["effective_from"],
                "effectiveTo": row["effective_to"],
                "sourceQuote": row["quote_path"],
                "sourceSheet": row["quote_sheet"],
                "sourceRow": row["quote_row"],
            }
            for row in rows
        ]

    def _report_funding_rows(self, stores: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        result = []
        for store in stores:
            balance = self._latest_withdrawable_balance(str(store["storeAlias"]))
            result.append(
                {
                    "storeAlias": store["storeAlias"],
                    "withdrawableBalanceCny": balance["withdrawable_balance_cny"] if balance else None,
                    "unallocatedAdvertisingReserveCny": store.get("advertisingProvisionCny"),
                    "approvedPendingDepositCny": _money(
                        sum(
                            (
                                Decimal(row["requested_amount_cny"])
                                for row in self.ledger.query_funding_actions(
                                    store_alias=str(store["storeAlias"]),
                                    action_type="advertising_top_up",
                                    lifecycle_status=("approved", "executing", "unknown"),
                                )
                            ),
                            _ZERO,
                        )
                    ),
                    "otherLockedFundsCny": "0.00",
                    "status": "PLAN_ONLY",
                    "note": "提现需人工操作；正式计划以 funds-plan 为准",
                }
            )
        return result

    def _latest_withdrawable_balance(self, store_alias: str) -> dict[str, Any] | None:
        row = self.ledger.connection.execute(
            """
            SELECT * FROM raw_balance
            WHERE store_alias=? AND withdrawable_balance_cny IS NOT NULL
            ORDER BY observed_at DESC, fetched_at DESC LIMIT 1
            """,
            (store_alias,),
        ).fetchone()
        return dict(row) if row else None

    def _require_fresh_balance_evidence(
        self,
        balance: Mapping[str, Any],
        *,
        store_alias: str,
    ) -> datetime:
        observed_at = _parse_datetime(balance.get("observed_at"))
        if observed_at is None:
            raise FinanceServiceError(
                f"Withdrawable-balance evidence timestamp is missing for {store_alias}"
            )
        _strict_non_negative_money(
            balance.get("withdrawable_balance_cny"),
            f"withdrawable balance for {store_alias}",
        )
        current = self._now().astimezone(timezone.utc)
        observed = observed_at.astimezone(timezone.utc)
        if observed - current > _MAX_FUTURE_EVIDENCE_SKEW:
            raise FinanceServiceError(
                f"Withdrawable-balance evidence is future-dated for {store_alias}"
            )
        if current - observed > _MAX_FUNDING_BALANCE_EVIDENCE_AGE:
            raise FinanceServiceError(
                f"Withdrawable-balance evidence is stale for {store_alias}; "
                "run finance daily again before creating a funds plan"
            )
        return observed_at

    def _require_current_deposit_report(self, report_id: str) -> dict[str, Any]:
        report = self.ledger.get_report(report_id)
        if report is None:
            raise FinanceServiceError(f"Unknown report in advertising deposit plan: {report_id}")
        if report["report_type"] != "weekly" or report["report_status"] not in {
            "FINAL",
            "RESTATED",
        }:
            raise FinanceServiceError(
                "Advertising deposits require a FINAL or RESTATED weekly report"
            )
        latest = self._latest_weekly_final_report()
        if latest is None or str(latest["report_id"]) != str(report_id):
            raise FinanceServiceError(
                "Advertising deposit report is not the latest finalized weekly report"
            )
        if str(report.get("policy_id")) != self.policy.reporting_policy_id:
            raise FinanceServiceError(
                "Advertising deposit report does not use the active financial policy"
            )
        return report

    def _require_governed_deposit_cap(
        self,
        payload: Mapping[str, Any],
        *,
        exclude_action_id: str | None = None,
    ) -> Decimal:
        if self.policy.advertising_deposit_cap_rule != ADVERTISING_DEPOSIT_CAP_RULE:
            raise FinanceServiceError("Active advertising-deposit cap rule is unsupported")
        governed = self._available_advertising_reserve_cny(
            str(payload["store"]),
            exclude_action_id=exclude_action_id,
        )
        frozen_reserve = _strict_non_negative_money(
            payload["availableAdvertisingReserveCny"],
            "frozen advertising reserve",
        )
        frozen_cap = _strict_non_negative_money(payload["policyCapCny"], "policy cap")
        if frozen_reserve != governed or frozen_cap != governed:
            raise FinanceServiceError(
                "Policy cap and advertising reserve must both equal the current "
                "ledger-derived available advertising reserve"
            )
        return governed

    def _require_exact_proposal(
        self,
        action: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> None:
        try:
            evidence = json.loads(str(action.get("evidence_json") or "{}"))
        except json.JSONDecodeError as exc:
            raise FinanceServiceError("Dry-run proposal evidence is unreadable") from exc
        if evidence != dict(payload):
            raise FinanceServiceError(
                "Dry-run proposal evidence does not match the immutable live plan"
            )
        events = self.ledger.get_funding_action_events(str(action["action_id"]))
        if not any(str(event["lifecycle_status"]) == "proposed" for event in events):
            raise FinanceServiceError(
                "Funding action has no prior dry-run proposed ledger event"
            )

    def _available_advertising_reserve_cny(
        self,
        store_alias: str,
        *,
        exclude_action_id: str | None = None,
    ) -> Decimal:
        activation = _parse_datetime(self.registry.get("fundingActivationAt"))
        if activation is None:
            raise FinanceServiceError("Store registry has no valid fundingActivationAt")
        reserve = sum(
            (
                max(_ZERO, accrual.amount_cny - accrual.allocated_cny)
                for accrual in self._advertising_accruals(
                    store_alias,
                    formal_activation_at=activation,
                )
                if not accrual.historical_bootstrap and accrual.accrued_at >= activation
            ),
            _ZERO,
        )
        for action in self.ledger.query_funding_actions(
            store_alias=store_alias,
            action_type="advertising_top_up",
        ):
            if str(action["action_id"]) == str(exclude_action_id or ""):
                continue
            if self._funding_action_locks_reserve(action):
                reserve -= _strict_non_negative_money(
                    action["requested_amount_cny"],
                    "locked advertising deposit",
                )
        return max(_ZERO, reserve).quantize(_CENT)

    def _funding_action_locks_reserve(self, action: Mapping[str, Any]) -> bool:
        status = str(action.get("lifecycle_status") or "").lower()
        if status not in _LOCKING_ADVERTISING_ACTION_STATUSES:
            return False
        if status != "approved":
            return True
        try:
            evidence = json.loads(str(action.get("evidence_json") or "{}"))
        except json.JSONDecodeError:
            return True
        expires_at = _parse_datetime(evidence.get("expiresAt"))
        if expires_at is None:
            return True
        return self._now().astimezone(timezone.utc) < expires_at.astimezone(timezone.utc)

    def _live_advertising_deposit_preflight(
        self,
        client: WbClient,
        payload: Mapping[str, Any],
        *,
        exclude_action_id: str,
    ) -> dict[str, Any]:
        campaign_id = int(payload["campaign"])
        campaign_response = self._live_funding_get(
            client,
            f"{ADVERTISING_BASE_URL}/api/advert/v2/adverts?ids={campaign_id}",
            "campaign ownership/status",
        )
        campaign_data = campaign_response.data
        status = _campaign_status_from_known_shapes(campaign_data, campaign_id)
        if status not in _FUNDABLE_CAMPAIGN_STATUSES:
            raise FinanceServiceError(
                f"Campaign status {status} is not eligible for a live budget deposit"
            )

        budget_response = self._live_funding_get(
            client,
            f"{ADVERTISING_BASE_URL}/adv/v1/budget?id={campaign_id}",
            "campaign budget permission",
        )
        if not isinstance(budget_response.data, Mapping):
            raise FinanceServiceError(
                "WB campaign budget preflight returned an unknown shape"
            )
        budget_before = _strict_non_negative_money(
            budget_response.data.get("total"),
            "campaign budget total",
        )

        store = next(
            item
            for item in self.registry["stores"]
            if item.get("alias") == payload["store"] and item.get("enabled", True)
        )
        store_currency = str(
            store.get("currency") or self.registry.get("reportingCurrency") or ""
        ).strip().upper()
        if store_currency != payload["currency"]:
            raise FinanceServiceError(
                "Deposit-plan currency no longer matches the enabled store currency"
            )
        seller_balance_response = self._live_funding_get(
            client,
            SELLER_BALANCE_URL,
            "same-store seller-balance currency",
        )
        seller_balance_data = seller_balance_response.data
        if not isinstance(seller_balance_data, Mapping):
            raise FinanceServiceError(
                "WB seller-balance currency preflight returned an unknown shape"
            )
        live_currency = str(seller_balance_data.get("currency") or "").strip().upper()
        if not live_currency or live_currency != payload["currency"]:
            raise FinanceServiceError(
                "WB seller-balance currency is missing or differs from the approved plan"
            )

        balance_response = self._live_funding_get(
            client,
            f"{ADVERTISING_BASE_URL}/adv/v1/balance",
            "advertising source balance",
        )
        balance_data = balance_response.data
        if not isinstance(balance_data, Mapping):
            raise FinanceServiceError(
                "WB advertising balance preflight returned an unknown shape"
            )
        advertising_currency = str(balance_data.get("currency") or "").strip().upper()
        if advertising_currency and advertising_currency != payload["currency"]:
            raise FinanceServiceError(
                "WB advertising-balance currency differs from the approved plan"
            )
        source_field = _ADVERTISING_SOURCE_FIELDS.get(str(payload["source"]))
        if source_field is None or source_field not in balance_data:
            raise FinanceServiceError(
                "Approved advertising funding source is absent from the live balance"
            )
        current_source_balance = _strict_non_negative_money(
            balance_data[source_field],
            f"live advertising {payload['source']} balance",
        )
        frozen_source_balance = _strict_non_negative_money(
            payload["sourceBalanceCny"],
            "frozen source balance",
        )
        if current_source_balance != frozen_source_balance:
            raise FinanceServiceError(
                "Advertising source balance changed after approval; create a new plan"
            )

        amount = _strict_non_negative_money(payload["amountCny"], "deposit amount")
        policy_cap = _strict_non_negative_money(payload["policyCapCny"], "policy cap")
        if amount <= _ZERO or amount > policy_cap:
            raise FinanceServiceError("Deposit amount exceeds the approved policy cap")
        if amount > current_source_balance:
            raise FinanceServiceError("Live advertising source balance is insufficient")

        current_reserve = self._available_advertising_reserve_cny(
            str(payload["store"]),
            exclude_action_id=exclude_action_id,
        )
        frozen_reserve = _strict_non_negative_money(
            payload["availableAdvertisingReserveCny"],
            "frozen advertising reserve",
        )
        if current_reserve != frozen_reserve or policy_cap != current_reserve:
            raise FinanceServiceError(
                "Governed policy cap or advertising reserve changed after approval; "
                "create a new plan"
            )
        if amount > current_reserve:
            raise FinanceServiceError("Deposit amount exceeds the live advertising reserve")

        return {
            "checkedAt": self._now().isoformat(timespec="seconds"),
            "currency": payload["currency"],
            "sellerBalanceCurrency": live_currency,
            "source": payload["source"],
            "sourceBalanceCny": _money(current_source_balance),
            "sourceBalanceEvidenceAt": payload["sourceBalanceEvidenceAt"],
            "policyCapCny": _money(policy_cap),
            "availableAdvertisingReserveCny": _money(current_reserve),
            "campaignStatus": status,
            "campaignBudgetBeforeCny": _money(budget_before),
            "permissionsConfirmed": True,
        }

    @staticmethod
    def _live_funding_get(client: WbClient, url: str, label: str):
        try:
            response = client.request("GET", url, retries=0)
        except Exception as exc:
            raise FinanceServiceError(
                f"WB {label} preflight could not be read; live deposit is blocked"
            ) from exc
        if not response.ok:
            raise FinanceServiceError(
                f"WB {label} preflight failed with HTTP {response.status}; "
                "live deposit is blocked"
            )
        return response

    def _cancel_pre_post_action(
        self,
        action: Mapping[str, Any],
        payload: Mapping[str, Any],
        reason: str,
    ) -> None:
        self.ledger.update_funding_action_status(
            str(action["action_id"]),
            "cancelled",
            occurred_at=self._now(),
            external_reference=str(payload["fingerprint"]),
            evidence={
                "phase": "pre_post",
                "planFingerprint": payload["fingerprint"],
                "reason": str(reason),
                "postAttempted": False,
            },
        )

    def _campaign_budget_readback(
        self,
        client: WbClient,
        payload: Mapping[str, Any],
        *,
        expected_total: Decimal,
    ) -> dict[str, Any]:
        response = None
        try:
            response = client.request(
                "GET",
                f"{ADVERTISING_BASE_URL}/adv/v1/budget?id={int(payload['campaign'])}",
                retries=0,
            )
            observed_total = (
                _strict_non_negative_money(
                    response.data.get("total")
                    if response.ok and isinstance(response.data, Mapping)
                    else None,
                    "campaign budget readback total",
                )
                if response.ok
                else None
            )
            matched = observed_total == expected_total
            return {
                "status": response.status,
                "data": response.data,
                "expectedTotalCny": _money(expected_total),
                "observedTotalCny": (
                    _money(observed_total) if observed_total is not None else None
                ),
                "matched": matched,
            }
        except Exception as exc:
            return {
                "status": getattr(response, "status", None),
                "data": getattr(response, "data", None),
                "expectedTotalCny": _money(expected_total),
                "observedTotalCny": None,
                "matched": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _resolve_deposit_readback_only(
        self,
        client: WbClient,
        payload: Mapping[str, Any],
        action: Mapping[str, Any],
    ) -> dict[str, Any]:
        expected_total = self._expected_deposit_total(action, payload)
        if expected_total is None:
            readback = {
                "status": None,
                "data": None,
                "expectedTotalCny": None,
                "observedTotalCny": None,
                "matched": False,
                "error": "No archived pre-POST campaign budget evidence",
            }
        else:
            readback = self._campaign_budget_readback(
                client,
                payload,
                expected_total=expected_total,
            )
        final_status = "readback_confirmed" if readback["matched"] else "unknown"
        self.ledger.update_funding_action_status(
            str(action["action_id"]),
            final_status,
            occurred_at=self._now(),
            external_reference=str(payload["fingerprint"]),
            evidence={
                "phase": "readback_only",
                "postAttempted": False,
                "readback": readback,
            },
        )
        return {
            "actionId": action["action_id"],
            "status": final_status,
            "store": payload["store"],
            "campaign": payload["campaign"],
            "amountCny": payload["amountCny"],
            "readback": readback,
            "readbackOnly": True,
            "automaticRetryAllowed": False,
        }

    def _expected_deposit_total(
        self,
        action: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> Decimal | None:
        for event in reversed(
            self.ledger.get_funding_action_events(str(action["action_id"]))
        ):
            try:
                evidence = json.loads(str(event.get("evidence_json") or "{}"))
            except json.JSONDecodeError:
                continue
            live_preflight = evidence.get("livePreflight")
            if not isinstance(live_preflight, Mapping):
                continue
            before = _strict_non_negative_money(
                live_preflight.get("campaignBudgetBeforeCny"),
                "archived campaign budget before deposit",
            )
            return (before + Decimal(str(payload["amountCny"]))).quantize(_CENT)
        return None

    def _advertising_accruals(
        self,
        store_alias: str,
        *,
        formal_activation_at: str | datetime | None = None,
    ) -> list[AdvertisingAccrual]:
        activation = _parse_datetime(
            formal_activation_at or self.registry.get("fundingActivationAt")
        )
        if activation is None:
            raise FinanceServiceError("Store registry has no valid fundingActivationAt")
        candidates: list[dict[str, Any]] = []
        for status in ("FINAL", "RESTATED"):
            for report in self.ledger.find_reports(
                report_type="weekly", report_status=status
            ):
                candidates.append(report)
        latest_reports: dict[str, dict[str, Any]] = {}
        for report in sorted(
            candidates,
            key=_report_version_sort_key,
            reverse=True,
        ):
            latest_reports.setdefault(report["report_key"], report)
        order_balances: dict[str, dict[str, Any]] = {}
        for report in sorted(
            latest_reports.values(),
            key=lambda value: (str(value["period_end"]), str(value["report_key"])),
        ):
            full = self.ledger.get_report(report["report_id"])
            if not full:
                continue
            for order in full.get("orders") or []:
                if order["store_alias"] != store_alias:
                    continue
                snapshot = order["snapshot"]
                amount = _decimal(snapshot.get("advertisingProvisionCny"))
                order_key = str(order["order_key"])
                state = order_balances.setdefault(
                    order_key,
                    {"amount": _ZERO, "accruedAt": None},
                )
                before = _decimal(state["amount"])
                after = before + amount
                if before <= _ZERO < after:
                    state["accruedAt"] = _advertising_accrued_at(snapshot, report)
                elif after <= _ZERO:
                    state["accruedAt"] = None
                state["amount"] = after
        accruals = [
            AdvertisingAccrual(
                accrual_id=f"weekly-order:{store_alias}:{order_key}",
                amount_cny=_decimal(state["amount"]),
                allocated_cny=_ZERO,
                accrued_at=state["accruedAt"],
                historical_bootstrap=False,
            )
            for order_key, state in order_balances.items()
            if _decimal(state["amount"]) > _ZERO and state["accruedAt"] is not None
        ]
        # A confirmed campaign deposit consumes the oldest eligible reserves.
        consumed = _ZERO
        for row in self.ledger.query_funding_actions(
            store_alias=store_alias,
            action_type="advertising_top_up",
            lifecycle_status="readback_confirmed",
        ):
            effective_at = self._confirmed_deposit_effective_at(row)
            if effective_at is not None and effective_at >= activation:
                consumed += Decimal(row["requested_amount_cny"])
        consumed = max(_ZERO, consumed)
        adjusted = []
        for accrual in sorted(accruals, key=lambda item: (item.accrued_at, item.accrual_id)):
            allocation = _ZERO
            if accrual.accrued_at >= activation:
                allocation = min(accrual.amount_cny, consumed)
                consumed -= allocation
            adjusted.append(
                AdvertisingAccrual(
                    accrual_id=accrual.accrual_id,
                    amount_cny=accrual.amount_cny,
                    allocated_cny=allocation,
                    accrued_at=accrual.accrued_at,
                    historical_bootstrap=accrual.historical_bootstrap,
                )
            )
        return adjusted

    def _confirmed_deposit_effective_at(
        self,
        action: Mapping[str, Any],
    ) -> datetime | None:
        """Return the first irreversible-attempt time, never late readback time."""

        events = self.ledger.get_funding_action_events(str(action["action_id"]))
        for target_statuses in (
            {"executing"},
            {"executed", "succeeded", "unknown"},
            {"readback_confirmed"},
        ):
            for event in events:
                if str(event.get("lifecycle_status") or "") in target_statuses:
                    occurred = _parse_datetime(event.get("occurred_at"))
                    if occurred is not None:
                        return occurred
        return _parse_datetime(action.get("requested_at"))

    def _promotion_credentials(self, store_alias: str):
        raw = next(
            item
            for item in self.registry["stores"]
            if item.get("alias") == store_alias and item.get("enabled", True)
        )
        spec = StoreSpec(
            alias=store_alias,
            token_env=str(raw.get("promotionTokenEnv") or raw.get("tokenEnv")),
            token_file=(self.repo_root / str(raw["tokenFile"])).resolve(),
        )
        return resolve_store_credentials(spec, environ=os.environ)

    def _latest_final_report(self, report_key: str) -> dict[str, Any] | None:
        candidates = []
        for status in ("FINAL", "RESTATED"):
            candidates.extend(
                self.ledger.find_reports(report_key=report_key, report_status=status)
            )
        if not candidates:
            return None
        row = max(candidates, key=_report_version_sort_key)
        return self.ledger.get_report(row["report_id"])

    def _refresh_finalized_reports(
        self,
        *,
        exclude_report_keys: set[str],
        affected_from: date,
        affected_through: date,
    ) -> tuple[list[dict[str, Any]], int]:
        latest_by_key: dict[str, dict[str, Any]] = {}
        for status in ("FINAL", "RESTATED"):
            for report in self.ledger.find_reports(report_status=status):
                key = str(report["report_key"])
                current = latest_by_key.get(key)
                if (
                    current is None
                    or _report_version_sort_key(report)
                    > _report_version_sort_key(current)
                ):
                    latest_by_key[key] = report

        refreshed: list[dict[str, Any]] = []
        checked = 0
        for report in sorted(
            latest_by_key.values(),
            key=lambda value: (
                str(value["period_end"]),
                str(value["report_key"]),
            ),
        ):
            report_key = str(report["report_key"])
            if report_key in exclude_report_keys:
                continue
            full = self.ledger.get_report(str(report["report_id"]), include_orders=False)
            metadata = dict((full or {}).get("metadata") or {})
            raw_period = dict(metadata.get("period") or {})
            try:
                selected = FinancePeriod(
                    str(report["report_type"]),
                    date.fromisoformat(str(raw_period.get("from"))),
                    date.fromisoformat(str(raw_period.get("to"))),
                    cumulative=bool(raw_period.get("cumulative", False)),
                )
            except (TypeError, ValueError) as exc:
                raise FinanceServiceError(
                    f"Finalized report has invalid period metadata: {report['report_id']}"
                ) from exc
            if selected.end < affected_from or selected.start > affected_through:
                continue
            checked += 1
            result = self.report(
                kind=selected.kind,
                period="existing",
                reason="Daily late-refund/platform-correction review",
                _selected_period=selected,
            )
            if not result.get("idempotent"):
                refreshed.append(result)
        return refreshed, checked

    def _latest_weekly_final_report(self) -> dict[str, Any] | None:
        candidates: list[dict[str, Any]] = []
        for status in ("FINAL", "RESTATED"):
            candidates.extend(
                self.ledger.find_reports(
                    report_type="weekly",
                    report_status=status,
                )
            )
        if not candidates:
            return None
        row = max(
            candidates,
            key=lambda value: (
                str(value["period_end"]),
                *_report_version_sort_key(value),
            ),
        )
        return self.ledger.get_report(row["report_id"])

    def _adjust_cumulative_start(self, period: FinancePeriod) -> FinancePeriod:
        if not period.cumulative:
            return period
        earliest: date | None = None
        rows = self.ledger.connection.execute(
            "SELECT payload_json FROM raw_finance"
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            parsed = _parse_datetime(payload.get("orderDt"))
            if parsed is None:
                continue
            day = parsed.astimezone(MOSCOW).date()
            earliest = day if earliest is None else min(earliest, day)
        if earliest is None or earliest >= period.start:
            return period
        return FinancePeriod(period.kind, earliest, period.end, cumulative=True)

    def _next_report_version(self, report_key: str, *, status: str, snapshot_hash: str) -> str:
        reports = self.ledger.find_reports(report_key=report_key)
        if status == "PENDING_DATA":
            return f"candidate-{snapshot_hash[:16]}"
        finals = [row for row in reports if row["report_status"] in {"FINAL", "RESTATED"}]
        return f"v{len(finals) + 1}"

    def _existing_report_result(self, report: Mapping[str, Any]) -> dict[str, Any]:
        metadata = report.get("metadata") or {}
        selected = metadata.get("period") or {}
        output_dir = self._report_output_dir(
            str(report["report_type"]), str(report["period_start"])[:4]
        )
        prefix = str(report["report_id"])
        paths = {
            "json": str(output_dir / f"{prefix}.json"),
            "markdown": str(output_dir / f"{prefix}.md"),
            "workbook": str(output_dir / f"{prefix}.xlsx"),
            "verification": str(output_dir / f"{prefix}.verification.json"),
            "previews": str(output_dir / ".previews" / prefix),
        }
        return {
            "reportId": report["report_id"],
            "reportKey": report["report_key"],
            "reportVersion": report["report_version"],
            "status": report["report_status"],
            "period": selected,
            "orderCount": len(report.get("orders") or []),
            "blockers": metadata.get("blockers") or [],
            "artifacts": paths,
            "idempotent": True,
        }

    def _write_report(self, payload: dict[str, Any], period: FinancePeriod) -> dict[str, str]:
        output = self._report_output_dir(period.kind, str(period.start.year))
        prefix = str(payload["reportId"])
        if (output / f"{prefix}.json").exists():
            return {
                "json": str(output / f"{prefix}.json"),
                "markdown": str(output / f"{prefix}.md"),
                "workbook": str(output / f"{prefix}.xlsx"),
                "verification": str(output / f"{prefix}.verification.json"),
            }
        artifacts = write_report_artifacts(payload, output)
        return {
            "json": str(artifacts.json_path),
            "markdown": str(artifacts.markdown_path),
            "workbook": str(artifacts.workbook_path),
            "verification": str(artifacts.verification_path),
            "previews": str(artifacts.preview_dir),
        }

    def _report_output_dir(self, kind: str, year: str) -> Path:
        return self.output_root / kind / year


def _record_in_period(row: Mapping[str, Any], period: FinancePeriod) -> bool:
    if str(row.get("_source")) == "order":
        if not any(row.get(key) not in (None, "") for key in ("saleID", "saleId")):
            return False
        fields = ("saleDt", "date", "lastChangeDate")
    elif str(row.get("_source")) == "return":
        fields = ("returnDate", "saleDt", "lastChangeDate", "date")
    else:
        fields = ("rrDate", "saleDt", "createDate")
    for field in fields:
        parsed = _parse_datetime(row.get(field))
        if parsed and period.start <= parsed.astimezone(MOSCOW).date() <= period.end:
            return True
    return False


def _record_at_or_before(row: Mapping[str, Any], end_exclusive: str) -> bool:
    boundary = datetime.fromisoformat(end_exclusive)
    source = str(row.get("_source") or "")
    fields = {
        "finance": ("rrDate", "saleDt", "createDate"),
        "return": ("returnDate", "saleDt", "lastChangeDate", "date"),
        "order": ("saleDt", "date", "lastChangeDate"),
    }.get(
        source,
        ("rrDate", "saleDt", "returnDate", "lastChangeDate", "date", "createDate"),
    )
    for field in fields:
        parsed = _parse_datetime(row.get(field))
        if parsed:
            return parsed < boundary
    return True


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(raw[:10]), time.min)
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=MOSCOW)


def _order_period_delta(
    current: Mapping[str, Any],
    prior: Mapping[str, Any],
) -> dict[str, Any]:
    delta = deepcopy(dict(current))
    for field in _DELTA_MONEY_FIELDS:
        current_value = current.get(field)
        prior_value = prior.get(field)
        if current_value is None and prior_value is None:
            delta[field] = None
        elif current_value is None:
            delta[field] = _money(-_decimal(prior_value))
        elif prior_value is None:
            delta[field] = _money(current_value)
        else:
            delta[field] = _money(
                _decimal(current_value) - _decimal(prior_value)
            )

    delta["netQuantity"] = _quantity(
        _decimal(current.get("netQuantity")) - _decimal(prior.get("netQuantity"))
    )
    delta["actualAdvertisingSpendCny"] = _money(
        current.get("actualAdvertisingSpendCny")
    )
    if delta.get("advertisingProvisionCny") is None:
        delta["advertisingReconciliationVarianceCny"] = None
    else:
        delta["advertisingReconciliationVarianceCny"] = _money(
            _decimal(delta["advertisingProvisionCny"])
            - _decimal(delta.get("actualAdvertisingSpendCny"))
        )
    current_template = dict(current.get("template") or {})
    prior_template = dict(prior.get("template") or {})
    delta["template"] = {
        key: (
            None
            if current_template.get(key) is None and prior_template.get(key) is None
            else _money(
                _decimal(current_template.get(key))
                - _decimal(prior_template.get(key))
            )
        )
        for key in sorted(set(current_template) | set(prior_template))
    }
    delta["periodRecognition"] = "PRIOR_PERIOD_CORRECTION"
    delta["priorOutcome"] = prior.get("currentOutcome") or prior.get("outcome")
    delta["currentOutcome"] = current.get("outcome")
    delta["outcome"] = "corrected"
    return delta


def _summarize_order_rows(
    store_alias: str,
    orders: Sequence[Mapping[str, Any]],
    issues: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    def total(field: str) -> str | None:
        values = [order.get(field) for order in orders]
        if any(value is None for value in values):
            return None
        return _money(sum((_decimal(value) for value in values), _ZERO))

    other_values: list[Decimal | None] = []
    for order in orders:
        storage = order.get("storageAndOperationsCny")
        other = order.get("otherPlatformCostCny")
        if storage is None or other is None:
            other_values.append(None)
        else:
            other_values.append(_decimal(storage) + _decimal(other))
    other_total = (
        None
        if any(value is None for value in other_values)
        else _money(sum((value or _ZERO for value in other_values), _ZERO))
    )
    unresolved = sum(order.get("evidenceStatus") == "BLOCKED" for order in orders)
    blocking_issues = sum(bool(issue.get("blocking")) for issue in issues)
    status = "PARTIAL" if unresolved or blocking_issues else "FINAL"
    return {
        "storeAlias": store_alias,
        "status": status,
        "orderCount": len(orders),
        "completedCount": sum(order.get("outcome") == "completed" for order in orders),
        "returnedCount": sum(order.get("outcome") == "returned" for order in orders),
        "correctedCount": sum(order.get("outcome") == "corrected" for order in orders),
        "netQuantity": _quantity(
            sum((_decimal(order.get("netQuantity")) for order in orders), _ZERO)
        ),
        "netTurnoverCny": total("netTurnoverCny"),
        "purchaseCostCny": total("purchaseCostCny"),
        "wbCommissionCny": total("wbCommissionCny"),
        "paymentFeeCny": total("paymentFeeCny"),
        "forwardOperationsCny": total("forwardOperationsUsedCny"),
        "managedOperationsCny": total("managedOperationsCny"),
        "returnLogisticsCny": total("returnLogisticsCny"),
        "advertisingProvisionCny": total("advertisingProvisionCny"),
        "excessReturnLossCny": total("excessReturnLossCny"),
        "actualAdvertisingSpendCny": total("actualAdvertisingSpendCny"),
        "otherPlatformCostCny": other_total,
        "ownerProfitCny": total("ownerProfitCny"),
        "unresolvedOrderCount": unresolved,
        "issueCount": blocking_issues,
    }


def _advertising_accrued_at(
    snapshot: Mapping[str, Any],
    report: Mapping[str, Any],
) -> datetime:
    fallback = _parse_datetime(report.get("period_end"))
    if snapshot.get("periodRecognition") == "PRIOR_PERIOD_CORRECTION":
        if fallback is None:
            raise FinanceServiceError(
                f"Report {report.get('report_id')} has no usable correction timestamp"
            )
        return fallback
    completed = _parse_datetime(snapshot.get("completedAt"))
    if completed is not None:
        return completed
    if fallback is None:
        raise FinanceServiceError(
            f"Report {report.get('report_id')} has no usable advertising accrual timestamp"
        )
    return fallback


def _report_version_sort_key(report: Mapping[str, Any]) -> tuple[int, str, str]:
    raw_version = str(report.get("report_version") or "")
    version = (
        int(raw_version[1:])
        if raw_version.startswith("v") and raw_version[1:].isdigit()
        else -1
    )
    return (
        version,
        str(report.get("generated_at") or ""),
        str(report.get("report_id") or ""),
    )


def _empty_store_summary(store_alias: str) -> dict[str, Any]:
    return {
        "storeAlias": store_alias,
        "status": "FINAL",
        "orderCount": 0,
        "completedCount": 0,
        "returnedCount": 0,
        "correctedCount": 0,
        "netQuantity": "0",
        "netTurnoverCny": "0.00",
        "purchaseCostCny": "0.00",
        "wbCommissionCny": "0.00",
        "paymentFeeCny": "0.00",
        "forwardOperationsCny": "0.00",
        "managedOperationsCny": "0.00",
        "returnLogisticsCny": "0.00",
        "advertisingProvisionCny": "0.00",
        "excessReturnLossCny": "0.00",
        "actualAdvertisingSpendCny": "0.00",
        "otherPlatformCostCny": "0.00",
        "ownerProfitCny": "0.00",
        "unresolvedOrderCount": 0,
        "issueCount": 0,
    }


def _period_start(day: date) -> str:
    return datetime.combine(day, time.min, tzinfo=MOSCOW).isoformat()


def _period_end_exclusive(day: date) -> str:
    return datetime.combine(day + timedelta(days=1), time.min, tzinfo=MOSCOW).isoformat()


def _period_end_inclusive(day: date) -> str:
    return datetime.combine(day, time.max, tzinfo=MOSCOW).isoformat()


def _decimal(value: Any) -> Decimal:
    return _ZERO if value in (None, "") else Decimal(str(value))


def _strict_non_negative_money(value: Any, label: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise FinanceServiceError(f"{label} is missing or invalid")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FinanceServiceError(f"{label} is missing or invalid") from exc
    if not amount.is_finite() or amount < _ZERO:
        raise FinanceServiceError(f"{label} is missing or invalid")
    quantized = amount.quantize(_CENT)
    if quantized != amount:
        raise FinanceServiceError(f"{label} has unsupported sub-cent precision")
    return quantized


def _exact_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0 or str(parsed) != str(value).strip():
        return None
    return parsed


def _exact_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise FinanceServiceError(f"{label} is missing or invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise FinanceServiceError(f"{label} is missing or invalid") from exc
    if str(parsed) != str(value).strip():
        raise FinanceServiceError(f"{label} is missing or invalid")
    return parsed


def _campaign_status_from_known_shapes(data: Any, campaign_id: int) -> int:
    adverts = data.get("adverts") if isinstance(data, Mapping) else None
    if not isinstance(adverts, list) or not adverts:
        raise FinanceServiceError(
            "WB campaign preflight returned an unknown shape; live deposit is blocked"
        )
    matches: list[int] = []
    for item in adverts:
        if not isinstance(item, Mapping):
            raise FinanceServiceError("WB campaign preflight contains an invalid row")
        if "advert_list" in item:
            rows = item.get("advert_list")
            if not isinstance(rows, list):
                raise FinanceServiceError("WB grouped campaign list is invalid")
            group_status = _exact_int(item.get("status"), "campaign group status")
            for row in rows:
                if not isinstance(row, Mapping):
                    raise FinanceServiceError("WB grouped campaign row is invalid")
                if _exact_positive_int(row.get("advertId")) == campaign_id:
                    matches.append(group_status)
            continue
        if "id" in item:
            if _exact_positive_int(item.get("id")) == campaign_id:
                matches.append(
                    _exact_int(
                        item.get("status", item.get("advertStatus")),
                        "campaign status",
                    )
                )
            continue
        raise FinanceServiceError(
            "WB campaign preflight returned an unsupported campaign row shape"
        )
    if len(matches) != 1:
        raise FinanceServiceError(
            "Campaign is not uniquely visible to the approved store token; "
            "ownership/permission cannot be confirmed"
        )
    return matches[0]


def _money(value: Any) -> str:
    return format(_decimal(value).quantize(Decimal("0.01")), "f")


def _quantity(value: Any) -> str:
    rendered = format(_decimal(value), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _dedupe_blockers(values: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    selected = {}
    for item in values:
        value = dict(item)
        key = (
            str(value.get("code") or "BLOCKER"),
            str(value.get("storeAlias") or ""),
            str(value.get("orderKey") or ""),
            str(value.get("message") or ""),
        )
        selected[key] = value
    return list(selected.values())
