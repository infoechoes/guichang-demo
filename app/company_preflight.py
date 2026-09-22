"""Read-only deployment inspection. Never request tokens or call upstream ERP."""
import argparse
import hashlib
import http.client
import ipaddress
import json
import re
from pathlib import Path
from urllib.parse import urlsplit


def status_request(port, path, host):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
    try:
        connection.request('GET', path, headers={'Host': host})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError('Unexpected local response')
        raw = response.read(32769)
        if len(raw)>32768:
            raise ValueError('Local response too large')
        return json.loads(raw)
    finally:
        connection.close()


def check(root, offline=False, request=status_request):
    root = Path(root).resolve()
    checks = []
    def add(name, ok, detail):
        checks.append({'check': name, 'ok': bool(ok), 'detail': detail})
    required = ('runtime/python.exe', 'runtime/python312._pth', 'app/multiuser_server.py', 'app/multiuser-control.ps1',
                'yonyou/server.py', 'web/index.html', 'python-deps/xlrd/__init__.py')
    for name in required:
        add(name, (root/name).is_file(), 'Required deployment file')
    config = {}
    try:
        config = json.loads((root/'multiuser-config.json').read_text(encoding='utf-8-sig'))
        parsed = urlsplit(config.get('origin', ''))
        valid = (parsed.scheme=='https' and bool(parsed.hostname) and parsed.path in ('','/') and not parsed.query
                 and not parsed.fragment and not parsed.username and parsed.hostname not in ('orders.example.com', 'example.com'))
        valid = valid and not (parsed.hostname or '').lower().endswith('.invalid') and 'replace' not in (parsed.hostname or '').lower()
        valid = valid and ipaddress.ip_address(config.get('listen', '127.0.0.1')).is_loopback
        valid = valid and not any(config.get(k) for k in ('tlsCert','tlsKey'))
        valid = valid and config.get('trustedProxy')==['127.0.0.1']
        add('company_origin', valid, 'Company HTTPS origin + same-machine proxy + loopback listener required')
    except (ValueError, OSError, TypeError, AttributeError):
        add('company_origin', False, 'Create multiuser-config.json from the company template')
    try:
        manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
        mismatches=[]
        if not isinstance(manifest.get('files'),dict) or any(name not in manifest['files'] for name in required):
            raise ValueError('Required files missing from manifest')
        for name,digest in manifest['files'].items():
            if not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest):
                raise ValueError('Invalid manifest digest')
            # Runtime policy becomes tenant-owned after install; report separately.
            if name=='yonyou/local-state/save-policy.json':continue
            target=(root/name).resolve()
            if not target.is_relative_to(root) or not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest()!=digest:
                mismatches.append(name)
        add('manifest', not mismatches, {'mismatchCount':len(mismatches)})
    except (ValueError,OSError,KeyError,TypeError,AttributeError):
        add('manifest',False,'Manifest missing or invalid')
    if not offline:
        try:
            status=request(4193,'/api/status','127.0.0.1:4193')
            expected=status.get('appId')=='guichang-yonyou-import-demo'
            add('existing_connector', expected and status.get('configured') and status.get('multiuserOrderIdentity') and status.get('submitSupported') and status.get('liveSaveEnabled'),
                {k:status.get(k) for k in ('configured','multiuserOrderIdentity','submitSupported','liveSaveEnabled')})
        except (OSError,ValueError,http.client.HTTPException):
            add('existing_connector',False,'Local connector unavailable; no restart attempted')
        try:
            origin=urlsplit(config.get('origin',''))
            status=request(4195,'/api/session',origin.netloc)
            add('operator_service',status.get('multiUser') is True and status.get('initialized') is True,{'initialized':bool(status.get('initialized'))})
        except (OSError,ValueError,http.client.HTTPException):
            add('operator_service',False,'Local service not ready or port occupied; no process stopped')
    return {'ready':all(c['ok'] for c in checks),'offline':offline,'checks':checks,
            'limitations':'Public DNS/TLS, gateway access, ACLs and real-order acceptance require company IT; this check makes no upstream ERP requests.'}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--offline',action='store_true')
    args=parser.parse_args()
    result=check(args.root,args.offline)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(0 if result['ready'] else 1)
