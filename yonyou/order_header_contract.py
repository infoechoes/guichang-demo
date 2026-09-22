"""Screenshot-aligned local fields that the running ERP adapter does NOT send.

These may be prepared locally, but a real save must not silently drop them.
No credentials, remote calls or changes to the credential-owning service.
"""
LOCAL_ONLY_HEADER_FIELDS = {
    'receiverName': '收货人',
    'receiverPhone': '收货电话',
    'customerSettlementDepartment': '客户结算部门',
    'projectSigningBank': '项目签约银行',
    'customerContact': '客户联系人',
    'project': '项目',
    'customerContactPhone': '客户联系电话',
    'plannedShipDate': '计划发货日期',
    'projectName': '项目名称',
    'projectCode': '项目编码',
    'advanceInvoiced': '是否预收已开票',
    'receivableInvoiceNumber': '税票号码（应收）',
    'deliveryWarehouse': '发货仓库',
}


def local_field_warnings(header):
    return [label + '：已保留在本机，但当前用友连接尚未映射此字段，不能静默省略后保存'
            for key, label in LOCAL_ONLY_HEADER_FIELDS.items()
            if header.get(key) is not None and str(header[key]).strip() != '']


def check_header_delivery(plan, header, mode):
    warnings = local_field_warnings(header)
    plan['headerDeliveryWarnings'] = warnings
    if mode == 'live' and warnings:
        plan['errors'].extend(warnings)
        plan['ready'] = False
        plan['payload'] = None
    return plan
