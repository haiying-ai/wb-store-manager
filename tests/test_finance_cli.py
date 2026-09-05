from __future__ import annotations

import argparse

from wb_manager.finance_cli import add_finance_parser


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    add_finance_parser(value.add_subparsers(dest="command", required=True))
    return value


def test_withdrawal_record_keeps_omitted_legacy_fee_facts_unknown():
    args = parser().parse_args(
        [
            "finance",
            "withdrawal-record",
            "--store",
            "owner-store",
            "--record-key",
            "manual-1",
            "--amount-cny",
            "100",
            "--withdrawn-at",
            "2026-09-04T10:00:00+00:00",
        ]
    )

    assert args.ad_fee_due_cny is None
    assert args.ad_fee_withheld_cny is None
    assert args.ad_fee_status is None


def test_withdrawal_record_accepts_settlement_balance_without_fee_amounts():
    args = parser().parse_args(
        [
            "finance",
            "withdrawal-record",
            "--store",
            "owner-store",
            "--record-key",
            "manual-current",
            "--amount-cny",
            "100",
            "--withdrawn-at",
            "2026-09-05T10:00:00+00:00",
            "--ad-fee-status",
            "settlement_balance",
        ]
    )

    assert args.ad_fee_status == "settlement_balance"
    assert args.ad_fee_due_cny is None
    assert args.ad_fee_withheld_cny is None
