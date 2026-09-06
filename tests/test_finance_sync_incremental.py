from datetime import date

from wb_manager.finance_sync import _coverage_changed, _incremental_source_start


class _Ledger:
    def __init__(self, watermark=None):
        self.watermark = watermark

    def get_watermark(self, _store, _source):
        return self.watermark


def test_statistics_incremental_cursor_replays_one_boundary_day():
    ledger = _Ledger("2026-08-31T23:59:59+03:00")
    assert _incremental_source_start(
        ledger, "store", "statistics_sales", date(2026, 8, 20)
    ) == date(2026, 8, 30)


def test_statistics_incremental_cursor_never_moves_before_floor_or_bad_watermark():
    assert _incremental_source_start(
        _Ledger("2026-08-01T00:00:00+03:00"),
        "store",
        "statistics_orders",
        date(2026, 8, 20),
    ) == date(2026, 8, 20)
    assert _incremental_source_start(
        _Ledger("not-a-date"), "store", "statistics_orders", date(2026, 8, 20)
    ) == date(2026, 8, 20)


def test_coverage_snapshot_change_detects_status_or_hash_only():
    previous = {
        "coverage_status": "complete",
        "snapshot_hash": "a" * 64,
        "consecutive_complete_hash_count": 2,
    }
    assert not _coverage_changed(previous, True, "a" * 64)
    assert _coverage_changed(previous, True, "b" * 64)
    assert _coverage_changed(previous, False, "a" * 64)
    assert _coverage_changed(None, True, "a" * 64)


def test_coverage_stability_is_not_a_sync_change_signal():
    previous = {
        "coverage_status": "complete",
        "snapshot_hash": "a" * 64,
        "period_start": "2026-08-01T00:00:00+03:00",
        "period_end": "2026-08-31T23:59:59+03:00",
        "consecutive_complete_hash_count": 1,
    }
    assert not _coverage_changed(
        previous,
        True,
        "a" * 64,
    )


def test_same_window_hash_change_detects_source_retraction_but_window_shift_does_not():
    previous = {
        "coverage_status": "complete",
        "snapshot_hash": "a" * 64,
        "period_start": "2026-08-01T00:00:00+03:00",
        "period_end": "2026-08-31T23:59:59+03:00",
    }
    assert _coverage_changed(
        previous,
        True,
        "b" * 64,
        content_changed=False,
        period_start="2026-08-01T00:00:00+03:00",
        period_end="2026-08-31T23:59:59+03:00",
    )
    assert not _coverage_changed(
        previous,
        True,
        "b" * 64,
        content_changed=False,
        period_start="2026-08-02T00:00:00+03:00",
        period_end="2026-09-01T23:59:59+03:00",
    )
