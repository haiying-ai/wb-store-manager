from __future__ import annotations

import csv
import hashlib
import io
import json
import posixpath
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Mapping, Sequence
from xml.etree import ElementTree
from zoneinfo import ZoneInfo


MOSCOW = ZoneInfo("Europe/Moscow")
_CNY_CENT = Decimal("0.01")
_CAPACITY = re.compile(r"^(\d+(?:\.\d+)?)(GB|G|TB|T)$", re.IGNORECASE)
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CELL_REFERENCE = re.compile(r"([A-Z]+)", re.IGNORECASE)
_SPECIFICATION_HEADERS = {
    "规格",
    "规格型号",
    "SPEC",
    "SPECIFICATION",
    "SKU",
    "PRODUCTKEY",
}
_RUB_COST_HEADERS = {
    "成本价格",
    "成本价",
    "卢布报价",
    "COST",
    "COSTRUB",
    "PURCHASECOSTRUB",
}
_CNY_COST_HEADERS = {
    "人民币报价",
    "人民币报价(¥)",
    "人民币报价(￥)",
    "COSTCNY",
    "PURCHASECOSTCNY",
    "成本CNY",
}


class CostQuoteError(ValueError):
    """Raised when a quote cannot be converted without guessing."""


@dataclass(frozen=True)
class ProductKey:
    product_key: str
    model: str
    capacity: str
    color: str


@dataclass(frozen=True)
class CostVersionRow:
    version_id: str
    product_key: str
    model: str
    capacity: str
    color: str
    specification: str
    cost_rub: Decimal
    rub_per_cny: Decimal
    cost_cny: Decimal
    effective_from: datetime
    source_path: str
    source_sheet: str
    source_row: int
    source_hash: str
    row_hash: str
    source_currency: str = "RUB"

    def as_ledger_record(self) -> dict[str, Any]:
        """Return the append-only shape accepted by FinanceLedger.

        The parser deliberately leaves ``effective_to`` open. Interval closure and
        overlap handling belong to the ledger, which has the existing history.
        """

        return {
            "sku": self.product_key,
            "effective_from": self.effective_from.isoformat(timespec="seconds"),
            "effective_to": None,
            "purchase_cost_cny": self.cost_cny,
            "quote_sha256": self.source_hash,
            "quote_row": self.source_row,
            "exchange_rate_rub_per_cny": self.rub_per_cny,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "costVersionId": self.version_id,
            "productKey": self.product_key,
            "model": self.model,
            "capacity": self.capacity,
            "color": self.color,
            "specification": self.specification,
            "sourceCurrency": self.source_currency,
            "costRub": _decimal_text(self.cost_rub),
            "exchangeRateRubPerCny": _decimal_text(self.rub_per_cny),
            "purchaseCostCny": _decimal_text(self.cost_cny),
            "effectiveFrom": self.effective_from.isoformat(timespec="seconds"),
            "effectiveTo": None,
            "sourcePath": self.source_path,
            "sourceSheet": self.source_sheet,
            "sourceRow": self.source_row,
            "sourceSha256": self.source_hash,
            "rowSha256": self.row_hash,
        }


@dataclass(frozen=True)
class _QuoteRow:
    specification: str
    source_cost: Any
    cost_rub_evidence: Any | None
    sheet: str
    row: int


def normalize_product_key(value: Any) -> str:
    """Return a case/separator-insensitive model-capacity-color key."""

    return parse_product_key(value).product_key


def parse_product_key(value: Any) -> ProductKey:
    original = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not original:
        raise CostQuoteError("Specification is empty")
    normalized = original.upper()
    normalized = re.sub(r"\bBULE\b", "BLUE", normalized)
    normalized = re.sub(r"\bGOID\b", "GOLD", normalized)
    normalized = re.sub(r"\bSLIVER\b", "SILVER", normalized)
    normalized = re.sub(r"(?<=\d)\s*(GB|G|TB|T)\b", r"\1", normalized)
    tokens = [token for token in re.split(r"[^A-Z0-9.]+", normalized) if token]
    if tokens and tokens[0].startswith("IPHONE") and tokens[0] != "IPHONE":
        suffix = tokens[0][len("IPHONE") :]
        if suffix:
            tokens = ["IPHONE", suffix, *tokens[1:]]

    capacity_index = -1
    capacity = ""
    for index, token in enumerate(tokens):
        match = _CAPACITY.fullmatch(token)
        if match:
            number, unit = match.groups()
            capacity = f"{number}{'TB' if unit.upper() in {'T', 'TB'} else 'GB'}"
            capacity_index = index
            break
    if capacity_index <= 0 or capacity_index >= len(tokens) - 1:
        raise CostQuoteError(
            f"Specification must contain model, capacity, and color: {original!r}"
        )

    model_tokens = tokens[:capacity_index]
    color_tokens = ["BLUE" if token == "BULE" else token for token in tokens[capacity_index + 1 :]]
    while color_tokens and re.fullmatch(r"(?:BV|V)\d+", color_tokens[-1]):
        color_tokens.pop()
    if model_tokens[0] == "IPHONE":
        model_suffix = "".join(model_tokens[1:])
        if not model_suffix:
            raise CostQuoteError(f"Specification has no iPhone model: {original!r}")
        model = f"IPHONE {model_suffix}"
    else:
        model = "".join(model_tokens)
    color = "".join(color_tokens)
    if not model or not color:
        raise CostQuoteError(
            f"Specification must contain model, capacity, and color: {original!r}"
        )
    return ProductKey(
        product_key=f"{model}-{capacity}-{color}",
        model=model,
        capacity=capacity,
        color=color,
    )


