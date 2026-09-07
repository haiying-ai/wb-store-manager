/*
 * Generic bridge from immutable finance report JSON artifacts to the single
 * canonical iterative workbook.
 *
 * This file deliberately uses @oai/artifact-tool.  It imports the existing
 * workbook and appends rows to the existing tables; it never recreates a
 * workbook and never clears unknown historical rows.  The bridge is invoked
 * by `python -m wb_manager finance daily` after the platform sync/report pass,
 * or directly through `finance workbook-sync`.
 */
import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import crypto from "node:crypto";
import { fileURLToPath } from "node:url";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const DEFAULT_CANONICAL = process.env.WB_FINANCE_CANONICAL_WORKBOOK || path.join(
  ROOT,
  "outputs",
  "wb-finance-workbook-20260831",
  "WB双店财务台账_可迭代_20260831.xlsx",
);
const DEFAULT_REPORT_ROOT = process.env.WB_FINANCE_REPORT_ROOT || path.join(ROOT, "outputs", "wb-finance-reports");
const DEFAULT_REGISTRY = process.env.WB_FINANCE_STORE_REGISTRY || path.join(ROOT, "config/finance/stores.json");
const DEFAULT_CASH_EVENTS = process.env.WB_FINANCE_CASH_EVENTS || path.join(ROOT, "config/finance/historical_cash_events.example.json");
const DATE_FMT = "yyyy-mm-dd";
const DATETIME_FMT = "yyyy-mm-dd hh:mm";
const MONEY_FMT = "#,##0.00;[Red](#,##0.00);-";
const HEADER_CACHE = new Map();
const BRIDGE_SCHEMA_VERSION = 2;
const LANDING_SECTION_TITLES = [
  "结算余额广告储备与历史欠费对账",
  "广告欠费/提现对账",
  "提现记录明细",
  "历史人工提现/现金事件",
  "当前开店至今聚合快照",
];

const args = parseArgs(process.argv.slice(2));
const canonical = path.resolve(args.canonical || DEFAULT_CANONICAL);
const reportRoot = path.resolve(args.reportRoot || DEFAULT_REPORT_ROOT);
const registryPath = path.resolve(args.registry || DEFAULT_REGISTRY);
const cashEventsPath = path.resolve(args.cashEvents || DEFAULT_CASH_EVENTS);
const debtJsonPath = args.debtJson ? path.resolve(args.debtJson) : null;
const lockPath = `${canonical}.lock`;
// This sidecar is deliberately kept next to the canonical workbook rather
// than inside the report tree. It records the exact input snapshot that was
// successfully written, allowing unchanged scheduled runs to return without
// importing/exporting the XLSX at all.
const sidecarPath = `${canonical}.bridge.json`;
let lockHandle = null;

const invokedAsMain = Boolean(process.argv[1])
  && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (invokedAsMain) {
  try {
    lockHandle = await acquireLock(lockPath);
    const result = await run();
    console.log(JSON.stringify(result, null, 2));
  } catch (error) {
    console.error(`canonical workbook sync failed: ${error?.stack || error}`);
    process.exitCode = 1;
  } finally {
    if (lockHandle) {
      try { await lockHandle.close(); } catch { /* best effort */ }
      try { await fs.unlink(lockPath); } catch { /* best effort */ }
    }
  }
}

async function run() {
  const startedAtMs = Date.now();
  const timingsMs = {};
  let phaseStartedMs = startedAtMs;
  const mark = (name) => {
    timingsMs[name] = Math.max(0, Date.now() - phaseStartedMs);
    phaseStartedMs = Date.now();
  };
  const [registry, cashEvents, debtSnapshot] = await Promise.all([
    readJson(registryPath, {}),
    readJson(cashEventsPath, { events: [] }),
    debtJsonPath ? readJson(debtJsonPath, null) : Promise.resolve(null),
  ]);
  const stores = enabledStores(registry);
  const reports = await selectReports(args.reports, reportRoot);
  mark("inputLoad");
  const hasCashEvents = Array.isArray(cashEvents?.events)
    && cashEvents.events.some((event) => event && String(event.eventType || "").toLowerCase() === "withdrawal" && event.eventId);
  const inputFingerprint = buildInputFingerprint({ reports, registry, cashEvents, debtSnapshot });
  if (!reports.length && !debtSnapshot && !hasCashEvents) {
    mark("preflight");
    return {
      operation: "workbook-sync",
      status: "NO_REPORTS",
      canonical,
      reportsSeen: 0,
      rowsAdded: { orders: 0, events: 0, weekly: 0, monthly: 0, debtSnapshots: 0, cashEvents: 0, withdrawalRecords: 0, sources: 0 },
      timing: timingSummary(startedAtMs, timingsMs),
    };
  }
  await ensureFile(canonical);
  const canonicalBefore = await statSnapshot(canonical);
  const priorState = await readJson(sidecarPath, null);
  if (priorState?.schemaVersion === BRIDGE_SCHEMA_VERSION
    && priorState?.status === "PASS"
    && priorState.canonical === path.resolve(canonical)
    && priorState.inputFingerprint === inputFingerprint
    && sameStat(priorState.canonicalStat, canonicalBefore)) {
    mark("preflight");
    return {
      operation: "workbook-sync",
      status: "NOOP",
      canonical,
      sidecar: sidecarPath,
      reportsSeen: reports.length,
      reportsAppended: 0,
      rowsAdded: { orders: 0, events: 0, weekly: 0, monthly: 0, debtSnapshots: 0, cashEvents: 0, withdrawalRecords: 0, sources: 0, skippedReports: reports.length },
      timing: timingSummary(startedAtMs, timingsMs),
    };
  }
  mark("preflight");
  const stamp = safeStamp(new Date());
  const backupDir = path.join(path.dirname(canonical), ".backups");
  await fs.mkdir(backupDir, { recursive: true });
  const backup = path.join(backupDir, `${path.basename(canonical)}.pre-sync-${stamp}.xlsx`);
  await fs.copyFile(canonical, backup);

  const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(canonical));
  mark("xlsxImport");
  const sheetNames = new Set((workbook.worksheets.items || []).map((s) => String(s.name)));
  const required = ["订单明细", "财务事件明细", "周报历史", "月报历史", "来源校验", "更新说明"];
  const missingSheets = required.filter((name) => !sheetNames.has(name));
  if (missingSheets.length) throw new Error(`canonical workbook is missing sheets: ${missingSheets.join(", ")}`);

  const sheets = {
    orders: workbook.worksheets.getItem("订单明细"),
    events: workbook.worksheets.getItem("财务事件明细"),
    weekly: workbook.worksheets.getItem("周报历史"),
    monthly: workbook.worksheets.getItem("月报历史"),
    sources: workbook.worksheets.getItem("来源校验"),
    instructions: workbook.worksheets.getItem("更新说明"),
  };
  const known = collectKnown(workbook, sheets);
  const counters = { orders: 0, events: 0, weekly: 0, monthly: 0, debtSnapshots: 0, cashEvents: 0, withdrawalRecords: 0, sources: 0, skippedReports: 0 };
  const appendedReports = [];
  // Keep source rows pending until report filtering is complete.  The source
  // table is followed by validation/model-status blocks on the same sheet, so
  // these rows are appended after all existing sheet content rather than by
  // expanding SourceLogTable into those blocks.
  const pendingSourceRows = [];
  // Keep the newest cumulative bootstrap candidate even when its report ID is
  // already present in 来源校验.  The append-only detail/history tables are
  // intentionally idempotent, but the landing dashboard is a mutable
  // "current view" and must be refreshed when a later bootstrap corrects an
  // aggregate (for example a newly matched return-logistics row).
  let latestBootstrap = null;
  const latestWeekly = selectLatestWeeklyReport(reports);

  // Reports are processed oldest-first so a later RESTATED snapshot is
  // appended after its original candidate/final snapshot.
  for (const report of reports) {
    const reportId = String(report.reportId || "").trim();
    if (!reportId) continue;
    const period = report.period || {};
    const kind = String(report.kind || "");
    if (kind === "bootstrap" && period.cumulative) {
      if (!latestBootstrap || compareGeneratedAt(report, latestBootstrap) > 0) {
        latestBootstrap = report;
      }
    }
    if (known.reportIds.has(reportId)) {
      counters.skippedReports += 1;
      continue;
    }
    const reportMeta = {
      reportId,
      kind,
      status: String(report.status || "PENDING_DATA"),
      generatedAt: report.generatedAt || null,
      from: period.from || null,
      to: period.to || null,
      cumulative: Boolean(period.cumulative),
    };

    const orderRows = buildOrderRows(report, stores);
    const eventRows = buildEventRows(report, stores);
    const orderAdded = appendVersionedRows(
      sheets.orders,
      "OrderDetailTable",
      orderRows,
      (row) => orderKey(row),
      known.orderKeys,
      orderFingerprint,
    );
    counters.orders += orderAdded.added;
    const eventAdded = appendVersionedRows(
      sheets.events,
      "FinanceEventTable",
      eventRows,
      (row) => eventKey(row),
      known.eventKeys,
      eventFingerprint,
    );
    counters.events += eventAdded.added;

    const summaries = [
      { alias: "ALL", summary: report.totals || {} },
      ...(report.stores || []).map((store) => ({ alias: store.storeAlias, summary: store.summary || store })),
    ].filter((item) => item.alias && item.summary);
    if (kind === "weekly") {
      const rows = buildHistoryRows(report, summaries, stores, "weekly");
      const added = appendUniqueRows(sheets.weekly, "WeeklyHistoryTable", rows, (row) => historyKey(row), known.weeklyKeys);
      counters.weekly += added.added;
    } else if (kind === "monthly" || kind === "bootstrap") {
      const rows = buildHistoryRows(report, summaries, stores, "monthly");
      const added = appendUniqueRows(sheets.monthly, "MonthlyHistoryTable", rows, (row) => monthlyKey(row), known.monthlyKeys);
      counters.monthly += added.added;
    }

    const sourceRow = buildSourceRow(reportMeta, report, reportPathFor(report));
    pendingSourceRows.push(sourceRow);
    // Retain the funding snapshot for the debt section while keeping the
    // metadata object compact for the run log/result.
    appendedReports.push({ ...reportMeta, funding: report.funding || [] });
  }

  if (pendingSourceRows.length) {
    const sourceAdded = appendUniqueRows(
      sheets.sources,
      "SourceLogTable",
      pendingSourceRows,
      (row) => reportIdFromSourceRow(row),
      known.reportIds,
    );
    counters.sources += sourceAdded.added;
  }

  // Keep a short append-only run log in 更新说明.  It is intentionally
  // appended below the existing table instead of replacing the instructions.
  appendRunNote(sheets.instructions, appendedReports, counters, backup);
  counters.debtSnapshots = appendFundingDebtSection(workbook, appendedReports, debtSnapshot);
  counters.cashEvents = appendHistoricalCashSection(workbook, cashEvents);
  counters.withdrawalRecords = appendWithdrawalRecordSection(workbook, debtSnapshot);
  const dashboardRefresh = latestBootstrap
    ? refreshDashboardFromBootstrap(workbook, latestBootstrap, stores)
    : null;
  const weeklyDashboardRefresh = latestWeekly
    ? refreshDashboardFromWeekly(workbook, latestWeekly, stores)
    : null;
  updateLandingNote(workbook, [latestWeekly, latestBootstrap].filter(Boolean));
  applyHistoryDateFormats(sheets.weekly, sheets.monthly);
  mark("appendAndRefresh");

  // Export to a sibling temporary file, re-import it, inspect formulas, and
  // only then replace the canonical path.  A backup is retained for recovery.
  const temp = `${canonical}.tmp-${process.pid}-${stamp}`;
  const exported = await SpreadsheetFile.exportXlsx(workbook);
  await exported.save(temp);
  await verifyExport(temp, appendedReports, counters, latestBootstrap, latestWeekly, stores);
  mark("xlsxExportAndVerify");
  await replaceWithBackup(temp, canonical);

  const canonicalAfter = await statSnapshot(canonical);
  const result = {
    operation: "workbook-sync",
    status: "PASS",
    canonical,
    backup,
    sidecar: sidecarPath,
    reportsSeen: reports.length,
    reportsAppended: appendedReports.length,
    rowsAdded: counters,
    dashboardRefresh,
    weeklyDashboardRefresh,
    enabledStores: stores.map((s) => s.alias),
  };
  const timing = timingSummary(startedAtMs, timingsMs);
  result.timing = timing;
  await writeJsonAtomic(sidecarPath, {
    schemaVersion: BRIDGE_SCHEMA_VERSION,
    status: "PASS",
    inputFingerprint,
    reportIds: reports.map((report) => String(report.reportId || "")).filter(Boolean),
    canonical: path.resolve(canonical),
    canonicalStat: canonicalAfter,
    updatedAt: new Date().toISOString(),
    reportsSeen: reports.length,
    reportsAppended: appendedReports.length,
    rowsAdded: counters,
    timing,
  });
  mark("sidecar");
  // Include the sidecar write in the final total while retaining phase
  // timings for quick scheduled-run diagnosis.
  result.timing = timingSummary(startedAtMs, timingsMs);
  return result;
}

