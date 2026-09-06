from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import re
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

import wb_manager.finance_reporting as finance_reporting
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
            "reserveAllocationMode": "LEGACY_SHARED_POOL",
            "managedOperationsRate": "0.02",
            "returnRiskReserveRate": "0",
            "promotionReserveRate": "0.03",
            "totalReserveRate": "0.03",
            "combinedPoolRate": "0.03",
            "reportingPolicyIdsUsed": ["phone-two-store-order-profit-20260828-v4"],
            "reportingPolicyVersions": [
                {
                    "reportingPolicyId": "phone-two-store-order-profit-20260828-v4",
                    "pricingPolicyId": "phone-hk-fbs-20260828-v5",
                    "reserveAllocationMode": "LEGACY_SHARED_POOL",
                    "managedOperationsRate": "0.02",
                    "returnRiskReserveRate": "0",
                    "promotionReserveRate": "0.03",
                    "totalReserveRate": "0.03",
                    "effectiveFrom": "2026-08-17T00:00:00+03:00",
                    "effectiveTo": None,
                }
            ],
            "paymentFallbackRate": None,
            "fixedOperationsFallbackCny": None,
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
                "managedOperationsCny": "20",
                "returnRiskReserveCny": "0",
                "promotionReserveCny": "30",
                "totalPolicyReserveCny": "30",
                "advertisingProvisionCny": "30",
                "excessReturnLossCny": "0",
                "strictOwnerProfitCny": "184",
                "ownerProfitCny": "184",
                "financialReportingPolicyVersionId": "phone-two-store-order-profit-20260828-v4",
                "sellingPricePolicyVersionId": "phone-hk-fbs-20260828-v5",
                "reserveAllocationMode": "LEGACY_SHARED_POOL",
                "managedOperationsRate": "0.02",
                "returnRiskReserveRate": "0",
                "promotionReserveRate": "0.03",
                "totalReserveRate": "0.03",
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
                "managedOperationsCny": "0",
                "returnRiskReserveCny": "0",
                "promotionReserveCny": "0",
                "totalPolicyReserveCny": "0",
                "advertisingProvisionCny": "0",
                "excessReturnLossCny": "16",
                "strictOwnerProfitCny": "-16",
                "ownerProfitCny": "-16",
                "financialReportingPolicyVersionId": "phone-two-store-order-profit-20260828-v4",
                "sellingPricePolicyVersionId": "phone-hk-fbs-20260828-v5",
                "reserveAllocationMode": "LEGACY_SHARED_POOL",
                "managedOperationsRate": "0.02",
                "returnRiskReserveRate": "0",
                "promotionReserveRate": "0.03",
                "totalReserveRate": "0.03",
                "costVersionId": "cv-2",
                "evidenceStatus": "COMPLETE",
            },
        ],
        "stores": [
            {
                "storeAlias": "owner-store",
                "orderCount": 1,
                "confirmedOrderCount": 1,
                "openItemOrderCount": 0,
                "salesAmountGCny": "1000",
                "netTurnoverCny": "1000",
                "actualCommissionCCny": "150",
                "acquiringFeeCny": "8",
                "forPayCny": "842",
                "accountIncrementCny": "842",
                "purchaseCostCny": "600",
                "managedOperationsCny": "20",
                "combinedAdvertisingAndReturnPoolCny": "30",
                "returnRiskReserveCny": "0",
                "promotionReserveCny": "30",
                "totalPolicyReserveCny": "30",
                "returnLogisticsCny": "0",
                "advertisingProvisionCny": "30",
                "excessReturnLossCny": "0",
                "ruleWithdrawableCny": "812",
                "actualAdvertisingSpendCny": "25",
                "strictOwnerProfitCny": "184",
                "ownerProfitCny": "184",
            },
            {
                "storeAlias": "new-store",
                "orderCount": 1,
                "confirmedOrderCount": 1,
                "openItemOrderCount": 0,
                "salesAmountGCny": "0",
                "netTurnoverCny": "0",
                "actualCommissionCCny": "0",
                "acquiringFeeCny": "0",
                "forPayCny": "0",
                "accountIncrementCny": "0",
                "purchaseCostCny": "0",
                "managedOperationsCny": "0",
                "combinedAdvertisingAndReturnPoolCny": "0",
                "returnRiskReserveCny": "0",
                "promotionReserveCny": "0",
                "totalPolicyReserveCny": "0",
                "returnLogisticsCny": "16",
                "advertisingProvisionCny": "0",
                "excessReturnLossCny": "16",
                "ruleWithdrawableCny": "0",
                "actualAdvertisingSpendCny": "0",
                "strictOwnerProfitCny": "-16",
                "ownerProfitCny": "-16",
            },
        ],
        "totals": {
            "confirmedOrderCount": 2,
            "openItemOrderCount": 0,
            "salesAmountGCny": "1000",
            "netTurnoverCny": "1000",
            "actualCommissionCCny": "150",
            "acquiringFeeCny": "8",
            "forPayCny": "842",
            "accountIncrementCny": "842",
            "purchaseCostCny": "600",
            "platformAndOperationsCostCny": "166",
            "managedOperationsCny": "20",
            "combinedAdvertisingAndReturnPoolCny": "30",
            "returnRiskReserveCny": "0",
            "promotionReserveCny": "30",
            "totalPolicyReserveCny": "30",
            "returnLogisticsCny": "16",
            "advertisingProvisionCny": "30",
            "excessReturnLossCny": "16",
            "ruleWithdrawableCny": "812",
            "strictOwnerProfitCny": "168",
            "ownerProfitCny": "168",
        },
        "costVersions": [
            {
                "costVersionId": "cv-1",
                "productKey": "IPHONE13-128GB-BLACK",
                "sourceCurrency": "CNY",
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


def install_fake_builder(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mutate_verification=None,
    barrier: Barrier | None = None,
    calls: list[dict] | None = None,
) -> None:
    def fake_run(command, **_kwargs):
        def argument_path(flag: str) -> Path:
            return Path(command[command.index(flag) + 1])

        input_path = argument_path("--input")
        workbook_path = argument_path("--output")
        preview_dir = argument_path("--preview-dir")
        verification_path = argument_path("--verification")
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        marker = str(payload.get("testMarker") or "default")

        workbook_path.write_bytes(f"workbook:{marker}".encode())
        preview_dir.mkdir(parents=True, exist_ok=False)
        rendered = []
        for index in range(7):
            preview = preview_dir / f"sheet-{index + 1}.png"
            preview.write_bytes(f"{marker}:{index + 1}".encode())
            rendered.append(str(preview))
        verification = {
            "status": "PASS",
            "formulaErrorCount": 0,
            "renderedSheets": rendered,
            "reportId": payload["reportId"],
            "workbookStatus": (
                "PENDING_DATA"
                if payload.get("status") == "PENDING_DATA"
                else "FINAL"
            ),
            "checkFailures": [],
            "consistencyFailures": [],
            "testMarker": marker,
        }
        if mutate_verification is not None:
            mutate_verification(verification)
        verification_path.write_text(
            json.dumps(verification, ensure_ascii=False), encoding="utf-8"
        )
        if calls is not None:
            calls.append(
                {
                    "input": input_path,
                    "workbook": workbook_path,
                    "previews": preview_dir,
                    "verification": verification_path,
                    "formalJsonExisted": (
                        input_path.parents[2] / f"{payload['reportId']}.json"
                    ).exists(),
                }
            )
        if barrier is not None:
            barrier.wait(timeout=10)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(finance_reporting.shutil, "which", lambda _name: "node")
    monkeypatch.setattr(finance_reporting.subprocess, "run", fake_run)


def test_markdown_uses_actual_return_language():
    text = build_owner_markdown(sample_payload())
    assert "实际退货物流费" in text
    assert "不使用 8/16 CNY 默认值" in text
    assert "已确认严格利润" in text
    assert "平台 forPay／账户增量：842.00 / 842.00 CNY" in text


def test_markdown_renders_missing_confirmed_amount_as_pending_not_zero():
    payload = sample_payload()
    payload["status"] = "PENDING_DATA"
    payload["totals"]["confirmedOrderCount"] = 0
    payload["totals"]["openItemOrderCount"] = 1
    payload["totals"]["salesAmountGCny"] = None
    payload["totals"]["strictOwnerProfitCny"] = None

    text = build_owner_markdown(payload)

    assert "订单时点营业额 G：待定 CNY" in text
    assert "已确认严格利润：**待定 CNY**" in text


def test_writes_verified_immutable_artifacts(tmp_path: Path):
    artifacts = write_report_artifacts(sample_payload(), tmp_path)
    assert artifacts.workbook_path.exists()
    assert artifacts.markdown_path.exists()
    assert artifacts.json_path.exists()
    verification = json.loads(artifacts.verification_path.read_text(encoding="utf-8"))
    assert verification["status"] == "PASS"
    assert verification["workbookStatus"] == "FINAL"
    assert verification["checkFailures"] == []
    assert verification["consistencyFailures"] == []
    assert len(verification["renderedSheets"]) == 7
    for preview in verification["renderedSheets"]:
        assert Path(preview).exists()
    try:
        write_report_artifacts(sample_payload(), tmp_path)
    except FileExistsError:
        pass
    else:
        raise AssertionError("report artifacts must be immutable")


def test_workbook_uses_store_period_pool_and_profit_values(tmp_path: Path):
    payload = sample_payload()
    # Deliberately make the report-level values differ from the order detail.
    # Both levels must display the finance engine values without rebuilding
    # policy amounts from one global workbook rate.
    payload["stores"][0]["advertisingProvisionCny"] = "14"
    payload["stores"][0]["strictOwnerProfitCny"] = "172"
    payload["stores"][0]["ownerProfitCny"] = "172"
    payload["totals"]["advertisingProvisionCny"] = "14"
    payload["totals"]["strictOwnerProfitCny"] = "156"
    payload["totals"]["ownerProfitCny"] = "156"
    artifacts = write_report_artifacts(payload, tmp_path)

    # The builder emits sheets in a stable order (店铺汇总 is sheet2 and
    # 订单利润 is sheet3).  Read only the relevant XML cells so this test
    # stays dependency-free.
    with ZipFile(artifacts.workbook_path) as workbook:
        stores_xml = workbook.read("xl/worksheets/sheet2.xml").decode("utf-8")
        orders_xml = workbook.read("xl/worksheets/sheet3.xml").decode("utf-8")
    assert re.search(r'<x:c r="L4"[^>]*>\s*<x:v>14(?:\.0+)?</x:v>', stores_xml)
    assert re.search(r'<x:c r="N4"[^>]*>\s*<x:v>172(?:\.0+)?</x:v>', stores_xml)
    assert not re.search(r'<x:c r="L4"[^>]*>\s*<x:f>', stores_xml)
    assert not re.search(r'<x:c r="N4"[^>]*>\s*<x:f>', stores_xml)
    assert re.search(r'<x:c r="R4"[^>]*>\s*<x:v>30(?:\.0+)?</x:v>', orders_xml)
    assert not re.search(r'<x:c r="R4"[^>]*>\s*<x:f>', orders_xml)
    assert "成本版本与口径&apos;!$B$" not in orders_xml


def test_workbook_preserves_mixed_order_time_policy_snapshots(tmp_path: Path):
    payload = sample_payload()
    boundary = "2026-09-06T13:08:38+03:00"
    old_version = payload["policy"]["reportingPolicyVersions"][0]
    old_version["effectiveTo"] = boundary
    new_version = {
        "reportingPolicyId": "phone-two-store-order-profit-20260906-v7",
        "pricingPolicyId": "phone-hk-fbs-20260906-v7",
        "reserveAllocationMode": "SEPARATE_RETURN_AND_PROMOTION",
        "managedOperationsRate": "0.02",
        "returnRiskReserveRate": "0.02",
        "promotionReserveRate": "0.03",
        "totalReserveRate": "0.05",
        "effectiveFrom": boundary,
        "effectiveTo": None,
    }
    payload["policy"].update(new_version)
    payload["policy"]["reportingPolicyIdsUsed"] = [
        old_version["reportingPolicyId"],
        new_version["reportingPolicyId"],
    ]
    payload["policy"]["reportingPolicyVersions"].append(new_version)
    payload["orders"].append(
        {
            **deepcopy(payload["orders"][0]),
            "orderKey": "srid:sale-v7",
            "orderCreatedAt": "2026-09-06T14:00:00+03:00",
            "completedAt": "2026-09-06T15:00:00+03:00",
            "financialReportingPolicyVersionId": new_version["reportingPolicyId"],
            "sellingPricePolicyVersionId": new_version["pricingPolicyId"],
            "reserveAllocationMode": new_version["reserveAllocationMode"],
            "returnRiskReserveRate": "0.02",
            "promotionReserveRate": "0.03",
            "totalReserveRate": "0.05",
            "returnRiskReserveCny": "20",
            "promotionReserveCny": "30",
            "totalPolicyReserveCny": "50",
            "actualAdvertisingAllocatedCny": "0",
        }
    )
    owner = payload["stores"][0]
    owner.update(
        orderCount=2,
        confirmedOrderCount=2,
        salesAmountGCny="2000",
        netTurnoverCny="2000",
        purchaseCostCny="1200",
        actualCommissionCCny="300",
        acquiringFeeCny="16",
        managedOperationsCny="40",
        returnRiskReserveCny="20",
        promotionReserveCny="60",
        totalPolicyReserveCny="80",
        combinedAdvertisingAndReturnPoolCny="80",
        advertisingProvisionCny="60",
        strictOwnerProfitCny="368",
        ownerProfitCny="368",
    )
    payload["totals"].update(
        confirmedOrderCount=3,
        salesAmountGCny="2000",
        netTurnoverCny="2000",
        purchaseCostCny="1200",
        platformAndOperationsCostCny="332",
        managedOperationsCny="40",
        returnRiskReserveCny="20",
        promotionReserveCny="60",
        totalPolicyReserveCny="80",
        combinedAdvertisingAndReturnPoolCny="80",
        advertisingProvisionCny="60",
        excessReturnLossCny="16",
        strictOwnerProfitCny="352",
        ownerProfitCny="352",
    )

    artifacts = write_report_artifacts(payload, tmp_path)
    with ZipFile(artifacts.workbook_path) as workbook:
        orders_xml = workbook.read("xl/worksheets/sheet3.xml").decode("utf-8")
        policy_xml = workbook.read("xl/worksheets/sheet5.xml").decode("utf-8")

    assert "$B$6" not in orders_xml and "$B$7" not in orders_xml
    for cell, value in (("Q4", 30), ("AF4", 0), ("AG4", 30), ("Q6", 50), ("AF6", 20), ("AG6", 30)):
        assert re.search(fr'<x:c r="{cell}"[^>]*>\s*<x:v>{value}(?:\.0+)?</x:v>', orders_xml)
        assert not re.search(fr'<x:c r="{cell}"[^>]*>\s*<x:f>', orders_xml)
    assert old_version["reportingPolicyId"] in policy_xml
    assert new_version["reportingPolicyId"] in policy_xml
    assert "2026-09-06 13:08:38 (莫斯科)" in policy_xml
    assert re.search(r'<x:c r="D21"[^>]*>\s*<x:v>600(?:\.0+)?</x:v>', policy_xml)
    assert re.search(r'<x:c r="E21"[^>]*>\s*<x:v>6900(?:\.0+)?</x:v>', policy_xml)


def test_workbook_excludes_blocked_order_amounts_from_confirmed_summary(tmp_path: Path):
    payload = sample_payload()
    payload["status"] = "PENDING_DATA"
    payload["totals"]["openItemOrderCount"] = 1
    payload["stores"][0]["orderCount"] = 2
    payload["stores"][0]["openItemOrderCount"] = 1
    payload["orders"].append(
        {
            "storeAlias": "owner-store",
            "orderKey": "srid:blocked-partial",
            "orderCreatedAt": "2026-08-21T10:00:00+03:00",
            "sku": "IPHONE 13-128GB-BLACK",
            "outcome": "OPEN",
            "netQuantity": "1",
            "netTurnoverCny": "500",
            "purchaseCostCny": "300",
            "wbCommissionCny": "75",
            "paymentFeeCny": "0",
            "forwardLogisticsCny": "8",
            "storageAndOperationsCny": "0",
            "otherCostCny": "0",
            "returnLogisticsCny": "0",
            "actualAdvertisingAllocatedCny": "0",
            # A blocked row can carry a provisional program amount for audit,
            # but the workbook must not let it enter any confirmed formula.
            "ownerProfitCny": "77",
            "costVersionId": "cv-1",
            "evidenceStatus": "BLOCKED",
        }
    )
    artifacts = write_report_artifacts(payload, tmp_path)

    with ZipFile(artifacts.workbook_path) as workbook:
        stores_xml = workbook.read("xl/worksheets/sheet2.xml").decode("utf-8")
        orders_xml = workbook.read("xl/worksheets/sheet3.xml").decode("utf-8")
        # The six legacy tabs keep their original indices; the new audit-only
        # pending-items tab is appended as sheet7 for compatibility.
        open_items_xml = workbook.read("xl/worksheets/sheet7.xml").decode("utf-8")

    # The store summary uses only COMPLETE rows, while the blocked row remains
    # visible in detail; its engine amounts are audit evidence but the store
    # SUMIFS still excludes it from confirmed totals.
    assert re.search(r'<x:c r="C4"[^>]*>\s*<x:f>IF\(COUNTIFS\(', stores_xml)
    assert "COMPLETE" in stores_xml
    assert "$Y$4:$Y$6" in stores_xml
    assert re.search(r'<x:c r="U6"[^>]*>\s*<x:v>77(?:\.0+)?</x:v>', orders_xml)
    assert not re.search(r'<x:c r="U6"[^>]*>\s*<x:f>', orders_xml)
    assert re.search(r'<x:c r="W6"[^>]*>\s*<x:f>IF\(Y6=', orders_xml)
    assert re.search(r'<x:c r="W6"[^>]*>.*?<x:v>0(?:\.0+)?</x:v>', orders_xml, re.S)
    assert re.search(r'<x:c r="H4"[^>]*>\s*<x:v>500(?:\.0+)?</x:v>', open_items_xml)
    assert "BLOCKED" in open_items_xml
    verification = json.loads(artifacts.verification_path.read_text(encoding="utf-8"))
    assert verification["status"] == "PASS"
    assert verification["workbookStatus"] == "PENDING_DATA"
    assert verification["checkFailures"] == []
    assert verification["consistencyFailures"] == []


def test_workbook_renders_detailed_previous_report_variance_bridge(tmp_path: Path):
    payload = sample_payload()
    payload["previousReport"] = {
        "reportId": "weekly-previous",
        "totals": {"netTurnoverCny": "900"},
        "varianceRows": [
            {
                "metric": "netTurnoverCny",
                "label": "已确认营业额",
                "previous": "900",
                "newlyConfirmed": "60",
                "removedFromConfirmed": "-10",
                "confirmedCorrections": "50",
                "aggregateRecalculation": "0",
                "current": "1000",
                "delta": "100",
                "reason": "新增确认及历史更正",
            }
        ],
    }
    artifacts = write_report_artifacts(payload, tmp_path)

    with ZipFile(artifacts.workbook_path) as workbook:
        summary_xml = workbook.read("xl/worksheets/sheet1.xml").decode("utf-8")

    assert re.search(r'<x:c r="B21"[^>]*>\s*<x:v>900(?:\.0+)?</x:v>', summary_xml)
    assert re.search(r'<x:c r="C21"[^>]*>\s*<x:v>60(?:\.0+)?</x:v>', summary_xml)
    assert re.search(r'<x:c r="D21"[^>]*>\s*<x:v>-10(?:\.0+)?</x:v>', summary_xml)
    assert re.search(r'<x:c r="E21"[^>]*>\s*<x:v>50(?:\.0+)?</x:v>', summary_xml)
    assert re.search(r'<x:c r="F21"[^>]*>\s*<x:v>0(?:\.0+)?</x:v>', summary_xml)
    assert re.search(r'<x:c r="G21"[^>]*>\s*<x:v>1000(?:\.0+)?</x:v>', summary_xml)
    assert re.search(r'<x:c r="H21"[^>]*>\s*<x:f>IF\(OR\(B21=', summary_xml)


def test_workbook_falls_back_to_totals_only_variance_bridge(tmp_path: Path):
    payload = sample_payload()
    payload["previousReport"] = {
        "reportId": "weekly-previous",
        "totals": {"netTurnoverCny": "900"},
    }
    artifacts = write_report_artifacts(payload, tmp_path)

    with ZipFile(artifacts.workbook_path) as workbook:
        summary_xml = workbook.read("xl/worksheets/sheet1.xml").decode("utf-8")

    assert re.search(r'<x:c r="B21"[^>]*>\s*<x:v>900(?:\.0+)?</x:v>', summary_xml)
    assert re.search(r'<x:c r="G21"[^>]*>\s*<x:v>1000(?:\.0+)?</x:v>', summary_xml)
    assert re.search(r'<x:c r="H21"[^>]*>.*?<x:v>100(?:\.0+)?</x:v>', summary_xml, re.S)


def test_failed_consistency_gate_leaves_no_publishable_artifacts(tmp_path: Path):
    payload = sample_payload()
    payload["totals"]["managedOperationsCny"] = "999"

    with pytest.raises(RuntimeError, match="Finance workbook build failed"):
        write_report_artifacts(payload, tmp_path)

    report_id = payload["reportId"]
    assert not list(tmp_path.glob(f"{report_id}.*"))
    assert not (tmp_path / ".previews" / report_id).exists()


def test_stages_then_publishes_json_last_and_rewrites_preview_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: list[dict] = []
    events: list[Path] = []
    install_fake_builder(monkeypatch, calls=calls)
    real_publish_file = finance_reporting._publish_file_no_clobber
    real_publish_directory = finance_reporting._publish_directory_no_clobber

    def record_file(source: Path, target: Path) -> None:
        events.append(target)
        real_publish_file(source, target)

    def record_directory(source: Path, target: Path) -> None:
        events.append(target)
        real_publish_directory(source, target)

    monkeypatch.setattr(finance_reporting, "_publish_file_no_clobber", record_file)
    monkeypatch.setattr(
        finance_reporting, "_publish_directory_no_clobber", record_directory
    )

    artifacts = write_report_artifacts(sample_payload(), tmp_path)

    assert len(calls) == 1
    call = calls[0]
    staging_dir = call["input"].parent
    assert staging_dir.parent == tmp_path / ".staging"
    assert call["workbook"].parent == staging_dir
    assert call["verification"].parent == staging_dir
    assert call["previews"].parent == staging_dir
    assert call["formalJsonExisted"] is False
    assert events[-1] == artifacts.json_path
    assert not (tmp_path / ".staging").exists()

    verification = json.loads(
        artifacts.verification_path.read_text(encoding="utf-8")
    )
    expected_preview_root = (tmp_path / ".previews" / artifacts.report_id).resolve()
    assert len(verification["renderedSheets"]) == 7
    for raw_path in verification["renderedSheets"]:
        preview = Path(raw_path)
        assert preview.parent == expected_preview_root
        assert preview.is_file()


@pytest.mark.parametrize(
    "missing_key", sorted(finance_reporting._REQUIRED_VERIFICATION_KEYS)
)
def test_missing_verification_key_never_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_key: str
):
    install_fake_builder(
        monkeypatch,
        mutate_verification=lambda value: value.pop(missing_key),
    )

    with pytest.raises(RuntimeError, match="Finance workbook verification failed"):
        write_report_artifacts(sample_payload(), tmp_path)

    report_id = sample_payload()["reportId"]
    assert not (tmp_path / f"{report_id}.json").exists()
    assert not (tmp_path / f"{report_id}.xlsx").exists()
    assert not (tmp_path / ".previews" / report_id).exists()
    assert not (tmp_path / ".staging").exists()


@pytest.mark.parametrize(
    ("case", "mutate"),
    [
        pytest.param(
            "failed-status",
            lambda value: value.__setitem__("status", "FAIL"),
            id="failed-status",
        ),
        pytest.param(
            "wrong-report",
            lambda value: value.__setitem__("reportId", "another-report"),
            id="wrong-report-id",
        ),
        pytest.param(
            "non-integer-formula-count",
            lambda value: value.__setitem__("formulaErrorCount", False),
            id="non-integer-formula-count",
        ),
        pytest.param(
            "formula-errors",
            lambda value: value.__setitem__("formulaErrorCount", 1),
            id="formula-errors",
        ),
        pytest.param(
            "illegal-workbook-status",
            lambda value: value.__setitem__("workbookStatus", "RESTATED"),
            id="illegal-workbook-status",
        ),
        pytest.param(
            "check-failure",
            lambda value: value.__setitem__("checkFailures", [{"row": 4}]),
            id="check-failure",
        ),
        pytest.param(
            "invalid-consistency-failures",
            lambda value: value.__setitem__("consistencyFailures", None),
            id="invalid-consistency-failures",
        ),
        pytest.param(
            "consistency-failure",
            lambda value: value.__setitem__(
                "consistencyFailures", [{"check": "total"}]
            ),
            id="consistency-failure",
        ),
        pytest.param(
            "six-previews",
            lambda value: value.__setitem__(
                "renderedSheets", value["renderedSheets"][:6]
            ),
            id="six-previews",
        ),
        pytest.param(
            "duplicate-previews",
            lambda value: value.__setitem__(
                "renderedSheets", [value["renderedSheets"][0]] * 7
            ),
            id="duplicate-previews",
        ),
    ],
)
def test_invalid_verification_never_publishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    mutate,
):
    del case
    install_fake_builder(monkeypatch, mutate_verification=mutate)

    with pytest.raises(RuntimeError, match="Finance workbook verification failed"):
        write_report_artifacts(sample_payload(), tmp_path)

    report_id = sample_payload()["reportId"]
    assert not (tmp_path / f"{report_id}.json").exists()
    assert not (tmp_path / f"{report_id}.xlsx").exists()
    assert not (tmp_path / ".previews" / report_id).exists()
    assert not (tmp_path / ".staging").exists()


