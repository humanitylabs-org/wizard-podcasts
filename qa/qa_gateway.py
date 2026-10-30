"""Tiny QA gateway: /wizard-os-home/* -> the live shell (read-only GETs), everything else -> the staged app.
Usage: python3 qa_gateway.py <listen_port> <app_port> [shell_port]"""
import http.client
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN, APP = int(sys.argv[1]), int(sys.argv[2])
SHELL = int(sys.argv[3]) if len(sys.argv) > 3 else 8796


class H(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def go(self):
        shell = self.path.startswith('/wizard-os-home/')
        if shell and self.command != 'GET':
            self.send_response(403); self.send_header('Content-Length', '0'); self.end_headers(); return
        port = SHELL if shell else APP
        body = None
        n = int(self.headers.get('Content-Length') or 0)
        if n:
            body = self.rfile.read(n)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in ('host', 'origin', 'connection', 'accept-encoding')}
        headers['Host'] = f'127.0.0.1:{port}'
        if self.headers.get('Origin'):
            headers['Origin'] = f'http://127.0.0.1:{port}'
        c = http.client.HTTPConnection('127.0.0.1', port, timeout=60)
        c.request(self.command, self.path, body=body, headers=headers)
        r = c.getresponse()
        self.send_response(r.status)
        length = None
        for k, v in r.getheaders():
            if k.lower() in ('connection', 'transfer-encoding', 'keep-alive'):
                continue
            if k.lower() == 'content-length':
                length = v
            self.send_header(k, v)
        if length is None:
            self.close_connection = True
        self.end_headers()
        try:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        c.close()

    do_GET = do_POST = do_HEAD = go

    def log_message(self, *a):
        pass


ThreadingHTTPServer(('127.0.0.1', LISTEN), H).serve_forever()
