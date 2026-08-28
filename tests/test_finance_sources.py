from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from wb_manager.finance_sources import (
    ApprovalRequiredError,
    CampaignFundingSource,
    FinanceSourceError,
    FinanceSources,
    RequestSpec,
    advertising_pnl_events,
    classify_logistics_row,
    date_windows,
    deposit_campaign_budget,
    load_store_specs,
    match_order_rows,
    normalize_advertising_cost_events,
    normalize_advertising_payment_events,
    resolve_enabled_store_credentials,
)


@dataclass
class Response:
    status: int
    ok: bool
    data: object = None


class QueueClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, body=None, retries=2):
        self.calls.append(
            {"method": method, "url": url, "body": body, "retries": retries}
        )
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def sources(client, **kwargs):
    return FinanceSources(
        client,
        store_alias="owner-store",
        finance_throttle_seconds=0,
        statistics_throttle_seconds=0,
        chunk_throttle_seconds=0,
        goods_return_throttle_seconds=0,
        **kwargs,
    )


def test_store_scope_auto_includes_future_enabled_store_and_redacts_tokens(
    tmp_path: Path, monkeypatch
):
    (tmp_path / "owner.env").write_text("OWNER_TOKEN=file-owner-secret\n", encoding="utf-8")
    (tmp_path / "future.env").write_text("FUTURE_TOKEN=file-future-secret\n", encoding="utf-8")
    scope = {
        "stores": [
            {"alias": "owner", "tokenEnv": "OWNER_TOKEN", "tokenFile": "owner.env"},
            {
                "alias": "disabled",
                "tokenEnv": "DISABLED_TOKEN",
                "tokenFile": "disabled.env",
                "enabled": False,
            },
            {
                "alias": "future",
                "tokenEnv": "FUTURE_TOKEN",
                "tokenFile": "future.env",
                "enabled": True,
            },
        ]
    }
    monkeypatch.setenv("OWNER_TOKEN", "environment-owner-secret")

    specs = load_store_specs(scope, repo_root=tmp_path)
    credentials = resolve_enabled_store_credentials(
        scope, repo_root=tmp_path, environ={"OWNER_TOKEN": "environment-owner-secret"}
    )

    assert [row.alias for row in specs] == ["owner", "future"]
    assert credentials[0].token == "environment-owner-secret"
    assert credentials[1].token == "file-future-secret"
    assert "environment-owner-secret" not in repr(credentials[0])
    assert "file-future-secret" not in repr(credentials[1])
    assert "<redacted>" in repr(credentials[0])


def test_scope_rejects_embedded_credential_values(tmp_path: Path):
    with pytest.raises(FinanceSourceError):
        load_store_specs(
            {
                "stores": [
                    {
                        "alias": "bad",
                        "tokenEnv": "BAD_TOKEN",
                        "tokenFile": "bad.env",
                        "token": "must-not-be-here",
                    }
                ]
            },
            repo_root=tmp_path,
        )


def test_relative_scope_path_is_resolved_from_repo_root(tmp_path: Path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "stores.json").write_text(
        '{"stores":[{"alias":"one","tokenEnv":"ONE_TOKEN","tokenFile":"one.env"}]}',
        encoding="utf-8",
    )

    specs = load_store_specs("config/stores.json", repo_root=tmp_path)

    assert specs[0].token_file == (tmp_path / "one.env").resolve()


def test_finance_detail_paginates_by_rrdid_until_204():
    client = QueueClient(
        [
            Response(200, True, [{"rrdId": 10}, {"rrdId": 11}]),
            Response(200, True, [{"rrdId": 12}]),
            Response(204, True, None),
        ]
    )

    result = sources(client).finance_detailed(
        "2026-08-01", "2026-08-07", fields=["orderId"]
    )

    assert result.status == 204
    assert result.complete is True
    assert [row["rrdId"] for row in result.rows] == [10, 11, 12]
    assert [call["body"]["rrdId"] for call in client.calls] == [0, 11, 12]
    assert client.calls[0]["body"]["fields"] == ["rrdId", "orderId"]
    assert all(call["retries"] == 0 for call in client.calls)


def test_finance_detail_keeps_partial_rows_and_http_429():
    client = QueueClient(
        [
            Response(200, True, [{"rrdId": 10}]),
            Response(429, False, {"error": "rate limited"}),
        ]
    )

    result = sources(client).finance_detailed("2026-08-01", "2026-08-07")

    assert result.status == 429
    assert result.complete is False
    assert result.rows == [{"rrdId": 10}]
    assert "429" in result.blocker


