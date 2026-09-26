"""Short isolated Apache fixture; only synthetic credentials and loopback listeners."""

import base64
import shutil
import hashlib
import http.client
import http.server
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
from render import render
from pathlib import Path

if os.geteuid() != 0 or not shutil.which("apache2") or not shutil.which("htpasswd"):
    raise SystemExit("Run as root on Debian with apache2 and apache2-utils installed")

records = []


class Backend(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        upgrade = self.headers.get("Upgrade", "").lower() == "websocket"
        records.append(
            {
                "authorization_present": bool(self.headers.get("Authorization")),
                "upgrade_present": upgrade,
            }
        )
        if self.path == "/api/live/ws" and upgrade:
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            key = self.headers["Sec-WebSocket-Key"]
            self.send_header(
                "Sec-WebSocket-Accept",
                base64.b64encode(
                    hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
                ).decode(),
            )
            self.end_headers()
        else:
            self.send_response(400 if self.path == "/api/live/ws" else 200)
            self.send_header("Content-Length", "0")
            self.end_headers()
        self.close_connection = True

    def log_message(self, *args):
        pass


backend = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Backend)
threading.Thread(target=backend.serve_forever, daemon=True).start()
backend_port = backend.server_port
root = Path(tempfile.mkdtemp(prefix="grafana-apache-fixture-"))
root.chmod(0o755)
subprocess.run(
    ["htpasswd", "-cbB", str(root / "synthetic.htpasswd"), "synthetic", "synthetic-fixture-only"],
    check=True,
    capture_output=True,
)
(root / "synthetic.htpasswd").chmod(0o644)
modules = [
    "mpm_prefork",
    "authn_core",
    "authn_file",
    "authz_core",
    "authz_user",
    "auth_basic",
    "proxy",
    "proxy_http",
    "proxy_wstunnel",
    "headers",
]
results = []
for fixed in [False, True]:
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    pre = "\n".join(f"LoadModule {m}_module /usr/lib/apache2/modules/mod_{m}.so" for m in modules)
    vhost = render("synthetic.invalid", str(root / "synthetic.htpasswd"), backend_port)
    vhost = vhost.replace("<VirtualHost *:80>", f"<VirtualHost 127.0.0.1:{port}>").replace(
        "${APACHE_LOG_DIR}", str(root)
    )
    if not fixed:
        vhost = "\n".join(
            line
            for line in vhost.splitlines()
            if "RequestHeader unset Authorization" not in line
            and "ProxyPass /api/live/ws" not in line
            and "ProxyPassReverse /api/live/ws" not in line
        )
    config = f"""ServerRoot /etc/apache2
{pre}
Listen 127.0.0.1:{port}
ServerName synthetic.invalid
PidFile {root}/apache.pid
ErrorLog {root}/error.log
User www-data
Group www-data
StartServers 1
MinSpareServers 1
MaxSpareServers 2
MaxRequestWorkers 3
{vhost}
"""
    path = root / "apache.conf"
    path.write_text(config)
    test = subprocess.run(["apache2", "-t", "-f", str(path)], capture_output=True, text=True)
    assert test.returncode == 0, test.stderr
    proc = subprocess.Popen(
        ["apache2", "-f", str(path), "-DFOREGROUND"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        for _ in range(40):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        for mode in ["unauthenticated_http", "authenticated_http", "authenticated_ws"]:
            headers = {}
            if mode.startswith("authenticated"):
                headers["Authorization"] = (
                    "Basic " + base64.b64encode(b"synthetic:synthetic-fixture-only").decode()
                )
            if mode.endswith("_ws"):
                headers.update(
                    {
                        "Upgrade": "websocket",
                        "Connection": "Upgrade",
                        "Sec-WebSocket-Version": "13",
                        "Sec-WebSocket-Key": base64.b64encode(os.urandom(16)).decode(),
                    }
                )
            before = len(records)
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", "/api/live/ws" if mode.endswith("_ws") else "/", headers=headers)
            r = c.getresponse()
            item = {
                "candidate": fixed,
                "case": mode,
                "status": r.status,
                "upgrade": r.getheader("Upgrade"),
                "connection": r.getheader("Connection"),
                "backend_observed": records[-1] if len(records) > before else None,
            }
            if mode.endswith("_ws"):
                expected = base64.b64encode(
                    hashlib.sha1(
                        (headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
                    ).digest()
                ).decode()
                item["accept_valid"] = r.getheader("Sec-WebSocket-Accept") == expected
            results.append(item)
            print(json.dumps(item), flush=True)
            c.close()
    finally:
        proc.terminate()
        proc.wait(timeout=5)
backend.shutdown()
assert [r["status"] for r in results] == [401, 200, 400, 401, 200, 101]
assert results[1]["backend_observed"]["authorization_present"] is True
assert results[2]["backend_observed"]["upgrade_present"] is False
assert results[4]["backend_observed"]["authorization_present"] is False
assert results[5]["backend_observed"] == {"authorization_present": False, "upgrade_present": True}
assert results[5]["accept_valid"] is True
shutil.rmtree(root)
print("PASS: Apache authentication, HTTP, WebSocket and upstream credential isolation")
