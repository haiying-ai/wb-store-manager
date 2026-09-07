from __future__ import annotations

from copy import deepcopy
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from wb_manager.client import ApiResult
from wb_manager.finance_funds import (
    AdvertisingAccrual,
    build_advertising_deposit_plan,
    build_funding_plan,
)
from wb_manager.finance_service import (
    FinanceService,
    FinanceServiceError,
    _campaign_status_from_known_shapes,
)
from wb_manager.finance_sources import SourceResult
from wb_manager.finance_periods import FinancePeriod
from wb_manager.finance_sync import _balance_amounts


SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_default_output_root_uses_portable_reporting_standard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("WB_FINANCE_REPORT_ROOT", raising=False)
    service = FinanceService(db_path=tmp_path / "finance.sqlite3")
    try:
        assert service.output_root == (
            Path(__file__).resolve().parents[1] / "outputs" / "wb-finance-reports"
        ).resolve()
    finally:
        service.close()


def _write_fake_complete_report_artifacts(
    paths: dict[str, Path],
    report_id: str,
    *,
    payload: dict | None = None,
) -> None:
    paths["json"].parent.mkdir(parents=True, exist_ok=True)
    report_payload = payload or {"reportId": report_id, "status": "FINAL"}
    paths["json"].write_text(json.dumps(report_payload), encoding="utf-8")
    paths["markdown"].write_text("report\n", encoding="utf-8")
    paths["workbook"].write_bytes(b"workbook")
    paths["previews"].mkdir(parents=True, exist_ok=True)
    rendered = []
    for index in range(7):
        preview = paths["previews"] / f"sheet-{index + 1}.png"
        preview.write_bytes(b"png")
        rendered.append(str(preview.resolve()))
    paths["verification"].write_text(
        json.dumps(
            {
                "status": "PASS",
                "formulaErrorCount": 0,
                "renderedSheets": rendered,
                "reportId": report_id,
                "workbookStatus": (
                    "PENDING_DATA"
                    if report_payload.get("status") == "PENDING_DATA"
                    else "FINAL"
                ),
                "checkFailures": [],
                "consistencyFailures": [],
            }
        ),
        encoding="utf-8",
    )


def test_live_shape_cny_seller_balance_uses_minor_units():
    account, withdrawable, advertising = _balance_amounts(
        {"currency": "CNY", "current": 1565807, "for_withdraw": 269163},
        "seller_balance",
    )

    assert account == Decimal("15658.07")
    assert withdrawable == Decimal("2691.63")
    assert advertising is None


