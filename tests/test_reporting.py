from wb_manager.reporting import build_summary


def test_build_summary_handles_empty_snapshot():
    text = build_summary({})
    assert "Product cards" in text
    assert "Advertising campaigns" in text
