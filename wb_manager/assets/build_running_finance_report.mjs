// Fixed owner-facing four-period dashboard. One selected basis; original facts retained.
import fs from 'node:fs/promises';
import path from 'node:path';
import {Workbook, SpreadsheetFile, FileBlob} from '@oai/artifact-tool';
const args=process.argv.slice(2), opt=k=>args[args.indexOf(k)+1];
const input=opt('--input'), output=opt('--output'), verification=opt('--verification');
if(!input||!output||!verification)throw new Error('input/output/verification required');
const m=JSON.parse(await fs.readFile(input,'utf8'));
if(m.templateVersion!=='wb-owner-dashboard-v2.4'||!m.ownerView||!m.ownerPeriodAccounts)throw new Error('Four-period owner payload required');
const wb=Workbook.create(), v=m.ownerView;
const periodKeys=['yesterday','last_week','last_month','cumulative'];
const periodByKey=Object.fromEntries(v.periods.map(p=>[p.key,p]));
if(v.periods.length!==periodKeys.length||new Set(v.periods.map(p=>p.key)).size!==periodKeys.length||periodKeys.some(k=>!periodByKey[k]))throw new Error('Four distinct owner periods required');
const periods=periodKeys.map(k=>periodByKey[k]);
const aliases=Object.keys(v.stores).sort((a,b)=>(({'owner-store':0,'new-store':1}[a]??2)-({'owner-store':0,'new-store':1}[b]??2))||a.localeCompare(b));
const labels=Object.fromEntries(aliases.map((a,i)=>[a,({'owner-store':'老店','new-store':'新店'}[a])||v.stores[a].displayName||('店铺'+(i+1))]));
const names=['汇总报告',...aliases.map(a=>labels[a]+'汇总'),...aliases.flatMap(a=>[labels[a]+'订单',labels[a]+'流水']),'周期对账'];
const sheets=Object.fromEntries(names.map(n=>[n,wb.worksheets.add(n)]));
const C={ink:'#23354A',head:'#244363',band:'#EAF1F7',muted:'#65758A',amber:'#FFF2CC',line:'#CAD4DF'};
const money='#,##0.00;[Red](#,##0.00);0.00';
const col=n=>{let s='';while(n){n--;s=String.fromCharCode(65+n%26)+s;n=Math.floor(n/26);}return s;};
const q=n=>"'"+n.replaceAll("'","''")+"'";
const dt=s=>new Date(s.slice(0,10)+'T00:00:00Z');
const prev=s=>new Date(dt(s).getTime()-86400000).toISOString().slice(0,10);
const num=x=>x==null?null:Number(x);
const n=x=>Number(x||0);
const MAX_COMPACT_EVIDENCE_CHARS=40;
const compactEvidence=x=>{
 const all=Array.isArray(x)?x.map(String).map(s=>s.replace(/\s+/g,' ').trim()).filter(Boolean):[];
 if(!all.length)return '—';
 const prefix=all.length>1?all.length+'项｜':'';
 const budget=Math.max(1,MAX_COMPACT_EVIDENCE_CHARS-prefix.length),first=all[0];
 if(first.length<=budget)return prefix+first;
 if(budget<=3)return prefix+first.slice(0,budget);
 const head=Math.ceil((budget-1)*0.6),tail=budget-1-head;
 return prefix+first.slice(0,head)+'…'+first.slice(-tail);
};
const ref=(name,cell)=>q(name)+'!'+cell;
function value(s,cell,x){s.getRange(cell).values=[[x]];}
function formula(s,cell,x){s.getRange(cell).formulas=[['='+x]];}
function base(s,end,rows){s.showGridLines=false;s.getRange('A1:'+end+rows).format={font:{name:'Arial',size:10,color:C.ink},rowHeight:26,verticalAlignment:'center'};}
function widths(s,items){for(const [c,w]of Object.entries(items))s.getRange(c+':'+c).format.columnWidth=w;}
function header(s,row,values){const r=s.getRange('A'+row+':'+col(values.length)+row);r.values=[values];r.format={fill:C.head,font:{name:'Arial',size:10,bold:true,color:'#FFFFFF'},rowHeight:40,wrapText:true,verticalAlignment:'center'};}
function note(s,range,text,height=35){s.getRange(range).merge();value(s,range.split(':')[0],text);s.getRange(range).format={wrapText:true,rowHeight:height,font:{name:'Arial',size:10,color:C.muted}};}
const metrics=['gross_sales','seller_proceeds','product_cost','agency_accrued','promotion_accrued','cash_ad_spend','logistics_cost','shared_pool_return_cost','other_cost','promo_credit_spend','agency_paid','receipts_received','withdrawal_requested','withdrawal_completed','withdrawal_cancelled','return_reserve_accrued','separate_return_cost','shared_pool_accrued'];
const metricLabels=['净销售额','平台结算应收','采购成本净额','代运营费（到账提现×2%）','广告预算净计提','自付广告实耗','独立物流费','历史共享池退货物流','其他费用','平台促销金消耗','代运营实收','账户实际回款','提现申请','提现到账','提现撤销','独立退货风险计提','独立退货池物流','历史共享储备计提'];
const flowCols=Object.fromEntries(metrics.map((k,i)=>[k,col(i+6)]));
const orderCols={gross_sales:'G',seller_proceeds:'H',product_cost:'I',promotion_accrued:'K',other_cost:'M',logistics_cost:'Y',shared_pool_return_cost:'Z',return_reserve_accrued:'AA',separate_return_cost:'AB',shared_pool_accrued:'AC'};
const bounds={}, sourceRows={}, checks=[], reconChecks=[];
for(const alias of aliases){
 const store=v.stores[alias], flowName=labels[alias]+'流水', orderName=labels[alias]+'订单';
 const rows=[...store.flowRows].sort((a,b)=>b.eventDate.localeCompare(a.eventDate)||a.eventId.localeCompare(b.eventId));
 const orders=[...store.orderRows].sort((a,b)=>b.eventDate.localeCompare(a.eventDate)||a.eventId.localeCompare(b.eventId));
 sourceRows[alias]=rows;bounds[alias]={flow:Math.max(7,6+rows.length),order:Math.max(7,6+orders.length)};
 const f=sheets[flowName];base(f,'AJ',bounds[alias].flow+2);widths(f,{A:12,B:17,C:18,D:31,E:15,X:13,Y:32,Z:31,AA:32,AB:48,AG:44,AH:38});
 for(let i=6;i<=23;i++)widths(f,{[col(i)]:16});for(let i=29;i<=32;i++)widths(f,{[col(i)]:18});widths(f,{AI:18});
 note(f,'A1:K1',labels[alias]+'账务流水｜截至 '+m.periodEnd+'｜人民币元',28);
 note(f,'A2:K2','每笔事件只记一次。订单项列真实订单号；代运营费、广告、提现等显示“店铺级”，不强行分摊到订单。',36);
 note(f,'A3:K3','“含暂估”仅供经营参考；右侧保留原始标识、正式金额与暂估差额。空金额=来源未齐，不代表0。',36);
 header(f,6,['发生日期','财务事项','WB订单号','SKU','本笔金额\n（含暂估）',...metricLabels,'归属类别','平台订单标识srid','关联说明','唯一流水ID','来源与关联证据','正式销售额','正式结算应收','暂估销售差额','暂估应收差额','成本版本','政策版本','正式广告计提','金额口径']);
 if(rows.length)f.getRange('A7:AJ'+bounds[alias].flow).values=rows.map(e=>[dt(e.eventDate),e.kindLabel+(e.amountBasis==='含暂估'?'（暂估）':''),e.orderId||(e.isOrderEvent?'待匹配':'店铺级'),e.sku||'—',null,...metrics.map(k=>num(e.amounts[k])),e.isOrderEvent?'订单项':'店铺级',e.srid||'—',e.mappingReason,e.eventId,compactEvidence(e.linkEvidence),num(e.actualAmounts.gross_sales),num(e.actualAmounts.seller_proceeds),num(e.estimateAmounts.gross_sales),num(e.estimateAmounts.seller_proceeds),e.costVersionId||'—',e.policyId||'—',num(e.actualAmounts.promotion_accrued),e.amountBasis]);
 rows.forEach((e,i)=>{
  const r=7+i;
  if(['sale','refund'].includes(e.kind)){
   if(e.amounts.gross_sales!=null)formula(f,'E'+r,'F'+r);
   return;
  }
  const keys=['agency_accrued','cash_ad_spend','promo_credit_spend','agency_paid','receipts_received','withdrawal_requested','withdrawal_completed','withdrawal_cancelled','logistics_cost','shared_pool_return_cost','separate_return_cost','other_cost'];
  const populated=keys.filter(k=>e.amounts[k]!=null&&num(e.amounts[k])!==0);
  if(!populated.length)return;
  // A withdrawal, its request and its 2% fee are different accounting facts.
  // Do not add multiple withdrawal states or a payout and its fee as one amount.
  const withdrawalKeys=populated.filter(k=>['withdrawal_requested','withdrawal_completed','withdrawal_cancelled'].includes(k));
  if(withdrawalKeys.length>1)return;
  const selected=e.sourceBasis==='completed_withdrawal_agency_fee'&&populated.includes('agency_accrued')
   ?['agency_accrued']
   :withdrawalKeys.length?populated.filter(k=>k!=='agency_accrued'):populated;
  formula(f,'E'+r,'SUM('+selected.map(k=>flowCols[k]+r).join(',')+')');
 });
 f.getRange('E7:E'+bounds[alias].flow).setNumberFormat(money);widths(f,{AJ:18});
 f.getRange('A7:A'+bounds[alias].flow).setNumberFormat('yyyy-mm-dd');f.getRange('F7:W'+bounds[alias].flow).setNumberFormat(money);f.getRange('AC7:AF'+bounds[alias].flow).setNumberFormat(money);f.getRange('AI7:AI'+bounds[alias].flow).setNumberFormat(money);
 f.getRange('A7:AI'+bounds[alias].flow).format.rowHeight=38;f.getRange('B7:E'+bounds[alias].flow).format.wrapText=true;
 f.getRange('Z7:Z'+bounds[alias].flow).format.wrapText=true;f.getRange('Y7:Y'+bounds[alias].flow).format.wrapText=false;f.getRange('AA7:AB'+bounds[alias].flow).format.wrapText=false;f.freezePanes.freezeRows(6);
 f.tables.add('A6:AJ'+bounds[alias].flow,true,'Flow_'+alias.replaceAll('-','_'));
 const versions=f.getRange('AG7:AH'+bounds[alias].flow);versions.format.font={name:'Arial',size:9,color:C.ink};versions.format.horizontalAlignment='left';versions.format.wrapText=false;
 f.getRange('E6:W'+bounds[alias].flow).format.horizontalAlignment='right';f.getRange('AC6:AF'+bounds[alias].flow).format.horizontalAlignment='right';f.getRange('AI6:AI'+bounds[alias].flow).format.horizontalAlignment='right';
 const o=sheets[orderName];base(o,'AC',bounds[alias].order+2);widths(o,{A:12,B:18,C:31,D:16,P:16,Q:14,R:34,S:34,T:48});
 for(let i=5;i<=15;i++)widths(o,{[col(i)]:16});for(let i=21;i<=29;i++)widths(o,{[col(i)]:17});
 note(o,'A1:M1',labels[alias]+'订单账｜截至 '+m.periodEnd+'｜人民币元，原币两列为RUB',28);
 note(o,'A2:M2','一行=一笔订单财务事件。同一订单可有销售、退款、运费多行；退款记在发生期，冲回原成本及订单储备计提。代运营费按已确认提现到账逐笔×2%，只列店铺级流水。',36);
 note(o,'A3:M3','按本页“发生日期”与流水筛选同一周期；订单金额＋店铺级流水＝全部流水。平台促销金不列入广告成本。',36);
 header(o,6,['发生日期','WB订单号','SKU','财务事项','销售额未扣退款','退款金额','净销售额','平台结算应收','采购成本净额','代运营费（店铺级，不按订单计）','广告预算净计提','实际物流合计','其他费用','原币销售RUB','原币应收RUB','金额口径','匹配状态','平台订单标识srid','唯一流水ID','关联证据','正式销售额','正式结算应收','暂估销售差额','暂估应收差额','独立物流费','历史共享池退货物流','独立退货风险计提','独立退货池物流','历史共享储备计提']);
 if(orders.length)o.getRange('A7:AC'+bounds[alias].order).values=orders.map(e=>{
  const a=e.amounts, g=num(a.gross_sales);
  const native=(m.nativeStatistics||[]).filter(x=>['sale','refund'].includes(e.kind)&&(x.store||x.storeAlias||x.store_id)===alias&&(e.saleIds||[]).includes(String(x.saleId||x.sale_id||x.saleID)));
  const nativeOne=native.length===1?native[0]:null;
  return [dt(e.eventDate),e.orderId||'待匹配',e.sku||'—',e.kindLabel,g==null?null:Math.max(0,g),g==null?null:Math.max(0,-g),g,num(a.seller_proceeds),num(a.product_cost),null,num(a.promotion_accrued),null,num(a.other_cost),nativeOne?num(nativeOne.grossRub):null,nativeOne?num(nativeOne.proceedsRub):null,e.amountBasis,e.mappingStatus,e.srid||'—',e.eventId,compactEvidence(e.linkEvidence),num(e.actualAmounts.gross_sales),num(e.actualAmounts.seller_proceeds),num(e.estimateAmounts.gross_sales),num(e.estimateAmounts.seller_proceeds),num(a.logistics_cost),num(a.shared_pool_return_cost),num(a.return_reserve_accrued),num(a.separate_return_cost),num(a.shared_pool_accrued)];
 });
 orders.forEach((e,i)=>{const r=7+i;if(e.amounts.gross_sales!=null)formula(o,'G'+r,'E'+r+'-F'+r);formula(o,'L'+r,'SUM(Y'+r+':Z'+r+',AB'+r+')');});
 o.getRange('A7:A'+bounds[alias].order).setNumberFormat('yyyy-mm-dd');o.getRange('E7:O'+bounds[alias].order).setNumberFormat(money);o.getRange('U7:AC'+bounds[alias].order).setNumberFormat(money);
 o.getRange('A7:AC'+bounds[alias].order).format.rowHeight=38;o.getRange('C7:D'+bounds[alias].order).format.wrapText=true;o.getRange('P7:Q'+bounds[alias].order).format.wrapText=true;o.getRange('R7:T'+bounds[alias].order).format.wrapText=false;o.freezePanes.freezeRows(6);
 o.tables.add('A6:AC'+bounds[alias].order,true,'Orders_'+alias.replaceAll('-','_'));
 o.getRange('E6:O'+bounds[alias].order).format.horizontalAlignment='right';o.getRange('U6:AC'+bounds[alias].order).format.horizontalAlignment='right';
}
// Use plain Excel date serials so helper dates never leak into reader-facing columns
// and the calculation engine does not propagate date formatting into SUMIFS results.
const dateRef=d=>String(Math.floor(Date.parse(d+'T00:00:00Z')/86400000)+25569);
function sum(alias,metric,start,end,{order=false,storeOnly=false,actual=false,sign=null}={}){
 const c=actual&&metric==='promotion_accrued'?'AI':(order?orderCols:flowCols)[metric];if(!c)return '0';
 const sn=labels[alias]+(order?'订单':'流水'),last=bounds[alias][order?'order':'flow'];
 const rr=cc=>ref(sn,'$'+cc+'$7:$'+cc+'$'+last);
 let s='SUMIFS('+rr(c)+','+rr('A')+',">="&'+dateRef(start)+','+rr('A')+',"<="&'+dateRef(end);
 if(storeOnly)s+=','+rr('X')+',"店铺级"';if(sign)s+=','+rr(c)+',"'+sign+'"';return s+')';
}
const periodAccount=(a,p,actual=false)=>(actual?m.ownerPeriodAccounts[p.key].actual:m.ownerPeriodAccounts[p.key].provisional).stores[a];
const metricRecord=(a,p,k,cumulative=false,actual=false)=>periodAccount(a,p,actual)?.[cumulative?'cumulative':'period']?.metrics?.[k];
const expected=(a,p,k,cumulative=false,actual=false)=>n(metricRecord(a,p,k,cumulative,actual)?.known_subtotal);
const known=(a,p,k,cumulative=false,actual=false)=>metricRecord(a,p,k,cumulative,actual)?.complete;
const opened=a=>v.stores[a].openedAt;
const unallocated=(p,cumulative=false)=>n(m.agencyReceiptRegister?.unallocatedByPeriod?.[p.key]?.[cumulative?'cumulativeCny':'periodCny']);
const cashPosition=(a,p)=>a==='ALL'?m.ownerCashPositions?.periods?.[p.key]?.ALL:m.ownerCashPositions?.periods?.[p.key]?.stores?.[a];
function excess(a,end){const ss=k=>sum(a,k,opened(a),end);return '(MAX(0,'+ss('shared_pool_return_cost')+'-MAX(0,'+ss('shared_pool_accrued')+'))+MAX(0,'+ss('separate_return_cost')+'-MAX(0,'+ss('return_reserve_accrued')+')))';}
const summaryMetricRows={
 gross_sales:5,refunds:6,net_sales:7,platform_receivable:8,net_purchase_cost:9,
 actual_logistics:10,other_expenses:11,operating_profit_cash_ads:12,
 formal_ad_accrual:13,cash_ad_spend:14,formal_ad_budget_balance:15,
 agency_accrual:16,agency_receipts:17,agency_payable:18,
 funds_total:19,completed_withdrawals_cumulative:20,wb_balance_snapshot:21,pending_withdrawals:22
};
const summaryLabels={
 5:'营业额｜销售额（未扣退款）',6:'营业额｜退款',7:'营业额｜净额',
 8:'平台结算应收',9:'采购成本净额',10:'实际物流费用',11:'其他费用（已知）',
 12:'经营利润｜按现金广告实耗（含暂估）',
 13:'广告｜正式净计提',14:'广告｜现金实耗',15:'广告｜正式预算结余（期末）',
 16:'代运营费｜本期应计',17:'代运营费｜本期实收',18:'代运营费｜期末待付',
 19:'资金｜期末回款总额',20:'资金｜累计提现到账',21:'资金｜WB余额快照',22:'资金｜处理中提现（期末）'
};
const summaryNotes={
 5:'本期正数销售额，含已批准暂估。',6:'本期退款金额，含前期订单本期退回。',
 7:'销售额减退款；与平台应收和到账不同。',8:'卖家结算应收净额，含已批准暂估；并非现金到账。',
 9:'采购成本扣除已确认可再售退货成本。',10:'独立物流、历史共享池及独立退货池实际运费，各计一次。',
 11:'仅列已采集费用；显示待补时不能视为零。',
 12:'应收减采购、代运营应计、现金广告实耗、实际物流及已知其他费；含暂估，非最终利润。',
 13:'本期正式订单政策计提减退款冲回；不含暂估计提。',14:'卖家自付实际广告消耗；平台促销金不计入。',
 15:'截至期末，正式累计净计提减累计现金广告实耗及共享池退货运费；负数保留。',
 16:'按已确认提现到账逐笔计提2%，按到账日归期。',
 17:'按收款日归期；分店只列已分摊实收，合并另加待分摊收款。',
 18:'截至期末累计应计减累计实收；合并再减待分摊收款和非现金尾差调整。',
 19:'截至期末累计提现到账＋该期末最近的WB账户余额快照；不是本期现金流入。',
 20:'截至该列期末已确认提现到账的累计额；申请与处理中均不算到账。',
 21:'该列期末以前最近的WB余额快照，保留负数；不是可提现额度。',
 22:'截至期末累计申请－累计到账－累计撤销；仅反映已登记的处理中金额。'
};
const adjustment=p=>n(m.agencySettlementAdjustments?.byPeriod?.[p.key]?.cumulativeCny);
const fmt=x=>Number(x).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
const summaryName=a=>a==='ALL'?'汇总报告':labels[a]+'汇总';
const summaryCell=(a,c,r)=>ref(summaryName(a),c+r);
const selectedMetrics=['gross_sales','seller_proceeds','product_cost','agency_accrued','cash_ad_spend','advertising_available','agency_paid','withdrawal_pending','operating_contribution'];
for(const alias of [...aliases,'ALL']){
 const s=sheets[summaryName(alias)];base(s,'G',28);widths(s,{A:43,B:22,C:22,D:22,E:22,F:3,G:86});s.freezePanes.freezeRows(4);
 header(s,1,[alias==='ALL'?'两店合并｜财务摘要':labels[alias]+'｜财务摘要',...periods.map(p=>p.label)]);
 value(s,'A2','周期开始');value(s,'A3','周期结束');value(s,'A4','人民币元｜期间数与期末数见行名');
 periods.forEach((p,i)=>{const cc=col(i+2);value(s,cc+'2',dt(p.start));value(s,cc+'3',dt(p.end));s.getRange(cc+'2:'+cc+'3').setNumberFormat('yyyy-mm-dd');});
 for(const [r,label]of Object.entries(summaryLabels))value(s,'A'+r,label);
 value(s,'G1','指标说明');
 value(s,'G2','列序：昨日 → 上周 → 上月 → 累计；四列有重叠，不能相加。');
 value(s,'G3','正式广告预算不含暂估；暂估与缺口见流水，辅助指标见周期对账。');
 value(s,'G4','日期按莫斯科财务日；待补不等于零。');
 for(const [r,t]of Object.entries(summaryNotes))value(s,'G'+r,t);
 const hasUnallocated=unallocated(periodByKey.cumulative,true)>0;
 if(alias!=='ALL'&&hasUnallocated){
  value(s,'A17','代运营费｜本期实收（已分摊）');
  value(s,'A18','代运营费｜期末待付（分摊前）');
  value(s,'G17',summaryNotes[17]+' 双店待分摊款不归入本店。');
  value(s,'G18',summaryNotes[18]+' 双店待分摊款尚未确定本店份额，此数为分摊前金额。');
 }
 if(alias==='ALL'&&hasUnallocated){
  const receiptDates=(m.agencyReceiptRegister?.unallocatedReceipts||[]).slice(-2).map(x=>x.date+' '+fmt(x.amountCny)+'元').join('；');
  value(s,'G17',summaryNotes[17]+' 待分摊：'+receiptDates+'。');
 }
 if(alias==='ALL'&&adjustment(periodByKey.cumulative)!==0)value(s,'G18',summaryNotes[18]+' 本笔非现金尾差 '+fmt(adjustment(periodByKey.cumulative))+' 元已处理。');
 const snapshotNotes=periods.map(p=>p.label+'：'+(alias==='ALL'?aliases:[alias]).map(a=>{const x=cashPosition(a,p);return labels[a]+(x?.snapshotDay?' '+x.snapshotDay:' 缺快照');}).join('/')).join('；');
 value(s,'G21',summaryNotes[21]+' 快照日 '+snapshotNotes+'；日内快照不代表日终确认。');
 s.getRange('G2:G26').format={font:{name:'Arial',size:10,color:C.muted},wrapText:true,verticalAlignment:'center'};
 s.getRange('A5:G22').format.rowHeight=34;s.getRange('A12:G12').format.rowHeight=46;
 s.getRange('A17:G18').format.rowHeight=45;s.getRange('A21:G21').format.rowHeight=72;
 for(const r of [7,12,15,18,19,22])s.getRange('A'+r+':E'+r).format={fill:r===12?C.amber:C.band,font:{name:'Arial',size:10,bold:true,color:C.ink}};
 for(let pi=0;pi<periods.length;pi++){
  const p=periods[pi],cc=col(pi+2),put=(r,x)=>formula(s,cc+r,x);
  if(alias==='ALL'){
   for(let r=5;r<=22;r++){
    if(r===17){put(r,'SUM('+aliases.map(a=>summaryCell(a,cc,r)).join(',')+')+'+unallocated(p));checks.push({sheet:s.name,cell:cc+r,expected:expected('ALL',p,'agency_paid')});continue;}
    if(r===18){
     if(aliases.some(a=>!known(a,p,'agency_paid',true))){value(s,cc+r,'实收历史待补');continue;}
     put(r,'ROUND('+aliases.map(a=>sum(a,'agency_accrued',opened(a),p.end)).join('+')+'-'+aliases.map(a=>sum(a,'agency_paid',opened(a),p.end)).join('-')+'-'+unallocated(p,true)+'-'+adjustment(p)+',2)+0');
     checks.push({sheet:s.name,cell:cc+r,expected:expected('ALL',p,'agency_payable',true)});continue;
    }
    if([19,21].includes(r)&&aliases.some(a=>cashPosition(a,p)?.accountBalanceCny==null)){value(s,cc+r,'缺期末余额快照');continue;}
    if(r===11&&aliases.every(a=>!known(a,p,'other_cost')&&expected(a,p,'other_cost')===0)){value(s,cc+r,'待补');continue;}
    put(r,'SUM('+aliases.map(a=>summaryCell(a,cc,r)).join(',')+')');
    if(r===19||r===21){const x=cashPosition('ALL',p);const target=r===19?x?.knownSubtotalCny:x?.accountBalanceCny;if(target!=null)checks.push({sheet:s.name,cell:cc+r,expected:Number(target)});}
   }
   continue;
  }
  const a=alias,ss=k=>sum(a,k,p.start,p.end),cs=k=>sum(a,k,opened(a),p.end);
  put(5,sum(a,'gross_sales',p.start,p.end,{sign:'>0'}));put(6,'-'+sum(a,'gross_sales',p.start,p.end,{sign:'<0'}));put(7,cc+'5-'+cc+'6');
  put(8,ss('seller_proceeds'));put(9,ss('product_cost'));
  put(10,ss('logistics_cost')+'+'+ss('shared_pool_return_cost')+'+'+ss('separate_return_cost'));
  if(!known(a,p,'other_cost')&&expected(a,p,'other_cost')===0)value(s,cc+'11','待补');else put(11,ss('other_cost'));
  put(12,cc+'8-'+cc+'9-'+cc+'16-'+cc+'14-'+cc+'10-SUM('+cc+'11)');
  put(13,sum(a,'promotion_accrued',p.start,p.end,{actual:true}));put(14,ss('cash_ad_spend'));
  put(15,sum(a,'promotion_accrued',opened(a),p.end,{actual:true})+'-'+cs('cash_ad_spend')+'-'+cs('shared_pool_return_cost'));
  put(16,ss('agency_accrued'));put(17,ss('agency_paid'));
  if(known(a,p,'agency_paid',true))put(18,'ROUND('+cs('agency_accrued')+'-'+cs('agency_paid')+',2)+0');else value(s,cc+'18','实收历史待补');
  const position=cashPosition(a,p);
  if(position?.accountBalanceCny!=null){value(s,cc+'21',Number(position.accountBalanceCny));put(19,cs('withdrawal_completed')+'+'+cc+'21');
   if(position.knownSubtotalCny!=null)checks.push({sheet:s.name,cell:cc+'19',expected:Number(position.knownSubtotalCny)});
   checks.push({sheet:s.name,cell:cc+'21',expected:Number(position.accountBalanceCny)});
  }else{value(s,cc+'19','缺期末余额快照');value(s,cc+'21','缺期末余额快照');}
  put(20,cs('withdrawal_completed'));
  put(22,cs('withdrawal_requested')+'-'+cs('withdrawal_completed')+'-'+cs('withdrawal_cancelled'));
  for(const [r,k,cumulative,actual]of [[7,'gross_sales'],[8,'seller_proceeds'],[9,'product_cost'],[12,'operating_contribution'],[13,'promotion_accrued',false,true],[14,'cash_ad_spend'],[15,'advertising_available',true,true],[16,'agency_accrued'],[17,'agency_paid'],[18,'agency_payable',true],[20,'withdrawal_completed',true],[22,'withdrawal_pending',true]]){
   if(r===18&&!known(a,p,'agency_paid',true))continue;
   if(metricRecord(a,p,k,!!cumulative,!!actual))checks.push({sheet:s.name,cell:cc+r,expected:expected(a,p,k,!!cumulative,!!actual)});
  }
 }
 if(alias==='ALL'){
  value(s,'A23','分店重点（利润按各列；其余为累计快照）');
  for(let i=0;i<aliases.length;i++){
   const a=aliases[i],r=24+i,p=periodByKey.cumulative,x=cashPosition(a,p),budget=expected(a,p,'advertising_available',true,true),pending=expected(a,p,'withdrawal_pending',true);
   const gaps=selectedMetrics.filter(k=>!known(a,p,k,['advertising_available','withdrawal_pending'].includes(k),k==='advertising_available')).length;
   value(s,'A'+r,labels[a]+'重点｜经营利润');
   periods.forEach((period,pi)=>{const cc=col(pi+2);formula(s,cc+r,summaryCell(a,cc,12));});
   value(s,'G'+r,'正式预算 '+fmt(budget)+'；'+(budget<0?'负预算；':'')+'WB余额 '+(x?.accountBalanceCny==null?'待补':fmt(x.accountBalanceCny))+(Number(x?.accountBalanceCny)<0?'（负余额）':'')+'；处理中 '+fmt(pending)+'；关键来源缺口 '+gaps+'项。');
   s.getRange('A'+r+':G'+r).format.rowHeight=54;
  }
  s.getRange('A23:E23').format={fill:C.head,font:{name:'Arial',size:10,bold:true,color:'#FFFFFF'},rowHeight:30};
  s.getRange('B24:E25').setNumberFormat(money);
 }
 s.getRange('B5:E22').setNumberFormat(money);s.getRange('B1:E25').format.horizontalAlignment='right';
 s.getRange('A1:E'+(alias==='ALL'?25:22)).format.borders={bottom:{style:'hair',color:C.line}};
}
// Reconciliation is terminal QA, never a source for reported money.
const recon=sheets['周期对账'], reconMetrics=['gross_sales','seller_proceeds','product_cost','agency_accrued','promotion_accrued','logistics_cost','shared_pool_return_cost','other_cost','cash_ad_spend','agency_paid','withdrawal_requested','withdrawal_completed'];
base(recon,'I',132);widths(recon,{A:14,B:29,C:23,D:18,E:18,F:18,G:36,H:17,I:22});
note(recon,'A1:I1','周期对账｜订单项＋店铺级流水＝全部流水｜人民币元',28);
note(recon,'A2:I3','对账通过只表示已采集账簿内部一致，不表示平台来源已全部齐全。广告及提现不强挂订单；无来源金额不靠造单补齐。',28);
header(recon,6,['店铺','周期','财务指标','订单表金额','店铺级流水','全部流水金额','差额','待匹配订单事件','核对结果']);
let rr=7;
for(const a of aliases)for(const p of periods)for(const k of reconMetrics){
 value(recon,'A'+rr,labels[a]);value(recon,'B'+rr,p.label+' '+p.start+'~'+p.end);value(recon,'C'+rr,metricLabels[metrics.indexOf(k)]);
 formula(recon,'D'+rr,sum(a,k,p.start,p.end,{order:true}));formula(recon,'E'+rr,sum(a,k,p.start,p.end,{storeOnly:true}));formula(recon,'F'+rr,sum(a,k,p.start,p.end));
 formula(recon,'G'+rr,'D'+rr+'+E'+rr+'-F'+rr);
 value(recon,'H'+rr,v.stores[a].orderRows.filter(e=>e.eventDate>=p.start&&e.eventDate<=p.end&&e.mappingStatus!=='已关联').length);
 formula(recon,'I'+rr,'IF(ABS(G'+rr+')>0.005,"差额待查",IF(H'+rr+'>0,"金额相符／订单待匹配","对账相符"))');reconChecks.push('G'+rr);rr++;
}
recon.getRange('D7:G'+(rr-1)).setNumberFormat(money);recon.getRange('B7:C'+(rr-1)).format.wrapText=true;recon.getRange('A7:I'+(rr-1)).format.rowHeight=36;recon.freezePanes.freezeRows(6);recon.tables.add('A6:I'+(rr-1),true,'Period_Reconciliation');
recon.getRange('D6:H'+(rr-1)).format.horizontalAlignment='right';
const auxiliaryMetrics=[
 ['budget_profit','预留广告预算后利润','按预算及独立退货风险预留；与首页现金广告利润不可相加。'],
 ['provisional_ad_balance','含暂估广告预算余额','累计预算含暂估，减实际自付广告及共享池退货运费。'],
 ['withdrawal_requests','有效提现申请','当期申请减撤销；申请不等于到账。'],
 ['promo_credit','平台促销金消耗','平台承担，不计卖家广告成本。'],
 ['return_reserve','独立退货风险净计提','本期订单计提减退单冲回。'],
 ['excess_return_loss','退货储备超额损失变动','分店分别计算池超额，再取期末与期初差。']
];
const appendixHeader=rr+3,appendixRows={};
note(recon,'A'+(appendixHeader-2)+':I'+(appendixHeader-2),'辅助指标｜保留首页精简后移出的经营、广告、提现与退货口径',28);
header(recon,appendixHeader,['店铺','辅助指标',...periods.map(p=>p.label),'口径说明']);
let ar=appendixHeader+1;
for(const a of [...aliases,'ALL']){
 appendixRows[a]={};
 for(const [key,label,description]of auxiliaryMetrics){
  const row=ar++;appendixRows[a][key]=row;value(recon,'A'+row,a==='ALL'?'两店合并':labels[a]);value(recon,'B'+row,label);value(recon,'G'+row,description);
  for(let pi=0;pi<periods.length;pi++){
   const p=periods[pi],cc=col(pi+3);
   if(a==='ALL'){formula(recon,cc+row,'SUM('+aliases.map(store=>cc+appendixRows[store][key]).join(',')+')');continue;}
   const ss=k=>sum(a,k,p.start,p.end),cs=k=>sum(a,k,opened(a),p.end);
   const expr={
    budget_profit:()=>ss('seller_proceeds')+'-'+ss('product_cost')+'-'+ss('agency_accrued')+'-'+ss('promotion_accrued')+'-'+ss('return_reserve_accrued')+'-'+ss('logistics_cost')+'-'+ss('other_cost')+'-('+excess(a,p.end)+'-'+excess(a,prev(p.start))+')',
    provisional_ad_balance:()=>cs('promotion_accrued')+'-'+cs('cash_ad_spend')+'-'+cs('shared_pool_return_cost'),
    withdrawal_requests:()=>ss('withdrawal_requested')+'-'+ss('withdrawal_cancelled'),
    promo_credit:()=>ss('promo_credit_spend'),
    return_reserve:()=>ss('return_reserve_accrued'),
    excess_return_loss:()=>excess(a,p.end)+'-'+excess(a,prev(p.start))
   }[key]();formula(recon,cc+row,expr);
  }
 }
}
recon.getRange('C'+(appendixHeader+1)+':F'+(ar-1)).setNumberFormat(money);
recon.getRange('A'+(appendixHeader+1)+':G'+(ar-1)).format.rowHeight=38;
recon.getRange('B'+(appendixHeader+1)+':B'+(ar-1)).format.wrapText=true;
recon.getRange('G'+(appendixHeader+1)+':G'+(ar-1)).format.wrapText=true;
const appendixPreviewRange='A'+(appendixHeader-2)+':I'+(ar-1);
const previewRanges=Object.fromEntries(names.map(name=>[name,name.endsWith('汇总')||name==='汇总报告'?'A1:G26':name==='周期对账'?'A1:I22':name.endsWith('订单')?'A1:AC15':'A1:AJ15']));
const formulaError=/^#(?:REF!|DIV\/0!|VALUE!|NAME\?|N\/A|NUM!|NULL!|SPILL!|CALC!)$/;
function validate(book){
 const failures=[];for(const x of checks){const actual=book.worksheets.getItem(x.sheet).getRange(x.cell).values[0][0];if(typeof actual!=='number'||Math.abs(actual-x.expected)>=0.005)failures.push({...x,actual});}
 for(const c of reconChecks){const actual=book.worksheets.getItem('周期对账').getRange(c).values[0][0];if(typeof actual!=='number'||Math.abs(actual)>0.005)failures.push({reconciliation:c,actual});}
 for(const name of names)for(const row of book.worksheets.getItem(name).getUsedRange(true).values)for(const x of row)if(typeof x==='string'&&formulaError.test(x))failures.push({sheet:name,formulaError:x});
 if(failures.length)throw new Error('Owner dashboard validation failed: '+JSON.stringify(failures.slice(0,18)));
}
function evidenceStats(book,name,column,rowCount){
 const stats={column,rowsChecked:rowCount,lastDataRow:rowCount?6+rowCount:null,longestEvidenceRow:null,longestCharacters:0,singleLine:true,withinCharacterLimit:true};
 if(!rowCount)return stats;
 const values=book.worksheets.getItem(name).getRange(column+'7:'+column+(6+rowCount)).values;
 values.forEach((row,index)=>{
  const text=String(row[0]??'');
  if(/[\r\n]/.test(text))stats.singleLine=false;
  if(text.length>MAX_COMPACT_EVIDENCE_CHARS)stats.withinCharacterLimit=false;
  if(text.length>=stats.longestCharacters){stats.longestCharacters=text.length;stats.longestEvidenceRow=7+index;}
 });
 return stats;
}
function validateLayout(book){
 const violations=[],sheetColumns={},evidence={};
 const checkSheet=(name,expectedMaxColumn,evidenceColumn,rowCount)=>{
  const used=book.worksheets.getItem(name).getUsedRange(),actualMaxColumn=col(used.columnIndex+used.columnCount);
  sheetColumns[name]={kind:name.endsWith('订单')?'order':name.endsWith('流水')?'flow':name==='周期对账'?'reconciliation':'summary',expectedMaxColumn,actualMaxColumn};
  if(actualMaxColumn!==expectedMaxColumn)violations.push({sheet:name,expectedMaxColumn,actualMaxColumn,reason:'column_boundary'});
  if(evidenceColumn){
   const stats=evidenceStats(book,name,evidenceColumn,rowCount);evidence[name]=stats;
   if(!stats.singleLine||!stats.withinCharacterLimit)violations.push({sheet:name,...stats,reason:'compact_evidence'});
  }
 };
 checkSheet('汇总报告','G');
 for(const alias of aliases){
  checkSheet(labels[alias]+'汇总','G');
  checkSheet(labels[alias]+'订单','AC','T',v.stores[alias].orderRows.length);
  checkSheet(labels[alias]+'流水','AJ','AB',sourceRows[alias].length);
 }
 checkSheet('周期对账','I');
 for(const [name,range]of Object.entries(previewRanges)){
  const match=/^A1:([A-Z]+)\d+$/.exec(range),expected=sheetColumns[name].expectedMaxColumn;
  if(!match||match[1]!==expected)violations.push({sheet:name,range,expectedMaxColumn:expected,reason:'preview_not_full_width'});
 }
 if(violations.length)throw new Error('Owner dashboard layout validation failed: '+JSON.stringify(violations.slice(0,18)));
 return {version:'owner-dashboard-layout-v2',passed:true,flowMaxColumn:'AJ',orderMaxColumn:'AC',noHelperColumnsBeyond:true,readableCompactEvidence:true,compactEvidenceMaxCharacters:MAX_COMPACT_EVIDENCE_CHARS,previewRangesFullWidth:true,sheets:sheetColumns,evidence,previewRanges,appendixPreviewRange};
}
wb.recalculate();validate(wb);validateLayout(wb);
// Test a source edit affects consolidated expense AND budget, then restore.
let recalcVerified=false,recalcMetric=null;
const cumulativeCol=col(periodKeys.indexOf('cumulative')+2);
for(const a of aliases){const i=sourceRows[a].findIndex(e=>n(e.amounts.cash_ad_spend)>0);if(i<0)continue;
 const f=sheets[labels[a]+'流水'],cell='K'+(7+i),old=f.getRange(cell).values[0][0],summary=sheets['汇总报告'];
 const expense=summary.getRange(cumulativeCol+summaryMetricRows.cash_ad_spend).values[0][0],balance=summary.getRange(cumulativeCol+summaryMetricRows.formal_ad_budget_balance).values[0][0];
 value(f,cell,old+1);wb.recalculate();
 if(Math.abs(summary.getRange(cumulativeCol+summaryMetricRows.cash_ad_spend).values[0][0]-expense-1)>0.001||Math.abs(summary.getRange(cumulativeCol+summaryMetricRows.formal_ad_budget_balance).values[0][0]-balance+1)>0.001)throw new Error('Source edit did not propagate');
 value(f,cell,old);wb.recalculate();validate(wb);recalcVerified=true;recalcMetric='cash_ad_spend';break;
}
if(!recalcVerified)for(const a of aliases){const i=sourceRows[a].findIndex(e=>n(e.amounts.agency_accrued)>0);if(i<0)continue;
 const f=sheets[labels[a]+'流水'],cell=flowCols.agency_accrued+(7+i),old=f.getRange(cell).values[0][0],summary=sheets['汇总报告'];
 const accrued=summary.getRange(cumulativeCol+summaryMetricRows.agency_accrual).values[0][0],profit=summary.getRange(cumulativeCol+summaryMetricRows.operating_profit_cash_ads).values[0][0];
 value(f,cell,old+1);wb.recalculate();
 if(Math.abs(summary.getRange(cumulativeCol+summaryMetricRows.agency_accrual).values[0][0]-accrued-1)>0.001||Math.abs(summary.getRange(cumulativeCol+summaryMetricRows.operating_profit_cash_ads).values[0][0]-profit+1)>0.001)throw new Error('Agency source edit did not propagate');
 value(f,cell,old);wb.recalculate();validate(wb);recalcVerified=true;recalcMetric='agency_accrued';break;
}
await fs.mkdir(path.dirname(output),{recursive:true});const previewDir=path.join(path.dirname(output),'previews');await fs.mkdir(previewDir,{recursive:true});
const rendered=[];
for(const name of names){const preview=await wb.render({sheetName:name,range:previewRanges[name],scale:1.25,format:'png'});const target=path.join(previewDir,name+'.png');await fs.writeFile(target,new Uint8Array(await preview.arrayBuffer()));rendered.push(target);}
const appendixPreview=path.join(previewDir,'周期对账_辅助指标.png');
const appendixImage=await wb.render({sheetName:'周期对账',range:appendixPreviewRange,scale:1.25,format:'png'});
await fs.writeFile(appendixPreview,new Uint8Array(await appendixImage.arrayBuffer()));
const file=await SpreadsheetFile.exportXlsx(wb);await file.save(output);
const saved=await SpreadsheetFile.importXlsx(await FileBlob.load(output));saved.recalculate();validate(saved);const layoutContract=validateLayout(saved);
await fs.writeFile(verification,JSON.stringify({templateVersion:m.templateVersion,formulaErrorCount:0,totalsVerified:true,verifiedMetricCount:checks.length,reconciliationRows:reconChecks.length,reconciliationDifference:0,recalcVerified,recalcMetric,summaryMetricRows,summaryMetricCount:Object.keys(summaryMetricRows).length,layoutContract,mapping:Object.fromEntries(aliases.map(a=>[a,v.stores[a].mappingSummary])),renderedSheets:rendered,appendixPreview,output,sourceCutoff:m.sourceCutoff},null,2));
console.log(JSON.stringify({output,sheets:names.length,formulaErrorCount:0,verifiedMetricCount:checks.length,reconciliationRows:reconChecks.length,recalcVerified,layoutContractPassed:layoutContract.passed}));
