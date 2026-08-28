/* Headless validation of the Calendar Spread payoff math in dashboard.html.
   Mirrors _payoffCal() exactly: same BS pricer, same debit anchoring,
   same dual-curve residual IV convention (ivL primary, fwdIV scenario). */

function N(x) {
  const t = 1 / (1 + 0.2316419 * Math.abs(x));
  const p = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937
          + t * (-1.821255978 + t * 1.330274429))));
  const pdf = Math.exp(-0.5 * x * x) / Math.sqrt(2 * Math.PI);
  const cdf = 1 - pdf * p;
  return x >= 0 ? cdf : 1 - cdf;
}
function bsCall(S, K, sigma, T) {
  if (T <= 0 || sigma <= 0 || S <= 0) return Math.max(0, S - K);
  const sq = Math.sqrt(T);
  const d1 = (Math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * sq);
  const d2 = d1 - sigma * sq;
  return S * N(d1) - K * N(d2);
}

function payoffCal(d) {
  const spot   = d.spot;
  const strike = d.strike || spot;
  const ivS    = (d.iv_short   || 20) / 100;
  const ivL    = (d.iv_long    || 25) / 100;
  const fwdIV  = (d.forward_iv || 22) / 100;
  const dteS   = d.dte_short || 30;
  const dteL   = d.dte_long  || 60;

  const calNat = (d.long_ask != null && d.short_bid != null) ? d.long_ask - d.short_bid : null;
  const calMid = (d.long_mid != null && d.short_mid != null) ? d.long_mid - d.short_mid : null;

  const priceShort = bsCall(spot, strike, ivS, dteS / 365);
  const priceLong  = bsCall(spot, strike, ivL, dteL / 365);

  let netDebit, calBasis;
  if (calNat != null && calNat > 0)      { netDebit = calNat; calBasis = 'natural'; }
  else if (calMid != null && calMid > 0) { netDebit = calMid; calBasis = 'mid'; }
  else                                   { netDebit = priceLong - priceShort; calBasis = 'theoretical'; }
  netDebit = Math.max(0.001, netDebit);

  const modelDebit = Math.max(0.001, priceLong - priceShort);
  const debitEdge  = netDebit - modelDebit;

  const remainT = Math.max(1, dteL - dteS) / 365;
  // Mirrors the adaptive window now used in dashboard.html _payoffCal()
  const sigFront = ivS * Math.sqrt(dteS / 365);
  const sigResid = ivL * Math.sqrt(remainT);
  const halfW    = Math.min(0.45, Math.max(0.12, 2.8 * Math.max(sigFront, sigResid)));
  const xMin = spot * (1 - halfW), xMax = spot * (1 + halfW);
  const pts = [], ptsF = [];
  for (let i = 0; i <= 300; i++) {
    const x = xMin + (xMax - xMin) * i / 300;
    const shortPnl = -Math.max(0, x - strike);
    pts .push([x, (shortPnl + bsCall(x, strike, ivL,   remainT) - netDebit) * 100]);
    ptsF.push([x, (shortPnl + bsCall(x, strike, fwdIV, remainT) - netDebit) * 100]);
  }

  let beLo = null, beHi = null;
  for (let i = 1; i < pts.length; i++) {
    if (pts[i-1][1] <  0 && pts[i][1] >= 0) beLo = pts[i][0];
    if (pts[i-1][1] >= 0 && pts[i][1] <  0) beHi = pts[i][0];
  }
  const maxProfit = Math.max(...pts.map(p => p[1]));
  const maxLoss   = -netDebit * 100;
  return { spot, strike, ivS, ivL, fwdIV, remainT, netDebit, calBasis,
           modelDebit, debitEdge, pts, ptsF, beLo, beHi, maxProfit, maxLoss };
}

const atOf = (pts, x) => {
  let best = pts[0];
  for (const p of pts) if (Math.abs(p[0] - x) < Math.abs(best[0] - x)) best = p;
  return best[1];
};

function report(name, d) {
  const r = payoffCal(d);
  const peakX = r.pts.reduce((a, p) => p[1] > a[1] ? p : a, r.pts[0])[0];
  console.log('\n=== ' + name + ' ===');
  console.log(`spot=${r.spot}  K=${r.strike}  ivS=${(r.ivS*100).toFixed(1)}%  ivL=${(r.ivL*100).toFixed(1)}%  fwd=${(r.fwdIV*100).toFixed(1)}%  remainT=${r.remainT.toFixed(4)}y`);
  console.log(`debit basis : ${r.calBasis}   net=$${r.netDebit.toFixed(3)}   model=$${r.modelDebit.toFixed(3)}   edge=${r.debitEdge>=0?'+':''}${r.debitEdge.toFixed(3)}`);
  console.log(`maxLoss=$${r.maxLoss.toFixed(2)}  maxProfit(ivL)=$${r.maxProfit.toFixed(2)}  peak@$${peakX.toFixed(2)}`);
  console.log(`maxProfit(fwd)=$${Math.max(...r.ptsF.map(p=>p[1])).toFixed(2)}`);
  console.log(`BE: ${r.beLo?('$'+r.beLo.toFixed(2)):'none'}  ..  ${r.beHi?('$'+r.beHi.toFixed(2)):'none'}`);
  console.log(`wings: left(ivL)=$${r.pts[0][1].toFixed(2)}  right(ivL)=$${r.pts[r.pts.length-1][1].toFixed(2)}  (target ${r.maxLoss.toFixed(2)})`);

  const tol = 0.02 * Math.abs(r.maxLoss) + 1;
  const checks = [
    ['left wing  -> -netDebit',  Math.abs(r.pts[0][1] - r.maxLoss) < tol],
    ['right wing -> -netDebit',  Math.abs(r.pts[r.pts.length-1][1] - r.maxLoss) < tol],
    ['peak near strike',         Math.abs(peakX - r.strike) < 0.03 * r.spot],
    ['maxProfit > 0',            r.maxProfit > 0],
    ['ptsF <= pts (fwd<ivL)',    r.fwdIV > r.ivL || r.pts.every((p,i) => p[1] >= r.ptsF[i][1] - 1e-6)],
    ['both breakevens found',    r.beLo != null && r.beHi != null],
  ];
  let pass = true;
  checks.forEach(([lbl, ok]) => { if (!ok) pass = false; console.log(`  ${ok?'PASS':'FAIL'}  ${lbl}`); });
  return pass;
}

