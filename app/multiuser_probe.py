"""Check the local service while validating the configured TLS hostname."""
import argparse
import http.client
import json
import socket
import ssl
from urllib.parse import urlsplit


def probe(listen, port, origin, ca_file=None, tls=False):
    parsed = urlsplit(origin)
    connection = http.client.HTTPConnection(listen, port, timeout=2)
    try:
        if tls:
            context = ssl.create_default_context(cafile=ca_file)
            sock = socket.create_connection((listen, port), timeout=2)
            try:
                connection.sock = context.wrap_socket(sock, server_hostname=parsed.hostname)
            except Exception:
                sock.close()
                raise
        connection.request('GET', '/api/session', headers={'Host': parsed.netloc})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError('Service check returned HTTP ' + str(response.status))
        result = json.loads(response.read(16384))
        if result.get('multiUser') is not True:
            raise ValueError('Unexpected service at the configured port')
        return {'ready': True, 'initialized': bool(result.get('initialized'))}
    finally:
        connection.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--listen', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=4195)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--ca-file')
    parser.add_argument('--tls', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(probe(args.listen, args.port, args.origin, args.ca_file, args.tls)))
    except Exception:
        print('Service or certificate validation is not ready.')
        raise SystemExit(1)
