"""Generate the final archive exclusively from the gateway's immutable server review."""
import io
import zipfile
import xml.etree.ElementTree as ET

NS='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL='http://schemas.openxmlformats.org/officeDocument/2006/relationships'

def xml(element): return ET.tostring(element,encoding='utf-8',xml_declaration=True)
def column(index):
    result=''
    while index: index,r=divmod(index-1,26);result=chr(65+r)+result
    return result

def sheet(rows):
    root=ET.Element('worksheet',xmlns=NS)
    views=ET.SubElement(root,'sheetViews');view=ET.SubElement(views,'sheetView',workbookViewId='0')
    ET.SubElement(view,'pane',ySplit='1',topLeftCell='A2',activePane='bottomLeft',state='frozen')
    cols=ET.SubElement(root,'cols');ET.SubElement(cols,'col',min='1',max=str(max(map(len,rows))),width='24',customWidth='1')
    data=ET.SubElement(root,'sheetData')
    for n,values in enumerate(rows,1):
        row=ET.SubElement(data,'row',r=str(n))
        for c,value in enumerate(values,1):
            text='' if value is None else str(value)
            if len(text)>32767: raise ValueError('最终订单字段过长，无法完整写入 Excel')
            cell=ET.SubElement(row,'c',r=f'{column(c)}{n}',t='inlineStr',s='1' if n==1 else '0')
            inline=ET.SubElement(cell,'is');ET.SubElement(inline,'t',{'{http://www.w3.org/XML/1998/namespace}space':'preserve'}).text=text
    return xml(root)

def review_excel(review,batch):
    if not review.get('ready') or not review.get('payload'): raise ValueError('订单预览尚未完成')
    inp=review['input'];rows=inp['rows'];payload=review['payload']['data']
    business=[['名称','数量','型号','单位','单价','金额','编码','下单科室','来源编号','用友商品ID','来源文件','原申请日期','价格依据','行备注','计算补全说明','用友商品编码','商品知识库来源','知识库原表行号','客户工作表','客户原表行号']]
    for index,row in enumerate(rows):
        detail=payload['orderDetails'][index]
        business.append([row.get('name'),row.get('quantity'),row.get('spec'),row.get('unit'),row.get('price'),detail.get('oriSum'),row.get('businessCode'),row.get('deliveryLocation'),row.get('originalCode'),detail.get('productId'),row.get('sourceFile') or row.get('sourceImage'),row.get('sourceDate'),row.get('priceSource'),detail.get('memo'),row.get('calculation'),row.get('code')])
        catalog = row.get('catalogSource') or {}
        business[-1].extend([catalog.get('sourceLabel'), '、'.join(map(str,catalog.get('sourceRows',[]))),row.get('sourceSheet'),row.get('sourceRow')])
    header=[['订单字段','最终确认值'],['批次编号',batch['batchId']],['源 Excel SHA256',batch['fileHash']],['预览版本',review['planId']],['业务摘要',review['digest']],['输入版本摘要',review['inputHash']],['模式',inp['mode']],['状态','最终确认版本存档；是否已保存用友请查看批次回执']]
    header.extend([[key,value] for key,value in inp['header'].items()])
    exact=[['发送字段路径','精确值（按文本保留）']]
    def flatten(value,prefix):
        if isinstance(value,dict):
            for key,item in value.items():flatten(item,f'{prefix}.{key}')
        elif isinstance(value,list):
            for index,item in enumerate(value):flatten(item,f'{prefix}[{index}]')
        else:exact.append([prefix,value])
    flatten(review['payload'],'payload')
    from workflow_mapping import confirmed_mapping, mapping_table
    mapping=confirmed_mapping(review,batch)
    tables=[('业务录单',business),('订单信息',header),('精确请求记录',exact)]
    if mapping:tables.append(('已确认客户商品映射',mapping_table(mapping['rows'])))
    import json
    tables.append(('价格规则与来源',[['原行ID','商品编码','结算确认','价格快照']]+[[r.get('sourceLineId'),r.get('code'),'未确认结算',json.dumps(r.get('priceEvidence') or {},ensure_ascii=False)] for r in rows]))
    return tables_excel(tables)


def tables_excel(tables):
    book=ET.Element('workbook',{'xmlns':NS,'xmlns:r':REL});sheets=ET.SubElement(book,'sheets')
    rels=ET.Element('Relationships',xmlns='http://schemas.openxmlformats.org/package/2006/relationships')
    types=ET.Element('Types',xmlns='http://schemas.openxmlformats.org/package/2006/content-types')
    ET.SubElement(types,'Default',Extension='rels',ContentType='application/vnd.openxmlformats-package.relationships+xml')
    ET.SubElement(types,'Default',Extension='xml',ContentType='application/xml')
    ET.SubElement(types,'Override',PartName='/xl/workbook.xml',ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml')
    ET.SubElement(types,'Override',PartName='/xl/styles.xml',ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml')
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
        for index,(name,data) in enumerate(tables,1):
            ET.SubElement(sheets,'sheet',name=name,sheetId=str(index),attrib={'r:id':f'rId{index}'})
            ET.SubElement(rels,'Relationship',Id=f'rId{index}',Type=REL+'/worksheet',Target=f'worksheets/sheet{index}.xml')
            ET.SubElement(types,'Override',PartName=f'/xl/worksheets/sheet{index}.xml',ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml')
            archive.writestr(f'xl/worksheets/sheet{index}.xml',sheet(data))
        ET.SubElement(rels,'Relationship',Id='rIdStyles',Type=REL+'/styles',Target='styles.xml')
        archive.writestr('xl/styles.xml',f'''<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="{NS}"><fonts count="2"><font><sz val="11"/><name val="Microsoft YaHei"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Microsoft YaHei"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF183D65"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="49" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf><xf numFmtId="49" fontId="1" fillId="2" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf></cellXfs></styleSheet>''')
        archive.writestr('xl/workbook.xml',xml(book));archive.writestr('xl/_rels/workbook.xml.rels',xml(rels));archive.writestr('[Content_Types].xml',xml(types))
        archive.writestr('_rels/.rels',f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
    return output.getvalue()
