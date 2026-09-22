"""Surgical OOXML template export; untouched hidden/version sheets remain byte-identical.

This is an application exporter, not an invoice API. It never calculates tax or
converts amount bases. External links and the previous example data are removed.
"""
import copy
import re
import zipfile
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath

NS='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
RNS='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PNS='http://schemas.openxmlformats.org/package/2006/relationships'
def tag(x):return '{'+NS+'}'+x
def xml(x):return ET.tostring(x,encoding='utf-8',xml_declaration=True)
def number(v,label,required=True):
    if v in ('',None):
        if required:raise ValueError(label+'必填')
        return None
    try:
        x=Decimal(str(v))
        if not x.is_finite():raise InvalidOperation()
        return x
    except InvalidOperation:raise ValueError(label+'须为有效数值')
def parts(path):
    with zipfile.ZipFile(path) as z:
        names=z.namelist()
        if len(names)>5000 or sum(i.file_size for i in z.infolist())>512*1024*1024:raise ValueError('模板压缩结构过大')
        wb=ET.fromstring(z.read('xl/workbook.xml'));rels=ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
        targets={r.attrib['Id']:r.attrib['Target'] for r in rels}
        sheets={s.attrib['name']:{'path':str(PurePosixPath('xl')/targets[s.attrib['{'+RNS+'}id']]).replace('xl//',''),'state':s.attrib.get('state','visible')} for s in wb.find(tag('sheets'))}
        for v in sheets.values():
            if v['path'].startswith('xl//xl/'):v['path']=v['path'][4:]
            if v['path'].startswith('/'):v['path']=v['path'].lstrip('/')
        return sheets
def inspect_template(path):
    sheets=parts(path)
    for name in ('1-明细模板','excelVersion','xzqhdm','2-特定业务信息'):
        if name not in sheets:raise ValueError('不是约定的电子税务局模板：缺少'+name)
    if any(sheets[n]['state']!='hidden' for n in ('excelVersion','xzqhdm')):raise ValueError('模板隐藏页状态异常')
    with zipfile.ZipFile(path) as z:
        strings=[]
        if 'xl/sharedStrings.xml' in z.namelist():strings=[''.join(si.itertext()) for si in ET.fromstring(z.read('xl/sharedStrings.xml'))]
        def value(c):
            if c is None:return ''
            if c.get('t')=='s':return strings[int(c.find(tag('v')).text)]
            return ''.join(c.find(tag('is')).itertext()) if c.find(tag('is')) is not None else c.findtext(tag('v'),'')
        sheet=ET.fromstring(z.read(sheets['1-明细模板']['path']));cells={c.attrib['r']:c for c in sheet.iter(tag('c'))}
        headers=[value(cells.get(c+'3')) for c in 'ABCDEFGH']
        if headers[:2]!=['项目名称','商品和服务税收分类编码'] or headers[6:]!=['金额','税率']:raise ValueError('模板字段顺序不符')
        limits={}
        for col in 'ABCDEFGHJ':
            m=re.search(r'限(\d+)字符',value(cells.get(col+'2')))
            if m:limits[col]=int(m.group(1))
        version=ET.fromstring(z.read(sheets['excelVersion']['path']))
        version_text=[value(c) for c in version.iter(tag('c'))]
        if not any(v.startswith('mx;') for v in version_text):raise ValueError('模板版本标记缺失')
        policies=[]
        for d in sheet.iter(tag('dataValidation')):
            if d.get('sqref','').startswith('J'):
                f=d.findtext(tag('formula1'),'');policies=f.strip('"').split(',')
        return {'sheets':sheets,'headers':headers,'limits':limits,'version':version_text,'policies':policies,'example_rows':sum(int(r.get('r','0'))>=4 for r in sheet.iter(tag('row')))}

