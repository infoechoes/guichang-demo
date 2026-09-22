export const PLATFORMS={
 jd:{name:'京东',home:'https://www.jd.com/',login:'https://passport.jd.com/new/login.aspx',search:q=>'https://search.jd.com/Search?keyword='+encodeURIComponent(q),domains:['jd.com'],cards:'.gl-item,[data-sku]',links:'a[href*="item.jd.com/"]',title:'.p-name em,.p-name a',price:'.p-price i,.p-price',shop:'.p-shop a,.curr-shop a'},
 taobao:{name:'淘宝',home:'https://www.taobao.com/',login:'https://login.taobao.com/member/login.jhtml',search:q=>'https://s.taobao.com/search?q='+encodeURIComponent(q),domains:['taobao.com','tmall.com'],cards:'[class*="Card--doubleCard"],.item.J_MouserOnverReq,.items .item,[data-category="auctions"]',links:'a[href*="item.taobao.com/item"],a[href*="detail.tmall.com/item"],a[href*="chaoshi.detail.tmall.com/item"]',title:'[class*="Title--title"],.title,[class*="title"]',price:'[class*="innerPriceWrapper"],[class*="Price--priceWrapper"],.price strong,[class*="priceWrapper"]',shop:'[class*="shopNameText"],[class*="ShopInfo--shopName"],[class*="shopName"],.shopname'},
 pdd:{name:'拼多多',home:'https://mobile.yangkeduo.com/',login:'https://mobile.yangkeduo.com/login.html',search:q=>'https://mobile.yangkeduo.com/search_result.html?search_key='+encodeURIComponent(q),domains:['yangkeduo.com','pinduoduo.com'],cards:'[data-goods-id],[class*="goods-item"],[class*="goodsItem"]',links:'a[href*="goods_id="]',title:'[class*="goods-name"],[class*="goodsName"],[class*="title"]',price:'[class*="price"]',shop:'[class*="mallName"],[class*="shop-name"]'}
};
export function productURL(value,platform){try{const u=new URL(value);const allowed=PLATFORMS[platform]?.domains.some(d=>u.hostname===d||u.hostname.endsWith('.'+d));if(u.protocol!=='https:'||!allowed||u.username||u.password)return '';if(platform==='jd'){const m=u.hostname==='item.jd.com'&&u.pathname.match(/^\/(\d+)\.html$/);return m?`https://item.jd.com/${m[1]}.html`:'';}const key=platform==='pdd'?'goods_id':'id',id=u.searchParams.get(key);if(!/^\d+$/.test(id||''))return '';return `${u.origin}${u.pathname}?${key}=${id}`;}catch{return '';}}
export function parsePrice(raw){const s=String(raw??'').replace(/[,，\s]/g,'');if(/[起~～至]|\d-\d/.test(s))return null;const a=s.match(/\d+(?:\.\d+)?/g);if(a?.length!==1)return null;const n=Number(a[0]);return n>0&&n<=1e8?n:null;}
// Runs only inside the platform page. Extract visible DOM, never private HTTP endpoints.
export function extractCards(config){
 const visible=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0;};
 const text=(el,s)=>{for(const selector of (s||'').split(',').filter(Boolean)){const e=el.querySelector(selector);if(e&&visible(e)&&e.textContent.trim())return e.textContent.trim();}return '';};
 let cards=[...document.querySelectorAll(config.cards)].filter(visible);
 if(!cards.length)cards=[...document.querySelectorAll(config.links)].filter(visible).map(a=>{let el=a;for(let n=0;n<3;n++){if(/(?:¥|￥)\s*\d/.test(el.innerText||''))break;if(el.parentElement)el=el.parentElement;}return el;});
 return [...new Set(cards)].slice(0,60).map((el,index)=>{
   const a=el.matches(config.links)?el:el.querySelector(config.links);let title=text(el,config.title);const image=el.querySelector('img');if(!title)title=image?.alt||a?.getAttribute('title')||'';
   const full=(el.innerText||'').trim();const priceRaw=text(el,config.price)||(full.match(/[¥￥]\s*\d[\d,.]*/)||[])[0]||'';
   el.setAttribute('data-guichang-card',String(index));return {index,title:title.slice(0,1000),priceRaw,store:text(el,config.shop).slice(0,200),url:a?.href||(el.getAttribute('data-sku')?'https://item.jd.com/'+el.getAttribute('data-sku')+'.html':''),visibleText:full.slice(0,2500)};
 });
}
export function extractDetail(platform){
 const visible=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0;};const first=selectors=>{for(const s of selectors){const e=[...document.querySelectorAll(s)].find(visible);if(e?.textContent.trim())return e.textContent.trim();}return '';};
 const title=first(platform==='jd'?['.sku-name']:platform==='taobao'?['[class*="ItemHeader--mainTitle"]','[class*="mainTitle"]','.tb-main-title']:['[class*="goodsName"]','[class*="goods-name"]','h1']);
 const priceRaw=first(platform==='jd'?['.p-price .price','.summary-price .price']:platform==='taobao'?['[class*="Price--priceText"]','[class*="Price--price"]','.tb-rmb-num']:['[class*="groupPrice"]','[class*="goods-price"]','[class*="price"]']);
 const store=first(platform==='jd'?['.J-hover-wrap .name a','.contact .name a','.shopName a']:platform==='taobao'?['[class*="ShopHeader--shopName"]','[class*="shopName"]','.tb-shop-name a']:['[class*="mallName"]','[class*="mall-name"]']);
 const selected=[...document.querySelectorAll('[id^="choose-attr"] .selected,[class*="SkuContent"] [class*="selected"],.tb-selected,[aria-checked="true"]')].filter(visible).map(e=>e.textContent.trim()).filter(Boolean).slice(0,12);
 return {title,priceRaw,store,spec:selected.join(' / '),url:location.href,visibleText:(document.body.innerText||'').slice(0,6000)};
}
export async function pageState(page){
 if(/passport\.jd\.com|login\.taobao\.com|\/(?:login|psnl_verification)(?:\.|\/|\?)/.test(page.url()))return {state:'login_required',message:'请在平台窗口完成登录或身份验证，再点继续采集。'};
 const text=await page.locator('body').innerText({timeout:2500}).catch(()=>'');
 for(const selector of ['.JDJRV-bigslider','#nc_1_wrapper','#baxia-dialog-content','iframe[src*="captcha"]'])if(await page.locator(selector).first().isVisible().catch(()=>false))return {state:'verification_required',message:'平台要求人工验证，请在原窗口完成后继续。'};
 if(/访问过于频繁|访问频繁|请完成验证|安全验证|滑动验证|点击.*完成验证/.test(text.slice(0,12000)))return {state:'verification_required',message:'平台要求人工验证，请在原窗口完成后继续。'};
 if(/没有找到.*商品|未找到相关商品|暂无搜索结果/.test(text))return {state:'notfound',message:'平台明确提示未找到相关商品。'};
 if(text.length<2500&&/扫码登录|密码登录|请登录后/.test(text))return {state:'login_required',message:'请在平台窗口完成登录后继续。'};
 return null;
}