function parseArgs(argv) {
  const out = { reports: [] };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--report") out.reports.push(argv[++i]);
    else if (arg === "--canonical") out.canonical = argv[++i];
    else if (arg === "--report-root") out.reportRoot = argv[++i];
    else if (arg === "--registry") out.registry = argv[++i];
    else if (arg === "--cash-events") out.cashEvents = argv[++i];
    else if (arg === "--debt-json") out.debtJson = argv[++i];
    else if (arg === "--through") out.through = argv[++i];
    else if (arg === "--run-id") out.runId = argv[++i];
    else throw new Error(`unknown argument: ${arg}`);
  }
  return out;
}

async function acquireLock(file) {
  await fs.mkdir(path.dirname(file), { recursive: true });
  try {
    const handle = await fs.open(file, "wx");
    await handle.writeFile(JSON.stringify({ pid: process.pid, createdAt: new Date().toISOString() }) + "\n", "utf8");
    return handle;
  } catch (error) {
    if (error?.code === "EEXIST") {
      throw new Error(`another canonical workbook sync is already running (lock: ${file})`);
    }
    throw error;
  }
}

async function readJson(file, fallback) {
  try { return JSON.parse(await fs.readFile(file, "utf8")); }
  catch (error) {
    if (fallback !== undefined) return fallback;
    throw error;
  }
}

async function ensureFile(file) {
  try { await fs.access(file); }
  catch { throw new Error(`canonical workbook does not exist: ${file}`); }
}

function enabledStores(registry) {
  const rows = Array.isArray(registry?.stores) ? registry.stores : [];
  return rows.filter((s) => s && s.enabled !== false).map((s) => ({
    alias: String(s.alias || s.storeAlias || ""),
    name: String(s.name || s.displayName || s.alias || s.storeAlias || ""),
    openedAt: String(s.openedAt || ""),
  })).filter((s) => s.alias);
}

async function selectReports(explicit, root) {
  const paths = explicit?.filter(Boolean).map((p) => path.resolve(p));
  let files = paths;
  if (!files.length) files = await walkJson(root);
  const parsed = [];
  for (const file of files) {
    try {
      if (file.endsWith(".verification.json")) continue;
      const report = JSON.parse(await fs.readFile(file, "utf8"));
      if (!report?.reportId || !report?.kind || !report?.period) continue;
      if (args.through && String(report.period.to || "") > String(args.through).slice(0, 10)) continue;
      parsed.push({ report, file });
    } catch { /* unrelated or incomplete sidecar */ }
  }
  // Keep only the latest artifact for each logical period/status family. This
  // avoids replaying every transient candidate while retaining FINAL/RESTATED
  // changes as separate report IDs.
  const latest = new Map();
  for (const item of parsed) {
    const p = item.report.period || {};
    const key = [item.report.kind, p.from, p.to, Boolean(p.cumulative), item.report.status].join("|");
    const prev = latest.get(key);
    if (!prev || String(item.report.generatedAt || "") > String(prev.report.generatedAt || "")) latest.set(key, item);
  }
  return [...latest.values()]
    .sort((a, b) => {
      const pa = a.report.period || {}; const pb = b.report.period || {};
      return `${pa.to || ""}|${a.report.generatedAt || ""}`.localeCompare(`${pb.to || ""}|${b.report.generatedAt || ""}`);
    })
    .map((item) => { item.report.__sourcePath = item.file; return item.report; });
}

async function walkJson(root) {
  const out = [];
  async function visit(dir, depth = 0) {
    if (depth > 4) return;
    let entries = [];
    try { entries = await fs.readdir(dir, { withFileTypes: true }); } catch { return; }
    for (const entry of entries) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) await visit(full, depth + 1);
      else if (entry.isFile() && entry.name.endsWith(".json") && !entry.name.endsWith(".verification.json")) out.push(full);
    }
  }
  await visit(root);
  return out;
}

function collectKnown(workbook, sheets) {
  const reportIds = new Set();
  const orderKeys = new Set();
  const eventKeys = new Set();
  const weeklyKeys = new Set();
  const monthlyKeys = new Set();
  const sourceValues = sheets.sources.getUsedRange()?.values || [];
  for (const row of sourceValues) {
    for (const value of row || []) {
      const text = String(value || "");
      // Report IDs are 64-char hex strings and may be embedded in a source
      // key/note (for example `monthly/<reportId>`), so collect substrings.
      for (const match of text.match(/[0-9a-f]{64}/gi) || []) reportIds.add(match);
    }
  }
  const orderValues = sheets.orders.getUsedRange()?.values || [];
  for (const row of orderValues.slice(5)) {
    const key = stableOrderKeyFromArray(row);
    if (key) orderKeys.add(key);
  }
  const eventValues = sheets.events.getUsedRange()?.values || [];
  for (const row of eventValues.slice(5)) {
    const key = stableEventKeyFromArray(row);
    if (key) eventKeys.add(key);
  }
  const weeklyValues = sheets.weekly.getUsedRange()?.values || [];
  for (const row of weeklyValues.slice(5)) {
    const key = historyKey(row); if (key) weeklyKeys.add(key);
  }
  const monthlyValues = sheets.monthly.getUsedRange()?.values || [];
  for (const row of monthlyValues.slice(5)) {
    const key = monthlyKey(row); if (key) monthlyKeys.add(key);
  }
  return { reportIds, orderKeys, eventKeys, weeklyKeys, monthlyKeys };
}

function buildOrderRows(report, stores) {
  const map = headerMapFor("订单明细", 52);
  const rows = [];
  for (const order of report.orders || []) {
    const row = Array(52).fill(null);
    const store = stores.find((s) => s.alias === order.storeAlias);
    set(row, map, "店铺标识", order.storeAlias);
    set(row, map, "店铺名称", store?.name || order.storeAlias);
    set(row, map, "订单ID", first(order.identities?.orderId));
    set(row, map, "订单创建时间(MSK)", dateTime(order.orderCreatedAt));
    set(row, map, "订单创建日(MSK)", dateOnly(order.orderCreatedAt));
    set(row, map, "业务关联ID(rid/srid)", first(order.identities?.srid) || first(order.identities?.orderId));
    set(row, map, "shkId/chrtId", first(order.identities?.shkId));
    set(row, map, "财务事件日(MSK)", dateOnly(order.completedAt || order.adjustedAt));
    set(row, map, "财务事件周起始", mondayOf(order.completedAt || order.adjustedAt));
    set(row, map, "创建周起始", mondayOf(order.orderCreatedAt));
    set(row, map, "SKU/货号", order.sku);
    set(row, map, "nmId", order.nmId);
    set(row, map, "WB订单状态", order.outcome);
    set(row, map, "生命周期", order.outcome);
    set(row, map, "认列状态", order.evidenceStatus || "PENDING");
    set(row, map, "结算证据行数", Array.isArray(order.evidenceIds) ? order.evidenceIds.length : null);
    set(row, map, "结算来源", sourceText(order));
    set(row, map, "订单时点售价G(CNY)", money(order.salesAmountGCny));
    set(row, map, "G来源", componentSource(order, "G") || "platform_finance.retailPriceWithDisc");
    set(row, map, "G时间匹配", order.salesAmountGCny != null ? "是" : null);
    set(row, map, "成本价(CNY)", money(order.purchaseCostCny));
    set(row, map, "成本版本", order.costVersionId);
    set(row, map, "成本来源", componentSource(order, "cost") || null);
    set(row, map, "成本选取依据", order.orderCreatedAt ? "orderCreatedAt" : null);
    set(row, map, "平台佣金C(CNY)", money(order.actualCommissionCCny ?? order.wbCommissionCny));
    set(row, map, "实际佣金率", ratio(order.actualCommissionCCny ?? order.wbCommissionCny, order.salesAmountGCny));
    set(row, map, "收单/支付费(CNY)", money(order.acquiringFeeCny ?? order.paymentFeeCny));
    set(row, map, "直接到账forPay(CNY)", money(order.forPayCny));
    set(row, map, "公式账户增量(CNY)", money(order.accountIncrementCny));
    set(row, map, "到账勾稽差额(CNY)", money(order.forPayReconciliationDifferenceCny));
    set(row, map, "源证据账户增量(CNY)", money(order.forPayCny));
    set(row, map, "正向物流(CNY)", money(order.forwardLogisticsCny));
    set(row, map, "实际退货/取消物流(CNY)", money(order.returnLogisticsCny));
    set(row, map, "代运营费2%(CNY)", money(order.managedOperationsCny));
    set(row, map, "广告+退货资金池（按行费率）(CNY)", money(order.combinedPoolCny ?? order.combinedAdvertisingAndReturnPoolCny));
    set(row, map, "按规则可提现(本行)(CNY)", money(order.ruleWithdrawableCny));
    set(row, map, "公式严格利润(CNY)", money(order.strictOwnerProfitCny ?? order.ownerProfitCny));
    set(row, map, "源证据严格利润(CNY)", money(order.ownerProfitCny));
    set(row, map, "利润勾稽差额(CNY)", money(order.ownerProfitCny != null && order.strictOwnerProfitCny != null ? Number(order.strictOwnerProfitCny) - Number(order.ownerProfitCny) : null));
    set(row, map, "价格公式版本", report.policy?.pricingPolicyId || null);
    set(row, map, "累计纳入", report.period?.cumulative ? 1 : 0);
    set(row, map, "报告纳入口径", `${report.kind}:${report.period?.from || ""}—${report.period?.to || ""}`);
    set(row, map, "财务证据ID", (order.evidenceIds || []).join("; "));
    set(row, map, "备注", `reportId=${report.reportId}; status=${report.status || "PENDING_DATA"}`);
    set(row, map, "C公式复核(CNY)", money(order.commissionFormulaCny ?? order.actualCommissionCCny));
    set(row, map, "C勾稽差额(CNY)", money(order.commissionReconciliationDifferenceCny));
    set(row, map, "代运营费率快照", decimalOrNull(report.policy?.managedOperationsRate));
    set(row, map, "广告+退货池率快照", decimalOrNull(report.policy?.combinedPoolRate));
    set(row, map, "报告政策版本快照", report.policy?.reportingPolicyId || null);
    set(row, map, "实际广告支出/充值(CNY)", money(order.actualAdvertisingSpendCny));
    set(row, map, "实际广告来源/状态", componentSource(order, "advertising") || "promotion API（仅对账）");
    rows.push(row);
  }
  return rows;
}

