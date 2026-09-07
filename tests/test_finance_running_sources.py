from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from types import SimpleNamespace

from wb_manager.finance_running_sources import extract_running_account_sources


class _PolicyTimeline:
    def select(self, created_at):
        assert created_at == "2026-08-01T09:00:00Z"
        return SimpleNamespace(reporting_policy_id="historical-policy", reserve_allocation_mode="LEGACY_SHARED_POOL",
                               managed_operations_rate=Decimal("0.02"), promotion_reserve_rate=Decimal("0.03"),
                               return_risk_reserve_rate=Decimal("0"), total_reserve_rate=Decimal("0.03"))


def _db(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE store_registry(store_alias TEXT, enabled INTEGER, opened_at TEXT, currency TEXT);
        INSERT INTO store_registry VALUES('shop',1,'2026-07-01','CNY');
        CREATE TABLE raw_finance(store_alias TEXT,rrd_id TEXT,order_id TEXT,srid TEXT,occurred_at TEXT,
            payload_json TEXT,fetched_at TEXT,logistics_cny TEXT,return_logistics_cny TEXT,other_cost_cny TEXT);
        CREATE TABLE cost_versions(cost_version_uid TEXT,purchase_cost_cny TEXT,approved_by TEXT,effective_from TEXT,effective_to TEXT);
        INSERT INTO cost_versions VALUES('cost-1','60','owner','2026-07-01',NULL);
        CREATE TABLE order_profit(store_alias TEXT,calculated_at TEXT,profit_version TEXT,evidence_json TEXT);
        CREATE TABLE raw_sales(store_alias TEXT,event_hash TEXT,occurred_at TEXT,payload_json TEXT,fetched_at TEXT,buyer_paid_cny TEXT);
        CREATE TABLE withdrawal_records(record_id TEXT,store_alias TEXT,withdrawn_at TEXT,withdrawal_amount_cny TEXT,status TEXT,settled_at TEXT,external_reference TEXT);
        CREATE TABLE withdrawal_record_events(record_id TEXT,event_id TEXT,status TEXT,occurred_at TEXT);
    """)
    raw = {"currency": "CNY", "retailPriceWithDisc": "100", "forPay": "85", "acquiringFee": "0",
           "rrDate": "2026-08-30", "createDate": "2026-09-07", "docTypeName": "销售", "quantity": 1,
           "orderDt": "2026-08-01T09:00:00Z", "vendorCode": "PHONE"}
    c.execute("INSERT INTO raw_finance VALUES(?,?,?,?,?,?,?,?,?,?)",
              ("shop", "1", "order-1", "srid-1", "2026-08-29T21:00:00Z", json.dumps(raw), "2026-09-07", "0", "0", "0"))
    snapshot = {"salesAmountGCny": "100", "forPayCny": "85", "costVersionId": "cost-1"}
    evidence = {"evidenceIds": ["finance:rrdId:1"], "fullOrderSnapshot": snapshot}
    for version in ("old", "new"):
        c.execute("INSERT INTO order_profit VALUES(?,?,?,?)", ("shop", "2026-09-07", version, json.dumps(evidence)))
    c.commit()
    c.close()
    return path


def _extract(path, cutoff="2026-09-06", **kwargs):
    return extract_running_account_sources(path, cutoff, policy_timeline=_PolicyTimeline(), **kwargs)


def test_original_settlement_once_with_locked_cost_and_policy(tmp_path):
    result = _extract(_db(tmp_path))
    assert len(result["settlementSales"]) == 1
    sale = result["settlementSales"][0]
    assert sale["purchaseCostCny"] == "60.00"
    assert sale["managedOperationsCny"] == "2.00"
    assert sale["promotionReserveCny"] == "3.00"
    assert sale["policyId"] == "historical-policy"
    assert sale["eventId"] == "finance:shop:rrd:1:sale"


def test_financial_business_date_not_report_create_date(tmp_path):
    path = _db(tmp_path)
    assert len(_extract(path, "2026-08-30")["settlementSales"]) == 1
    assert _extract(path, "2026-08-29")["settlementSales"] == []


def test_rub_and_legacy_mislabelled_stats_not_cny(tmp_path):
    path = _db(tmp_path)
    with sqlite3.connect(path) as c:
        raw = {"saleID": "S123", "date": "2026-09-01", "priceWithDisc": 9000, "forPay": 8000}
        c.execute("INSERT INTO raw_sales VALUES(?,?,?,?,?,?)", ("shop", "stats-1", "2026-09-01", json.dumps(raw), "2026-09-07", "9000"))
        raw_finance = json.loads(c.execute("SELECT payload_json FROM raw_finance").fetchone()[0])
        raw_finance["currency"] = "RUB"
        c.execute("UPDATE raw_finance SET payload_json=?", (json.dumps(raw_finance),))
    result = _extract(path)
    assert result["settlementSales"] == []
    assert result["rawStatistics"][0]["currency"] == "RUB"
    assert result["rawStatistics"][0]["raw"]["priceWithDisc"] == 9000
    assert "buyer_paid_cny" not in result["rawStatistics"][0]
    assert result["issues"][0]["reason"] == "non_CNY_or_missing_currency_not_mapped_to_CNY"
    assert result["issues"][0]["occurredAt"] == "2026-08-30T00:00:00+03:00"


def test_latest_withdrawal_status_and_cross_source_dedup(tmp_path):
    path = _db(tmp_path)
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO withdrawal_records VALUES('wrong','shop','2026-08-31','50','reported',NULL,'typo')")
        c.execute("INSERT INTO withdrawal_records VALUES('right','shop','2026-08-31','50','reported',NULL,'correct')")
        c.execute("INSERT INTO withdrawal_record_events VALUES('wrong','status-1','reported','2026-08-31')")
        c.execute("INSERT INTO withdrawal_record_events VALUES('wrong','status-2','voided','2026-09-02')")
    cash = tmp_path / "cash.json"
    cash.write_text(json.dumps({"events": [{"eventType": "withdrawal", "eventId": "historical-right", "store": "shop",
                                           "requestedAt": "2026-08-31", "status": "reported", "externalReference": "correct", "amountCny": 50}]}))
    result = _extract(path, historical_cash_path=cash)
    assert len(result["withdrawals"]) == 1
    assert result["withdrawals"][0]["recordId"] == "right"
    assert result["withdrawals"][0]["settledByCutoff"] is False
    assert result["excludedWithdrawals"][0]["recordId"] == "wrong"
    cancelled = result["excludedWithdrawals"][0]
    assert cancelled["eventId"] == "withdrawal:shop:typo"
    assert cancelled["requestedAt"] == "2026-08-31"
    assert cancelled["cancelledAt"] == "2026-09-02"
    assert cancelled["amountCny"] == "50.00"
    assert cancelled["latestStatus"] == "voided"


def test_voided_cash_registry_does_not_resurrect_cancelled_request(tmp_path):
    path = _db(tmp_path)
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO withdrawal_records VALUES('wrong','shop','2026-08-31','50','reported',NULL,'typo')")
        c.execute("INSERT INTO withdrawal_record_events VALUES('wrong','status-2','voided','2026-09-02')")
    cash = tmp_path / "cash.json"
    cash.write_text(json.dumps({"events": [
        {"eventType": "withdrawal", "eventId": "historical-wrong", "store": "shop", "requestedAt": "2026-08-31", "status": "reported", "externalReference": "typo", "amountCny": 50},
        {"eventType": "withdrawal", "eventId": "historical-void", "store": "shop", "requestedAt": "2026-08-31", "status": "voided", "externalReference": "other", "amountCny": 40},
    ]}))
    result = _extract(path, historical_cash_path=cash)
    assert result["withdrawals"] == []
    assert len(result["excludedWithdrawals"]) == 1


def test_ocr_identity_correction_restates_original_day_before_discovery(tmp_path):
    path = _db(tmp_path)
    with sqlite3.connect(path) as c:
        c.execute("ALTER TABLE withdrawal_record_events ADD COLUMN evidence_json TEXT")
        c.execute("INSERT INTO withdrawal_records VALUES('wrong','shop','2026-08-31','50','reported',NULL,'typo')")
        c.execute("INSERT INTO withdrawal_records VALUES('right','shop','2026-08-31','50','reported',NULL,'correct')")
        c.execute("INSERT INTO withdrawal_record_events VALUES('wrong','status-1','reported','2026-08-31','{}')")
        correction = {"incorrectExternalReference": "typo", "correctExternalReference": "correct", "reason": "portal ID OCR correction"}
        c.execute("INSERT INTO withdrawal_record_events VALUES(?,?,?,?,?)", ("wrong", "status-2", "voided", "2026-09-02", json.dumps(correction)))
    august = _extract(path, "2026-08-31")
    september = _extract(path, "2026-09-06")
    for result in (august, september):
        assert len(result["withdrawals"]) == 1
        assert result["withdrawals"][0]["externalReference"] == "correct"
        correction = result["excludedWithdrawals"][0]
        assert correction["cancelledAt"] == "2026-08-31"
        assert correction["discoveredAt"] == "2026-09-02"
        assert correction["correctionType"] == "historical_identity_correction"
        assert correction["correctedExternalReference"] == "correct"


def test_true_cancellation_does_not_restate_earlier_period(tmp_path):
    path = _db(tmp_path)
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO withdrawal_records VALUES('w','shop','2026-08-31','50','reported',NULL,'ref')")
        c.execute("INSERT INTO withdrawal_record_events VALUES('w','status-1','reported','2026-08-31')")
        c.execute("INSERT INTO withdrawal_record_events VALUES('w','status-2','voided','2026-09-02')")
    assert len(_extract(path, "2026-08-31")["withdrawals"]) == 1
    september = _extract(path, "2026-09-06")
    assert september["withdrawals"] == []
    assert september["excludedWithdrawals"][0]["cancelledAt"] == "2026-09-02"
    assert september["excludedWithdrawals"][0]["correctionType"] == "cancellation"


def test_balances_have_raw_currency_evidence_and_never_relabel_rub(tmp_path):
    path = _db(tmp_path)
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE raw_balance(store_alias TEXT,event_hash TEXT,observed_at TEXT,payload_json TEXT,account_balance_cny TEXT,withdrawable_balance_cny TEXT,ad_balance_cny TEXT)")
        cny = {"source": "advertising_balance", "currency": "CNY", "net": 300, "balance": 0}
        rub = {**cny, "currency": "RUB", "net": 5000}
        c.execute("INSERT INTO raw_balance VALUES(?,?,?,?,?,?,?)", ("shop", "balance-1", "2026-09-01", json.dumps(cny), None, None, "0"))
        c.execute("INSERT INTO raw_balance VALUES(?,?,?,?,?,?,?)", ("shop", "balance-2", "2026-09-02", json.dumps(rub), None, None, "0"))
    balances = _extract(path)["balanceSnapshots"]
    assert len(balances) == 1
    assert balances[0]["mutualSettlementNetCny"] == "300.00"
    assert balances[0]["currency"] == "CNY"
    assert balances[0]["sourceCurrencyField"] == "raw_balance.payload_json.currency"


def test_row_specific_missing_policy_issue_has_business_date(tmp_path):
    path = _db(tmp_path)
    with sqlite3.connect(path) as c:
        c.execute("DELETE FROM order_profit")
    issue = _extract(path)["issues"][0]
    assert issue["reason"] == "historical_cost_policy_snapshot_unavailable_or_conflicting"
    assert issue["occurredAt"] == "2026-08-30T00:00:00+03:00"


def test_withdrawal_request_date_not_settlement_date(tmp_path):
    path = _db(tmp_path)
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO withdrawal_records VALUES('w','shop','2026-08-17','50','readback_confirmed','2026-08-28','ref')")
    before = _extract(path, "2026-08-20")["withdrawals"][0]
    after = _extract(path, "2026-08-30")["withdrawals"][0]
    assert before["settledByCutoff"] is False
    assert after["settledByCutoff"] is True


def test_read_only_and_unknown_agency_payments(tmp_path):
    path = _db(tmp_path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = _extract(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert result["managedOperationsPaidCny"] is None
    assert result["managedOperationsPayments"] == []


def test_independent_charge_keeps_stable_identity(tmp_path):
    path = _db(tmp_path)
    with sqlite3.connect(path) as c:
        raw = {"currency": "CNY", "rrDate": "2026-09-01", "sellerOperName": "物流", "bonusTypeName": "取消时给客户"}
        c.execute("INSERT INTO raw_finance VALUES(?,?,?,?,?,?,?,?,?,?)", ("shop", "2", "order-2", "srid-2", "2026-09-01", json.dumps(raw), "2026-09-07", "0", "12", "0"))
    result = _extract(path)
    assert len(result["settlementSales"]) == 1
    assert result["settlementCharges"][0]["amountCny"] == "12.00"
    assert result["settlementCharges"][0]["eventId"] == "finance:shop:rrd:2:return_logistics"
