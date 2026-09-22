"""Credential preflight for the local admin window; never emits a token."""
import json
from core import YonSuite, YonSuiteError


def check(client=None):
    try:
        (client or YonSuite()).token()
        return {'ready': True, 'message': '用友鉴权通过'}
    except YonSuiteError as error:
        if error.diagnostic.get('category') == 'auth_rejected':
            message = ('用友签名校验失败，请核对同一应用的 App Key / App Secret。'
                       if '签名' in error.diagnostic.get('message','')
                       else '用友应用鉴权失败，请核对应用凭证及有效状态。')
        else:
            message = '用友鉴权请求未完成，请检查网络后重试。'
        return {'ready': False, 'message': message}
    except Exception:
        return {'ready': False, 'message': '用友鉴权未通过，请检查凭证和连接配置。'}


if __name__ == '__main__':
    result = check()
    print(json.dumps(result, ensure_ascii=False), flush=True)
    raise SystemExit(0 if result['ready'] else 1)
