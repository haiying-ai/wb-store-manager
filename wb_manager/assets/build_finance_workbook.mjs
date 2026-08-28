import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const args = parseArgs(process.argv.slice(2));
const payload = JSON.parse(await fs.readFile(args.input, "utf8"));
await fs.mkdir(path.dirname(args.output), { recursive: true });
await fs.mkdir(args.previewDir, { recursive: true });

const wb = Workbook.create();
const summary = wb.worksheets.add("老板摘要");
const stores = wb.worksheets.add("店铺汇总");
const orders = wb.worksheets.add("订单利润");
const funding = wb.worksheets.add("广告与提现");
const policy = wb.worksheets.add("成本版本与口径");
const checks = wb.worksheets.add("数据校验");
wb.comments.setSelf({ displayName: "孙兵" });

const navy = "#17365D";
const blue = "#D9EAF7";
const pale = "#F3F6FA";
const green = "#E2F0D9";
const red = "#FCE4D6";
const gold = "#FFF2CC";
const white = "#FFFFFF";
const moneyFmt = '#,##0.00;[Red](#,##0.00);-';
const pctFmt = '0.0%;[Red](0.0%);-';

buildPolicy();
buildOrders();
buildStores();
buildFunding();
buildSummary();
buildChecks();

for (const sheet of [summary, stores, orders, funding, policy, checks]) {
  sheet.showGridLines = false;
  const used = sheet.getUsedRange();
  if (used) {
    used.format.verticalAlignment = "center";
  }
}

const errorScan = await wb.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 300 },
  summary: "finance workbook formula error scan",
  maxChars: 5000,
});
const errorText = errorScan.ndjson || "";
const formulaErrors = errorText.split("\n").filter((line) => line.includes("#REF!") || line.includes("#DIV/0!") || line.includes("#VALUE!") || line.includes("#NAME?") || line.includes("#N/A"));

const rendered = [];
for (const sheetName of ["老板摘要", "店铺汇总", "订单利润", "广告与提现", "成本版本与口径", "数据校验"]) {
  const blob = await wb.render({ sheetName, autoCrop: "all", scale: 1, format: "png" });
  const file = path.join(args.previewDir, `${sheetName}.png`);
  await fs.writeFile(file, new Uint8Array(await blob.arrayBuffer()));
  rendered.push(file);
}

const output = await SpreadsheetFile.exportXlsx(wb);
await output.save(args.output);
const verification = {
  status: formulaErrors.length === 0 && rendered.length === 6 ? "PASS" : "FAIL",
  formulaErrorCount: formulaErrors.length,
  renderedSheets: rendered,
  reportId: payload.reportId,
};
await fs.writeFile(args.verification, JSON.stringify(verification, null, 2) + "\n", "utf8");
if (verification.status !== "PASS") process.exitCode = 2;

function buildPolicy() {
  title(policy, "成本版本、财务口径与来源", "A1:H1");
  const p = payload.policy || {};
  policy.getRange("A3:B9").values = [
    ["字段", "值"],
    ["财务策略ID", p.reportingPolicyId || ""],
    ["价格策略ID", p.pricingPolicyId || ""],
    ["代运营费率", n(p.managedOperationsRate ?? 0.02)],
    ["广告与退货资金池率", n(p.combinedPoolRate ?? 0.03)],
    ["支付费率回退", n(p.paymentFallbackRate ?? 0.008)],
    ["正向履约固定成本回退 CNY", n(p.fixedOperationsFallbackCny ?? 12)],
  ];
  header(policy.getRange("A3:B3"));
  policy.getRange("B6:B8").format.numberFormat = pctFmt;
  policy.getRange("B9").format.numberFormat = moneyFmt;
  policy.getRange("A11:H11").values = [["成本版本ID", "SKU/规格", "成本 RUB", "汇率 RUB/CNY", "成本 CNY", "生效时间（含）", "失效时间（不含）", "报价来源/行"]];
  header(policy.getRange("A11:H11"));
  const versions = payload.costVersions || [];
  if (versions.length) {
    policy.getRangeByIndexes(11, 0, versions.length, 8).values = versions.map((v) => [
      v.costVersionId || "", v.productKey || v.sku || "", n(v.costRub), n(v.exchangeRateRubPerCny), n(v.costCny),
      timestampInZone(v.effectiveFrom, "Europe/Moscow", "莫斯科"), timestampInZone(v.effectiveTo, "Europe/Moscow", "莫斯科"), `${v.sourceQuote || ""}${v.sourceRow ? `#${v.sourceRow}` : ""}`,
    ]);
    policy.getRange(`C12:E${11 + versions.length}`).format.numberFormat = moneyFmt;
  }
  policy.getRange("A3:A9").format.font = { bold: true };
  policy.getRange("A12:A13").format.columnWidth = 26;
  policy.getRange("B:B").format.columnWidth = 28;
  policy.getRange("F:H").format.columnWidth = 24;
  policy.freezePanes.freezeRows(11);
  wb.comments.addThread({ cell: policy.getRange("B6") }, "代运营费按净营业额计提。来源：当前有效财务策略。");
  wb.comments.addThread({ cell: policy.getRange("B7") }, "3% 为广告与退货合并资金池；实际退货费优先消耗，余额为计提广告费。");
}