def parse_effective_from(value: date | datetime | str) -> datetime:
    """Normalize cost-effectivity timestamps to Europe/Moscow.

    A date without a time starts exactly at Moscow 00:00. Naive datetimes are
    interpreted in Moscow; aware datetimes are converted to Moscow.
    """

    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise CostQuoteError("effective_from is empty")
        try:
            if _DATE_ONLY.fullmatch(raw):
                parsed = datetime.combine(date.fromisoformat(raw), time.min)
            else:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CostQuoteError(f"Invalid effective_from: {value!r}") from exc
    else:
        raise CostQuoteError(f"Unsupported effective_from type: {type(value).__name__}")
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=MOSCOW)
    return parsed.astimezone(MOSCOW)


def parse_cost_quote(
    path: str | Path,
    *,
    effective_from: date | datetime | str,
    rub_per_cny: Decimal | str | int | float | None = None,
    source_currency: str = "RUB",
) -> list[CostVersionRow]:
    """Parse one JSON, CSV, or XLSX quote into append-only cost-version rows."""

    source = Path(path)
    if not source.is_file():
        raise CostQuoteError(f"Cost quote does not exist: {source}")
    currency = str(source_currency or "").strip().upper()
    if currency not in {"RUB", "CNY"}:
        raise CostQuoteError("source_currency must be explicitly RUB or CNY")
    rate = (
        _positive_decimal(rub_per_cny, "rub_per_cny")
        if rub_per_cny not in (None, "")
        else None
    )
    if currency == "RUB" and rate is None:
        raise CostQuoteError("rub_per_cny is required when source_currency is RUB")
    effective = parse_effective_from(effective_from)
    source_bytes = source.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    suffix = source.suffix.lower()
    if suffix == ".json":
        raw_rows = _read_json_rows(source_bytes, source.stem, currency)
    elif suffix in {".csv", ".tsv"}:
        raw_rows = _read_csv_rows(source_bytes, source.stem, suffix, currency)
    elif suffix == ".xlsx":
        raw_rows = _read_xlsx_rows(source, currency)
    else:
        raise CostQuoteError(f"Unsupported cost quote format: {source.suffix or '<none>'}")
    if not raw_rows:
        raise CostQuoteError(f"Cost quote has no usable rows: {source}")

    output: list[CostVersionRow] = []
    seen: dict[str, tuple[str, int]] = {}
    for raw in raw_rows:
        if raw.row <= 0:
            raise CostQuoteError(f"Source row must be positive in {raw.sheet!r}: {raw.row}")
        parsed_key = parse_product_key(raw.specification)
        if parsed_key.product_key in seen:
            previous = seen[parsed_key.product_key]
            raise CostQuoteError(
                f"Duplicate product key {parsed_key.product_key}: "
                f"{previous[0]} row {previous[1]} and {raw.sheet} row {raw.row}"
            )
        seen[parsed_key.product_key] = (raw.sheet, raw.row)
        source_cost = _positive_decimal(
            raw.source_cost, f"{currency} cost at {raw.sheet} row {raw.row}"
        )
        if currency == "CNY":
            cost_cny = source_cost.quantize(_CNY_CENT, rounding=ROUND_HALF_UP)
            if not _blank(raw.cost_rub_evidence):
                cost_rub = _positive_decimal(
                    raw.cost_rub_evidence,
                    f"RUB evidence at {raw.sheet} row {raw.row}",
                )
                row_rate = rate or (cost_rub / cost_cny)
            elif rate is not None:
                row_rate = rate
                cost_rub = (cost_cny * row_rate).quantize(
                    _CNY_CENT, rounding=ROUND_HALF_UP
                )
            else:
                raise CostQuoteError(
                    f"CNY source row {raw.sheet} {raw.row} requires RUB evidence "
                    "when rub_per_cny is omitted"
                )
            converted = (cost_rub / row_rate).quantize(
                _CNY_CENT, rounding=ROUND_HALF_UP
            )
            if converted != cost_cny:
                raise CostQuoteError(
                    f"CNY/RUB quote mismatch at {raw.sheet} row {raw.row}: "
                    f"{cost_cny} CNY != {cost_rub} RUB / {row_rate}"
                )
        else:
            assert rate is not None
            row_rate = rate
            cost_rub = source_cost
            cost_cny = (cost_rub / row_rate).quantize(
                _CNY_CENT, rounding=ROUND_HALF_UP
            )
        row_payload = {
            "schema": 1,
            "productKey": parsed_key.product_key,
            "sourceCurrency": currency,
            "sourceCost": _decimal_text(source_cost),
            "costRub": _decimal_text(cost_rub),
            "sourceSheet": raw.sheet,
            "sourceRow": raw.row,
        }
        row_hash = _json_hash(row_payload)
        version_payload = {
            "schema": 1,
            "rowSha256": row_hash,
            "effectiveFrom": effective.isoformat(timespec="seconds"),
            "rubPerCny": _decimal_text(row_rate),
            "costCny": _decimal_text(cost_cny),
        }
        version_id = f"costv_{_json_hash(version_payload)[:24]}"
        output.append(
            CostVersionRow(
                version_id=version_id,
                product_key=parsed_key.product_key,
                model=parsed_key.model,
                capacity=parsed_key.capacity,
                color=parsed_key.color,
                specification=unicodedata.normalize("NFKC", raw.specification).strip(),
                source_currency=currency,
                cost_rub=cost_rub,
                rub_per_cny=row_rate,
                cost_cny=cost_cny,
                effective_from=effective,
                source_path=str(source.resolve()),
                source_sheet=raw.sheet,
                source_row=raw.row,
                source_hash=source_hash,
                row_hash=row_hash,
            )
        )
    return output


