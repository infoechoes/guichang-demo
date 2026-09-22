const $=s=>document.querySelector(s),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let session=null,items=[],busy=false,epoch=0;
const money=v=>v===null||v===undefined||v===''?'未提供':Number.isFinite(Number(v))?'¥'+Number(v).toFixed(2):'未提供';
async function identity(){const r=await fetch('/api/session',{credentials:'same-origin'});const s=await r.json();if(!s.user)throw Error('请先在工作台登录');if(session&&s.user.id!==session.user.id){items=[];$('#results').replaceChildren();throw Error('账号已变化，请重新打开查价模块');}session=s;return s;}
async function api(path,data){const s=await identity();const r=await fetch(path,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':s.csrf},body:JSON.stringify(data)});const v=await r.json();if(!r.ok)throw Error(v.error||'请求未完成');return v;}
function render(){ $('#results').innerHTML=items.map(x=>`<article class="card"><span class="tag">${esc(x.source)} · ${esc(x.platform)}</span><h2>${esc(x.title)}</h2><div class="shop">${esc(x.shop||'店铺未提供')}</div><div class="prices"><div><small>参考价 · 条件待核</small><strong>${money(x.referencePrice)}</strong></div><div><small>来源原价字段 · 非已核官网价</small><strong class="original">${money(x.sourceOriginalPrice)}</strong></div></div><footer><span>规格 / SKU 尚未核实</span><button data-id="${esc(x.id)}">查看详情与来源</button></footer></article>`).join('');$('#export').disabled=!items.length;}
$('#search').addEventListener('submit',async e=>{e.preventDefault();if($('#platform').value==='xinfadi')return;if(busy)return;busy=true;const n=++epoch;$('#submit').disabled=true;$('#status').textContent='正在查询，通常需要几秒钟…';items=[];render();try{const v=await api('/api/price-lookup/search',{query:$('#query').value,platform:$('#platform').value});if(n!==epoch)return;items=v.items;render();$('#status').textContent=`${v.platform} · ${items.length} 条候选 · ${new Date(v.queriedAt).toLocaleString('zh-CN')} · ${v.cached?'一分钟内缓存':'本次获取'}${items.length?'':'；可简化关键词再查一次'}`;}catch(e){$('#status').textContent=e.message;}finally{busy=false;$('#submit').disabled=false;}});
$('.examples').addEventListener('click',e=>{if(e.target.dataset.query){$('#query').value=e.target.dataset.query;$('#query').focus();}});
$('#results').addEventListener('click',async e=>{
 const button=e.target.closest('[data-id]');if(!button||busy)return;const n=epoch;busy=true;button.disabled=true;
 $('#detail-body').textContent='正在读取商品详情…';$('#detail').showModal();
 try{const v=await api('/api/price-lookup/detail',{id:button.dataset.id});if(n!==epoch)return;const x=v.item;items=items.map(a=>a.id===x.id?x:a);
 const fields=[['搜索参考价',money(x.referencePrice)],['来源原价字段',money(x.sourceOriginalPrice)],['品牌 / 型号',[x.brand,x.model].filter(Boolean).join(' / ')||'未提供'],['运费',x.shipping],['型号 / SKU',x.skuStatus],['价格条件',x.conditions],['本次读取时间',new Date(x.detailCheckedAt).toLocaleString('zh-CN')],['原平台更新时间','接口未明确提供']];
 $('#detail-body').innerHTML=`<h2>${esc(x.title)}</h2><p>${esc(x.source)} · ${esc(x.platform)} · ${esc(x.shop)} ${v.cached?'· 一分钟内详情缓存':''}</p><dl>${fields.map(([k,v])=>`<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>${(x.warnings||[]).map(w=>`<p class="notice">${esc(w)}</p>`).join('')}${x.skuOptions?.length?`<h3>按规格查看 SKU 标价</h3><p>选择具体型号后核对原页；以下价格不含已确认的优惠、运费和税费。</p><div style="overflow:auto"><table style="width:100%;text-align:left"><thead><tr><th>规格</th><th>SKU</th><th>SKU标价</th></tr></thead><tbody>${x.skuOptions.map(s=>`<tr><td style="padding:10px">${esc(s.spec)}</td><td>${esc(s.sku)}</td><td>${money(s.price)}</td></tr>`).join('')}</tbody></table></div>`:''}<p class="notice">${esc(x.linkWarning||'所选SKU仍需核对')}。以上金额尚未通过同款和价格条件核验。</p>${x.link?`<a href="${esc(x.link)}" target="_blank" rel="noopener noreferrer">打开商品来源 ↗</a>`:`<p>${esc(x.linkError||'没有可用链接')}</p>`}`;
 }catch(e){if(n===epoch)$('#detail-body').textContent=e.message;}finally{busy=false;button.disabled=false;}
});
$('#close').onclick=()=>$('#detail').close();
$('#export').onclick=()=>{if($('#platform').value==='xinfadi')return;const safe=v=>{let s=String(v??'');if(/^[=+\-@\t\r]/.test(s))s="'"+s;return '"'+s.replaceAll('"','""')+'"';};const rows=[['平台','商品','店铺','参考价（未核条件）','来源原价字段（非已核官网价）','运费','规格核验','查询时间','推广来源链接'],...items.map(x=>[x.platform,x.title,x.shop,x.referencePrice,x.sourceOriginalPrice,x.shipping,x.skuStatus,x.queriedAt,x.link||'尚未获取'])];const url=URL.createObjectURL(new Blob(['\uFEFF'+rows.map(r=>r.map(safe).join(',')).join('\r\n')],{type:'text/csv;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download='查价参考结果.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
window.addEventListener('message',e=>{if(e.origin===location.origin&&e.data==='price-lookup-clear'){epoch++;items=[];session=null;render();$('#detail').close();}});
identity().catch(e=>{$('#status').textContent=e.message;$('#submit').disabled=true;});

$('#platform').addEventListener('change',()=>{epoch++;items=[];render();$('#detail').close();});
let goodsSource='justone-jd';
function switchBusiness(food){
 if(busy||$('#submit').disabled)return;
 if(food&&$('#platform').value!=='xinfadi')goodsSource=$('#platform').value;
 $('#platform').value=food?'xinfadi':goodsSource;
 $('#goods-source-label').hidden=food;$('#goods-source-label').style.display=food?'none':'';
 $('#mode-goods').classList.toggle('primary',!food);$('#mode-food').classList.toggle('primary',food);
 $('#mode-goods').setAttribute('aria-pressed',String(!food));$('#mode-food').setAttribute('aria-pressed',String(food));
 $('.source').textContent=food?'新发地 · 食材行情':'政企商品';
 $('h1').textContent=food?'查食材行情':'找商品，核价格';$('#query').value='';
 $('.footnote').textContent=food?'新发地行情供食材采购参考，与政企电商三方比价分开。':'不同数据源可能对应同一电商平台，不能重复计为两家。';
 $('#platform').dispatchEvent(new Event('change'));
}
$('#mode-goods').onclick=()=>switchBusiness(false);$('#mode-food').onclick=()=>switchBusiness(true);
