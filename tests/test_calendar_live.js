/* Executes the REAL _payoffCal() lifted straight out of dashboard.html
   (no re-implementation) against a stubbed DOM, and intercepts the
   _drawChart payload. This verifies the actual shipped display layer:
   adaptive window, dual curves, dash styling, legend text, meta strip. */
const fs = require('fs');
const path = require('path');

// ── Minimal DOM stub: every element is a permissive sink ──────────────
const el = new Proxy({}, {
  get(t, k) {
    if (k === 'style' || k === 'classList' || k === 'dataset') return el;
    if (k === 'children' || k === 'childNodes') return [];
    if (k === 'getContext') return () => ctx;
    if (k === Symbol.toPrimitive || k === 'toString') return () => '';
    if (typeof k === 'string' && /^(add|remove|toggle|contains|append|insert|set|scroll|focus|blur|click|replace)/.test(k))
      return () => el;
    return el;
  },
  set() { return true; },
});
const ctx = new Proxy({}, { get: () => () => ctx, set: () => true });

global.document = {
  getElementById: () => el, querySelector: () => el, querySelectorAll: () => [],
  createElement: () => el, addEventListener: () => {}, body: el, documentElement: el,
};
global.window = { location: { href: '' }, addEventListener: () => {},
                  devicePixelRatio: 1, requestAnimationFrame: () => 0,
                  matchMedia: () => ({ matches: false, addEventListener: () => {} }) };
global.navigator = { userAgent: 'node' };
global.requestAnimationFrame = () => 0;
global.setInterval = () => 0;
global.setTimeout = () => 0;
global.fetch = () => new Promise(() => {});
global.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
global.EventSource = function () { return { addEventListener: () => {}, close: () => {} }; };

// ── Lift all inline <script> bodies ───────────────────────────────────
// Resolved relative to this file (or DASHBOARD_HTML env override) so the
// test works from any checkout location, not just one specific dev
// container path. That was the bug: a hardcoded /workspace/dashboard.html
// only ever passed by coincidence of where it happened to be run.
const DASHBOARD_PATH = process.env.DASHBOARD_HTML
  || path.join(__dirname, 'dashboard.html');
if (!fs.existsSync(DASHBOARD_PATH)) {
  console.error(`dashboard.html not found at ${DASHBOARD_PATH}`);
  console.error('Set DASHBOARD_HTML=/path/to/dashboard.html or place it next to this test.');
  process.exit(1);
}
const src = fs.readFileSync(DASHBOARD_PATH, 'utf8');
let code = '';
for (const m of src.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) code += m[1] + '\n;\n';

