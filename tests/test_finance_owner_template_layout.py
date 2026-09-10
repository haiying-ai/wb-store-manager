from copy import deepcopy
from datetime import date, timedelta
import json
from pathlib import Path
import shutil
import subprocess
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "wb_manager" / "assets" / "build_running_finance_report.mjs"
RELEASE_MANIFEST = ROOT / "config" / "finance" / "owner_report_template_release.json"
ARTIFACT_TOOL = ROOT / "node_modules" / "@oai" / "artifact-tool" / "package.json"

EVENT_METRICS = (
    "gross_sales",
    "seller_proceeds",
    "product_cost",
    "agency_accrued",
    "promotion_accrued",
    "cash_ad_spend",
    "logistics_cost",
    "shared_pool_return_cost",
    "other_cost",
    "promo_credit_spend",
    "agency_paid",
    "receipts_received",
    "withdrawal_requested",
    "withdrawal_completed",
    "withdrawal_cancelled",
    "return_reserve_accrued",
    "separate_return_cost",
    "shared_pool_accrued",
)
ACCOUNT_METRICS = EVENT_METRICS + (
    "advertising_available",
    "operating_contribution",
    "profit",
    "withdrawal_pending",
    "withdrawal_available",
    "excess_return_loss",
)
PERIODS = (
    {"key": "cumulative", "label": "开业至截止日", "start": "2026-08-01", "end": "2026-09-09"},
    {"key": "last_month", "label": "上个完整月", "start": "2026-08-01", "end": "2026-08-31"},
    {"key": "last_week", "label": "上个完整周", "start": "2026-08-31", "end": "2026-09-06"},
    {"key": "yesterday", "label": "截止日", "start": "2026-09-09", "end": "2026-09-09"},
)


def _amounts(*, cash_ad_spend=0):
    values = {metric: "0.00" for metric in EVENT_METRICS}
    values["cash_ad_spend"] = f"{cash_ad_spend:.2f}"
    return values


def _event(alias, index):
    event_date = date(2026, 8, 21) + timedelta(days=index)
    if index == 0:
        evidence = [
            f"finance:{alias}:oldest-and-longest:" + "x" * 360,
            *(f"supporting:{alias}:{item:03d}" for item in range(1, 120)),
        ]
    elif index == 10:
        evidence = [f"finance:{alias}:middle:" + "y" * 180, "secondary:trace"]
    else:
        evidence = [f"finance:{alias}:rrd:{index:03d}"]
    return {
        "eventDate": event_date.isoformat(),
        "eventId": f"sale:{alias}:{index:03d}",
        "kind": "sale",
        "kindLabel": "销售",
        "isOrderEvent": True,
        "orderId": f"order-{alias}-{index:03d}",
        "sku": f"SKU-{index:03d}",
        "srid": f"srid-{alias}-{index:03d}",
        "mappingReason": "按店铺和完整平台标识关联",
        "mappingStatus": "已关联",
        "amountBasis": "正式",
        "amounts": _amounts(),
        "actualAmounts": {"gross_sales": "0.00", "seller_proceeds": "0.00", "promotion_accrued": "0.00"},
        "estimateAmounts": {"gross_sales": "0.00", "seller_proceeds": "0.00"},
        "costVersionId": "fixture-cost-v1",
        "policyId": "fixture-policy-v1",
        "linkEvidence": evidence,
        "saleIds": [],
    }


def _cash_event(alias, amount):
    return {
        "eventDate": "2026-09-09",
        "eventId": f"advertising:{alias}:2026-09-09",
        "kind": "advertising_expense",
        "kindLabel": "广告实际消耗",
        "isOrderEvent": False,
        "orderId": None,
        "sku": None,
        "srid": None,
        "mappingReason": "可信店铺级费用",
        "mappingStatus": "店铺级",
        "amountBasis": "正式",
        "amounts": _amounts(cash_ad_spend=amount),
        "actualAmounts": {"gross_sales": None, "seller_proceeds": None, "promotion_accrued": "0.00"},
        "estimateAmounts": {"gross_sales": None, "seller_proceeds": None},
        "costVersionId": None,
        "policyId": "fixture-policy-v1",
        "linkEvidence": [f"advertising:{alias}:document:2026-09-09"],
        "saleIds": [],
    }


def _metric(value):
    return {"known_subtotal": f"{value:.2f}", "complete": True}


def _account(*, period_cash, cumulative_cash):
    period_values = {metric: 0 for metric in ACCOUNT_METRICS}
    cumulative_values = {metric: 0 for metric in ACCOUNT_METRICS}
    period_values.update(cash_ad_spend=period_cash, operating_contribution=-period_cash)
    cumulative_values.update(
        cash_ad_spend=cumulative_cash,
        advertising_available=-cumulative_cash,
        operating_contribution=-cumulative_cash,
    )
    return {
        "period": {"metrics": {key: _metric(value) for key, value in period_values.items()}},
        "cumulative": {"metrics": {key: _metric(value) for key, value in cumulative_values.items()}},
    }


