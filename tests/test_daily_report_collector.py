from datetime import datetime
import json
from zoneinfo import ZoneInfo

import tools.fetch_wb_daily_report as daily


def test_call_disables_client_retry_multiplication():
    seen = {}

    class Client:
        def request(self, method, url, *, body, timeout, retries):
            seen.update(method=method, url=url, body=body, timeout=timeout, retries=retries)
            return type("Result", (), {"status": 200, "ok": True, "data": {}})()

    result = daily.call(Client(), "GET", "https://example.test", timeout=7)

    assert result["status"] == 200
    assert seen["timeout"] == 7
    assert seen["retries"] == 0


def test_search_failure_keeps_business_metrics_unavailable(monkeypatch):
    monkeypatch.setattr(
        daily,
        "readonly_call",
        lambda *args, **kwargs: {"status": 429, "ok": False, "data": {"error": "rate limited"}},
    )

    result = daily.search_performance(object(), [1, 2], datetime(2026, 8, 3, tzinfo=ZoneInfo("Asia/Shanghai")))

    assert result["status"] == 429
    assert result["returnedSkuCount"] is None
    assert result["missingSkuCount"] is None
    assert result["totals"] is None
    assert result["position"] is None


def test_ad_count_failure_does_not_fabricate_zero_or_http_200(monkeypatch):
    def fake_readonly_call(client, method, url, body=None, **kwargs):
        if url.endswith("/adv/v1/promotion/count"):
            return {"status": None, "ok": False, "data": {"error": "transport failure"}}
        return {"status": 200, "ok": True, "data": {}}

    monkeypatch.setattr(daily, "readonly_call", fake_readonly_call)

    result = daily.extended_readonly_metrics(
        object(), datetime(2026, 8, 3, tzinfo=ZoneInfo("Asia/Shanghai")), [1]
    )

    advertising = result["advertising"]
    assert advertising["countStatus"] is None
    assert advertising["campaigns"] is None
    assert advertising["statsStatus"] is None
    assert "计数不可用" in advertising["statsNote"]


def test_previous_snapshot_skips_newer_incomplete_capture(monkeypatch, tmp_path):
    incomplete = {
        "generatedAt": "2026-08-02T09:00:00+08:00",
        "dataComplete": False,
        "cards": None,
    }
    complete = {
        "generatedAt": "2026-08-01T09:00:00+08:00",
        "dataComplete": True,
        "cards": 187,
    }
    (tmp_path / "wb_daily_snapshot_20260802_090000.json").write_text(
        json.dumps(incomplete), encoding="utf-8"
    )
    valid_path = tmp_path / "wb_daily_snapshot_20260801_090000.json"
    valid_path.write_text(json.dumps(complete), encoding="utf-8")
    monkeypatch.setattr(daily, "OUT_DIR", tmp_path)

    result = daily.previous_daily_snapshot(datetime(2026, 8, 3).date())

    assert result["cards"] == 187
    assert result["_snapshotPath"] == str(valid_path)


def test_compare_value_does_not_subtract_null_baseline():
    assert daily.compare_value(5, {"orders": None}, "orders") == "5（—）"