// Neutralise the real renderer so we can inspect its argument instead.
code = code.replace(/function _drawChart\s*\(/, 'function __realDrawChart(');

const drawn = [];
code += '\n;globalThis.__payoffCal = _payoffCal;\n';
// _payoffCal calls _drawChart(...) -> route it to our collector
global._drawChart = (o) => { drawn.push(o); };

(0, eval)(code);
const payoffCal = globalThis.__payoffCal;

const cases = [
  ['earnings pop  (47/32 -> fwd 24, 14d -> 45d, quoted)', {
    ticker:'XYZ', spot:100, strike:100, iv_short:47, iv_long:32, forward_iv:24,
    dte_short:14, dte_long:45, long_ask:4.10, short_bid:2.55,
    long_bid:3.90, short_ask:2.70, long_mid:4.00, short_mid:2.62,
    ff:18.2, gap_days:31, bucket:'2-6w', exp_short:'2026-08-21', exp_long:'2026-09-21' }],
  ['high IV       (75/50 -> fwd 34,  7d -> 35d, no quotes)', {
    ticker:'HIV', spot:250, strike:250, iv_short:75, iv_long:50, forward_iv:34,
    dte_short:7, dte_long:35, ff:31.0, gap_days:28, bucket:'2-6w' }],
  ['low IV        (22/19 -> fwd 17.5, 21d -> 56d, mid only)', {
    ticker:'LOW', spot:60, strike:60, iv_short:22, iv_long:19, forward_iv:17.5,
    dte_short:21, dte_long:56, long_mid:2.85, short_mid:1.70,
    ff:6.1, gap_days:35, bucket:'2-6w' }],
  ['long back     (35/28 -> fwd 25, 10d -> 120d, quoted)', {
    ticker:'LB', spot:100, strike:100, iv_short:35, iv_long:28, forward_iv:25,
    dte_short:10, dte_long:120, long_ask:5.60, short_bid:1.90,
    ff:12.4, gap_days:110, bucket:'>6w' }],
  // Regression: partial book (ask + bid but no mid on either leg). This
  // combination used to throw inside fba2() and blank the whole modal.
  ['partial book  (no mid fields at all)', {
    ticker:'PB', spot:100, strike:100, iv_short:40, iv_long:30, forward_iv:26,
    dte_short:14, dte_long:45, long_ask:4.10, short_bid:2.55,
    ff:14.0, gap_days:31, bucket:'2-6w' }],
  // Regression: only a long ask exists; short leg entirely unquoted.
  ['one-sided book (long ask only)', {
    ticker:'OS', spot:100, strike:100, iv_short:40, iv_long:30, forward_iv:26,
    dte_short:14, dte_long:45, long_ask:4.10,
    ff:14.0, gap_days:31, bucket:'2-6w' }],
];

const get = (meta, lbl) => (meta.find(m => m.lbl.trim() === lbl) || {}).val;
let ok = true;
const fail = (m) => { ok = false; console.log('     FAIL  ' + m); };

for (const [name, d] of cases) {
  drawn.length = 0;
  payoffCal(d);
  const o = drawn[0];
  if (!o) { fail(name + ' : _drawChart never called'); continue; }

  const p = o.lines[0].pts, f = o.lines[1].pts;
  const halfPct = (o.xMax / o.spot - 1) * 100;
  // 'Max Loss' is rendered already signed ("$-155/contract"); do NOT negate again.
  const floor   = parseFloat(String(get(o.meta,'Max Loss')).replace(/[^0-9.\-]/g,''));

  console.log('\n' + name);
  console.log('  window   $' + o.xMin.toFixed(2) + ' .. $' + o.xMax.toFixed(2) +
              '   (+/-' + halfPct.toFixed(1) + '%)');
  console.log('  debit    ' + get(o.meta,'Net Debit'));
  console.log('  basis    ' + get(o.meta,'Debit Basis'));
  console.log('  model    ' + get(o.meta,'Model Debit'));
  console.log('  risk     ' + get(o.meta,'Max Loss') + '   ' + get(o.meta,'Max Profit'));
  console.log('  curves   primary w=' + o.lines[0].width + ' fill=' + !!o.lines[0].fill +
              ' | scenario w=' + o.lines[1].width + ' dash=' + JSON.stringify(o.lines[1].dash));
  o.legend.forEach(l => console.log('  legend   ' + l.lbl));
  console.log('  markers  ' + o.lines[0].markers.map(m => m.lbl).join('  '));
  console.log('  wings    L $' + p[0][1].toFixed(2) + '   R $' + p[p.length-1][1].toFixed(2) +
              '   floor $' + floor.toFixed(2));

  const wingTol = 0.03 * Math.abs(floor) + 1;
  if (Math.abs(p[0][1] - floor) > wingTol)              fail('left wing off floor');
  if (Math.abs(p[p.length-1][1] - floor) > wingTol)     fail('right wing off floor');
  if (!p.every((q,i) => q[1] >= f[i][1] - 1e-9))        fail('scenario curve above primary');
  if (JSON.stringify(o.lines[1].dash) !== '[6,4]')      fail('scenario curve not dashed');
  if (o.legend.length !== 2)                            fail('legend missing a curve');
  if (!/IB convention/.test(o.legend[0].lbl))           fail('primary legend lacks IB note');
  if (!get(o.meta,'Debit Basis'))                       fail('debit basis not surfaced');
  if (!o.lines[0].markers.some(m => /^K /.test(m.lbl))) fail('strike marker missing');
  if (halfPct < 11.9 || halfPct > 45.1)                 fail('window outside clamp');
}

console.log('\n' + (ok ? 'LIVE DISPLAY LAYER OK' : 'LIVE DISPLAY LAYER HAS FAILURES'));
process.exit(ok ? 0 : 1);
