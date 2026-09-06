from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
WORKBOOK_BUILDER = ROOT / "wb_manager" / "assets" / "build_finance_workbook.mjs"
_PREVIEW_SHEET_COUNT = 7
_VALID_WORKBOOK_STATUSES = {"FINAL", "PENDING_DATA"}
_REQUIRED_VERIFICATION_KEYS = {
    "status",
    "formulaErrorCount",
    "renderedSheets",
    "reportId",
    "workbookStatus",
    "checkFailures",
    "consistencyFailures",
}


@dataclass(frozen=True)
class ReportArtifacts:
    report_id: str
    json_path: Path
    markdown_path: Path
    workbook_path: Path
    verification_path: Path
    preview_dir: Path


def write_report_artifacts(
    payload: dict[str, Any],
    output_dir: Path,
    *,
    repair_incomplete: bool = False,
) -> ReportArtifacts:
    """Write immutable JSON, Chinese Markdown, and verified XLSX report artifacts."""
    payload_report_id = str(payload.get("reportId") or "")
    report_id = _safe_report_id(payload_report_id)
    if not report_id:
        raise ValueError("report payload requires reportId")
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{report_id}.json"
    markdown_path = output_dir / f"{report_id}.md"
    workbook_path = output_dir / f"{report_id}.xlsx"
    verification_path = output_dir / f"{report_id}.verification.json"
    inspection_path = output_dir / f"{report_id}.xlsx.inspect.ndjson"
    preview_dir = output_dir / ".previews" / report_id
    final_files = (
        json_path,
        markdown_path,
        workbook_path,
        verification_path,
        inspection_path,
    )
    # JSON is the completion marker.  A normal caller may never overwrite it;
    # the finance service can opt into repairing a set that fails the strict
    # completeness check below.
    if json_path.exists() and not repair_incomplete:
        raise FileExistsError(
            f"Report artifact is immutable and already exists: {json_path}"
        )

    staging_root = output_dir / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(
        tempfile.mkdtemp(prefix=f"{report_id}-", dir=str(staging_root))
    )
    staged_json = staging_dir / f"{report_id}.json"
    staged_markdown = staging_dir / f"{report_id}.md"
    staged_workbook = staging_dir / f"{report_id}.xlsx"
    staged_verification = staging_dir / f"{report_id}.verification.json"
    staged_inspection = staging_dir / f"{report_id}.xlsx.inspect.ndjson"
    staged_previews = staging_dir / "previews"
    publish_lock = staging_root / f"{report_id}.publish.lock"
    lock_fd: int | None = None
    try:
        staged_json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default)
            + "\n",
            encoding="utf-8",
        )
        staged_markdown.write_text(build_owner_markdown(payload), encoding="utf-8")
        node = os.getenv("WB_FINANCE_NODE", "").strip() or shutil.which("node")
        if not node:
            raise RuntimeError("Node.js is required to build the finance workbook")
        command = [
            node,
            str(WORKBOOK_BUILDER),
            "--input",
            str(staged_json),
            "--output",
            str(staged_workbook),
            "--preview-dir",
            str(staged_previews),
            "--verification",
            str(staged_verification),
        ]
        proc = subprocess.run(
            command, cwd=ROOT, text=True, capture_output=True, timeout=180
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "Finance workbook build failed: "
                + (
                    proc.stderr.strip()
                    or proc.stdout.strip()
                    or f"exit {proc.returncode}"
                )
            )
        verification = json.loads(staged_verification.read_text(encoding="utf-8"))
        rendered_previews = _validate_report_verification(
            verification,
            payload=payload,
            workbook_path=staged_workbook,
            preview_dir=staged_previews,
        )
        published_verification = dict(verification)
        published_verification["renderedSheets"] = [
            str(preview_dir / path.name) for path in rendered_previews
        ]
        staged_verification.write_text(
            json.dumps(published_verification, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        try:
            lock_fd = os.open(
                publish_lock,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError as exc:
            raise FileExistsError(
                f"Report artifact publication is already in progress: {report_id}"
            ) from exc
        if _publication_targets_exist(final_files, preview_dir):
            if _existing_publication_is_valid(
                report_id,
                json_path=json_path,
                markdown_path=markdown_path,
                workbook_path=workbook_path,
                verification_path=verification_path,
                preview_dir=preview_dir,
            ):
                raise FileExistsError(
                    f"Report artifact is immutable and already exists: {json_path}"
                )
            if json_path.exists() and not repair_incomplete:
                raise FileExistsError(
                    f"Report artifact is immutable and already exists: {json_path}"
                )
            _remove_incomplete_publication(final_files, preview_dir)

        # Publish only after every staged artifact has passed verification.
        # Each destination is created as a fresh file so Windows applies the
        # final directory's inherited ACL.  Reusing the staged file object via
        # a hard link would also reuse an artifact runtime's protected DACL and
        # can make an otherwise valid workbook unreadable to desktop Excel.
        # The JSON payload remains the sole completion marker and is copied last.
        _publish_file_no_clobber(staged_markdown, markdown_path)
        _publish_file_no_clobber(staged_workbook, workbook_path)
        _publish_directory_no_clobber(staged_previews, preview_dir)
        _publish_file_no_clobber(staged_verification, verification_path)
        if staged_inspection.exists():
            _publish_file_no_clobber(staged_inspection, inspection_path)
        _assert_published_support_files(
            markdown_path,
            workbook_path,
            verification_path,
            preview_dir,
        )
        _publish_file_no_clobber(staged_json, json_path)
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
            publish_lock.unlink(missing_ok=True)
        # Never remove formal paths here: another concurrent process may have
        # won publication.  This invocation owns only its unique staging tree.
        shutil.rmtree(staging_dir, ignore_errors=True)
        try:
            staging_root.rmdir()
        except OSError:
            pass
    return ReportArtifacts(
        report_id=report_id,
        json_path=json_path,
        markdown_path=markdown_path,
        workbook_path=workbook_path,
        verification_path=verification_path,
        preview_dir=preview_dir,
    )


def _validate_report_verification(
    verification: Any,
    *,
    payload: Mapping[str, Any],
    workbook_path: Path,
    preview_dir: Path,
) -> list[Path]:
    errors: list[str] = []
    if not isinstance(verification, Mapping):
        raise RuntimeError("Finance workbook verification failed: result is not an object")

    missing = sorted(_REQUIRED_VERIFICATION_KEYS - set(verification))
    if missing:
        errors.append("missing keys: " + ", ".join(missing))
    if verification.get("status") != "PASS":
        errors.append("status must be PASS")
    if verification.get("reportId") != payload.get("reportId"):
        errors.append("reportId does not match the report payload")

    formula_error_count = verification.get("formulaErrorCount")
    if type(formula_error_count) is not int or formula_error_count != 0:
        errors.append("formulaErrorCount must be the integer 0")

    payload_status = str(payload.get("status") or "").upper()
    expected_workbook_status = {
        "FINAL": "FINAL",
        "RESTATED": "FINAL",
        "PENDING_DATA": "PENDING_DATA",
    }.get(payload_status)
    workbook_status = verification.get("workbookStatus")
    if workbook_status not in _VALID_WORKBOOK_STATUSES:
        errors.append("workbookStatus must be FINAL or PENDING_DATA")
    elif expected_workbook_status is None:
        errors.append(f"unsupported report status: {payload_status or 'missing'}")
    elif workbook_status != expected_workbook_status:
        errors.append(
            f"workbookStatus {workbook_status} does not match report status {payload_status}"
        )

    for key in ("checkFailures", "consistencyFailures"):
        value = verification.get(key)
        if not isinstance(value, list):
            errors.append(f"{key} must be a list")
        elif value:
            errors.append(f"{key} must be empty")

    rendered = verification.get("renderedSheets")
    rendered_paths: list[Path] = []
    preview_root = preview_dir.resolve()
    if not isinstance(rendered, list) or len(rendered) != _PREVIEW_SHEET_COUNT:
        errors.append(f"renderedSheets must contain exactly {_PREVIEW_SHEET_COUNT} paths")
    else:
        for index, raw_path in enumerate(rendered, start=1):
            if not isinstance(raw_path, str) or not raw_path.strip():
                errors.append(f"renderedSheets[{index}] must be a non-empty path")
                continue
            path = Path(raw_path).resolve()
            if path.parent != preview_root or path.suffix.lower() != ".png":
                errors.append(f"renderedSheets[{index}] is outside the staging preview directory")
                continue
            if not path.is_file() or path.stat().st_size <= 0:
                errors.append(f"renderedSheets[{index}] does not exist or is empty")
                continue
            rendered_paths.append(path)
        if len(set(rendered_paths)) != len(rendered_paths):
            errors.append("renderedSheets contains duplicate paths")
        actual_previews = {
            path.resolve()
            for path in preview_dir.glob("*.png")
            if path.is_file() and path.stat().st_size > 0
        }
        if len(actual_previews) != _PREVIEW_SHEET_COUNT or set(rendered_paths) != actual_previews:
            errors.append("renderedSheets does not match the seven generated preview files")

    if not workbook_path.is_file() or workbook_path.stat().st_size <= 0:
        errors.append("workbook artifact does not exist or is empty")
    if errors:
        raise RuntimeError("Finance workbook verification failed: " + "; ".join(errors))
    return rendered_paths


def _publication_targets_exist(files: tuple[Path, ...], preview_dir: Path) -> bool:
    return preview_dir.exists() or any(path.exists() for path in files)


def _existing_publication_is_valid(
    report_id: str,
    *,
    json_path: Path,
    markdown_path: Path,
    workbook_path: Path,
    verification_path: Path,
    preview_dir: Path,
) -> bool:
    try:
        if not markdown_path.is_file() or markdown_path.stat().st_size <= 0:
            return False
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping) or payload.get("reportId") != report_id:
            return False
        verification = json.loads(verification_path.read_text(encoding="utf-8"))
        _validate_report_verification(
            verification,
            payload=payload,
            workbook_path=workbook_path,
            preview_dir=preview_dir,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError, TypeError, ValueError):
        return False
    return True


def _remove_incomplete_publication(
    files: tuple[Path, ...], preview_dir: Path
) -> None:
    for path in files:
        path.unlink(missing_ok=True)
    shutil.rmtree(preview_dir, ignore_errors=True)


def _publish_file_no_clobber(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with source.open("rb") as source_stream:
            with target.open("xb") as target_stream:
                created = True
                shutil.copyfileobj(source_stream, target_stream)
                target_stream.flush()
                os.fsync(target_stream.fileno())
    except BaseException:
        # Never remove a pre-existing target: ``created`` becomes true only
        # after the exclusive create succeeds for this invocation.
        if created:
            target.unlink(missing_ok=True)
        raise


def _publish_directory_no_clobber(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir(exist_ok=False)
    try:
        for path in source.iterdir():
            if not path.is_file():
                raise RuntimeError(f"Staged preview entry is not a file: {path}")
            _publish_file_no_clobber(path, target / path.name)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise


def _assert_published_support_files(
    markdown_path: Path,
    workbook_path: Path,
    verification_path: Path,
    preview_dir: Path,
) -> None:
    for path in (markdown_path, workbook_path, verification_path):
        if not path.is_file() or path.stat().st_size <= 0:
            raise RuntimeError(f"Published report support artifact is missing or empty: {path}")
    previews = [
        path for path in preview_dir.glob("*.png") if path.is_file() and path.stat().st_size > 0
    ]
    if len(previews) != _PREVIEW_SHEET_COUNT:
        raise RuntimeError(
            f"Published report preview count must be {_PREVIEW_SHEET_COUNT}, got {len(previews)}"
        )


def build_owner_markdown(payload: dict[str, Any]) -> str:
    period = payload.get("period") or {}
    totals = payload.get("totals") or {}
    policy = payload.get("policy") or {}
    policy_ids_used = policy.get("reportingPolicyIdsUsed") or [
        policy.get("reportingPolicyId")
    ]
    policy_ids_used = [str(value) for value in policy_ids_used if value]
    policy_versions = policy.get("reportingPolicyVersions") or []

    def reserve_values(row: Mapping[str, Any]) -> tuple[Any, Any, Any]:
        """Read split reserves while remaining compatible with legacy snapshots."""

        total = row.get("totalPolicyReserveCny")
        if total in (None, ""):
            total = row.get("combinedAdvertisingAndReturnPoolCny")
        risk = row.get("returnRiskReserveCny")
        promotion = row.get("promotionReserveCny")
        if risk in (None, "") and promotion in (None, ""):
            # Before v7 the single 3% value was a shared advertising/returns
            # reserve.  Show it in the promotion/shared column without
            # pretending that a separate 2% return reserve existed.
            risk = "0.00"
            promotion = total
        return risk, promotion, total

    total_return_reserve, total_promotion_reserve, total_policy_reserve = (
        reserve_values(totals)
    )
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
        f"- 已确认订单：{totals.get('confirmedOrderCount', 0)}；开放订单：{totals.get('openItemOrderCount', 0)}。",
        f"- 订单时点营业额 G：{_money(totals.get('salesAmountGCny'))} CNY。",
        f"- 平台实际佣金 C：{_money(totals.get('actualCommissionCCny'))} CNY。",
        f"- 收单费：{_money(totals.get('acquiringFeeCny'))} CNY。",
        f"- 平台 forPay／账户增量：{_money(totals.get('forPayCny'))} / {_money(totals.get('accountIncrementCny'))} CNY。",
        f"- 采购成本：{_money(totals.get('purchaseCostCny'))} CNY。",
        f"- 平台及履约费用：{_money(totals.get('platformAndOperationsCostCny'))} CNY。",
        f"- 代运营费：{_money(totals.get('managedOperationsCny'))} CNY。",
        f"- 退货风险专项预留（新口径 2% 部分）：{_money(total_return_reserve)} CNY。",
        f"- 推广预留／历史共享预留（新口径 3%；旧口径为共享 3%）：{_money(total_promotion_reserve)} CNY。",
        f"- 政策计提合计：{_money(total_policy_reserve)} CNY。",
        f"- 实际退货物流费：{_money(totals.get('returnLogisticsCny'))} CNY。",
        f"- 平台广告/推广周期扣款（仅对账）：{_money(totals.get('advertisingSettlementDeductionCny'))} CNY。",
        f"- 规则可分配活动推广：{_money(totals.get('advertisingProvisionCny'))} CNY。",
        f"- 规则可提现：{_money(totals.get('ruleWithdrawableCny'))} CNY。",
        f"- 历史已提现（独立现金事件，不重复扣当前余额）：{_money(totals.get('historicalWithdrawalCny'))} CNY。",
        f"- 已确认严格利润：**{_money(totals.get('strictOwnerProfitCny'))} CNY**。",
        f"- 本报告实际使用的财务策略：{', '.join(policy_ids_used) if policy_ids_used else '待定'}。",
        "",
        "> 每笔订单按其创建时间选择财务策略和成本版本：旧订单保留历史共享 3% 口径；新口径分别计提 2% 退货风险与 3% 推广预留。实际广告及平台推广周期扣款仅用于对账，不重复扣减；退货物流费只取平台财务流水，不使用 8/16 CNY 默认值。",
        "",
    ]
    if policy_versions:
        lines.extend(
            [
                "## 财务策略时序",
                "",
                "| 策略 | 生效（含） | 失效（不含） | 计提模式 | 退货风险率 | 推广率 | 合计率 |",
                "|---|---|---|---|---:|---:|---:|",
            ]
        )
        for version in policy_versions:
            lines.append(
                "| {policy_id} | {effective_from} | {effective_to} | {mode} | {return_rate} | {promotion_rate} | {total_rate} |".format(
                    policy_id=version.get("reportingPolicyId") or "待定",
                    effective_from=version.get("effectiveFrom") or "待定",
                    effective_to=version.get("effectiveTo") or "持续有效",
                    mode=version.get("reserveAllocationMode") or "待定",
                    return_rate=version.get("returnRiskReserveRate") or "待定",
                    promotion_rate=version.get("promotionReserveRate") or "待定",
                    total_rate=version.get("totalReserveRate") or "待定",
                )
            )
        lines.append("")
    lines.extend(
        [
            "## 店铺汇总",
            "",
            "| 店铺 | 已确认/开放 | G CNY | C CNY | forPay CNY | 2%代运营 CNY | 退货风险预留 | 推广/历史共享预留 | 政策计提合计 | 周期广告扣款（对账） | 可分配推广 CNY | 可提现 CNY | 严格利润 CNY |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in payload.get("stores") or []:
        row_return_reserve, row_promotion_reserve, row_policy_reserve = (
            reserve_values(row)
        )
        lines.append(
            "| {alias} | {confirmed}/{open_items} | {turnover} | {commission} | {for_pay} | {managed} | {return_reserve} | {promotion_reserve} | {policy_reserve} | {settlement_ads} | {ads} | {withdrawable} | {profit} |".format(
                alias=row.get("storeAlias"),
                confirmed=row.get("confirmedOrderCount", 0),
                open_items=row.get("openItemOrderCount", 0),
                turnover=_money(row.get("salesAmountGCny")),
                commission=_money(row.get("actualCommissionCCny")),
                for_pay=_money(row.get("forPayCny")),
                managed=_money(row.get("managedOperationsCny")),
                return_reserve=_money(row_return_reserve),
                promotion_reserve=_money(row_promotion_reserve),
                policy_reserve=_money(row_policy_reserve),
                settlement_ads=_money(
                    row.get("advertisingSettlementDeductionCny")
                ),
                ads=_money(row.get("advertisingProvisionCny")),
                withdrawable=_money(row.get("ruleWithdrawableCny")),
                profit=_money(row.get("strictOwnerProfitCny")),
            )
        )
    previous_report = payload.get("previousReport") or {}
    variance_rows = previous_report.get("varianceRows") or []
    if variance_rows:
        previous_period = previous_report.get("period") or {}
        bridge_schema = str(previous_report.get("schemaVersion") or "")
        bridge_status = (
            str(previous_report.get("status") or "")
            if bridge_schema == "finance-report-variance-v2"
            else ""
        )
        previous_report_status = previous_report.get("reportStatus")
        if previous_report_status in (None, "") and not bridge_schema:
            previous_report_status = previous_report.get("status")
        lines.extend(
            [
                "",
                "## 较上一版变动",
                "",
                (
                    f"- 对比报告：`{previous_report.get('reportId') or '未提供'}`，"
                    f"期间截至 {previous_period.get('to') or '待定'}"
                    + (
                        f"，报告状态 {previous_report_status}。"
                        if previous_report_status not in (None, "")
                        else "。"
                    )
                ),
            ]
        )
        compatibility_issues = previous_report.get("compatibilityIssues") or []
        basis_changes = previous_report.get("basisChanges") or []
        if bridge_status and bridge_status != "COMPLETE":
            lines.append(
                f"> 差异桥状态：{bridge_status}。部分归因待核对；不影响本期报告金额及发布状态。"
            )
        if isinstance(compatibility_issues, list) and compatibility_issues:
            lines.append(
                "> 兼容性提示："
                + "；".join(str(item) for item in compatibility_issues if item not in (None, ""))
                + "。"
            )
        if isinstance(basis_changes, list) and basis_changes:
            lines.append(
                "> 口径变化："
                + "；".join(str(item) for item in basis_changes if item not in (None, ""))
                + "。"
            )
        bridge_v2 = bridge_schema == "finance-report-variance-v2" or any(
            isinstance(row, Mapping) and "aggregateRecalculation" in row
            for row in variance_rows
        )
        if bridge_v2:
            lines.extend(
                [
                    "",
                    "| 项目 | 上一版 | 新转确认 | 移出确认 | 留存订单更正 | 汇总重算 | 本版 | 差额 | 原因 |",
                    "|---|---:|---:|---:|---:|---:|---:|---:|---|",
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    "| 项目 | 上一版 | 新转确认 | 移出确认 | 其他更正 | 本版 | 差额 | 原因 |",
                    "|---|---:|---:|---:|---:|---:|---:|---|",
                ]
            )
        for row in variance_rows:
            reason = str(row.get("reason") or "")
            row_status = str(row.get("status") or "")
            if row_status and row_status != "COMPLETE":
                reason = f"{reason}（状态：{row_status}）" if reason else f"状态：{row_status}"
            values = {
                "label": row.get("label") or row.get("metric") or "",
                "previous": _variance_value(row, "previous"),
                "added": _variance_value(row, "newlyConfirmed"),
                "removed": _variance_value(row, "removedFromConfirmed"),
                "corrected": _variance_value(row, "confirmedCorrections"),
                "current": _variance_value(row, "current"),
                "delta": _variance_value(row, "delta"),
                "reason": reason,
            }
            if bridge_v2:
                values["aggregate"] = _variance_value(row, "aggregateRecalculation")
                lines.append(
                    "| {label} | {previous} | {added} | {removed} | {corrected} | {aggregate} | {current} | {delta} | {reason} |".format(
                        **values
                    )
                )
            else:
                lines.append(
                    "| {label} | {previous} | {added} | {removed} | {corrected} | {current} | {delta} | {reason} |".format(
                        **values
                    )
                )
    activation_rows = payload.get("advertisingFundingActivation") or []
    if activation_rows:
        lines.extend([
            "",
            "## 广告资金起算点",
            "",
            "| 店铺 | 起算时间 | 证据来源 | 状态 |",
            "|---|---|---|---|",
        ])
        for row in activation_rows:
            lines.append(
                "| {store} | {at} | {source} | {status} |".format(
                    store=row.get("storeAlias", ""),
                    at=row.get("at") or "待定",
                    source=row.get("source") or row.get("message") or "待补平台证据",
                    status=row.get("status") or "PENDING_DATA",
                )
            )
    funding_rows = payload.get("funding") or []
    if funding_rows:
        lines.extend([
            "",
            "## 广告余额结算与历史提现欠费",
            "",
            "| 店铺 | 平台可提现余额 | 未分配3%推广储备 | 已批准/执行中余额划拨 | 实际广告消耗 | 平台周期广告扣款 | 历史欠广告费 | 历史欠费状态 | 历史已冲账 | 历史未冲账 | 历史欠费后可提现 | 未核实历史提现 |",
            "|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|",
        ])
        for row in funding_rows:
            lines.append(
                "| {store} | {balance} | {reserve} | {allocation} | {spend} | {settlement} | {debt} | {status} | {applied} | {unapplied} | {after} | {unverified} |".format(
                    store=row.get("storeAlias", ""),
                    balance=_money(row.get("withdrawableBalanceCny")),
                    reserve=_money(row.get("unallocatedAdvertisingReserveCny")),
                    allocation=_money(
                        row.get("approvedPendingCampaignAllocationCny")
                        or row.get("approvedPendingDepositCny")
                    ),
                    spend=_money(row.get("actualAdvertisingSpendCny")),
                    settlement=_money(
                        row.get("advertisingSettlementDeductionCny")
                    ),
                    debt=_money(row.get("advertisingDebtOutstandingCny")),
                    status=row.get("advertisingDebtStatus") or (
                        "PENDING_DATA"
                        if row.get("advertisingFeeUnverifiedCount")
                        else "KNOWN"
                    ),
                    applied=_money(row.get("advertisingPaymentAppliedCny")),
                    unapplied=_money(row.get("advertisingPaymentUnappliedCny")),
                    after=_money(row.get("withdrawableAfterAdvertisingDebtCny")),
                    unverified=row.get("advertisingFeeUnverifiedCount", 0),
                )
            )
        lines.append(
            "- 后续提现与广告费不关联：活动预算直接使用 WB 结算余额，平台按结算周期扣除；"
            "`settlement_balance` 提现记录的应扣、已扣和欠费均为 0，且不计入 PENDING_DATA。"
            "仅历史提现中明确未扣/少扣的广告费继续按 WB advertising_payments 实际到账金额 FIFO 冲账；"
            "历史记录未说明广告费时仍标记为 PENDING_DATA，未知金额显示为‘待定’，不等同于 0。"
        )
    blockers = payload.get("blockers") or []
    lines.extend(["", "## 数据完整性", ""])
    if blockers:
        lines.append("以下问题阻止证据封账，但不阻止本期报告按时发布；已结算行仍可用于资金分配，未结算行不进入确认金额：")
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
        "- 采购成本、售价/价格策略和财务策略按订单创建时间匹配有效版本并锁定；新版本不静默回写旧订单。",
        "- 成交价 G、平台扣费 C、forPay 和退货物流以 WB 财务证据为准；2%与3%按订单时点 G 计算。",
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
    if value in (None, ""):
        return "待定"
    try:
        return f"{Decimal(str(value)).quantize(Decimal('0.01')):,.2f}"
    except Exception:
        return "待定"


def _variance_value(row: Mapping[str, Any], key: str) -> str:
    value = row.get(key)
    if value in (None, ""):
        return "待定"
    if row.get("metric") == "confirmedOrderCount":
        return str(value)
    return _money(value)
