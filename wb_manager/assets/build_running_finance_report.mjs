// Fixed owner-facing four-period dashboard. One selected basis; original facts retained.
import fs from 'node:fs/promises';
import path from 'node:path';
import {Workbook, SpreadsheetFile, FileBlob} from '@oai/artifact-tool';
const args=process.argv.slice(2), opt=k=>args[args.indexOf(k)+1];
const input=opt('--input'), output=opt('--output'), verification=opt('--verification');
if(!input||!output||!verification)throw new Error('input/output/verification required');
const m=JSON.parse(await fs.readFile(input,'utf8'));
if(m.templateVersion!=='wb-owner-dashboard-v2.1'||!m.ownerView||!m.ownerPeriodAccounts)throw new Error('Four-period owner payload required');
const wb=Workbook.create(), v=m.ownerView, periods=v.periods, aliases=Object.keys(v.stores);
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
const metricLabels=['净销售额','平台结算应收','采购成本净额','代运营净计提','广告预算净计提','自付广告实耗','独立物流费','历史共享池退货物流','其他费用','平台促销金消耗','代运营实收','账户实际回款','提现申请','提现到账','提现撤销','独立退货风险计提','独立退货池物流','历史共享储备计提'];
const flowCols=Object.fromEntries(metrics.map((k,i)=>[k,col(i+6)]));
const orderCols={gross_sales:'G',seller_proceeds:'H',product_cost:'I',agency_accrued:'J',promotion_accrued:'K',other_cost:'M',logistics_cost:'Y',shared_pool_return_cost:'Z',return_reserve_accrued:'AA',separate_return_cost:'AB',shared_pool_accrued:'AC'};
const bounds={}, sourceRows={}, checks=[], reconChecks=[];
for(const alias of aliases){
 const store=v.stores[alias], flowName=labels[alias]+'流水', orderName=labels[alias]+'订单';
 const rows=[...store.flowRows].sort((a,b)=>b.eventDate.localeCompare(a.eventDate)||a.eventId.localeCompare(b.eventId));
 const orders=[...store.orderRows].sort((a,b)=>b.eventDate.localeCompare(a.eventDate)||a.eventId.localeCompare(b.eventId));
 sourceRows[alias]=rows;bounds[alias]={flow:Math.max(7,6+rows.length),order:Math.max(7,6+orders.length)};
 const f=sheets[flowName];base(f,'AJ',bounds[alias].flow+2);widths(f,{A:12,B:17,C:18,D:31,E:15,X:13,Y:32,Z:31,AA:32,AB:48,AG:44,AH:38});
 for(let i=6;i<=23;i++)widths(f,{[col(i)]:16});for(let i=29;i<=32;i++)widths(f,{[col(i)]:18});widths(f,{AI:18});
 note(f,'A1:K1',labels[alias]+'账务流水｜截至 '+m.periodEnd+'｜人民币元',28);
 note(f,'A2:K2','每笔事件只记一次。订单项列真实订单号；广告、提现等显示“店铺级”，不强行分摊到订单。',36);
 note(f,'A3:K3','“含暂估”仅供经营参考；右侧保留原始标识、正式金额与暂估差额。空金额=来源未齐，不代表0。',36);
 header(f,6,['发生日期','财务事项','WB订单号','SKU','本笔金额\n（含暂估）',...metricLabels,'归属类别','平台订单标识srid','关联说明','唯一流水ID','来源与关联证据','正式销售额','正式结算应收','暂估销售差额','暂估应收差额','成本版本','政策版本','正式广告计提','金额口径']);
 if(rows.length)f.getRange('A7:AJ'+bounds[alias].flow).values=rows.map(e=>[dt(e.eventDate),e.kindLabel+(e.amountBasis==='含暂估'?'（暂估）':''),e.orderId||(e.isOrderEvent?'待匹配':'店铺级'),e.sku||'—',null,...metrics.map(k=>num(e.amounts[k])),e.isOrderEvent?'订单项':'店铺级',e.srid||'—',e.mappingReason,e.eventId,compactEvidence(e.linkEvidence),num(e.actualAmounts.gross_sales),num(e.actualAmounts.seller_proceeds),num(e.estimateAmounts.gross_sales),num(e.estimateAmounts.seller_proceeds),e.costVersionId||'—',e.policyId||'—',num(e.actualAmounts.promotion_accrued),e.amountBasis]);
 rows.forEach((e,i)=>{const r=7+i;if(['sale','refund'].includes(e.kind)){if(e.amounts.gross_sales!=null)formula(f,'E'+r,'F'+r);}else{const keys=['cash_ad_spend','promo_credit_spend','agency_paid','receipts_received','withdrawal_requested','withdrawal_completed','withdrawal_cancelled','logistics_cost','shared_pool_return_cost','separate_return_cost','other_cost'];if(keys.every(k=>e.amounts[k]!=null))formula(f,'E'+r,'SUM('+keys.map(k=>flowCols[k]+r).join(',')+')');}});
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
 note(o,'A2:M2','一行=一笔订单财务事件。同一订单可有销售、退款、运费多行；退款记在发生期，负数冲回原成本和计提。',36);
 note(o,'A3:M3','按本页“发生日期”与流水筛选同一周期；订单金额＋店铺级流水＝全部流水。平台促销金不列入广告成本。',36);
 header(o,6,['发生日期','WB订单号','SKU','财务事项','销售额未扣退款','退款金额','净销售额','平台结算应收','采购成本净额','代运营净计提','广告预算净计提','实际物流合计','其他费用','原币销售RUB','原币应收RUB','金额口径','匹配状态','平台订单标识srid','唯一流水ID','关联证据','正式销售额','正式结算应收','暂估销售差额','暂估应收差额','独立物流费','历史共享池退货物流','独立退货风险计提','独立退货池物流','历史共享储备计提']);
 if(orders.length)o.getRange('A7:AC'+bounds[alias].order).values=orders.map(e=>{
  const a=e.amounts, g=num(a.gross_sales);
  const native=(m.nativeStatistics||[]).filter(x=>['sale','refund'].includes(e.kind)&&(x.store||x.storeAlias||x.store_id)===alias&&(e.saleIds||[]).includes(String(x.saleId||x.sale_id||x.saleID)));
  const nativeOne=native.length===1?native[0]:null;
  return [dt(e.eventDate),e.orderId||'待匹配',e.sku||'—',e.kindLabel,g==null?null:Math.max(0,g),g==null?null:Math.max(0,-g),g,num(a.seller_proceeds),num(a.product_cost),num(a.agency_accrued),num(a.promotion_accrued),null,num(a.other_cost),nativeOne?num(nativeOne.grossRub):null,nativeOne?num(nativeOne.proceedsRub):null,e.amountBasis,e.mappingStatus,e.srid||'—',e.eventId,compactEvidence(e.linkEvidence),num(e.actualAmounts.gross_sales),num(e.actualAmounts.seller_proceeds),num(e.estimateAmounts.gross_sales),num(e.estimateAmounts.seller_proceeds),num(a.logistics_cost),num(a.shared_pool_return_cost),num(a.return_reserve_accrued),num(a.separate_return_cost),num(a.shared_pool_accrued)];
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
const metricRecord=(a,p,k,cumulative=false,actual=false)=>periodAccount(a,p,actual)[cumulative?'cumulative':'period'].metrics[k];
const expected=(a,p,k,cumulative=false,actual=false)=>n(metricRecord(a,p,k,cumulative,actual)?.known_subtotal);
const known=(a,p,k,cumulative=false,actual=false)=>metricRecord(a,p,k,cumulative,actual)?.complete;
const opened=a=>v.stores[a].openedAt;
function excess(a,end){const ss=k=>sum(a,k,opened(a),end);return '(MAX(0,'+ss('shared_pool_return_cost')+'-MAX(0,'+ss('shared_pool_accrued')+'))+MAX(0,'+ss('separate_return_cost')+'-MAX(0,'+ss('return_reserve_accrued')+')))';}
const rowNames={5:'营业额－合计（未扣退单）',6:'营业额－退单（退款金额）',7:'营业额－净额（扣除退单）',8:'代运营费－合计（净计提）',9:'代运营费－已到账（已登记）',10:'代运营费－待支付（期末）',11:'广告费－预算合计（净计提）',12:'广告费－自付合计已使用',13:'广告费－老店已使用',14:'广告费－新店已使用',15:'广告费－老店预算余额（期末·暂估）',16:'广告费－新店预算余额（期末·暂估）',17:'广告费－预算余额合计（期末·暂估）',18:'平台结算应收（不等于现金回款）',19:'采购成本－净额（已扣退回可售）',20:'物流费用－实际合计',21:'其他费用－已知部分',22:'利润－按已用广告计算（暂估）',23:'利润－预留广告预算后（暂估）',24:'店铺回款－账户实际入账',25:'提现－有效申请（扣除撤销）',26:'提现－已到账（已知部分）',27:'提现－处理中（期末·已登记）',28:'提现－未申请可提（期末）',29:'平台促销金－消耗（不计成本）',30:'退货风险－独立净计提',31:'退货损失－储备超额变动',32:'广告预算－正式已知余额（期末）'};
const metricExplanations = {
 "store": {
  "5": "当期已有的正数销售额合计，未减退款；包含已批准的暂估销售额。",
  "6": "当期负数销售额取绝对值合计；以前卖出的订单在本期退款，也在本期冲回。",
  "7": "当期未扣退单销售额－当期退款金额，即第5行－第6行；不是账户到账金额。",
  "8": "当期按原订单政策计提的代运营费－本期退单冲回的原计提；不是实际收到的钱。",
  "9": "当期登记收到的代运营费合计，按实收日期归属；不从订单计提额推算。",
  "10": "截至该列期末：累计代运营净计提－累计已登记实收。实收历史缺失时显示待补；计提可含暂估。",
  "11": "当期按原订单政策计提的广告预算－本期退单冲回的原计提；这是预算，不是已花费用。",
  "12": "当期卖家自付的广告实际消耗；不含平台促销金，也不把活动充值算作消耗。",
  "13": "当期平台承担的促销金消耗，不计卖家成本、不扣卖家预算；与本表第29行相同。",
  "14": "当期由历史共享广告／退货预算承担的实际退货或取消运费；已从广告预算余额中扣除。",
  "15": "期末累计广告净计提－累计自付广告消耗－累计共享预算退货运费。含暂估计提，负数保留。",
  "16": "期末正式已知广告净计提－累计自付广告消耗－累计共享预算退货运费；不使用暂估计提。",
  "17": "当期广告净计提－当期自付广告消耗－当期共享预算退货运费；这是余额变化，不是期末余额。",
  "18": "当期订单应收卖家金额的净额，退款同步冲回；可含暂估，不等于账户实际到账。",
  "19": "当期销售的原采购成本－本期确认退回且可再售商品的原成本；返库不是新增收入。",
  "20": "当期独立物流费＋共享预算承担的退货运费＋独立退货预算承担的运费；实际运费只计一次。",
  "21": "当期已有来源的其他费用合计；数据不全且已知部分为零时显示待补，不能认定没有费用。",
  "22": "应收－采购成本－代运营－自付广告实耗－实际物流－已知其他费。不再扣广告预算；缺项未计，非最终利润。",
  "23": "应收减采购、代运营、广告预算、独立退货风险计提、独立物流、其他费及退货超储备变动；不另扣实耗广告。",
  "24": "当期有完整入账来源的账户回款合计；不以订单应收或余额差代替。回款流水不完整时显示待补。",
  "25": "当期提现申请－当期撤销或更正的申请。历史重复记录按原申请期更正；提交申请不等于到账。",
  "26": "按到账日汇总当期已确认的提现到账；未确认的不算到账。历史不全且已知为零时显示待补。",
  "27": "截至该列期末：累计申请－累计到账－累计撤销；只反映已登记的处理中提现金额。",
  "28": "期末累计账户入账－累计有效提现申请；是账簿未申请金额，非平台实时可提现额度。缺回款流水则待补。",
  "29": "当期平台促销金使用额，与本表第13行相同；不与自付广告费相加，不作为成本重复扣除。",
  "30": "当期原订单独立退货风险预算净计提，退单冲回原计提；历史共享预算不重复计入此行。",
  "31": "共享池、独立退货池分别比较累计运费与累计计提（负计提按零）；超额部分的期末－期初差，可为负数。",
  "32": "与本表第16行相同：正式已知广告计提减已发生的自付广告及共享预算退货运费；不含暂估计提。"
 },
 "consolidated": {
  "5": "两店当期已有的正数销售额相加，尚未减退款；包含已批准的暂估。",
  "6": "两店当期退款冲回金额相加；原销售发生在以前的退单，也归入本期退款。",
  "7": "两店净销售额相加，即合并第5行－第6行；不是账户回款。",
  "8": "两店当期代运营净计提相加，均已扣除本期退单冲回的原计提。",
  "9": "两店当期已登记的代运营实收相加，按各笔实际收到日期归属。",
  "10": "两店各自的期末累计净计提减累计实收，再相加；任一店实收历史缺失时显示待补。",
  "11": "两店当期广告预算净计提相加，已扣退单冲回；不是实际已花广告费。",
  "12": "两店当期自付广告实际消耗相加；平台促销金和活动充值不计入。",
  "13": "仅列老店当期自付广告消耗，取老店汇总第12行；已包含在本页第12行中。",
  "14": "仅列新店当期自付广告消耗，取新店汇总第12行；已包含在本页第12行中。",
  "15": "老店截至该列期末的预算余额，取老店汇总第15行；含暂估计提，非平台广告账户余额。",
  "16": "新店截至该列期末的预算余额，取新店汇总第15行；含暂估计提，非平台广告账户余额。",
  "17": "两店期末预算余额相加，即本页第15行＋第16行；不是本期预算变化，不能与分店余额再相加。",
  "18": "两店当期订单结算应收净额相加；包含退款冲回和批准暂估，不代表现金已入账。",
  "19": "两店当期采购成本净额相加，已减本期退回且可再次销售商品的原成本。",
  "20": "两店当期实际物流合计相加，包含独立物流、共享预算及独立退货预算承担的运费。",
  "21": "只汇总两店已有数值的其他费用；待补部分尚未计入。两店均缺数据且无已知金额时显示待补。",
  "22": "两店按实际自付广告计算的利润相加；均扣采购、代运营、实际物流及已知其他费，不再扣预算计提。缺项未计。",
  "23": "两店预留广告及退货预算后的利润相加；退货超储备损失各店独立计算，不拿另一店储备抵消。",
  "24": "两店当期账户实际入账相加；任一店回款流水不完整即显示待补，不拿应收或余额差补数。",
  "25": "两店当期有效提现申请相加，已扣各店撤销和历史重复更正；申请与到账分开统计。",
  "26": "两店当期已确认提现到账相加，仅为已知部分；两店均缺到账历史且无已知金额时显示待补。",
  "27": "两店各自期末的累计申请－累计到账－累计撤销，再相加；表示已登记处理中金额。",
  "28": "两店各自累计账户入账减有效累计申请后相加；缺任一店回款流水即待补，非平台实时可提现额度。",
  "29": "两店当期由平台承担的促销金消耗相加；不计卖家成本，不从卖家广告预算再扣。",
  "30": "两店当期独立退货风险预算净计提相加；历史共享预算不重复计入。",
  "31": "先逐店、逐退货资金池算累计运费超出累计计提的部分，再汇总各店期末－期初差；不跨店抵消。",
  "32": "两店正式已知广告预算余额相加；不含暂估计提，不能与第17行暂估余额再相加。"
 }
};
for(const alias of [...aliases,'ALL']){
 const s=sheets[alias==='ALL'?'汇总报告':labels[alias]+'汇总'];base(s,'G',34);widths(s,{A:43,B:23,C:23,D:23,E:23,F:3,G:90});s.freezePanes.freezeRows(4);
 header(s,1,[alias==='ALL'?'财务指标｜两店合并':('财务指标｜'+labels[alias]),...periods.map(p=>p.label)]);
 value(s,'A2','周期开始');value(s,'A3','周期结束');value(s,'A4','单位：人民币元｜含暂估参考');
 periods.forEach((p,i)=>{value(s,col(i+2)+'2',dt(p.start));value(s,col(i+2)+'3',dt(p.end));s.getRange(col(i+2)+'2:'+col(i+2)+'3').setNumberFormat('yyyy-mm-dd');});
 for(const [r,label]of Object.entries(rowNames))value(s,'A'+r,label);
 value(s,'A28','提现－账簿未申请余额（期末）');
 if(alias!=='ALL'){
  value(s,'A13','广告费－平台促销金消耗（不计成本）');value(s,'A14','退货物流－历史共享预算已扣');value(s,'A15','广告费－预算可用余额（期末·暂估）');value(s,'A16','广告费－正式已知余额（期末）');value(s,'A17','广告预算－本期余额变化');
 }
 value(s,'G1','计算说明（逐行对应左侧金额）');s.getRange('G1').format.font.bold=true;
 value(s,'G2','当期＝该列日期内发生额；期末＝截至该列结束日的累计余额。四列有重叠，不能相加。');
 value(s,'G3','人民币暂估仅供参考，不据此付款或充值。正式金额及暂估差额在流水右侧保留。');
 value(s,'G4','缺少来源标为待补，不等于0。财务发生日期按莫斯科时区。');
 for(const [r,text]of Object.entries(metricExplanations[alias==='ALL'?'consolidated':'store']))value(s,'G'+r,text);
 s.getRange('G2:G32').format={font:{name:'Arial',size:10,color:C.muted},wrapText:true,verticalAlignment:'center'};
 for(const [r,text]of Object.entries(metricExplanations[alias==='ALL'?'consolidated':'store']))if(text.length>52)s.getRange('A'+r+':G'+r).format.rowHeight=38;
 for(const r of [7,10,17,22,23,27,32])s.getRange('A'+r+':E'+r).format={fill:[22,23].includes(r)?C.amber:C.band,font:{name:'Arial',size:10,bold:true,color:C.ink}};
 for(const p of periods){
  const cc=col(periods.indexOf(p)+2), put=(r,x)=>formula(s,cc+r,x);
  if(alias==='ALL'){
   for(let r=5;r<=32;r++){
    if([13,14,15,16].includes(r)){const a=aliases.find(key=>labels[key]===(r===13||r===15?'老店':'新店'));if(a){put(r,ref(labels[a]+'汇总',cc+([13,14].includes(r)?12:15)));checks.push({sheet:s.name,cell:cc+r,expected:expected(a,p,[13,14].includes(r)?'cash_ad_spend':'advertising_available',[15,16].includes(r))});}else value(s,cc+r,'—');continue;}
    if(r===17){put(r,aliases.map(a=>ref(labels[a]+'汇总',cc+'15')).join('+'));continue;}
    if([24,28].includes(r)&&aliases.some(a=>!known(a,p,r===24?'receipts_received':'withdrawal_available',r===28))){value(s,cc+r,'待补回款流水');continue;}
    if(r===10&&aliases.some(a=>!known(a,p,'agency_paid',true))){value(s,cc+r,'实收历史待补');continue;}
    if(r===26&&aliases.every(a=>!known(a,p,'withdrawal_completed')&&expected(a,p,'withdrawal_completed')===0)){value(s,cc+r,'到账历史待补');continue;}
    if(r===21&&aliases.every(a=>!known(a,p,'other_cost')&&expected(a,p,'other_cost')===0)){value(s,cc+r,'待补');continue;}
    const pieces=aliases.map(a=>ref(labels[a]+'汇总',cc+r));put(r,'SUM('+pieces.join(',')+')');
   }continue;
  }
  const a=alias, ss=k=>sum(a,k,p.start,p.end), cs=k=>sum(a,k,opened(a),p.end);
  put(5,sum(a,'gross_sales',p.start,p.end,{sign:'>0'}));put(6,'-'+sum(a,'gross_sales',p.start,p.end,{sign:'<0'}));put(7,cc+'5-'+cc+'6');
  put(8,ss('agency_accrued'));put(9,ss('agency_paid'));
  if(known(a,p,'agency_paid',true))put(10,cs('agency_accrued')+'-'+cs('agency_paid'));else value(s,cc+'10','实收历史待补');
  put(11,ss('promotion_accrued'));put(12,ss('cash_ad_spend'));put(13,ss('promo_credit_spend'));put(14,ss('shared_pool_return_cost'));
  put(15,cs('promotion_accrued')+'-'+cs('cash_ad_spend')+'-'+cs('shared_pool_return_cost'));
  put(16,sum(a,'promotion_accrued',opened(a),p.end,{actual:true})+'-'+cs('cash_ad_spend')+'-'+cs('shared_pool_return_cost'));
  put(17,ss('promotion_accrued')+'-'+ss('cash_ad_spend')+'-'+ss('shared_pool_return_cost'));
  put(18,ss('seller_proceeds'));put(19,ss('product_cost'));put(20,ss('logistics_cost')+'+'+ss('shared_pool_return_cost')+'+'+ss('separate_return_cost'));
  if(!known(a,p,'other_cost')&&expected(a,p,'other_cost')===0)value(s,cc+'21','待补');else put(21,ss('other_cost'));
  put(22,cc+'18-'+cc+'19-'+cc+'8-'+cc+'12-'+cc+'20-SUM('+cc+'21)');
  put(23,cc+'18-'+cc+'19-'+cc+'8-'+cc+'11-'+ss('return_reserve_accrued')+'-'+ss('logistics_cost')+'-'+ss('other_cost')+'-('+excess(a,p.end)+'-'+excess(a,prev(p.start))+')');
  if(known(a,p,'receipts_received'))put(24,ss('receipts_received'));else value(s,cc+'24','待补回款流水');
  put(25,ss('withdrawal_requested')+'-'+ss('withdrawal_cancelled'));
  if(!known(a,p,'withdrawal_completed')&&expected(a,p,'withdrawal_completed')===0)value(s,cc+'26','到账历史待补');else put(26,ss('withdrawal_completed'));
  put(27,cs('withdrawal_requested')+'-'+cs('withdrawal_completed')+'-'+cs('withdrawal_cancelled'));
  if(known(a,p,'withdrawal_available',true))put(28,cs('receipts_received')+'-'+cs('withdrawal_requested')+'+'+cs('withdrawal_cancelled'));else value(s,cc+'28','待补回款流水');
  put(29,ss('promo_credit_spend'));put(30,ss('return_reserve_accrued'));put(31,excess(a,p.end)+'-'+excess(a,prev(p.start)));put(32,cc+'16');
  for(const [r,k,cumulative,actual]of [[7,'gross_sales'],[8,'agency_accrued'],[9,'agency_paid'],[11,'promotion_accrued'],[12,'cash_ad_spend'],[15,'advertising_available',true],[16,'advertising_available',true,true],[18,'seller_proceeds'],[19,'product_cost'],[22,'operating_contribution'],[23,'profit'],[27,'withdrawal_pending',true],[29,'promo_credit_spend'],[30,'return_reserve_accrued'],[31,'excess_return_loss']]){
   const record=metricRecord(a,p,k,!!cumulative,!!actual);if(record)checks.push({sheet:s.name,cell:cc+r,expected:expected(a,p,k,!!cumulative,!!actual)});
  }
 }
 s.getRange('B5:E32').setNumberFormat(money);s.getRange('B1:E32').format.horizontalAlignment='right';s.getRange('A1:E32').format.borders={bottom:{style:'hair',color:C.line}};
}
// Reconciliation is terminal QA, never a source for reported money.
const recon=sheets['周期对账'], reconMetrics=['gross_sales','seller_proceeds','product_cost','agency_accrued','promotion_accrued','logistics_cost','shared_pool_return_cost','other_cost','cash_ad_spend','agency_paid','withdrawal_requested','withdrawal_completed'];
base(recon,'I',120);widths(recon,{A:10,B:24,C:23,D:18,E:18,F:18,G:16,H:17,I:22});
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
const previewRanges=Object.fromEntries(names.map(name=>[name,name.endsWith('汇总')||name==='汇总报告'?'A1:G32':name==='周期对账'?'A1:I22':name.endsWith('订单')?'A1:AC15':'A1:AJ15']));
const formulaError=/^#(?:REF!|DIV\/0!|VALUE!|NAME\?|N\/A|NUM!|NULL!|SPILL!|CALC!)$/;
function validate(book){
 const failures=[];for(const x of checks){const actual=book.worksheets.getItem(x.sheet).getRange(x.cell).values[0][0];if(typeof actual!=='number'||Math.abs(actual-x.expected)>0.011)failures.push({...x,actual});}
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
 return {version:'owner-dashboard-layout-v1',passed:true,flowMaxColumn:'AJ',orderMaxColumn:'AC',noHelperColumnsBeyond:true,readableCompactEvidence:true,compactEvidenceMaxCharacters:MAX_COMPACT_EVIDENCE_CHARS,previewRangesFullWidth:true,sheets:sheetColumns,evidence,previewRanges};
}
wb.recalculate();validate(wb);validateLayout(wb);
// Test a source edit affects consolidated expense AND budget, then restore.
let recalcVerified=false;
for(const a of aliases){const i=sourceRows[a].findIndex(e=>n(e.amounts.cash_ad_spend)>0);if(i<0)continue;
 const f=sheets[labels[a]+'流水'],cell='K'+(7+i),old=f.getRange(cell).values[0][0],summary=sheets['汇总报告'];
 const expense=summary.getRange('B12').values[0][0],balance=summary.getRange('B17').values[0][0];
 value(f,cell,old+1);wb.recalculate();
 if(Math.abs(summary.getRange('B12').values[0][0]-expense-1)>0.001||Math.abs(summary.getRange('B17').values[0][0]-balance+1)>0.001)throw new Error('Source edit did not propagate');
 value(f,cell,old);wb.recalculate();validate(wb);recalcVerified=true;break;
}
await fs.mkdir(path.dirname(output),{recursive:true});const previewDir=path.join(path.dirname(output),'previews');await fs.mkdir(previewDir,{recursive:true});
const rendered=[];
for(const name of names){const preview=await wb.render({sheetName:name,range:previewRanges[name],scale:1.25,format:'png'});const target=path.join(previewDir,name+'.png');await fs.writeFile(target,new Uint8Array(await preview.arrayBuffer()));rendered.push(target);}
const file=await SpreadsheetFile.exportXlsx(wb);await file.save(output);
const saved=await SpreadsheetFile.importXlsx(await FileBlob.load(output));saved.recalculate();validate(saved);const layoutContract=validateLayout(saved);
await fs.writeFile(verification,JSON.stringify({templateVersion:m.templateVersion,formulaErrorCount:0,totalsVerified:true,verifiedMetricCount:checks.length,reconciliationRows:reconChecks.length,reconciliationDifference:0,recalcVerified,layoutContract,mapping:Object.fromEntries(aliases.map(a=>[a,v.stores[a].mappingSummary])),renderedSheets:rendered,output,sourceCutoff:m.sourceCutoff},null,2));
console.log(JSON.stringify({output,sheets:names.length,formulaErrorCount:0,verifiedMetricCount:checks.length,reconciliationRows:reconChecks.length,recalcVerified,layoutContractPassed:layoutContract.passed}));
