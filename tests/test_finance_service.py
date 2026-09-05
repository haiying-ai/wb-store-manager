from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from wb_manager.client import ApiResult
from wb_manager.finance_funds import build_advertising_deposit_plan
from wb_manager.finance_service import (
    FinanceService,
    FinanceServiceError,
    _campaign_status_from_known_shapes,
)
from wb_manager.finance_sources import SourceResult
from wb_manager.finance_sync import _balance_amounts


SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_live_shape_cny_seller_balance_uses_minor_units():
    account, withdrawable, advertising = _balance_amounts(
        {"currency": "CNY", "current": 1565807, "for_withdraw": 269163},
        "seller_balance",
    )

    assert account == Decimal("15658.07")
    assert withdrawable == Decimal("2691.63")
    assert advertising is None


def _registry(
    path: Path,
    *,
    financial_timezone: str = "Europe/Moscow",
) -> Path:
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "registryId": "test-registry",
                "status": "active",
                "reportingCurrency": "CNY",
                "financialTimezone": financial_timezone,
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
    service._write_report = lambda payload, period: {"json": "verified-by-artifact-tests"}
    return service


def _enable_modern_settlement_policy(service: FinanceService) -> None:
    raw = deepcopy(service.policy.raw)
    raw["effectiveFrom"] = "2026-09-05"
    raw.setdefault("funding", {}).setdefault("advertisingDeposit", {}).update(
        {
            "fundingSource": "balance",
            "platformBalanceField": "net",
            "platformRequestType": 1,
            "deductionMode": "future_sales_settlement_netting",
            "separateWithdrawalRequired": False,
            "manualCashTopUpRequired": False,
            "actualDeductionEvidenceRequired": True,
        }
    )
    raw["funding"]["withdrawalAdvertisingDebt"] = {
        "cutoverEffectiveFrom": "2026-09-05",
        "modeFromCutover": "not_applicable_to_withdrawal",
        "preserveHistoricalRecords": True,
        "noRetroactiveClearing": True,
    }
    service.policy = replace(
        service.policy,
        effective_from="2026-09-05",
        settlement_balance_contract_enabled=True,
        advertising_funding_source="balance",
        advertising_balance_field="net",
        advertising_request_type=1,
        advertising_deduction_mode="future_sales_settlement_netting",
        withdrawal_advertising_debt_cutover=date(2026, 9, 5),
        raw=raw,
    )


def test_debt_reconcile_date_bounds_use_financial_timezone_and_inclusive_through(
    tmp_path, monkeypatch
):
    service = _service(
        tmp_path,
        monkeypatch,
        datetime(2026, 9, 6, 9, 30, tzinfo=SHANGHAI),
    )
    captured = {}

    def fake_import(ledger, store_alias, *, period_start, period_end):
        captured.update(
            store_alias=store_alias,
            period_start=period_start,
            period_end=period_end,
        )
        return {"importedCount": 0}

    monkeypatch.setattr(
        "wb_manager.finance_service.import_advertising_payment_events_from_ledger",
        fake_import,
    )
    try:
        result = service.reconcile_advertising_debt(
            store_alias="owner-store",
            period_start="2026-09-04",
            period_end="2026-09-05",
        )
    finally:
        service.close()

    assert result == {"importedCount": 0}
    assert captured["store_alias"] == "owner-store"
    assert captured["period_start"].isoformat() == "2026-09-04T00:00:00+03:00"
    assert captured["period_end"].isoformat() == "2026-09-06T00:00:00+03:00"


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
    )
    assert result["rowCount"] == 2


