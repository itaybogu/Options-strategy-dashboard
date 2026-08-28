/* Refine plot half-width. The tent flattens once the SURVIVING back-month
   call is deep ITM/OTM, which is governed by ivL*sqrt(remainT), not by the
   front-month sigma. Take the max of both drivers. */

function N(x){const t=1/(1+0.2316419*Math.abs(x));const p=t*(0.319381530+t*(-0.356563782+t*(1.781477937+t*(-1.821255978+t*1.330274429))));const pdf=Math.exp(-0.5*x*x)/Math.sqrt(2*Math.PI);const cdf=1-pdf*p;return x>=0?cdf:1-cdf;}
function bsCall(S,K,s,T){if(T<=0||s<=0||S<=0)return Math.max(0,S-K);const sq=Math.sqrt(T);const d1=(Math.log(S/K)+0.5*s*s*T)/(s*sq);return S*N(d1)-K*N(d1-s*sq);}

function wings(spot,strike,ivL,remainT,debit,half){
  const xMin=spot*(1-half),xMax=spot*(1+half),t=-debit*100;
  const y=x=>(-Math.max(0,x-strike)+bsCall(x,strike,ivL,remainT)-debit)*100;
  let hi=-Infinity;
  for(let i=0;i<=300;i++){hi=Math.max(hi,y(xMin+(xMax-xMin)*i/300));}
  return { l:Math.abs(y(xMin)-t)/Math.abs(t), r:Math.abs(y(xMax)-t)/Math.abs(t), peak:hi };
}

// candidate: driven by max(front sigma, residual back-month sigma)
function halfWidth(ivS,ivL,dteS,dteL){
  const sigFront = ivS * Math.sqrt(dteS/365);
  const sigResid = ivL * Math.sqrt(Math.max(1,dteL-dteS)/365);
  return Math.min(0.45, Math.max(0.12, 2.8 * Math.max(sigFront, sigResid)));
}

const cases=[
 {n:'earnings pop (47/32, 14->45)', spot:100,ivS:.47,ivL:.32,dteS:14,dteL:45,debit:1.55},
 {n:'high IV      (75/50,  7->35)', spot:250,ivS:.75,ivL:.50,dteS:7, dteL:35,debit:4.10},
 {n:'extreme IV   (120/70, 5->40)', spot:80, ivS:1.20,ivL:.70,dteS:5, dteL:40,debit:3.00},
 {n:'low IV       (22/19, 21->56)', spot:60, ivS:.22,ivL:.19,dteS:21,dteL:56,debit:0.95},
 {n:'very low IV  (14/13, 30->60)', spot:400,ivS:.14,ivL:.13,dteS:30,dteL:60,debit:5.20},
 {n:'long back    (35/28, 10->120)',spot:150,ivS:.35,ivL:.28,dteS:10,dteL:120,debit:6.00},
];

for(const c of cases){
  const remainT=Math.max(1,c.dteL-c.dteS)/365;
  const h=halfWidth(c.ivS,c.ivL,c.dteS,c.dteL);
  const f=wings(c.spot,c.spot,c.ivL,remainT,c.debit,0.18);
  const a=wings(c.spot,c.spot,c.ivL,remainT,c.debit,h);
  console.log('\n'+c.n);
  console.log(`  fixed +/-18.0% : L ${(f.l*100).toFixed(1)}%  R ${(f.r*100).toFixed(1)}%  peak $${f.peak.toFixed(0)}`);
  console.log(`  new   +/-${(h*100).toFixed(1).padStart(4)}% : L ${(a.l*100).toFixed(1)}%  R ${(a.r*100).toFixed(1)}%  peak $${a.peak.toFixed(0)}`);
}