def test_missing_rendered_preview_never_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def remove_rendered_preview(verification: dict) -> None:
        Path(verification["renderedSheets"][-1]).unlink()

    install_fake_builder(
        monkeypatch, mutate_verification=remove_rendered_preview
    )

    with pytest.raises(RuntimeError, match="Finance workbook verification failed"):
        write_report_artifacts(sample_payload(), tmp_path)

    assert not (tmp_path / f"{sample_payload()['reportId']}.json").exists()
    assert not (tmp_path / ".staging").exists()


@pytest.mark.parametrize(
    ("report_status", "workbook_status"),
    [("FINAL", "PENDING_DATA"), ("PENDING_DATA", "FINAL")],
)
def test_workbook_status_must_match_report_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    report_status: str,
    workbook_status: str,
):
    payload = sample_payload()
    payload["status"] = report_status
    install_fake_builder(
        monkeypatch,
        mutate_verification=lambda value: value.__setitem__(
            "workbookStatus", workbook_status
        ),
    )

    with pytest.raises(RuntimeError, match="does not match report status"):
        write_report_artifacts(payload, tmp_path)

    assert not (tmp_path / f"{payload['reportId']}.json").exists()


def test_restated_report_accepts_final_workbook_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    payload = sample_payload()
    payload["status"] = "RESTATED"
    install_fake_builder(monkeypatch)

    artifacts = write_report_artifacts(payload, tmp_path)

    verification = json.loads(
        artifacts.verification_path.read_text(encoding="utf-8")
    )
    assert verification["workbookStatus"] == "FINAL"
    assert artifacts.json_path.exists()


