/* Probe: is the fixed +/-18% plot window wide enough for the tent to flatten?
   Compare fixed window vs an IV-scaled window across IV/DTE regimes. */

function N(x){const t=1/(1+0.2316419*Math.abs(x));const p=t*(0.319381530+t*(-0.356563782+t*(1.781477937+t*(-1.821255978+t*1.330274429))));const pdf=Math.exp(-0.5*x*x)/Math.sqrt(2*Math.PI);const cdf=1-pdf*p;return x>=0?cdf:1-cdf;}
function bsCall(S,K,s,T){if(T<=0||s<=0||S<=0)return Math.max(0,S-K);const sq=Math.sqrt(T);const d1=(Math.log(S/K)+0.5*s*s*T)/(s*sq);return S*N(d1)-K*N(d1-s*sq);}

function shape(spot, strike, ivL, remainT, netDebit, half) {
  const xMin = spot*(1-half), xMax = spot*(1+half);
  let lo = Infinity, hi = -Infinity, wl, wr;
  for (let i=0;i<=300;i++){
    const x = xMin+(xMax-xMin)*i/300;
    const y = (-Math.max(0,x-strike)+bsCall(x,strike,ivL,remainT)-netDebit)*100;
    if(i===0) wl=y; if(i===300) wr=y;
    lo=Math.min(lo,y); hi=Math.max(hi,y);
  }
  const target = -netDebit*100;
  return { wl, wr, hi, target,
           lErr: Math.abs(wl-target)/Math.abs(target),
           rErr: Math.abs(wr-target)/Math.abs(target) };
}

// IV-scaled half-width: cover ~2.2 sigma of the FRONT month move, clamped.
function adaptiveHalf(ivS, dteS) {
  const sig = ivS * Math.sqrt(dteS/365);
  return Math.min(0.40, Math.max(0.12, 2.6 * sig));
}

const cases = [
  { n:'earnings pop  (ivS 47, 14d)', spot:100, ivS:.47, ivL:.32, dteS:14, dteL:45, debit:1.55 },
  { n:'high IV       (ivS 75, 7d)',  spot:250, ivS:.75, ivL:.50, dteS:7,  dteL:35, debit:4.10 },
  { n:'low IV        (ivS 22, 21d)', spot:60,  ivS:.22, ivL:.19, dteS:21, dteL:56, debit:0.95 },
  { n:'very low IV   (ivS 14, 30d)', spot:400, ivS:.14, ivL:.13, dteS:30, dteL:60, debit:5.20 },
];

for (const c of cases) {
  const remainT = Math.max(1, c.dteL-c.dteS)/365;
  const fx = shape(c.spot, c.spot, c.ivL, remainT, c.debit, 0.18);
  const ah = adaptiveHalf(c.ivS, c.dteS);
  const ad = shape(c.spot, c.spot, c.ivL, remainT, c.debit, ah);
  console.log('\n' + c.n);
  console.log(`  fixed +/-18.0%  : wings ${(fx.lErr*100).toFixed(1)}% / ${(fx.rErr*100).toFixed(1)}% above floor`);
  console.log(`  adapt +/-${(ah*100).toFixed(1)}%  : wings ${(ad.lErr*100).toFixed(1)}% / ${(ad.rErr*100).toFixed(1)}% above floor`);
}