def load_cost_versions(
    path: str | Path,
    *,
    effective_from: date | datetime | str,
    rub_per_cny: Decimal | str | int | float | None = None,
    source_currency: str = "RUB",
) -> list[CostVersionRow]:
    """Compatibility-friendly name for callers that load rows into a ledger."""

    return parse_cost_quote(
        path,
        effective_from=effective_from,
        rub_per_cny=rub_per_cny,
        source_currency=source_currency,
    )


def _read_json_rows(
    source: bytes, default_sheet: str, source_currency: str
) -> list[_QuoteRow]:
    payload = _decode_json(source)
    top_sheet = default_sheet
    if isinstance(payload, Mapping):
        top_sheet = str(payload.get("sheet") or payload.get("sourceSheet") or default_sheet).strip()
        records = payload.get("rows")
        if records is None:
            records = payload.get("records")
        if records is None and _mapping_value(payload, _SPECIFICATION_HEADERS) is not None:
            records = [payload]
    else:
        records = payload
    if not isinstance(records, list):
        raise CostQuoteError("JSON quote must be a row list or an object containing rows")
    rows: list[_QuoteRow] = []
    for index, item in enumerate(records, start=1):
        if not isinstance(item, Mapping):
            raise CostQuoteError(f"JSON quote row {index} is not an object")
        specification = _mapping_value(item, _SPECIFICATION_HEADERS)
        cost_headers = _CNY_COST_HEADERS if source_currency == "CNY" else _RUB_COST_HEADERS
        cost = _mapping_value(item, cost_headers)
        rub_evidence = _mapping_value(item, _RUB_COST_HEADERS)
        if _blank(specification) and _blank(cost):
            continue
        if _blank(specification) or _blank(cost):
            raise CostQuoteError(f"JSON quote row {index} must contain both specification and cost")
        sheet = str(item.get("sourceSheet") or item.get("sheet") or top_sheet).strip() or default_sheet
        row_value = (
            item.get("sourceRow")
            or item.get("excelRow")
            or item.get("rowNumber")
            or item.get("row")
            or index
        )
        try:
            row_number = int(row_value)
        except (TypeError, ValueError) as exc:
            raise CostQuoteError(f"Invalid JSON source row: {row_value!r}") from exc
        rows.append(
            _QuoteRow(
                str(specification).strip(), cost, rub_evidence, sheet, row_number
            )
        )
    return rows