function buildEventRows(report, stores) {
  const map = headerMapFor("财务事件明细", 17);
  const rows = [];
  for (const order of report.orders || []) {
    const eventDate = order.completedAt || order.adjustedAt;
    if (!eventDate && !(order.evidenceIds || []).length) continue;
    const row = Array(17).fill(null);
    const store = stores.find((s) => s.alias === order.storeAlias);
    set(row, map, "店铺标识", order.storeAlias);
    set(row, map, "店铺名称", store?.name || order.storeAlias);
    set(row, map, "订单ID", first(order.identities?.orderId) || first(order.identities?.srid));
    set(row, map, "订单创建日(MSK)", dateOnly(order.orderCreatedAt));
    set(row, map, "SKU/货号", order.sku);
    set(row, map, "财务事件日(MSK)", dateOnly(eventDate));
    set(row, map, "事件周起始", mondayOf(eventDate));
    set(row, map, "生命周期", order.outcome);
    set(row, map, "认列状态", order.evidenceStatus || "PENDING");
    set(row, map, "财务rrdId集合（类型待拆）", (order.evidenceIds || []).join("; "));
    set(row, map, "财务行数", Array.isArray(order.evidenceIds) ? order.evidenceIds.length : null);
    set(row, map, "G订单级参考(CNY)", money(order.salesAmountGCny));
    set(row, map, "C订单级参考(CNY)", money(order.actualCommissionCCny ?? order.wbCommissionCny));
    set(row, map, "forPay订单级参考(CNY)", money(order.forPayCny));
    set(row, map, "正向物流订单级参考(CNY)", money(order.forwardLogisticsCny));
    set(row, map, "退/取消物流订单级参考(CNY)", money(order.returnLogisticsCny));
    set(row, map, "事件拆分说明", `reportId=${report.reportId}; ${report.kind}; ${report.status || "PENDING_DATA"}`);
    rows.push(row);
  }
  return rows;
}

// Historical withdrawals are supplied outside the platform order/finance
// feed.  Prefer the per-store funding snapshot, then fall back to the
// immutable cash-event registry embedded in the report.  Period (non-cumulative)
// views deliberately return zero; the adjustment is applied once in the
// cumulative/bootstrap view only.
function historicalWithdrawalForStore(report, alias) {
  const funding = Array.isArray(report?.funding)
    ? report.funding.find((item) => String(item?.storeAlias || "") === String(alias || ""))
    : null;
  if (funding && funding.historicalWithdrawalCny !== null && funding.historicalWithdrawalCny !== undefined && funding.historicalWithdrawalCny !== "") {
    const value = Number(funding.historicalWithdrawalCny);
    if (Number.isFinite(value)) return Math.max(0, value);
  }
  const events = Array.isArray(report?.historicalCashEvents) ? report.historicalCashEvents : [];
  const withdrawals = events.filter((event) => String(event?.eventType || "").toLowerCase() === "withdrawal");
  const amount = withdrawals
    .filter((event) => alias === "ALL" || String(event?.store || event?.storeAlias || "") === String(alias || ""))
    .reduce((sum, event) => sum + (Number(event?.amountCny) || 0), 0);
  if (amount > 0) return Math.round(amount * 100) / 100;
  if (alias === "ALL") {
    const total = Number(report?.totals?.historicalWithdrawalCny);
    if (Number.isFinite(total)) return Math.max(0, total);
  }
  return 0;
}

function buildHistoryRows(report, summaries, stores, kind) {
  const rows = [];
  const p = report.period || {};
  const from = dateOnly(p.from); const to = dateOnly(p.to);
  for (const item of summaries) {
    const s = item.summary || {};
    const store = stores.find((x) => x.alias === item.alias);
    if (kind === "weekly") {
      const row = Array(25).fill(null);
      row[0] = from; row[1] = to; row[2] = item.alias; row[3] = item.alias === "ALL" ? "合计" : (store?.name || item.alias);
      row[4] = "财务事件"; row[5] = num(s.orderCount); row[6] = num(s.confirmedOrderCount); row[7] = money(s.salesAmountGCny); row[8] = money(s.actualCommissionCCny); row[9] = money(s.forPayCny); row[10] = money(s.accountIncrementCny); row[11] = money(s.forwardOperationsCny); row[12] = money(s.managedOperationsCny); row[13] = money(s.combinedPoolCny ?? s.combinedAdvertisingAndReturnPoolCny); row[14] = money(s.returnLogisticsCny); row[15] = money(s.advertisingProvisionCny); row[16] = money(s.ruleWithdrawableCny); row[17] = money(s.strictOwnerProfitCny ?? s.ownerProfitCny); row[18] = 0; row[19] = money(s.ruleWithdrawableCny); row[20] = report.status || "PENDING_DATA"; row[21] = `reportId=${report.reportId}; ${blockerText(report)}`; row[22] = report.policy?.reportingPolicyId || null; row[23] = report.policy?.pricingPolicyId || null; row[24] = dateTime(report.generatedAt);
      rows.push(row);
    } else {
      const row = Array(27).fill(null);
      row[0] = dateOnly((p.to || p.from || "").slice(0, 7) + "-01"); row[1] = from; row[2] = to; row[3] = p.cumulative ? "BOOTSTRAP_CUMULATIVE" : "MONTHLY_PERIOD"; row[4] = item.alias; row[5] = item.alias === "ALL" ? "合计" : (store?.name || item.alias);
      row[6] = num(s.orderCount); row[7] = num(s.confirmedOrderCount); row[8] = money(s.salesAmountGCny); row[9] = money(s.actualCommissionCCny); row[10] = money(s.forPayCny); row[11] = money(s.accountIncrementCny); row[12] = money(s.forwardOperationsCny); row[13] = money(s.managedOperationsCny); row[14] = money(s.combinedPoolCny ?? s.combinedAdvertisingAndReturnPoolCny); row[15] = money(s.returnLogisticsCny); row[16] = money(s.advertisingProvisionCny); row[17] = money(s.ruleWithdrawableCny); row[18] = money(s.strictOwnerProfitCny ?? s.ownerProfitCny);
      const historicalWithdrawal = p.cumulative ? historicalWithdrawalForStore(report, item.alias) : 0;
      row[19] = money(historicalWithdrawal);
      row[20] = money(Math.max(0, Number(s.ruleWithdrawableCny || 0) - Number(historicalWithdrawal || 0)));
      row[21] = report.status || "PENDING_DATA"; row[22] = (report.blockers || []).length; row[23] = `reportId=${report.reportId}; ${blockerText(report)}`; row[24] = report.policy?.reportingPolicyId || null; row[25] = report.policy?.pricingPolicyId || null; row[26] = dateTime(report.generatedAt);
      rows.push(row);
    }
  }
  return rows;
}

function buildSourceRow(meta, report, sourcePath) {
  return [
    `${meta.kind}/${meta.reportId}`,
    "全局",
    "periodic_report",
    sourcePath || report.__sourcePath || "",
    `${meta.from || ""}—${meta.to || ""}`,
    meta.status,
    (report.orders || []).length,
    dateTime(meta.generatedAt),
    `${meta.kind}报告（canonical append）`,
    `reportId=${meta.reportId}; blockers=${(report.blockers || []).length}; cumulative=${meta.cumulative ? "yes" : "no"}`,
  ];
}

function appendUniqueRows(sheet, tableName, rows, keyFn, knownKeys) {
  if (!rows.length) return { added: 0, skipped: 0 };
  const fresh = [];
  for (const row of rows) {
    const key = keyFn(row);
    if (!key || knownKeys.has(key)) { if (key) knownKeys.add(key); continue; }
    knownKeys.add(key); fresh.push(row);
  }
  if (!fresh.length) return { added: 0, skipped: rows.length };
  const table = tableItems(sheet).find((item) => String(item.name || "") === tableName);
  if (tableName === "SourceLogTable") {
    // SourceLogTable ends immediately above permanent validation formulas.
    // Extending the native table overwrites those cells instead of inserting
    // worksheet rows in some imported XLSX files.  Preserve the original
    // table boundary and put continuation records after every existing block.
    const used = sheet.getUsedRange()?.values || [];
    const startIndex = Math.max(used.length, lastUsedRow(sheet) + 1);
    const width = fresh[0].length;
    sheet.getRangeByIndexes(startIndex, 0, fresh.length, width).values = fresh;
    sheet.getRange(`H${startIndex + 1}:H${startIndex + fresh.length}`).format.numberFormat = DATETIME_FMT;
  } else if (table?.rows?.add) {
    table.rows.add(null, fresh);
  } else {
    const used = sheet.getUsedRange()?.values || [];
    const width = fresh[0].length;
    sheet.getRangeByIndexes(used.length, 0, fresh.length, width).values = fresh;
  }
  return { added: fresh.length, skipped: rows.length - fresh.length };
}

function applyHistoryDateFormats(weeklySheet, monthlySheet) {
  const weeklyLast = lastUsedRow(weeklySheet);
  if (weeklyLast >= 6) {
    weeklySheet.getRange(`A6:B${weeklyLast}`).format.numberFormat = DATE_FMT;
    weeklySheet.getRange(`Y6:Y${weeklyLast}`).format.numberFormat = DATETIME_FMT;
  }
  const monthlyLast = lastUsedRow(monthlySheet);
  if (monthlyLast >= 6) {
    monthlySheet.getRange(`A6:C${monthlyLast}`).format.numberFormat = DATE_FMT;
    monthlySheet.getRange(`AA6:AA${monthlyLast}`).format.numberFormat = DATETIME_FMT;
  }
}

// Order/event details are versioned snapshots.  A repeated report with the
// same values is idempotent; a changed FINAL/RESTATED snapshot is appended as
// a new row so the prior evidence remains auditable.
function appendVersionedRows(sheet, tableName, rows, keyFn, knownKeys, fingerprintFn) {
  if (!rows.length) return { added: 0, skipped: 0 };
  const existing = sheet.getUsedRange()?.values || [];
  const fingerprints = new Map();
  for (const row of existing.slice(5)) {
    const key = keyFn(row);
    if (key) fingerprints.set(key, fingerprintFn(row));
  }
  const fresh = [];
  for (const row of rows) {
    const key = keyFn(row);
    if (!key) continue;
    const fp = fingerprintFn(row);
    if (fingerprints.get(key) === fp) continue;
    // If the key already exists with a different snapshot, append it and
    // update the latest fingerprint; do not overwrite the prior row.
    fingerprints.set(key, fp);
    knownKeys.add(key);
    fresh.push(row);
  }
  if (!fresh.length) return { added: 0, skipped: rows.length };
  const table = tableItems(sheet).find((item) => String(item.name || "") === tableName);
  if (table?.rows?.add) table.rows.add(null, fresh);
  else {
    const used = sheet.getUsedRange()?.values || [];
    sheet.getRangeByIndexes(used.length, 0, fresh.length, fresh[0].length).values = fresh;
  }
  return { added: fresh.length, skipped: rows.length - fresh.length };
}

function appendRunNote(sheet, reports, counters, backup) {
  if (!reports.length) return;
  const used = sheet.getUsedRange()?.values || [];
  const start = Math.max(used.length + 1, 18);
  const row = [
    new Date(),
    "canonical workbook sync",
    `追加 ${reports.length} 个报告；订单 ${counters.orders}、财务事件 ${counters.events}、周报 ${counters.weekly}、月报 ${counters.monthly}`,
    "订单/事件/期间行只追加；未知历史行保留",
    `备份：${backup}`,
    reports.map((r) => `${r.kind}:${r.reportId}`).join("；").slice(0, 900),
  ];
  try {
    sheet.getRangeByIndexes(start - 1, 0, 1, 6).values = [row];
    sheet.getRange(`A${start}:F${start}`).format.wrapText = true;
    sheet.getRange(`A${start}:F${start}`).format.rowHeight = 42;
    sheet.getRange(`A${start}`).format.numberFormat = DATETIME_FMT;
  } catch { /* instructions are supplementary; data append already succeeded */ }
}

