from __future__ import annotations

import json
import shutil
import zipfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from wb_manager.finance_costs import (
    CostQuoteError,
    normalize_product_key,
    parse_cost_quote,
    parse_effective_from,
)


def _inline_cell(reference: str, value: str) -> str:
    return (
        f'<c r="{reference}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
    )


def _number_cell(reference: str, value: str) -> str:
    return f'<c r="{reference}"><v>{escape(value)}</v></c>'


def _write_quote_xlsx(
    path: Path,
    *,
    cost_header: str = "成本价格",
    cost_value: str = "22500",
    specification: str = "iPhone 13-128gb-Black",
) -> None:
    worksheet = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1">{_inline_cell("B1", "7月4日报价单")}</row>
    <row r="2">
      {_inline_cell("B2", "型号")}
      {_inline_cell("C2", "内存")}
      {_inline_cell("D2", "颜色")}
      {_inline_cell("E2", "规格")}
      {_inline_cell("F2", cost_header)}
    </row>
    <row r="3">
      {_inline_cell("B3", "13")}
      {_inline_cell("C3", "128G")}
      {_inline_cell("D3", "黑色")}
      {_inline_cell("E3", specification)}
      {_number_cell("F3", cost_value)}
    </row>
  </sheetData>
</worksheet>"""
    workbook = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="手机报价" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
    relationships = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)


def _write_sectioned_quote_xlsx(path: Path) -> None:
    worksheet = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1">{_inline_cell("E1", "IPHONE")}</row>
    <row r="2">{_inline_cell("E2", "规格")}{_inline_cell("F2", "成本价格")}</row>
    <row r="3">{_inline_cell("E3", "iPhone 13-128gb-Black")}{_number_cell("F3", "22500")}</row>
    <row r="4">{_inline_cell("E4", "SAMSUNG")}</row>
    <row r="5">{_inline_cell("E5", "规格")}{_inline_cell("F5", "成本价格")}</row>
    <row r="6">{_inline_cell("E6", "Samsung S22 Ultra-1tb-black")}{_number_cell("F6", "32000")}</row>
    <row r="7">{_inline_cell("B7", "电话")}{_inline_cell("C7", "8(000)000-00-00")}</row>
  </sheetData>
</worksheet>"""
    workbook = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="手机报价" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
    relationships = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)