@pytest.mark.parametrize("kind", ["sales", "orders"])
def test_statistics_incremental_uses_last_change_cursor_until_empty(kind):
    client = QueueClient(
        [
            Response(
                200,
                True,
                [
                    {"srid": "a", "lastChangeDate": "2026-08-01T01:00:00"},
                    {"srid": "b", "lastChangeDate": "2026-08-01T02:00:00"},
                ],
            ),
            Response(200, True, []),
        ]
    )
    adapter = sources(client)

    result = getattr(adapter, f"statistics_{kind}")("2026-08-01T00:00:00")

    assert result.status == 200
    assert result.complete is True
    assert len(result.rows) == 2
    first_query = parse_qs(urlparse(client.calls[0]["url"]).query)
    second_query = parse_qs(urlparse(client.calls[1]["url"]).query)
    assert first_query["dateFrom"] == ["2026-08-01T00:00:00"]
    assert second_query["dateFrom"] == ["2026-08-01T02:00:00"]
    assert first_query["flag"] == ["0"]


def test_statistics_inclusive_boundary_repeat_can_terminate_complete():
    row = {"srid": "a", "lastChangeDate": "2026-08-01T01:00:00"}
    client = QueueClient([Response(200, True, [row]), Response(200, True, [dict(row)])])

    result = sources(client).statistics_sales("2026-08-01T00:00:00")

    assert result.complete is True
    assert result.rows == [row]
    assert len(client.calls) == 2


def test_goods_return_and_advertising_ranges_are_split_into_at_most_31_days():
    windows = date_windows(date(2026, 1, 1), date(2026, 3, 5))
    assert windows == (
        (date(2026, 1, 1), date(2026, 1, 31)),
        (date(2026, 2, 1), date(2026, 3, 3)),
        (date(2026, 3, 4), date(2026, 3, 5)),
    )
    assert all((end - start).days + 1 <= 31 for start, end in windows)

    goods_client = QueueClient(
        [
            Response(200, True, {"report": [{"orderId": 1}]}),
            Response(204, True),
            Response(200, True, {"report": [{"orderId": 2}]}),
        ]
    )
    captured = []

    def builder(start, end):
        captured.append((start, end))
        return RequestSpec("GET", f"https://example.test?from={start}&to={end}")

    goods = sources(goods_client).goods_returns(
        "2026-01-01", "2026-03-05", request_builder=builder
    )
    assert goods.complete is True
    assert goods.rows == [{"orderId": 1}, {"orderId": 2}]
    assert captured == list(windows)

    ad_client = QueueClient(
        [Response(200, True, []), Response(200, True, []), Response(200, True, [])]
    )
    ads = sources(ad_client).advertising_costs("2026-01-01", "2026-03-05")
    assert ads.complete is True
    for call in ad_client.calls:
        query = parse_qs(urlparse(call["url"]).query)
        start = date.fromisoformat(query["from"][0])
        end = date.fromisoformat(query["to"][0])
        assert (end - start).days + 1 <= 31


def test_uncertain_goods_return_contract_is_injectable_and_failure_is_not_zero():
    client = QueueClient([Response(200, True, {"unexpected": []})])
    result = sources(client).goods_returns(
        "2026-08-01",
        "2026-08-02",
        request_builder=lambda start, end: RequestSpec(
            "POST", "https://example.test/goods-return", {"start": str(start), "end": str(end)}
        ),
    )

    assert result.status == 200
    assert result.complete is False
    assert result.rows == []
    assert "contract" in result.blocker
    assert client.calls[0]["method"] == "POST"


def test_order_match_prefers_srid_but_falls_back_to_orderid_and_shkid():
    exact = match_order_rows(
        {"srid": "same", "orderId": 1}, {"srid": "same", "orderId": 2}
    )
    mismatched_srid = match_order_rows(
        {"srid": "goods-return-r", "orderId": 5345892555, "shkId": 56183033024},
        {"srid": "finance-0.0", "orderId": 5345892555, "shkId": 56183033024},
    )
    only_shk = match_order_rows(
        {"srid": "left", "shkId": 99}, {"srid": "right", "shkId": 99}
    )

    assert (exact.matched, exact.key) == (True, "srid")
    assert (mismatched_srid.matched, mismatched_srid.key) == (True, "orderId")
    assert (only_shk.matched, only_shk.key) == (True, "shkId")