def test_concurrent_publication_has_one_complete_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    barrier = Barrier(2)
    calls: list[dict] = []
    install_fake_builder(monkeypatch, barrier=barrier, calls=calls)
    payloads = []
    for marker in ("A", "B"):
        payload = deepcopy(sample_payload())
        payload["testMarker"] = marker
        payload["stores"][0]["storeAlias"] = f"store-{marker}"
        payloads.append(payload)

    def publish(payload: dict):
        try:
            return write_report_artifacts(payload, tmp_path)
        except Exception as exc:  # The losing writer is the expected result.
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, payloads))

    successes = [result for result in results if not isinstance(result, Exception)]
    failures = [result for result in results if isinstance(result, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], FileExistsError)
    assert len({call["input"].parent for call in calls}) == 2
    assert all(call["formalJsonExisted"] is False for call in calls)

    artifacts = successes[0]
    published_payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    winner = published_payload["testMarker"]
    assert artifacts.workbook_path.read_bytes() == f"workbook:{winner}".encode()
    assert f"store-{winner}" in artifacts.markdown_path.read_text(encoding="utf-8")
    verification = json.loads(
        artifacts.verification_path.read_text(encoding="utf-8")
    )
    assert verification["testMarker"] == winner
    assert len(verification["renderedSheets"]) == 7
    for index, raw_path in enumerate(verification["renderedSheets"], start=1):
        assert Path(raw_path).read_bytes() == f"{winner}:{index}".encode()
    assert not (tmp_path / ".staging").exists()