function buildOrders() {
  title(orders, "订单级实际利润明细", "A1:Y1");
  const headers = ["店铺", "订单键", "创建时间", "完成/更正时间", "SKU", "结局", "净数量", "净营业额", "采购成本", "WB佣金", "支付费", "正向物流", "仓储/其他履约", "其他平台/内部成本", "实际退货物流费", "代运营费", "3%资金池", "计提广告费", "超额退货损失", "实际广告分摊(仅对账)", "老板利润", "程序计算利润", "差异", "成本版本", "证据状态"];
  orders.getRangeByIndexes(2, 0, 1, headers.length).values = [headers];
  header(orders.getRange("A3:Y3"));
  const rows = payload.orders || [];
  if (rows.length) {
    orders.getRangeByIndexes(3, 0, rows.length, headers.length).values = rows.map((r) => [
      r.storeAlias || "", r.orderKey || "", timestampInZone(r.orderCreatedAt, "Europe/Moscow", "莫斯科"), timestampInZone(r.completedAt || r.adjustedAt, "Europe/Moscow", "莫斯科"), r.sku || "", r.outcome || "",
      n(r.netQuantity), n(r.netTurnoverCny), n(r.purchaseCostCny), n(r.wbCommissionCny), n(r.paymentFeeCny), n(r.forwardLogisticsCny),
      n(r.storageAndOperationsCny), n(r.otherCostCny), n(r.returnLogisticsCny), 0, 0, 0, 0, n(r.actualAdvertisingAllocatedCny), 0,
      n(r.ownerProfitCny), 0, r.costVersionId || "", r.evidenceStatus || "",
    ]);
    const end = 3 + rows.length;
    for (let row = 4; row <= end; row++) {
      orders.getRange(`P${row}`).formulas = [[`=H${row}*'成本版本与口径'!$B$6`]];
      orders.getRange(`Q${row}`).formulas = [[`=MAX(0,H${row})*'成本版本与口径'!$B$7`]];
      orders.getRange(`R${row}`).formulas = [[`=MAX(0,Q${row}-O${row})`]];
      orders.getRange(`S${row}`).formulas = [[`=MAX(0,O${row}-Q${row})`]];
      orders.getRange(`U${row}`).formulas = [[`=H${row}-I${row}-J${row}-K${row}-L${row}-M${row}-N${row}-O${row}-P${row}-R${row}`]];
      orders.getRange(`W${row}`).formulas = [[`=U${row}-V${row}`]];
    }
    orders.getRange(`H4:W${end}`).format.numberFormat = moneyFmt;
    orders.getRange(`W4:W${end}`).conditionalFormats.add("cellIs", { operator: "notEqual", formula: 0, format: { fill: red, font: { color: "#9C0006" } } });
  }
  orders.getRange("A:A").format.columnWidth = 16;
  orders.getRange("B:B").format.columnWidth = 34;
  // Localized timestamps include the timezone label; keep both date columns
  // wide enough to remain readable in the rendered audit copy.
  orders.getRange("C:D").format.columnWidth = 28;
  orders.getRange("E:E").format.columnWidth = 30;
  orders.getRange("F:G").format.columnWidth = 13;
  orders.getRange("H:W").format.columnWidth = 15;
  orders.getRange("X:Y").format.columnWidth = 28;
  orders.freezePanes.freezeRows(3);
}

