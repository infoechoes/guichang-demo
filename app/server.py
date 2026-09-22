"""Portable, loopback-only web + OCR server. No Node, API key, or external requests."""
import argparse
import hashlib
import json
import mimetypes
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'python-deps'))
from ocr_server import Handler as OcrHandler, ThreadingHTTPServer, APP_VERSION

APP_ID = 'guichang-song-image-excel'
WEB = ROOT / 'web'
RELEASE = json.loads((ROOT / 'manifest.json').read_text(encoding='utf-8')).get('releaseId', APP_VERSION)


class Handler(OcrHandler):
    def allowed(self):
        host = self.headers.get('Host', '')
        allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        return host in allowed and self.headers.get('Origin') in (None, *('http://' + h for h in allowed))

    def do_GET(self):
        if not self.allowed():
            self.send_json(403, {'error': 'Local access only'})
            return
        path = unquote(urlsplit(self.path).path)
        if path.startswith(('/api/workflow/', '/api/yonyou/')):
            return super().do_GET()
        if path == '/api/health':
            self.send_json(200, {'ready': True, 'local': True, 'engine': 'RapidOCR',
                                 'appId': APP_ID, 'root': str(ROOT), 'version': APP_VERSION, 'releaseId': RELEASE})
            return
        if path == '/':
            target = WEB / 'index.html'
        elif path.startswith(('/assets/', '/templates/')):
            target = (WEB / path.lstrip('/')).resolve()
            if not target.is_relative_to(WEB.resolve()):
                self.send_json(404, {'error': 'Not found'})
                return
        else:
            self.send_json(404, {'error': 'Not found'})
            return
        if not target.is_file():
            self.send_json(404, {'error': 'Not found'})
            return
        raw = target.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', mimetypes.guess_type(target.name)[0] or 'application/octet-stream')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        if not self.allowed():
            self.send_json(403, {'error': 'Local access only'})
            return
        super().do_POST()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=4190)
    args = parser.parse_args()
    print(f'Song demo ready: http://127.0.0.1:{args.port}/', flush=True)
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()
