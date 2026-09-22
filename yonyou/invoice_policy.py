"""Non-secret invoice configuration, separate from sales-order saving."""
import json
import os
import re
from pathlib import Path


def validate(value):
    if isinstance(value,dict) and value.get('schemaVersion')==2:
        if (value.get('mode')!='history' or type(value.get('enabled')) is not bool
                or value.get('requireHumanConfirmation') is not True):
            raise ValueError('自动匹配开票配置无效')
        return {'enabled':value['enabled'],'mode':'history','control':'local-invoice-history-v2'}
    if not isinstance(value, dict) or value.get('schemaVersion') != 1:
        raise ValueError('开票配置版本无效')
    for key in ('enabled', 'saveOnlyVerified'):
        if type(value.get(key)) is not bool: raise ValueError('开票配置缺少明确开关')
    for key in ('salesOrgId', 'settlementOrgId', 'transactionTypeId'):
        if not isinstance(value.get(key), str) or not re.fullmatch(r'\d{1,20}', value[key]):
            raise ValueError('开票配置需要有效的' + key)
    if not isinstance(value.get('bizFlow'), str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', value['bizFlow']):
        raise ValueError('开票配置需要有效的业务流编号')
    if value['enabled'] and not value['saveOnlyVerified']:
        raise ValueError('须先验证该业务流保存销售发票不会自动提交、审核或再次税务开票')
    return {'enabled': value['enabled'], 'verified': value['saveOnlyVerified'],
            **{k:value[k] for k in ('salesOrgId','settlementOrgId','transactionTypeId','bizFlow')},
            'control':'local-invoice-policy-v1'}


def read_policy(state):
    path = Path(state) / 'invoice-policy.json'
    if path.exists():
        try:
            if path.stat().st_size > 16384: raise ValueError('开票配置过大')
            return validate(json.loads(path.read_text(encoding='utf-8-sig')))
        except (OSError, ValueError, TypeError):
            return {'enabled':False, 'verified':False, 'control':'local-invoice-policy-invalid'}
    value = {'schemaVersion':1, 'enabled':os.environ.get('YONYOU_ENABLE_INVOICE_SAVE') == '1',
             'saveOnlyVerified':os.environ.get('YONYOU_INVOICE_FLOW_VERIFIED') == '1',
             'salesOrgId':os.environ.get('YONYOU_INVOICE_SALES_ORG_ID',''),
             'settlementOrgId':os.environ.get('YONYOU_INVOICE_SETTLEMENT_ORG_ID',''),
             'transactionTypeId':os.environ.get('YONYOU_INVOICE_TRANSACTION_TYPE_ID',''),
             'bizFlow':os.environ.get('YONYOU_INVOICE_FLOW_ID','')}
    try: return {**validate(value),'control':'invoice-environment'}
    except ValueError: return {'enabled':False, 'verified':False, 'control':'invoice-unconfigured'}


def require_scope(config, header, order):
    for key, actual in [('salesOrgId',header['salesOrgId']), ('settlementOrgId',order.get('settlementOrgId'))]:
        if config.get(key) and str(actual) != config[key]:
            raise ValueError('当前销售组织或开票组织不在已验证的发票配置范围内')
