from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WORKBOOK_BUILDER = ROOT / "wb_manager" / "assets" / "build_finance_workbook.mjs"


@dataclass(frozen=True)
class ReportArtifacts:
    report_id: str
    json_path: Path
    markdown_path: Path
    workbook_path: Path
    verification_path: Path
    preview_dir: Path


def write_report_artifacts(payload: dict[str, Any], output_dir: Path) -> ReportArtifacts:
    """Write immutable JSON, Chinese Markdown, and verified XLSX report artifacts."""
    report_id = _safe_report_id(str(payload.get("reportId") or ""))
    if not report_id:
        raise ValueError("report payload requires reportId")
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{report_id}.json"
    markdown_path = output_dir / f"{report_id}.md"
    workbook_path = output_dir / f"{report_id}.xlsx"
    verification_path = output_dir / f"{report_id}.verification.json"
    preview_dir = output_dir / ".previews" / report_id
    for path in (json_path, markdown_path, workbook_path, verification_path):
        if path.exists():
            raise FileExistsError(f"Report artifact is immutable and already exists: {path}")
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(build_owner_markdown(payload), encoding="utf-8")
    node = os.getenv("WB_FINANCE_NODE", "").strip() or shutil.which("node")
    if not node:
        raise RuntimeError("Node.js is required to build the finance workbook")
    command = [
        node,
        str(WORKBOOK_BUILDER),
        "--input",
        str(json_path),
        "--output",
        str(workbook_path),
        "--preview-dir",
        str(preview_dir),
        "--verification",
        str(verification_path),
    ]
    proc = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(
            "Finance workbook build failed: "
            + (proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}")
        )
    verification = json.loads(verification_path.read_text(encoding="utf-8"))
    if verification.get("status") != "PASS":
        raise RuntimeError(f"Finance workbook verification failed: {verification}")
    return ReportArtifacts(
        report_id=report_id,
        json_path=json_path,
        markdown_path=markdown_path,
        workbook_path=workbook_path,
        verification_path=verification_path,
        preview_dir=preview_dir,
    )


def build_owner_markdown(payload: dict[str, Any]) -> str:
    period = payload.get("period") or {}
    totals = payload.get("totals") or {}
    status = str(payload.get("status") or "PENDING_DATA")
    kind_labels = {"bootstrap": "首次累计", "weekly": "周度", "monthly": "月度"}
    kind = kind_labels.get(str(payload.get("kind") or ""), str(payload.get("kind") or "财务"))
    lines = [
        f"# WB 双店{kind}财务报告",
        "",
        "## 老板结论",
        "",
        f"- 报告状态：**{status}**。",
        f"- 财务期间：{period.get('from')} 至 {period.get('to')}（{period.get('timezone', 'Europe/Moscow')}）。",
        f"- 营业额：{_money(totals.get('netTurnoverCny'))} CNY。",
        f"- 采购成本：{_money(totals.get('purchaseCostCny'))} CNY。",
        f"- 平台及履约费用：{_money(totals.get('platformAndOperationsCostCny'))} CNY。",
        f"- 代运营费：{_money(totals.get('managedOperationsCny'))} CNY。",
        f"- 实际退货物流费：{_money(totals.get('returnLogisticsCny'))} CNY。",
        f"- 计提广告费：{_money(totals.get('advertisingProvisionCny'))} CNY。",
        f"- 老板实际利润：**{_money(totals.get('ownerProfitCny'))} CNY**。",
        "",
        "> 实际广告消耗仅用于对账，不会在固定 3% 广告与退货资金池之外再次扣减。退货物流费来自平台财务流水，不使用 8/16 CNY 默认值。",
        "",
        "## 店铺汇总",
        "",
        "| 店铺 | 完成/净订单 | 营业额 CNY | 采购成本 CNY | 代运营费 CNY | 退货费 CNY | 计提广告费 CNY | 老板利润 CNY |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in payload.get("stores") or []:
        lines.append(
            "| {alias} | {orders} | {turnover} | {purchase} | {managed} | {returns} | {ads} | {profit} |".format(
                alias=row.get("storeAlias"),
                orders=row.get("orderCount", 0),
                turnover=_money(row.get("netTurnoverCny")),
                purchase=_money(row.get("purchaseCostCny")),
                managed=_money(row.get("managedOperationsCny")),
                returns=_money(row.get("returnLogisticsCny")),
                ads=_money(row.get("advertisingProvisionCny")),
                profit=_money(row.get("ownerProfitCny")),
            )
        )
    blockers = payload.get("blockers") or []
    lines.extend(["", "## 数据完整性", ""])
    if blockers:
        lines.append("以下问题阻止报告封账：")
        lines.append("")
        lines.extend(f"- {item.get('code', 'BLOCKER')}：{item.get('message', item)}" for item in blockers)
    else:
        lines.append("- 财务流水、成本版本、退货实际扣款和广告对账数据均已通过完整性校验。")
    lines.extend([
        "",
        "## 口径与来源",
        "",
        f"- 财务策略：`{(payload.get('policy') or {}).get('reportingPolicyId')}`。",
        f"- 价格策略：`{(payload.get('policy') or {}).get('pricingPolicyId')}`。",
        "- 采购成本按订单创建时间匹配有效期成本版本；调价节点之前使用旧成本，节点时刻起使用新成本。",
        "- 成交价、平台扣费和退货物流以 WB 财务证据为准；非采购成本百分比使用最新有效模板。",
        "- 财务 API：https://dev.wildberries.ru/docs/openapi/financial-reports-and-accounting",
        "- 推广 API：https://dev.wildberries.ru/ru/openapi/promotion",
        "",
    ])
    return "\n".join(lines)


def _safe_report_id(value: str) -> str:
    return "".join(char for char in value if char.isalnum() or char in {"-", "_"}).strip("-_")


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def _money(value: Any) -> str:
    try:
        return f"{Decimal(str(value or 0)).quantize(Decimal('0.01')):,.2f}"
    except Exception:
        return "0.00"