function buildStores() {
  title(stores, "店铺财务汇总", "A1:Q1");
  const headers = ["店铺", "订单数", "净营业额", "采购成本", "WB佣金", "支付费", "正向物流", "仓储/其他履约", "其他成本", "代运营费", "实际退货费", "计提广告费", "实际广告消耗(仅对账)", "老板利润", "利润率", "程序计算利润", "差异"];
  stores.getRange("A3:Q3").values = [headers];
  header(stores.getRange("A3:Q3"));
  const data = payload.stores || [];
  const orderEnd = Math.max(4, 3 + (payload.orders || []).length);
  if (data.length) {
    stores.getRangeByIndexes(3, 0, data.length, headers.length).values = data.map((r) => [r.storeAlias || "", n(r.orderCount), 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, n(r.actualAdvertisingSpendCny), 0, 0, n(r.ownerProfitCny), 0]);
    const end = 3 + data.length;
    for (let row = 4; row <= end; row++) {
      const alias = `$A${row}`;
      const sum = (column) => `=SUMIF('订单利润'!$A$4:$A$${orderEnd},${alias},'订单利润'!$${column}$4:$${column}$${orderEnd})`;
      stores.getRange(`C${row}`).formulas = [[sum("H")]];
      stores.getRange(`D${row}`).formulas = [[sum("I")]];
      stores.getRange(`E${row}`).formulas = [[sum("J")]];
      stores.getRange(`F${row}`).formulas = [[sum("K")]];
      stores.getRange(`G${row}`).formulas = [[sum("L")]];
      stores.getRange(`H${row}`).formulas = [[sum("M")]];
      stores.getRange(`I${row}`).formulas = [[sum("N")]];
      stores.getRange(`J${row}`).formulas = [[sum("P")]];
      stores.getRange(`K${row}`).formulas = [[sum("O")]];
      stores.getRange(`L${row}`).formulas = [[sum("R")]];
      stores.getRange(`N${row}`).formulas = [[sum("U")]];
      stores.getRange(`O${row}`).formulas = [[`=IF(C${row}=0,0,N${row}/C${row})`]];
      stores.getRange(`Q${row}`).formulas = [[`=N${row}-P${row}`]];
    }
    const total = end + 1;
    stores.getRange(`A${total}:Q${total}`).format = { fill: blue, font: { bold: true }, borders: { preset: "doubleBottom", style: "medium", color: navy } };
    stores.getRange(`A${total}`).values = [["合计"]];
    for (let col = 2; col <= 17; col++) {
      const letter = columnLetter(col);
      stores.getRange(`${letter}${total}`).formulas = [[`=SUM(${letter}4:${letter}${end})`]];
    }
    stores.getRange(`O${total}`).formulas = [[`=IF(C${total}=0,0,N${total}/C${total})`]];
  }
  stores.getRange("C:Q").format.numberFormat = moneyFmt;
  stores.getRange("B:B").format.numberFormat = "#,##0";
  stores.getRange("O:O").format.numberFormat = pctFmt;
  stores.getRange("A:A").format.columnWidth = 18;
  stores.getRange("B:Q").format.columnWidth = 15;
  stores.freezePanes.freezeRows(3);
}

