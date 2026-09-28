// Offline behavioral checks only: the mock cannot connect to WPS.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../tools/wps_warehouse/airscript-sync.js'), 'utf8');
const run = new Function('Application', 'Context', source);
const HEADERS = ['序号', '平台下单 日期', '店铺名尾号', '订单编号', '型号', '内存', '颜色', 'IMEI', 'WB 标签码', '供货订单二维码', '备注'];
const RETURN_HEADERS = ['序号', '平台', '店铺名尾号', '退货原订单编号', '型号', '内存', '颜色', 'IMEI', '数量', '备注', '状态'];
function fixture(options = {}) {
  const writes = [];
  let failImage = false;
  const data = [['9发货清单'], HEADERS.slice(), ['1', 46260, 'Скиди Выбор（扬州沫德）', '5589475494', 'iPhone 14 Pro', '128GB', '黑色', 'manual-imei', '=DISPIMG("OLD_I",1)', '=DISPIMG("OLD_J",1)', '仓库手工备注'], ['2']];
  const returnData = options.returnData ? structuredClone(options.returnData) : [
    ['2026年 退货清单'], RETURN_HEADERS.slice(),
    ['1', 'WB', '', '', '', '', '', '', '', '', ''],
    ['2', '', '', '', '', '', '', '', '', '', ''],
    ['3', '', '', '', '', '', '', '', '', '', ''],
    ['4', '', '', '', '', '', '', '', '', '', ''],
    ['5', '', '', '', '', '', '', '', '', '', '']
  ];
  const sheet = {
    Name: '2026年9月发货',
    get UsedRange() {
      if (options.missingBounds) return {};
      return {Row: 1, Rows: {Count: data.length},
        RowEnd: options.noRowEnd ? undefined : data.length,
        Address() { return '$A$1:$L$' + (data.length + (options.badAddress ? 1 : 0)); }};
    },
    Range(address) {
      const found = /^([A-L])(\d+)(?::([A-L])(\d+))?$/.exec(address);
      assert.ok(found, address);
      const c1 = found[1].charCodeAt(0) - 65, r1 = Number(found[2]) - 1;
      const c2 = (found[3] || found[1]).charCodeAt(0) - 65, r2 = Number(found[4] || found[2]) - 1;
      const read = (r, c) => data[r] && data[r][c] !== undefined ? data[r][c] : '';
      const range = {
        get Value2() {
          if (options.onRead) options.onRead(address, data, writes);
          if (options.missingFeedbackCell === address) return undefined;
          if (!found[3]) return options.missingScalar && address === 'K3' ? undefined : read(r1, c1);
          const matrix = Array.from({length: r2 - r1 + 1}, (_, i) => Array.from({length: c2 - c1 + 1}, (_, j) => read(r1 + i, c1 + j)));
          if (options.badBulk) return matrix.map(row => row.slice(0, 10));
          return options.singleRowVector && r1 === r2 ? matrix[0] : matrix;
        },
        set Value2(values) {
          writes.push(['value', address]);
          for (let r = r1; r <= r2; r++) {
            data[r] ||= [];
            for (let c = c1; c <= c2; c++) data[r][c] = found[3] ? values[r - r1][c - c1] : values;
          }
          if (options.afterWrite) options.afterWrite(address, data, writes);
        },
        get Formula() { return options.formulas && Object.hasOwn(options.formulas, address) ? options.formulas[address] : String(read(r1, c1)); },
        get Text() { return c1 === 1 && read(r1, c1) === 46260 ? '2026/8/26' : String(read(r1, c1)); },
        get MergeCells() { return options.unknownMerge ? null : false; },
        set NumberFormat(value) { writes.push(['format', address, value]); },
        InsertImage(value) {
          writes.push(['image', address]);
          if (failImage) throw new Error('Simulated image failure');
          data[r1][c1] = '=DISPIMG("NEW_' + address + '",1)';
        },
        get EntireRow() {
          if (options.noDelete) return {};
          return {Delete() {
            writes.push(['delete-row', r1 + 1]);
            if (options.deleteFailureRow === r1 + 1) throw new Error('Simulated row delete failure');
            data.splice(r1, r2 - r1 + 1);
            if (options.afterDelete) options.afterDelete(address, data, writes);
          }};
        }
      };
      if (options.noImage) delete range.InsertImage;
      return range;
    }
  };
  const returnSheet = {
    Name: '2026年退货',
    get UsedRange() {
      return {Row: 1, Rows: {Count: returnData.length}, RowEnd: returnData.length,
        Address() { return '$A$1:$N$' + returnData.length; }};
    },
    Range(address) {
      const found = /^([A-L])(\d+)(?::([A-L])(\d+))?$/.exec(address);
      assert.ok(found, address);
      const c1 = found[1].charCodeAt(0) - 65, r1 = Number(found[2]) - 1;
      const c2 = (found[3] || found[1]).charCodeAt(0) - 65, r2 = Number(found[4] || found[2]) - 1;
      const read = (r, c) => returnData[r] && returnData[r][c] !== undefined ? returnData[r][c] : '';
      return {
        get Value2() {
          if (options.returnOnRead) options.returnOnRead(address, returnData, writes);
          if (!found[3]) return read(r1, c1);
          return Array.from({length: r2 - r1 + 1}, (_, i) =>
            Array.from({length: c2 - c1 + 1}, (_, j) => read(r1 + i, c1 + j)));
        },
        set Value2(values) {
          writes.push(['value', address]);
          for (let r = r1; r <= r2; r++) {
            returnData[r] ||= [];
            for (let c = c1; c <= c2; c++) returnData[r][c] = found[3] ? values[r - r1][c - c1] : values;
          }
          if (options.returnAfterWrite) options.returnAfterWrite(address, returnData, writes);
        },
        get Formula() {
          const formulas = options.returnFormulas || {};
          return Object.hasOwn(formulas, address) ? formulas[address] : String(read(r1, c1));
        },
        get Text() { return String(read(r1, c1)); },
        get MergeCells() { return options.returnUnknownMerge ? null : false; },
        set NumberFormat(value) { writes.push(['format', address, value]); }
      };
    }
  };
  const duplicateSheet = Object.create(sheet);
  duplicateSheet.Name = '2026年8月发货';
  const App = {ActiveWorkbook: {ReadOnly: options.readOnly === true}, Sheets: {Count: options.duplicateShippingSheet ? 3 : 2, Item(index) {
    if (options.duplicateShippingSheet && (index === 3 || index === duplicateSheet.Name)) return duplicateSheet;
    if (index === 2 || index === returnSheet.Name) return returnSheet;
    return sheet;
  }}};
  const execute = argv => run(App, {argv});
  return {data, returnData, writes, execute, failImage(value) { failImage = value; }};
}
function operation() {
  return {operation_id: 'a'.repeat(24), action: 'append', sheet: '2026年9月发货', row_number: 4, store_alias: 'new-store', order_id: '9999999999', required_image_kinds: ['order_sticker', 'supply_qr'],
    values: ['2', '2026/09/04', 'Скиди Выбор（扬州沫德）', '9999999999', 'iPhone 14', '128GB', '黑色', '', '', '', '待仓库处理']};
}
const images = {order_sticker: 'data:image/png;base64,YQ==', supply_qr: 'data:image/png;base64,Yg=='};
let checked = 0;
function check(name, body) { body(); checked++; }
check('read is read only and converts dates / retains image formulas', () => {
  const f = fixture(), r = f.execute({});
  assert.equal(r.status, 'ok');
  assert.equal(r.build, '20260908-r11');
  assert.deepEqual(r.store_names, {'new-store': '沫德DL05', 'owner-store': '瑞盈8597'});
  assert.equal(r.snapshot.complete, true);
  assert.equal(r.snapshot.sheets[0].rows[0].values[1], '2026/8/26');
  assert.match(r.snapshot.sheets[0].rows[0].values[8], /^=DISPIMG/);
  assert.deepEqual(f.writes, []);
});
check('2.0 style used-range bounds do not require RowEnd', () => {
  const f = fixture({noRowEnd: true});
  assert.equal(f.execute({}).snapshot.complete, true);
  assert.deepEqual(f.writes, []);
});
check('incomplete bulk response is recovered by reading every real scalar cell', () => {
  const f = fixture({badBulk: true});
  const result = f.execute({});
  assert.equal(result.snapshot.complete, true);
  assert.equal(result.snapshot.sheets[0].rows[0].values[7], 'manual-imei');
  assert.equal(result.snapshot.sheets[0].rows[0].values[10], '仓库手工备注');
  assert.deepEqual(f.writes, []);
});
check('an unreadable scalar cannot be fabricated as an empty cell', () => {
  const f = fixture({badBulk: true, missingScalar: true});
  assert.throws(() => f.execute({}), /Unverified scalar cell value/);
  assert.deepEqual(f.writes, []);
});
check('unknown and inconsistent bounds cannot become complete snapshots', () => {
  const unknown = fixture({missingBounds: true});
  assert.throws(() => unknown.execute({}), /bounds unavailable/);
  assert.equal(unknown.execute({action: 'diagnose'}).complete, false);
  assert.deepEqual(unknown.writes, []);
  assert.throws(() => fixture({badAddress: true}).execute({}), /Inconsistent/);
});
check('missing image API blocks before text or format changes', () => {
  const f = fixture({noImage: true});
  assert.throws(() => f.execute({action: 'apply', expected_state_token: f.execute({}).state_token, operation: operation(), images}), /image API is unavailable/);
  assert.deepEqual(f.writes, []);
});
check('ambiguous merge metadata blocks before mutation', () => {
  const f = fixture({unknownMerge: true});
  assert.throws(() => f.execute({action: 'apply', expected_state_token: f.execute({}).state_token, operation: operation(), images}), /merge state is unknown/);
  assert.deepEqual(f.writes, []);
});
check('a complete single-row vector is accepted without padding data', () => {
  const f = fixture({singleRowVector: true});
  const r = f.execute({action: 'apply', expected_state_token: f.execute({}).state_token, operation: operation(), images});
  assert.equal(r.status, 'verified');
});
check('stale snapshot stops without mutation', () => {
  const f = fixture(), token = f.execute({}).state_token;
  f.data[2][10] = 'another collaborator changed this';
  assert.throws(() => f.execute({action: 'apply', expected_state_token: token, operation: operation(), images}), /changed/);
  assert.deepEqual(f.writes, []);
});
check('occupied destination stops without mutation', () => {
  const f = fixture(); f.data[3][7] = 'manual';
  const token = f.execute({}).state_token;
  assert.throws(() => f.execute({action: 'apply', expected_state_token: token, operation: operation(), images}), /no longer empty/);
  assert.deepEqual(f.writes, []);
});
check('apply reuses preprinted sequence and verifies images without changing previous row/title', () => {
  const f = fixture(), saved = JSON.stringify(f.data.slice(0, 3));
  const r = f.execute({action: 'apply', expected_state_token: f.execute({}).state_token, operation: operation(), images});
  assert.equal(r.status, 'verified');
  assert.deepEqual(r.inserted_image_kinds, ['order_sticker', 'supply_qr']);
  assert.equal(f.data[3][3], '9999999999');
  assert.equal(JSON.stringify(f.data.slice(0, 3)), saved);
});
check('existing order does not append twice', () => {
  const f = fixture(), op = operation(); op.order_id = op.values[3] = '5589475494';
  const r = f.execute({action: 'apply', expected_state_token: f.execute({}).state_token, operation: op, images});
  assert.equal(r.status, 'already_present');
  assert.deepEqual(f.writes, []);
});
check('partial image failure can repair only blank images', () => {
  const f = fixture(); f.failImage(true);
  const partial = f.execute({action: 'apply', expected_state_token: f.execute({}).state_token, operation: operation(), images});
  assert.equal(partial.status, 'partial');
  f.failImage(false);
  const repaired = f.execute({action: 'repair_images', expected_state_token: f.execute({}).state_token, operation: operation(), images});
  assert.equal(repaired.status, 'verified');
  assert.equal(f.writes.filter(w => w[0] === 'value').length, 1);
});
check('repair refuses a changed manual field', () => {
  const f = fixture(); f.failImage(true);
  f.execute({action: 'apply', expected_state_token: f.execute({}).state_token, operation: operation(), images});
  f.data[3][7] = 'warehouse IMEI'; f.writes.length = 0;
  assert.throws(() => f.execute({action: 'repair_images', expected_state_token: f.execute({}).state_token, operation: operation(), images}), /edited field/);
  assert.deepEqual(f.writes, []);
});
check('restoration must preserve manual fields and explanation', () => {
  const f = fixture(), op = operation(); op.action = 'restore'; op.restore_number = 1; op.preserved_manual = {imei: 'saved', note: 'saved note'};
  op.values[10] += '原表记录缺失，仍待仓库处理，自动补回，第1次';
  assert.throws(() => f.execute({action: 'apply', expected_state_token: f.execute({}).state_token, operation: op, images}), /Archived IMEI/);
  assert.deepEqual(f.writes, []);
});
function correction(snapshot, alias = 'new-store', rowNumber = 3) {
  const row = snapshot.sheets[0].rows.find(item => item.row_number === rowNumber);
  return {store_alias: alias, order_id: row.values[3], sheet: '2026年9月发货', row_number: rowNumber, expected_values: row.values.slice()};
}
function correctionRequest(read, corrections) {
  return {action: 'correct_store_names', expected_state_token: read.state_token, corrections};
}
check('canonical append names work and historical names remain confined to their own store', () => {
  for (const [alias, name] of [['new-store', '沫德DL05'], ['owner-store', '瑞盈8597'], ['owner-store', 'Скиди（香港瑞盈）']]) {
    const f = fixture(), op = operation(); op.store_alias = alias; op.values[2] = name;
    assert.equal(f.execute({action: 'apply', expected_state_token: f.execute({}).state_token, operation: op, images}).status, 'verified');
  }
  const f = fixture(), op = operation(); op.values[2] = '瑞盈8597';
  assert.throws(() => f.execute({action: 'apply', expected_state_token: f.execute({}).state_token, operation: op, images}), /identity mismatch/);
  op.values[2] = 'Скиди（香港瑞盈）';
  assert.throws(() => f.execute({action: 'apply', expected_state_token: f.execute({}).state_token, operation: op, images}), /identity mismatch/);
  assert.deepEqual(f.writes, []);
});
check('store correction changes only C and preserves titles, dates, manual fields and image formulas', () => {
  const f = fixture();
  f.data[2][2] = '此前手工填写的其他店名';
  f.data[3] = ['2', 46260, '旧店铺标识', '8888888888', 'iPhone 14', '128GB', '蓝色', '', '', '', '另一行备注'];
  const saved = structuredClone(f.data), before = f.execute({});
  const batch = [correction(before.snapshot), correction(before.snapshot, 'owner-store', 4)];
  const result = f.execute(correctionRequest(before, batch));
  saved[2][2] = '沫德DL05'; saved[3][2] = '瑞盈8597';
  assert.equal(result.status, 'verified');
  assert.deepEqual(f.data, saved);
  assert.deepEqual(f.writes, [['value', 'C3'], ['value', 'C4']]);
  assert.deepEqual(result.corrected, batch.map(item => ({order_id: item.order_id, store_alias: item.store_alias, row_number: item.row_number,
    from: item.expected_values[2], to: item.store_alias === 'new-store' ? '沫德DL05' : '瑞盈8597'})));
  assert.equal(result.snapshot.sheets[0].rows[0].values[2], '沫德DL05');
  f.writes.length = 0;
  const unchanged = f.execute({});
  assert.equal(f.execute(correctionRequest(unchanged, [correction(unchanged.snapshot)])).status, 'verified');
  assert.deepEqual(f.writes, []);
});
check('correction stale snapshots and wrong rows block with the actual snapshot', () => {
  const f = fixture(), before = f.execute({}), item = correction(before.snapshot);
  f.data[2][10] = 'concurrent note';
  let result = f.execute(correctionRequest(before, [item]));
  assert.equal(result.status, 'blocked');
  assert.match(result.error, /changed/);
  assert.equal(result.snapshot.sheets[0].rows[0].values[10], 'concurrent note');
  const current = f.execute({}), wrongRow = correction(current.snapshot); wrongRow.row_number = 4;
  result = f.execute(correctionRequest(current, [wrongRow]));
  assert.equal(result.status, 'blocked');
  assert.match(result.error, /unique exact order row/);
  assert.deepEqual(f.writes, []);
});
check('same order in another shipping sheet blocks the correction', () => {
  const f = fixture({duplicateShippingSheet: true}), before = f.execute({});
  const result = f.execute(correctionRequest(before, [correction(before.snapshot)]));
  assert.equal(result.status, 'blocked');
  assert.match(result.error, /unique exact order row/);
  assert.deepEqual(f.writes, []);
});
check('whole-batch validation rejects duplicate orders, unknown stores and caller-supplied destinations', () => {
  for (const variant of ['duplicate', 'unknown', 'extra', 'empty', 'oversized', 'nontext', 'mismatch']) {
    const f = fixture(), before = f.execute({}), first = correction(before.snapshot);
    let batch = [first];
    if (variant === 'duplicate') batch.push(structuredClone(first));
    if (variant === 'unknown') batch.push({...first, store_alias: 'unknown'});
    if (variant === 'extra') first.to = 'arbitrary text';
    if (variant === 'empty') batch = [];
    if (variant === 'oversized') batch = Array(101).fill(first);
    if (variant === 'nontext') first.expected_values[0] = 1;
    if (variant === 'mismatch') first.expected_values[7] = 'wrong archived manual field';
    const result = f.execute(correctionRequest(before, batch));
    assert.equal(result.status, 'blocked', variant);
    assert.deepEqual(f.writes, [], variant);
  }
});
check('store formulas, unknown merge state and read-only workbooks block correction', () => {
  for (const options of [{formula: true}, {unknownMerge: true}, {readOnly: true}]) {
    const f = fixture(options); if (options.formula) f.data[2][2] = '=A1';
    const before = f.execute({});
    const result = f.execute(correctionRequest(before, [correction(before.snapshot)]));
    assert.equal(result.status, 'blocked');
    assert.match(result.error, /formula|merge|read only/);
    assert.deepEqual(f.writes, []);
  }
});
check('second complete snapshot detects edits during correction preflight', () => {
  const options = {}, f = fixture(options), before = f.execute({});
  let reads = 0;
  options.onRead = (address, data) => {
    if (address === 'A1:K4' && ++reads === 2) data[2][10] = 'edited during preflight';
  };
  const result = f.execute(correctionRequest(before, [correction(before.snapshot)]));
  assert.equal(result.status, 'blocked');
  assert.match(result.error, /preflight/);
  assert.equal(result.snapshot.sheets[0].rows[0].values[10], 'edited during preflight');
  assert.deepEqual(f.writes, []);
});
check('last-moment row edits block before the first C write', () => {
  const options = {}, f = fixture(options), before = f.execute({});
  options.onRead = (address, data) => { if (address === 'A3:K3') data[2][7] = 'last-moment manual value'; };
  const result = f.execute(correctionRequest(before, [correction(before.snapshot)]));
  assert.equal(result.status, 'blocked');
  assert.match(result.error, /row changed/);
  assert.equal(f.data[2][2], before.snapshot.sheets[0].rows[0].values[2]);
  assert.deepEqual(f.writes, []);
});
check('a later row edit stops a batch and retains earlier C writes without overwriting manual data', () => {
  const options = {}, f = fixture(options);
  f.data[3] = ['2', 46260, '历史店名', '8888888888', 'iPhone 14', '128GB', '蓝色', '', '', '', 'original note'];
  const before = f.execute({});
  options.afterWrite = (address, data) => { if (address === 'C3') data[3][10] = 'edited during batch'; };
  const result = f.execute(correctionRequest(before, [correction(before.snapshot), correction(before.snapshot, 'owner-store', 4)]));
  assert.equal(result.status, 'partial');
  assert.match(result.error, /row changed/);
  assert.deepEqual(f.writes, [['value', 'C3']]);
  assert.equal(f.data[2][2], '沫德DL05');
  assert.equal(f.data[3][2], '历史店名');
  assert.equal(f.data[3][10], 'edited during batch');
  assert.equal(result.corrected.length, 1);
  assert.equal(result.snapshot.sheets[0].rows[1].values[10], 'edited during batch');
});
check('final complete snapshot detects unrelated edits and never rolls them back', () => {
  const options = {}, f = fixture(options), before = f.execute({});
  options.afterWrite = (address, data) => { if (address === 'C3') data[0][0] = 'collaborator changed title'; };
  const result = f.execute(correctionRequest(before, [correction(before.snapshot)]));
  assert.equal(result.status, 'partial');
  assert.match(result.error, /outside the permitted/);
  assert.equal(result.snapshot.sheets[0].title[0], 'collaborator changed title');
  assert.equal(f.data[2][2], '沫德DL05');
  assert.deepEqual(f.writes, [['value', 'C3']]);
});
check('L feedback, formulas and headings are read separately while values remain eleven columns', () => {
  const f = fixture({formulas: {L3: '=IF(A3=1,"已核实","")'}});
  f.data[0][11] = '仓库反馈区'; f.data[1][11] = '补充信息';
  f.data[2][11] = '已核实'; f.data[3][11] = 123;
  const result = f.execute({}), sheet = result.snapshot.sheets[0];
  assert.equal(result.snapshot.read_columns, 'A:L');
  assert.equal(result.snapshot.warehouse_feedback_complete, true);
  assert.equal(sheet.warehouse_feedback_title, '仓库反馈区');
  assert.equal(sheet.warehouse_feedback_header, '补充信息');
  assert.equal(sheet.rows[0].warehouse_feedback, '已核实');
  assert.equal(sheet.rows[0].warehouse_feedback_formula, '=IF(A3=1,"已核实","")');
  assert.equal(sheet.rows[1].warehouse_feedback, '123');
  assert.equal(sheet.rows[1].warehouse_feedback_formula, '');
  assert.equal(sheet.rows[0].values.length, 11);
  assert.equal(sheet.headers.length, 11);
  assert.equal(sheet.title.length, 11);
  assert.deepEqual(JSON.parse(result.state_token).sheets[0], sheet);
  assert.deepEqual(f.writes, []);
  const diagnostic = f.execute({action: 'diagnose'});
  assert.equal(diagnostic.complete, false);
  assert.equal(diagnostic.warehouse_feedback_complete, false);
});
check('rows with only L feedback or an empty-result L formula are retained', () => {
  const f = fixture({formulas: {L5: '=""'}});
  f.data[3] = Array(12).fill(''); f.data[3][11] = '独立仓库备注';
  f.data[4] = Array(12).fill('');
  const rows = f.execute({}).snapshot.sheets[0].rows;
  assert.equal(rows.length, 3);
  assert.deepEqual(rows[1].values, Array(11).fill(''));
  assert.equal(rows[1].warehouse_feedback, '独立仓库备注');
  assert.equal(rows[2].row_number, 5);
  assert.equal(rows[2].warehouse_feedback, '');
  assert.equal(rows[2].warehouse_feedback_formula, '=""');
  assert.deepEqual(f.writes, []);
});
check('unreadable or nonscalar L cells cannot produce a complete snapshot or start a write', () => {
  for (const options of [{missingFeedbackCell: 'L1'}, {missingFeedbackCell: 'L2'}, {missingFeedbackCell: 'L3'}, {missingFeedbackCell: 'L4'}, {invalidFeedback: true}, {formulas: {L3: undefined}}]) {
    const f = fixture(options); if (options.invalidFeedback) f.data[2][11] = {unexpected: 'cell object'};
    assert.throws(() => f.execute({}), /Unverified warehouse feedback scalar/);
    assert.throws(() => f.execute({action: 'apply', expected_state_token: 'unverified', operation: operation(), images}), /Unverified warehouse feedback scalar/);
    assert.deepEqual(f.writes, []);
  }
});
check('changed L values, headings or formulas invalidate old tokens for apply and store correction', () => {
  for (const target of ['title', 'header', 'value', 'formula']) {
    const options = {formulas: {}}, f = fixture(options), before = f.execute({});
    if (target === 'title') f.data[0][11] = 'new feedback title';
    if (target === 'header') f.data[1][11] = 'new feedback header';
    if (target === 'value') f.data[2][11] = 'new warehouse feedback';
    if (target === 'formula') options.formulas.L3 = '=""';
    assert.throws(() => f.execute({action: 'apply', expected_state_token: before.state_token, operation: operation(), images}), /changed/);
    const result = f.execute(correctionRequest(before, [correction(before.snapshot)]));
    assert.equal(result.status, 'blocked', target);
    assert.match(result.error, /changed/);
    assert.deepEqual(f.writes, []);
  }
  const options = {}, f = fixture(options), before = f.execute({});
  let reads = 0;
  options.onRead = (address, data) => { if (address === 'A1:K4' && ++reads === 2) data[2][11] = 'feedback during preflight'; };
  assert.throws(() => f.execute({action: 'apply', expected_state_token: before.state_token, operation: operation(), images}), /preflight/);
  assert.deepEqual(f.writes, []);
});
check('post-correction comparison includes L feedback and preserves concurrent feedback edits', () => {
  const options = {}, f = fixture(options), before = f.execute({});
  options.afterWrite = (address, data) => { if (address === 'C3') data[2][11] = 'warehouse feedback during correction'; };
  const result = f.execute(correctionRequest(before, [correction(before.snapshot)]));
  assert.equal(result.status, 'partial');
  assert.match(result.error, /outside the permitted/);
  assert.equal(result.snapshot.sheets[0].rows[0].warehouse_feedback, 'warehouse feedback during correction');
  assert.equal(f.data[2][2], '沫德DL05');
  assert.deepEqual(f.writes, [['value', 'C3']]);
});
check('append, restore, image repair and store correction never write or format L', () => {
  for (const kind of ['append', 'restore', 'repair_images', 'correct_store_names']) {
    const f = fixture(), op = operation();
    f.data[0][11] = 'feedback title'; f.data[1][11] = 'feedback header';
    f.data[2][11] = 'existing order feedback'; f.data[3][11] = 'destination feedback';
    const saved = f.data.map(row => row[11]);
    let result;
    if (kind === 'correct_store_names') {
      const before = f.execute({}); result = f.execute(correctionRequest(before, [correction(before.snapshot)]));
    } else {
      if (kind === 'restore') {
        op.action = 'restore'; op.restore_number = 1;
        op.values[10] += '原表记录缺失，仍待仓库处理，自动补回，第1次';
      }
      if (kind === 'repair_images') {
        f.failImage(true);
        assert.equal(f.execute({action: 'apply', expected_state_token: f.execute({}).state_token, operation: op, images}).status, 'partial');
        f.failImage(false);
      }
      result = f.execute({action: kind === 'repair_images' ? 'repair_images' : 'apply', expected_state_token: f.execute({}).state_token, operation: op, images});
    }
    assert.equal(result.status, 'verified', kind);
    assert.deepEqual(f.data.map(row => row[11]), saved, kind);
    assert.ok(f.writes.every(write => !/L\d/.test(write[1])), kind);
    assert.equal(result.snapshot.warehouse_feedback_complete, true);
  }
});
function statusNoteRequest(f, overrides = {}) {
  f.data[2][2] = '沫德DL05';
  const before = f.execute({});
  const operation = {store_alias: 'new-store', order_id: '5589475494', row_number: 3,
    expected_old_note: '仓库手工备注', new_note: 'WB已分拣，请按最新状态处理。', ...(overrides.operation || {})};
  return {before, request: {action: 'update_status_notes', sheet: '2026年9月发货', operation,
    expected_state_token: before.state_token, request_id: 'status-note-offline-1', ...overrides,
    operation}};
}
check('status-note update changes only K and requires independent HTTP readback', () => {
  const f = fixture(), {request} = statusNoteRequest(f), saved = structuredClone(f.data);
  const result = f.execute(request);
  saved[2][10] = request.operation.new_note;
  assert.equal(result.status, 'applied_pending_readback');
  assert.equal(result.build, '20260908-r11');
  assert.equal(result.snapshot, null);
  assert.equal(result.state_token, null);
  assert.equal(result.independent_http_readback_required, true);
  assert.deepEqual(f.data, saved);
  assert.deepEqual(f.writes, [['value', 'K3']]);
  assert.equal(f.data[2][11] || '', '');
});
check('status-note update blocks stale old notes and every wrong identity field', () => {
  for (const variant of ['old-note', 'store', 'order', 'row']) {
    const f = fixture();
    const change = variant === 'old-note' ? {expected_old_note: '过期备注'} :
      variant === 'store' ? {store_alias: 'owner-store'} :
      variant === 'order' ? {order_id: '9999999999'} : {row_number: 4};
    const {request} = statusNoteRequest(f, {operation: change});
    const result = f.execute(request);
    assert.equal(result.status, 'blocked', variant);
    assert.deepEqual(f.writes, [], variant);
  }
});
check('status-note update blocks stale full snapshots and duplicate shipping identities', () => {
  const stale = fixture(), planned = statusNoteRequest(stale);
  stale.data[0][0] = 'collaborator changed title';
  assert.equal(stale.execute(planned.request).status, 'blocked');
  assert.deepEqual(stale.writes, []);
  const duplicate = fixture({duplicateShippingSheet: true}), duplicated = statusNoteRequest(duplicate);
  const result = duplicate.execute(duplicated.request);
  assert.equal(result.status, 'blocked');
  assert.match(result.error, /unique exact/);
  assert.deepEqual(duplicate.writes, []);
});
check('status-note K formula, merged state and read-only workbook block before writing', () => {
  for (const options of [{formulas: {K3: '=A1'}}, {unknownMerge: true}, {readOnly: true}]) {
    const f = fixture(options), {request} = statusNoteRequest(f), result = f.execute(request);
    assert.equal(result.status, 'blocked');
    assert.match(result.error, /formula|merged|read only/);
    assert.deepEqual(f.writes, []);
  }
});
check('status-note input is one bounded plain-text operation', () => {
  for (const variant of ['array', 'empty', 'long', 'formula']) {
    const f = fixture(), built = statusNoteRequest(f), request = built.request;
    if (variant === 'array') request.operation = [request.operation];
    if (variant === 'empty') request.operation.new_note = '   ';
    if (variant === 'long') request.operation.new_note = 'x'.repeat(501);
    if (variant === 'formula') request.operation.new_note = '=A1';
    const result = f.execute(request);
    assert.equal(result.status, 'blocked', variant);
    assert.deepEqual(f.writes, [], variant);
  }
});
check('WPS wrapper metadata is ignored and cannot expand the fixed K mutation', () => {
  const f = fixture(), {request} = statusNoteRequest(f), saved = structuredClone(f.data);
  request.sheet_name = 'host-injected';
  request.column = 'A';
  request.operation.column = 'A';
  request.operation.row_number_override = 99;
  const result = f.execute(request);
  saved[2][10] = request.operation.new_note;
  assert.equal(result.status, 'applied_pending_readback');
  assert.deepEqual(f.data, saved);
  assert.deepEqual(f.writes, [['value', 'K3']]);
});
check('second complete A:L snapshot detects any concurrent edit before K write', () => {
  const options = {}, f = fixture(options), {request} = statusNoteRequest(f);
  let reads = 0;
  options.onRead = (address, data) => {
    if (address === 'A1:K4' && ++reads === 2) data[2][11] = 'concurrent L feedback';
  };
  const result = f.execute(request);
  assert.equal(result.status, 'blocked');
  assert.match(result.error, /preflight/);
  assert.deepEqual(f.writes, []);
  assert.equal(f.data[2][10], request.operation.expected_old_note);
});
function orderStatusBatch(options = {}) {
  const f = fixture(options);
  f.data[2][2] = '沫德DL05';
  f.data[2][11] = '';
  f.data[3] = ['2', 46260, '瑞盈8597', '5667302901', 'iPhone 15', '512GB', '黑色', '', '', '', '人工备注', ''];
  const before = f.execute({});
  const operations = [
    {store_alias: 'new-store', order_id: '5589475494', row_number: 3,
      expected_old_status: '', new_status: '运输中（WB已接收，分拣中）'},
    {store_alias: 'owner-store', order_id: '5667302901', row_number: 4,
      expected_old_status: '', new_status: '运输中（WB已接收，分拣中）'}
  ];
  return {f, before, operations, request: {action: 'update_order_statuses', sheet: '2026年9月发货',
    expected_state_token: before.state_token, request_id: 'order-status-offline-1', operations}};
}
check('order-status batch changes only exact L cells and requires independent readback', () => {
  const {f, request, operations} = orderStatusBatch(), saved = structuredClone(f.data);
  const result = f.execute(request);
  saved[2][11] = operations[0].new_status; saved[3][11] = operations[1].new_status;
  assert.equal(result.status, 'applied_pending_readback');
  assert.equal(result.build, '20260908-r11');
  assert.equal(result.snapshot, null);
  assert.equal(result.state_token, null);
  assert.equal(result.independent_http_readback_required, true);
  assert.deepEqual(result.receipts.map(item => item.status), ['applied_pending_readback', 'applied_pending_readback']);
  assert.deepEqual(f.data, saved);
  assert.deepEqual(f.writes, [['value', 'L3'], ['value', 'L4']]);
});
check('order-status action is idempotent with a fresh token and performs no write', () => {
  const built = orderStatusBatch(), {f} = built;
  f.data[2][11] = built.operations[0].new_status;
  const before = f.execute({});
  const operation = {...built.operations[0], expected_old_status: '先前状态'};
  const result = f.execute({action: 'update_order_statuses', sheet: '2026年9月发货',
    expected_state_token: before.state_token, request_id: 'order-status-idempotent', operations: [operation]});
  assert.equal(result.status, 'already_applied');
  assert.equal(result.receipts[0].status, 'already_applied');
  assert.equal(result.independent_http_readback_required, false);
  assert.equal(result.snapshot.sheets[0].rows[0].warehouse_feedback, operation.new_status);
  assert.deepEqual(f.writes, []);
});
check('order-status batch rejects stale tokens, duplicate orders and wrong exact identities', () => {
  for (const variant of ['stale', 'duplicate', 'store', 'row', 'table-duplicate']) {
    const options = variant === 'table-duplicate' ? {duplicateShippingSheet: true} : {};
    const built = orderStatusBatch(options), {f, request} = built;
    if (variant === 'stale') f.data[0][0] = 'changed after planning';
    if (variant === 'duplicate') request.operations.push({...request.operations[0]});
    if (variant === 'store') request.operations[0].store_alias = 'owner-store';
    if (variant === 'row') request.operations[0].row_number = 4;
    const result = f.execute(request);
    assert.equal(result.status, 'blocked', variant);
    assert.deepEqual(f.writes, [], variant);
  }
});
check('order-status formulas, merged cells, read-only workbooks and unsafe text block every write', () => {
  for (const variant of ['formula-cell', 'merged', 'read-only', 'formula-text', 'empty', 'long', 'control']) {
    const options = variant === 'formula-cell' ? {formulas: {L3: '=A1'}} :
      variant === 'merged' ? {unknownMerge: true} : variant === 'read-only' ? {readOnly: true} : {};
    const built = orderStatusBatch(options), {f, request} = built;
    if (variant === 'formula-text') request.operations[0].new_status = '  =A1';
    if (variant === 'empty') request.operations[0].new_status = '   ';
    if (variant === 'long') request.operations[0].new_status = 'x'.repeat(501);
    if (variant === 'control') request.operations[0].new_status = '待发货\u0001';
    const result = f.execute(request);
    assert.equal(result.status, 'blocked', variant);
    assert.deepEqual(f.writes, [], variant);
  }
});
check('order-status caller metadata cannot redirect the fixed L writes', () => {
  const {f, request} = orderStatusBatch();
  request.column = 'A'; request.operations[0].column = 'A'; request.operations.length = 1;
  const result = f.execute(request);
  assert.equal(result.status, 'applied_pending_readback');
  assert.deepEqual(f.writes, [['value', 'L3']]);
});
check('later row changes return partial receipts without redirecting a subsequent L write', () => {
  const options = {}, built = orderStatusBatch(options), {f, request} = built;
  options.afterWrite = (address, data) => { if (address === 'L3') data[3][3] = 'collaborator-replaced-order'; };
  const result = f.execute(request);
  assert.equal(result.status, 'partial');
  assert.equal(result.independent_http_readback_required, true);
  assert.deepEqual(result.receipts.map(item => item.status), ['applied_pending_readback', 'not_attempted']);
  assert.deepEqual(f.writes, [['value', 'L3']]);
  assert.equal(f.data[2][11], request.operations[0].new_status);
  assert.equal(f.data[3][11], '');
});
function cancellationDeleteBatch(options = {}) {
  const f = fixture(options);
  f.data[2] = ['1', 46260, '沫德DL05', '5685827269', 'iPhone 14 Pro', '128GB', '黑色', '', '', '', '取消前仓库备注', '发货前取消'];
  f.data[3] = ['2', 46260, '沫德DL05', '5685829340', 'iPhone 15', '256GB', '蓝色', '', '', '', '另一条仓库备注', '发货前取消'];
  const before = f.execute({});
  const operations = before.snapshot.sheets[0].rows.map((row, index) => ({
    store_alias: 'new-store', order_id: row.values[3], row_number: row.row_number,
    expected_values: row.values.slice(), expected_warehouse_feedback: row.warehouse_feedback,
    expected_warehouse_feedback_formula: row.warehouse_feedback_formula,
    expected_a_l_sha256: String(index + 1).repeat(64),
    evidence_code: 'PRE_SHIPMENT_CANCELLATION_VERIFIED', evidence_sha256: String(index + 3).repeat(64)
  }));
  return {f, before, operations, request: {action: 'delete_pre_shipment_cancellations',
    sheet: '2026年9月发货', request_id: 'cancel-delete-offline-1',
    expected_state_token: before.state_token, operations}};
}
check('pre-shipment cancellations are fully preflighted and deleted in descending row order', () => {
  const built = cancellationDeleteBatch({formulas: {L3: '=IF(A3=1,"发货前取消","")'}});
  const {f, request} = built;
  request.operations[0].expected_warehouse_feedback_formula = '=IF(A3=1,"发货前取消","")';
  // Rebuild the token and exact operations so the L formula is part of the bound snapshot.
  const before = f.execute({});
  request.expected_state_token = before.state_token;
  request.operations = before.snapshot.sheets[0].rows.map((row, index) => ({
    store_alias: 'new-store', order_id: row.values[3], row_number: row.row_number,
    expected_values: row.values.slice(), expected_warehouse_feedback: row.warehouse_feedback,
    expected_warehouse_feedback_formula: row.warehouse_feedback_formula,
    expected_a_l_sha256: String(index + 1).repeat(64),
    evidence_code: 'PRE_SHIPMENT_CANCELLATION_VERIFIED', evidence_sha256: String(index + 3).repeat(64)
  }));
  const result = f.execute(request);
  assert.equal(result.status, 'applied_pending_readback');
  assert.equal(result.build, '20260908-r11');
  assert.equal(result.snapshot, null);
  assert.equal(result.state_token, null);
  assert.equal(result.independent_http_readback_required, true);
  assert.deepEqual(result.receipts.map(item => [item.row_number, item.status]), [
    [4, 'applied_pending_readback'], [3, 'applied_pending_readback']]);
  assert.deepEqual(f.writes, [['delete-row', 4], ['delete-row', 3]]);
  assert.equal(f.data.length, 2);
});
check('cancellation delete rejects stale snapshots, wrong exact identities and invalid evidence bindings', () => {
  const variants = ['stale', 'values', 'l-text', 'l-formula', 'store', 'order', 'row', 'duplicate-order',
    'duplicate-sheet', 'sheet', 'state-hash', 'evidence-code', 'evidence-hash', 'extra-field'];
  for (const variant of variants) {
    const built = cancellationDeleteBatch(variant === 'duplicate-sheet' ? {duplicateShippingSheet: true} : {});
    const {f, request} = built;
    if (variant === 'stale') f.data[0][0] = 'changed after planning';
    if (variant === 'values') request.operations[0].expected_values[10] = 'stale K value';
    if (variant === 'l-text') request.operations[0].expected_warehouse_feedback = '已发货';
    if (variant === 'l-formula') request.operations[0].expected_warehouse_feedback_formula = '=A1';
    if (variant === 'store') request.operations[0].store_alias = 'owner-store';
    if (variant === 'order') request.operations[0].order_id = '9999999999';
    if (variant === 'row') request.operations[0].row_number = 4;
    if (variant === 'duplicate-order') request.operations.push({...request.operations[0]});
    if (variant === 'sheet') request.sheet = '2026年8月发货';
    if (variant === 'state-hash') request.operations[0].expected_a_l_sha256 = 'not-a-sha';
    if (variant === 'evidence-code') request.operations[0].evidence_code = 'CANCELLED';
    if (variant === 'evidence-hash') request.operations[0].evidence_sha256 = 'ABC';
    if (variant === 'extra-field') request.operations[0].range = 'A1:L1';
    const result = f.execute(request);
    assert.equal(result.status, 'blocked', variant);
    assert.deepEqual(f.writes, [], variant);
  }
});
check('caller metadata cannot redirect a cancellation delete away from the fixed exact row', () => {
  const {f, request} = cancellationDeleteBatch();
  request.operations.length = 1;
  request.sheet_name = '2026年8月发货'; request.range = 'A1:L99'; request.column = 'A';
  const result = f.execute(request);
  assert.equal(result.status, 'applied_pending_readback');
  assert.deepEqual(f.writes, [['delete-row', 3]]);
  assert.equal(f.data[2][3], '5685829340');
});
check('read-only, merged and missing delete API states block the entire cancellation batch', () => {
  for (const options of [{readOnly: true}, {unknownMerge: true}, {noDelete: true}]) {
    const {f, request} = cancellationDeleteBatch(options);
    const result = f.execute(request);
    assert.equal(result.status, 'blocked');
    assert.match(result.error, /read only|merged|delete API/);
    assert.deepEqual(f.writes, []);
  }
});
check('a concurrent A:L edit during the second complete preflight blocks every cancellation delete', () => {
  const options = {}, built = cancellationDeleteBatch(options), {f, request} = built;
  let reads = 0;
  options.onRead = (address, data) => {
    if (address === 'A1:K4' && ++reads === 2) data[2][10] = 'concurrent warehouse edit';
  };
  const result = f.execute(request);
  assert.equal(result.status, 'blocked');
  assert.match(result.error, /preflight/);
  assert.deepEqual(f.writes, []);
});
check('a row change after the first descending delete yields partial receipts and never redirects the next delete', () => {
  const options = {}, built = cancellationDeleteBatch(options), {f, request} = built;
  options.afterDelete = (address, data) => { if (address === 'A4:L4') data[2][3] = 'collaborator-replaced-order'; };
  const result = f.execute(request);
  assert.equal(result.status, 'partial');
  assert.equal(result.independent_http_readback_required, true);
  assert.deepEqual(result.receipts.map(item => item.status), ['applied_pending_readback', 'not_attempted']);
  assert.deepEqual(f.writes, [['delete-row', 4]]);
  assert.equal(f.data[2][3], 'collaborator-replaced-order');
});
function shippingSequenceBatch(options = {}) {
  const f = fixture(options);
  f.data[2] = ['7', 46260, '沫德DL05', '5686033990', 'iPhone 14 Pro', '128GB', '黑色', '', '', '', '仓库备注', '待发货'];
  f.data[3] = ['9', 46260, '瑞盈8597', '5667302901', 'iPhone 15', '512GB', '白色', 'imei', '=DISPIMG("I4",1)', '=DISPIMG("J4",1)', '另一条备注', '待收货'];
  const before = f.execute({});
  const operations = before.snapshot.sheets[0].rows.map(row => ({
    store_alias: row.values[2] === '沫德DL05' ? 'new-store' : 'owner-store',
    order_id: row.values[3], row_number: row.row_number,
    expected_old_sequence: row.values[0], new_sequence: String(row.row_number - 2),
    expected_values: row.values.slice(), expected_warehouse_feedback: row.warehouse_feedback,
    expected_warehouse_feedback_formula: row.warehouse_feedback_formula
  }));
  return {f, before, operations, request: {action: 'renumber_shipping_sequences',
    sheet: '2026年9月发货', request_id: 'sequence-offline-1',
    expected_state_token: before.state_token, operations}};
}
check('shipping sequences are fully preflighted and only exact A cells are changed', () => {
  const built = shippingSequenceBatch({formulas: {L3: '=IF(D3<>"","待发货","")'}});
  // Rebind the complete snapshot because this fixture intentionally preserves an L formula.
  const before = built.f.execute({});
  built.request.expected_state_token = before.state_token;
  built.request.operations = before.snapshot.sheets[0].rows.map(row => ({
    store_alias: row.values[2] === '沫德DL05' ? 'new-store' : 'owner-store',
    order_id: row.values[3], row_number: row.row_number,
    expected_old_sequence: row.values[0], new_sequence: String(row.row_number - 2),
    expected_values: row.values.slice(), expected_warehouse_feedback: row.warehouse_feedback,
    expected_warehouse_feedback_formula: row.warehouse_feedback_formula
  }));
  const saved = structuredClone(built.f.data);
  const result = built.f.execute(built.request);
  saved[2][0] = '1'; saved[3][0] = '2';
  assert.equal(result.status, 'applied_pending_readback');
  assert.equal(result.build, '20260908-r11');
  assert.equal(result.snapshot, null);
  assert.equal(result.state_token, null);
  assert.equal(result.independent_http_readback_required, true);
  assert.deepEqual(result.receipts.map(item => [item.row_number, item.status]), [
    [3, 'applied_pending_readback'], [4, 'applied_pending_readback']]);
  assert.deepEqual(built.f.data, saved);
  assert.deepEqual(built.f.writes, [['value', 'A3'], ['value', 'A4']]);
});
check('shipping sequence request may contain only incorrect rows when omitted rows are already correct', () => {
  const built = shippingSequenceBatch(), {f, request} = built;
  f.data[2][0] = '1';
  const before = f.execute({});
  request.expected_state_token = before.state_token;
  const row = before.snapshot.sheets[0].rows[1];
  request.operations = [{store_alias: 'owner-store', order_id: row.values[3], row_number: row.row_number,
    expected_old_sequence: row.values[0], new_sequence: '2', expected_values: row.values.slice(),
    expected_warehouse_feedback: row.warehouse_feedback,
    expected_warehouse_feedback_formula: row.warehouse_feedback_formula}];
  const result = f.execute(request);
  assert.equal(result.status, 'applied_pending_readback');
  assert.deepEqual(f.writes, [['value', 'A4']]);
  assert.equal(f.data[2][0], '1');
  assert.equal(f.data[3][0], '2');
});
check('shipping sequence can repair blank or nonnumeric old sequence text', () => {
  for (const oldValue of ['', '错号']) {
    const built = shippingSequenceBatch(), {f, request} = built;
    f.data[2][0] = '1'; f.data[3][0] = oldValue;
    const before = f.execute({}), row = before.snapshot.sheets[0].rows[1];
    request.expected_state_token = before.state_token;
    request.operations = [{store_alias: 'owner-store', order_id: row.values[3], row_number: 4,
      expected_old_sequence: row.values[0], new_sequence: '2', expected_values: row.values.slice(),
      expected_warehouse_feedback: row.warehouse_feedback,
      expected_warehouse_feedback_formula: row.warehouse_feedback_formula}];
    const result = f.execute(request);
    assert.equal(result.status, 'applied_pending_readback', oldValue);
    assert.deepEqual(f.writes, [['value', 'A4']], oldValue);
    assert.equal(f.data[3][0], '2', oldValue);
  }
});
check('shipping sequence action reports an already continuous requested row without writing', () => {
  const built = shippingSequenceBatch(), {f, request} = built;
  f.data[2][0] = '1'; f.data[3][0] = '2';
  const before = f.execute({});
  request.expected_state_token = before.state_token;
  request.operations = before.snapshot.sheets[0].rows.map(row => ({
    store_alias: row.values[2] === '沫德DL05' ? 'new-store' : 'owner-store',
    order_id: row.values[3], row_number: row.row_number,
    expected_old_sequence: row.values[0], new_sequence: String(row.row_number - 2),
    expected_values: row.values.slice(), expected_warehouse_feedback: row.warehouse_feedback,
    expected_warehouse_feedback_formula: row.warehouse_feedback_formula
  }));
  const result = f.execute(request);
  assert.equal(result.status, 'already_applied');
  assert.equal(result.independent_http_readback_required, false);
  assert.deepEqual(result.receipts.map(item => item.status), ['already_applied', 'already_applied']);
  assert.deepEqual(f.writes, []);
});
check('shipping sequence rejects stale A:L bindings, wrong identities and caller-added operation fields', () => {
  const variants = ['stale', 'values', 'l-text', 'l-formula', 'store', 'order', 'row', 'duplicate-order',
    'duplicate-row', 'duplicate-sheet', 'sheet', 'old-sequence', 'new-sequence', 'extra-field', 'omitted-wrong-row'];
  for (const variant of variants) {
    const built = shippingSequenceBatch(variant === 'duplicate-sheet' ? {duplicateShippingSheet: true} : {});
    const {f, request} = built;
    if (variant === 'stale') f.data[0][0] = 'changed after planning';
    if (variant === 'values') request.operations[0].expected_values[10] = 'stale K';
    if (variant === 'l-text') request.operations[0].expected_warehouse_feedback = '已发货';
    if (variant === 'l-formula') request.operations[0].expected_warehouse_feedback_formula = '=A1';
    if (variant === 'store') request.operations[0].store_alias = 'owner-store';
    if (variant === 'order') request.operations[0].order_id = '9999999999';
    if (variant === 'row') request.operations[0].row_number = 5;
    if (variant === 'duplicate-order') request.operations[1].order_id = request.operations[0].order_id;
    if (variant === 'duplicate-row') request.operations[1].row_number = request.operations[0].row_number;
    if (variant === 'sheet') request.sheet = '2026年8月发货';
    if (variant === 'old-sequence') request.operations[0].expected_old_sequence = '8';
    if (variant === 'new-sequence') request.operations[0].new_sequence = '99';
    if (variant === 'extra-field') request.operations[0].column = 'L';
    if (variant === 'omitted-wrong-row') request.operations.splice(0, 1);
    const result = f.execute(request);
    assert.equal(result.status, 'blocked', variant);
    assert.deepEqual(f.writes, [], variant);
  }
});
check('shipping sequence caller metadata cannot redirect fixed A writes', () => {
  const built = shippingSequenceBatch(), {f, request} = built;
  request.range = 'L1:L99'; request.column = 'L'; request.sheet_name = '2026年8月发货';
  const result = f.execute(request);
  assert.equal(result.status, 'applied_pending_readback');
  assert.deepEqual(f.writes, [['value', 'A3'], ['value', 'A4']]);
});
check('shipping sequence formula, merged state and read-only workbook block the whole batch', () => {
  for (const options of [{formulas: {A3: '=ROW()-2'}}, {unknownMerge: true}, {readOnly: true}]) {
    const built = shippingSequenceBatch(options), result = built.f.execute(built.request);
    assert.equal(result.status, 'blocked');
    assert.match(result.error, /formula|merged|read only/);
    assert.deepEqual(built.f.writes, []);
  }
});
check('shipping sequence detects a physical row gap and an edit during its second full preflight', () => {
  const gap = shippingSequenceBatch();
  gap.f.data.splice(3, 0, []);
  const gapBefore = gap.f.execute({});
  gap.request.expected_state_token = gapBefore.state_token;
  gap.request.operations = gapBefore.snapshot.sheets[0].rows.map(row => ({
    store_alias: row.values[2] === '沫德DL05' ? 'new-store' : 'owner-store', order_id: row.values[3],
    row_number: row.row_number, expected_old_sequence: row.values[0], new_sequence: String(row.row_number - 2),
    expected_values: row.values.slice(), expected_warehouse_feedback: row.warehouse_feedback,
    expected_warehouse_feedback_formula: row.warehouse_feedback_formula
  }));
  assert.equal(gap.f.execute(gap.request).status, 'blocked');
  assert.deepEqual(gap.f.writes, []);

  const options = {}, concurrent = shippingSequenceBatch(options);
  let reads = 0;
  options.onRead = (address, data) => {
    if (address === 'A1:K4' && ++reads === 2) data[2][10] = 'concurrent warehouse edit';
  };
  const result = concurrent.f.execute(concurrent.request);
  assert.equal(result.status, 'blocked');
  assert.match(result.error, /preflight/);
  assert.deepEqual(concurrent.f.writes, []);
});
check('a later row edit produces reconcilable partial shipping-sequence receipts', () => {
  const options = {}, built = shippingSequenceBatch(options), {f, request} = built;
  options.afterWrite = (address, data) => { if (address === 'A3') data[3][10] = 'collaborator changed K4'; };
  const result = f.execute(request);
  assert.equal(result.status, 'partial');
  assert.equal(result.independent_http_readback_required, true);
  assert.deepEqual(result.receipts.map(item => item.status), ['applied_pending_readback', 'not_attempted']);
  assert.deepEqual(f.writes, [['value', 'A3']]);
  assert.equal(f.data[2][0], '1');
  assert.equal(f.data[3][0], '9');
});
function returnsSnapshot(read) {
  return read.snapshot.sheets.find(sheet => sheet.name === '2026年退货');
}
function returnOperation(read, rowNumber = 3, overrides = {}) {
  const row = returnsSnapshot(read).rows.find(item => item.row_number === rowNumber);
  assert.ok(row, 'return fixture row ' + rowNumber);
  const alias = overrides.store_alias || 'owner-store';
  const orderId = overrides.order_id || '5369901737';
  const next = row.values.slice();
  next[0] = String(rowNumber - 2);
  next[1] = 'WB';
  next[2] = alias === 'owner-store' ? '瑞盈8597' : '沫德DL05';
  next[3] = orderId;
  next[4] = overrides.model || 'iPhone 15';
  next[5] = overrides.memory || '512GB';
  next[6] = overrides.color || '黑色';
  next[7] = Object.hasOwn(overrides, 'imei') ? overrides.imei : '356024610646838';
  next[8] = overrides.quantity || '1';
  next[9] = Object.hasOwn(overrides, 'note') ? overrides.note : '【自动退货】WB退货已到提货点';
  next[10] = overrides.status || '待提货';
  return {operation_id: overrides.operation_id || 'b'.repeat(24), mode: overrides.mode || 'append',
    return_key: overrides.return_key || alias + ':' + orderId, store_alias: alias, order_id: orderId,
    row_number: rowNumber, expected_values: row.values.slice(), new_values: next};
}
function returnRequest(read, operations) {
  return {action: 'sync_return_rows', expected_state_token: read.state_token, operations};
}
check('complete read includes the fixed A:K return sheet and formula identities without writing', () => {
  const f = fixture({returnFormulas: {K4: '=IF(D4<>"","待提货","")'}});
  const result = f.execute({});
  const returns = returnsSnapshot(result);
  assert.equal(result.build, '20260908-r11');
  assert.equal(result.snapshot.return_read_columns, 'A:K');
  assert.equal(returns.kind, 'returns');
  assert.equal(returns.read_columns, 'A:K');
  assert.deepEqual(returns.headers, RETURN_HEADERS);
  assert.equal(returns.used_row_end, 7);
  assert.equal(returns.rows[0].values[0], '1');
  assert.equal(returns.rows[1].values[10], '=IF(D4<>"","待提货","")');
  assert.deepEqual(f.writes, []);
});
check('return append fills the first empty order row only and requires independent readback', () => {
  const f = fixture(), before = f.execute({}), op = returnOperation(before);
  const result = f.execute(returnRequest(before, [op]));
  assert.equal(result.status, 'applied_pending_readback');
  assert.equal(result.snapshot, null);
  assert.equal(result.state_token, null);
  assert.equal(result.independent_http_readback_required, true);
  assert.deepEqual(result.operation_ids, [op.operation_id]);
  assert.deepEqual(result.receipts[0].changed_columns, ['C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K']);
  assert.equal(f.returnData[2][0], '1');
  assert.deepEqual(f.returnData[2].slice(1), op.new_values.slice(1));
  assert.deepEqual(f.writes.map(item => item[1]), ['C3', 'D3', 'E3', 'F3', 'G3', 'H3', 'I3', 'J3', 'K3']);
});
check('return update binds exact C/D, preserves H, and appends rather than replaces J', () => {
  const f = fixture();
  f.returnData[2] = ['1', 'WB', '瑞盈8597', '5369901737', 'iPhone 15', '512GB', '黑色',
    'manual-imei', '1', '仓库手工备注', '运输中'];
  const before = f.execute({}), op = returnOperation(before, 3, {mode: 'update', imei: 'manual-imei',
    note: '仓库手工备注\n【自动退货】已到提货点', status: '待提货'});
  const result = f.execute(returnRequest(before, [op]));
  assert.equal(result.status, 'applied_pending_readback');
  assert.deepEqual(result.receipts[0].changed_columns, ['J', 'K']);
  assert.equal(f.returnData[2][7], 'manual-imei');
  assert.equal(f.returnData[2][9], '仓库手工备注\n【自动退货】已到提货点');
  assert.equal(f.returnData[2][10], '待提货');
  assert.deepEqual(f.writes, [['value', 'J3'], ['value', 'K3']]);
});
check('return update with identical target is idempotent and performs no write', () => {
  const f = fixture();
  f.returnData[2] = ['1', 'WB', '瑞盈8597', '5369901737', 'iPhone 15', '512GB', '黑色',
    'manual-imei', '1', '【自动退货】已到提货点', '待提货'];
  const before = f.execute({}), row = returnsSnapshot(before).rows[0];
  const op = {operation_id: 'c'.repeat(24), mode: 'update', return_key: 'owner-store:5369901737',
    store_alias: 'owner-store', order_id: '5369901737', row_number: 3,
    expected_values: row.values.slice(), new_values: row.values.slice()};
  const result = f.execute(returnRequest(before, [op]));
  assert.equal(result.status, 'already_applied');
  assert.equal(result.independent_http_readback_required, false);
  assert.equal(result.receipts[0].status, 'already_applied');
  assert.deepEqual(f.writes, []);
});
check('return action rejects a non-first append, stale row, duplicate identity and extra operation fields', () => {
  for (const variant of ['later-row', 'stale', 'duplicate', 'extra']) {
    const f = fixture(), before = f.execute({});
    let ops = [returnOperation(before)];
    if (variant === 'later-row') ops = [returnOperation(before, 4)];
    if (variant === 'stale') f.returnData[2][9] = 'concurrent warehouse note';
    if (variant === 'duplicate') ops.push({...structuredClone(ops[0]), operation_id: 'd'.repeat(24), row_number: 4,
      expected_values: returnsSnapshot(before).rows[1].values.slice()});
    if (variant === 'extra') ops[0].column = 'L';
    const result = f.execute(returnRequest(before, ops));
    assert.equal(result.status, 'blocked', variant);
    assert.deepEqual(f.writes, [], variant);
  }
});
check('return action never overwrites IMEI and never replaces an existing manual note', () => {
  for (const variant of ['imei', 'note']) {
    const f = fixture();
    f.returnData[2] = ['1', 'WB', '瑞盈8597', '5369901737', 'iPhone 15', '512GB', '黑色',
      'manual-imei', '1', '仓库手工备注', '运输中'];
    const before = f.execute({}), op = returnOperation(before, 3, {mode: 'update', imei: 'manual-imei',
      note: '仓库手工备注\n【自动退货】更新', status: '待提货'});
    if (variant === 'imei') op.new_values[7] = 'different-imei';
    if (variant === 'note') op.new_values[9] = '替换仓库手工备注';
    const result = f.execute(returnRequest(before, [op]));
    assert.equal(result.status, 'blocked', variant);
    assert.deepEqual(f.writes, [], variant);
  }
});
check('return note may replace or delete whole automatic lines while preserving manual lines exactly', () => {
  for (const variant of ['replace', 'delete']) {
    const f = fixture();
    f.returnData[2] = ['1', 'WB', '瑞盈8597', '5369901737', 'iPhone 15', '512GB', '黑色',
      'manual-imei', '1', '仓库手工备注\n【自动退货】旧状态\n第二条人工备注\n【自动退货】旧提货点', '运输中'];
    const note = variant === 'replace' ?
      '仓库手工备注\n【自动退货】待提货\n第二条人工备注' :
      '仓库手工备注\n第二条人工备注';
    const before = f.execute({}), op = returnOperation(before, 3, {
      mode: 'update', imei: 'manual-imei', note, status: '待提货'
    });
    const result = f.execute(returnRequest(before, [op]));
    assert.equal(result.status, 'applied_pending_readback', variant);
    assert.equal(f.returnData[2][9], note, variant);
    assert.deepEqual(result.receipts[0].changed_columns, ['J', 'K'], variant);
  }
});
check('return restore recovers archived IMEI and manual note with an automatic restoration explanation', () => {
  const f = fixture(), before = f.execute({});
  const note = '仓库历史手工备注\n【自动退货】旧的活动状态\n【自动补回】本地归档显示该退货仍待提货';
  const op = returnOperation(before, 3, {
    mode: 'restore', imei: 'archived-manual-imei', note, status: '待提货'
  });
  const result = f.execute(returnRequest(before, [op]));
  assert.equal(result.status, 'applied_pending_readback');
  assert.equal(f.returnData[2][7], 'archived-manual-imei');
  assert.equal(f.returnData[2][9], note);
  assert.ok(result.receipts[0].changed_columns.includes('H'));
  assert.ok(result.receipts[0].changed_columns.includes('J'));
});
check('return note modes isolate restore explanations from append and protect them during update', () => {
  for (const variant of ['restore-without-explanation', 'append-restoration', 'update-restoration']) {
    const f = fixture();
    if (variant === 'update-restoration') {
      f.returnData[2] = ['1', 'WB', '瑞盈8597', '5369901737', 'iPhone 15', '512GB', '黑色',
        'manual-imei', '1', '仓库手工备注\n【自动补回】原补回说明', '运输中'];
    }
    const before = f.execute({});
    let op;
    if (variant === 'restore-without-explanation') {
      op = returnOperation(before, 3, {mode: 'restore', note: '归档人工备注'});
    } else if (variant === 'append-restoration') {
      op = returnOperation(before, 3, {note: '【自动补回】不属于新行说明'});
    } else {
      op = returnOperation(before, 3, {mode: 'update', imei: 'manual-imei',
        note: '仓库手工备注\n【自动补回】被替换的说明\n【自动退货】待提货'});
    }
    const result = f.execute(returnRequest(before, [op]));
    assert.equal(result.status, 'blocked', variant);
    assert.deepEqual(f.writes, [], variant);
  }
});
check('return headers, formulas, merged cells and read-only workbooks block before any mutation', () => {
  for (const variant of ['header', 'formula', 'merged', 'read-only']) {
    const options = variant === 'formula' ? {returnFormulas: {K3: '=IF(D3<>"","待提货","")'}} :
      variant === 'merged' ? {returnUnknownMerge: true} : variant === 'read-only' ? {readOnly: true} : {};
    const f = fixture(options);
    if (variant === 'header') f.returnData[1][10] = '其他字段';
    let result;
    if (variant === 'header') {
      assert.throws(() => f.execute({}), /Return headers changed/);
      result = null;
    } else {
      const before = f.execute({}), op = returnOperation(before);
      result = f.execute(returnRequest(before, [op]));
      assert.equal(result.status, 'blocked', variant);
    }
    assert.deepEqual(f.writes, [], variant);
  }
});
check('return batch fully preflights every row before its first write', () => {
  const f = fixture({returnFormulas: {K4: '=IF(D4<>"","待提货","")'}}), before = f.execute({});
  const first = returnOperation(before, 3, {operation_id: 'e'.repeat(24)});
  const second = returnOperation(before, 4, {operation_id: 'f'.repeat(24), order_id: '5369901738'});
  const result = f.execute(returnRequest(before, [first, second]));
  assert.equal(result.status, 'blocked');
  assert.match(result.error, /formula/);
  assert.deepEqual(f.writes, []);
});
check('return caller metadata cannot redirect the fixed return-sheet cells', () => {
  const f = fixture(), before = f.execute({}), op = returnOperation(before);
  const request = returnRequest(before, [op]);
  request.sheet = '2026年9月发货'; request.range = 'L1:L99'; request.column = 'L';
  const result = f.execute(request);
  assert.equal(result.status, 'applied_pending_readback');
  assert.equal(f.data[2][3], '5589475494');
  assert.equal(f.returnData[2][3], '5369901737');
});
check('shipping identity checks ignore the same original order archived on the return sheet', () => {
  const f = fixture();
  f.returnData[2] = ['1', 'WB', '沫德DL05', '5589475494', 'iPhone 14 Pro', '128GB', '黑色',
    'manual-imei', '1', '退货记录', '待提货'];
  const op = operation(); op.order_id = op.values[3] = '5589475494';
  const result = f.execute({action: 'apply', expected_state_token: f.execute({}).state_token, operation: op, images});
  assert.equal(result.status, 'already_present');
  assert.deepEqual(f.writes, []);
});
check('an edit after one return row produces partial receipts and never redirects the next row', () => {
  const options = {}, f = fixture(options), before = f.execute({});
  const first = returnOperation(before, 3, {operation_id: '1'.repeat(24)});
  const second = returnOperation(before, 4, {operation_id: '2'.repeat(24), order_id: '5369901738'});
  options.returnAfterWrite = (address, data) => { if (address === 'C3') data[3][9] = 'concurrent note'; };
  const result = f.execute(returnRequest(before, [first, second]));
  assert.equal(result.status, 'partial');
  assert.equal(result.independent_http_readback_required, true);
  assert.deepEqual(result.receipts.map(item => item.status), ['applied_pending_readback', 'not_attempted']);
  assert.equal(f.returnData[2][3], '5369901737');
  assert.equal(f.returnData[3][3], '');
});
console.log(JSON.stringify({offline_airscript_checks: checked, passed: checked}));
