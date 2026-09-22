import fs from 'node:fs';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import ExcelJS from 'exceljs';
const idPattern=/^[a-f0-9-]{36}$/;
const text=(v,max=2000)=>String(v??'').trim().slice(0,max);
export const platform=v=>/淘宝|天猫|淘系/.test(v)?'淘系':text(v,40);
const url=v=>{try{const u=new URL(v);return ['https:','http:'].includes(u.protocol)&&!u.username&&!u.password?u.href:'';}catch{return '';}};
const number=v=>v===''||v==null?null:Number(v);
const positive=v=>typeof v==='number'&&Number.isFinite(v)&&v>0;
export const signature=r=>JSON.stringify(['originalName','category','brand','model','spec','dimensions','material','pack','quantity','unit','custom'].map(k=>r[k]??''));
const time=v=>{const n=Date.parse(v);return Number.isFinite(n)?new Date(n).toISOString():'';};
const dims=s=>[...String(s).matchAll(/(\d+(?:\.\d+)?)\s*[*×xX]\s*(\d+(?:\.\d+)?)/g)].map(m=>m[1]+'x'+m[2]);
export function conflicts(r,q){
 const s=(q.title+' '+q.spec+' '+q.sku).normalize('NFKC').toLowerCase();const out=[];
 if(r.model&&!s.match(new RegExp('(?:^|[^a-z0-9])'+r.model.toLowerCase().replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+'(?:$|[^a-z0-9])')))out.push('未找到需求型号');
 if(/原装/.test(r.originalName+' '+r.spec)&&/兼容|适用|代用|替代/.test(s))out.push('原装需求不能采用兼容候选');
 const requested=dims(r.dimensions||r.spec),offered=dims(q.spec+' '+q.title);if(requested.length&&offered.length&&!requested.every(d=>offered.includes(d)))out.push('尺寸不一致');
 if(r.category&&!s.includes(r.category.toLowerCase()))out.push('候选未明确包含需求类别，请核实类别与供货规格');
 return out;
}
export function quoteGaps(r,q,root,now=Date.now()){
 const gaps=[];
 if(!r.specConfirmed||!r.originalName||!r.unit||!positive(r.quantity)||!(r.spec||r.model||r.dimensions))gaps.push('需求规格/数量/单位未确认');
 if(q.match!=='exact'||q.targetSignature!==signature(r))gaps.push('同款待核对');
 gaps.push(...conflicts(r,q));
 if(!q.spec||!q.saleUnit||!q.sku||!q.pack||!q.unitConfirmed||q.saleUnit!==r.unit)gaps.push('SKU、包装或售卖单位待核对');
 if(!q.conditionsConfirmed||!q.conditions||!q.freight)gaps.push('价格条件/运费待确认');
 if(!positive(q.price)||!q.store||!url(q.url)||!q.platform)gaps.push('价格/店铺/来源缺失');
 const domains={'京东':/(^|\.)jd\.com$/,'淘系':/(^|\.)(taobao|tmall)\.com$/,'苏宁':/(^|\.)suning\.com$/,'拼多多':/(^|\.)(yangkeduo|pinduoduo)\.com$/};if(domains[q.platform]&&(!url(q.url)||!domains[q.platform].test(new URL(q.url).hostname)))gaps.push('平台与原页链接不一致，短链须换成商品原页');
 const age=now-Date.parse(q.fetchedAt);if(!Number.isFinite(age)||age<0)gaps.push('实际取价时间未知或在未来');
 if(!q.marketAt)gaps.push('行情时间未知，参考价待核');
 else if(Date.parse(q.marketAt)>now)gaps.push('行情时间在未来，待核');
 if(q.validUntil&&Date.parse(q.validUntil)<now)gaps.push('价格有效期已过，待重新核对');
 const evidence=q.evidence||[];
 const valid=evidence.some(e=>{if(e.kind!=='origin'||e.sourceUrl!==q.url||!idPattern.test(e.id)||Math.abs(Date.parse(e.capturedAt)-Date.parse(q.fetchedAt))>600000||!Number.isFinite(Date.parse(e.capturedAt)))return false;try{const m=JSON.parse(fs.readFileSync(path.join(root,'files',e.id+'.json'),'utf8'));return m.purpose==='origin'&&['image/png','image/jpeg'].includes(m.mime)&&fs.existsSync(path.join(root,'files',e.id));}catch{return false;}});
 if(!valid)gaps.push('缺对应商品原页截图');return [...new Set(gaps)];
}
export function assess(r,root,now=Date.now()){
 const quotes=r.quotes.map(q=>({...q,gaps:quoteGaps(r,q,root,now)}));const seen=new Set();const valid=quotes.filter(q=>{const k=q.platform+'|'+q.url+'|'+q.sku;if(q.gaps.length||seen.has(k))return false;seen.add(k);return true;});
 const chosen=quotes.filter(q=>r.selected[q.platform]===q.id);const gaps=[];
 for(const p of r.requiredPlatforms){const q=chosen.find(q=>q.platform===p);if(!q||q.gaps.length)gaps.push(p+'报价未齐或待重新核对');}
 if(r.requiredPlatforms.length<3)gaps.push('至少需要三个独立平台');
 const origins=chosen.filter(q=>!q.gaps.length&&r.requiredPlatforms.includes(q.platform)).map(q=>new URL(q.url).hostname.replace(/^www\./,''));if(new Set(origins).size<Math.min(3,r.requiredPlatforms.length))gaps.push('独立来源不足三个');
 if(!positive(r.finalPrice))gaps.push('最终报价未填写');if(!r.finalNote)gaps.push('最终报价说明未填写');
 const prices=valid.map(q=>q.price).sort((a,b)=>a-b),n=prices.length;
 return {quotes,gaps,ready:!gaps.length,referenceCount:quotes.filter(q=>positive(q.price)).length,missingPlatforms:r.requiredPlatforms.filter(p=>!chosen.some(q=>q.platform===p&&!q.gaps.length)),statistics:{count:n,min:n?prices[0]:null,max:n?prices[n-1]:null,median:n?(prices[Math.floor((n-1)/2)]+prices[Math.floor(n/2)])/2:null}};
}
export class QuoteWorkflow{
 constructor(root){this.root=root;this.dir=path.join(root,'tasks','workflow');fs.mkdirSync(this.dir,{recursive:true});this.file=path.join(this.dir,'current.json');}
 read(){return fs.existsSync(this.file)?JSON.parse(fs.readFileSync(this.file,'utf8')):{revision:0,title:'商品报价（待业务审核）',rows:[],updatedAt:null};}
 importRows(input){
  const old=this.read();if(input.revision!==old.revision){const e=Error('清单已在其他窗口更新，请重新读取');e.status=409;throw e;}
  if(!text(input.sourceDocumentId)||!Array.isArray(input.rows)||!input.rows.length||input.rows.length>200)throw Error('需要来源清单ID与1至200行');
  const rows=structuredClone(old.rows),seen=new Set();
  for(const raw of input.rows){
   const sourceRowId=text(raw.sourceRowId||raw.id),sourceDocumentId=text(input.sourceDocumentId);
   if(!sourceRowId||seen.has(sourceRowId))throw Error('来源行ID缺失或重复');seen.add(sourceRowId);
   const snapshot=raw.originalRow||raw;
   if(JSON.stringify(snapshot).length>30000)throw Error('原行字段过长，请保留业务字段');
   const previous=rows.find(r=>r.sourceDocumentId===sourceDocumentId&&r.sourceRowId===sourceRowId);
   const fields={originalName:raw.originalName??raw.name??'',spec:raw.spec??'',model:raw.model??'',brand:raw.brand??'',custom:!!raw.custom,unit:raw.unit??'',quantity:raw.quantity??null};
   if(!fields.originalName)throw Error('来源行缺少商品名称');
   if(previous){if(JSON.stringify(previous.originalRow)!==JSON.stringify(snapshot))throw Error('来源行已变化，请在原报价行核对后更新，不能覆盖已有人工补价');continue;}
   rows.push({id:randomUUID(),...fields,sourceDocumentId,sourceRowId,originalRow:snapshot,source:text(input.sourceLabel||sourceDocumentId),query:raw.query||fields.originalName,quotes:[],selected:{},requiredPlatforms:raw.requiredPlatforms||['京东','淘系','苏宁']});
  }
  return this.save({...old,rows});
 }
 view(){const d=this.read();return {...d,rows:d.rows.map(r=>({...r,assessment:assess(r,this.root)}))};}
 save(input){const old=this.read();if(input.revision!==old.revision){const e=Error('清单已在其他窗口更新，请重新读取；本页修改尚未保存');e.status=409;throw e;}
  if(!Array.isArray(input.rows)||input.rows.length>200)throw Error('清单最多200行');const seen=new Set();const allQuotes=new Set();
  const rows=input.rows.map(raw=>{
   if(!idPattern.test(raw.id)||seen.has(raw.id))throw Error('需求行编号无效或重复');seen.add(raw.id);
   const r={id:raw.id};for(const k of ['originalName','category','brand','model','spec','dimensions','material','pack','unit','query','source','finalNote'])r[k]=text(raw[k]);
   r.quantity=number(raw.quantity);r.finalPrice=number(raw.finalPrice);if([r.quantity,r.finalPrice].some(v=>v!==null&&!positive(v)))throw Error('数量和报价必须大于0');
   r.custom=!!raw.custom;r.specConfirmed=!!raw.specConfirmed;r.requiredPlatforms=[...new Set((raw.requiredPlatforms||['京东','淘系','苏宁']).map(platform).filter(Boolean))];if(r.requiredPlatforms.length>10)throw Error('平台过多');
   r.selected={};r.status=text(raw.status,1000);r.quotes=[];
   const previous=old.rows.find(x=>x.id===r.id),changed=previous&&signature(previous)!==signature(r);r.originalInput=previous?.originalInput||r.originalName;
   r.sourceDocumentId=text(previous?.sourceDocumentId||raw.sourceDocumentId);r.sourceRowId=text(previous?.sourceRowId||raw.sourceRowId);
   r.originalRow=previous?.originalRow||raw.originalRow||null;if(r.originalRow&&JSON.stringify(r.originalRow).length>30000)throw Error('原行字段过长');
   if(changed)r.specConfirmed=false;
   if(!Array.isArray(raw.quotes)||raw.quotes.length>100)throw Error('每行最多100候选');
   r.quotes=raw.quotes.map(a=>{
    if(!idPattern.test(a.id)||allQuotes.has(a.id))throw Error('报价编号无效或重复');allQuotes.add(a.id);
    const q={id:a.id};for(const k of ['title','store','spec','sku','saleUnit','pack','conditions','freight','driver','note','candidateId','lookupPlatform'])q[k]=text(a[k]);q.platform=platform(a.platform);q.url=url(a.url);q.price=number(a.price);if(q.price!==null&&!positive(q.price))throw Error('候选价格必须为正数');q.fetchedAt=time(a.fetchedAt);q.collectedAt=time(a.collectedAt);q.marketAt=time(a.marketAt);q.validUntil=time(a.validUntil);q.manualEdited=!!a.manualEdited;q.manualEditedAt=time(a.manualEditedAt);q.providerSnapshot=a.providerSnapshot||null;if(q.providerSnapshot&&JSON.stringify(q.providerSnapshot).length>30000)throw Error('原始候选过长');q.sourceKind=['manual','search','detail','aggregate'].includes(a.sourceKind)?a.sourceKind:'search';
    q.match=['pending','exact','mismatch'].includes(a.match)?a.match:'pending';if(changed)q.match='pending';q.targetSignature=q.match==='exact'?signature(r):'';q.unitConfirmed=!!a.unitConfirmed;q.conditionsConfirmed=!!a.conditionsConfirmed;
    q.evidence=(a.evidence||[]).slice(0,8).map(e=>{if(!idPattern.test(e.id)||!fs.existsSync(path.join(this.root,'files',e.id)))throw Error('截图不存在或不属于当前账号');return {id:e.id,kind:e.kind==='origin'?'origin':'input',name:text(e.name,180),sourceUrl:url(e.sourceUrl),capturedAt:time(e.capturedAt),url:'/api/files/'+e.id};});
    if(q.match==='exact'&&conflicts(r,q).length)throw Error(conflicts(r,q).join('；'));
    return q;
   });
   for(const [p,id] of Object.entries(raw.selected||{})){const group=platform(p);if(r.quotes.some(q=>q.id===id&&q.platform===group))r.selected[group]=id;}
   return r;
  });
  const doc={revision:old.revision+1,title:text(input.title,150)||'报价（待业务审核）',rows,updatedAt:new Date().toISOString()};const tmp=this.file+'.'+randomUUID()+'.tmp';fs.writeFileSync(tmp,JSON.stringify(doc));fs.renameSync(tmp,this.file);return this.view();
 }
 async export({revision,mode='draft'}={}){
  const d=this.read();if(d.revision!==revision)throw Error('清单已变化，请重新读取再导出');if(!d.rows.length)throw Error('请先添加需求');if(!['draft','reviewed'].includes(mode))throw Error('导出模式无效');
  const assessed=d.rows.map(r=>({...r,assessment:assess(r,this.root)}));const rows=mode==='reviewed'?assessed.filter(r=>r.assessment.ready):assessed;if(!rows.length)throw Error('尚无满足三方依据的行，请先补齐或导出草稿');
  const w=new ExcelJS.Workbook();w.creator='贵昌报价工作台';const names=['需求清单','三方对照','最终报价','截图凭证','原行字段'];const sheets=names.map(name=>w.addWorksheet(name));const safe=v=>{const s=String(v??'');return /^[=+\-@\t\r]/.test(s)?"'"+s:s;};
  for(const s of sheets){s.addRow([safe(d.title),mode==='draft'?'待补齐草稿':'已核对材料 · 待业务审核',new Date().toISOString()]);s.views=[{state:'frozen',ySplit:2}];s.columns=Array.from({length:24},()=>({width:22}));}
  const [input,compare,final,proof,original]=sheets;original.addRow(['需求行ID','来源清单ID','来源行ID','原行字段JSON']);
  input.addRow(['原始品名','类别','品牌','型号','规格','尺寸','材质','包装','需求数量','单位','实际检索词','来源','缺口','需求行ID','来源清单ID','来源行ID']);
  compare.addRow(['需求','平台','店铺','商品','所选SKU','供货规格','售卖单位','包装','单价','条件/运费','来源链接','取价时间','人工/自动','核对结果','需求行ID','报价ID','选作依据','采集时间','行情时间','有效期至','人工修改','人工修改时间','采集原值JSON','核对或差异说明']);
  final.addRow(['序号','物品名称','需求参数/型号','单位','数量','最终单价','总价','报价说明','状态','缺口','需求行ID']);
  proof.addRow(['需求','平台','报价ID','来源链接','截图时间','截图','需求行ID']);
  for(const [i,r] of rows.entries()){
   original.addRow([r.id,r.sourceDocumentId,r.sourceRowId,JSON.stringify(r.originalRow)].map(safe));
   input.addRow([r.originalName,r.category,r.brand,r.model,r.spec,r.dimensions,r.material,r.pack,r.quantity,r.unit,r.query,r.source,r.assessment.gaps.join('；'),r.id,r.sourceDocumentId,r.sourceRowId].map(v=>typeof v==='number'?v:safe(v)));
   const chosen=r.assessment.quotes.filter(q=>r.selected[q.platform]===q.id);
   for(const q of r.assessment.quotes){compare.addRow([r.originalName,q.platform,q.store,q.title,q.sku,q.spec,q.saleUnit,q.pack,q.price,q.conditions+'；运费：'+q.freight,q.url,q.fetchedAt,q.sourceKind==='manual'?'人工录入':q.driver,q.gaps.length?q.gaps.join('；'):'已核对',r.id,q.id,r.selected[q.platform]===q.id?'是':'否',q.collectedAt,q.marketAt,q.validUntil,q.manualEdited?'是':'否',q.manualEditedAt,JSON.stringify(q.providerSnapshot),q.note].map(v=>typeof v==='number'?v:safe(v)));if(q.url)compare.getRow(compare.rowCount).getCell(11).value={text:q.url,hyperlink:q.url};}
   for(const p of r.requiredPlatforms.filter(p=>!chosen.some(q=>q.platform===p)))compare.addRow([safe(r.originalName),safe(p),'待补齐',null,null,null,null,null,null,null,null,null,null,'缺少已选依据',r.id]);
   const valid=chosen.filter(q=>!q.gaps.length),canTotal=r.specConfirmed&&positive(r.quantity)&&positive(r.finalPrice)&&valid.length&&valid.every(q=>q.saleUnit===r.unit&&q.unitConfirmed);
   final.addRow([i+1,safe(r.originalName),safe([r.model,r.spec,r.dimensions,r.pack].filter(Boolean).join('；')),safe(r.unit),r.quantity,r.finalPrice,canTotal?Math.round(r.quantity*r.finalPrice*100)/100:null,safe(r.finalNote),r.assessment.ready?'已核对 · 待业务审核':'待补齐草稿',safe(r.assessment.gaps.join('；')),r.id]);
   for(const q of r.assessment.quotes)for(const e of q.evidence){const n=proof.rowCount+1;proof.addRow([safe(r.originalName),safe(q.platform),q.id,safe(e.sourceUrl),safe(e.capturedAt),'',r.id]);if(e.sourceUrl)proof.getRow(n).getCell(4).value={text:e.sourceUrl,hyperlink:e.sourceUrl};try{const m=JSON.parse(fs.readFileSync(path.join(this.root,'files',e.id+'.json'),'utf8'));if(!['image/png','image/jpeg'].includes(m.mime))continue;const id=w.addImage({buffer:fs.readFileSync(path.join(this.root,'files',e.id)),extension:m.mime==='image/png'?'png':'jpeg'});proof.addImage(id,{tl:{col:5,row:n-1},ext:{width:400,height:240}});proof.getRow(n).height=185;}catch{throw Error('截图读取失败，请检查附件后再导出');}}
  }
  for(const s of sheets)s.eachRow((row,n)=>{row.font={name:'Microsoft YaHei',size:11,bold:n<=2};row.alignment={vertical:'top',wrapText:true};if(n===2)row.fill={type:'pattern',pattern:'solid',fgColor:{argb:'FFE8F0F2'}};});
  return Buffer.from(await w.xlsx.writeBuffer());
 }
}