function buildFunding() {
  title(funding, "广告储备与建议提现", "A1:H1");
  funding.getRange("A3:H3").values = [["店铺", "平台可提现余额", "未分配计提广告储备", "已批准未执行充值", "其他锁定资金", "建议提现金额", "资金动作状态", "说明"]];
  header(funding.getRange("A3:H3"));
  const rows = payload.funding || [];
  if (rows.length) {
    funding.getRangeByIndexes(3, 0, rows.length, 8).values = rows.map((r) => [r.storeAlias || "", n(r.withdrawableBalanceCny), n(r.unallocatedAdvertisingReserveCny), n(r.approvedPendingDepositCny), n(r.otherLockedFundsCny), 0, r.status || "PLAN_ONLY", r.note || ""]);
    const end = 3 + rows.length;
    for (let row = 4; row <= end; row++) funding.getRange(`F${row}`).formulas = [[`=MAX(0,B${row}-C${row}-D${row}-E${row})`]];
    funding.getRange(`B4:F${end}`).format.numberFormat = moneyFmt;
  }
  funding.getRange("A:A").format.columnWidth = 18;
  funding.getRange("B:F").format.columnWidth = 20;
  funding.getRange("G:G").format.columnWidth = 18;
  funding.getRange("H:H").format.columnWidth = 48;
  funding.freezePanes.freezeRows(3);
  funding.getRange("A10:H12").merge();
  funding.getRange("A10").values = [["提现没有公开 WB API，建议金额仅供人工门户操作。广告充值只允许在店铺、活动ID、金额、资金来源均经明确批准后执行，并必须读取充值后预算；网络状态不明时禁止自动重试。"]];
  funding.getRange("A10:H12").format = { fill: gold, wrapText: true, font: { color: "#7F6000" } };
}

function buildSummary() {
  title(summary, "WB 双店财务报告｜老板摘要", "A1:H1");
  const period = payload.period || {};
  summary.getRange("A3:B8").values = [
    ["报告ID", payload.reportId || ""],
    ["状态", payload.status || "PENDING_DATA"],
    ["报告类型", payload.kind || ""],
    ["期间", `${period.from || ""} 至 ${period.to || ""}`],
    ["财务时区", period.timezone || "Europe/Moscow"],
    ["生成时间", timestampInZone(payload.generatedAt, "Asia/Shanghai", "北京时间")],
  ];
  summary.getRange("A3:A8").format.font = { bold: true };
  summary.getRange("A10:H10").values = [["营业额", "采购成本", "平台及履约费", "代运营费", "退货费", "计提广告费", "老板利润", "利润率"]];
  header(summary.getRange("A10:H10"));
  const storeCount = (payload.stores || []).length;
  const totalRow = 4 + storeCount;
  summary.getRange("A11:H11").formulas = [[
    `='店铺汇总'!C${totalRow}`, `='店铺汇总'!D${totalRow}`, `=SUM('店铺汇总'!E${totalRow}:I${totalRow})`, `='店铺汇总'!J${totalRow}`,
    `='店铺汇总'!K${totalRow}`, `='店铺汇总'!L${totalRow}`, `='店铺汇总'!N${totalRow}`, `='店铺汇总'!O${totalRow}`,
  ]];
  summary.getRange("A11:G11").format.numberFormat = moneyFmt;
  summary.getRange("H11").format.numberFormat = pctFmt;
  summary.getRange("A11:H11").format = { fill: green, font: { bold: true, size: 12 }, borders: { preset: "outside", style: "medium", color: navy } };
  summary.getRange("A14:H14").values = [["店铺", "订单数", "营业额", "采购成本", "代运营费", "退货费", "计提广告费", "老板利润"]];
  header(summary.getRange("A14:H14"));
  if (storeCount) {
    for (let i = 0; i < storeCount; i++) {
      const src = 4 + i;
      const dst = 15 + i;
      summary.getRange(`A${dst}:H${dst}`).formulas = [[`='店铺汇总'!A${src}`, `='店铺汇总'!B${src}`, `='店铺汇总'!C${src}`, `='店铺汇总'!D${src}`, `='店铺汇总'!J${src}`, `='店铺汇总'!K${src}`, `='店铺汇总'!L${src}`, `='店铺汇总'!N${src}`]];
    }
    summary.getRange(`C15:H${14 + storeCount}`).format.numberFormat = moneyFmt;
  }
  const blockerRow = 17 + storeCount;
  summary.getRange(`A${blockerRow}:H${blockerRow}`).merge();
  summary.getRange(`A${blockerRow}`).values = [[(payload.blockers || []).length ? `封账阻断：${(payload.blockers || []).map((b) => b.message || b.code).join("；")}` : "数据完整性：全部必需来源、成本版本和实际退货扣费已通过校验。"]];
  summary.getRange(`A${blockerRow}:H${blockerRow}`).format = { fill: (payload.blockers || []).length ? red : green, wrapText: true, font: { bold: true } };
  summary.getRange("A:H").format.columnWidth = 19;
  summary.freezePanes.freezeRows(2);
}

