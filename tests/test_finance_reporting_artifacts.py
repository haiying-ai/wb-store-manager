from __future__ import annotations

import json
from pathlib import Path

from wb_manager.finance_reporting import build_owner_markdown, write_report_artifacts


def sample_payload() -> dict:
    return {
        "reportId": "weekly-2026-08-17_2026-08-23-v1",
        "status": "FINAL",
        "kind": "weekly",
        "generatedAt": "2026-08-28T09:30:00+08:00",
        "period": {"from": "2026-08-17", "to": "2026-08-23", "timezone": "Europe/Moscow"},
        "policy": {
            "reportingPolicyId": "phone-two-store-order-profit-20260828-v4",
            "pricingPolicyId": "phone-hk-fbs-20260828-v5",
            "managedOperationsRate": "0.02",
            "combinedPoolRate": "0.03",
            "paymentFallbackRate": "0.008",
            "fixedOperationsFallbackCny": "12",
        },
        "orders": [
            {
                "storeAlias": "owner-store",
                "orderKey": "srid:sale-1",
                "orderCreatedAt": "2026-08-18T10:00:00+03:00",
                "completedAt": "2026-08-20T10:00:00+03:00",
                "sku": "IPHONE 13-128GB-BLACK",
                "outcome": "SOLD",
                "netQuantity": "1",
                "netTurnoverCny": "1000",
                "purchaseCostCny": "600",
                "wbCommissionCny": "150",
                "paymentFeeCny": "8",
                "forwardLogisticsCny": "8",
                "storageAndOperationsCny": "0",
                "otherCostCny": "0",
                "returnLogisticsCny": "0",
                "actualAdvertisingAllocatedCny": "25",
                "ownerProfitCny": "184",
                "costVersionId": "cv-1",
                "evidenceStatus": "COMPLETE",
            },
            {
                "storeAlias": "new-store",
                "orderKey": "order:returned-1",
                "orderCreatedAt": "2026-08-19T10:00:00+03:00",
                "completedAt": "2026-08-22T10:00:00+03:00",
                "sku": "IPHONE 13-128GB-WHITE",
                "outcome": "RETURNED",
                "netQuantity": "0",
                "netTurnoverCny": "0",
                "purchaseCostCny": "0",
                "wbCommissionCny": "0",
                "paymentFeeCny": "0",
                "forwardLogisticsCny": "0",
                "storageAndOperationsCny": "0",
                "otherCostCny": "0",
                "returnLogisticsCny": "16",
                "actualAdvertisingAllocatedCny": "0",
                "ownerProfitCny": "-16",
                "costVersionId": "cv-2",
                "evidenceStatus": "COMPLETE",
            },
        ],
        "stores": [
            {
                "storeAlias": "owner-store",
                "orderCount": 1,
                "netTurnoverCny": "1000",
                "purchaseCostCny": "600",
                "managedOperationsCny": "20",
                "returnLogisticsCny": "0",
                "advertisingProvisionCny": "30",
                "actualAdvertisingSpendCny": "25",
                "ownerProfitCny": "184",
            },
            {
                "storeAlias": "new-store",
                "orderCount": 1,
                "netTurnoverCny": "0",
                "purchaseCostCny": "0",
                "managedOperationsCny": "0",
                "returnLogisticsCny": "16",
                "advertisingProvisionCny": "0",
                "actualAdvertisingSpendCny": "0",
                "ownerProfitCny": "-16",
            },
        ],
        "totals": {
            "netTurnoverCny": "1000",
            "purchaseCostCny": "600",
            "platformAndOperationsCostCny": "166",
            "managedOperationsCny": "20",
            "returnLogisticsCny": "16",
            "advertisingProvisionCny": "30",
            "advertisingSettlementDeductionCny": "30",
            "ownerProfitCny": "168",
        },
        "costVersions": [
            {
                "costVersionId": "cv-1",
                "productKey": "IPHONE13-128GB-BLACK",
                "costRub": "6900",
                "exchangeRateRubPerCny": "11.5",
                "costCny": "600",
                "effectiveFrom": "2026-07-04T00:00:00+03:00",
                "effectiveTo": None,
                "sourceQuote": "7月4日报价单.xlsx",
                "sourceRow": 3,
            }
        ],
        "funding": [
            {
                "storeAlias": "owner-store",
                "withdrawableBalanceCny": "500",
                "unallocatedAdvertisingReserveCny": "30",
                "approvedPendingDepositCny": "0",
                "otherLockedFundsCny": "0",
                "status": "PLAN_ONLY",
                "note": "提现需人工操作",
            }
        ],
        "blockers": [],
    }


def test_markdown_uses_actual_return_language():
    text = build_owner_markdown(sample_payload())
    assert "实际退货物流费" in text
    assert "不使用 8/16 CNY 默认值" in text
    assert "老板实际利润" in text
    assert "WB 结算周期广告扣款（仅对账）：30.00 CNY" in text
    assert "balance/net/type=1" in text
    assert "settlement_balance" in text


def test_writes_verified_immutable_artifacts(tmp_path: Path):
    artifacts = write_report_artifacts(sample_payload(), tmp_path)
    assert artifacts.workbook_path.exists()
    assert artifacts.markdown_path.exists()
    assert artifacts.json_path.exists()
    verification = json.loads(artifacts.verification_path.read_text(encoding="utf-8"))
    assert verification["status"] == "PASS"
    assert len(verification["renderedSheets"]) == 6
    for preview in verification["renderedSheets"]:
        assert Path(preview).exists()
    try:
        write_report_artifacts(sample_payload(), tmp_path)
    except FileExistsError:
        pass
    else:
        raise AssertionError("report artifacts must be immutable")