function compareGeneratedAt(left, right) {
  const a = Date.parse(String(left?.generatedAt || ""));
  const b = Date.parse(String(right?.generatedAt || ""));
  const aa = Number.isFinite(a) ? a : 0;
  const bb = Number.isFinite(b) ? b : 0;
  if (aa !== bb) return aa - bb;
  return String(left?.reportId || "").localeCompare(String(right?.reportId || ""));
}

function compareReportPeriodThenGenerated(left, right) {
  const lp = left?.period || {};
  const rp = right?.period || {};
  const period = `${dateText(lp.to)}|${dateText(lp.from)}`.localeCompare(
    `${dateText(rp.to)}|${dateText(rp.from)}`,
  );
  return period || compareGeneratedAt(left, right);
}

function selectLatestWeeklyReport(reports) {
  return (reports || [])
    .filter((report) => String(report?.kind || "") === "weekly" && !report?.period?.cumulative)
    .reduce(
      (latest, report) => (!latest || compareReportPeriodThenGenerated(report, latest) > 0 ? report : latest),
      null,
    );
}

function firstSummaryNumber(summary, ...keys) {
  for (const key of keys) {
    if (summary?.[key] !== null && summary?.[key] !== undefined && summary?.[key] !== "") {
      return money(summary[key]);
    }
  }
  return null;
}

function sumSummaryRows(rows, ...keys) {
  return money((rows || []).reduce((sum, row) => sum + Number(firstSummaryNumber(row, ...keys) || 0), 0));
}

function buildWeeklyDashboardSnapshot(report, stores = []) {
  if (!report || String(report.kind || "") !== "weekly") return null;
  const period = report.period || {};
  const from = dateText(period.from);
  const to = dateText(period.to);
  const reportStores = new Map(
    (Array.isArray(report.stores) ? report.stores : [])
      .filter((item) => item?.storeAlias)
      .map((item) => [String(item.storeAlias), item]),
  );
  const totals = report.totals || {};
  reportStores.set("ALL", { ...totals, storeAlias: "ALL" });
  const displayNames = new Map(
    (stores || []).map((store) => [String(store.alias || ""), String(store.name || store.alias || "")]),
  );
  if (!displayNames.get("owner-store") || displayNames.get("owner-store") === "owner-store") displayNames.set("owner-store", "老店");
  if (!displayNames.get("new-store") || displayNames.get("new-store") === "new-store") displayNames.set("new-store", "新店");
  const aliases = [
    ...(stores || []).map((store) => String(store.alias || "")).filter(Boolean),
    ...(Array.isArray(report.stores) ? report.stores : []).map((store) => String(store?.storeAlias || "")).filter(Boolean),
    "ALL",
  ].filter((alias, index, list) => list.indexOf(alias) === index);
  const storeRows = aliases.map((alias) => {
    const summary = reportStores.get(alias) || {};
    return [
      alias,
      alias === "ALL" ? "合计" : (displayNames.get(alias) || alias),
      firstSummaryNumber(summary, "orderCount"),
      firstSummaryNumber(summary, "confirmedOrderCount", "completedCount"),
      firstSummaryNumber(summary, "salesAmountGCny"),
      firstSummaryNumber(summary, "actualCommissionCCny", "wbCommissionCny"),
      firstSummaryNumber(summary, "forPayCny"),
      firstSummaryNumber(summary, "combinedPoolCny", "combinedAdvertisingAndReturnPoolCny"),
      firstSummaryNumber(summary, "returnLogisticsCny"),
      firstSummaryNumber(summary, "advertisingProvisionCny"),
      firstSummaryNumber(summary, "ruleWithdrawableCny"),
    ];
  });
  const orders = Array.isArray(report.orders) ? report.orders : [];
  const recognitionRows = aliases.map((alias) => {
    const rows = alias === "ALL" ? orders : orders.filter((order) => String(order?.storeAlias || "") === alias);
    const outcomeCount = (outcome) => rows.filter((order) => String(order?.outcome || "").toLowerCase() === outcome).length;
    return [
      alias === "ALL" ? "合计" : (displayNames.get(alias) || alias),
      rows.length,
      outcomeCount("completed"),
      outcomeCount("returned"),
      outcomeCount("corrected"),
      rows.filter((order) => String(order?.evidenceStatus || "").toUpperCase() !== "COMPLETE").length,
    ];
  });
  const created = orders.filter((order) => {
    const day = dateText(order?.orderCreatedAt);
    return day && day >= from && day <= to;
  });
  const createdComplete = created.filter((order) => String(order?.evidenceStatus || "").toUpperCase() === "COMPLETE");
  const account = sumSummaryRows(createdComplete, "accountIncrementCny", "forPayCny");
  const pool = sumSummaryRows(createdComplete, "combinedPoolCny", "combinedAdvertisingAndReturnPoolCny");
  const returns = sumSummaryRows(createdComplete, "returnLogisticsCny");
  const accountIncrement = firstSummaryNumber(totals, "accountIncrementCny", "forPayCny");
  const forPay = firstSummaryNumber(totals, "forPayCny");
  return {
    reportId: String(report.reportId || ""),
    status: String(report.status || "PENDING_DATA"),
    generatedAt: report.generatedAt || null,
    sourcePath: report.__sourcePath || "",
    from,
    to,
    coreValues: [
      firstSummaryNumber(totals, "orderCount"),
      firstSummaryNumber(totals, "confirmedOrderCount", "completedCount"),
      firstSummaryNumber(totals, "salesAmountGCny"),
      firstSummaryNumber(totals, "actualCommissionCCny", "wbCommissionCny"),
      forPay,
      accountIncrement,
      firstSummaryNumber(totals, "forwardOperationsCny"),
      firstSummaryNumber(totals, "managedOperationsCny"),
      firstSummaryNumber(totals, "combinedPoolCny", "combinedAdvertisingAndReturnPoolCny"),
      firstSummaryNumber(totals, "returnLogisticsCny"),
      firstSummaryNumber(totals, "advertisingProvisionCny"),
      firstSummaryNumber(totals, "ruleWithdrawableCny"),
      firstSummaryNumber(totals, "strictOwnerProfitCny", "ownerProfitCny"),
      accountIncrement == null || forPay == null ? null : money(accountIncrement - forPay),
    ],
    storeRows,
    recognitionRows,
    createdCohortValues: [
      created.length,
      createdComplete.length,
      sumSummaryRows(createdComplete, "salesAmountGCny"),
      sumSummaryRows(createdComplete, "actualCommissionCCny", "wbCommissionCny"),
      Math.max(0, money(pool - returns)),
      money(account - pool),
    ],
  };
}

// The landing sheet contains several independently maintained blocks.  Never
// calculate an append row by counting non-empty cells in a broad range: later
// blocks (for example the cumulative snapshot) would be counted as if they
// belonged to the current section and the next write could overwrite them.
// These helpers keep each section bounded by the next known section title and
// create a clearly labelled continuation block when the existing spacer is
// exhausted.
function hasRowValues(row) {
  return Array.isArray(row) && row.some((value) => value !== null && value !== "");
}

function sectionTitleIndexes(used, token) {
  const needle = String(token || "");
  return (used || []).reduce((indexes, row, index) => {
    if (String(row?.[0] || "").includes(needle)) indexes.push(index);
    return indexes;
  }, []);
}

function nextLandingSectionRow(used, titleRow) {
  for (let index = Math.max(0, titleRow); index < (used || []).length; index += 1) {
    const first = String(used[index]?.[0] || "");
    if (LANDING_SECTION_TITLES.some((token) => first.includes(token))) return index + 1;
  }
  return null;
}

function sectionWindows(sheet, token, width) {
  const used = sheet.getUsedRange()?.values || [];
  const titles = sectionTitleIndexes(used, token);
  return titles.map((titleIndex) => {
    const titleRow = titleIndex + 1;
    const dataStart = titleRow + 2;
    const nextRow = nextLandingSectionRow(used, titleIndex + 1);
    const endRow = nextRow ? nextRow - 1 : used.length;
    const count = Math.max(0, endRow - dataStart + 1);
    const values = count
      ? (sheet.getRangeByIndexes(dataStart - 1, 0, count, width).values || [])
      : [];
    return { titleRow, dataStart, nextRow, endRow, values };
  });
}

function lastNonEmptyRow(window) {
  let last = window.dataStart - 1;
  for (let index = 0; index < (window.values || []).length; index += 1) {
    if (hasRowValues(window.values[index])) last = window.dataStart + index;
  }
  return last;
}

function lastUsedRow(sheet) {
  const used = sheet.getUsedRange()?.values || [];
  for (let index = used.length - 1; index >= 0; index -= 1) {
    if (hasRowValues(used[index])) return index + 1;
  }
  return 0;
}

function writeLandingSectionHeader(sheet, titleRow, width, title, headers, palette) {
  const endColumn = String.fromCharCode(64 + width);
  try { sheet.getRange(`A${titleRow}:${endColumn}${titleRow}`).merge(); } catch { /* already merged */ }
  sheet.getRange(`A${titleRow}`).values = [[title]];
  sheet.getRange(`A${titleRow}:${endColumn}${titleRow}`).format = palette.title;
  sheet.getRange(`A${titleRow + 1}:${endColumn}${titleRow + 1}`).values = [headers];
  sheet.getRange(`A${titleRow + 1}:${endColumn}${titleRow + 1}`).format = palette.header;
}

function collectSectionRows(sheet, token, width) {
  return sectionWindows(sheet, token, width).flatMap((window) => window.values || []);
}

/**
 * Refresh every mutable weekly landing-sheet value from one immutable report
 * snapshot. The detail sheets are append-only and can contain several
 * versions of the same order, so summing them is not a valid latest-week view.
 */