def test_publish_failure_before_json_leaves_no_completion_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_fake_builder(monkeypatch)
    real_publish = finance_reporting._publish_file_no_clobber

    def fail_before_verification(source: Path, target: Path) -> None:
        if target.name.endswith(".verification.json"):
            raise OSError("injected publish failure")
        real_publish(source, target)

    monkeypatch.setattr(
        finance_reporting, "_publish_file_no_clobber", fail_before_verification
    )
    payload = sample_payload()

    with pytest.raises(OSError, match="injected publish failure"):
        write_report_artifacts(payload, tmp_path)

    assert not (tmp_path / f"{payload['reportId']}.json").exists()
    assert not (tmp_path / ".staging").exists()

    # Files published before the JSON commit marker are an interrupted set,
    # not an immutable report.  A retry owns the publish lock, removes those
    # exact orphans, and completes normally.
    monkeypatch.setattr(
        finance_reporting,
        "_publish_file_no_clobber",
        real_publish,
    )
    artifacts = write_report_artifacts(payload, tmp_path)
    assert artifacts.json_path.exists()
    assert json.loads(artifacts.verification_path.read_text(encoding="utf-8"))[
        "status"
    ] == "PASS"


def test_existing_json_marker_rejects_before_starting_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    payload = sample_payload()
    marker = tmp_path / f"{payload['reportId']}.json"
    marker.write_text("published", encoding="utf-8")
    calls: list[dict] = []
    install_fake_builder(monkeypatch, calls=calls)

    with pytest.raises(FileExistsError, match="immutable and already exists"):
        write_report_artifacts(payload, tmp_path)

    assert marker.read_text(encoding="utf-8") == "published"
    assert calls == []