def test_withdrawal_cutover_defaults_to_settlement_balance_and_preserves_history(
    tmp_path, monkeypatch
):
    service = _service(
        tmp_path,
        monkeypatch,
        datetime(2026, 9, 6, 9, 30, tzinfo=SHANGHAI),
    )
    _enable_modern_settlement_policy(service)
    try:
        service.withdrawal_record(
            store_alias="owner-store",
            record_key="legacy-withdrawal",
            amount_cny="100.00",
            withdrawn_at="2026-09-04T12:00:00+03:00",
            advertising_fee_due_cny="10.00",
            advertising_fee_withheld_cny="0.00",
            advertising_fee_status="not_deducted",
        )
        service.withdrawal_record(
            store_alias="owner-store",
            record_key="post-cutover-withdrawal",
            amount_cny="200.00",
            withdrawn_at="2026-09-05T12:00:00+03:00",
        )
        rows = service.ledger.list_withdrawal_records(store_alias="owner-store")
        assert rows[0]["advertising_fee_debt_cny"] == "10.00"
        assert rows[1]["advertising_fee_status"] == "settlement_balance"
        assert rows[1]["advertising_fee_due_cny"] == "0.00"
        assert rows[1]["advertising_fee_withheld_cny"] == "0.00"
        assert rows[1]["advertising_fee_debt_cny"] == "0.00"

        with pytest.raises(FinanceServiceError, match="policy cutover"):
            service.withdrawal_record(
                store_alias="owner-store",
                record_key="invalid-current",
                amount_cny="100.00",
                withdrawn_at="2026-09-06T12:00:00+03:00",
                advertising_fee_status="unknown",
            )
        with pytest.raises(FinanceServiceError, match="pre-cutover"):
            service.withdrawal_record(
                store_alias="owner-store",
                record_key="invalid-legacy",
                amount_cny="100.00",
                withdrawn_at="2026-09-04T12:00:00+03:00",
                advertising_fee_status="settlement_balance",
            )
    finally:
        service.close()


def test_legacy_policy_disables_money_actions_and_withdrawal_debt_writes(
    tmp_path,
    monkeypatch,
):
    service = _service(
        tmp_path,
        monkeypatch,
        datetime(2026, 9, 6, 9, 30, tzinfo=SHANGHAI),
    )
    approved_at = service._now()
    plan = build_advertising_deposit_plan(
        report_id="report-placeholder",
        store="owner-store",
        campaign=1234567,
        amount_cny="10",
        source="balance",
        currency="CNY",
        source_balance_cny="100",
        source_balance_evidence_at=approved_at,
        policy_cap_cny="10",
        available_advertising_reserve_cny="10",
        approved_by="boss",
        approved_at=approved_at,
        expires_at=approved_at + timedelta(minutes=15),
    )
    plan_path = tmp_path / "legacy-policy-deposit-plan.json"
    plan_path.write_text(json.dumps(plan.as_dict()), encoding="utf-8")
    client = _FakeFundingClient()
    monkeypatch.setattr("wb_manager.finance_service.WbClient", lambda token: client)
    try:
        assert service.policy.settlement_balance_contract_enabled is False
        with pytest.raises(FinanceServiceError, match="not enabled"):
            service.ads_deposit(
                plan_path=plan_path,
                approved_by="boss",
                confirm_live=False,
            )
        assert client.calls == []

        with pytest.raises(FinanceServiceError, match="not enabled"):
            service.withdrawal_record(
                store_alias="owner-store",
                record_key="legacy-policy-post-cutover-unknown",
                amount_cny="100.00",
                withdrawn_at="2026-09-06T12:00:00+03:00",
            )
        assert service.ledger.list_withdrawal_records(store_alias="owner-store") == []
    finally:
        service.close()


@pytest.mark.parametrize(
    ("financial_timezone", "withdrawn_at", "expected_status"),
    [
        ("Asia/Tokyo", "2026-09-04T15:30:00+00:00", "settlement_balance"),
        ("America/New_York", "2026-09-05T03:30:00+00:00", "unknown"),
    ],
)
def test_withdrawal_cutover_uses_configured_financial_timezone_boundary(
    tmp_path,
    monkeypatch,
    financial_timezone,
    withdrawn_at,
    expected_status,
):
    monkeypatch.setenv("TEST_OWNER_TOKEN", "owner-token")
    monkeypatch.setenv("TEST_NEW_TOKEN", "new-token")
    registry_path = _registry(
        tmp_path / "stores.json",
        financial_timezone=financial_timezone,
    )
    monkeypatch.setattr(
        "wb_manager.finance_service.read_registry",
        lambda path: json.loads(Path(path).read_text(encoding="utf-8-sig")),
    )
    service = FinanceService(
        db_path=tmp_path / "finance.sqlite3",
        registry_path=registry_path,
        output_root=tmp_path / "reports",
        now=datetime(2026, 9, 6, 9, 30, tzinfo=SHANGHAI),
        source_factory=lambda client, alias: FakeSources(alias),
    )
    _enable_modern_settlement_policy(service)
    try:
        assert service.financial_timezone.key == financial_timezone
        service.withdrawal_record(
            store_alias="owner-store",
            record_key=f"timezone-boundary-{financial_timezone}",
            amount_cny="100.00",
            withdrawn_at=withdrawn_at,
        )
        row = service.ledger.list_withdrawal_records(store_alias="owner-store")[0]
        assert row["advertising_fee_status"] == expected_status
    finally:
        service.close()