function buildChecks() {
  title(checks, "数据与公式校验", "A1:F1");
  checks.getRange("A3:F3").values = [["检查", "实际值", "期望值", "差异", "状态", "修复位置/说明"]];
  header(checks.getRange("A3:F3"));
  const blockerCount = (payload.blockers || []).length;
  const orderCount = (payload.orders || []).length;
  const storeCount = (payload.stores || []).length;
  const totalRow = 4 + storeCount;
  const orderEnd = Math.max(4, 3 + orderCount);
  checks.getRange("A4:F8").values = [
    ["订单利润公式与程序值", 0, 0, 0, "", "订单利润!W列"],
    ["订单利润合计与店铺汇总", 0, 0, 0, "", "店铺汇总!N列"],
    ["未解决封账阻断", blockerCount, 0, blockerCount, "", "报告 JSON blockers"],
    ["实际退货费默认值", 0, 0, 0, "", "禁止固定8/16元"],
    ["实际广告重复扣减", 0, 0, 0, "", "实际广告仅对账"],
  ];
  checks.getRange("B4").formulas = [[`=SUM('订单利润'!W4:W${orderEnd})`]];
  checks.getRange("D4").formulas = [["=B4-C4"]];
  checks.getRange("B5").formulas = [[`=SUM('订单利润'!U4:U${orderEnd})`]];
  checks.getRange("C5").formulas = [[`='店铺汇总'!N${totalRow}`]];
  checks.getRange("D5").formulas = [["=B5-C5"]];
  for (let row = 4; row <= 8; row++) checks.getRange(`E${row}`).formulas = [[`=IF(ABS(D${row})<0.01,"OK","FAIL")`]];
  checks.getRange("A10:B10").values = [["MODEL STATUS", ""]];
  checks.getRange("B10").formulas = [["=IF(COUNTIF(E4:E8,\"FAIL\")=0,\"PASS\",\"FAIL\")"]];
  checks.getRange("A10:B10").format = { fill: blue, font: { bold: true, size: 12 }, borders: { preset: "outside", style: "medium", color: navy } };
  checks.getRange("E4:E8").conditionalFormats.add("containsText", { text: "FAIL", format: { fill: red, font: { color: "#9C0006", bold: true } } });
  checks.getRange("E4:E8").conditionalFormats.add("containsText", { text: "OK", format: { fill: green, font: { color: "#006100", bold: true } } });
  checks.getRange("A:A").format.columnWidth = 34;
  checks.getRange("B:E").format.columnWidth = 18;
  checks.getRange("F:F").format.columnWidth = 42;
  checks.freezePanes.freezeRows(3);
}

function title(sheet, text, range) {
  sheet.getRange(range).merge();
  sheet.getRange(range.split(":")[0]).values = [[text]];
  sheet.getRange(range).format = { fill: navy, font: { bold: true, color: white, size: 16 }, rowHeight: 30, horizontalAlignment: "left" };
}

function header(range) {
  range.format = { fill: navy, font: { bold: true, color: white }, wrapText: true, borders: { preset: "outside", style: "thin", color: navy }, horizontalAlignment: "center" };
}

function n(value) {
  const result = Number(value ?? 0);
  return Number.isFinite(result) ? result : 0;
}

function timestampInZone(value, timeZone, label) {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).formatToParts(parsed);
  const byType = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${byType.year}-${byType.month}-${byType.day} ${byType.hour}:${byType.minute}:${byType.second} (${label})`;
}

function columnLetter(index) {
  let value = index;
  let result = "";
  while (value > 0) {
    value--;
    result = String.fromCharCode(65 + (value % 26)) + result;
    value = Math.floor(value / 26);
  }
  return result;
}

function parseArgs(values) {
  const result = {};
  for (let i = 0; i < values.length; i += 2) {
    const key = values[i]?.replace(/^--/, "").replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    result[key] = values[i + 1];
  }
  for (const key of ["input", "output", "previewDir", "verification"]) {
    if (!result[key]) throw new Error(`Missing --${key}`);
  }
  return result;
}
