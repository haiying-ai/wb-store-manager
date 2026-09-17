"""A checkout must not accrue fees from a missing private policy."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from wb_manager import finance_running_adapter as adapter


def test_missing_approved_agency_policy_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "AGENCY_FEE_POLICY_PATH", tmp_path / "missing-policy.json")
    service = SimpleNamespace(repo_root=tmp_path)
    with pytest.raises(ValueError, match="Invalid approved agency withdrawal fee policy"):
        adapter._agency_fee_policy(service)
