from __future__ import annotations

"""Safe bridge for appending immutable finance reports to the canonical XLSX.

The workbook itself is authored/updated by the Node ``@oai/artifact-tool``
bridge in ``tools/append_wb_finance_workbook.mjs``.  Keeping the process
invocation here makes the same operation available to the CLI and to the
scheduled daily controller without importing optional spreadsheet libraries
into ordinary finance calculations.
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

from .finance_reporting_standard import load_active_wb_periodic_reporting_standard


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "tools" / "append_wb_finance_workbook.mjs"


class FinanceWorkbookError(RuntimeError):
    """Raised when the canonical workbook cannot be safely updated."""


def sync_canonical_workbook(
    *,
    report_paths: Iterable[str | Path] | None = None,
    through: str | None = None,
    canonical: str | Path | None = None,
    report_root: str | Path | None = None,
    registry: str | Path | None = None,
    cash_events: str | Path | None = None,
    debt_snapshot: Mapping[str, Any] | None = None,
    repo_root: Path = ROOT,
    timeout_seconds: int = 420,
) -> dict[str, Any]:
    """Append report artifacts to the configured canonical workbook.

    The Node bridge performs the lock, backup, temp export, re-import and
    formula/readback checks.  This wrapper only resolves configuration and
    returns its compact JSON result.  No platform or money-moving API is
    called here.
    """

    standard = load_active_wb_periodic_reporting_standard(
        repo_root=Path(repo_root).resolve()
    )
    canonical_override = canonical or os.getenv("WB_FINANCE_CANONICAL_WORKBOOK", "").strip()
    target = Path(canonical_override).resolve() if canonical_override else standard.canonical_workbook
    if not target.exists():
        raise FinanceWorkbookError(f"canonical workbook does not exist: {target}")
    if not BRIDGE.exists():
        raise FinanceWorkbookError(f"canonical workbook bridge is missing: {BRIDGE}")
    node = os.getenv("WB_FINANCE_NODE", "").strip() or shutil.which("node")
    if not node:
        raise FinanceWorkbookError("Node.js is required for canonical workbook sync")

    registry_override = registry or os.getenv("WB_FINANCE_STORE_REGISTRY", "").strip()
    cash_events_override = cash_events or os.getenv("WB_FINANCE_CASH_EVENTS", "").strip()
    command = [
        node,
        str(BRIDGE),
        "--canonical",
        str(target),
        "--report-root",
        str(Path(report_root).resolve() if report_root else _default_report_root(standard, Path(repo_root).resolve())),
        "--registry",
        str(Path(registry_override).resolve() if registry_override else standard.store_registry),
        "--cash-events",
        str(Path(cash_events_override).resolve() if cash_events_override else standard.historical_cash_events_registry),
    ]
    if through:
        command.extend(["--through", str(through)[:10]])
    for report in _existing_reports(report_paths):
        command.extend(["--report", str(report)])

    debt_file: Path | None = None
    if debt_snapshot is not None:
        # Pass the current liability snapshot through a short-lived JSON file;
        # it lets a manual withdrawal update the workbook even when no new
        # periodic report artifact was generated in this run.
        temp_dir = Path(repo_root).resolve() / "tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        handle, raw_path = tempfile.mkstemp(prefix="wb-debt-", suffix=".json", dir=temp_dir)
        os.close(handle)
        debt_file = Path(raw_path)
        debt_file.write_text(
            json.dumps(dict(debt_snapshot), ensure_ascii=False, indent=2, default=_json_default) + "\n",
            encoding="utf-8",
        )
        command.extend(["--debt-json", str(debt_file)])

    try:
        completed = subprocess.run(
            command,
            cwd=Path(repo_root).resolve(),
            text=True,
            capture_output=True,
            timeout=max(30, int(timeout_seconds)),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise FinanceWorkbookError(
            f"canonical workbook sync timed out after {timeout_seconds}s"
        ) from exc
    finally:
        if debt_file is not None:
            try:
                debt_file.unlink()
            except OSError:
                pass
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise FinanceWorkbookError(
            f"canonical workbook sync failed (exit {completed.returncode}): {detail[-4000:]}"
        )
    result = _last_json_object(completed.stdout)
    if result is None:
        raise FinanceWorkbookError(
            "canonical workbook bridge returned no JSON result: "
            + (completed.stdout or "")[-2000:]
        )
    result.setdefault("bridge", str(BRIDGE))
    return result


def _existing_reports(report_paths: Iterable[str | Path] | None) -> list[Path]:
    result: list[Path] = []
    seen: set[Path] = set()
    for value in report_paths or ():
        if not value:
            continue
        path = Path(value).resolve()
        if path.suffix.lower() != ".json" or path.name.endswith(".verification.json"):
            continue
        if path.exists() and path not in seen:
            seen.add(path)
            result.append(path)
    return result


def _default_report_root(standard: Any | None = None, repo_root: Path = ROOT) -> Path:
    """Resolve the report archive without baking a dated local path into code.

    The environment override is useful for a scheduled task running on a
    different workstation.  The active periodic standard may provide a
    ``reportRoot`` value; relative values are interpreted from the repository
    root.  The final fallback is repository-local so a clean checkout never
    depends on one operator's drive layout.
    """

    override = os.getenv("WB_FINANCE_REPORT_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    raw = getattr(standard, "raw", None)
    configured = raw.get("reportRoot") if isinstance(raw, Mapping) else None
    if configured:
        candidate = Path(str(configured)).expanduser()
        return (repo_root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    return (repo_root / "outputs" / "wb-finance-reports").resolve()


def _last_json_object(text: str | None) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    # The artifact runtime may print an informational line before the final
    # JSON object.  Parse candidate opening braces from the end, not by
    # blindly concatenating logs into a JSON document.
    positions = [index for index, char in enumerate(raw) if char == "{"]
    for index in reversed(positions):
        try:
            value = json.loads(raw[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _json_default(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return str(value)
