#!/usr/bin/env python3
from probe import CONFIG, load_env, make_jwt, request_json

cfg = load_env(CONFIG)
token = make_jwt(
    cfg["GITHUB_APP_CLIENT_ID"],
    cfg["GITHUB_APP_PRIVATE_KEY_PATH"],
)

status, app = request_json("/app", token)
print(f"APP_STATUS={status}")
print(f"APP_SLUG={app.get('slug')}")
print("APP_EVENTS=" + ",".join(sorted(app.get("events") or [])))
permissions = app.get("permissions") or {}
print("APP_PERMISSIONS=" + ",".join(
    f"{key}:{permissions[key]}" for key in sorted(permissions)
))

status, hook = request_json("/app/hook/config", token)
print(f"HOOK_CONFIG_STATUS={status}")
print(f"WEBHOOK_URL={hook.get('url') or ''}")
print(f"WEBHOOK_CONTENT_TYPE={hook.get('content_type') or ''}")
print(f"WEBHOOK_INSECURE_SSL={hook.get('insecure_ssl')}")
secret = hook.get("secret")
print("WEBHOOK_SECRET_CONFIGURED=" + ("YES" if secret else "NO"))
