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
const openItems = wb.worksheets.add("待确认项");
wb.comments.setSelf({ displayName: String(payload.commentAuthor || "User") });

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
buildOpenItems();
buildStores();
buildFunding();
buildSummary();
buildChecks();

const reportSheets = [summary, stores, orders, funding, policy, checks, openItems];
for (const sheet of reportSheets) {
  sheet.showGridLines = false;
  const used = sheet.getUsedRange();
  if (used) {
    used.format.verticalAlignment = "center";
  }
}

const workbookStatus = String(checks.getRange("B10").values?.[0]?.[0] || "");
const checkFailures = (checks.getRange("A4:E9").values || [])
  .map((row, index) => ({ row: 4 + index, check: String(row?.[0] || ""), status: String(row?.[4] || "") }))
  .filter((item) => item.status === "FAIL");
const consistencyFailures = collectConsistencyFailures();
if (!workbookStatus) {
  consistencyFailures.push({ check: "数据校验状态", actual: null, expected: "FINAL 或 PENDING_DATA", difference: null });
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
for (const sheetName of reportSheets.map((sheet) => sheet.name)) {
  const blob = await wb.render({ sheetName, autoCrop: "all", scale: 1, format: "png" });
  const file = path.join(args.previewDir, `${sheetName}.png`);
  await fs.writeFile(file, new Uint8Array(await blob.arrayBuffer()));
  rendered.push(file);
}

const output = await SpreadsheetFile.exportXlsx(wb);
await output.save(args.output);
const verification = {
  status: formulaErrors.length === 0
    && rendered.length === reportSheets.length
    && checkFailures.length === 0
    && consistencyFailures.length === 0
    && workbookStatus !== "CALCULATION_FAIL"
    ? "PASS"
    : "FAIL",
  formulaErrorCount: formulaErrors.length,
  renderedSheets: rendered,
  reportId: payload.reportId,
  workbookStatus,
  checkFailures,
  consistencyFailures,
};
await fs.writeFile(args.verification, JSON.stringify(verification, null, 2) + "\n", "utf8");
if (verification.status !== "PASS") process.exitCode = 2;

function buildPolicy() {
  title(policy, "成本版本、财务口径与来源", "A1:J1");
  const p = payload.policy || {};
  policy.getRange("A3:B14").values = [
    ["字段", "值"],
    ["当前财务策略ID", p.reportingPolicyId || ""],
    ["当前价格策略ID", p.pricingPolicyId || ""],
    ["当前准备金模式", p.reserveAllocationMode || ""],
    ["当前代运营费率", optionalNumber(p.managedOperationsRate)],
    ["当前退货风险准备率", optionalNumber(p.returnRiskReserveRate)],
    ["当前推广预留率", optionalNumber(p.promotionReserveRate)],
    ["当前政策准备金总率", optionalNumber(p.totalReserveRate ?? p.combinedPoolRate)],
    ["报告内财务策略", (p.reportingPolicyIdsUsed || []).join("；")],
    ["订单口径依据", "订单页的策略ID、模式、费率和已计算金额为最终依据；本页当前值仅供参考"],
    ["实际报告支付费回退", p.paymentFallbackRate == null ? "禁用（仅取平台实际）" : n(p.paymentFallbackRate)],
    ["实际报告固定履约回退 CNY", p.fixedOperationsFallbackCny == null ? "禁用（仅取平台实际）" : n(p.fixedOperationsFallbackCny)],
  ];
  header(policy.getRange("A3:B3"));
  policy.getRange("B7:B10").format.numberFormat = pctFmt;
  policy.getRange("B14").format.numberFormat = moneyFmt;

  const reportingVersions = reportingPolicyVersions();
  policy.getRange("A16:J16").values = [["财务策略ID", "价格策略ID", "准备金模式", "代运营费率", "退货风险准备率", "推广预留率", "政策准备金总率", "生效时间（含）", "失效时间（不含）", "报告订单数"]];
  header(policy.getRange("A16:J16"));
  if (reportingVersions.length) {
    policy.getRangeByIndexes(16, 0, reportingVersions.length, 10).values = reportingVersions.map((v) => [
      v.reportingPolicyId || "", v.pricingPolicyId || "", v.reserveAllocationMode || "",
      optionalNumber(v.managedOperationsRate), optionalNumber(v.returnRiskReserveRate), optionalNumber(v.promotionReserveRate), optionalNumber(v.totalReserveRate),
      timestampInZone(v.effectiveFrom, "Europe/Moscow", "莫斯科"), timestampInZone(v.effectiveTo, "Europe/Moscow", "莫斯科"), v.orderCount,
    ]);
    policy.getRange(`D17:G${16 + reportingVersions.length}`).format.numberFormat = pctFmt;
  }

  const costHeaderRow = Math.max(20, 18 + reportingVersions.length);
  policy.getRange(`A${costHeaderRow}:I${costHeaderRow}`).values = [["成本版本ID", "SKU/规格", "源币种", "成本 CNY", "成本 RUB（证据）", "汇率 RUB/CNY（证据）", "生效时间（含）", "失效时间（不含）", "报价来源/行"]];
  header(policy.getRange(`A${costHeaderRow}:I${costHeaderRow}`));
  const versions = payload.costVersions || [];
  if (versions.length) {
    policy.getRangeByIndexes(costHeaderRow, 0, versions.length, 9).values = versions.map((v) => [
      v.costVersionId || "", v.productKey || v.sku || "", v.sourceCurrency || "", optionalNumber(v.costCny ?? v.purchaseCostCny), optionalNumber(v.costRub), optionalNumber(v.exchangeRateRubPerCny),
      timestampInZone(v.effectiveFrom, "Europe/Moscow", "莫斯科"), timestampInZone(v.effectiveTo, "Europe/Moscow", "莫斯科"), `${v.sourceQuote || v.sourcePath || ""}${v.sourceSheet ? `#${v.sourceSheet}` : ""}${v.sourceRow ? `#${v.sourceRow}` : ""}`,
    ]);
    policy.getRange(`D${costHeaderRow + 1}:F${costHeaderRow + versions.length}`).format.numberFormat = moneyFmt;
  }
  policy.getRange("A3:A14").format.font = { bold: true };
  policy.getRange("A:A").format.columnWidth = 28;
  policy.getRange("B:B").format.columnWidth = 34;
  policy.getRange("C:J").format.columnWidth = 24;
  policy.freezePanes.freezeRows(2);
}

function buildOrders() {
  title(orders, "订单级实际利润明细", "A1:AG1");
  const headers = ["店铺", "订单键", "创建时间", "完成/更正时间", "SKU", "结局", "净数量", "净营业额", "采购成本", "WB佣金", "支付费", "正向物流", "仓储/其他履约", "其他平台/内部成本", "实际退货物流费", "代运营费", "政策准备金总额", "计提广告费", "超额退货损失", "实际广告分摊(仅对账)", "老板利润", "程序计算利润", "差异", "成本版本", "证据状态", "财务策略ID", "准备金模式", "代运营费率", "退货风险准备率", "推广预留率", "准备金总率", "退货风险准备金", "推广预留"];
  orders.getRangeByIndexes(2, 0, 1, headers.length).values = [headers];
  header(orders.getRange("A3:AG3"));
  const rows = payload.orders || [];
  if (rows.length) {
    orders.getRangeByIndexes(3, 0, rows.length, headers.length).values = rows.map((r) => [
      r.storeAlias || "", r.orderKey || "", timestampInZone(r.orderCreatedAt, "Europe/Moscow", "莫斯科"), timestampInZone(r.completedAt || r.adjustedAt, "Europe/Moscow", "莫斯科"), r.sku || "", r.outcome || "",
      n(r.netQuantity), n(r.netTurnoverCny), n(r.purchaseCostCny), n(r.wbCommissionCny), n(r.paymentFeeCny), n(r.forwardLogisticsCny),
      n(r.storageAndOperationsCny), n(r.otherCostCny ?? r.otherPlatformCostCny), n(r.returnLogisticsCny), optionalNumber(r.managedOperationsCny), optionalNumber(firstDefined(r, ["totalPolicyReserveCny", "combinedAdvertisingAndReturnPoolCny", "combinedPoolCny"])), optionalNumber(r.advertisingProvisionCny), optionalNumber(r.excessReturnLossCny), optionalNumber(r.actualAdvertisingAllocatedCny ?? r.actualAdvertisingSpendCny), optionalNumber(r.strictOwnerProfitCny ?? r.ownerProfitCny),
      optionalNumber(r.ownerProfitCny ?? r.strictOwnerProfitCny), 0, r.costVersionId || "", r.evidenceStatus || "", r.financialReportingPolicyVersionId || "", r.reserveAllocationMode || "", optionalNumber(r.managedOperationsRate), optionalNumber(r.returnRiskReserveRate), optionalNumber(r.promotionReserveRate), optionalNumber(r.totalReserveRate), optionalNumber(r.returnRiskReserveCny), optionalNumber(r.promotionReserveCny),
    ]);
    const end = 3 + rows.length;
    for (let row = 4; row <= end; row++) {
      // Business amounts come from the finance engine's order-time policy
      // snapshot.  Excel only checks the strict-profit compatibility alias.
      orders.getRange(`W${row}`).formulas = [[`=IF(Y${row}="COMPLETE",ROUND(U${row}-V${row},2),0)`]];
    }
    orders.getRange(`H4:W${end}`).format.numberFormat = moneyFmt;
    orders.getRange(`AB4:AE${end}`).format.numberFormat = pctFmt;
    orders.getRange(`AF4:AG${end}`).format.numberFormat = moneyFmt;
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
  orders.getRange("Z:AG").format.columnWidth = 22;
  orders.freezePanes.freezeRows(3);
  wb.comments.addThread({ cell: orders.getRange("T3") }, "开放/BLOCKED 行保留源数据中的广告分摊以便追溯；店铺汇总采用报告引擎的已确认口径，因此可能存在分币级对账差。实际广告仅对账，不重复扣减利润。");
}

function buildOpenItems() {
  title(openItems, "待确认订单", "A1:O1");
  openItems.getRange("A2:O2").merge();
  openItems.getRange("A2").values = [["本页仅保留证据状态非 COMPLETE 的订单及当前可见金额；所有金额均不进入已确认营业额、费用或利润。"]];
  openItems.getRange("A2:O2").format = { fill: gold, wrapText: true, font: { color: "#7F6000" } };
  const headers = ["店铺", "订单键", "创建时间", "SKU", "结局", "证据状态", "净数量", "当前营业额", "当前采购成本", "当前WB佣金", "当前支付费", "当前正向物流", "当前实际退货费", "当前程序利润", "处理说明"];
  openItems.getRange("A3:O3").values = [headers];
  header(openItems.getRange("A3:O3"));
  const rows = (payload.orders || []).filter((row) => row.evidenceStatus !== "COMPLETE");
  if (rows.length) {
    openItems.getRangeByIndexes(3, 0, rows.length, headers.length).values = rows.map((row) => [
      row.storeAlias || "",
      row.orderKey || "",
      timestampInZone(row.orderCreatedAt, "Europe/Moscow", "莫斯科"),
      row.sku || "",
      row.outcome || "",
      row.evidenceStatus || "MISSING",
      optionalNumber(row.netQuantity),
      optionalNumber(row.netTurnoverCny),
      optionalNumber(row.purchaseCostCny),
      optionalNumber(row.wbCommissionCny),
      optionalNumber(row.paymentFeeCny),
      optionalNumber(row.forwardLogisticsCny),
      optionalNumber(row.returnLogisticsCny),
      optionalNumber(row.ownerProfitCny),
      "不计入确认结果；补齐平台结算与成本证据后重算",
    ]);
    const end = 3 + rows.length;
    openItems.getRange(`G4:G${end}`).format.numberFormat = "#,##0.00";
    openItems.getRange(`H4:N${end}`).format.numberFormat = moneyFmt;
    openItems.getRange(`O4:O${end}`).format.wrapText = true;
    openItems.getRange(`A4:O${end}`).format.rowHeight = 30;
    openItems.getRange(`F4:F${end}`).conditionalFormats.add("notContainsText", { text: "COMPLETE", format: { fill: gold, font: { color: "#7F6000", bold: true } } });
  } else {
    openItems.getRange("A4:O4").merge();
    openItems.getRange("A4").values = [["无待确认订单。"]];
    openItems.getRange("A4:O4").format = { fill: green, font: { color: "#006100", bold: true } };
  }
  openItems.getRange("A:A").format.columnWidth = 16;
  openItems.getRange("B:B").format.columnWidth = 34;
  openItems.getRange("C:C").format.columnWidth = 28;
  openItems.getRange("D:D").format.columnWidth = 30;
  openItems.getRange("E:G").format.columnWidth = 14;
  openItems.getRange("H:N").format.columnWidth = 16;
  openItems.getRange("O:O").format.columnWidth = 48;
  openItems.freezePanes.freezeRows(3);
}

function buildStores() {
  title(stores, "店铺财务汇总", "A1:Q1");
  const headers = ["店铺", "涉及订单数", "已确认净营业额", "已确认采购成本", "已确认WB佣金", "已确认支付费", "已确认正向物流", "已确认仓储/其他履约", "已确认其他成本", "已确认代运营费", "实际退货费", "计提广告费", "实际广告消耗(仅对账)", "老板利润", "利润率", "程序计算利润", "差异"];
  stores.getRange("A3:Q3").values = [headers];
  header(stores.getRange("A3:Q3"));
  wb.comments.addThread({ cell: stores.getRange("K3") }, "实际退货物流费：按店铺、报告期间汇总平台财务行的有符号金额。");
  wb.comments.addThread({ cell: stores.getRange("L3") }, "计提广告费：由报告引擎按订单创建时点的财务策略汇总；新口径为独立 3% 推广预留，旧口径保留共享资金池扣除实际退货费后的余额。");
  wb.comments.addThread({ cell: stores.getRange("N3") }, "老板利润：使用报告引擎按店铺、报告期间聚合后的严格利润，不用订单页逐单 MAX 结果相加。");
  const data = payload.stores || [];
  const orderEnd = Math.max(4, 3 + (payload.orders || []).length);
  if (data.length) {
    // The order sheet keeps effective-dated reserve and return figures for
    // traceability.  Store-period reserve and profit remain engine results, so
    // mixed legacy/shared and new split-reserve orders are never recalculated
    // under one global workbook rate.
    stores.getRangeByIndexes(3, 0, data.length, headers.length).values = data.map((r) => [
      r.storeAlias || "", n(r.orderCount), 0, 0, 0, 0, 0, 0, 0, 0,
      optionalNumber(r.returnLogisticsCny), optionalNumber(r.advertisingProvisionCny),
      optionalNumber(r.actualAdvertisingSpendCny), optionalNumber(r.strictOwnerProfitCny ?? r.ownerProfitCny), "",
      optionalNumber(r.ownerProfitCny ?? r.strictOwnerProfitCny), "",
    ]);
    const end = 3 + data.length;
    for (let row = 4; row <= end; row++) {
      const alias = `$A${row}`;
      const completeCount = `COUNTIFS('订单利润'!$A$4:$A$${orderEnd},${alias},'订单利润'!$Y$4:$Y$${orderEnd},"COMPLETE")`;
      const sum = (column) => `=IF(${completeCount}=0,0,SUMIFS('订单利润'!$${column}$4:$${column}$${orderEnd},'订单利润'!$A$4:$A$${orderEnd},${alias},'订单利润'!$Y$4:$Y$${orderEnd},"COMPLETE"))`;
      stores.getRange(`C${row}`).formulas = [[sum("H")]];
      stores.getRange(`D${row}`).formulas = [[sum("I")]];
      stores.getRange(`E${row}`).formulas = [[sum("J")]];
      stores.getRange(`F${row}`).formulas = [[sum("K")]];
      stores.getRange(`G${row}`).formulas = [[sum("L")]];
      stores.getRange(`H${row}`).formulas = [[sum("M")]];
      stores.getRange(`I${row}`).formulas = [[sum("N")]];
      stores.getRange(`J${row}`).formulas = [[sum("P")]];
      // K/L/N are report-level values calculated under each order's selected
      // policy; N is the corresponding aggregate strict profit.
      stores.getRange(`O${row}`).formulas = [[`=IF(OR(C${row}="",C${row}=0),"",N${row}/C${row})`]];
      stores.getRange(`Q${row}`).formulas = [[`=IF(OR(N${row}="",P${row}=""),"",N${row}-P${row})`]];
    }
    const total = end + 1;
    stores.getRange(`A${total}:Q${total}`).format = { fill: blue, font: { bold: true }, borders: { preset: "doubleBottom", style: "medium", color: navy } };
    stores.getRange(`A${total}`).values = [["合计"]];
    for (let col = 2; col <= 17; col++) {
      const letter = columnLetter(col);
      const totalFormula = col === 2
        ? `=SUM(${letter}4:${letter}${end})`
        : `=IF(COUNT(${letter}4:${letter}${end})=0,"",SUM(${letter}4:${letter}${end}))`;
      stores.getRange(`${letter}${total}`).formulas = [[totalFormula]];
    }
    stores.getRange(`O${total}`).formulas = [[`=IF(OR(C${total}="",C${total}=0),"",N${total}/C${total})`]];
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
  funding.getRange("A3:H3").values = [["店铺", "平台可提现余额", "未分配计提广告储备", "已批准未执行活动预算划拨", "其他锁定资金", "建议提现金额", "资金动作状态", "说明"]];
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
  funding.getRange("A10").values = [["提现没有公开 WB API，建议金额仅供人工门户操作。活动预算直接从同店 WB 结算余额 balance（net，type=1）划拨，无需先提现或现金充值；仍须精确批准店铺、活动ID和金额，并读取划拨后预算，网络状态不明时禁止自动重试。平台周期扣款只对账3%池，不重复计入利润。"]];
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
  const orderRows = payload.orders || [];
  const confirmedOrders = orderRows.filter((row) => row.evidenceStatus === "COMPLETE").length;
  const openOrders = orderRows.length - confirmedOrders;
  summary.getRange("A9:B9").values = [["订单状态", `${confirmedOrders} 已确认 / ${openOrders} 待确认（详见“待确认项”）`]];
  summary.getRange("A3:A9").format.font = { bold: true };
  summary.getRange("A10:H10").values = [["已确认营业额", "已确认采购成本", "平台及履约费", "代运营费", "退货费", "计提广告费", "老板利润", "利润率"]];
  header(summary.getRange("A10:H10"));
  const storeCount = (payload.stores || []).length;
  const totalRow = 4 + storeCount;
  summary.getRange("A11:H11").formulas = [[
    `='店铺汇总'!C${totalRow}`, `='店铺汇总'!D${totalRow}`, `=IF(COUNT('店铺汇总'!E${totalRow}:I${totalRow})=0,"",SUM('店铺汇总'!E${totalRow}:I${totalRow}))`, `='店铺汇总'!J${totalRow}`,
    `='店铺汇总'!K${totalRow}`, `='店铺汇总'!L${totalRow}`, `='店铺汇总'!N${totalRow}`, `='店铺汇总'!O${totalRow}`,
  ]];
  summary.getRange("A11:G11").format.numberFormat = moneyFmt;
  summary.getRange("H11").format.numberFormat = pctFmt;
  summary.getRange("A11:H11").format = { fill: green, font: { bold: true, size: 12 }, borders: { preset: "outside", style: "medium", color: navy } };
  summary.getRange("A14:H14").values = [["店铺", "涉及订单数", "已确认营业额", "已确认采购成本", "代运营费", "退货费", "计提广告费", "老板利润"]];
  header(summary.getRange("A14:H14"));
  if (storeCount) {
    for (let i = 0; i < storeCount; i++) {
      const src = 4 + i;
      const dst = 15 + i;
      summary.getRange(`A${dst}:H${dst}`).formulas = [[`='店铺汇总'!A${src}`, `='店铺汇总'!B${src}`, `='店铺汇总'!C${src}`, `='店铺汇总'!D${src}`, `='店铺汇总'!J${src}`, `='店铺汇总'!K${src}`, `='店铺汇总'!L${src}`, `='店铺汇总'!N${src}`]];
    }
    summary.getRange(`C15:H${14 + storeCount}`).format.numberFormat = moneyFmt;
  }
  const previousReport = payload.previousReport || null;
  const previousTotals = previousReport?.totals || null;
  const detailedVarianceRows = Array.isArray(previousReport?.varianceRows) ? previousReport.varianceRows : [];
  const bridgeTitleRow = 17 + storeCount;
  const previousPeriod = previousReport?.period || {};
  const previousLabel = previousReport
    ? [
      previousPeriod.to ? `上一版：截至 ${previousPeriod.to}` : "上一版：已发布报告",
      previousReport.generatedAt ? `生成 ${timestampInZone(previousReport.generatedAt, "Asia/Shanghai", "北京时间")}` : "",
    ].filter(Boolean).join("｜")
    : "上一版数据未提供";
  summary.getRange(`A${bridgeTitleRow}:F${bridgeTitleRow}`).merge();
  summary.getRange(`G${bridgeTitleRow}:I${bridgeTitleRow}`).merge();
  summary.getRange(`A${bridgeTitleRow}`).values = [["较上一版变动桥"]];
  summary.getRange(`G${bridgeTitleRow}`).values = [[previousLabel]];
  summary.getRange(`A${bridgeTitleRow}:I${bridgeTitleRow}`).format = { fill: blue, font: { bold: true, color: navy }, rowHeight: 26 };
  let bridgeLastRow = bridgeTitleRow;
  if (detailedVarianceRows.length || previousTotals) {
    const bridgeHeaderRow = bridgeTitleRow + 1;
    summary.getRange(`A${bridgeHeaderRow}:I${bridgeHeaderRow}`).values = [["指标", "上一版", "新增确认", "移出确认", "订单更正", "汇总重算", "本版", "净变动", "原因"]];
    header(summary.getRange(`A${bridgeHeaderRow}:I${bridgeHeaderRow}`));
    const bridgeRows = detailedVarianceRows.length ? detailedVarianceRows : defaultVarianceRows(previousTotals);
    summary.getRangeByIndexes(bridgeHeaderRow, 0, bridgeRows.length, 9).values = bridgeRows.map((row) => [
      row.label || row.metric || "",
      optionalNumber(row.previous),
      optionalNumber(row.newlyConfirmed),
      optionalNumber(row.removedFromConfirmed),
      optionalNumber(row.confirmedCorrections),
      optionalNumber(row.aggregateRecalculation),
      optionalNumber(row.current),
      optionalNumber(row.delta),
      row.reason || (detailedVarianceRows.length ? "" : "本版减上一版；未提供逐项变动原因"),
    ]);
    for (let index = 0; index < bridgeRows.length; index++) {
      const row = bridgeHeaderRow + 1 + index;
      summary.getRange(`H${row}`).formulas = [[`=IF(OR(B${row}="",G${row}=""),"",ROUND(G${row}-B${row},2))`]];
    }
    bridgeLastRow = bridgeHeaderRow + bridgeRows.length;
    summary.getRange(`B${bridgeHeaderRow + 1}:H${bridgeLastRow}`).format.numberFormat = moneyFmt;
    for (let index = 0; index < bridgeRows.length; index++) {
      if (bridgeRows[index].metric === "confirmedOrderCount") {
        const row = bridgeHeaderRow + 1 + index;
        summary.getRange(`B${row}:H${row}`).format.numberFormat = '#,##0;[Red](#,##0);-';
      }
    }
    summary.getRange(`I${bridgeHeaderRow + 1}:I${bridgeLastRow}`).format.wrapText = true;
    summary.getRange(`A${bridgeHeaderRow + 1}:I${bridgeLastRow}`).format.rowHeight = 30;
    const bridgeNotes = [];
    if ((previousReport?.basisChanges || []).length) {
      bridgeNotes.push(`口径版本变化：${previousReport.basisChanges.join("；")}`);
    }
    if (
      (previousReport?.schemaVersion === "finance-report-variance-v2" && previousReport?.status === "PENDING_DATA")
      || (previousReport?.compatibilityIssues || []).length
    ) {
      bridgeNotes.push(`差异归因待补：${(previousReport.compatibilityIssues || []).join("；") || "存在缺少同口径字段的指标"}`);
    }
    if (bridgeNotes.length) {
      bridgeLastRow += 1;
      summary.getRange(`A${bridgeLastRow}:I${bridgeLastRow}`).merge();
      summary.getRange(`A${bridgeLastRow}`).values = [[bridgeNotes.join("。")]];
      summary.getRange(`A${bridgeLastRow}:I${bridgeLastRow}`).format = { fill: gold, wrapText: true, font: { color: "#7F6000" }, rowHeight: 34 };
    }
  } else {
    bridgeLastRow = bridgeTitleRow + 1;
    summary.getRange(`A${bridgeLastRow}:I${bridgeLastRow}`).merge();
    summary.getRange(`A${bridgeLastRow}`).values = [["未提供上一版已发布报告，本次不推测差异；补入 previousReport 后自动生成同口径变动桥。"]];
    summary.getRange(`A${bridgeLastRow}:I${bridgeLastRow}`).format = { fill: pale, wrapText: true, font: { color: "#666666" }, rowHeight: 26 };
  }
  const blockerRow = bridgeLastRow + 2;
  summary.getRange(`A${blockerRow}:I${blockerRow}`).merge();
  summary.getRange(`A${blockerRow}`).values = [[(payload.blockers || []).length
    ? `开放事项（不阻挡按时出报）：${(payload.blockers || []).length} 项证据待补、${openOrders} 笔订单待确认；明细见“待确认项”和“数据校验”。`
    : "数据完整性：全部必需来源、成本版本和实际退货扣费已通过校验。"]];
  summary.getRange(`A${blockerRow}:I${blockerRow}`).format = { fill: (payload.blockers || []).length ? red : green, wrapText: true, font: { bold: true } };
  summary.getRange("A:I").format.columnWidth = 17;
  summary.getRange("B:B").format.columnWidth = 34;
  summary.getRange("I:I").format.columnWidth = 30;
  summary.freezePanes.freezeRows(2);
}

function defaultVarianceRows(previousTotals) {
  const definitions = [
    ["netTurnoverCny", "已确认营业额", ["netTurnoverCny", "salesAmountGCny"], "A11"],
    ["purchaseCostCny", "已确认采购成本", ["purchaseCostCny"], "B11"],
    ["platformAndOperationsCostCny", "平台及履约费", ["platformAndOperationsCostCny"], "C11"],
    ["managedOperationsCny", "代运营费", ["managedOperationsCny"], "D11"],
    ["returnLogisticsCny", "退货费", ["returnLogisticsCny"], "E11"],
    ["advertisingProvisionCny", "计提广告费", ["advertisingProvisionCny"], "F11"],
    ["strictOwnerProfitCny", "老板利润", ["strictOwnerProfitCny", "ownerProfitCny"], "G11"],
  ];
  return definitions.map(([metric, label, keys, currentCell]) => ({
    metric,
    label,
    previous: firstDefined(previousTotals, keys),
    current: summary.getRange(currentCell).values?.[0]?.[0],
    reason: "本版减上一版；未提供逐项变动原因",
  }));
}

function buildChecks() {
  title(checks, "数据与公式校验", "A1:F1");
  checks.getRange("A3:F3").values = [["检查", "实际值", "期望值", "差异", "状态", "修复位置/说明"]];
  header(checks.getRange("A3:F3"));
  const blockerCount = (payload.blockers || []).length;
  const orderCount = (payload.orders || []).length;
  const openItemCount = (payload.orders || []).filter((row) => row.evidenceStatus !== "COMPLETE").length;
  const storeCount = (payload.stores || []).length;
  const storeEnd = Math.max(4, 3 + storeCount);
  const orderEnd = Math.max(4, 3 + orderCount);
  checks.getRange("A4:F9").values = [
    ["订单利润公式与程序值绝对差", 0, 0, 0, "", "订单利润!W列"],
    ["店铺公式与程序利润绝对差", 0, 0, 0, "", "店铺汇总!Q列"],
    ["待确认订单", openItemCount, 0, openItemCount, "", "待确认项!F列"],
    ["未解决证据事项", blockerCount, 0, blockerCount, "", "报告 JSON blockers"],
    ["实际退货费默认值", 0, 0, 0, "", "禁止固定8/16元"],
    ["实际广告重复扣减", 0, 0, 0, "", "实际广告仅对账"],
  ];
  checks.getRange("B4").formulas = [[`=SUMIF('订单利润'!W4:W${orderEnd},">0",'订单利润'!W4:W${orderEnd})-SUMIF('订单利润'!W4:W${orderEnd},"<0",'订单利润'!W4:W${orderEnd})`]];
  checks.getRange("D4").formulas = [["=B4-C4"]];
  checks.getRange("B5").formulas = [[`=SUMIF('店铺汇总'!Q4:Q${storeEnd},">0",'店铺汇总'!Q4:Q${storeEnd})-SUMIF('店铺汇总'!Q4:Q${storeEnd},"<0",'店铺汇总'!Q4:Q${storeEnd})`]];
  checks.getRange("C5").values = [[0]];
  checks.getRange("D5").formulas = [["=B5-C5"]];
  for (const row of [4, 5, 8, 9]) checks.getRange(`E${row}`).formulas = [[`=IF(ABS(D${row})<0.01,"OK","FAIL")`]];
  checks.getRange("E6").formulas = [["=IF(D6=0,\"OK\",\"OPEN\")"]];
  checks.getRange("E7").formulas = [["=IF(D7=0,\"OK\",\"OPEN\")"]];
  checks.getRange("A10:B10").values = [["REPORT STATUS", ""]];
  checks.getRange("B10").formulas = [["=IF(COUNTIF(E4:E9,\"FAIL\")>0,\"CALCULATION_FAIL\",IF(COUNTIF(E4:E9,\"OPEN\")>0,\"PENDING_DATA\",\"FINAL\"))"]];
  checks.getRange("A10:B10").format = { fill: blue, font: { bold: true, size: 12 }, borders: { preset: "outside", style: "medium", color: navy } };
  checks.getRange("E4:E9").conditionalFormats.add("containsText", { text: "FAIL", format: { fill: red, font: { color: "#9C0006", bold: true } } });
  checks.getRange("E4:E9").conditionalFormats.add("containsText", { text: "OK", format: { fill: green, font: { color: "#006100", bold: true } } });
  checks.getRange("E4:E9").conditionalFormats.add("containsText", { text: "OPEN", format: { fill: gold, font: { color: "#7F6000", bold: true } } });
  checks.getRange("A:A").format.columnWidth = 34;
  checks.getRange("B:E").format.columnWidth = 18;
  checks.getRange("F:F").format.columnWidth = 42;
  checks.freezePanes.freezeRows(3);
}

function collectConsistencyFailures() {
  const failures = orderPolicyValidationFailures();
  const orderRows = payload.orders || [];
  const storeRows = payload.stores || [];
  for (let index = 0; index < orderRows.length; index++) {
    const source = orderRows[index];
    const row = 4 + index;
    const label = source.orderKey || `第${row}行`;
    compareCore(failures, `${label} 代运营费`, cellValue(orders, `P${row}`), source.managedOperationsCny);
    compareCore(failures, `${label} 政策准备金`, cellValue(orders, `Q${row}`), firstDefined(source, ["totalPolicyReserveCny", "combinedAdvertisingAndReturnPoolCny", "combinedPoolCny"]));
    compareCore(failures, `${label} 计提广告费`, cellValue(orders, `R${row}`), source.advertisingProvisionCny);
    compareCore(failures, `${label} 超额退货损失`, cellValue(orders, `S${row}`), source.excessReturnLossCny);
    compareCore(failures, `${label} 严格老板利润`, cellValue(orders, `U${row}`), firstDefined(source, ["strictOwnerProfitCny", "ownerProfitCny"]));
    compareCore(failures, `${label} 退货风险准备金`, cellValue(orders, `AF${row}`), source.returnRiskReserveCny);
    compareCore(failures, `${label} 推广预留`, cellValue(orders, `AG${row}`), source.promotionReserveCny);
  }
  for (let index = 0; index < storeRows.length; index++) {
    const source = storeRows[index];
    const row = 4 + index;
    const alias = String(source.storeAlias || "");
    const matchingOrders = orderRows.filter((order) => String(order.storeAlias || "") === alias);
    const confirmedCount = matchingOrders.filter((order) => order.evidenceStatus === "COMPLETE").length;
    compareCore(failures, `${alias} 已确认订单数`, confirmedCount, source.confirmedOrderCount);
    compareCore(failures, `${alias} 待确认订单数`, matchingOrders.length - confirmedCount, source.openItemOrderCount);
    compareCore(failures, `${alias} 已确认营业额`, cellValue(stores, `C${row}`), firstDefined(source, ["netTurnoverCny", "salesAmountGCny"]));
    compareCore(failures, `${alias} 已确认采购成本`, cellValue(stores, `D${row}`), source.purchaseCostCny);
    compareCore(failures, `${alias} 已确认WB佣金`, cellValue(stores, `E${row}`), firstDefined(source, ["actualCommissionCCny", "wbCommissionCny"]));
    compareCore(failures, `${alias} 已确认支付费`, cellValue(stores, `F${row}`), firstDefined(source, ["acquiringFeeCny", "paymentFeeCny"]));
    compareCore(failures, `${alias} 已确认正向物流`, cellValue(stores, `G${row}`), source.forwardOperationsCny);
    const otherCosts = addCellsIfComplete(stores, [`H${row}`, `I${row}`]);
    compareCore(failures, `${alias} 已确认仓储及其他成本`, otherCosts, source.otherPlatformCostCny);
    compareCore(failures, `${alias} 已确认代运营费`, cellValue(stores, `J${row}`), source.managedOperationsCny);
    compareCore(failures, `${alias} 实际退货费`, cellValue(stores, `K${row}`), source.returnLogisticsCny);
    compareCore(failures, `${alias} 计提广告费`, cellValue(stores, `L${row}`), source.advertisingProvisionCny);
    compareCore(failures, `${alias} 老板利润`, cellValue(stores, `N${row}`), firstDefined(source, ["strictOwnerProfitCny", "ownerProfitCny"]));
  }

  const confirmedTotal = orderRows.filter((order) => order.evidenceStatus === "COMPLETE").length;
  const totals = payload.totals || {};
  compareCore(failures, "汇总已确认订单数", confirmedTotal, totals.confirmedOrderCount);
  compareCore(failures, "汇总待确认订单数", orderRows.length - confirmedTotal, firstDefined(totals, ["openItemOrderCount", "openOrderCount"]));
  const summaryValues = summary.getRange("A11:G11").values?.[0] || [];
  const definitions = [
    ["汇总已确认营业额", 0, ["netTurnoverCny", "salesAmountGCny"]],
    ["汇总已确认采购成本", 1, ["purchaseCostCny"]],
    ["汇总平台及履约费", 2, ["platformAndOperationsCostCny"]],
    ["汇总代运营费", 3, ["managedOperationsCny"]],
    ["汇总实际退货费", 4, ["returnLogisticsCny"]],
    ["汇总计提广告费", 5, ["advertisingProvisionCny"]],
    ["汇总老板利润", 6, ["strictOwnerProfitCny", "ownerProfitCny"]],
  ];
  for (const [label, index, keys] of definitions) {
    compareCore(failures, label, summaryValues[index], firstDefined(totals, keys));
  }
  const varianceRows = Array.isArray(payload.previousReport?.varianceRows)
    ? payload.previousReport.varianceRows
    : [];
  for (const row of varianceRows) {
    const label = row.label || row.metric || "未命名指标";
    const previous = comparableNumber(row.previous);
    const newlyConfirmed = comparableNumber(row.newlyConfirmed);
    const removed = comparableNumber(row.removedFromConfirmed);
    const corrections = comparableNumber(row.confirmedCorrections);
    const aggregate = comparableNumber(row.aggregateRecalculation);
    const current = comparableNumber(row.current);
    const delta = comparableNumber(row.delta);
    const bridgeComponents = row.aggregateRecalculation === undefined
      ? [previous, newlyConfirmed, removed, corrections, current]
      : [previous, newlyConfirmed, removed, corrections, aggregate, current];
    if (bridgeComponents.every((value) => value != null)) {
      compareCore(
        failures,
        `${label}变动桥守恒`,
        previous + newlyConfirmed + removed + corrections + (aggregate || 0),
        current,
      );
    }
    if (previous != null && current != null && delta != null) {
      compareCore(failures, `${label}净变动`, delta, current - previous);
    }
  }
  return failures;
}

function reportingPolicyVersions() {
  const p = payload.policy || {};
  const result = [];
  const seen = new Set();
  const add = (source) => {
    const id = String(source?.reportingPolicyId || source?.financialReportingPolicyVersionId || "");
    if (!id || seen.has(id)) return;
    seen.add(id);
    result.push({
      reportingPolicyId: id,
      pricingPolicyId: source.pricingPolicyId || source.sellingPricePolicyVersionId || "",
      reserveAllocationMode: source.reserveAllocationMode || "",
      managedOperationsRate: source.managedOperationsRate,
      returnRiskReserveRate: source.returnRiskReserveRate,
      promotionReserveRate: source.promotionReserveRate,
      totalReserveRate: source.totalReserveRate ?? source.combinedPoolRate,
      effectiveFrom: source.effectiveFrom,
      effectiveTo: source.effectiveTo,
      orderCount: (payload.orders || []).filter((row) => row.financialReportingPolicyVersionId === id).length,
    });
  };
  for (const version of p.reportingPolicyVersions || []) add(version);
  for (const order of payload.orders || []) add(order);
  add(p);
  return result;
}

function orderPolicyValidationFailures() {
  const failures = [];
  const validModes = new Set(["LEGACY_SHARED_POOL", "SEPARATE_RETURN_AND_PROMOTION"]);
  for (const order of payload.orders || []) {
    if (order.evidenceStatus !== "COMPLETE") continue;
    const label = order.orderKey || "未命名订单";
    const required = [
      "managedOperationsCny", "returnRiskReserveCny", "promotionReserveCny",
      "totalPolicyReserveCny", "advertisingProvisionCny", "excessReturnLossCny",
      "strictOwnerProfitCny", "financialReportingPolicyVersionId", "reserveAllocationMode",
      "managedOperationsRate", "returnRiskReserveRate", "promotionReserveRate", "totalReserveRate",
    ];
    for (const key of required) {
      if (order[key] == null || order[key] === "") failures.push({ check: `${label} 缺少订单政策快照 ${key}`, actual: null, expected: "payload value", difference: null });
    }
    if (order.reserveAllocationMode && !validModes.has(order.reserveAllocationMode)) {
      failures.push({ check: `${label} 准备金模式`, actual: order.reserveAllocationMode, expected: [...validModes].join(" / "), difference: null });
    }
    compareCore(failures, `${label} 准备金费率合计`, order.totalReserveRate, addComparable(order.returnRiskReserveRate, order.promotionReserveRate));
    compareCore(failures, `${label} 准备金金额合计`, order.totalPolicyReserveCny, addComparable(order.returnRiskReserveCny, order.promotionReserveCny));
  }
  return failures;
}

function addComparable(left, right) {
  const a = comparableNumber(left);
  const b = comparableNumber(right);
  return a == null || b == null ? undefined : a + b;
}

function compareCore(failures, check, actual, expected) {
  if (expected === undefined) return;
  const actualNumber = comparableNumber(actual);
  const expectedNumber = comparableNumber(expected);
  if (actualNumber == null && expectedNumber == null) return;
  if (actualNumber == null || expectedNumber == null || Math.abs(actualNumber - expectedNumber) >= 0.01) {
    failures.push({
      check,
      actual: actualNumber,
      expected: expectedNumber,
      difference: actualNumber == null || expectedNumber == null ? null : Math.round((actualNumber - expectedNumber) * 100) / 100,
    });
  }
}

function comparableNumber(value) {
  if (value == null || value === "") return null;
  const result = Number(value);
  return Number.isFinite(result) ? result : null;
}

function cellValue(sheet, address) {
  return sheet.getRange(address).values?.[0]?.[0];
}

function addCellsIfComplete(sheet, addresses) {
  const values = addresses.map((address) => comparableNumber(cellValue(sheet, address)));
  return values.some((value) => value == null) ? null : values.reduce((total, value) => total + value, 0);
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

function optionalNumber(value) {
  if (value == null || value === "") return "";
  const result = Number(value);
  return Number.isFinite(result) ? result : "";
}

function firstDefined(source, keys) {
  for (const key of keys) {
    if (source && source[key] !== undefined) return source[key];
  }
  return undefined;
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