def test_cancel_logistics_is_return_even_when_return_amount_is_zero_and_sign_is_kept():
    canceled = classify_logistics_row(
        {
            "sellerOperName": "物流",
            "bonusTypeName": "取消时给客户",
            "deliveryAmount": 1,
            "returnAmount": 0,
            "deliveryService": "8",
            "rebillLogisticCost": "0",
        }
    )
    adjusted = classify_logistics_row(
        {
            "sellerOperName": "物流",
            "bonusTypeName": "从客户返回退货",
            "deliveryService": "-8",
            "rebillLogisticCost": "-1.25",
        }
    )

    assert canceled.direction == "return"
    assert canceled.amount == Decimal("8")
    assert canceled.blocker is None
    assert adjusted.direction == "return"
    assert adjusted.amount == Decimal("-9.25")


def test_forward_and_unknown_logistics_are_not_guessed_as_return():
    forward = classify_logistics_row(
        {
            "sellerOperName": "物流",
            "bonusTypeName": "销售时给客户",
            "deliveryService": "8",
        }
    )
    unknown = classify_logistics_row(
        {"sellerOperName": "物流", "deliveryAmount": 1, "deliveryService": "16"}
    )

    assert forward.direction == "forward"
    assert forward.blocker is None
    assert unknown.direction == "unknown"
    assert unknown.amount == Decimal("16")
    assert unknown.blocker


def test_advertising_upd_is_stable_deduplicated_pnl_and_payments_are_not_expense():
    upd = {
        "updNum": 77,
        "updTime": "2026-08-10T12:00:00+03:00",
        "updSum": "24.00",
        "advertId": 123,
        "campName": "phone",
        "paymentType": "Баланс",
    }
    costs_one = normalize_advertising_cost_events([upd], "owner-store")
    costs_two = normalize_advertising_cost_events([dict(upd)], "owner-store")
    funding = normalize_advertising_payment_events(
        [{"id": 1, "sum": 500, "date": "2026-08-10"}], "owner-store"
    )

    selected = advertising_pnl_events(costs_one, costs_two, funding)

    assert costs_one[0]["eventId"] == costs_two[0]["eventId"]
    assert len(selected) == 1
    assert selected[0]["amount"] == "24"
    assert funding[0]["pnl"] is False


def test_balance_and_payments_keep_explicit_empty_success_and_failure_status():
    client = QueueClient(
        [
            Response(200, True, {"currency": "CNY", "current": 10}),
            Response(429, False, {"error": "rate limited"}),
        ]
    )
    adapter = sources(client)

    balance = adapter.seller_balance()
    payments = adapter.advertising_payments("2026-08-01", "2026-08-01")

    assert balance.status == 200 and balance.complete is True
    assert balance.rows == [{"currency": "CNY", "current": 10}]
    assert payments.status == 429 and payments.complete is False
    assert payments.rows == []


def test_campaign_deposit_requires_explicit_live_approval_and_valid_inputs():
    client = QueueClient([])
    with pytest.raises(ApprovalRequiredError):
        deposit_campaign_budget(
            client,
            campaign_id=1,
            amount=100,
            source="balance",
            approved_by="boss",
            confirm_live=False,
        )
    with pytest.raises(ApprovalRequiredError):
        deposit_campaign_budget(
            client,
            campaign_id=1,
            amount=100,
            source="balance",
            approved_by="",
            confirm_live=True,
        )
    with pytest.raises(FinanceSourceError):
        deposit_campaign_budget(
            client,
            campaign_id=1,
            amount=0,
            source="balance",
            approved_by="boss",
            confirm_live=True,
        )
    assert client.calls == []


def test_campaign_deposit_uses_no_retry_is_not_pnl_and_requires_readback():
    client = QueueClient([Response(200, True, {"total": 7289})])

    result = deposit_campaign_budget(
        client,
        campaign_id=1234567,
        amount=5000,
        source=CampaignFundingSource.BALANCE,
        approved_by="boss",
        confirm_live=True,
    )

    assert result.state == "succeeded"
    assert result.status == 200
    assert result.pnl is False
    assert result.requires_readback is True
    assert client.calls == [
        {
            "method": "POST",
            "url": "https://advert-api.wildberries.ru/adv/v1/budget/deposit?id=1234567",
            "body": {"sum": 5000, "type": 1, "return": True},
            "retries": 0,
        }
    ]


def test_campaign_deposit_transport_timeout_is_unknown_and_never_retried():
    client = QueueClient([TimeoutError("response lost")])

    result = deposit_campaign_budget(
        client,
        campaign_id=123,
        amount=100,
        source="account",
        approved_by="boss",
        confirm_live=True,
    )

    assert result.state == "unknown"
    assert result.status is None
    assert result.requires_readback is True
    assert len(client.calls) == 1
    assert client.calls[0]["retries"] == 0
