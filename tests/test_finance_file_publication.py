from __future__ import annotations

import os
from pathlib import Path

import pytest

from wb_manager.finance_reporting import _publish_file_no_clobber


def test_publish_file_copies_bytes_into_a_distinct_destination_file(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "staging"
    published = tmp_path / "published"
    staging.mkdir()
    published.mkdir()
    source = staging / "report.xlsx"
    target = published / "report.xlsx"
    source.write_bytes(b"PK\x03\x04test-workbook-bytes")

    _publish_file_no_clobber(source, target)

    assert target.read_bytes() == source.read_bytes()
    assert not os.path.samefile(source, target)


def test_publish_file_preserves_no_clobber_semantics(tmp_path: Path) -> None:
    source = tmp_path / "source.xlsx"
    target = tmp_path / "target.xlsx"
    source.write_bytes(b"new workbook")
    target.write_bytes(b"existing workbook")

    with pytest.raises(FileExistsError):
        _publish_file_no_clobber(source, target)

    assert target.read_bytes() == b"existing workbook"
