from __future__ import annotations

from decimal import Decimal

from wb_manager.finance_service import _build_previous_report_bridge
from wb_manager.finance_reporting import build_owner_markdown


def test_previous_report_bridge_explains_order_removed_from_confirmation():
    order_key = "srid:status-corrected"
    previous = {
        "reportId": "previous-report",
        "status": "PENDING_DATA",
        "generatedAt": "2026-09-01T09:00:00+08:00",
        "period": {"from": "2026-07-15", "to": "2026-08-31"},
        "totals": {
            "confirmedOrderCount": 2,
            "salesAmountGCny": "55128.06",
            "managedOperationsCny": "1102.56",
            "combinedAdvertisingAndReturnPoolCny": "1653.84",
            "strictOwnerProfitCny": "2110.06",
        },
        "orders": [
            {
                "storeAlias": "owner-store",
                "orderKey": "srid:retained",
                "evidenceStatus": "COMPLETE",
                "cashReconciliationStatus": "PASS",
                "salesAmountGCny": "49378.03",
                "managedOperationsCny": "987.56",
                "combinedAdvertisingAndReturnPoolCny": "1481.34",
                "strictOwnerProfitCny": "1868.87",
            },
            {
                "storeAlias": "new-store",
                "orderKey": order_key,
                "evidenceStatus": "COMPLETE",
                "cashReconciliationStatus": "PASS",
                "salesAmountGCny": "5750.03",
                "managedOperationsCny": "115.00",
                "combinedAdvertisingAndReturnPoolCny": "172.50",
                "strictOwnerProfitCny": "241.19",
            }
        ],
    }
    current_orders = [
        {
            "storeAlias": "owner-store",
            "orderKey": "srid:retained",
            "evidenceStatus": "COMPLETE",
            "cashReconciliationStatus": "PASS",
            "salesAmountGCny": "49378.03",
            "managedOperationsCny": "987.56",
            "combinedAdvertisingAndReturnPoolCny": "1481.34",
            "strictOwnerProfitCny": "1868.87",
        },
        {
            "storeAlias": "new-store",
            "orderKey": order_key,
            "evidenceStatus": "BLOCKED",
            "cashReconciliationStatus": "PASS",
            "salesAmountGCny": "5750.03",
            "managedOperationsCny": "115.00",
            "combinedAdvertisingAndReturnPoolCny": "172.50",
            "strictOwnerProfitCny": None,
        }
    ]
    current_totals = {
        "confirmedOrderCount": 1,
        "salesAmountGCny": "49378.03",
        "managedOperationsCny": "987.56",
        "combinedAdvertisingAndReturnPoolCny": "1481.34",
        "strictOwnerProfitCny": "1868.87",
    }

    bridge = _build_previous_report_bridge(
        previous,
        current_orders=current_orders,
        current_totals=current_totals,
    )

    rows = {row["metric"]: row for row in bridge["varianceRows"]}
    assert bridge["status"] == "COMPLETE"
    assert bridge["removedFromConfirmedOrders"] == [
        {"storeAlias": "new-store", "orderKey": order_key}
    ]
    assert rows["confirmedOrderCount"]["delta"] == -1
    assert rows["salesAmountGCny"]["removedFromConfirmed"] == "-5750.03"
    assert rows["managedOperationsCny"]["removedFromConfirmed"] == "-115.00"
    assert rows["combinedAdvertisingAndReturnPoolCny"]["removedFromConfirmed"] == "-172.50"
    assert rows["strictOwnerProfitCny"]["removedFromConfirmed"] == "-241.19"
    assert all(row["aggregateRecalculation"] == "0.00" or row["aggregateRecalculation"] == 0 for row in rows.values())
    for row in rows.values():
        previous_value = Decimal(str(row["previous"]))
        current_value = Decimal(str(row["current"]))
        explained = sum(
            Decimal(str(row[key]))
            for key in (
                "newlyConfirmed",
                "removedFromConfirmed",
                "confirmedCorrections",
            )
        )
        assert previous_value + explained == current_value

    markdown = build_owner_markdown(
        {
            "reportId": "current-report",
            "status": "PENDING_DATA",
            "kind": "bootstrap",
            "period": {"from": "2026-07-15", "to": "2026-09-05"},
            "totals": current_totals,
            "stores": [],
            "previousReport": bridge,
        }
    )
    assert "## 较上一版变动" in markdown
    assert "| 代运营费 2% | 1,102.56 | 0.00 | -115.00 | 0.00 | 0.00 | 987.56 | -115.00 | 1笔移出确认 |" in markdown


def test_previous_report_bridge_separates_order_corrections_and_aggregate_recalculation():
    previous_order = {
        "storeAlias": "owner-store",
        "orderKey": "srid:retained",
        "evidenceStatus": "COMPLETE",
        "cashReconciliationStatus": "PASS",
        "salesAmountGCny": "100.00",
        "managedOperationsCny": "2.00",
        "combinedAdvertisingAndReturnPoolCny": "3.00",
        "strictOwnerProfitCny": "10.00",
    }
    current_order = {
        **previous_order,
        "salesAmountGCny": "110.00",
        "managedOperationsCny": "2.20",
        "strictOwnerProfitCny": "12.00",
    }
    previous = {
        "reportId": "previous",
        "period": {"from": "2026-08-01", "to": "2026-08-31"},
        "totals": {
            "confirmedOrderCount": 1,
            "salesAmountGCny": "100.00",
            "managedOperationsCny": "2.00",
            "combinedAdvertisingAndReturnPoolCny": "3.00",
            "strictOwnerProfitCny": "10.00",
        },
        "orders": [previous_order],
    }
    bridge = _build_previous_report_bridge(
        previous,
        current_orders=[current_order],
        current_totals={
            "confirmedOrderCount": 1,
            "salesAmountGCny": "110.00",
            "managedOperationsCny": "2.20",
            "combinedAdvertisingAndReturnPoolCny": "3.50",
            "strictOwnerProfitCny": "13.00",
        },
    )

    rows = {row["metric"]: row for row in bridge["varianceRows"]}
    assert rows["salesAmountGCny"]["confirmedCorrections"] == "10.00"
    assert rows["salesAmountGCny"]["aggregateRecalculation"] == "0.00"
    assert rows["combinedAdvertisingAndReturnPoolCny"]["confirmedCorrections"] == "0.00"
    assert rows["combinedAdvertisingAndReturnPoolCny"]["aggregateRecalculation"] == "0.50"
    assert rows["strictOwnerProfitCny"]["confirmedCorrections"] == "2.00"
    assert rows["strictOwnerProfitCny"]["aggregateRecalculation"] == "1.00"


def test_previous_report_bridge_propagates_missing_money_as_pending():
    previous = {
        "reportId": "legacy",
        "period": {"from": "2026-08-01", "to": "2026-08-31"},
        "totals": {"confirmedOrderCount": 1},
        "orders": [
            {
                "storeAlias": "owner-store",
                "orderKey": "srid:legacy",
                "evidenceStatus": "COMPLETE",
                "cashReconciliationStatus": "PASS",
                "strictOwnerProfitCny": "1.00",
            }
        ],
    }
    bridge = _build_previous_report_bridge(
        previous,
        current_orders=[],
        current_totals={"confirmedOrderCount": 0},
    )

    rows = {row["metric"]: row for row in bridge["varianceRows"]}
    assert bridge["status"] == "PENDING_DATA"
    assert rows["salesAmountGCny"]["removedFromConfirmed"] is None
    assert rows["salesAmountGCny"]["aggregateRecalculation"] is None
    assert "精确归因待定" in rows["salesAmountGCny"]["reason"]
