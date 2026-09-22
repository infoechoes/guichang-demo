import iconv from 'iconv-lite';
import {extractProduct} from './image-recognition.mjs';
const normalize=s=>String(s||'').normalize('NFKC').toLowerCase().replace(/\s+/g,'');
// Preserve Chinese phrases while separating adjacent Latin identifiers (PDA充电线).
const queryTerms=s=>s.normalize('NFKC').replace(/([A-Za-z0-9])([\u3400-\u9fff])/g,'$1 $2').replace(/([\u3400-\u9fff])([A-Za-z0-9])/g,'$1 $2').trim().split(/\s+/).filter(Boolean);
export function searchUrl(keyword){
 const value=keyword.trim();
 if(iconv.decode(iconv.encode(value,'gbk'),'gbk')!==value)throw Error('检索词含网站不支持的字符，请移除表情或特殊符号');
 return 'https://www.gwdang.com/search?crc64=1&s_product='+[...iconv.encode(value,'gbk')].map(b=>/[a-zA-Z0-9_.~-]/.test(String.fromCharCode(b))?String.fromCharCode(b):'%'+b.toString(16).toUpperCase().padStart(2,'0')).join('');
}
export function queryFromUrl(url){try{const raw=new URL(url).search.match(/(?:^|[?&])(?:s_product|keyword)=([^&]*)/)?.[1];if(!raw)return '';try{return decodeURIComponent(raw.replaceAll('+',' '));}catch{return iconv.decode(Buffer.from([...raw.replaceAll('+',' ').matchAll(/%([\da-f]{2})|([^%])/gi)].map(m=>m[1]?parseInt(m[1],16):m[2].charCodeAt(0))),'gbk');}}catch{return '';}}
export function searchPlan(keyword){
 const fields=Object.fromEntries(Object.entries(extractProduct([{text:keyword,confidence:1}]).fields).map(([k,v])=>[k,v.value]));
 // Specifications remain filtering constraints even when a shorter identity query is sent.
 const effective=fields.model?[fields.brand,fields.model].filter(Boolean).join(' '):fields.brand&&fields.name?[fields.brand,fields.name].join(' '):keyword;
 return {keyword,fields,effective};
}
function numbers(text,re){return [...text.matchAll(re)].map(m=>Number(m[1]));}
export function filterCandidates(items,keyword){
 if(!keyword?.trim())return {items,excludedCount:0,unfiltered:true};
 const {fields}=searchPlan(keyword), out=[],excluded=[];let excludedCount=0;
 const expectedPacks=numbers(keyword,/(\d+)\s*(?:支|瓶|只|个|包|卷|片|枚)(?:装|\/|每|\b|$)?/g);
 const units=s=>[...s.matchAll(/(\d+(?:\.\d+)?)\s*(ml|毫升|l|升|mm|毫米|cm|厘米|kg|千克|g|克)(?![a-z])/gi)];
 for(const item of items){
  const text=item.title||'', n=normalize(text), missing=[], conflicts=[];
  const found=searchPlan(text).fields;
  if(fields.brand&&found.brand!==fields.brand)conflicts.push('品牌不符或缺失');
  if(fields.brand&&new RegExp('[\\u4e00-\\u9fff]'+fields.brand).test(text)&&!new RegExp('(?:品牌|正品|官方|原装|适用于)'+fields.brand).test(text))conflicts.push('品牌仅作为其他名称的片段出现');
  if(fields.model&&!new RegExp('(^|[^a-z0-9])'+fields.model.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+'(?![a-z0-9])','i').test(text))conflicts.push('型号不符');
  if(fields.model){const models=[...text.matchAll(/(?:^|[^a-z0-9])([a-z]{1,5}-?\d{1,6}[a-z]{0,3})(?![a-z0-9])/gi)].map(m=>m[1].toUpperCase());if(models.some(m=>m!==fields.model))conflicts.push('标题含其他型号，无法将标价归属于目标型号');}
  if(fields.model&&/[a-z]{1,5}\d{1,6}\s*\/\s*\d{2,6}(?![\dA-Za-z]|支|瓶|个|只|盒|包)/i.test(text))conflicts.push('标题包含省略前缀的多个型号');
  if(fields.name&&!n.includes(normalize(fields.name)))conflicts.push('品类不符');
  if(fields.name==='鼠标'&&/鼠标垫|桌垫|收纳|保护套|替换|适用/.test(text))conflicts.push('配件不是鼠标');
  if((fields.name==='中性笔'||fields.model==='K35')&&!/笔芯|替芯/.test(keyword)&&/替芯|适用|专用笔芯|中性笔芯/.test(text))conflicts.push('笔芯不是整笔');
  if(fields.color){if(found.color&&found.color!==fields.color)conflicts.push('颜色不符');else if(!n.includes(normalize(fields.color)))missing.push('颜色待确认');}
  if(fields.color){const colorWords={黑色:/黑色|黑笔|碳素黑|\d黑/,蓝色:/蓝色|蓝笔|\d蓝/,红色:/红色|红笔|\d红/,墨蓝色:/墨蓝|蓝黑/,白色:/米白|白色/};const other=Object.entries(colorWords).some(([c,re])=>c!==fields.color&&re.test(text));if(other){if(colorWords[fields.color]?.test(text)&&!/\d+黑\d+蓝|\d+蓝\d+红|\d+黑\d+红/.test(text))missing.push('标题含多种颜色，需确认所选SKU');else conflicts.push('颜色或组合不符');}}
  if(expectedPacks.length===1){const packs=numbers(text,/(\d+)\s*(?:支|瓶|只|个|包|卷|片|枚)/g);if(packs.length&&!packs.includes(expectedPacks[0]))conflicts.push('包装数量不符');else if(!packs.length)missing.push('包装数量待确认');}
  if(expectedPacks[0]>1&&/单支|单瓶|单个|单只/.test(text))conflicts.push('单件不符');
  if(expectedPacks.length===1&&!/\d+\s*箱/.test(keyword)&&/[×x*]\s*[2-9]\d*\s*箱/i.test(text))conflicts.push('多箱组合数量不符');
  if(fields.model&&/[+＋]/.test(text)&&/套装|组合|\+|＋/.test(text))conflicts.push('组合装待单独报价');
  if(fields.model==='K35'&&!/小金戒|瑞宝|胡桃木/.test(keyword)&&/小金戒|瑞宝|胡桃木/.test(text))conflicts.push('不同系列');
  if(fields.name==='中性笔'||fields.model==='K35'){const width=keyword.match(/(0\.[357]|1\.0)\s*(?:mm|毫米)?/i)?.[1],got=text.match(/(0\.[357]|1\.0)(?!\d)/)?.[1];if(width&&got&&Number(width)!==Number(got))conflicts.push('笔尖规格不符');}
  const wanted=units(keyword);for(const m of wanted){const family=u=>/^(ml|毫升|l|升)$/i.test(u)?'volume':/^(mm|毫米|cm|厘米)$/i.test(u)?'length':'weight';const val=(v,u)=>Number(v)*(/^(l|升|kg|千克)$/i.test(u)?1000:/^(cm|厘米)$/i.test(u)?10:1);const matches=units(text).filter(x=>family(x[2])===family(m[2]));if(matches.length&&!matches.some(x=>val(x[1],x[2])===val(m[1],m[2])))conflicts.push('规格不符');else if(!matches.length)missing.push('规格待确认');}
  if(!fields.brand&&!fields.model&&!fields.name){const terms=queryTerms(keyword);if(!terms.every(t=>n.includes(normalize(t))))conflicts.push('关键词不符');}
  if(/充电线/.test(keyword)){
   if(!/充电线|充电连接线/.test(text))conflicts.push('未明确包含所需充电线');
   if(/(?:不含|不带|不配|无)\s*充电线/.test(text))conflicts.push('商品明确不含充电线');
   if(!/[+＋]|套装|组合/.test(keyword)&&/(?:充电器|充电头)\s*[+＋]\s*充电线/.test(text))conflicts.push('充电器与线组合装，不是单独充电线');
   missing.push('接口、适配机型及线长待确认');
   if(/充电器|充电头|适配器/.test(text)&&!/(?:不含|不带)\s*充电头/.test(text))missing.push('需核实标价对应线材还是充电器SKU');
  }
  if(conflicts.length){excludedCount++;excluded.push({id:item.id,platform:item.platform,title:item.title,reasons:[...new Set(conflicts)]});continue;}
  out.push({...item,relevance:'identity_candidate',match:'pending',matchNotes:missing.length?[...new Set(missing)].join('；'):'标题符合检索线索；选中SKU和当前价格待核验'});
 }
 return {items:out,excludedCount,excluded};
}
export function platformCoverage(rawItems,items,excluded=[]){return [...new Set(['京东','淘宝','拼多多','苏宁','南粤分享汇','史泰博',...rawItems.map(q=>q.platform)])].map(platform=>{const received=rawItems.filter(q=>q.platform===platform).length,kept=items.filter(q=>q.platform===platform).length;const reasons=[...new Set(excluded.filter(q=>q.platform===platform).flatMap(q=>q.reasons))];return {platform,received,kept,excluded:received-kept,reasons,status:kept?'有相关候选，规格待核验':received?'已返回，但未通过商品筛选':'本次聚合页未返回，尚未逐平台查询'};});}