def write_template(template,rows,output):
    meta=inspect_template(template);sheets=meta['sheets'];target=sheets['1-明细模板']['path']
    if not rows or len(rows)>5000:raise ValueError('单张模板须为1至5000条，不能静默截断')
    with zipfile.ZipFile(template) as src:
        data={i.filename:src.read(i.filename) for i in src.infolist()}
        root=ET.fromstring(data[target]);sheetdata=root.find(tag('sheetData'))
        prototype=next((r for r in sheetdata if int(r.get('r','0'))>=4),None)
        styles={re.sub(r'\d','',c.get('r','')):c.get('s','0') for c in prototype} if prototype is not None else {}
        for r in list(sheetdata):
            if int(r.get('r','0'))>=4:sheetdata.remove(r)
        for rowno,item in enumerate(rows,4):
            values={'A':item['name'],'B':item['tax_code'],'C':item.get('spec',''),'D':item.get('unit',''),
                    'E':number(item.get('qty'),'数量'),'F':number(item.get('unit_price'),'单价',False),
                    'G':number(item.get('amount'),'金额'),'H':number(item['rate'],'税率'),'J':item.get('export_policy','')}
            if values['G'].as_tuple().exponent < -2:raise ValueError('金额超过模板两位小数：第'+str(rowno-3)+'项；不能静默舍入')
            if values['J'] and values['J'] not in meta['policies']:raise ValueError('优惠政策不在此模板枚举中，须先核实模板口径')
            if values['F'] is not None and abs(values['E']*values['F']-values['G'])>Decimal('.01'):raise ValueError('数量×单价与原金额不符，须核实或将非必填单价留空')
            r=ET.SubElement(sheetdata,tag('row'),{'r':str(rowno)})
            for col,v in values.items():
                if v is None:continue
                if col in meta['limits'] and len(str(v))>meta['limits'][col]:raise ValueError('第'+str(rowno-3)+'项 '+col+' 列超模板字符限长，须人工修改并重审')
                c=ET.SubElement(r,tag('c'),{'r':col+str(rowno),'s':styles.get(col,'0')})
                if isinstance(v,Decimal):ET.SubElement(c,tag('v')).text=format(v,'f')
                else:
                    c.set('t','inlineStr');t=ET.SubElement(ET.SubElement(c,tag('is')),tag('t'));t.set('{http://www.w3.org/XML/1998/namespace}space','preserve');t.text=str(v)
        dimension=root.find(tag('dimension'))
        if dimension is not None:dimension.set('ref','A1:V'+str(len(rows)+3))
        # Preserve validation rules while extending their coverage, not their semantics.
        for d in root.iter(tag('dataValidation')):
            d.set('sqref',re.sub(r'([A-Z]+)4:\1\d+',lambda m:m.group(1)+'4:'+m.group(1)+str(max(1001,len(rows)+3)),d.get('sqref','')))
        data[target]=xml(root)
        wb=ET.fromstring(data['xl/workbook.xml'])
        for e in list(wb):
            if e.tag==tag('externalReferences'):wb.remove(e)
            elif e.tag==tag('definedNames'):
                for n in list(e):
                    if '[' in (n.text or ''):e.remove(n)
        data['xl/workbook.xml']=xml(wb)
        for path in ['xl/_rels/workbook.xml.rels','[Content_Types].xml']:
            e=ET.fromstring(data[path])
            for n in list(e):
                if any(x in str(n.attrib) for x in ('externalLink','calcChain')):e.remove(n)
            data[path]=xml(e)
        # Blank unused shared strings without renumbering references in the hidden sheets.
        if 'xl/sharedStrings.xml' in data:
            used=set()
            for name,raw in data.items():
                if name.startswith('xl/worksheets/') and name.endswith('.xml'):
                    for c in ET.fromstring(raw).iter(tag('c')):
                        if c.get('t')=='s':used.add(int(c.findtext(tag('v'))))
            ss=ET.fromstring(data['xl/sharedStrings.xml'])
            for i,si in enumerate(ss):
                if i not in used:
                    si.clear();ET.SubElement(si,tag('t')).text=''
            data['xl/sharedStrings.xml']=xml(ss)
        temp=str(output)+'.tmp'
        with zipfile.ZipFile(temp,'w',zipfile.ZIP_DEFLATED) as out:
            for name,raw in data.items():
                if name.startswith('xl/externalLinks') or 'calcChain' in name:continue
                out.writestr(name,raw)
        from pathlib import Path
        Path(temp).replace(output)
    return {'rows':len(rows),'template_version':meta['version'],'amount_conversion':False,'tax_bureau_import_verified':False}