function refreshDashboardFromWeekly(workbook, report, stores) {
  const snapshot = buildWeeklyDashboardSnapshot(report, stores);
  if (!snapshot?.from || !snapshot?.to) {
    return { status: "NO_WEEKLY_PERIOD", reportId: String(report?.reportId || "") };
  }
  const sheet = workbook.worksheets.getItem("周报汇总");
  sheet.getRange("B3").values = [[dateOnly(snapshot.from)]];
  sheet.getRange("D3").values = [[dateOnly(snapshot.to)]];
  sheet.getRange("B3").format.numberFormat = DATE_FMT;
  sheet.getRange("D3").format.numberFormat = DATE_FMT;

  try {
    const policy = workbook.worksheets.getItem("参数与口径");
    if (String(policy.getRange("A9").values?.[0]?.[0] || "").includes("上周起始")) {
      policy.getRange("B9").values = [[dateOnly(snapshot.from)]];
      policy.getRange("B9").format.numberFormat = DATE_FMT;
    }
    if (String(policy.getRange("A10").values?.[0]?.[0] || "").includes("上周结束")) {
      policy.getRange("B10").values = [[dateOnly(snapshot.to)]];
      policy.getRange("B10").format.numberFormat = DATE_FMT;
    }
  } catch { /* parameter mirror is supplementary */ }

  sheet.getRange("B8:B19").values = snapshot.coreValues.slice(0, 12).map((value) => [value]);
  sheet.getRange("B20:B21").values = [[null], [null]];
  sheet.getRange("B22:B23").values = snapshot.coreValues.slice(12, 14).map((value) => [value]);
  if (snapshot.storeRows.length === 3) sheet.getRange("F8:P10").values = snapshot.storeRows;

  try { sheet.getRange("F20:K20").merge(); } catch { /* already merged */ }
  sheet.getRange("F20").values = [["本期订单认列构成（最新周报快照）"]];
  sheet.getRange("F21:K21").values = [["店铺", "报告订单", "完成认列", "退货认列", "更正认列", "开放证据"]];
  if (snapshot.recognitionRows.length === 3) sheet.getRange("F22:K24").values = snapshot.recognitionRows;
  try { sheet.getRange("F25:K25").merge(); } catch { /* already merged */ }
  sheet.getRange("F25").values = [["本区直接取最新周报版本；报告可能包含本周认列的历史订单更正，不按 append-only 订单明细重复求和。"]];

  try { sheet.getRange("A27:D27").merge(); } catch { /* already merged */ }
  sheet.getRange("A27").values = [["本期新建订单队列（按订单创建日）"]];
  sheet.getRange("B29:B34").values = snapshot.createdCohortValues.map((value) => [value]);
  sheet.getRange("D29").values = [["最新周报内、订单创建日位于本期的去重订单"]];

  const previous = String(sheet.getRange("A2").values?.[0]?.[0] || "");
  let subtitle = previous
    .replace(/\n最近自动增补：[\s\S]*$/, "")
    .replace(/周报快照为\s*\d{4}-\d{2}-\d{2}[—-]\d{4}-\d{2}-\d{2}/, `周报快照为 ${snapshot.from}—${snapshot.to}`)
    .replace(/上周\s*\d{4}-\d{2}-\d{2}[—-]\d{4}-\d{2}-\d{2}/, `上周 ${snapshot.from}—${snapshot.to}`);
  if (!subtitle.includes(`${snapshot.from}—${snapshot.to}`)) {
    subtitle = `${subtitle}\n当前周报快照：${snapshot.from}—${snapshot.to}`;
  }
  sheet.getRange("A2").values = [[subtitle]];
  sheet.getRange("H3").values = [[
    `模型状态：最新周报 ${snapshot.status}；reportId=${snapshot.reportId}；开放项与来源状态见“周报历史/来源校验”，不把缺失证据当作 0。`,
  ]];
  return {
    status: "REFRESHED",
    reportId: snapshot.reportId,
    sourcePath: snapshot.sourcePath,
    from: snapshot.from,
    to: snapshot.to,
  };
}

/**
 * Refresh the mutable dashboard view from the newest cumulative bootstrap.
 *
 * Detail and history tables are append-only snapshots.  Summing those tables
 * directly can double-count a corrected order after a RESTATED/bootstrap
 * rerun.  This small source-backed block stores one current aggregate per
 * store (plus ALL), and the visible cumulative dashboard cells reference it.
 * Existing weekly/monthly history rows are never edited or deleted.
 */
function refreshDashboardFromBootstrap(workbook, report, stores) {
  const sheet = workbook.worksheets.getItem("周报汇总");
  const reportStores = Array.isArray(report?.stores) ? report.stores : [];
  const byAlias = new Map(
    reportStores
      .filter((item) => item && item.storeAlias)
      .map((item) => [String(item.storeAlias), item]),
  );
  if (report?.totals) byAlias.set("ALL", { ...report.totals, storeAlias: "ALL" });
  const aliases = [
    ...(stores || []).map((item) => String(item.alias || "")).filter(Boolean),
    "ALL",
  ].filter((alias, index, list) => list.indexOf(alias) === index);
  const usableAliases = aliases.filter((alias) => byAlias.has(alias));
  if (!usableAliases.length) return { status: "NO_BOOTSTRAP_STORES", reportId: String(report?.reportId || "") };

  const used = sheet.getUsedRange()?.values || [];
  let titleRow = used.findIndex((row) => String(row?.[0] || "").includes("当前开店至今聚合快照")) + 1;
  if (titleRow <= 0) titleRow = Math.max(50, used.length + 3);

  // If this block already exists, clear only its own previous contents.  The
  // end marker is written in the note row below; no adjacent debt/cash block
  // is touched even when the sheet has grown over several runs.
  if (used.findIndex((row) => String(row?.[0] || "").includes("当前开店至今聚合快照")) >= 0) {
    let oldEnd = titleRow + 4;
    for (let i = titleRow; i < Math.min(used.length, titleRow + 100); i += 1) {
      if (String(used[i]?.[0] || "").includes("bootstrapReportId=")) {
        oldEnd = i + 1;
        break;
      }
    }
    try { sheet.getRange(`A${titleRow}:T${oldEnd}`).clear({ applyTo: "contents" }); } catch { /* best effort; overwrite below */ }
  }

  const headerRow = titleRow + 1;
  const dataStart = titleRow + 2;
  const noteRow = dataStart + usableAliases.length;
  const headers = [
    "店铺标识", "店铺名称", "订单数", "已完成数", "退货数", "G营业额", "平台佣金C", "直接到账",
    "账户增量", "正向物流", "代运营费2%", "广告+退货池", "退/取消物流", "广告拨备/可分配活动推广",
    "按规则可提现", "严格公式利润", "报告状态", "报告ID", "生成时间", "来源文件",
  ];
  const storeNameByAlias = new Map((stores || []).map((item) => [String(item.alias || ""), String(item.name || item.alias || "")]));
  // Older credential-free registries contain aliases/opening dates only. Keep
  // the established Chinese labels in the visible snapshot when no display
  // name is configured, while retaining the immutable alias as the key.
  if (!storeNameByAlias.get("owner-store") || storeNameByAlias.get("owner-store") === "owner-store") storeNameByAlias.set("owner-store", "老店");
  if (!storeNameByAlias.get("new-store") || storeNameByAlias.get("new-store") === "new-store") storeNameByAlias.set("new-store", "新店");
  const rows = usableAliases.map((alias) => {
    const s = byAlias.get(alias) || {};
    const pool = money(s.combinedPoolCny ?? s.combinedAdvertisingAndReturnPoolCny);
    const returns = money(s.returnLogisticsCny);
    const provision = money(s.advertisingProvisionCny ?? Math.max(0, Number(pool || 0) - Number(returns || 0)));
    return [
      alias,
      alias === "ALL" ? "合计" : (storeNameByAlias.get(alias) || alias),
      num(s.orderCount),
      num(s.completedCount ?? s.confirmedOrderCount),
      num(s.returnedCount),
      money(s.salesAmountGCny),
      money(s.actualCommissionCCny ?? s.wbCommissionCny),
      money(s.forPayCny),
      money(s.accountIncrementCny ?? s.forPayCny),
      money(s.forwardOperationsCny),
      money(s.managedOperationsCny),
      pool,
      returns,
      provision,
      money(s.ruleWithdrawableCny),
      money(s.strictOwnerProfitCny ?? s.ownerProfitCny),
      String(s.status || report.status || "PENDING_DATA"),
      String(report.reportId || ""),
      dateTime(report.generatedAt),
      report.__sourcePath || "",
    ];
  });

  try { sheet.getRange(`A${titleRow}:T${titleRow}`).merge(); } catch { /* already merged */ }
  sheet.getRange(`A${titleRow}`).values = [["当前开店至今聚合快照（最新 bootstrap；仅刷新 dashboard，不改历史）"]];
  sheet.getRange(`A${titleRow}:T${titleRow}`).format = { fill: "#2F75B5", font: { bold: true, color: "#FFFFFF" } };
  sheet.getRange(`A${headerRow}:T${headerRow}`).values = [headers];
  sheet.getRange(`A${headerRow}:T${headerRow}`).format = { fill: "#D9EAF7", font: { bold: true, color: "#17365D" }, wrapText: true };
  sheet.getRange(`A${dataStart}:T${noteRow - 1}`).format = { wrapText: true, rowHeight: 32, borders: { preset: "all", style: "thin", color: "#D9E2F3" } };
  sheet.getRange(`A${dataStart}:T${noteRow - 1}`).values = rows;
  sheet.getRange(`C${dataStart}:E${noteRow - 1}`).format.numberFormat = "#,##0;[Red](#,##0);-";
  sheet.getRange(`F${dataStart}:P${noteRow - 1}`).format.numberFormat = MONEY_FMT;
  sheet.getRange(`S${dataStart}:S${noteRow - 1}`).format.numberFormat = DATETIME_FMT;
  try { sheet.getRange(`A${noteRow}:T${noteRow}`).merge(); } catch { /* already merged */ }
  sheet.getRange(`A${noteRow}`).values = [[
    `bootstrapReportId=${report.reportId}; generatedAt=${report.generatedAt || ""}; status=${report.status || "PENDING_DATA"}; source=${report.__sourcePath || ""}；当前 dashboard 累计值直接引用本快照，避免把 append-only 订单/历史快照重复求和；周报/月报历史行保持不变。`,
  ]];
  sheet.getRange(`A${noteRow}:T${noteRow}`).format = { fill: "#FFF2CC", font: { color: "#7F6000" }, wrapText: true, rowHeight: 42 };
  // Keep the source block readable without touching existing dashboard widths.
  const widths = { A: 16, B: 13, C: 10, D: 11, E: 10, F: 14, G: 14, H: 14, I: 14, J: 12, K: 12, L: 14, M: 13, N: 17, O: 14, P: 14, Q: 13, R: 22, S: 18, T: 42 };
  for (const [column, width] of Object.entries(widths)) {
    try { sheet.getRange(`${column}:${column}`).format.columnWidth = Math.max(Number(sheet.getRange(`${column}:${column}`).format.columnWidth || 0), width); } catch { /* optional style */ }
  }

  const rowByAlias = new Map(usableAliases.map((alias, index) => [alias, dataStart + index]));
  const ref = (alias, column) => {
    const row = rowByAlias.get(alias);
    return row ? `='周报汇总'!$${column}$${row}` : "=0";
  };
  const all = rowByAlias.get("ALL");
  if (all) {
    const coreMap = { 8: "C", 9: "D", 10: "F", 11: "G", 12: "H", 13: "I", 14: "J", 15: "K", 16: "L", 17: "M", 18: "N", 19: "O", 22: "P" };
    for (const [row, column] of Object.entries(coreMap)) sheet.getRange(`C${row}`).formulas = [[ref("ALL", column)]];
    sheet.getRange("C21").formulas = [["=C19-C20"]];
    sheet.getRange("C23").formulas = [["=C13-C12"]];
  }
  const dashboardRows = { "owner-store": 15, "new-store": 16, ALL: 17 };
  const storeMap = { H: "C", I: "D", J: "F", K: "G", L: "H", M: "L", N: "M", O: "N", P: "O" };
  for (const [alias, dashboardRow] of Object.entries(dashboardRows)) {
    if (!rowByAlias.has(alias)) continue;
    for (const [dashboardColumn, snapshotColumn] of Object.entries(storeMap)) {
      sheet.getRange(`${dashboardColumn}${dashboardRow}`).formulas = [[ref(alias, snapshotColumn)]];
    }
  }
  return {
    status: "REFRESHED",
    reportId: String(report.reportId || ""),
    sourcePath: report.__sourcePath || "",
    titleRow,
    dataStart,
    dataEnd: noteRow - 1,
    stores: usableAliases,
  };
}