let allPass = true;

// 1. Realistic quoted calendar (live bid/ask present)
allPass &= report('Quoted calendar (natural debit)', {
  spot:100, strike:100, iv_short:47, iv_long:32, forward_iv:24,
  dte_short:14, dte_long:45,
  long_ask:4.10, short_bid:2.55, long_mid:4.00, short_mid:2.62,
});

// 2. No quotes -> theoretical fallback
allPass &= report('No quotes (theoretical fallback)', {
  spot:250, strike:250, iv_short:55, iv_long:38, forward_iv:29,
  dte_short:7, dte_long:35,
});

// 3. Mid-only quotes
allPass &= report('Mid-only quotes', {
  spot:60, strike:60, iv_short:40, iv_long:30, forward_iv:25,
  dte_short:21, dte_long:56, long_mid:2.85, short_mid:1.70,
});

// 4. Edge: fwdIV == ivL -> curves must converge exactly
{
  const r = payoffCal({ spot:100, strike:100, iv_short:45, iv_long:30, forward_iv:30,
                        dte_short:14, dte_long:45, long_ask:3.9, short_bid:2.4 });
  const maxDiff = Math.max(...r.pts.map((p,i) => Math.abs(p[1] - r.ptsF[i][1])));
  const ok = maxDiff < 1e-9;
  console.log('\n=== Edge: fwdIV == ivL ===');
  console.log(`  ${ok?'PASS':'FAIL'}  curves converge (max diff $${maxDiff.toExponential(2)})`);
  allPass &= ok;
}

// 5. Edge: remainT minimal (dteL == dteS) -> tent height == 1 day of ATM time value.
//    The old expectation ("curve nearly flat") was wrong: it compared the tent
//    height against netDebit, but those are independent quantities. With a
//    1-day residual the surviving leg is still worth bsCall(K,K,ivL,1/365)
//    at the money, so the tent MUST rise by exactly that much above the floor
//    regardless of how small the debit is. Assert against the analytic value.
{
  const r = payoffCal({ spot:100, strike:100, iv_short:45, iv_long:30, forward_iv:25,
                        dte_short:30, dte_long:30, long_ask:3.0, short_bid:2.9 });
  const spread   = Math.max(...r.pts.map(p=>p[1])) - Math.min(...r.pts.map(p=>p[1]));
  const atmResid = bsCall(r.strike, r.strike, r.ivL, r.remainT) * 100;
  const ok = Math.abs(spread - atmResid) < 0.02 * atmResid + 1;
  console.log('\n=== Edge: dteL == dteS (1-day residual) ===');
  console.log(`  remainT=${r.remainT.toFixed(5)}y  curve spread=$${spread.toFixed(2)}  maxLoss=$${r.maxLoss.toFixed(2)}`);
  console.log(`  tent height $${spread.toFixed(2)} vs 1-day ATM residual $${atmResid.toFixed(2)}`);
  console.log(`  ${ok?'PASS':'FAIL'}  tent height == 1-day residual time value`);
  allPass &= ok;
}

// 6. Edge: deep OTM / deep ITM convergence on a wide grid
{
  const d = { spot:100, strike:100, iv_short:45, iv_long:32, forward_iv:26,
              dte_short:14, dte_long:45, long_ask:4.1, short_bid:2.5 };
  const r = payoffCal(d);
  const wide = [40, 60, 160, 200].map(x => {
    const shortPnl = -Math.max(0, x - r.strike);
    return [x, (shortPnl + bsCall(x, r.strike, r.ivL, r.remainT) - r.netDebit) * 100];
  });
  console.log('\n=== Edge: extreme wings (outside plotted range) ===');
  let ok = true;
  wide.forEach(([x,y]) => {
    const near = Math.abs(y - r.maxLoss) < 0.02 * Math.abs(r.maxLoss) + 1;
    if (!near) ok = false;
    console.log(`  x=$${x}  P&L=$${y.toFixed(2)}  (target $${r.maxLoss.toFixed(2)})  ${near?'PASS':'FAIL'}`);
  });
  allPass &= ok;
}

console.log('\n' + (allPass ? 'ALL CHECKS PASSED' : 'SOME CHECKS FAILED'));
process.exit(allPass ? 0 : 1);
