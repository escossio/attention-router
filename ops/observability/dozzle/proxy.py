#!/usr/bin/env python3
"""Read-only Docker API view for Dozzle; labels exist only in this view."""
import http.client
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import re
import socket
from socketserver import ThreadingMixIn, UnixStreamServer
from urllib.parse import urlsplit

MAP = Path('/etc/attention-router/docker-ops-classes.json')
SOCKET = '/run/andy-dozzle-view/docker.sock'

# GET/HEAD are not inherently safe on Docker API. Allow only explicitly
# required Dozzle inventory/log/metrics streams; deny archive/exec/attach.
VERSION = re.compile(r'^/v[0-9]+(?:\.[0-9]+)?(?=/|$)')
ALLOWED = [
    re.compile(r'^/_ping$'),
    re.compile(r'^/version$'),
    re.compile(r'^/info$'),
    re.compile(r'^/events$'),
    re.compile(r'^/containers/json$'),
    re.compile(r'^/containers/[A-Za-z0-9_.:-]+/(?:json|logs|stats)$'),
    re.compile(r'^/images/json$'),
    re.compile(r'^/networks$'),
]

def is_allowed_route(path):
    normalized = VERSION.sub('', path)
    return any(rule.fullmatch(normalized) for rule in ALLOWED)


class DockerConnection(http.client.HTTPConnection):
    def __init__(self):
        super().__init__('localhost', timeout=3600)
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect('/var/run/docker.sock')

def labels_view(payload, path):
    classes = json.loads(MAP.read_text())
    if path.endswith('/containers/json'):
        rows = payload
    elif re.search(r'/containers/[^/]+/json$', path):
        rows = [payload]
    else:
        return payload
    for row in rows:
        names = row.get('Names') or [row.get('Name', '')]
        name = names[0].lstrip('/') if names else ''
        entry = classes.get(name)
        if not entry:
            entry = {'group': 'Validation' if (row.get('Labels') or {}).get('com.docker.compose.oneoff') == 'True' else 'UNKNOWN_HOLD', 'display': name}
        target = row.get('Config', row)
        target.setdefault('Labels', {})
        target['Labels']['dev.dozzle.group'] = entry['group']
        target['Labels']['dev.dozzle.name'] = entry['display']
    return payload

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *args):
        pass
    def do_HEAD(self):
        self.forward()
    def do_GET(self):
        self.forward()
    def do_POST(self):
        self.send_error(405, 'Read-only Docker view')
    do_PUT = do_DELETE = do_PATCH = do_POST
    def forward(self):
        self.close_connection = True
        path = urlsplit(self.path).path
        if not is_allowed_route(path):
            self.send_error(403, 'Docker route not allowed')
            return
        upstream = DockerConnection()
        try:
            upstream.request(self.command, self.path, headers={'Host':'localhost'})
            response = upstream.getresponse()
            is_json = self.command == 'GET' and response.status == 200 and (path.endswith('/containers/json') or re.search(r'/containers/[^/]+/json$',path))
            body = json.dumps(labels_view(json.loads(response.read()),path),separators=(',',':')).encode() if is_json else None
            self.send_response(response.status, response.reason)
            for key,value in response.getheaders():
                if key.lower() not in {'transfer-encoding','connection','content-length'}:
                    self.send_header(key,value)
            self.send_header('Connection','close')
            if body is not None:
                self.send_header('Content-Length',str(len(body)))
            elif response.getheader('Content-Length'):
                self.send_header('Content-Length',response.getheader('Content-Length'))
            self.end_headers()
            if body is not None:
                self.wfile.write(body)
            elif self.command != 'HEAD':
                while True:
                    chunk=response.read1(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (BrokenPipeError,ConnectionResetError):
            pass
        except Exception as error:
            print(type(error).__name__,flush=True)
        finally:
            upstream.close()

class Server(ThreadingMixIn,UnixStreamServer):
    daemon_threads = True

if __name__=='__main__':
    os.umask(0o077)
    Path(SOCKET).unlink(missing_ok=True)
    with Server(SOCKET,Handler) as server:
        server.serve_forever()