def _decode_json(source: bytes) -> Any:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return json.loads(source.decode(encoding))
        except UnicodeDecodeError as exc:
            last_error = exc
        except json.JSONDecodeError as exc:
            raise CostQuoteError(f"Invalid JSON quote: {exc}") from exc
    raise CostQuoteError("JSON quote encoding is not UTF-8 or GB18030") from last_error


def _read_csv_rows(
    source: bytes, sheet: str, suffix: str, source_currency: str
) -> list[_QuoteRow]:
    text = _decode_text(source, "CSV")
    sample = text[:8192]
    if suffix == ".tsv":
        dialect: csv.Dialect | type[csv.excel] = csv.excel_tab
    else:
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
    table = list(csv.reader(io.StringIO(text), dialect=dialect))
    return _tabular_quote_rows(table, sheet, source_currency)


def _decode_text(source: bytes, label: str) -> str:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return source.decode(encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
    raise CostQuoteError(f"{label} encoding is not UTF-8 or GB18030") from last_error


def _read_xlsx_rows(path: Path, source_currency: str) -> list[_QuoteRow]:
    if not zipfile.is_zipfile(path):
        raise CostQuoteError(f"Invalid XLSX archive: {path}")
    try:
        with zipfile.ZipFile(path) as archive:
            shared_strings = _xlsx_shared_strings(archive)
            workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
            relationships = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            targets = {
                rel.attrib.get("Id", ""): rel.attrib.get("Target", "")
                for rel in relationships
                if _local_name(rel.tag) == "Relationship"
                and rel.attrib.get("TargetMode") != "External"
            }
            parsed: list[_QuoteRow] = []
            found_header = False
            relationship_key = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
            for sheet in workbook.iter():
                if _local_name(sheet.tag) != "sheet":
                    continue
                name = str(sheet.attrib.get("name") or "").strip() or "Sheet"
                relationship_id = sheet.attrib.get(relationship_key)
                target = targets.get(str(relationship_id or ""), "")
                if not target:
                    raise CostQuoteError(f"XLSX sheet relationship is missing for {name!r}")
                sheet_path = _xlsx_target(target)
                worksheet = ElementTree.fromstring(archive.read(sheet_path))
                table: list[tuple[int, list[str]]] = []
                for ordinal, row in enumerate(
                    (element for element in worksheet.iter() if _local_name(element.tag) == "row"),
                    start=1,
                ):
                    try:
                        row_number = int(row.attrib.get("r") or ordinal)
                    except ValueError as exc:
                        raise CostQuoteError(f"Invalid XLSX row number in {name!r}") from exc
                    values: list[str] = []
                    for cell in (element for element in row if _local_name(element.tag) == "c"):
                        column = _xlsx_column_index(cell.attrib.get("r", ""))
                        while len(values) < column:
                            values.append("")
                        values[column - 1] = _xlsx_cell_value(cell, shared_strings)
                    table.append((row_number, values))
                rows, has_header = _tabular_quote_rows_numbered(
                    table, name, source_currency
                )
                found_header = found_header or has_header
                parsed.extend(rows)
            if not found_header:
                raise CostQuoteError(
                    f"XLSX quote has no sheet with 规格 and {source_currency} cost columns"
                )
            return parsed
    except (KeyError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise CostQuoteError(f"Malformed XLSX quote: {path}") from exc


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    except ElementTree.ParseError as exc:
        raise CostQuoteError("Malformed XLSX shared strings") from exc
    values: list[str] = []
    for item in root.iter():
        if _local_name(item.tag) == "si":
            values.append(
                "".join(text.text or "" for text in item.iter() if _local_name(text.tag) == "t")
            )
    return values


def _xlsx_cell_value(cell: ElementTree.Element, shared_strings: Sequence[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(
            text.text or "" for text in cell.iter() if _local_name(text.tag) == "t"
        ).strip()
    value = next((element for element in cell if _local_name(element.tag) == "v"), None)
    if value is None or value.text is None:
        return ""
    raw = value.text.strip()
    if cell_type == "s":
        try:
            return shared_strings[int(raw)].strip()
        except (ValueError, IndexError) as exc:
            raise CostQuoteError(f"Invalid XLSX shared-string index: {raw!r}") from exc
    return raw


def _xlsx_target(target: str) -> str:
    normalized = target.replace("\\", "/")
    if normalized.startswith("/"):
        return normalized.lstrip("/")
    if normalized.startswith("xl/"):
        return posixpath.normpath(normalized)
    return posixpath.normpath(posixpath.join("xl", normalized))


def _xlsx_column_index(reference: str) -> int:
    match = _CELL_REFERENCE.match(reference)
    if not match:
        raise CostQuoteError(f"Invalid XLSX cell reference: {reference!r}")
    index = 0
    for character in match.group(1).upper():
        index = index * 26 + ord(character) - ord("A") + 1
    return index


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _tabular_quote_rows(
    table: Sequence[Sequence[Any]], sheet: str, source_currency: str
) -> list[_QuoteRow]:
    numbered = [(index, list(row)) for index, row in enumerate(table, start=1)]
    rows, found_header = _tabular_quote_rows_numbered(
        numbered, sheet, source_currency
    )
    if not found_header:
        raise CostQuoteError(f"{sheet} has no 规格 and {source_currency} cost columns")
    return rows


def _tabular_quote_rows_numbered(
    table: Sequence[tuple[int, Sequence[Any]]], sheet: str, source_currency: str
) -> tuple[list[_QuoteRow], bool]:
    header_position = -1
    specification_column = -1
    cost_column = -1
    rub_evidence_column = -1
    cost_headers = _CNY_COST_HEADERS if source_currency == "CNY" else _RUB_COST_HEADERS
    for position, (_, row) in enumerate(table[:50]):
        for column, value in enumerate(row):
            normalized = _header(value)
            if normalized in _SPECIFICATION_HEADERS:
                specification_column = column
            if normalized in cost_headers:
                cost_column = column
            if normalized in _RUB_COST_HEADERS:
                rub_evidence_column = column
        if specification_column >= 0 and cost_column >= 0:
            header_position = position
            break
        specification_column = -1
        cost_column = -1
        rub_evidence_column = -1
    if header_position < 0:
        return [], False

    rows: list[_QuoteRow] = []
    for row_number, row in table[header_position + 1 :]:
        specification = _cell(row, specification_column)
        cost = _cell(row, cost_column)
        rub_evidence = _cell(row, rub_evidence_column)
        # Real supplier workbooks may contain several product sections in one
        # sheet, separated by a one-cell brand label, contact footer, and a
        # repeated header. Those structural rows are not partial cost rows.
        if (
            _header(specification) in _SPECIFICATION_HEADERS
            and _header(cost) in cost_headers
        ):
            continue
        if _blank(specification) and _blank(cost):
            continue
        if _blank(specification) or _blank(cost):
            non_blank_cells = sum(not _blank(value) for value in row)
            if _blank(cost) and non_blank_cells <= 2:
                # Section titles such as ``SAMSUNG`` are allowed; an otherwise
                # populated product row with a missing cost remains fail-closed.
                continue
            raise CostQuoteError(
                f"{sheet} row {row_number} must contain both 规格 and 成本价格"
            )
        rows.append(
            _QuoteRow(
                str(specification).strip(),
                cost,
                rub_evidence,
                sheet,
                int(row_number),
            )
        )
    return rows, True


def _cell(row: Sequence[Any], index: int) -> Any:
    return row[index] if 0 <= index < len(row) else ""


def _mapping_value(mapping: Mapping[str, Any], headers: set[str]) -> Any | None:
    found: list[Any] = []
    for key, value in mapping.items():
        if _header(key) in headers and not _blank(value):
            found.append(value)
    if not found:
        return None
    normalized = {str(value).strip() for value in found}
    if len(normalized) > 1:
        raise CostQuoteError(f"Conflicting values for quote field: {sorted(normalized)!r}")
    return found[0]


def _header(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip().upper()
    return re.sub(r"[\s_\-/]+", "", normalized)


def _blank(value: Any) -> bool:
    return value is None or not str(value).strip()


def _positive_decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise CostQuoteError(f"{label} must be a positive decimal")
    raw = unicodedata.normalize("NFKC", str(value or "")).strip().upper()
    raw = raw.replace("\u00a0", "").replace(" ", "")
    for marker in ("RUB", "РУБ.", "РУБ", "₽", "CNY", "¥"):
        raw = raw.replace(marker, "")
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        left, right = raw.rsplit(",", 1)
        raw = left + right if len(right) == 3 and left.replace("-", "").isdigit() else left + "." + right
    try:
        result = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise CostQuoteError(f"Invalid {label}: {value!r}") from exc
    if not result.is_finite() or result <= 0:
        raise CostQuoteError(f"{label} must be a finite positive decimal: {value!r}")
    return result


def _decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _json_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "CostQuoteError",
    "CostVersionRow",
    "ProductKey",
    "load_cost_versions",
    "normalize_product_key",
    "parse_cost_quote",
    "parse_effective_from",
    "parse_product_key",
]