def _stress_payload():
    stores = {}
    cash_by_store = {"owner-store": 25, "new-store": 35}
    for alias, amount in cash_by_store.items():
        orders = [_event(alias, index) for index in range(20)]
        stores[alias] = {
            "displayName": alias,
            "openedAt": "2026-08-01",
            "flowRows": [*orders, _cash_event(alias, amount)],
            "orderRows": orders,
            "mappingSummary": {"linked": len(orders), "pending": 0},
        }

    accounts = {}
    for period in PERIODS:
        period_stores = {}
        for alias, amount in cash_by_store.items():
            cash_date = "2026-09-09"
            period_cash = amount if period["start"] <= cash_date <= period["end"] else 0
            cumulative_cash = amount if cash_date <= period["end"] else 0
            period_stores[alias] = _account(period_cash=period_cash, cumulative_cash=cumulative_cash)
        accounts[period["key"]] = {
            "actual": {"stores": deepcopy(period_stores)},
            "provisional": {"stores": deepcopy(period_stores)},
        }

    return {
        "templateVersion": "wb-owner-dashboard-v2.1",
        "periodEnd": "2026-09-09",
        "sourceCutoff": "2026-09-09",
        "nativeStatistics": [],
        "ownerView": {"periods": list(PERIODS), "stores": stores},
        "ownerPeriodAccounts": accounts,
    }


def _node_runtime():
    node = shutil.which("node")
    if not node or not ARTIFACT_TOOL.exists():
        pytest.skip("offline Node spreadsheet runtime is not installed")
    return node


def test_repository_release_manifest_matches_builder_bytes():
    import hashlib

    release = json.loads(RELEASE_MANIFEST.read_text(encoding="utf-8"))
    assert release["templateVersion"] == "wb-owner-dashboard-v2.1"
    assert (ROOT / release["builder"]).resolve() == BUILDER.resolve()
    assert release["approvedBuilderSha256"] == hashlib.sha256(BUILDER.read_bytes()).hexdigest()
    assert release["layoutContractVersion"] == "owner-dashboard-layout-v1"


def test_real_owner_builder_enforces_full_width_layout_contract(tmp_path):
    input_path = tmp_path / "stress-report.json"
    workbook_path = tmp_path / "stress-report.xlsx"
    verification_path = tmp_path / "verification.json"
    input_path.write_text(json.dumps(_stress_payload(), ensure_ascii=False), encoding="utf-8")

    completed = subprocess.run(
        [
            _node_runtime(),
            str(BUILDER),
            "--input",
            str(input_path),
            "--output",
            str(workbook_path),
            "--verification",
            str(verification_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout

    result = json.loads(completed.stdout.strip().splitlines()[-1])
    verification = json.loads(verification_path.read_text(encoding="utf-8"))
    layout = verification["layoutContract"]

    assert result == {
        "output": str(workbook_path),
        "sheets": 8,
        "formulaErrorCount": 0,
        "verifiedMetricCount": 136,
        "reconciliationRows": 96,
        "recalcVerified": True,
        "layoutContractPassed": True,
    }
    assert verification["templateVersion"] == "wb-owner-dashboard-v2.1"
    assert verification["formulaErrorCount"] == 0
    assert verification["totalsVerified"] is True
    assert verification["verifiedMetricCount"] == 136
    assert verification["reconciliationRows"] == 96
    assert verification["reconciliationDifference"] == 0
    assert verification["recalcVerified"] is True

    assert layout["passed"] is True
    assert layout["flowMaxColumn"] == "AJ"
    assert layout["orderMaxColumn"] == "AC"
    assert layout["noHelperColumnsBeyond"] is True
    assert layout["readableCompactEvidence"] is True
    assert layout["compactEvidenceMaxCharacters"] == 40
    assert layout["previewRangesFullWidth"] is True
    assert len(layout["sheets"]) == 8
    assert all(item["actualMaxColumn"] == item["expectedMaxColumn"] for item in layout["sheets"].values())
    assert {item["actualMaxColumn"] for item in layout["sheets"].values() if item["kind"] == "flow"} == {"AJ"}
    assert {item["actualMaxColumn"] for item in layout["sheets"].values() if item["kind"] == "order"} == {"AC"}

    for sheet_name, stats in layout["evidence"].items():
        assert stats["rowsChecked"] == (21 if sheet_name.endswith("流水") else 20)
        assert stats["longestEvidenceRow"] == stats["lastDataRow"]
        assert stats["longestCharacters"] <= 40
        assert stats["singleLine"] is True
        assert stats["withinCharacterLimit"] is True

    for sheet_name, preview_range in layout["previewRanges"].items():
        expected_max = layout["sheets"][sheet_name]["expectedMaxColumn"]
        assert preview_range.startswith("A1:")
        assert preview_range.split(":", 1)[1].rstrip("0123456789") == expected_max

    rendered = [Path(item) for item in verification["renderedSheets"]]
    assert len(rendered) == 8
    assert all(item.is_file() and item.stat().st_size > 0 for item in rendered)
    assert workbook_path.is_file() and workbook_path.stat().st_size > 0
    with ZipFile(workbook_path) as archive:
        workbook_xml = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    assert len(workbook_xml.findall("x:sheets/x:sheet", namespace)) == 8