def _registry(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "registryId": "test-registry",
                "status": "active",
                "reportingCurrency": "CNY",
                "financialTimezone": "Europe/Moscow",
                "schedulerTimezone": "Asia/Shanghai",
                "officialHistoryStart": "2026-07-01",
                "fundingActivationAt": "2026-08-28T00:00:00+08:00",
                "stores": [
                    {
                        "alias": "owner-store",
                        "enabled": True,
                        "openedAt": "2026-07-01",
                        "tokenEnv": "TEST_OWNER_TOKEN",
                        "promotionTokenEnv": "TEST_OWNER_TOKEN",
                        "tokenFile": ".env.test",
                        "currency": "CNY",
                    },
                    {
                        "alias": "new-store",
                        "enabled": True,
                        "openedAt": "2026-08-01",
                        "tokenEnv": "TEST_NEW_TOKEN",
                        "promotionTokenEnv": "TEST_NEW_TOKEN",
                        "tokenFile": ".env.test",
                        "currency": "CNY",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _finance_rows(
    return_logistics_cny: str = "16",
    *,
    include_return: bool = True,
    return_event_date: str = "2026-08-21",
):
    rows = [
        {
            "rrdId": 1,
            "orderId": "order-sold",
            "srid": "srid-sold",
            "shkId": "shk-sold",
            "vendorCode": "IPHONE 13-128GB-BLACK",
            "docTypeName": "销售",
            "sellerOperName": "销售",
            "quantity": 1,
            "returnAmount": 0,
            "retailPriceWithDisc": "1000",
            "forPay": "850",
            "acquiringFee": "0",
            "deliveryService": "0",
            "rebillLogisticCost": "0",
            "paidStorage": "0",
            "deduction": "0",
            "paidAcceptance": "0",
            "penalty": "0",
            "additionalPayment": "0",
            "orderDt": "2026-08-18T10:00:00+03:00",
            "saleDt": "2026-08-20T10:00:00+03:00",
            "rrDate": "2026-08-20",
        },
        {
            "rrdId": 2,
            "orderId": "order-sold",
            "srid": "srid-sold",
            "shkId": "shk-sold",
            "vendorCode": "IPHONE 13-128GB-BLACK",
            "sellerOperName": "物流",
            "bonusTypeName": "销售时给客户",
            "quantity": 0,
            "returnAmount": 0,
            "retailPriceWithDisc": "0",
            "forPay": "0",
            "acquiringFee": "0",
            "deliveryService": "8",
            "rebillLogisticCost": "0",
            "paidStorage": "0",
            "deduction": "0",
            "paidAcceptance": "0",
            "penalty": "0",
            "additionalPayment": "0",
            "orderDt": "2026-08-18T10:00:00+03:00",
            "saleDt": "2026-08-20T10:00:00+03:00",
            "rrDate": "2026-08-20",
        },
        {
            "rrdId": 3,
            "orderId": "order-return",
            "srid": "srid-return-sale",
            "shkId": "shk-return",
            "vendorCode": "IPHONE 13-128GB-WHITE",
            "docTypeName": "销售",
            "sellerOperName": "销售",
            "quantity": 1,
            "returnAmount": 0,
            "retailPriceWithDisc": "500",
            "forPay": "425",
            "acquiringFee": "5",
            "deliveryService": "0",
            "rebillLogisticCost": "0",
            "paidStorage": "0",
            "deduction": "0",
            "paidAcceptance": "0",
            "penalty": "0",
            "additionalPayment": "0",
            "orderDt": "2026-08-18T11:00:00+03:00",
            "saleDt": "2026-08-20T11:00:00+03:00",
            "rrDate": "2026-08-20",
        },
        {
            "rrdId": 4,
            "orderId": "order-return",
            "srid": "srid-return-new",
            "shkId": "shk-return",
            "vendorCode": "IPHONE 13-128GB-WHITE",
            "docTypeName": "退货",
            "sellerOperName": "退货",
            "quantity": 1,
            "returnAmount": 1,
            "retailPriceWithDisc": "500",
            "forPay": "-425",
            "acquiringFee": "-5",
            "deliveryService": "0",
            "rebillLogisticCost": "0",
            "paidStorage": "0",
            "deduction": "0",
            "paidAcceptance": "0",
            "penalty": "0",
            "additionalPayment": "0",
            "orderDt": "2026-08-18T11:00:00+03:00",
            "saleDt": f"{return_event_date}T11:00:00+03:00",
            "rrDate": return_event_date,
        },
        {
            "rrdId": 5,
            "orderId": "order-return",
            "srid": "srid-return-new",
            "shkId": "shk-return",
            "vendorCode": "IPHONE 13-128GB-WHITE",
            "sellerOperName": "物流",
            "bonusTypeName": "从客户返回退货",
            "quantity": 0,
            "returnAmount": 0,
            "retailPriceWithDisc": "0",
            "forPay": "0",
            "acquiringFee": "0",
            "deliveryService": return_logistics_cny,
            "rebillLogisticCost": "0",
            "paidStorage": "0",
            "deduction": "0",
            "paidAcceptance": "0",
            "penalty": "0",
            "additionalPayment": "0",
            "orderDt": "2026-08-18T11:00:00+03:00",
            "saleDt": f"{return_event_date}T11:00:00+03:00",
            "rrDate": return_event_date,
        },
    ]
    return rows if include_return else rows[:3]


class FakeSources:
    return_logistics_cny = "16"
    include_return = True
    return_event_date = "2026-08-21"

    def __init__(self, alias: str):
        self.alias = alias

    def finance_detailed(self, start, end):
        return SourceResult(
            "finance_detailed", 204, True,
            (
                _finance_rows(
                    self.return_logistics_cny,
                    include_return=self.include_return,
                    return_event_date=self.return_event_date,
                )
                if self.alias == "owner-store"
                else []
            ),
        )

    def statistics_sales(self, start):
        return SourceResult("statistics_sales", 204, True, [])

    def statistics_orders(self, start):
        return SourceResult("statistics_orders", 204, True, [])

    def goods_returns(self, start, end):
        return SourceResult("goods_returns", 204, True, [])

    def advertising_costs(self, start, end):
        rows = []
        if self.alias == "owner-store":
            rows = [
                {
                    "eventId": "ad-1",
                    "storeAlias": self.alias,
                    "eventType": "advertising_expense",
                    "pnl": True,
                    "paymentType": "Balance",
                    "amount": "20",
                    "advertId": 100,
                    "occurredAt": "2026-08-21T10:00:00+03:00",
                }
            ]
        return SourceResult("advertising_costs", 200, True, rows)

    def advertising_payments(self, start, end):
        return SourceResult("advertising_payments", 204, True, [])

    def seller_balance(self):
        return SourceResult(
            "seller_balance", 200, True,
            [{"balance": "5000", "available": "5000"}],
        )

    def advertising_balance(self):
        return SourceResult("advertising_balance", 200, True, [{"balance": "100"}])


def _service(tmp_path: Path, monkeypatch, now: datetime) -> FinanceService:
    monkeypatch.setenv("TEST_OWNER_TOKEN", "owner-token")
    monkeypatch.setenv("TEST_NEW_TOKEN", "new-token")
    service = FinanceService(
        db_path=tmp_path / "finance.sqlite3",
        registry_path=_registry(tmp_path / "stores.json"),
        output_root=tmp_path / "reports",
        now=now,
        source_factory=lambda client, alias: FakeSources(alias),
    )

    def write_verified_stub(payload, period):
        report_id = str(payload["reportId"])
        paths = service._report_artifact_paths(
            period.kind,
            str(period.start.year),
            report_id,
        )
        _write_fake_complete_report_artifacts(
            paths,
            report_id,
            payload=payload,
        )
        return {"json": "verified-by-artifact-tests"}

    service._write_report = write_verified_stub
    return service


def _import_costs(service: FinanceService, tmp_path: Path):
    quote = tmp_path / "quote.json"
    quote.write_text(
        json.dumps(
            [
                {"规格": "iPhone 13-128gb-Black", "成本价格": 4600},
                {"规格": "iPhone 13-128gb-White", "成本价格": 4600},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    result = service.cost_import(
        quote=quote,
        effective_from="2026-07-04",
        approved_by="tester",
        rub_per_cny="11.5",
        source_currency="RUB",
    )
    assert result["rowCount"] == 2


def test_withdrawal_policy_defaults_new_records_to_settlement_balance(
    tmp_path, monkeypatch
):
    service = _service(
        tmp_path,
        monkeypatch,
        datetime(2026, 9, 6, 9, 30, tzinfo=SHANGHAI),
    )
    try:
        result = service.withdrawal_record(
            store_alias="owner-store",
            record_key="post-cutover-withdrawal",
            amount_cny="100.00",
            withdrawn_at="2026-09-05T12:00:00+03:00",
        )
        assert result["recordId"]
        row = service.ledger.list_withdrawal_records(store_alias="owner-store")[0]
        assert row["advertising_fee_status"] == "settlement_balance"
        assert row["advertising_fee_debt_cny"] == "0.00"

        with pytest.raises(FinanceServiceError, match="policy cutover"):
            service.withdrawal_record(
                store_alias="owner-store",
                record_key="invalid-post-cutover-withdrawal",
                amount_cny="100.00",
                withdrawn_at="2026-09-06T12:00:00+03:00",
                advertising_fee_status="unknown",
            )
        with pytest.raises(FinanceServiceError, match="pre-cutover"):
            service.withdrawal_record(
                store_alias="owner-store",
                record_key="invalid-pre-cutover-withdrawal",
                amount_cny="100.00",
                withdrawn_at="2026-09-04T12:00:00+03:00",
                advertising_fee_status="settlement_balance",
            )
    finally:
        service.close()


def test_daily_is_idempotent_and_second_stable_capture_finalizes_week(tmp_path, monkeypatch):
    first_now = datetime(2026, 8, 28, 9, 30, tzinfo=SHANGHAI)
    service = _service(tmp_path, monkeypatch, first_now)
    _import_costs(service, tmp_path)
    first = service.daily(through="2026-08-27")
    bootstrap = next(item for item in first["reports"] if item["period"]["cumulative"])
    assert bootstrap["period"]["from"] == "2026-07-01"
    assert bootstrap["status"] == "FINAL", bootstrap
    weekly = next(item for item in first["reports"] if item["period"]["to"] == "2026-08-23")
    assert weekly["status"] == "PENDING_DATA"
    assert weekly["artifacts"] is None  # Friday capture; Monday is the publish date.
    assert any(item["code"] == "SOURCE_NOT_STABLE_TWICE" for item in weekly["blockers"])
    assert service.ledger.connection.execute("SELECT COUNT(*) FROM raw_finance").fetchone()[0] == 5
    service.close()

    second = _service(tmp_path, monkeypatch, first_now + timedelta(days=1))
    result = second.daily(through="2026-08-28")
    assert result["historicalReportsChecked"] == 1
    assert len(result["reports"]) == 2  # unchanged historical reports are not repeated in output
    weekly = next(item for item in result["reports"] if item["period"]["to"] == "2026-08-23")
    assert weekly["status"] == "FINAL"
    report = second.ledger.get_report(weekly["reportId"])
    assert len(report["orders"]) == 2
    snapshots = {row["snapshot"]["outcome"]: row["snapshot"] for row in report["orders"]}
    assert snapshots["returned"]["returnLogisticsCny"] == "16.00"
    assert snapshots["returned"]["ownerProfitCny"] == "-16.00"
    assert snapshots["returned"]["actualPaymentFeeCny"] == "0.00"
    assert snapshots["completed"]["netQuantity"] == "1"
    assert snapshots["completed"]["actualAdvertisingSpendCny"] == "20.00"
    assert second.ledger.connection.execute("SELECT COUNT(*) FROM raw_finance").fetchone()[0] == 5
    second.close()


def test_daily_publishes_open_item_reports_only_on_weekly_or_monthly_schedule(
    tmp_path,
    monkeypatch,
):
    (tmp_path / "monday").mkdir()
    monday = _service(
        tmp_path / "monday",
        monkeypatch,
        datetime(2026, 8, 31, 9, 30, tzinfo=SHANGHAI),
    )
    _import_costs(monday, tmp_path / "monday")
    monday_result = monday.daily(through="2026-08-30")
    monday_weekly = next(
        item for item in monday_result["reports"] if item["reportKey"].startswith("weekly:")
    )
    monday_monthly = next(
        item for item in monday_result["reports"] if item["reportKey"].startswith("monthly:")
    )
    assert monday_weekly["status"] == "PENDING_DATA"
    assert monday_weekly["artifacts"] == {"json": "verified-by-artifact-tests"}
    assert monday_monthly["artifacts"] is None
    assert monday_result["performance"]["correctionWindowDays"] == 120
    monday.close()

    (tmp_path / "month-start").mkdir()
    month_start = _service(
        tmp_path / "month-start",
        monkeypatch,
        datetime(2026, 9, 1, 9, 30, tzinfo=SHANGHAI),
    )
    _import_costs(month_start, tmp_path / "month-start")
    month_result = month_start.daily(through="2026-08-31")
    bootstrap = next(
        item for item in month_result["reports"] if item["reportKey"].startswith("bootstrap:")
    )
    monthly = next(
        item for item in month_result["reports"] if item["reportKey"].startswith("monthly:")
    )
    weekly = next(
        item for item in month_result["reports"] if item["reportKey"].startswith("weekly:")
    )
    assert bootstrap["period"]["cumulative"] is True
    assert bootstrap["period"]["to"] == "2026-08-31"
    assert monthly["period"] == {
        "from": "2026-08-01",
        "to": "2026-08-31",
        "timezone": "Europe/Moscow",
        "cumulative": False,
    }
    assert monthly["artifacts"] == {"json": "verified-by-artifact-tests"}
    assert weekly["artifacts"] is None
    assert month_result["performance"]["correctionWindowDays"] == 120
    month_start.close()


def test_daily_reports_performance_and_reuses_evidence_snapshot(tmp_path, monkeypatch):
    service = _service(
        tmp_path,
        monkeypatch,
        datetime(2026, 8, 28, 9, 30, tzinfo=SHANGHAI),
    )
    _import_costs(service, tmp_path)

    result = service.daily(through="2026-08-27")

    performance = result["performance"]
    assert performance["correctionWindowDays"] == 30
    assert performance["phaseTimingsMs"]["sync"] >= 0
    assert performance["phaseTimingsMs"]["weeklyReport"] >= 0
    assert performance["cacheEntries"]["rawEvidence"] == 1
    assert performance["cacheEntries"]["grouping"] == 1
    assert performance["cacheHits"].get("rawEvidence", 0) >= 1
    assert performance["cacheHits"].get("grouping", 0) >= 1
    assert performance["totalMs"] >= performance["phaseTimingsMs"]["sync"]
    service.close()


def test_idempotent_pending_candidate_does_not_expose_missing_artifact_paths(
    tmp_path, monkeypatch
):
    service = _service(
        tmp_path,
        monkeypatch,
        datetime(2026, 8, 28, 9, 30, tzinfo=SHANGHAI),
    )
    pending = {
        "report_id": "pending-report",
        "report_key": "weekly:2026-08-17:2026-08-23",
        "report_version": "candidate-deadbeef",
        "report_type": "weekly",
        "report_status": "PENDING_DATA",
        "period_start": "2026-08-17T00:00:00+03:00",
        "metadata": {"period": {"from": "2026-08-17", "to": "2026-08-23"}},
        "orders": [],
    }
    result = service._existing_report_result(pending)
    assert result["artifacts"] is None
    service.close()


@pytest.mark.parametrize(
    ("artifact_case", "is_complete"),
    [
        ("complete", True),
        ("missing_markdown", False),
        ("verification_fail", False),
        ("verification_report_id_mismatch", False),
        ("verification_report_id_wrong_type", False),
        ("verification_schema_missing", False),
        ("missing_preview", False),
        ("payload_report_id_mismatch", False),
    ],
)
def test_existing_report_result_requires_complete_verified_artifacts(
    tmp_path,
    monkeypatch,
    artifact_case,
    is_complete,
):
    service = _service(
        tmp_path,
        monkeypatch,
        datetime(2026, 8, 28, 9, 30, tzinfo=SHANGHAI),
    )
    report_id = "artifact-qualification-report"
    paths = service._report_artifact_paths("weekly", "2026", report_id)
    _write_fake_complete_report_artifacts(paths, report_id)
    verification = json.loads(paths["verification"].read_text(encoding="utf-8"))
    if artifact_case == "missing_markdown":
        paths["markdown"].unlink()
    elif artifact_case == "verification_fail":
        verification["status"] = "FAIL"
    elif artifact_case == "verification_report_id_mismatch":
        verification["reportId"] = "another-report"
    elif artifact_case == "verification_report_id_wrong_type":
        verification["reportId"] = 123
    elif artifact_case == "verification_schema_missing":
        verification.pop("consistencyFailures")
    elif artifact_case == "missing_preview":
        next(paths["previews"].glob("*.png")).unlink()
    elif artifact_case == "payload_report_id_mismatch":
        paths["json"].write_text(
            json.dumps({"reportId": "another-report", "status": "FINAL"}),
            encoding="utf-8",
        )
    paths["verification"].write_text(
        json.dumps(verification),
        encoding="utf-8",
    )
    report = {
        "report_id": report_id,
        "report_key": "weekly:2026-08-17:2026-08-23",
        "report_version": "v1",
        "report_type": "weekly",
        "report_status": "FINAL",
        "period_start": "2026-08-17T00:00:00+03:00",
        "metadata": {"period": {"from": "2026-08-17", "to": "2026-08-23"}},
        "orders": [],
    }

    result = service._existing_report_result(report)

    if is_complete:
        assert set(result["artifacts"]) == {
            "json",
            "markdown",
            "workbook",
            "verification",
            "previews",
            "deliveryWorkbook",
        }
    else:
        assert result["artifacts"] is None
    service.close()


def test_idempotent_existing_report_exposes_readable_copy_without_rebuild(tmp_path, monkeypatch):
    service = _service(
        tmp_path, monkeypatch, datetime(2026, 8, 28, 9, 30, tzinfo=SHANGHAI)
    )
    report_id = "existing-readable-report"
    paths = service._report_artifact_paths("weekly", "2026", report_id)
    _write_fake_complete_report_artifacts(paths, report_id)
    immutable_bytes = paths["workbook"].read_bytes()
    report = {
        "report_id": report_id,
        "report_key": "weekly:2026-08-17:2026-08-23",
        "report_version": "v1",
        "report_type": "weekly",
        "report_status": "FINAL",
        "period_start": "2026-08-17T00:00:00+03:00",
        "metadata": {"period": {"from": "2026-08-17", "to": "2026-08-23"}},
        "orders": [],
    }

    def forbidden_rebuild(*_args, **_kwargs):
        pytest.fail("An idempotent hit must not rebuild the verified workbook")

    monkeypatch.setattr("wb_manager.finance_service.write_report_artifacts", forbidden_rebuild)
    first = service._existing_report_result(report)
    second = service._existing_report_result(report)
    delivery = Path(first["artifacts"]["deliveryWorkbook"])
    assert first["idempotent"] is True
    assert second["artifacts"]["deliveryWorkbook"] == str(delivery)
    assert delivery.name == "WB双店财务周报_2026-08-17至2026-08-23.xlsx"
    assert delivery.read_bytes() == immutable_bytes
    assert paths["workbook"].read_bytes() == immutable_bytes
    service.close()


def test_write_report_rebuilds_partial_artifact_set(tmp_path, monkeypatch):
    service = _service(
        tmp_path,
        monkeypatch,
        datetime(2026, 8, 28, 9, 30, tzinfo=SHANGHAI),
    )
    report_id = "partial-report"
    period = FinancePeriod(
        "weekly", datetime(2026, 8, 17).date(), datetime(2026, 8, 23).date()
    )
    paths = service._report_artifact_paths("weekly", "2026", report_id)
    paths["json"].parent.mkdir(parents=True, exist_ok=True)
    paths["json"].write_text("stale", encoding="utf-8")
    paths["inspection"].write_text("stale", encoding="utf-8")
    paths["previews"].mkdir(parents=True, exist_ok=True)
    (paths["previews"] / "stale.png").write_bytes(b"stale")
    calls = []

    def write_artifacts(payload, output, *, repair_incomplete=False):
        calls.append((deepcopy(payload), output))
        assert repair_incomplete is True
        assert paths["json"].exists()
        paths["json"].unlink()
        paths["inspection"].unlink()
        for preview in paths["previews"].glob("*"):
            preview.unlink()
        paths["previews"].rmdir()
        _write_fake_complete_report_artifacts(paths, report_id, payload=payload)
        paths["workbook"].write_bytes(b"rebuilt")

    monkeypatch.setattr(
        "wb_manager.finance_service.write_report_artifacts",
        write_artifacts,
    )

    result = FinanceService._write_report(
        service,
        {"reportId": report_id, "status": "FINAL"},
        period,
    )

    assert len(calls) == 1
    assert set(result) == {
        "json",
        "markdown",
        "workbook",
        "verification",
        "previews",
        "deliveryWorkbook",
    }
    assert paths["workbook"].read_bytes() == b"rebuilt"
    assert Path(result["deliveryWorkbook"]).read_bytes() == b"rebuilt"
    service.close()


def test_same_snapshot_final_repairs_incomplete_artifacts_without_new_ledger_row(
    tmp_path,
    monkeypatch,
):
    service = _service(
        tmp_path,
        monkeypatch,
        datetime(2026, 8, 28, 9, 30, tzinfo=SHANGHAI),
    )
    _import_costs(service, tmp_path)
    first = service.daily(through="2026-08-27")
    original = next(item for item in first["reports"] if item["period"]["cumulative"])
    paths = service._report_artifact_paths("bootstrap", "2026", original["reportId"])
    paths["workbook"].unlink()
    report_count = service.ledger.connection.execute(
        "SELECT COUNT(*) FROM reports WHERE report_key = ?",
        (original["reportKey"],),
    ).fetchone()[0]
    calls = []
    write_report = service._write_report

    def tracked_write(payload, period):
        calls.append(str(payload["reportId"]))
        return write_report(payload, period)

    service._write_report = tracked_write

    repeated = service.report(kind="bootstrap", period="auto")

    assert repeated["reportId"] == original["reportId"]
    assert repeated["idempotent"] is True
    assert calls == [original["reportId"]]
    assert paths["workbook"].exists()
    assert service.ledger.connection.execute(
        "SELECT COUNT(*) FROM reports WHERE report_key = ?",
        (original["reportKey"],),
    ).fetchone()[0] == report_count
    service.close()


def test_same_snapshot_pending_repairs_partial_started_publication(
    tmp_path,
    monkeypatch,
):
    service = _service(
        tmp_path,
        monkeypatch,
        datetime(2026, 8, 28, 9, 30, tzinfo=SHANGHAI),
    )
    _import_costs(service, tmp_path)
    service._source_blockers = lambda _selected, _evidence: [
        {"code": "TEST_OPEN_ITEM", "message": "keep candidate pending"}
    ]
    candidate = service.report(
        kind="weekly",
        period="2026-W34",
        _publish_open_items=False,
    )
    paths = service._report_artifact_paths("weekly", "2026", candidate["reportId"])
    paths["json"].parent.mkdir(parents=True, exist_ok=True)
    paths["json"].write_text("partial", encoding="utf-8")
    calls = []
    write_report = service._write_report

    def tracked_write(payload, period):
        calls.append(str(payload["reportId"]))
        return write_report(payload, period)

    service._write_report = tracked_write

    repeated = service.report(
        kind="weekly",
        period="2026-W34",
        _publish_open_items=False,
    )

    assert repeated["reportId"] == candidate["reportId"]
    assert repeated["idempotent"] is True
    assert calls == [candidate["reportId"]]
    assert paths["verification"].exists()
    service.close()


def test_previous_report_payload_skips_newer_incompatible_artifact_set(
    tmp_path,
    monkeypatch,
):
    service = _service(
        tmp_path,
        monkeypatch,
        datetime(2026, 9, 1, 9, 30, tzinfo=SHANGHAI),
    )
    totals = {
        "revenue_cny": "0",
        "cost_cny": "0",
        "profit_cny": "0",
        "managed_operations_cny": "0",
        "return_fee_cny": "0",
        "advertising_cny": "0",
    }
    complete_id = service.ledger.create_report(
        "weekly:2026-08-10:2026-08-16",
        "v1",
        report_type="weekly",
        period_start="2026-08-10T00:00:00+03:00",
        period_end="2026-08-17T00:00:00+03:00",
        policy_id=service.policy.reporting_policy_id,
        totals=totals,
        orders=[],
        generated_at="2026-08-18T10:00:00+03:00",
        metadata={"calculationHash": "complete"},
        report_status="FINAL",
    )
    incomplete_id = service.ledger.create_report(
        "weekly:2026-08-17:2026-08-23",
        "v1",
        report_type="weekly",
        period_start="2026-08-17T00:00:00+03:00",
        period_end="2026-08-24T00:00:00+03:00",
        policy_id=service.policy.reporting_policy_id,
        totals=totals,
        orders=[],
        generated_at="2026-08-25T10:00:00+03:00",
        metadata={"calculationHash": "incomplete"},
        report_status="FINAL",
    )
    complete_paths = service._report_artifact_paths("weekly", "2026", complete_id)
    _write_fake_complete_report_artifacts(
        complete_paths,
        complete_id,
        payload={
            "reportId": complete_id,
            "status": "FINAL",
            "kind": "weekly",
            "period": {
                "from": "2026-08-10",
                "to": "2026-08-16",
                "cumulative": False,
            },
            "totals": {
                "confirmedOrderCount": 0,
                "salesAmountGCny": "0.00",
                "managedOperationsCny": "0.00",
                "combinedAdvertisingAndReturnPoolCny": "0.00",
                "strictOwnerProfitCny": "0.00",
            },
            "orders": [],
        },
    )
    incompatible_paths = service._report_artifact_paths(
        "weekly", "2026", incomplete_id
    )
    _write_fake_complete_report_artifacts(
        incompatible_paths,
        incomplete_id,
        payload={
            "reportId": incomplete_id,
            "status": "FINAL",
            "kind": "weekly",
            "period": {
                "from": "2026-08-17",
                "to": "2026-08-23",
                "cumulative": False,
            },
            # A formally published file set that cannot support the v2 bridge
            # must not suppress the next older compatible baseline.
            "totals": {},
            "orders": [],
        },
    )
    current_period = type(
        "TestPeriod",
        (),
        {
            "kind": "weekly",
            "start": datetime(2026, 8, 24).date(),
            "end": datetime(2026, 8, 30).date(),
            "cumulative": False,
        },
    )()

    previous = service._previous_published_report_payload(current_period)

    assert previous["reportId"] == complete_id
    service.close()


def test_previous_report_payload_honors_explicit_restatement_predecessor(
    tmp_path,
    monkeypatch,
):
    service = _service(
        tmp_path,
        monkeypatch,
        datetime(2026, 9, 1, 9, 30, tzinfo=SHANGHAI),
    )
    ledger_totals = {
        "revenue_cny": "0",
        "cost_cny": "0",
        "profit_cny": "0",
        "managed_operations_cny": "0",
        "return_fee_cny": "0",
        "advertising_cny": "0",
    }

    def add_report(start: str, end: str, generated: str) -> str:
        report_id = service.ledger.create_report(
            f"weekly:{start}:{end}",
            "v1",
            report_type="weekly",
            period_start=f"{start}T00:00:00+03:00",
            period_end=(
                datetime.fromisoformat(end) + timedelta(days=1)
            ).date().isoformat()
            + "T00:00:00+03:00",
            policy_id=service.policy.reporting_policy_id,
            totals=ledger_totals,
            orders=[],
            generated_at=generated,
            metadata={"calculationHash": generated},
            report_status="FINAL",
        )
        paths = service._report_artifact_paths("weekly", "2026", report_id)
        _write_fake_complete_report_artifacts(
            paths,
            report_id,
            payload={
                "reportId": report_id,
                "status": "FINAL",
                "kind": "weekly",
                "period": {"from": start, "to": end, "cumulative": False},
                "totals": {
                    "confirmedOrderCount": 0,
                    "salesAmountGCny": "0.00",
                    "managedOperationsCny": "0.00",
                    "combinedAdvertisingAndReturnPoolCny": "0.00",
                    "strictOwnerProfitCny": "0.00",
                },
                "orders": [],
            },
        )
        return report_id

    predecessor_id = add_report(
        "2026-08-10",
        "2026-08-16",
        "2026-08-18T10:00:00+03:00",
    )
    add_report(
        "2026-08-17",
        "2026-08-23",
        "2026-08-25T10:00:00+03:00",
    )
    current_period = type(
        "TestPeriod",
        (),
        {
            "kind": "weekly",
            "start": datetime(2026, 8, 24).date(),
            "end": datetime(2026, 8, 30).date(),
            "cumulative": False,
        },
    )()

    previous = service._previous_published_report_payload(
        current_period,
        preferred_report_id=predecessor_id,
    )

    assert previous["reportId"] == predecessor_id
    service.close()


def test_explicit_report_publishes_missing_idempotent_pending_candidate(
    tmp_path, monkeypatch
):
    service = _service(
        tmp_path,
        monkeypatch,
        datetime(2026, 8, 28, 9, 30, tzinfo=SHANGHAI),
    )
    written = []

    def write_report(payload, _period):
        written.append(deepcopy(payload))
        return {
            "json": "pending-report.json",
            "markdown": "pending-report.md",
            "workbook": "pending-report.xlsx",
            "verification": "pending-report.verification.json",
        }

    service._write_report = write_report
    try:
        candidate = service.report(
            kind="weekly",
            period="2026-W34",
            _publish_open_items=False,
        )
        assert candidate["status"] == "PENDING_DATA"
        assert candidate["artifacts"] is None
        assert written == []

        explicit = service.report(kind="weekly", period="2026-W34")

        assert explicit["reportId"] == candidate["reportId"]
        assert explicit["reportVersion"] == candidate["reportVersion"]
        assert explicit["status"] == candidate["status"]
        assert explicit["idempotent"] is True
        assert set(explicit["artifacts"]) == {
            "json",
            "markdown",
            "workbook",
            "verification",
        }
        assert len(written) == 1
        assert written[0]["reportId"] == candidate["reportId"]
        assert written[0]["reportVersion"] == candidate["reportVersion"]
        assert written[0]["status"] == candidate["status"]
        assert written[0]["blockers"] == explicit["blockers"]
    finally:
        service.close()


def test_daily_does_not_publish_missing_idempotent_pending_candidate(
    tmp_path, monkeypatch
):
    service = _service(
        tmp_path,
        monkeypatch,
        datetime(2026, 8, 28, 9, 30, tzinfo=SHANGHAI),
    )
    _import_costs(service, tmp_path)
    service._source_blockers = lambda _selected, _evidence: [
        {"code": "TEST_OPEN_ITEM", "message": "keep candidate pending"}
    ]
    written_report_ids = []

    def write_report(payload, _period):
        written_report_ids.append(payload["reportId"])
        return {"json": "unexpected.json"}

    service._write_report = write_report
    try:
        first = service.daily(through="2026-08-27")
        first_weekly = next(
            item for item in first["reports"] if item["reportKey"].startswith("weekly:")
        )
        assert first_weekly["status"] == "PENDING_DATA"
        assert first_weekly["artifacts"] is None
        assert first_weekly["reportId"] not in written_report_ids

        second = service.daily(through="2026-08-27")
        second_weekly = next(
            item for item in second["reports"] if item["reportKey"].startswith("weekly:")
        )
        assert second_weekly["reportId"] == first_weekly["reportId"]
        assert second_weekly["reportVersion"] == first_weekly["reportVersion"]
        assert second_weekly["status"] == "PENDING_DATA"
        assert second_weekly["artifacts"] is None
        assert second_weekly["reportId"] not in written_report_ids
    finally:
        service.close()


def test_cost_import_replay_keeps_version_and_approval_timestamp(tmp_path, monkeypatch):
    service = _service(
        tmp_path,
        monkeypatch,
        datetime(2026, 8, 28, 9, 30, tzinfo=SHANGHAI),
    )
    _import_costs(service, tmp_path)
    original = service.ledger.connection.execute(
        "SELECT cost_version_uid, approved_at FROM cost_versions ORDER BY cost_version_uid"
    ).fetchall()
    _import_costs(service, tmp_path)
    replay = service.ledger.connection.execute(
        "SELECT cost_version_uid, approved_at FROM cost_versions ORDER BY cost_version_uid"
    ).fetchall()
    assert [tuple(row) for row in replay] == [tuple(row) for row in original]
    service.close()


def test_cost_import_requires_explicit_exchange_rate(tmp_path, monkeypatch):
    service = _service(
        tmp_path,
        monkeypatch,
        datetime(2026, 8, 31, 9, 30, tzinfo=SHANGHAI),
    )
    quote = tmp_path / "quote.json"
    quote.write_text(
        json.dumps([{"规格": "iPhone 13-128gb-Black", "成本价格": 4600}], ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(FinanceServiceError, match="explicit --rub-per-cny"):
        service.cost_import(
            quote=quote,
            effective_from="2026-08-31T00:00:00+03:00",
            approved_by="tester",
            rub_per_cny=None,
            source_currency="RUB",
        )
    service.close()


def test_cost_import_accepts_cny_source_and_preserves_rub_evidence(
    tmp_path, monkeypatch
):
    service = _service(
        tmp_path,
        monkeypatch,
        datetime(2026, 9, 6, 18, 30, tzinfo=SHANGHAI),
    )
    quote = tmp_path / "0830.json"
    quote.write_text(
        json.dumps(
            [
                {
                    "规格": "iPhone 13-128gb-Black",
                    "人民币报价（¥）": 400,
                    "卢布报价": 4600,
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = service.cost_import(
        quote=quote,
        effective_from="2026-09-06T13:08:38+03:00",
        approved_by="tester",
        source_currency="CNY",
    )
    selected = service.ledger.select_cost_version(
        "IPHONE 13-128GB-BLACK", "2026-09-06T13:08:38+03:00"
    )
    evidence = service.ledger.connection.execute(
        "SELECT source_currency, purchase_cost_rub FROM cost_versions"
    ).fetchone()

    assert result["rowCount"] == 1
    assert result["sourceCurrency"] == "CNY"
    assert result["exchangeRateRubPerCny"] == "11.5"
    assert selected.purchase_cost_cny == Decimal("400.00")
    assert evidence["source_currency"] == "CNY"
    assert Decimal(evidence["purchase_cost_rub"]) == Decimal("4600")
    service.close()


def _finalized_weekly_service(tmp_path, monkeypatch):
    first_now = datetime(2026, 8, 28, 9, 30, tzinfo=SHANGHAI)
    first = _service(tmp_path, monkeypatch, first_now)
    _import_costs(first, tmp_path)
    first.daily(through="2026-08-27")
    first.close()

    second = _service(tmp_path, monkeypatch, first_now + timedelta(days=1))
    result = second.daily(through="2026-08-28")
    weekly = next(item for item in result["reports"] if item["period"]["to"] == "2026-08-23")
    assert weekly["status"] == "FINAL"
    return second, weekly


def test_pending_weekly_report_allows_settled_only_funds_plan(tmp_path, monkeypatch):
    service = _service(
        tmp_path,
        monkeypatch,
        datetime(2026, 8, 28, 9, 30, tzinfo=SHANGHAI),
    )
    _import_costs(service, tmp_path)
    result = service.daily(through="2026-08-27")
    weekly = next(
        item for item in result["reports"] if item["reportKey"].startswith("weekly:")
    )
    assert weekly["status"] == "PENDING_DATA"

    plan = service.funds_plan(report_id=weekly["reportId"])

    assert plan["reportEvidenceStatus"] == "PENDING_DATA"
    assert plan["allocationMode"] == "SETTLED_ONLY_WITH_OPEN_ITEMS"
    assert len(plan["stores"]) == 2
    service.close()


def test_late_finance_correction_requires_two_new_stable_captures_and_restates(
    tmp_path,
    monkeypatch,
):
    service, original = _finalized_weekly_service(tmp_path, monkeypatch)
    monkeypatch.setattr(FakeSources, "return_logistics_cny", "8")

    service.now = datetime(2026, 9, 12, 10, 30, tzinfo=SHANGHAI)
    first_changed = service.daily(through="2026-09-11")
    pending = next(
        item for item in first_changed["reports"] if item["period"]["to"] == "2026-08-23"
    )
    assert pending["status"] == "PENDING_DATA"
    assert any(item["code"] == "SOURCE_NOT_STABLE_TWICE" for item in pending["blockers"])

    service.now = datetime(2026, 9, 12, 11, 30, tzinfo=SHANGHAI)
    second_changed = service.daily(through="2026-09-11")
    restated = next(
        item for item in second_changed["reports"] if item["period"]["to"] == "2026-08-23"
    )
    assert restated["status"] == "RESTATED"
    assert restated["reportVersion"] == "v2"
    report = service.ledger.get_report(restated["reportId"])
    assert report["previous_report_id"] == original["reportId"]
    returned = next(row["snapshot"] for row in report["orders"] if row["snapshot"]["outcome"] == "returned")
    assert returned["returnLogisticsCny"] == "8.00"
    assert returned["ownerProfitCny"] == "-8.00"

    repeated = service.report(kind="weekly", period="2026-W34")
    assert repeated["reportId"] == restated["reportId"]
    assert repeated["idempotent"] is True
    service.close()


def test_pending_historical_candidate_is_revisited_after_window_moves(
    tmp_path,
    monkeypatch,
):
    service, original = _finalized_weekly_service(tmp_path, monkeypatch)
    monkeypatch.setattr(FakeSources, "return_logistics_cny", "8")

    service.now = datetime(2026, 9, 12, 10, 30, tzinfo=SHANGHAI)
    first = service.daily(through="2026-09-11")
    pending = next(
        item for item in first["reports"] if item["period"]["to"] == "2026-08-23"
    )
    assert pending["status"] == "PENDING_DATA"

    # The next scheduled run advances the rolling sync window and therefore
    # must still revisit the pending historical candidate instead of dropping
    # it when it no longer overlaps the new API window.
    service.now = datetime(2026, 9, 13, 10, 30, tzinfo=SHANGHAI)
    second = service.daily(through="2026-09-12")
    restated = next(
        item for item in second["reports"] if item["period"]["to"] == "2026-08-23"
    )
    assert restated["status"] == "RESTATED"
    assert restated["reportVersion"] == "v2"
    report = service.ledger.get_report(restated["reportId"])
    assert report["previous_report_id"] == original["reportId"]
    service.close()


def test_cross_week_return_is_reported_as_delta_and_reverses_advertising_accrual(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(FakeSources, "include_return", False)
    first_now = datetime(2026, 8, 28, 9, 30, tzinfo=SHANGHAI)
    first = _service(tmp_path, monkeypatch, first_now)
    _import_costs(first, tmp_path)
    first.daily(through="2026-08-27")
    first.close()

    service = _service(tmp_path, monkeypatch, first_now + timedelta(days=1))
    initial = service.daily(through="2026-08-28")
    initial_week = next(
        item for item in initial["reports"] if item["period"]["to"] == "2026-08-23"
    )
    assert initial_week["status"] == "FINAL"
    initial_report = service.ledger.get_report(initial_week["reportId"])
    prior = next(
        row["snapshot"]
        for row in initial_report["orders"]
        if row["snapshot"]["identities"]["orderId"] == ["order-return"]
    )
    assert prior["outcome"] == "completed"
    assert prior["advertisingProvisionCny"] == "15.00"

    monkeypatch.setattr(FakeSources, "include_return", True)
    monkeypatch.setattr(FakeSources, "return_event_date", "2026-09-08")
    service.now = datetime(2026, 9, 17, 10, 30, tzinfo=SHANGHAI)
    first_return_capture = service.daily(through="2026-09-16")
    return_week = next(
        item
        for item in first_return_capture["reports"]
        if item["period"]["to"] == "2026-09-13"
    )
    assert return_week["status"] == "PENDING_DATA"

    service.now = datetime(2026, 9, 17, 11, 30, tzinfo=SHANGHAI)
    second_return_capture = service.daily(through="2026-09-16")
    return_week = next(
        item
        for item in second_return_capture["reports"]
        if item["period"]["to"] == "2026-09-13"
    )
    assert return_week["status"] == "FINAL"
    report = service.ledger.get_report(return_week["reportId"])
    correction = report["orders"][0]["snapshot"]
    assert correction["outcome"] == "corrected"
    assert correction["priorOutcome"] == "completed"
    assert correction["currentOutcome"] == "returned"
    assert correction["netQuantity"] == "-1"
    assert correction["netTurnoverCny"] == "-500.00"
    assert correction["purchaseCostCny"] == "-400.00"
    assert correction["advertisingProvisionCny"] == "-15.00"
    assert correction["returnLogisticsCny"] == "16.00"
    assert Decimal(correction["ownerProfitCny"]) == (
        Decimal("-16.00") - Decimal(prior["ownerProfitCny"])
    )
    assert report["revenue_cny"] == "-500.00"
    assert report["advertising_cny"] == "-15.00"
    assert report["return_fee_cny"] == "16.00"

    accruals = service._advertising_accruals("owner-store")
    assert len(accruals) == 1
    assert accruals[0].amount_cny == Decimal("30.00")
    service.close()


def test_funds_plan_uses_only_latest_weekly_versions_and_post_activation_accruals(
    tmp_path,
    monkeypatch,
):
    service, original = _finalized_weekly_service(tmp_path, monkeypatch)
    original_report = service.ledger.get_report(original["reportId"])
    completed = next(
        row for row in original_report["orders"] if row["snapshot"]["outcome"] == "completed"
    )
    second_order = next(
        row for row in original_report["orders"] if row["order_key"] != completed["order_key"]
    )
    post_activation_snapshot = deepcopy(completed["snapshot"])
    post_activation_snapshot["completedAt"] = "2026-08-29T10:00:00+03:00"
    post_activation_snapshot["orderKey"] = second_order["order_key"]
    report_order = {
        "store_alias": second_order["store_alias"],
        "order_key": second_order["order_key"],
        "profit_version": second_order["profit_version"],
        "owner_profit_cny": second_order["owner_profit_cny"],
        "snapshot": post_activation_snapshot,
    }
    totals = {
        "revenue_cny": original_report["revenue_cny"],
        "cost_cny": original_report["cost_cny"],
        "profit_cny": original_report["profit_cny"],
        "managed_operations_cny": original_report["managed_operations_cny"],
        "return_fee_cny": original_report["return_fee_cny"],
        "advertising_cny": original_report["advertising_cny"],
    }
    latest_id = service.ledger.create_report(
        "weekly:2026-08-24:2026-08-30",
        "v1",
        report_type="weekly",
        period_start="2026-08-24T00:00:00+03:00",
        period_end="2026-08-31T00:00:00+03:00",
        policy_id=service.policy.reporting_policy_id,
        totals=totals,
        orders=[report_order],
        generated_at="2026-09-03T10:00:00+03:00",
        metadata={"calculationHash": "synthetic-weekly"},
        report_status="FINAL",
    )
    service.ledger.create_report(
        "monthly:2026-08-01:2026-08-31",
        "v1",
        report_type="monthly",
        period_start="2026-08-01T00:00:00+03:00",
        period_end="2026-09-01T00:00:00+03:00",
        policy_id=service.policy.reporting_policy_id,
        totals=totals,
        orders=[report_order],
        generated_at="2026-09-04T10:00:00+03:00",
        metadata={"calculationHash": "synthetic-monthly"},
        report_status="FINAL",
    )
    confirmed_action_id = service.ledger.record_funding_action(
        "confirmed-deposit-test",
        "owner-store",
        action_type="advertising_top_up",
        requested_amount_cny="10",
        report_id=latest_id,
        status="proposed",
        requested_at="2026-08-30T10:00:00+03:00",
    )
    service.ledger.update_funding_action_status(
        confirmed_action_id,
        "readback_confirmed",
        occurred_at="2026-08-30T10:01:00+03:00",
    )
    historical_action_id = service.ledger.record_funding_action(
        "pre-activation-confirmed-deposit-test",
        "owner-store",
        action_type="advertising_top_up",
        requested_amount_cny="50",
        report_id=latest_id,
        status="proposed",
        requested_at="2026-08-27T10:00:00+03:00",
    )
    service.ledger.update_funding_action_status(
        historical_action_id,
        "executing",
        occurred_at="2026-08-27T10:01:00+03:00",
    )
    service.ledger.update_funding_action_status(
        historical_action_id,
        "readback_confirmed",
        occurred_at="2026-08-29T10:01:00+03:00",
    )

    accruals = service._advertising_accruals("owner-store")
    assert len(accruals) == 2  # the monthly copy is excluded
    historical = next(item for item in accruals if item.accrued_at.date().isoformat() == "2026-08-20")
    eligible = next(item for item in accruals if item.accrued_at.date().isoformat() == "2026-08-29")
    # The historical week has one completed sale (30 CNY pool) and one
    # returned order with 16 CNY actual logistics, so its store-period reserve
    # is 14 CNY after aggregate pool consumption.  The synthetic later week
    # contributes a separate 30 CNY store-period reserve.
    assert historical.amount_cny == Decimal("14.00")
    assert eligible.amount_cny == Decimal("30.00")
    assert historical.allocated_cny == 0
    assert eligible.allocated_cny == 10

    with pytest.raises(FinanceServiceError, match="superseded"):
        service.funds_plan(report_id=original["reportId"])

    plan = service.funds_plan(report_id=latest_id)
    owner = next(item for item in plan["stores"] if item["store"] == "owner-store")
    assert owner["components"]["postActivationUnallocatedAdvertisingAccrualCny"] == "20.00"
    assert owner["components"]["historicalAdvertisingAccrualExcludedCny"] == "14.00"
    assert owner["suggestedWithdrawalCny"] == "4980.00"
    service.close()


def test_funds_plan_rejects_stale_or_missing_balance_evidence(tmp_path, monkeypatch):
    service, weekly = _finalized_weekly_service(tmp_path, monkeypatch)
    service.now = service.now + timedelta(hours=1, seconds=1)
    with pytest.raises(FinanceServiceError, match="stale"):
        service.funds_plan(report_id=weekly["reportId"])

    service.ledger.connection.execute(
        "DELETE FROM raw_balance WHERE store_alias='owner-store'"
    )
    service.ledger.connection.commit()
    with pytest.raises(FinanceServiceError, match="No platform withdrawable-balance evidence"):
        service.funds_plan(report_id=weekly["reportId"])
    service.close()


class _FakeFundingClient:
    def __init__(
        self,
        *,
        source_balance: str = "500",
        campaign_status: int = 9,
        campaign_http_status: int = 200,
        budget_after: str = "140",
        campaign_shape: str = "detail",
        seller_currency: str | None = "CNY",
        seller_http_status: int = 200,
        advertising_currency: str | None = None,
        post_status: int = 200,
        post_error: bool = False,
    ) -> None:
        self.source_balance = source_balance
        self.campaign_status = campaign_status
        self.campaign_http_status = campaign_http_status
        self.budget_after = budget_after
        self.campaign_shape = campaign_shape
        self.seller_currency = seller_currency
        self.seller_http_status = seller_http_status
        self.advertising_currency = advertising_currency
        self.post_status = post_status
        self.post_error = post_error
        self.calls: list[tuple[str, str, object]] = []
        self.budget_reads = 0

    def request(self, method, url, *, body=None, retries=2, **kwargs):
        self.calls.append((method, url, body))
        if "/api/advert/v2/adverts" in url:
            if self.campaign_http_status != 200:
                return ApiResult(self.campaign_http_status, False, {"error": "denied"}, {})
            if self.campaign_shape == "detail":
                data = {"adverts": [{"id": 1234567, "status": self.campaign_status}]}
            elif self.campaign_shape == "grouped":
                data = {
                    "adverts": [
                        {
                            "type": 8,
                            "status": self.campaign_status,
                            "count": 1,
                            "advert_list": [{"advertId": 1234567}],
                        }
                    ]
                }
            else:
                data = {"adverts": [{"advertId": 1234567, "status": 9}]}
            return ApiResult(200, True, data, {})
        if url.endswith("/api/v1/account/balance"):
            if self.seller_http_status != 200:
                return ApiResult(self.seller_http_status, False, {"error": "denied"}, {})
            data = {"current": 100000, "for_withdraw": 50000}
            if self.seller_currency is not None:
                data["currency"] = self.seller_currency
            return ApiResult(200, True, data, {})
        if url.endswith("/adv/v1/balance"):
            data = {
                "balance": "1000",
                "net": self.source_balance,
                "bonus": "0",
            }
            if self.advertising_currency is not None:
                data["currency"] = self.advertising_currency
            return ApiResult(
                200,
                True,
                data,
                {},
            )
        if "/adv/v1/budget/deposit" in url:
            if self.post_error:
                raise TimeoutError("deposit transport result unknown")
            return ApiResult(
                self.post_status,
                200 <= self.post_status < 300,
                {"accepted": self.post_status == 200},
                {},
            )
        if "/adv/v1/budget?id=" in url:
            self.budget_reads += 1
            total = "40" if self.budget_reads == 1 else self.budget_after
            return ApiResult(200, True, {"cash": "0", "netting": "0", "total": total}, {})
        raise AssertionError(f"Unexpected funding request: {method} {url}")


@pytest.mark.parametrize(
    "payload",
    [
        {"adverts": [{"id": 1234567, "status": 9, "nm_settings": []}]},
        {
            "adverts": [
                {
                    "type": 8,
                    "status": 9,
                    "count": 1,
                    "advert_list": [{"advertId": 1234567}],
                }
            ]
        },
    ],
)
def test_campaign_preflight_accepts_only_repository_evidenced_wb_shapes(payload):
    assert _campaign_status_from_known_shapes(payload, 1234567) == 9


def _advertising_deposit_plan_file(
    service: FinanceService,
    tmp_path: Path,
    report_id: str,
) -> Path:
    approved_at = service._now()
    plan = build_advertising_deposit_plan(
        report_id=report_id,
        store="owner-store",
        campaign=1234567,
        amount_cny="100",
        source="balance",
        currency="CNY",
        source_balance_cny="500",
        source_balance_evidence_at=approved_at - timedelta(minutes=5),
        policy_cap_cny="120",
        available_advertising_reserve_cny="120",
        approved_by="boss",
        approved_at=approved_at,
        expires_at=approved_at + timedelta(minutes=15),
    )
    path = tmp_path / "advertising-deposit-plan.json"
    path.write_text(
        json.dumps(plan.as_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _weekly_funds_plan_file(
    service: FinanceService,
    tmp_path: Path,
    report_id: str,
    *,
    reserve_cny: str = "120",
) -> Path:
    plan = build_funding_plan(
        report_id=report_id,
        store="owner-store",
        platform_available_balance_cny="5000",
        balance_evidence_at=service._now(),
        formal_activation_at="2026-08-28T00:00:00+08:00",
        advertising_accruals=[
            AdvertisingAccrual(
                accrual_id="test-current-reserve",
                amount_cny=Decimal(reserve_cny),
                allocated_cny=Decimal("0"),
                accrued_at=service._now(),
            )
        ],
    )
    payload = {
        "schemaVersion": 1,
        "planType": "weekly_funding",
        "reportId": report_id,
        "generatedAt": service._now().isoformat(),
        "withdrawalExecutionSupported": False,
        "stores": [plan.as_dict()],
    }
    path = tmp_path / "weekly-funds-plan.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _synthetic_final_weekly_service(tmp_path, monkeypatch):
    service = _service(
        tmp_path,
        monkeypatch,
        datetime(2026, 9, 3, 10, 0, tzinfo=SHANGHAI),
    )
    report_id = service.ledger.create_report(
        "weekly:2026-08-24:2026-08-30",
        "v1",
        report_type="weekly",
        period_start="2026-08-24T00:00:00+03:00",
        period_end="2026-08-31T00:00:00+03:00",
        policy_id=service.policy.reporting_policy_id,
        totals={
            "revenue_cny": "0",
            "cost_cny": "0",
            "profit_cny": "0",
            "managed_operations_cny": "0",
            "return_fee_cny": "0",
            "advertising_cny": "0",
        },
        orders=[],
        generated_at=service._now(),
        metadata={"calculationHash": "synthetic-funding-test"},
        report_status="FINAL",
    )
    return service, {"reportId": report_id}


def test_ads_deposit_plan_is_get_only_balance_forced_and_executor_compatible(
    tmp_path,
    monkeypatch,
):
    service, weekly = _synthetic_final_weekly_service(tmp_path, monkeypatch)
    funds_path = _weekly_funds_plan_file(
        service,
        tmp_path,
        weekly["reportId"],
    )
    client = _FakeFundingClient(source_balance="500")
    monkeypatch.setattr("wb_manager.finance_service.WbClient", lambda token: client)
    monkeypatch.setattr(
        service,
        "_available_advertising_reserve_cny",
        lambda store_alias, *, exclude_action_id=None: Decimal("120.00"),
    )

    result = service.ads_deposit_plan(
        funds_plan_path=funds_path,
        store_alias="owner-store",
        campaign=1234567,
        amount_cny=100,
        approved_by="boss",
    )

    assert result["status"] == "immutable_plan_created"
    assert result["networkWriteAttempted"] is False
    assert result["source"] == "balance"
    assert [call[0] for call in client.calls] == ["GET", "GET", "GET", "GET"]
    assert not any("/adv/v1/budget/deposit" in call[1] for call in client.calls)
    assert service.ledger.query_funding_actions(
        store_alias="owner-store", action_type="advertising_top_up"
    ) == []

    plan = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    assert plan["store"] == "owner-store"
    assert plan["campaign"] == 1234567
    assert plan["amountCny"] == "100.00"
    assert plan["source"] == "balance"
    assert plan["currency"] == "CNY"
    assert plan["sourceBalanceCny"] == "500.00"
    assert plan["policyCapCny"] == "120.00"
    assert plan["availableAdvertisingReserveCny"] == "120.00"
    assert datetime.fromisoformat(plan["expiresAt"]) - datetime.fromisoformat(
        plan["approvedAt"]
    ) == timedelta(minutes=15)

    evidence = json.loads(Path(result["preflightPath"]).read_text(encoding="utf-8"))
    assert evidence["networkWriteAttempted"] is False
    assert evidence["planFingerprint"] == plan["fingerprint"]
    assert evidence["livePreflight"]["campaignBudgetBeforeCny"] == "40.00"
    assert evidence["livePreflight"]["source"] == "balance"

    proposal = service.ads_deposit(
        plan_path=Path(result["path"]),
        approved_by="boss",
        confirm_live=False,
    )
    assert proposal["status"] == "proposal_recorded"
    assert [call[0] for call in client.calls] == ["GET", "GET", "GET", "GET"]
    service.close()


@pytest.mark.parametrize(
    ("amount", "message"),
    [
        ("100.5", "whole positive CNY"),
        (121, "ledger-derived available reserve"),
    ],
)
def test_ads_deposit_plan_rejects_invalid_amount_before_live_reads(
    tmp_path,
    monkeypatch,
    amount,
    message,
):
    service, weekly = _synthetic_final_weekly_service(tmp_path, monkeypatch)
    funds_path = _weekly_funds_plan_file(service, tmp_path, weekly["reportId"])
    client = _FakeFundingClient()
    monkeypatch.setattr("wb_manager.finance_service.WbClient", lambda token: client)
    monkeypatch.setattr(
        service,
        "_available_advertising_reserve_cny",
        lambda store_alias, *, exclude_action_id=None: Decimal("120.00"),
    )

    with pytest.raises(FinanceServiceError, match=message):
        service.ads_deposit_plan(
            funds_plan_path=funds_path,
            store_alias="owner-store",
            campaign=1234567,
            amount_cny=amount,
            approved_by="boss",
        )
    assert client.calls == []
    service.close()


def test_ads_deposit_plan_rejects_stale_funds_plan_and_insufficient_live_balance(
    tmp_path,
    monkeypatch,
):
    service, weekly = _synthetic_final_weekly_service(tmp_path, monkeypatch)
    funds_path = _weekly_funds_plan_file(service, tmp_path, weekly["reportId"])
    client = _FakeFundingClient(source_balance="99")
    monkeypatch.setattr("wb_manager.finance_service.WbClient", lambda token: client)
    monkeypatch.setattr(
        service,
        "_available_advertising_reserve_cny",
        lambda store_alias, *, exclude_action_id=None: Decimal("119.00"),
    )
    with pytest.raises(FinanceServiceError, match="no longer matches"):
        service.ads_deposit_plan(
            funds_plan_path=funds_path,
            store_alias="owner-store",
            campaign=1234567,
            amount_cny=100,
            approved_by="boss",
        )
    assert client.calls == []

    monkeypatch.setattr(
        service,
        "_available_advertising_reserve_cny",
        lambda store_alias, *, exclude_action_id=None: Decimal("120.00"),
    )
    with pytest.raises(FinanceServiceError, match="live WB balance source"):
        service.ads_deposit_plan(
            funds_plan_path=funds_path,
            store_alias="owner-store",
            campaign=1234567,
            amount_cny=100,
            approved_by="boss",
        )
    assert [call[0] for call in client.calls] == ["GET", "GET", "GET", "GET"]
    assert not any("/adv/v1/budget/deposit" in call[1] for call in client.calls)
    service.close()


def test_ads_deposit_executor_rejects_bonus_plan_before_recording_proposal(
    tmp_path,
    monkeypatch,
):
    service, weekly = _synthetic_final_weekly_service(tmp_path, monkeypatch)
    approved_at = service._now()
    plan = build_advertising_deposit_plan(
        report_id=weekly["reportId"],
        store="owner-store",
        campaign=1234567,
        amount_cny="100",
        source="bonus",
        currency="CNY",
        source_balance_cny="500",
        source_balance_evidence_at=approved_at - timedelta(minutes=5),
        policy_cap_cny="120",
        available_advertising_reserve_cny="120",
        approved_by="boss",
        approved_at=approved_at,
        expires_at=approved_at + timedelta(minutes=15),
    )
    path = tmp_path / "bonus-plan.json"
    path.write_text(
        json.dumps(plan.as_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(FinanceServiceError, match="must use WB balance"):
        service.ads_deposit(
            plan_path=path,
            approved_by="boss",
            confirm_live=False,
        )
    assert service.ledger.query_funding_actions(
        store_alias="owner-store", action_type="advertising_top_up"
    ) == []
    service.close()


def test_ads_deposit_rechecks_all_live_evidence_and_confirms_exact_readback(
    tmp_path,
    monkeypatch,
):
    service, weekly = _finalized_weekly_service(tmp_path, monkeypatch)
    plan_path = _advertising_deposit_plan_file(
        service, tmp_path, weekly["reportId"]
    )
    client = _FakeFundingClient()
    monkeypatch.setattr("wb_manager.finance_service.WbClient", lambda token: client)
    monkeypatch.setattr(
        service,
        "_available_advertising_reserve_cny",
        lambda store_alias, *, exclude_action_id=None: Decimal("120.00"),
    )
    lock_ttls = []
    original_acquire_lock = service.ledger.acquire_lock

    def acquire_lock(name, owner, *, ttl_seconds, now=None):
        lock_ttls.append(ttl_seconds)
        return original_acquire_lock(name, owner, ttl_seconds=ttl_seconds, now=now)

    monkeypatch.setattr(service.ledger, "acquire_lock", acquire_lock)

    dry_run = service.ads_deposit(
        plan_path=plan_path,
        approved_by="boss",
        confirm_live=False,
    )
    assert dry_run["status"] == "proposal_recorded"
    assert client.calls == []
    proposal = service.ledger.query_funding_actions(
        store_alias="owner-store", action_type="advertising_top_up"
    )[0]
    assert proposal["lifecycle_status"] == "proposed"

    result = service.ads_deposit(
        plan_path=plan_path,
        approved_by="boss",
        confirm_live=True,
    )
    assert result["status"] == "readback_confirmed"
    assert result["readback"]["matched"] is True
    assert [call[0] for call in client.calls] == [
        "GET", "GET", "GET", "GET", "POST", "GET"
    ]
    assert "/api/advert/v2/adverts" in client.calls[0][1]
    assert "/adv/v1/budget?id=" in client.calls[1][1]
    assert client.calls[2][1].endswith("/api/v1/account/balance")
    assert client.calls[3][1].endswith("/adv/v1/balance")
    assert client.calls[4][2] == {"sum": 100, "type": 1, "return": True}
    assert lock_ttls == [900]

    with pytest.raises(FinanceServiceError, match="completed readback"):
        service.ads_deposit(
            plan_path=plan_path,
            approved_by="boss",
            confirm_live=True,
        )
    assert sum(call[0] == "POST" for call in client.calls) == 1
    service.close()


def test_ads_deposit_confirm_live_requires_prior_dry_run_proposal(tmp_path, monkeypatch):
    service, weekly = _finalized_weekly_service(tmp_path, monkeypatch)
    plan_path = _advertising_deposit_plan_file(service, tmp_path, weekly["reportId"])
    client = _FakeFundingClient()
    monkeypatch.setattr("wb_manager.finance_service.WbClient", lambda token: client)
    monkeypatch.setattr(
        service,
        "_available_advertising_reserve_cny",
        lambda store_alias, *, exclude_action_id=None: Decimal("120"),
    )

    with pytest.raises(FinanceServiceError, match="prior dry-run proposal"):
        service.ads_deposit(
            plan_path=plan_path,
            approved_by="boss",
            confirm_live=True,
        )
    assert client.calls == []
    service.close()


@pytest.mark.parametrize(
    ("client_kwargs", "reserve", "message"),
    [
        ({"source_balance": "499"}, "120", "source balance changed"),
        ({"campaign_status": 7}, "120", "not eligible"),
        ({"campaign_http_status": 401}, "120", "HTTP 401"),
        ({"seller_currency": "RUB"}, "120", "seller-balance currency"),
        ({"seller_currency": None}, "120", "seller-balance currency"),
        ({"seller_http_status": 401}, "120", "HTTP 401"),
        ({"advertising_currency": "RUB"}, "120", "advertising-balance currency"),
        ({"campaign_shape": "unknown"}, "120", "unsupported campaign row"),
        ({}, "119", "ledger-derived available advertising reserve"),
    ],
)
def test_ads_deposit_fails_closed_before_post_when_live_scope_changed(
    tmp_path,
    monkeypatch,
    client_kwargs,
    reserve,
    message,
):
    service, weekly = _finalized_weekly_service(tmp_path, monkeypatch)
    plan_path = _advertising_deposit_plan_file(
        service, tmp_path, weekly["reportId"]
    )
    client = _FakeFundingClient(**client_kwargs)
    monkeypatch.setattr("wb_manager.finance_service.WbClient", lambda token: client)
    monkeypatch.setattr(
        service,
        "_available_advertising_reserve_cny",
        lambda store_alias, *, exclude_action_id=None: Decimal("120"),
    )
    service.ads_deposit(
        plan_path=plan_path,
        approved_by="boss",
        confirm_live=False,
    )
    monkeypatch.setattr(
        service,
        "_available_advertising_reserve_cny",
        lambda store_alias, *, exclude_action_id=None: Decimal(reserve),
    )

    with pytest.raises(FinanceServiceError, match=message):
        service.ads_deposit(
            plan_path=plan_path,
            approved_by="boss",
            confirm_live=True,
        )
    assert not any(call[0] == "POST" for call in client.calls)
    action = service.ledger.query_funding_actions(
        store_alias="owner-store", action_type="advertising_top_up"
    )[0]
    assert action["lifecycle_status"] == "cancelled"
    assert service._funding_action_locks_reserve(action) is False
    service.close()


def test_expired_unexecuted_approval_is_cancelled_and_does_not_lock_reserve(
    tmp_path,
    monkeypatch,
):
    service, weekly = _finalized_weekly_service(tmp_path, monkeypatch)
    plan_path = _advertising_deposit_plan_file(service, tmp_path, weekly["reportId"])
    client = _FakeFundingClient()
    monkeypatch.setattr("wb_manager.finance_service.WbClient", lambda token: client)
    monkeypatch.setattr(
        service,
        "_available_advertising_reserve_cny",
        lambda store_alias, *, exclude_action_id=None: Decimal("120"),
    )
    proposal = service.ads_deposit(
        plan_path=plan_path,
        approved_by="boss",
        confirm_live=False,
    )
    service.ledger.update_funding_action_status(
        proposal["actionId"],
        "approved",
        occurred_at=service._now(),
    )
    service.now = service.now + timedelta(minutes=16)
    action = service.ledger.query_funding_actions(
        store_alias="owner-store", action_type="advertising_top_up"
    )[0]
    assert service._funding_action_locks_reserve(action) is False

    with pytest.raises(FinanceServiceError, match="expired"):
        service.ads_deposit(
            plan_path=plan_path,
            approved_by="boss",
            confirm_live=True,
        )
    action = service.ledger.query_funding_actions(
        store_alias="owner-store", action_type="advertising_top_up"
    )[0]
    assert action["lifecycle_status"] == "cancelled"
    assert client.calls == []
    service.close()


def test_ads_deposit_unknown_readback_is_never_retried(tmp_path, monkeypatch):
    service, weekly = _finalized_weekly_service(tmp_path, monkeypatch)
    plan_path = _advertising_deposit_plan_file(
        service, tmp_path, weekly["reportId"]
    )
    client = _FakeFundingClient(budget_after="139")
    monkeypatch.setattr("wb_manager.finance_service.WbClient", lambda token: client)
    monkeypatch.setattr(
        service,
        "_available_advertising_reserve_cny",
        lambda store_alias, *, exclude_action_id=None: Decimal("120"),
    )

    service.ads_deposit(
        plan_path=plan_path,
        approved_by="boss",
        confirm_live=False,
    )

    first = service.ads_deposit(
        plan_path=plan_path,
        approved_by="boss",
        confirm_live=True,
    )
    assert first["status"] == "unknown"
    assert first["automaticRetryAllowed"] is False
    assert sum(call[0] == "POST" for call in client.calls) == 1

    client.budget_after = "140"
    resolved = service.ads_deposit(
        plan_path=plan_path,
        approved_by="boss",
        confirm_live=True,
    )
    assert resolved["status"] == "readback_confirmed"
    assert resolved["readbackOnly"] is True
    assert sum(call[0] == "POST" for call in client.calls) == 1
    service.close()


@pytest.mark.parametrize("client_kwargs", [{"post_status": 500}, {"post_error": True}])
def test_ads_deposit_unknown_post_immediately_uses_readback_without_retry(
    tmp_path,
    monkeypatch,
    client_kwargs,
):
    service, weekly = _finalized_weekly_service(tmp_path, monkeypatch)
    plan_path = _advertising_deposit_plan_file(service, tmp_path, weekly["reportId"])
    client = _FakeFundingClient(**client_kwargs)
    monkeypatch.setattr("wb_manager.finance_service.WbClient", lambda token: client)
    monkeypatch.setattr(
        service,
        "_available_advertising_reserve_cny",
        lambda store_alias, *, exclude_action_id=None: Decimal("120"),
    )
    service.ads_deposit(
        plan_path=plan_path,
        approved_by="boss",
        confirm_live=False,
    )

    result = service.ads_deposit(
        plan_path=plan_path,
        approved_by="boss",
        confirm_live=True,
    )
    assert result["status"] == "readback_confirmed"
    assert result["readback"]["matched"] is True
    assert sum(call[0] == "POST" for call in client.calls) == 1
    assert client.budget_reads == 2
    service.close()