def test_ads_deposit_rejects_non_settlement_balance_source(tmp_path, monkeypatch):
    service = _service(
        tmp_path,
        monkeypatch,
        datetime(2026, 9, 6, 9, 30, tzinfo=SHANGHAI),
    )
    _enable_modern_settlement_policy(service)
    approved_at = service._now()
    plan = build_advertising_deposit_plan(
        report_id="report-placeholder",
        store="owner-store",
        campaign=1234567,
        amount_cny="10",
        source="account",
        currency="CNY",
        source_balance_cny="100",
        source_balance_evidence_at=approved_at,
        policy_cap_cny="10",
        available_advertising_reserve_cny="10",
        approved_by="boss",
        approved_at=approved_at,
        expires_at=approved_at + timedelta(minutes=15),
    )
    plan_path = tmp_path / "account-source-plan.json"
    plan_path.write_text(json.dumps(plan.as_dict()), encoding="utf-8")
    try:
        with pytest.raises(FinanceServiceError, match="balance/net/type=1"):
            service.ads_deposit(
                plan_path=plan_path,
                approved_by="boss",
                confirm_live=False,
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
    assert len(accruals) == 2  # the monthly copy and zero-value returned order are excluded
    historical = next(item for item in accruals if item.accrued_at.date().isoformat() == "2026-08-20")
    eligible = next(item for item in accruals if item.accrued_at.date().isoformat() == "2026-08-29")
    assert historical.amount_cny == eligible.amount_cny
    assert historical.allocated_cny == 0
    assert eligible.allocated_cny == 10

    with pytest.raises(FinanceServiceError, match="superseded"):
        service.funds_plan(report_id=original["reportId"])

    plan = service.funds_plan(report_id=latest_id)
    owner = next(item for item in plan["stores"] if item["store"] == "owner-store")
    assert owner["components"]["postActivationUnallocatedAdvertisingAccrualCny"] == "20.00"
    assert owner["components"]["historicalAdvertisingAccrualExcludedCny"] == "30.00"
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
    _enable_modern_settlement_policy(service)
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


@pytest.mark.parametrize("lifecycle_status", ["executing", "unknown"])
def test_ads_deposit_existing_execution_state_allows_readback_after_policy_fallback(
    tmp_path,
    monkeypatch,
    lifecycle_status,
):
    service, weekly = _finalized_weekly_service(tmp_path, monkeypatch)
    plan_path = _advertising_deposit_plan_file(service, tmp_path, weekly["reportId"])
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
        lifecycle_status,
        occurred_at=service._now(),
        evidence={"livePreflight": {"campaignBudgetBeforeCny": "40.00"}},
    )
    service.policy = replace(
        service.policy,
        settlement_balance_contract_enabled=False,
    )
    client = _FakeFundingClient()
    client.budget_reads = 1
    monkeypatch.setattr("wb_manager.finance_service.WbClient", lambda token: client)

    result = service.ads_deposit(
        plan_path=plan_path,
        approved_by="boss",
        confirm_live=True,
    )

    assert result["status"] == "readback_confirmed"
    assert result["readbackOnly"] is True
    assert [call[0] for call in client.calls] == ["GET"]
    assert "/adv/v1/budget?id=" in client.calls[0][1]
    assert not any(call[0] == "POST" for call in client.calls)
    service.close()


def test_ads_deposit_proposed_action_cannot_post_after_policy_fallback(
    tmp_path,
    monkeypatch,
):
    service, weekly = _finalized_weekly_service(tmp_path, monkeypatch)
    plan_path = _advertising_deposit_plan_file(service, tmp_path, weekly["reportId"])
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
    service.policy = replace(
        service.policy,
        settlement_balance_contract_enabled=False,
    )
    client = _FakeFundingClient()
    monkeypatch.setattr("wb_manager.finance_service.WbClient", lambda token: client)

    with pytest.raises(FinanceServiceError, match="not enabled"):
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
