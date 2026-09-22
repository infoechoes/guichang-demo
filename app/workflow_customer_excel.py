"""Inspect and archive customer workbooks without trusting browser-normalized rows."""
import base64
import hashlib
import importlib.util
import sys
from pathlib import Path
from workflow_excel import tables_excel

def inspect(data, workflow):
    name=data.get('filename')
    if not isinstance(name,str) or Path(name).suffix.lower() not in ('.xls','.xlsx') or len(name)>200:
        raise ValueError('请选择名称不超过200字的.xls或.xlsx文件')
    encoded=data.get('file')
    if not isinstance(encoded,str) or len(encoded)>13_400_000: raise ValueError('Excel最大10MB')
    raw=base64.b64decode(encoded,validate=True)
    if not 0<len(raw)<=10_000_000: raise ValueError('请选择不超过10MB的非空Excel文件')
    expected=Path(name).suffix.lower()[1:]
    candidates=[workflow.ROOT/'yonyou'/'customer_excel.py',workflow.ROOT.parent/'yonyou-import'/'customer_excel.py']
    path=next((p for p in candidates if p.is_file()),None)
    if path is None: raise ValueError('客户Excel解析模块尚未就绪')
    sys.path.insert(0,str(path.parent))
    spec=importlib.util.spec_from_file_location('guichang_customer_excel',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    result=module.inspect_customer_excel(raw,sheet=data.get('sheet'),header_row=data.get('headerRow'),mapping=data.get('mapping'))
    if result.get('format') not in (('xls','html_xls') if expected=='xls' else ('xlsx',)):
        raise ValueError('文件扩展名与识别出的Excel格式不一致，请使用对应格式的原始导出文件')
    return raw,result

def archive(data,workflow):
    raw,result=inspect(data,workflow)
    if not result.get('ready'): raise ValueError('请先完成工作表和列映射，处理预览中的阻断问题')
    rows=result.get('rows',[])
    if not 1<=len(rows)<=200: raise ValueError('每次需要1至200条商品')
    batch_id=data.get('batchId');folder=workflow.STORE.folder(batch_id)
    provenance={'filename':data['filename'],'fileHash':hashlib.sha256(raw).hexdigest(),'sheet':result['sheet'],'headerRow':result['headerRow'],'mapping':result['mapping']}
    snapshot={'sourceType':'customer_excel','customerOriginal':provenance,'images':[], 'warnings':result.get('warnings',[]),'rowWarnings':result.get('rowWarnings',[]),
      'rows':[{**row,'id':str(i+1),'code':row.get('sourceCode',''),'price':row.get('sourcePrice',''),'amount':row.get('sourceAmount',''),'orderDate':row.get('sourceDate',''),'sourceName':data['filename'],'reviewed':False,'sourceWarnings':next((w.get('warnings',[]) for w in result.get('rowWarnings',[]) if str(w.get('row'))==str(row.get('sourceRow'))),[])} for i,row in enumerate(rows)]}
    fields=[('name','名称'),('quantity','数量'),('spec','型号'),('unit','单位'),('sourcePrice','参考价格'),('sourceAmount','金额'),('sourceCode','编码'),('deliveryLocation','下单科室'),('sourceDate','原申请日期'),('memo','客户备注'),('sourceSheet','来源工作表'),('sourceRow','来源行号')]
    values=[[label for key,label in fields]]+[[row.get(key,'') for key,label in fields] for row in rows]
    notes=[['项目','内容'],['来源文件',data['filename']],['原文件SHA256',provenance['fileHash']],['复核状态','待人工复核；来源价格不是成交价']]
    notes.extend(['提示',str(item)] for item in result.get('warnings',[]))
    notes.extend(['行提示',str(item)] for item in result.get('rowWarnings',[]))
    encoded=base64.b64encode(tables_excel([('业务录单',values),('来源及提示',notes)])).decode('ascii')
    with workflow.LOCK:
        if (folder/'batch.json').exists():
            existing=workflow.STORE.get(batch_id)
            workflow.STORE.require_active(existing)
            if existing.get('snapshotHash')!=hashlib.sha256(workflow.json_bytes(snapshot)).hexdigest():
                raise ValueError('此批次已绑定其他内容，请创建新批次')
        original=folder/('customer-original.'+('xls' if result['format']=='html_xls' else result['format']))
        if original.exists() and hashlib.sha256(original.read_bytes()).hexdigest()!=provenance['fileHash']:
            raise ValueError('此批次已绑定其他客户原文件，请创建新批次')
        # Raw bytes are immutable through this API; retry only reuses identical content.
        if not original.exists(): workflow.atomic_write(original,raw)
        record=workflow.STORE.save_source({'batchId':batch_id,'excel':encoded,'snapshot':snapshot})
        record['customerOriginal']={**provenance,'format':result['format'],'path':str(original),'downloadUrl':f'/api/workflow/batches/{batch_id}/files/{original.name}'}
        return workflow.STORE.persist(record)
