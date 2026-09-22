const E=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));let selected='tax',step=0,caseIndex=0;let policy;
const delivery=[
['接单','保留客户原意','录入客户原称、原订购量和单位，把规格、品牌、加工要求一起保留。历史商品只作候选，确认后才接到采购。','已归档原单有“鲜大棒骨 2斤/根（中间剁开）”及按“根”订购的记录。2斤/根是规格线索，不能直接当仓库或客户实收重量。','本笔核算主体、目标商品及单位对应关系需经业务确认。'],
['采购','落实供应商与需求','按实际供应商汇总采购需求，记录采购单号、采购量和采购进价。不同商品单位不能直接合计数量。','同一张采购汇总里可能有斤、根、个、袋。采购进价与对客销售价分开，不能互相覆盖。','临时加单、缺货替换及供应商归属要补到原订单关联中。'],
['实收','以仓库实际收到为准','记录仓库实收量，保留供应商送货量与原需求的差异。采购据收货资料报账，不能把库管记录当客户签收。','现有业务资料记录了“打印采购单—库管记收货斤数—采购据此报账”的过程。库管收货单与供应商送货单是不同依据。','缺货、错货、质量问题的处理和补送量须按本笔实际记录。'],
['配送','分清发出与客户收到','分拣发出量和客户实际签收量分别填写。客户实收有差异时，先留差异与确认结果，再修改业务量。','加工要求若遗漏，可能导致退货。原单、送货表、签收量和修改版本分别保留，不能只留一个最终数字。','无签收依据时保持待补，不自动把订购量视为已交付。'],
['补最终价格','按合同与实际依据定价','保留暂价，另填最终销售单价和定价依据。网站参考价、采购价、对客结算价是不同数据。','历史单中的 0.01 属于暂存阶段，不能计作真实成交金额。定价表已有“先加加工调整再下浮”的实例，顺序与舍入按对应表核对。','取价来源、日期、最高/平均/最低口径、单位和加工费规则由本客户条件决定。'],
['对账','量价齐全后核对金额','本地对账按客户实收量乘最终销售价逐行计算；数量或价格改变后重新核对。导出保留原数量、差异和依据。','对供应商按日总额核对只能初筛；日期错位、退货和小数精度仍要回到明细。采购单号反查附件只说明已导入文件中的命中。','确认对账不代表付款、收款、实际开票或外部系统已结算。']
];
const industry=[
['品类','先认商品属于什么','蔬菜、水果、肉禽蛋、水产及干杂等按来源自身分类保留。同名可能跨类，不单凭名称视为同一商品。','同名商品在行情中可能有多行；类别、规格、产地与日期要一起核。'],
['规格','先比是不是同一实物','规格、等级、品牌、箱规与产地影响对应关系。系列名称与具体 SKU 分开。','“每条约 2.5 斤”的要求必须保留；不能因品名相同忽略大小或等级。'],
['单位','先对数量与计价口径','订购单位、采购单位、行情计价单位分别保留。根、个、袋、箱不能未经确认就换算成斤。','创价现有导出缺独立计价单位列，不能自行补成“元/斤”。'],
['加工','把备注变成采购要求','剁开、去皮、生熟、腌制及调味要求影响采购、价格，也影响税率待核事实。','“中间剁开”应随订单到分拣配送；“半成品”字样不能直接决定税率。'],
['价格来源','找到来源与适用日期','新发地保留最低、平均、最高价和日期；创价导出保留批发/零售、等级等差别。按对应合同选择来源与口径。','市场行情是参考价；报价、采购进价和对客结算价分别记录。缺价与数值 0 不混为一谈。'],
['业务使用','确认后再带入本单','按客户、商品身份、单位和周期核对候选，再形成实际采购或对客价格。仍缺依据的保持待核。','政企客户原始需求先映射商品和单位，再到销售订单；用友“销售发票”业务单据与税务实际开票分开。']
];
function sources(names,extra=''){return `<details class="sources"><summary>查看资料来源与使用范围</summary><p>${names.map(E).join('<br>')}</p><p>依据已有业务整理页展示；原始访谈、凭证和内部排障内容不在此页展示。具体商品、主体和单据仍以业务人员确认结果为准。</p>${extra}</details>`}
function render(){document.querySelectorAll('[data-category]').forEach(b=>{b.classList.toggle('active',b.dataset.category===selected);b.setAttribute('aria-pressed',b.dataset.category===selected)});document.getElementById('view').innerHTML=selected==='tax'?tax():process(selected==='delivery'?delivery:industry);bind()}
function process(items){const d=selected==='delivery',x=items[step];return `<div class="intro"><div><h2>${d?'一笔食材业务，沿着这六步走':'同一个商品，六件事要对齐'}</h2><p>${d?'点击每一步，查看怎么做、有哪些现成例子、什么还要确认。':'先把商品和单位认准，再把合适的价格用到正确的业务里。'}</p></div><button class="quiet-link" onclick="gcGo('food')">去食材接单与对账 →</button></div><div class="flow">${items.map((v,i)=>`<button data-step="${i}" class="${step===i?'active':''}" aria-pressed="${step===i}"><span>0${i+1}</span><b>${v[0]}</b><small>${v[1]}</small></button>`).join('')}</div><section class="detail"><div><h3>${x[0]} · ${x[1]}</h3><p>${x[2]}</p>${x[4]?`<div class="pending"><b>需要确认</b><br>${x[4]}</div>`:''}</div><div class="example"><b>已有业务资料中的例子</b><p>${x[3]}</p></div></section>${!d?`<div class="two"><div class="box"><h3>参考行情 → 实际采购 → 对客结算</h3><p>三个价格分别记录。合同指定的来源、日期及取价口径不能由最低价自动代替。</p></div><div class="box"><h3>食材与政企，各用对应资料</h3><p>食材行情保留品类、单位、日期；政企电商报价需核品牌、型号、规格、报价条件。不能混成同一类报价。</p></div></div>`:''}${sources(d?['《食材采购与价格维护》：接单、采购、库管实收、配送、暂价和补价','《财务税率维护与采购结算》：对账与付款衔接','已有食材 Demo：历史原单保留与量价计算']:['《食材采购与价格维护》：规格、单位、加工与定价表','《食材价格信息源》：新发地与创价字段及使用范围','《政企订单到回款》：商品映射、订单与开票环节'])}`}
function tax(){const owner=new URLSearchParams(location.search).get('owner');return `<iframe title="真实税率知识库" src="/business/tax/?owner=${encodeURIComponent(owner)}&view=library" style="width:100%;height:1100px;border:0" loading="lazy"></iframe>`}
function bind(){document.querySelectorAll('[data-step]').forEach(b=>b.onclick=()=>{step=Number(b.dataset.step);render()});document.querySelectorAll('[data-case]').forEach(b=>b.onclick=()=>{caseIndex=Number(b.dataset.case);render()})}
document.querySelectorAll('[data-category]').forEach(b=>b.onclick=()=>{selected=b.dataset.category;step=0;render()});fetch('policy.json').then(r=>r.json()).then(p=>{policy=p;render()}).catch(()=>document.getElementById('view').textContent='内容暂未读取，请重新登录后打开。');