// Current settlement-balance advertising reserves and immutable legacy
// withdrawal-linked debt are shown together without rewriting the old landing
// section.  From the 2026-09-05 cutover, new withdrawals are independent of
// advertising; payment/FIFO fields below apply only to historical debt.
function appendFundingDebtSection(workbook, reports, debtSnapshot = null) {
  const rows = [];
  for (const report of reports) {
    for (const funding of report.funding || []) {
      if (!funding || !funding.storeAlias) continue;
      rows.push({ report, funding });
    }
  }
  if (debtSnapshot && Array.isArray(debtSnapshot.stores)) {
    const snapshotId = crypto.createHash("sha256").update(JSON.stringify(debtSnapshot)).digest("hex");
    for (const funding of debtSnapshot.stores) {
      if (!funding || !funding.storeAlias) continue;
      rows.push({
        report: {
          reportId: snapshotId,
          kind: "debt_snapshot",
          status: "DEBT_SNAPSHOT",
          generatedAt: debtSnapshot.generatedAt || new Date().toISOString(),
          period: { from: debtSnapshot.asOf || "", to: debtSnapshot.asOf || "" },
        },
        funding: {
          ...funding,
          note: funding.note || "2026-09-05 前历史提现广告欠费快照；仅历史 advertising payments 证据按 FIFO 冲账",
        },
      });
    }
  }
  if (!rows.length) return 0;
  const sheet = workbook.worksheets.getItem("周报汇总");
  const headers = [
    "报告ID", "期间/状态", "店铺", "平台可提现余额", "未分配广告/退货储备", "已批准/执行中余额划拨", "实际广告消耗", "平台周期广告扣款(仅对账)", "历史欠广告费", "历史广告付款回读", "历史已冲账", "历史未冲账", "历史欠费后可提现", "说明",
  ];
  const title = "结算余额广告储备与历史欠费对账（当前余额划拨规则 + 旧提现债务；各店分开）";
  const palette = {
    title: { fill: "#1F4E78", font: { bold: true, color: "#FFFFFF" } },
    header: { fill: "#D9EAF7", font: { bold: true, color: "#17365D" }, wrapText: true },
  };
  let windows = sectionWindows(sheet, "结算余额广告储备与历史欠费对账", headers.length);
  if (!windows.length) {
    const titleRow = lastUsedRow(sheet) + 3;
    writeLandingSectionHeader(sheet, titleRow, headers.length, title, headers, palette);
    windows = sectionWindows(sheet, "结算余额广告储备与历史欠费对账", headers.length);
  }
  const fingerprints = new Map();
  for (const row of collectSectionRows(sheet, "结算余额广告储备与历史欠费对账", headers.length)) {
    const key = `${String(row?.[0] || "")}|${String(row?.[2] || "")}`;
    if (key !== "|") fingerprints.set(key, JSON.stringify(row.slice(0, 13).map(normalizeValue)));
  }
  const fresh = [];
  for (const item of rows) {
    const r = item.report; const f = item.funding;
    const reportId = String(r.reportId || ""); const alias = String(f.storeAlias || "");
    const debtStatus = String(f.advertisingDebtStatus || "KNOWN");
    const unverified = Number(f.advertisingFeeUnverifiedCount || 0) > 0;
    const debtKnown = f.advertisingDebtAmountKnown !== false
      && !unverified
      && debtStatus !== "PENDING_DATA";
    const debtDisplay = debtKnown
      ? money(f.advertisingDebtOutstandingCny)
      : (f.advertisingDebtUnknownAmountCny || "待核实");
    const afterDebtDisplay = debtKnown
      ? money(f.withdrawableAfterAdvertisingDebtCny ?? f.suggestedWithdrawalAfterAdvertisingDebtCny)
      : "待核实";
    const values = [
      reportId,
      `${r.period?.from || r.from || ""}—${r.period?.to || r.to || ""} / ${r.status || "PENDING_DATA"}`,
      alias,
      money(f.withdrawableBalanceCny),
      money(f.unallocatedAdvertisingReserveCny),
      money(f.approvedPendingCampaignAllocationCny ?? f.approvedPendingDepositCny),
      money(f.actualAdvertisingSpendCny),
      money(f.advertisingSettlementDeductionCny),
      debtDisplay,
      money(f.advertisingPaymentReadbackCny),
      money(f.advertisingPaymentAppliedCny),
      money(f.advertisingPaymentUnappliedCny),
      afterDebtDisplay,
      `${f.note || "2026-09-05 起活动预算直接从 balance(type=1) 划拨，平台按结算周期扣款；无需先提现或现金充值；旧提现欠费单列保留"}；历史广告欠费状态=${debtStatus}${debtKnown ? "" : "（金额待核实，不能按零处理）"}`,
    ];
    const key = `${reportId}|${alias}`;
    const fp = JSON.stringify(values.slice(0, 13).map(normalizeValue));
    if (fingerprints.get(key) === fp) continue;
    fingerprints.set(key, fp);
    fresh.push(values);
  }
  if (!fresh.length) return 0;
  // Prefer the most recent continuation/original block.  Its window ends at
  // the next landing section title, so later blocks cannot be overwritten.
  let target = windows[windows.length - 1];
  let end = lastNonEmptyRow(target) + 1;
  const available = target.nextRow ? target.nextRow - end : Number.POSITIVE_INFINITY;
  if (fresh.length > available) {
    const continuationRow = lastUsedRow(sheet) + 3;
    writeLandingSectionHeader(
      sheet,
      continuationRow,
      headers.length,
      `${title}（续）`,
      headers,
      palette,
    );
    target = {
      titleRow: continuationRow,
      dataStart: continuationRow + 2,
      nextRow: null,
      endRow: lastUsedRow(sheet),
      values: [],
    };
    end = target.dataStart;
  }
  sheet.getRangeByIndexes(end - 1, 0, fresh.length, 14).values = fresh;
  sheet.getRange(`D${end}:M${end + fresh.length - 1}`).format.numberFormat = MONEY_FMT;
  sheet.getRange(`A${end}:N${end + fresh.length - 1}`).format = { wrapText: true, rowHeight: 42, borders: { preset: "all", style: "thin", color: "#D9E2F3" } };
  sheet.getRange(`A${target.titleRow}:N${end + fresh.length - 1}`).format.columnWidth = 18;
  sheet.getRange(`N${target.titleRow}:N${end + fresh.length - 1}`).format.columnWidth = 42;
  return fresh.length;
}

// Keep each user-supplied portal withdrawal visible at record level.  The
// aggregate debt snapshot above is useful for allocation, but it intentionally
// does not replace the underlying immutable request rows.  A later status
// correction (for example processing -> readback_confirmed) is appended as a
// new snapshot row so the workbook remains auditable and never overwrites the
// earlier evidence.
function appendWithdrawalRecordSection(workbook, debtSnapshot = null) {
  if (!debtSnapshot || !Array.isArray(debtSnapshot.stores)) return 0;
  const rows = [];
  for (const store of debtSnapshot.stores) {
    if (!store || !store.storeAlias || !Array.isArray(store.withdrawals)) continue;
    for (const withdrawal of store.withdrawals) {
      if (!withdrawal || !withdrawal.record_id) continue;
      const evidence = withdrawal.evidence || {};
      const latest = withdrawal.latest_status_event || {};
      const currentStatus = String(withdrawal.current_status || withdrawal.status || "reported");
      const feeStatus = String(withdrawal.advertising_fee_status || "unknown");
      const unknownFee = feeStatus === "unknown" || feeStatus === "";
      const settlementBalance = feeStatus === "settlement_balance";
      rows.push({
        key: [
          String(withdrawal.record_id),
          currentStatus,
          String(latest.occurred_at || withdrawal.withdrawn_at || ""),
        ].join("|"),
        values: [
          String(withdrawal.record_id),
          String(store.storeAlias),
          String(withdrawal.external_reference || withdrawal.idempotency_key || ""),
          money(withdrawal.withdrawal_amount_cny),
          dateOnly(withdrawal.withdrawn_at),
          currentStatus,
          settlementBalance ? "不适用（结算余额扣款）" : feeStatus,
          settlementBalance ? "不适用" : (unknownFee ? "待核实" : money(withdrawal.advertising_fee_due_cny)),
          settlementBalance ? "不适用" : (unknownFee ? "待核实" : money(withdrawal.advertising_fee_withheld_cny)),
          settlementBalance ? "不适用" : (unknownFee ? "待核实" : money(withdrawal.outstanding_advertising_fee_cny)),
          String(evidence.screenshot || ""),
          `portalStatus=${String(evidence.portalStatus || "")}; datePrecision=${String(evidence.datePrecision || "")}; source=${String(withdrawal.source_type || "user_provided")}`,
        ],
      });
    }
  }
  if (!rows.length) return 0;

  const sheet = workbook.worksheets.getItem("周报汇总");
  const headers = [
    "提现记录ID(账本)", "店铺", "平台申请单ID", "金额(CNY)", "申请日期(MSK)",
    "当前状态", "广告费扣除状态", "应扣广告费", "实际扣除广告费", "当前欠广告费",
    "截图证据", "备注",
  ];
  const title = "提现记录明细（平台截图/人工回填；各店分开）";
  const palette = {
    title: { fill: "#8064A2", font: { bold: true, color: "#FFFFFF" } },
    header: { fill: "#E4DFEC", font: { bold: true, color: "#403152" }, wrapText: true },
  };
  let windows = sectionWindows(sheet, "提现记录明细", headers.length);
  if (!windows.length) {
    const titleRow = lastUsedRow(sheet) + 3;
    writeLandingSectionHeader(sheet, titleRow, headers.length, title, headers, palette);
    windows = sectionWindows(sheet, "提现记录明细", headers.length);
  }
  const known = new Set(
    collectSectionRows(sheet, "提现记录明细", headers.length)
      .map((row) => [String(row?.[0] || ""), String(row?.[5] || ""), dateText(row?.[4])].join("|"))
      .filter((key) => key !== "||"),
  );
  const fresh = [];
  for (const row of rows) {
    if (known.has(row.key)) continue;
    known.add(row.key);
    fresh.push(row.values);
  }
  if (!fresh.length) return 0;

  let target = windows[windows.length - 1];
  let end = lastNonEmptyRow(target) + 1;
  const available = target.nextRow ? target.nextRow - end : Number.POSITIVE_INFINITY;
  if (fresh.length > available) {
    const continuationRow = lastUsedRow(sheet) + 3;
    writeLandingSectionHeader(
      sheet,
      continuationRow,
      headers.length,
      `${title}（续）`,
      headers,
      palette,
    );
    target = {
      titleRow: continuationRow,
      dataStart: continuationRow + 2,
      nextRow: null,
      endRow: lastUsedRow(sheet),
      values: [],
    };
    end = target.dataStart;
  }
  sheet.getRangeByIndexes(end - 1, 0, fresh.length, headers.length).values = fresh;
  sheet.getRange(`D${end}:D${end + fresh.length - 1}`).format.numberFormat = MONEY_FMT;
  sheet.getRange(`E${end}:E${end + fresh.length - 1}`).format.numberFormat = DATE_FMT;
  sheet.getRange(`A${end}:L${end + fresh.length - 1}`).format = {
    wrapText: true,
    rowHeight: 48,
    borders: { preset: "all", style: "thin", color: "#D9E2F3" },
  };
  const widths = { A: 28, B: 14, C: 40, D: 14, E: 15, F: 16, G: 18, H: 16, I: 18, J: 16, K: 58, L: 46 };
  for (const [column, width] of Object.entries(widths)) {
    try { sheet.getRange(`${column}:${column}`).format.columnWidth = Math.max(Number(sheet.getRange(`${column}:${column}`).format.columnWidth || 0), width); } catch { /* optional style */ }
  }
  return fresh.length;
}