def test_xlsx_parses_realistic_headers_and_emits_ledger_row(tmp_path: Path) -> None:
    quote = tmp_path / "7月4日报价单.xlsx"
    _write_quote_xlsx(quote)

    rows = parse_cost_quote(
        quote,
        effective_from="2026-07-04",
        rub_per_cny=Decimal("11.5"),
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.product_key == "IPHONE 13-128GB-BLACK"
    assert (row.model, row.capacity, row.color) == ("IPHONE 13", "128GB", "BLACK")
    assert row.cost_rub == Decimal("22500")
    assert row.rub_per_cny == Decimal("11.5")
    assert row.cost_cny == Decimal("1956.52")
    assert row.source_sheet == "手机报价"
    assert row.source_row == 3
    assert len(row.source_hash) == 64
    assert len(row.row_hash) == 64
    assert row.version_id.startswith("costv_")
    assert row.as_ledger_record() == {
        "sku": "IPHONE 13-128GB-BLACK",
        "effective_from": "2026-07-04T00:00:00+03:00",
        "effective_to": None,
        "purchase_cost_cny": Decimal("1956.52"),
        "quote_sha256": row.source_hash,
        "quote_row": 3,
        "exchange_rate_rub_per_cny": Decimal("11.5"),
    }


def test_xlsx_supports_repeated_brand_sections_and_contact_footers(tmp_path: Path) -> None:
    quote = tmp_path / "sectioned.xlsx"
    _write_sectioned_quote_xlsx(quote)

    rows = parse_cost_quote(
        quote, effective_from="2026-07-04", rub_per_cny="11.5"
    )

    assert [row.product_key for row in rows] == [
        "IPHONE 13-128GB-BLACK",
        "SAMSUNGS22ULTRA-1TB-BLACK",
    ]
    assert [row.source_row for row in rows] == [3, 6]


def test_product_key_normalization_is_case_separator_safe_and_fixes_bule() -> None:
    assert normalize_product_key("iPhone 13-128gb-Black") == normalize_product_key(
        "IPHONE 13-128GB-BLACK"
    )
    assert normalize_product_key("iPhone 13-128gb-BULE") == "IPHONE 13-128GB-BLUE"
    assert normalize_product_key("iPhone 13 Pro Max / 1 tb / Deep Blue") == (
        "IPHONE 13PROMAX-1TB-DEEPBLUE"
    )
    assert normalize_product_key("IPHONE 16PM-256GB-WHITE-BV5") == (
        "IPHONE 16PM-256GB-WHITE"
    )
    assert normalize_product_key("IPHONE 16PRO-256GB-GOID-V3") == (
        "IPHONE 16PRO-256GB-GOLD"
    )
    assert normalize_product_key("SAMSUNG FOLD 6-1TB-SLIVER") == (
        "SAMSUNGFOLD6-1TB-SILVER"
    )


def test_date_without_time_starts_at_moscow_midnight() -> None:
    effective = parse_effective_from("2026-07-04")
    assert effective.isoformat(timespec="seconds") == "2026-07-04T00:00:00+03:00"
    assert effective.astimezone(timezone.utc) == datetime(
        2026, 7, 3, 21, 0, tzinfo=timezone.utc
    )
    assert parse_effective_from("2026-07-03T21:00:00Z") == effective


@pytest.mark.parametrize("extension", ["json", "csv"])
def test_json_and_csv_are_supported_with_decimal_conversion(
    tmp_path: Path, extension: str
) -> None:
    quote = tmp_path / f"quote.{extension}"
    if extension == "json":
        quote.write_text(
            json.dumps(
                {
                    "sheet": "JSON报价",
                    "rows": [
                        {
                            "excelRow": 7,
                            "规格": "iPhone 13-128gb-BULE",
                            "成本价格": "23 000 ₽",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    else:
        quote.write_text(
            "说明,内部报价\n规格,成本价格\niPhone 13-128gb-BULE,23000\n",
            encoding="utf-8-sig",
        )

    row = parse_cost_quote(
        quote, effective_from="2026-07-04", rub_per_cny="11.5"
    )[0]

    assert row.product_key == "IPHONE 13-128GB-BLUE"
    assert row.cost_rub == Decimal("23000")
    assert row.cost_cny == Decimal("2000.00")
    assert row.source_row == (7 if extension == "json" else 3)


def test_hashes_and_version_id_are_stable_when_file_is_moved(tmp_path: Path) -> None:
    quote = tmp_path / "quote.xlsx"
    copied = tmp_path / "moved.xlsx"
    _write_quote_xlsx(quote)
    shutil.copyfile(quote, copied)

    first = parse_cost_quote(
        quote, effective_from="2026-07-04", rub_per_cny="11.5"
    )[0]
    second = parse_cost_quote(
        copied, effective_from="2026-07-04", rub_per_cny="11.5"
    )[0]

    assert first.source_hash == second.source_hash
    assert first.row_hash == second.row_hash
    assert first.version_id == second.version_id
    assert first.source_path != second.source_path


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("missing_header", "22500"),
        ("bad_cost", "free"),
        ("incomplete_spec", "22500"),
    ],
)
def test_bad_xlsx_data_fails_closed(tmp_path: Path, kind: str, value: str) -> None:
    quote = tmp_path / f"{kind}.xlsx"
    _write_quote_xlsx(
        quote,
        cost_header="售价" if kind == "missing_header" else "成本价格",
        cost_value=value,
        specification=(
            "iPhone 13-128gb" if kind == "incomplete_spec" else "iPhone 13-128gb-Black"
        ),
    )

    with pytest.raises(CostQuoteError):
        parse_cost_quote(
            quote, effective_from="2026-07-04", rub_per_cny="11.5"
        )


def test_bad_exchange_rate_and_duplicate_product_keys_fail_closed(tmp_path: Path) -> None:
    quote = tmp_path / "quote.json"
    quote.write_text(
        json.dumps(
            [
                {"规格": "iPhone 13-128gb-Black", "成本价格": 22500},
                {"规格": "IPHONE 13-128GB-BLACK", "成本价格": 22500},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(CostQuoteError, match="rub_per_cny"):
        parse_cost_quote(quote, effective_from="2026-07-04", rub_per_cny="0")
    with pytest.raises(CostQuoteError, match="Duplicate product key"):
        parse_cost_quote(quote, effective_from="2026-07-04", rub_per_cny="11.5")
