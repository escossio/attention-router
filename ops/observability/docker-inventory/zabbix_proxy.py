#!/usr/bin/env python3
"""Small allowlisted, local Docker API view for Zabbix Agent2."""
import http.client
import json
import os
import re
import socket
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn, UnixStreamServer
from urllib.parse import parse_qs, urlsplit

SOCKET = '/run/andy-zabbix-docker/docker.sock'
UPSTREAM = '/var/run/docker.sock'
PREFIX = re.compile(r'^/v\d+(?:\.\d+)?(?=/|$)')
IDENT = r'[a-zA-Z0-9_.:-]+'
ALLOWED = [
    re.compile(r'^/_ping$'), re.compile(r'^/version$'), re.compile(r'^/info$'),
    re.compile(r'^/containers/json$'),
    re.compile(r'^/containers/' + IDENT + r'/json$'),
    re.compile(r'^/containers/' + IDENT + r'/stats$'),
    re.compile(r'^/networks$'), re.compile(r'^/networks/' + IDENT + r'$'),
    re.compile(r'^/images/json$'), re.compile(r'^/system/df$'),
]
SENSITIVE_LABEL = re.compile(r'(?:secret|token|pass(?:word)?|credential|api.?key)', re.I)


def labels_for_inventory(labels):
    return {key: value for key, value in (labels or {}).items()
            if not SENSITIVE_LABEL.search(key)}


def project_response(path, payload):
    if path == '/containers/json':
        return [{'Id': row.get('Id'), 'Names': row.get('Names') or [],
                 'Image': row.get('Image'), 'State': row.get('State'),
                 'Labels': labels_for_inventory(row.get('Labels'))}
                for row in payload]
    if re.fullmatch(r'/containers/' + IDENT + r'/json', path):
        config = payload.get('Config') or {}
        host = payload.get('HostConfig') or {}
        settings = payload.get('NetworkSettings') or {}
        state = payload.get('State') or {}
        return {'Id': payload.get('Id'), 'Name': payload.get('Name'),
                'Created': payload.get('Created'), 'Image': payload.get('Image'),
                'RestartCount': payload.get('RestartCount'),
                'Config': {'Image': config.get('Image'),
                           'Labels': labels_for_inventory(config.get('Labels')),
                           'ExposedPorts': config.get('ExposedPorts')},
                'HostConfig': {'RestartPolicy': host.get('RestartPolicy'),
                               'PortBindings': host.get('PortBindings')},
                'NetworkSettings': {'Networks': settings.get('Networks'),
                                    'Ports': settings.get('Ports')},
                'State': {'Status': state.get('Status'), 'Running': state.get('Running'),
                          'StartedAt': state.get('StartedAt'),
                          'Health': {'Status': (state.get('Health') or {}).get('Status')}}}
    if re.fullmatch(r'/networks/' + IDENT, path):
        return {key: payload.get(key) for key in
                ('Id', 'Name', 'Driver', 'Scope', 'IPAM', 'Internal',
                 'Attachable', 'Ingress', 'Containers')}
    return payload


class DockerConnection(http.client.HTTPConnection):
    def __init__(self):
        super().__init__('localhost', timeout=20)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(20)
        self.sock.connect(UPSTREAM)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.forward()

    def do_HEAD(self):
        self.forward()

    def do_POST(self):
        self.send_error(405, 'Read-only Docker API')

    do_PUT = do_PATCH = do_DELETE = do_POST

    def forward(self):
        self.close_connection = True
        parsed = urlsplit(self.path)
        path = PREFIX.sub('', parsed.path)
        if not any(rule.fullmatch(path) for rule in ALLOWED):
            self.send_error(403, 'Docker API route denied')
            return
        query = parse_qs(parsed.query, keep_blank_values=True)
        if path.endswith('/stats') and (query.get('stream', ['false']) != ['false']):
            self.send_error(403, 'Streaming stats denied')
            return
        upstream = DockerConnection()
        try:
            upstream.request(self.command, self.path, headers={'Host': 'localhost'})
            response = upstream.getresponse()
            body = response.read(16 * 1024 * 1024 + 1)
            if len(body) > 16 * 1024 * 1024:
                self.send_error(502, 'Docker response too large')
                return
            if self.command == 'GET' and response.status == 200 and (
                path == '/containers/json' or
                re.fullmatch(r'/containers/' + IDENT + r'/json', path) or
                re.fullmatch(r'/networks/' + IDENT, path)):
                body = json.dumps(project_response(path, json.loads(body)),
                                  separators=(',', ':')).encode()
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in {'transfer-encoding', 'connection', 'content-length'}:
                    self.send_header(key, value)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)
        except (OSError, http.client.HTTPException) as exc:
            self.send_error(502, type(exc).__name__)
        finally:
            upstream.close()


class Server(ThreadingMixIn, UnixStreamServer):
    daemon_threads = True
    request_queue_size = 128


if __name__ == '__main__':
    os.umask(0o007)
    Path(SOCKET).unlink(missing_ok=True)
    with Server(SOCKET, Handler) as server:
        server.serve_forever()