// Historical withdrawals supplied outside the finance API remain visible as
// immutable cash events.  They are intentionally kept separate from the ad
// debt section: the old screenshot proves the cash movement, but does not
// prove whether an advertising fee was withheld, so no debt is inferred.
function appendHistoricalCashSection(workbook, cashEvents) {
  const events = Array.isArray(cashEvents?.events) ? cashEvents.events : [];
  const withdrawals = events.filter((event) =>
    event && String(event.eventType || "").toLowerCase() === "withdrawal" && event.eventId,
  );
  if (!withdrawals.length) return 0;
  const sheet = workbook.worksheets.getItem("周报汇总");
  const headers = [
    "事件ID", "店铺", "事件类型", "金额(CNY)", "申请/发生时间", "结算时间", "状态", "来源与会计处理",
  ];
  const title = "历史人工提现/现金事件（用户提供证据；不推断广告费扣除）";
  const palette = {
    title: { fill: "#8064A2", font: { bold: true, color: "#FFFFFF" } },
    header: { fill: "#E4DFEC", font: { bold: true, color: "#403152" }, wrapText: true },
  };
  let windows = sectionWindows(sheet, "历史人工提现/现金事件", headers.length);
  if (!windows.length) {
    const titleRow = lastUsedRow(sheet) + 3;
    writeLandingSectionHeader(sheet, titleRow, headers.length, title, headers, palette);
    windows = sectionWindows(sheet, "历史人工提现/现金事件", headers.length);
  }
  const known = new Set(
    collectSectionRows(sheet, "历史人工提现/现金事件", headers.length)
      .map((row) => String(row?.[0] || ""))
      .filter(Boolean),
  );
  const fresh = [];
  for (const event of withdrawals) {
    const id = String(event.eventId);
    if (known.has(id)) continue;
    known.add(id);
    fresh.push([
      id,
      String(event.store || event.storeAlias || ""),
      "withdrawal",
      money(event.amountCny),
      dateOnly(event.requestedAt || event.withdrawnAt),
      dateOnly(event.settledAt),
      String(event.status || "reported"),
      `${event.sourceType || "user-provided"}；${event.accountingTreatment || "仅从对应店铺累计可提现中扣除一次；2026-09-05 起广告周期扣款与提现分账"}`,
    ]);
  }
  if (!fresh.length) return 0;
  let target = windows[windows.length - 1];
  let end = lastNonEmptyRow(target) + 1;
  const available = target.nextRow ? target.nextRow - end : Number.POSITIVE_INFINITY;
  if (fresh.length > available) {
    const continuationRow = lastUsedRow(sheet) + 3;
    writeLandingSectionHeader(
      sheet,
      continuationRow,
      headers.length,
      `${title}（续）`,
      headers,
      palette,
    );
    target = {
      titleRow: continuationRow,
      dataStart: continuationRow + 2,
      nextRow: null,
      endRow: lastUsedRow(sheet),
      values: [],
    };
    end = target.dataStart;
  }
  sheet.getRangeByIndexes(end - 1, 0, fresh.length, 8).values = fresh;
  sheet.getRange(`D${end}:D${end + fresh.length - 1}`).format.numberFormat = MONEY_FMT;
  sheet.getRange(`A${end}:H${end + fresh.length - 1}`).format = { wrapText: true, rowHeight: 48, borders: { preset: "all", style: "thin", color: "#D9E2F3" } };
  sheet.getRange(`A${target.titleRow}:H${end + fresh.length - 1}`).format.columnWidth = 18;
  sheet.getRange(`H${target.titleRow}:H${end + fresh.length - 1}`).format.columnWidth = 58;
  return fresh.length;
}

function updateLandingNote(workbook, reports) {
  if (!reports.length) return;
  try {
    const sheet = workbook.worksheets.getItem("周报汇总");
    const previous = String(sheet.getRange("A2").values?.[0]?.[0] || "");
    const note = `\n最近自动增补：${reports.map((r) => {
      const period = r.period || {};
      return `${r.kind} ${r.from || period.from || ""}—${r.to || period.to || ""} ${r.status}`;
    }).join("；")}`;
    const base = previous.replace(/\n最近自动增补：[\s\S]*$/, "");
    sheet.getRange("A2").values = [[base + note]];
  } catch { /* landing text must not block append */ }
}

