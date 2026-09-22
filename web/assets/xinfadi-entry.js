(() => {
 const $=s=>document.querySelector(s);
 const fields=[['name','品名'],['category','分类'],['subcategory','子分类'],['low_price','最低价'],['average_price','平均价'],['high_price','最高价'],['unit','单位'],['spec','规格'],['origin','产地'],['published_at','发布日期']];
 let result=null,epoch=0;
 const batchToggle=document.createElement('button');batchToggle.type='button';batchToggle.id='food-batch-toggle';batchToggle.textContent='供应商 / 项目浮动定价与打印';batchToggle.hidden=true;
 document.querySelector('nav[aria-label="查价业务"]').append(batchToggle);
 const frame=document.createElement('iframe');frame.title='供应商协议价与项目浮动';frame.id='food-price-batch';frame.hidden=true;frame.style.cssText='width:100%;border:0;height:1100px';document.querySelector('main').append(frame);
 batchToggle.onclick=()=>{frame.hidden=!frame.hidden;if(!frame.hidden){if(!frame.src)frame.src='/assets/price-batch.html';frame.scrollIntoView({behavior:'smooth'})}};
 window.addEventListener('message',e=>{if(e.origin===location.origin&&e.source===frame.contentWindow&&e.data?.type==='price-batch-height')frame.style.height=Math.min(20000,Math.max(700,Number(e.data.height)||1100))+'px';if(e.origin===location.origin&&e.data==='price-lookup-clear'){frame.removeAttribute('src');frame.hidden=true;}});

 function switchSource(){epoch++;result=null;$('#results').replaceChildren();$('#export').disabled=true;$('#submit').disabled=false;const market=$('#platform').value==='xinfadi';batchToggle.hidden=!market;if(!market)frame.hidden=true;$('#results').style.display=market?'block':'';$('#query-label').textContent=market?'食材名称':'商品名称 / 型号';$('#market-date-label').hidden=!market;$('#market-date-label').style.display=market?'':'none';$('#query').placeholder=market?'例如：大白菜、土豆':'例如：惠普 915XL 黑色 原装';$('#query').maxLength=market?80:120;$('#status').textContent=market?'输入食材品名；日期留空查近 7 天最新可用行情。':'输入商品信息开始查询。';$('.notice').textContent=market?'新发地市场参考行情，不是供应商报价。保留原单位，不自动把斤、公斤、箱换算；同名食材仍需核对规格和产地。':'参考价不等于最终报价，请确认规格、优惠条件和运费。';$('.examples').style.display=market?'none':'';}
 $('#platform').addEventListener('change',switchSource);switchSource();
 $('#search').addEventListener('submit',async e=>{
  if($('#platform').value!=='xinfadi')return;e.preventDefault();const n=++epoch;result=null;$('#results').replaceChildren();$('#export').disabled=true;$('#submit').disabled=true;$('#status').textContent='正在查询新发地行情…';
  try{const session=await(await fetch('/api/session')).json();if(!session.user)throw Error('请先在工作台登录');const r=await fetch('/api/price-lookup/xinfadi',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':session.csrf},body:JSON.stringify({query:$('#query').value,date:$('#market-date').value||'latest'})});const v=await r.json();if(!r.ok)throw Error(v.error||'查询失败');if(n!==epoch)return;result=v;
   $('#status').textContent=v.items.length?`行情日期：${v.effective_date} · ${v.items.length} 条 · ${v.complete?'完整返回':'部分结果'} · ${v.cached||v.catalogue_cached?'近期缓存':'本次获取'}`:'指定日期范围暂无同名行情，不以旧价或零价补填。';
   const wrap=document.createElement('div');wrap.style.overflowX='auto';const table=document.createElement('table');table.style.cssText='width:100%;border-collapse:collapse;font-size:14px';
   for(const row of [null,...v.items]){const tr=document.createElement('tr');for(const [key,label] of fields){const cell=document.createElement(row?'td':'th');cell.textContent=row?row[key]??'—':label;cell.style.cssText='padding:12px;text-align:left;border-bottom:1px solid #ddd;white-space:nowrap';tr.append(cell);}table.append(tr);}wrap.append(table);$('#results').append(wrap);
   const a=document.createElement('a');a.href='http://www.xinfadi.com.cn/priceDetail.html';a.target='_blank';a.rel='noopener noreferrer';a.textContent='打开新发地官网行情来源 ↗';$('#results').append(a);$('#export').disabled=!v.items.length;
  }catch(error){if(n===epoch)$('#status').textContent=error.message;}finally{if(n===epoch)$('#submit').disabled=false;}
 });
 $('#export').addEventListener('click',()=>{if($('#platform').value!=='xinfadi'||!result)return;const safe=v=>'"'+String(v??'').replace(/^[=+\-@\t\r]/,"'$&").replaceAll('"','""')+'"';const rows=[['来源','行情日期',...fields.map(x=>x[1])],...result.items.map(row=>['新发地市场参考行情',result.effective_date,...fields.map(([key])=>row[key])])];const url=URL.createObjectURL(new Blob(['\uFEFF'+rows.map(row=>row.map(safe).join(',')).join('\r\n')],{type:'text/csv;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download='新发地行情.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
 window.addEventListener('message',e=>{if(e.origin===location.origin&&e.data==='price-lookup-clear'){epoch++;result=null;}});
})();
