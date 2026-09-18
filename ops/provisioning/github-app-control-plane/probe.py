#!/usr/bin/env python3
import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

CONFIG = Path("/etc/andy-github-app/app.env")
INSTALLATION_CONFIG = Path("/etc/andy-github-app/installation.env")
API = "https://api.github.com"
API_VERSION = "2026-03-10"
TARGET_OWNER = "escossio"

def load_env(path):
    data = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        data[k.strip()] = v.strip().strip('"').strip("'")
    return data

def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

def make_jwt(client_id, key_path):
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {"iat": now - 60, "exp": now + 540, "iss": client_id}
    h = b64url(json.dumps(header, separators=(",", ":")).encode())
    p = b64url(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{h}.{p}".encode()
    key = serialization.load_pem_private_key(Path(key_path).read_bytes(), password=None)
    sig = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{h}.{p}.{b64url(sig)}"

def request_json(path, token, method="GET", body=None):
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "andy-github-app-agt01-probe",
    }
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as response:
        raw = response.read()
        return response.status, (json.loads(raw) if raw else None)

def main():
    cfg = load_env(CONFIG)
    app_id = cfg["GITHUB_APP_ID"]
    client_id = cfg["GITHUB_APP_CLIENT_ID"]
    key_path = cfg["GITHUB_APP_PRIVATE_KEY_PATH"]

    token = make_jwt(client_id, key_path)
    status, app = request_json("/app", token)
    print(f"APP_AUTH_STATUS={status}")
    print(f"APP_ID_EXPECTED={app_id}")
    print(f"APP_ID_RETURNED={app.get('id')}")
    print(f"APP_SLUG={app.get('slug')}")
    print(f"APP_OWNER={((app.get('owner') or {}).get('login'))}")

    if str(app.get("id")) != str(app_id):
        raise RuntimeError("Authenticated App ID does not match configured App ID")

    status, installs = request_json("/app/installations", token)
    matches = [
        item for item in installs
        if str(((item.get("account") or {}).get("login")) or "").lower()
        == TARGET_OWNER.lower()
    ]
    print(f"INSTALLATIONS_STATUS={status}")
    print(f"INSTALLATIONS_TOTAL={len(installs)}")
    print(f"ESCOSSIO_MATCHES={len(matches)}")

    if len(matches) != 1:
        raise RuntimeError("Expected exactly one escossio installation")

    inst = matches[0]
    installation_id = int(inst["id"])
    print(f"INSTALLATION_ID={installation_id}")
    print(f"INSTALLATION_TARGET={((inst.get('account') or {}).get('login'))}")
    print(f"REPOSITORY_SELECTION={inst.get('repository_selection')}")

    status, access = request_json(
        f"/app/installations/{installation_id}/access_tokens",
        token,
        method="POST",
        body={},
    )
    installation_token = access["token"]
    print(f"INSTALLATION_TOKEN_STATUS={status}")
    print(f"INSTALLATION_TOKEN_EXPIRES_AT={access.get('expires_at')}")
    print("INSTALLATION_TOKEN_PERSISTED=NO")

    status, repos = request_json(
        "/installation/repositories?per_page=100",
        installation_token,
    )
    names = sorted(r.get("full_name") for r in repos.get("repositories", []))
    print(f"REPOSITORY_LIST_STATUS={status}")
    print(f"REPOSITORY_COUNT={len(names)}")

    print("ATTENTION_ROUTER_ACCESS=" + ("YES" if "escossio/attention-router" in names else "NO"))
    print("ANDY_ANDROID_ACCESS=" + ("YES" if "escossio/andy-android" in names else "NO"))
    print("REPOSITORIES=" + ",".join(names))

    INSTALLATION_CONFIG.write_text(
        f"GITHUB_APP_INSTALLATION_ID={installation_id}\n"
        f"GITHUB_APP_INSTALLATION_OWNER={TARGET_OWNER}\n"
    )
    os.chmod(INSTALLATION_CONFIG, 0o644)
    print(f"INSTALLATION_CONFIG_WRITTEN={INSTALLATION_CONFIG}")
    print("GITHUB_APP_PROBE=PASS")

if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(body).get("message", "")
        except Exception:
            message = ""
        print("GITHUB_APP_PROBE=FAIL")
        print(f"HTTP_STATUS={exc.code}")
        print(f"GITHUB_MESSAGE={message[:200]}")
        raise SystemExit(2)
    except Exception as exc:
        print("GITHUB_APP_PROBE=FAIL")
        print(f"ERROR_TYPE={type(exc).__name__}")
        print(f"ERROR={str(exc)[:200]}")
        raise SystemExit(3)