async function verifyExport(file, reports, counters, latestBootstrap = null, latestWeekly = null, stores = []) {
  const wb = await SpreadsheetFile.importXlsx(await FileBlob.load(file));
  const errors = await wb.inspect({ kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!", options: { useRegex: true, maxResults: 300 }, summary: "canonical workbook sync formula scan" });
  if ((errors.ndjson || "").includes('"kind":"match"')) throw new Error(`formula error scan failed: ${errors.ndjson}`);
  const source = wb.worksheets.getItem("来源校验").getUsedRange()?.values || [];
  const text = source.flat().map((v) => String(v || ""));
  const missing = reports.filter((r) => !text.some((value) => value.includes(String(r.reportId))));
  if (missing.length) throw new Error(`readback missing report IDs: ${missing.map((r) => r.reportId).join(", ")}`);
  const orderCount = wb.worksheets.getItem("订单明细").getUsedRange()?.values?.length || 0;
  if (counters.orders > 0 && orderCount < 6 + counters.orders) {
    // Existing workbooks may have a footer below the table, so this is only a
    // sanity lower bound rather than an exact row-count assertion.
    throw new Error(`readback order detail unexpectedly short: ${orderCount}`);
  }
  verifySourceValidationLayout(wb);
  verifyDashboardSnapshotLayout(wb, stores);
  if (latestBootstrap) verifyDashboardBootstrap(wb, latestBootstrap);
  if (latestWeekly) verifyDashboardWeekly(wb, latestWeekly, stores);
}

function verifyDashboardWeekly(workbook, report, stores = []) {
  const snapshot = buildWeeklyDashboardSnapshot(report, stores);
  const sheet = workbook.worksheets.getItem("周报汇总");
  if (dateText(sheet.getRange("B3").values?.[0]?.[0]) !== snapshot.from) {
    throw new Error(`dashboard weekly start mismatch: expected ${snapshot.from}`);
  }
  if (dateText(sheet.getRange("D3").values?.[0]?.[0]) !== snapshot.to) {
    throw new Error(`dashboard weekly end mismatch: expected ${snapshot.to}`);
  }
  const actualCore = [
    ...(sheet.getRange("B8:B19").values || []).flat(),
    ...(sheet.getRange("B22:B23").values || []).flat(),
  ];
  for (let index = 0; index < snapshot.coreValues.length; index += 1) {
    const expected = snapshot.coreValues[index];
    const actual = actualCore[index];
    if (expected === null || expected === undefined || expected === "") continue;
    if (Math.abs(Number(actual) - Number(expected)) > 0.011) {
      throw new Error(`dashboard weekly metric mismatch at index ${index}: ${actual} != ${expected}`);
    }
  }
  const reportId = String(report?.reportId || "");
  if (!String(sheet.getRange("H3").values?.[0]?.[0] || "").includes(reportId)) {
    throw new Error(`dashboard weekly report ID missing: ${reportId}`);
  }
}

function verifySourceValidationLayout(workbook) {
  const sheet = workbook.worksheets.getItem("来源校验");
  const expectedLabels = [
    "历史提现证据",
    "促销额度处理",
    "确认行 G 时间匹配缺口",
    "确认行成本/版本缺口",
    "确认行 C 勾稽非零数",
    "确认行到账勾稽非零数",
    "确认行利润勾稽非零数",
    "确认行费率快照缺口",
  ];
  const labels = sheet.getRange("A50:A57").values || [];
  for (let index = 0; index < expectedLabels.length; index += 1) {
    if (String(labels[index]?.[0] || "") !== expectedLabels[index]) {
      throw new Error(`source validation label missing at A${50 + index}: ${expectedLabels[index]}`);
    }
  }
  const formulaCells = [
    "D50", "F50", "D51", "F51",
    "B52", "D52", "F52", "B53", "D53", "F53",
    "B54", "D54", "F54", "B55", "D55", "F55",
    "B56", "D56", "F56", "B57", "D57", "F57", "B60",
  ];
  for (const address of formulaCells) {
    const formula = sheet.getRange(address).formulas?.[0]?.[0];
    if (typeof formula !== "string" || !formula.startsWith("=")) {
      throw new Error(`source validation formula missing at ${address}`);
    }
  }
  if (String(sheet.getRange("A60").values?.[0]?.[0] || "") !== "模型状态：") {
    throw new Error("source model-status label missing at A60");
  }
}

function verifyDashboardSnapshotLayout(workbook, stores = []) {
  const sheet = workbook.worksheets.getItem("周报汇总");
  const used = sheet.getUsedRange()?.values || [];
  const titleIndex = used.findIndex((row) => String(row?.[0] || "").includes("当前开店至今聚合快照"));
  if (titleIndex < 0) return;
  const expectedHeaders = [
    "店铺标识", "店铺名称", "订单数", "已完成数", "退货数", "G营业额", "平台佣金C", "直接到账",
    "账户增量", "正向物流", "代运营费2%", "广告+退货池", "退/取消物流", "广告拨备/可分配活动推广",
    "按规则可提现", "严格公式利润", "报告状态", "报告ID", "生成时间", "来源文件",
  ];
  const header = used[titleIndex + 1] || [];
  for (let index = 0; index < expectedHeaders.length; index += 1) {
    if (String(header[index] || "") !== expectedHeaders[index]) {
      throw new Error(`dashboard snapshot header mismatch at column ${index + 1}`);
    }
  }
  const aliases = new Set([
    ...(stores || []).map((store) => String(store.alias || "")).filter(Boolean),
    "ALL",
  ]);
  const dataRows = [];
  for (let index = titleIndex + 2; index < used.length; index += 1) {
    const row = used[index] || [];
    const first = String(row[0] || "");
    if (first.startsWith("bootstrapReportId=")) break;
    if (!hasRowValues(row)) continue;
    dataRows.push(row);
  }
  for (const row of dataRows) {
    if (!aliases.has(String(row[0] || ""))) {
      throw new Error(`dashboard snapshot row has invalid store key: ${String(row[0] || "")}`);
    }
    if (row.length < expectedHeaders.length) {
      throw new Error("dashboard snapshot row is shorter than its header");
    }
    if (String(row[1] || "").includes(" / ") || String(row[1] || "").includes("PENDING_DATA")) {
      throw new Error("dashboard snapshot row appears to be a debt/history row");
    }
  }
  const allRow = dataRows.find((row) => String(row[0] || "") === "ALL");
  if (!allRow) return;
  const coreMap = { 8: 2, 9: 3, 10: 5, 11: 6, 12: 7, 13: 8, 14: 9, 15: 10, 16: 11, 17: 12, 18: 13, 19: 14, 22: 15 };
  for (const [dashboardRow, snapshotIndex] of Object.entries(coreMap)) {
    const actual = sheet.getRange(`C${dashboardRow}`).values?.[0]?.[0];
    const expected = allRow[snapshotIndex];
    if (expected === null || expected === undefined || expected === "") continue;
    const actualNumber = Number(actual);
    const expectedNumber = Number(expected);
    if (Number.isFinite(actualNumber) && Number.isFinite(expectedNumber)) {
      if (Math.abs(actualNumber - expectedNumber) > 0.011) {
        throw new Error(`dashboard cumulative formula mismatch C${dashboardRow}: ${actual} != ${expected}`);
      }
    } else if (String(actual || "") !== String(expected || "")) {
      throw new Error(`dashboard cumulative formula mismatch C${dashboardRow}`);
    }
  }
}

function verifyDashboardBootstrap(workbook, report) {
  const sheet = workbook.worksheets.getItem("周报汇总");
  const values = sheet.getRange("F14:P17").values || [];
  const byAlias = new Map(values.slice(1).map((row) => [String(row?.[0] || ""), row]));
  const reportStores = new Map(
    (report.stores || []).filter((item) => item?.storeAlias).map((item) => [String(item.storeAlias), item]),
  );
  if (report.totals) reportStores.set("ALL", { ...report.totals, storeAlias: "ALL" });
  for (const alias of ["owner-store", "new-store", "ALL"]) {
    const expected = reportStores.get(alias);
    const actual = byAlias.get(alias);
    if (!expected || !actual) continue;
    const checks = [
      [2, expected.orderCount],
      [3, expected.completedCount ?? expected.confirmedOrderCount],
      [4, expected.salesAmountGCny],
      [5, expected.actualCommissionCCny ?? expected.wbCommissionCny],
      [6, expected.forPayCny],
      [7, expected.combinedPoolCny ?? expected.combinedAdvertisingAndReturnPoolCny],
      [8, expected.returnLogisticsCny],
      [9, expected.advertisingProvisionCny],
      [10, expected.ruleWithdrawableCny],
    ];
    for (const [index, value] of checks) {
      if (value === null || value === undefined || value === "") continue;
      if (Math.abs(Number(actual[index]) - Number(value)) > 0.011) {
        throw new Error(`dashboard bootstrap mismatch ${alias} column ${index}: ${actual[index]} != ${value}`);
      }
    }
  }
  const notes = (sheet.getUsedRange()?.values || []).flat().map((value) => String(value || ""));
  if (!notes.some((value) => value.includes("bootstrapReportId=") && value.includes(String(report.reportId || "")))) {
    throw new Error(`dashboard bootstrap source note missing report ID ${report.reportId}`);
  }
}

async function replaceWithBackup(temp, target) {
  // Artifact-tool output can carry the isolated runtime's protected Windows
  // DACL.  Copy only its bytes into a newly created sibling so the file that
  // reaches the canonical path inherits the canonical directory's ACL.
  const publishTemp = `${target}.publish-${process.pid}-${Date.now()}`;
  try {
    await copyBytesToFreshFile(temp, publishTemp);
    await fs.unlink(temp);
  } catch (error) {
    try { await fs.unlink(temp); } catch { /* best effort */ }
    try { await fs.unlink(publishTemp); } catch { /* best effort */ }
    throw error;
  }
  try {
    await fs.rename(publishTemp, target);
    return;
  } catch (firstError) {
    // Windows may refuse rename-over-existing. The original file has already
    // been copied to a timestamped backup, so an overwrite copy is recoverable.
    try {
      await fs.copyFile(publishTemp, target);
      await fs.unlink(publishTemp);
      return;
    } catch (secondError) {
      try { await fs.unlink(publishTemp); } catch { /* best effort */ }
      throw new Error(`cannot replace canonical workbook: ${firstError}; fallback: ${secondError}`);
    }
  }
}

async function copyBytesToFreshFile(source, target) {
  const bytes = await fs.readFile(source);
  let handle;
  let created = false;
  try {
    handle = await fs.open(target, "wx", 0o666);
    created = true;
    await handle.writeFile(bytes);
    await handle.sync();
  } catch (error) {
    if (handle) {
      try { await handle.close(); } catch { /* best effort */ }
      handle = undefined;
    }
    if (created) {
      try { await fs.unlink(target); } catch { /* best effort */ }
    }
    throw error;
  } finally {
    if (handle) await handle.close();
  }
}

function tableItems(sheet) { try { return sheet.tables.items || []; } catch { return []; } }

// Header lookup is kept local to the bridge so existing/translated workbook
// labels can be extended without changing the report service.
function headerMapFor(sheetName, width) {
  if (HEADER_CACHE.has(sheetName)) return HEADER_CACHE.get(sheetName);
  // The imported workbook is available only inside run(); use canonical known
  // schemas as a conservative fallback.  Indexes match the current workbook.
  const schemas = {
    "订单明细": ["店铺标识","店铺名称","订单ID","订单创建时间(MSK)","订单创建日(MSK)","业务关联ID(rid/srid)","shkId/chrtId","财务事件日(MSK)","财务事件周起始","创建周起始","SKU/货号","nmId","WB订单状态","生命周期","认列状态","结算证据行数","结算来源","订单时点售价G(CNY)","G来源","G时间匹配","convertedPrice转换价旁证(CNY)","成本价(CNY)","成本版本","成本来源","成本选取依据","平台佣金C(CNY)","实际佣金率","收单/支付费(CNY)","直接到账forPay(CNY)","公式账户增量(CNY)","到账勾稽差额(CNY)","源证据账户增量(CNY)","正向物流(CNY)","实际退货/取消物流(CNY)","代运营费2%(CNY)","广告+退货资金池（按行费率）(CNY)","按规则可提现(本行)(CNY)","公式严格利润(CNY)","源证据严格利润(CNY)","利润勾稽差额(CNY)","价格公式版本","累计纳入","报告纳入口径","财务证据ID","备注","C公式复核(CNY)","C勾稽差额(CNY)","代运营费率快照","广告+退货池率快照","报告政策版本快照","实际广告支出/充值(CNY)","实际广告来源/状态"],
    "财务事件明细": ["店铺标识","店铺名称","订单ID","订单创建日(MSK)","SKU/货号","财务事件日(MSK)","事件周起始","生命周期","认列状态","财务rrdId集合（类型待拆）","财务行数","G订单级参考(CNY)","C订单级参考(CNY)","forPay订单级参考(CNY)","正向物流订单级参考(CNY)","退/取消物流订单级参考(CNY)","事件拆分说明"],
  };
  const map = new Map((schemas[sheetName] || Array(width).fill("")).map((h, i) => [h, i]));
  HEADER_CACHE.set(sheetName, map);
  return map;
}
function set(row, map, header, value) { const i = map.get(header); if (i !== undefined) row[i] = value === undefined ? null : value; }

function orderKey(row) { return stableOrderKeyFromArray(row); }
function stableOrderKeyFromArray(row) { const store = String(row?.[0] || ""); const ref = String(row?.[5] || row?.[2] || row?.[6] || ""); return store && ref ? `${store}|${ref}` : ""; }
function orderFingerprint(row) { return JSON.stringify((row || []).map((v, i) => (i >= 41 && i <= 45 ? null : normalizeValue(v)))); }
function eventKey(row) { return stableEventKeyFromArray(row); }
function stableEventKeyFromArray(row) { const store = String(row?.[0] || ""); const ref = String(row?.[2] || ""); const day = dateText(row?.[5]); const ids = String(row?.[9] || ""); return store && (ref || ids) ? `${store}|${ref || ids}|${day}` : ""; }
function eventFingerprint(row) { return JSON.stringify((row || []).map(normalizeValue)); }
function historyKey(row) { return [dateText(row?.[0]), dateText(row?.[1]), String(row?.[2] || ""), String(row?.[4] || ""), extractReportId(row?.[21])].join("|"); }
function monthlyKey(row) { return [dateText(row?.[1]), dateText(row?.[2]), String(row?.[3] || ""), String(row?.[4] || ""), extractReportId(row?.[23])].join("|"); }
function extractReportId(value) { const match = String(value || "").match(/reportId=([0-9a-f]{32,})/i); return match ? match[1] : ""; }
function reportIdFromSourceRow(row) {
  const firstCell = String(row?.[0] || "");
  // Source keys are stored as `kind/<64-hex-id>` while the note cell uses
  // `reportId=<id>`.  Support both forms; falling back to the store cell
  // would collapse every row to the same `全局` key and drop later reports.
  return extractReportId(firstCell)
    || (firstCell.match(/[0-9a-f]{64}/i) || [])[0]
    || String(row?.[1] || "");
}
function normalizeValue(value) { return value instanceof Date ? value.toISOString() : value == null ? null : String(value); }
function reportPathFor(report) { return report.__sourcePath || ""; }
function sourceText(order) { return (order.evidenceIds || []).length ? "platform_finance_detailed" : "platform_statistics/待结算"; }
function componentSource(order, key) { const sources = order.componentSources || {}; const value = sources[key] ?? sources[key === "G" ? "salesAmountG" : key]; return value == null ? "" : typeof value === "string" ? value : JSON.stringify(value); }
function first(value) { return Array.isArray(value) && value.length ? String(value[0]) : (value == null ? null : String(value)); }
function dateOnly(value) { if (!value) return null; const raw = String(value).slice(0, 10); return /^\d{4}-\d{2}-\d{2}$/.test(raw) ? new Date(`${raw}T00:00:00Z`) : null; }
function dateTime(value) { if (!value) return null; const d = new Date(value); return Number.isNaN(d.getTime()) ? null : d; }
function dateText(value) {
  if (value === null || value === undefined || value === "") return "";
  if (value instanceof Date) return value.toISOString().slice(0, 10);
  // artifact-tool exposes imported Excel dates as serial day numbers while
  // newly written rows use Date objects. Normalize both representations so
  // idempotency keys do not create duplicate history rows.
  const numeric = typeof value === "number"
    ? value
    : (typeof value === "string" && /^\d+(?:\.\d+)?$/.test(value.trim()) ? Number(value) : NaN);
  if (Number.isFinite(numeric) && numeric > 20000 && numeric < 100000) {
    const excelEpoch = Date.UTC(1899, 11, 30);
    const d = new Date(excelEpoch + numeric * 86400000);
    return Number.isNaN(d.getTime()) ? String(value).slice(0, 10) : d.toISOString().slice(0, 10);
  }
  return String(value).slice(0, 10);
}
function mondayOf(value) { const d = dateOnly(value); if (!d) return null; const day = d.getUTCDay() || 7; d.setUTCDate(d.getUTCDate() - day + 1); return d; }
function money(value) { if (value === null || value === undefined || value === "") return null; const n = Number(value); return Number.isFinite(n) ? Math.round(n * 100) / 100 : null; }
function num(value) { return money(value); }
function ratio(value, base) { const n = money(value); const b = money(base); return n == null || !b ? null : n / b; }
function decimalOrNull(value) { const n = Number(value); return Number.isFinite(n) ? n : null; }
function dateTimeText(value) { return value ? new Date(value).toISOString() : ""; }
function blockerText(report) { const codes = [...new Set((report.blockers || []).map((b) => b.code || b.message).filter(Boolean))]; return codes.length ? `开放项${codes.slice(0, 5).join("、")}` : "无开放事项"; }
function stableStringify(value) {
  if (value === null || value === undefined) return "null";
  if (typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map((item) => stableStringify(item)).join(",")}]`;
  return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableStringify(value[key])}`).join(",")}}`;
}
function buildInputFingerprint({ reports, registry, cashEvents, debtSnapshot }) {
  const input = {
    schemaVersion: BRIDGE_SCHEMA_VERSION,
    reports: (reports || []).map((report) => ({
      // __sourcePath identifies the related immutable source artifact; the
      // full parsed report catches same-ID corrections even when its mtime is
      // unchanged.
      sourcePath: report.__sourcePath || "",
      report,
    })),
    registry,
    cashEvents,
    debtSnapshot,
  };
  return crypto.createHash("sha256").update(stableStringify(input), "utf8").digest("hex");
}
async function statSnapshot(file) {
  const stat = await fs.stat(file);
  return { size: Number(stat.size), mtimeMs: Math.round(Number(stat.mtimeMs)) };
}
function sameStat(left, right) {
  return Boolean(left && right)
    && Number(left.size) === Number(right.size)
    && Number(left.mtimeMs) === Number(right.mtimeMs);
}
async function writeJsonAtomic(file, value) {
  const temp = `${file}.tmp-${process.pid}-${Date.now()}`;
  await fs.writeFile(temp, `${JSON.stringify(value, null, 2)}\n`, "utf8");
  try {
    await fs.rename(temp, file);
  } catch (firstError) {
    try {
      await fs.copyFile(temp, file);
      await fs.unlink(temp);
    } catch (secondError) {
      try { await fs.unlink(temp); } catch { /* best effort */ }
      throw new Error(`cannot replace bridge sidecar: ${firstError}; fallback: ${secondError}`);
    }
  }
}
function timingSummary(startedAtMs, timingsMs) {
  return {
    ...timingsMs,
    totalMs: Math.max(0, Date.now() - startedAtMs),
  };
}
function safeStamp(d) { return d.toISOString().replace(/[-:.TZ]/g, "").slice(0, 14); }

export { buildWeeklyDashboardSnapshot, selectLatestWeeklyReport };