def test_explicit_repair_replaces_invalid_completion_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    payload = sample_payload()
    marker = tmp_path / f"{payload['reportId']}.json"
    marker.write_text("not-json", encoding="utf-8")
    install_fake_builder(monkeypatch)

    artifacts = write_report_artifacts(
        payload,
        tmp_path,
        repair_incomplete=True,
    )

    assert json.loads(artifacts.json_path.read_text(encoding="utf-8"))[
        "reportId"
    ] == payload["reportId"]
    assert artifacts.workbook_path.exists()


def test_markdown_renders_variance_v2_aggregate_and_warning():
    payload = sample_payload()
    payload["previousReport"] = {
        "schemaVersion": "finance-report-variance-v2",
        "status": "PENDING_DATA",
        "reportStatus": "FINAL",
        "reportId": "weekly-previous",
        "period": {"to": "2026-08-16"},
        "compatibilityIssues": ["上一版缺少同口径字段"],
        "basisChanges": ["财务策略：policy-v1 → policy-v2"],
        "varianceRows": [
            {
                "metric": "strictOwnerProfitCny",
                "label": "已确认严格利润",
                "status": "PENDING_DATA",
                "previous": "100",
                "newlyConfirmed": "10",
                "removedFromConfirmed": "0",
                "confirmedCorrections": "5",
                "aggregateRecalculation": None,
                "current": "120",
                "delta": "20",
                "reason": "缺少同口径字段，精确归因待定",
            }
        ],
    }

    text = build_owner_markdown(payload)

    assert "报告状态 FINAL" in text
    assert "差异桥状态：PENDING_DATA" in text
    assert "不影响本期报告金额及发布状态" in text
    assert "兼容性提示：上一版缺少同口径字段" in text
    assert "口径变化：财务策略：policy-v1 → policy-v2" in text
    assert "| 留存订单更正 | 汇总重算 |" in text
    assert "| 5.00 | 待定 | 120.00 |" in text
    assert "状态：PENDING_DATA" in text
