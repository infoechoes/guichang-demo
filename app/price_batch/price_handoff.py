"""Stable source-row identities and complete, status-bearing order handoff."""
import copy, hashlib, json

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()

def identity(s,ws,row):
    if s.get('_identity_raw') is not s['raw']:
        s['_identity_raw']=s['raw'];s['_identity_batch']=hashlib.sha256(s['raw']).hexdigest()
    batch=s['_identity_batch']
    values=[c.value for c in ws[row]]
    return {'source_batch_id':batch,'source_line_id':digest([batch,ws.title,row]),'sheet':ws.title,'row':row,'input_fingerprint':digest([values,s.get('mapping')]),'calculation_batch_id':s.get('calculation_batch_id')}

def original(s,ws,row):
    m=s.get('mapping') or {}
    def value(key):
        col=int(m.get(key,-1));return ws.cell(row,col+1).value if col>=0 else None
    prices=[{'column':c.column,'header':c.value,'value':ws.cell(row,c.column).value} for c in ws[s['header']] if str(c.value).strip() in ('单价','固定价','协议价') and ws.cell(row,c.column).value is not None]
    return {'name':value('name'),'spec':value('spec'),'unit':value('unit'),'quantity':value('quantity'),'source_values':[c.value for c in ws[row]],'original_prices':prices}

def current_result(s,ws,row):
    r=s.get('results',{}).get(row)
    if not r:return None
    expected=identity(s,ws,row)
    return r if all(r.get(k)==expected[k] for k in ('source_batch_id','source_line_id','input_fingerprint','calculation_batch_id')) else None

def handoff(s,ws,rows=None,line_ids=None):
    all_rows=list(range(s['header']+1,ws.max_row+1))
    if rows is not None and line_ids is not None:raise ValueError('rows 与 line_ids 只能指定一种')
    if line_ids is not None:
        ids={identity(s,ws,row)['source_line_id']:row for row in all_rows}
        if not isinstance(line_ids,list) or any(not isinstance(x,str) or x not in ids for x in line_ids):raise ValueError('行标识不属于当前文件/工作表，不能沿用旧价')
        rows=[ids[x] for x in line_ids]
    scope='all' if rows is None else 'selection'
    if rows is None:rows=all_rows
    allowed=set(all_rows)
    if not isinstance(rows,list) or any(type(row) is not int or row not in allowed for row in rows) or len(set(rows))!=len(rows):raise ValueError('交接行号无效或重复')
    output=[]
    for row in rows:
        orig=original(s,ws,row);r=copy.deepcopy(current_result(s,ws,row) or {})
        candidate=None
        if r.get('confirmed') and r.get('selected') is not None:
            candidate=copy.deepcopy(r['items'][r['selected']])
        if orig['original_prices']:
            state='preserved_original';price=None;original_price=orig['original_prices'][0]['value']
        elif r.get('confirmed') and candidate is not None:
            state='confirmed_reference';price=r.get('price_decimal');original_price=None
        elif r.get('confirmed') and r.get('rule',{}).get('basis')=='fixed':
            state='project_fixed_reference';price=r.get('price_decimal');original_price=None
        else:state='pending';price=None;original_price=None
        item={**identity(s,ws,row),**orig,'price_state':state,'price_decimal':price,'original_price':original_price,'protected':state=='preserved_original','confirmed':state=='confirmed_reference','can_fill_price':state in ('confirmed_reference','project_fixed_reference'),'requires_price_review':state=='pending','settlement_confirmed':False,'status':r.get('status','输入或单位已变化，须重新查询确认' if row in s.get('results',{}) else '未查询/未确认'),'candidate_count':len(r.get('items',[])),'selected_item':candidate}
        # Only valid confirmed calculations may leave the price module.
        for key in ('base_price','factor','fee','adjustment','formula','rule','source','date'):
            if state in ('confirmed_reference','project_fixed_reference') and key in r:item[key]=copy.deepcopy(r[key])
        if state=='preserved_original':item['status']='保留原单已有价，不重复浮动；未重新确认结算价'
        output.append(item)
    counts={state:sum(r['price_state']==state for r in output) for state in ('preserved_original','confirmed_reference','project_fixed_reference','pending')}
    return {'contract_version':2,'source_batch_id':hashlib.sha256(s['raw']).hexdigest(),'calculation_batch_id':s.get('calculation_batch_id'),'project':copy.deepcopy(s.get('project')),'sheet':ws.title,'scope':scope,'total_input_rows':len(all_rows),'returned_rows':len(output),'counts':counts,'rows':output,'settlement_confirmed':False}
