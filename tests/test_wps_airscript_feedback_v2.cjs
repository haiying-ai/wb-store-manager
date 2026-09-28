// Offline candidate checks only. This mock cannot connect to WPS.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../tools/wps_warehouse/airscript-feedback-v2.js'), 'utf8');
const run = new Function('Application', 'Context', 'RGB', source);
const FONT_KEYS = ['Name', 'Size', 'Bold', 'Italic', 'Underline', 'Color', 'Strikethrough', 'Subscript', 'Superscript'];
const rgb = (r, g, b) => r + g * 256 + b * 65536;
const clone = value => JSON.parse(JSON.stringify(value));
function font(index) {
  return {Name: index % 2 ? 'Arial' : 'Microsoft YaHei', Size: 10 + index % 2,
    Bold: index % 2 === 0, Italic: false, Underline: -4142,
    Color: index % 2 ? rgb(180, 20, 20) : rgb(0, 0, 0),
    Strikethrough: false, Subscript: false, Superscript: false};
}
function fixture(options = {}) {
  const writes = [], characterReads = [];
  const data = [
    ['仓库发货表', '', '', '', '', '', '', '', '', '', '', '反馈区标题'],
    ['序号', '日期', '店铺', '订单', '型号', '内存', '颜色', 'IMEI', '标签', '二维码', '备注', '仓库补充'],
    ['1', '2026/9/4', '沫德DL05', '1234567890', 'Phone', '128GB', '黑色', 'synthetic', '=DISPIMG("I",1)', '=DISPIMG("J",1)', '原备注', '请核实订单状态'],
    ['2', '2026/9/4', '瑞盈8597', '2234567890', 'Phone', '256GB', '白色', '', '', '', '其他备注', '其他订单原反馈']
  ];
  const fonts = data.map(row => Array.from(row[11], (_, index) => font(index)));
  let snapshotCount = 0;
  const sheet = {
    Name: '2026年9月发货',
    get UsedRange() {
      snapshotCount++;
      if (options.onSnapshot) options.onSnapshot(snapshotCount, data, fonts);
      return {Row: 1, Rows: {Count: data.length}};
    },
    Range(address) {
      const match = /^([A-L])([1-9][0-9]*)$/.exec(address);
      assert.ok(match, address);
      const row = Number(match[2]) - 1, column = match[1].charCodeAt(0) - 65;
      return {
        get Value2() {
          if ((options.undefinedCells || []).includes(address)) return undefined;
          if (options.imageValueObjects && /^=DISPIMG/.test(String(data[row][column]))) return {image: true};
          return data[row][column];
        },
        get Formula() { return Object.hasOwn(options.formulaOverrides || {}, address) ? options.formulaOverrides[address] : data[row][column]; },
        get Text() { return Object.hasOwn(options.textOverrides || {}, address) ? options.textOverrides[address] : String(data[row][column]); },
        get MergeCells() { return Object.hasOwn(options, 'merged') ? options.merged : false; },
        Characters(start, length) {
          assert.equal(column, 11, 'Characters is restricted to L');
          assert.ok(Number.isInteger(start) && start >= 1);
          assert.ok(Number.isInteger(length) && length >= 1);
          assert.ok(start + length - 1 <= data[row][11].length, 'Only existing character ranges are supported');
          const offset = start - 1, selectedFont = {}, bridge = {fontGets: 0, keys: []};
          characterReads.push(bridge);
          for (const key of FONT_KEYS) Object.defineProperty(selectedFont, key, {
            get() { bridge.keys.push(key); return fonts[row][offset][key]; },
            set(value) {
              writes.push({kind: 'font', address, start, length, key, value});
              for (let i = offset; i < offset + length; i++) fonts[row][i][key] = value;
              if (options.afterFontWrite) options.afterFontWrite(data, fonts);
            }
          });
          return {
            get Text() { return data[row][11].slice(offset, offset + length); },
            set Text(value) {
              writes.push({kind: 'text', address, start, length});
              const original = data[row][11], previous = fonts[row];
              const inserted = Array.from(value, (_, index) => clone(previous[offset + Math.min(index, length - 1)]));
              data[row][11] = original.slice(0, offset) + value + original.slice(offset + length);
              fonts[row] = previous.slice(0, offset).concat(inserted, previous.slice(offset + length));
              if (options.changeOriginalFont) fonts[row][0].Bold = !fonts[row][0].Bold;
            },
            get Font() { bridge.fontGets++; return selectedFont; }
          };
        }
      };
    }
  };
  const application = {ActiveWorkbook: {ReadOnly: Object.hasOwn(options, 'readOnly') ? options.readOnly : false}, Sheets: {Count: 2,
    Item(index) { return index === 2 ? {Name: '库存概览'} : sheet; }
  }};
  return {data, fonts, writes, characterReads,
    execute(argv = {}) { return run(application, {argv}, rgb); },
    styles(rowNumber = 3) { return clone(fonts[rowNumber - 1]); },
    setFeedback(value) { data[2][11] = value; fonts[2] = Array.from(value, (_, index) => font(index)); }
  };
}
function request(before) {
  return {action: 'append_reply', sheet: '2026年9月发货', row_number: 3,
    order_id: '1234567890', store_alias: 'new-store', request_id: 'offline-request-1',
    expected_state_token: before.state_token, expected_existing_text: before.snapshot.sheets[0].rows[2].values[11],
    reply_time_bj: '2026-09-05 00:30', reply: '已核实，订单仍待仓库处理。'};
}
function suffixFor(args) {
  return '\n\n【核实回复 北京时间 ' + args.reply_time_bj + '】\n' + args.reply;
}
function interruptedAppend(f, args) {
  const suffix = suffixFor(args), last = clone(f.fonts[2][f.fonts[2].length - 1]);
  f.data[2][11] += suffix;
  f.fonts[2].push(...Array.from(suffix, () => clone(last)));
  return {...args, action: 'repair_reply_color'};
}
function changed(value) { return typeof value === 'boolean' ? !value : typeof value === 'number' ? value + 1 : value + '-changed'; }
let checked = 0;
function check(name, test) { test(); checked++; }
check('default probe reads without mutation', () => {
  const f = fixture(), result = f.execute();
  assert.equal(result.version, 'warehouse-feedback-v2');
  assert.equal(result.action, 'probe');
  assert.equal(result.status, 'ok');
  assert.equal(result.read_only, true);
  assert.equal(result.capabilities.live_append_verified, false);
  assert.equal(result.snapshot.sheets[0].rows[2].values[11], '请核实订单状态');
  assert.deepEqual(f.writes, []);
});
check('picture calculation objects retain the verified formula without scalar coercion', () => {
  const f = fixture({imageValueObjects: true}), result = f.execute();
  assert.equal(result.status, 'ok');
  assert.equal(result.snapshot.sheets[0].rows[2].values[8], '=DISPIMG("I",1)');
  assert.deepEqual(f.writes, []);
});
check('undefined blank values require independently explicit empty formula and text', () => {
  const f = fixture({undefinedCells: ['B1', 'H4']}), result = f.execute();
  assert.equal(result.status, 'ok');
  assert.equal(result.build, '20260905-v2-feedback-r3');
  assert.equal(result.snapshot.sheets[0].rows[0].values[1], '');
  assert.equal(result.snapshot.sheets[0].rows[3].values[7], '');
  assert.deepEqual(f.writes, []);
});
check('undefined nonempty value never becomes a blank', () => {
  const f = fixture({undefinedCells: ['D3']}), result = f.execute();
  assert.equal(result.status, 'blocked');
  assert.match(result.error, /D3.Value2: undefined/);
  assert.deepEqual(f.writes, []);
});
check('unknown or conflicting blank evidence remains blocked', () => {
  for (const overrides of [
    {formulaOverrides: {B1: null}},
    {formulaOverrides: {B1: undefined}},
    {textOverrides: {B1: undefined}},
    {textOverrides: {B1: 'visible content'}}
  ]) {
    const f = fixture({undefinedCells: ['B1'], ...overrides}), result = f.execute();
    assert.equal(result.status, 'blocked');
    assert.match(result.error, /B1\.(Value2|Formula): undefined/);
    assert.deepEqual(f.writes, []);
  }
});
check('append preserves the original text and nine font properties and only colors the new suffix', () => {
  const f = fixture(), before = f.execute(), args = request(before);
  const savedData = clone(f.data), savedFonts = f.styles(), result = f.execute(args);
  const suffix = '\n\n【核实回复 北京时间 ' + args.reply_time_bj + '】\n' + args.reply;
  assert.equal(result.status, 'applied_pending_readback');
  assert.equal(result.snapshot, null);
  assert.equal(result.state_token, null);
  assert.equal(result.independent_http_readback_required, true);
  assert.equal(f.data[2][11], savedData[2][11] + suffix);
  assert.ok(!f.data[2][11].includes(args.request_id));
  assert.deepEqual(f.styles().slice(0, savedFonts.length), savedFonts);
  assert.deepEqual(f.data.map(row => row.slice(0, 11)), savedData.map(row => row.slice(0, 11)));
  for (const rowIndex of [0, 1, 3]) assert.equal(f.data[rowIndex][11], savedData[rowIndex][11]);
  assert.ok(savedFonts.every(style => style.Color !== result.reply_color));
  assert.ok(f.styles().slice(savedFonts.length).every(style => style.Color === result.reply_color));
  assert.deepEqual(f.writes.map(write => [write.kind, write.address]), [['text', 'L3'], ['font', 'L3']]);
});
check('retry with the same original request verifies already_present without writing', () => {
  const f = fixture(), args = request(f.execute());
  assert.equal(f.execute(args).status, 'applied_pending_readback');
  f.writes.length = 0;
  const result = f.execute(args);
  assert.equal(result.status, 'already_present');
  assert.equal(result.request_id, args.request_id);
  assert.deepEqual(f.writes, []);
});
check('a changed original makes its old token unusable', () => {
  const f = fixture(), args = request(f.execute());
  f.setFeedback('仓库已补充新的情况');
  const result = f.execute(args);
  assert.equal(result.status, 'blocked');
  assert.match(result.error, /changed/);
  assert.equal(f.data[2][11], '仓库已补充新的情况');
  assert.deepEqual(f.writes, []);
});
check('text expansion that changes an original font returns partial without retry or rollback', () => {
  const f = fixture({changeOriginalFont: true}), args = request(f.execute());
  const previous = f.styles(), result = f.execute(args);
  assert.equal(result.status, 'partial');
  assert.match(result.error, /Original character styles changed/);
  assert.notDeepEqual(f.styles().slice(0, previous.length), previous);
  assert.ok(f.data[2][11].startsWith(args.expected_existing_text));
  assert.ok(f.data[2][11].includes(args.reply));
  assert.deepEqual(f.writes.map(write => [write.kind, write.address]), [['text', 'L3']]);
});
check('one Font bridge per character still reads every one of the nine properties', () => {
  const f = fixture(), result = f.execute();
  assert.equal(result.status, 'ok');
  assert.equal(f.characterReads.length, f.data[2][11].length + f.data[3][11].length);
  for (const read of f.characterReads) {
    assert.equal(read.fontGets, 1);
    assert.deepEqual(read.keys, FONT_KEYS);
  }
});
check('repair writes only the existing suffix color and preserves every other field and style', () => {
  const f = fixture(), args = request(f.execute()), savedData = clone(f.data), savedFonts = f.styles();
  const repair = interruptedAppend(f, args), text = f.data[2][11], pendingFonts = f.styles();
  const result = f.execute(repair);
  assert.equal(result.status, 'applied_pending_readback');
  assert.equal(result.snapshot, null);
  assert.equal(result.state_token, null);
  assert.equal(result.read_only, false);
  assert.equal(result.independent_http_readback_required, true);
  assert.equal(f.data[2][11], text);
  assert.deepEqual(f.styles().slice(0, savedFonts.length), savedFonts);
  assert.deepEqual(f.data.map(row => row.slice(0, 11)), savedData.map(row => row.slice(0, 11)));
  for (const i of [0, 1, 3]) assert.equal(f.data[i][11], savedData[i][11]);
  for (let i = savedFonts.length; i < f.styles().length; i++) {
    assert.deepEqual(f.styles()[i], {...pendingFonts[i], Color: result.reply_color});
  }
  assert.deepEqual(f.writes, [{kind: 'font', address: 'L3', start: savedFonts.length + 1,
    length: suffixFor(args).length, key: 'Color', value: result.reply_color}]);
  assert.equal(f.execute({action: 'read'}).snapshot.sheets[0].rows[2].values[11], text);
});
check('repair of an already correctly colored suffix is read-only and idempotent', () => {
  const f = fixture(), args = request(f.execute());
  const appended = f.execute(args);
  assert.equal(appended.status, 'applied_pending_readback');
  f.writes.length = 0;
  const result = f.execute({...args, action: 'repair_reply_color'});
  assert.equal(result.status, 'already_present');
  assert.equal(result.read_only, true);
  assert.equal(result.reply_color, appended.reply_color);
  assert.deepEqual(f.writes, []);
});
check('repair refuses absent, changed or incomplete suffixes and a changed original', () => {
  for (const mutation of ['absent', 'prefix', 'suffix', 'incomplete']) {
    const f = fixture(), args = request(f.execute());
    const repair = mutation === 'absent' ? {...args, action: 'repair_reply_color'} : interruptedAppend(f, args);
    if (mutation === 'prefix') f.data[2][11] = '变' + f.data[2][11].slice(1);
    if (mutation === 'suffix') f.data[2][11] = f.data[2][11].slice(0, -1) + '变';
    if (mutation === 'incomplete') { f.data[2][11] = f.data[2][11].slice(0, -1); f.fonts[2].pop(); }
    assert.equal(f.execute(repair).status, 'blocked', mutation);
    assert.deepEqual(f.writes, [], mutation);
  }
});
check('repair refuses changes to any original font property or any inherited non-color property', () => {
  for (const original of [true, false]) for (const key of FONT_KEYS) {
    if (!original && key === 'Color') continue;
    const f = fixture(), args = request(f.execute()), repair = interruptedAppend(f, args);
    const index = original ? 0 : args.expected_existing_text.length;
    f.fonts[2][index][key] = changed(f.fonts[2][index][key]);
    assert.equal(f.execute(repair).status, 'blocked', String(original) + ':' + key);
    assert.deepEqual(f.writes, []);
  }
});
check('repair refuses unknown values in every font property', () => {
  for (const key of FONT_KEYS) for (const value of [undefined, null, {}]) {
    const f = fixture(), args = request(f.execute()), repair = interruptedAppend(f, args);
    f.fonts[2][args.expected_existing_text.length][key] = value;
    assert.equal(f.execute(repair).status, 'blocked', key);
    assert.deepEqual(f.writes, []);
  }
});
check('repair refuses a third color and changes in another cell', () => {
  for (const mutation of ['third-color', 'other-cell', 'other-L-font']) {
    const f = fixture(), args = request(f.execute()), repair = interruptedAppend(f, args);
    if (mutation === 'third-color') f.fonts[2][args.expected_existing_text.length].Color = rgb(20, 30, 40);
    if (mutation === 'other-cell') f.data[3][4] = 'changed model';
    if (mutation === 'other-L-font') f.fonts[3][0].Size++;
    assert.equal(f.execute(repair).status, 'blocked', mutation);
    assert.deepEqual(f.writes, []);
  }
});
check('repair completes a suffix containing only inherited and already correct colors', () => {
  const f = fixture(), args = request(f.execute()), repair = interruptedAppend(f, args);
  f.fonts[2][args.expected_existing_text.length].Color = rgb(0, 102, 204);
  assert.equal(f.execute(repair).status, 'applied_pending_readback');
  assert.deepEqual(f.writes.map(write => write.kind), ['font']);
});
check('repair does not write a merged, read-only, formula or unverified target', () => {
  for (const setting of [
    {merged: true}, {merged: null}, {merged: undefined},
    {readOnly: true}, {readOnly: undefined},
    {formulaOverrides: {L3: '=FORMULA()'}}, {formulaOverrides: {L3: undefined}}
  ]) {
    const options = {}, f = fixture(options), args = request(f.execute()), repair = interruptedAppend(f, args);
    Object.assign(options, setting);
    // ReadOnly is captured when the workbook mock is created.
    if (Object.hasOwn(setting, 'readOnly')) {
      const blockedFixture = fixture(setting), blockedArgs = request(blockedFixture.execute());
      const blockedRepair = interruptedAppend(blockedFixture, blockedArgs);
      assert.equal(blockedFixture.execute(blockedRepair).status, 'blocked');
      assert.deepEqual(blockedFixture.writes, []);
    } else {
      assert.equal(f.execute(repair).status, 'blocked');
      assert.deepEqual(f.writes, []);
    }
  }
});
check('repair rechecks the entire current snapshot before its only write', () => {
  const options = {onSnapshot(count, data) { if (count === 3) data[3][4] = 'concurrent warehouse edit'; }};
  const f = fixture(options), args = request(f.execute()), repair = interruptedAppend(f, args);
  const result = f.execute(repair);
  assert.equal(result.status, 'blocked');
  assert.match(result.error, /changed during preflight/);
  assert.deepEqual(f.writes, []);
});
check('repair remains pending and the independent read exposes any other cell change', () => {
  const f = fixture({afterFontWrite(data) { data[3][4] = 'concurrent warehouse edit'; }});
  const args = request(f.execute()), repair = interruptedAppend(f, args), result = f.execute(repair);
  assert.equal(result.status, 'applied_pending_readback');
  assert.equal(result.snapshot, null);
  assert.equal(result.independent_http_readback_required, true);
  const independent = f.execute({action: 'read'});
  assert.equal(independent.snapshot.sheets[0].rows[3].values[4], 'concurrent warehouse edit');
  assert.notEqual(independent.snapshot.sheets[0].rows[3].values[4], JSON.parse(args.expected_state_token).sheets[0].rows[3].values[4]);
  assert.deepEqual(f.writes.map(write => write.kind), ['font']);
});
console.log(JSON.stringify({offline_feedback_v2_checks: checked, passed: checked}));
