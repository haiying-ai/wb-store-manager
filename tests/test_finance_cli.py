from __future__ import annotations

import argparse

from wb_manager.finance_cli import add_finance_parser


def test_cost_import_requires_explicit_source_currency_and_allows_cny_without_fx():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_finance_parser(subparsers)

    args = parser.parse_args(
        [
            "finance",
            "cost-import",
            "--quote",
            "0830.xlsx",
            "--effective-from",
            "2026-09-06T13:08:38+03:00",
            "--approved-by",
            "boss",
            "--source-currency",
            "CNY",
        ]
    )

    assert args.source_currency == "CNY"
    assert args.rub_per_cny is None


def test_ads_deposit_plan_cli_is_single_store_balance_only() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_finance_parser(subparsers)

    args = parser.parse_args(
        [
            "finance",
            "ads-deposit-plan",
            "--funds-plan",
            "weekly-funds-plan.json",
            "--store",
            "owner-store",
            "--campaign",
            "1234567",
            "--amount-cny",
            "100",
            "--approved-by",
            "boss",
        ]
    )

    assert args.finance_command == "ads-deposit-plan"
    assert args.funds_plan == "weekly-funds-plan.json"
    assert args.store == "owner-store"
    assert args.campaign == 1234567
    assert args.amount_cny == 100
    assert args.approved_by == "boss"
    assert not hasattr(args, "source")


def test_withdrawal_record_omitted_ad_fee_flags_are_none() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_finance_parser(subparsers)

    args = parser.parse_args(
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
            "2026-08-01T10:00:00+00:00",
        ]
    )

    assert args.ad_fee_due_cny is None
    assert args.ad_fee_withheld_cny is None
    assert args.ad_fee_status is None


def test_withdrawal_record_cli_accepts_settlement_balance_without_fee_amounts() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_finance_parser(subparsers)

    args = parser.parse_args(
        [
            "finance",
            "withdrawal-record",
            "--store",
            "owner-store",
            "--record-key",
            "manual-settlement-balance",
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
