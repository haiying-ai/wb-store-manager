import json

from tools.wb_daily_report_render import render_multi, render_store


def snapshot(alias, when, views):
    return {
        "storeAlias": alias, "generatedAt": when, "dataComplete": True,
        "endpoints": {key: {"status": 200, "ok": True} for key in ("content", "salesFunnel", "warehouses")},
        "totals": {"views": views},
    }


def test_multi_uses_independent_store_baselines(tmp_path):
    archive = tmp_path / "multi_store"
    runs = []
    for alias, before, current in (("owner-store", 7420, 7601), ("new-store", 11547, 11687)):
        directory = archive / alias
        directory.mkdir(parents=True)
        prior = snapshot(alias, "2026-09-07T09:00:00+08:00", before)
        (directory / "wb_daily_snapshot_20260907_090000.json").write_text(json.dumps(prior), encoding="utf-8")
        runs.append({"alias": alias, "result": snapshot(alias, "2026-09-08T09:00:00+08:00", current)})
    text = render_multi(runs, "20260908_090000", tmp_path, archive)
    assert "| 浏览 | 7601 | 7420 | +181 | HTTP 200 |" in text
    assert "| 浏览 | 11687 | 11547 | +140 | HTTP 200 |" in text
    assert text.count("\n## ") == 10


def test_failed_sources_ignore_stale_zero_metrics():
    prior = snapshot("owner-store", "2026-09-07T09:00:00+08:00", 7420)
    prior["extendedReadOnly"] = {"questions": {"status": 200, "ok": True, "unanswered": 2}}
    current = snapshot("owner-store", "2026-09-08T09:00:00+08:00", 0)
    current["endpoints"]["salesFunnel"] = {"status": 503, "ok": False}
    current["extendedReadOnly"] = {"questions": {"status": 429, "ok": False, "unanswered": 0}}
    current["searchPerformance"] = {"status": 403, "ok": False, "totals": {"orders": 0}}
    text = render_store(current, prior, "owner-store", "", "")
    assert "| 浏览 | 不可用 | 7420 | 不可比 | HTTP 503 |" in text
    assert "| 未回复问题返回条数 | 不可用 | 2 | 不可比 | HTTP 429 |" in text
    assert "HTTP 403" in text
    assert "| 搜索订单 | 不可用 / 不可用 / 不可比 |" in text
    assert "HTTP None" not in text
