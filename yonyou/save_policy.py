"""Non-secret, local save switch; read on every request, no credential reload.

An explicit file overrides the legacy environment flags, including when it is
disabled or invalid. Scope is limited to verified organization/transaction pairs.
"""
import json
import os
from pathlib import Path


def save_policy(state):
    path = Path(state) / 'save-policy.json'
    if path.exists():
        try:
            if path.stat().st_size > 32768: raise ValueError('policy too large')
            policy = json.loads(path.read_text(encoding='utf8'))
            scopes = policy.get('scopes', [])
            if policy.get('schemaVersion') != 1 or not isinstance(scopes, list): raise ValueError('invalid schema')
            enabled = policy.get('enabled') is True and bool(scopes)
            for scope in scopes:
                if (not isinstance(scope, dict) or scope.get('saveOnlyVerified') is not True
                        or not isinstance(scope.get('salesOrgId'), str) or not scope['salesOrgId'].strip()
                        or not isinstance(scope.get('transactionTypeId'), str) or not scope['transactionTypeId'].strip()):
                    raise ValueError('unverified scope')
            return {'enabled': enabled, 'scopes': scopes, 'control': 'local-policy-v1'}
        except (OSError, ValueError, TypeError, AttributeError):
            return {'enabled': False, 'scopes': [], 'control': 'local-policy-invalid'}
    enabled = os.environ.get('YONYOU_ENABLE_SAVE') == '1' and os.environ.get('YONYOU_SAVE_BEHAVIOR_VERIFIED') == '1'
    return {'enabled': enabled, 'scopes': None, 'control': 'legacy-environment'}


def require_save_allowed(state, payload):
    policy = save_policy(state)
    if not policy['enabled']: raise ValueError('真实保存尚未开放，请检查本机保存配置')
    scopes = policy['scopes']
    if scopes is not None and not any(s['salesOrgId'] == str(payload.get('salesOrgId', ''))
                                     and s['transactionTypeId'] == str(payload.get('transactionTypeId', '')) for s in scopes):
        raise ValueError('当前组织或交易类型尚未开放保存，请使用已验证的普通销售配置')


def save_status(state):
    policy = save_policy(state)
    return {'liveSaveEnabled': policy['enabled'], 'saveControl': policy['control'],
            'saveScopes': None if policy['scopes'] is None else [
                {k: s.get(k, '') for k in ('salesOrgId', 'transactionTypeId', 'salesOrgName', 'transactionTypeName')}
                for s in policy['scopes']]}
