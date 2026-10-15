#!/usr/bin/env python3
"""Secure interactive Telegram onboarding for Zabbix ROC.

No bot tokens on shell command line, repository, console output, or ChatGPT.
Credential file stays on AGT, root-only. Action remains disabled until verified.
"""
import getpass
import json
import os
from pathlib import Path
import re
import sys
import urllib.request

from provision import client

CONFIG = Path("/etc/andy-ops-network/telegram-channel.json")
MEDIA_ID = "97"
ACTION_ID = "7"
USER_ID = "1"


def read_config():
    if not CONFIG.is_file():
        raise RuntimeError("CHANNEL_NOT_CAPTURED")
    stat = CONFIG.stat()
    if stat.st_uid != 0 or stat.st_mode & 0o077:
        raise RuntimeError("CHANNEL_FILE_MUST_BE_ROOT_ONLY_0600")
    settings = json.loads(CONFIG.read_text())
    token, chat = settings.get("bot_token", ""), settings.get("chat_id", "")
    if not re.fullmatch(r"[0-9]{5,15}:[A-Za-z0-9_-]{25,}", token):
        raise RuntimeError("INVALID_BOT_TOKEN_FORMAT")
    if not re.fullmatch(r"(?:-?[0-9]{4,24}|@[A-Za-z0-9_]{5,32})(?::[0-9]{1,12})?", chat):
        raise RuntimeError("INVALID_CHAT_ID_FORMAT")
    return token, chat


def capture():
    if os.geteuid() != 0 or not sys.stdin.isatty():
        raise RuntimeError("CAPTURE_REQUIRES_ROOT_INTERACTIVE_TERMINAL")
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    token = getpass.getpass("Telegram Bot Token (oculto): ").strip()
    chat = getpass.getpass("Telegram Chat ID ou @grupo (oculto): ").strip()
    temp = CONFIG.with_name(CONFIG.name + ".tmp." + str(os.getpid()))
    with temp.open("x") as f:
        json.dump({"bot_token": token, "chat_id": chat}, f)
        f.write("\n")
    os.chmod(temp, 0o600)
    temp.replace(CONFIG)
    read_config()
    print("CHANNEL_STORED_PRIVATELY")
    print("NEXT: sudo python3 /usr/local/lib/andy-ops-alerts/telegram_connect.py configure")


def telegram_api(token, method, payload):
    endpoint = "https://api.telegram.org/bot" + token + "/" + method
    request = urllib.request.Request(
        endpoint, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=9) as response:
            obj = json.load(response)
    except Exception:
        raise RuntimeError("TELEGRAM_API_CONNECTIVITY_OR_AUTH_FAILED") from None
    if obj.get("ok") is not True:
        raise RuntimeError("TELEGRAM_API_REJECTED_REQUEST")
    return obj.get("result", {})


def check_bot(token, chat):
    info = telegram_api(token, "getMe", {})
    telegram_api(token, "getChat", {"chat_id": chat.split(":", 1)[0]})
    print("BOT_VALIDATED", "username_present" if info.get("username") else "name_unavailable")
    print("CHAT_DESTINATION_VALIDATED")


def configure():
    token, chat = read_config()
    check_bot(token, chat)
    api = client()
    result = api.call("mediatype.get", {
        "mediatypeids": [MEDIA_ID],
        "output": "extend",
        "selectParameters": "extend"
    })
    if len(result) != 1 or result[0]["name"] != "Telegram":
        raise RuntimeError("UNEXPECTED_TELEGRAM_MEDIATYPE")
    params = result[0].get("parameters", [])
    keys = {p["name"] for p in params}
    if not {"api_token", "api_chat_id", "api_parse_mode"} <= keys:
        raise RuntimeError("TELEGRAM_WEBHOOK_PARAMS_INCOMPLETE")
    updates = []
    for p in params:
        key = p["name"]
        value = token if key == "api_token" else (
            "HTML" if key == "api_parse_mode" else p["value"]
        )
        updates.append({"name": key, "value": value})
    api.call("mediatype.update", {
        "mediatypeid": MEDIA_ID, "parameters": updates, "status": 0
    })
    users = api.call("user.get", {
        "userids": [USER_ID], "output": ["userid", "username"], "selectMedias": "extend"
    })
    if len(users) != 1 or users[0]["username"] != "Admin":
        raise RuntimeError("TARGET_ZABBIX_USER_UNEXPECTED")
    media = [
        {k: v for k, v in entry.items() if k in
         {"mediatypeid", "sendto", "active", "severity", "period"}}
        for entry in users[0].get("medias", []) if entry.get("mediatypeid") != MEDIA_ID
    ]
    media.append({"mediatypeid": MEDIA_ID, "sendto": chat,
                  "active": 0, "severity": 63, "period": "1-7,00:00-24:00"})
    api.call("user.update", {"userid": USER_ID, "medias": media})
    print("ZABBIX_TELEGRAM_MEDIA_AND_RECIPIENT_CONFIGURED")
    print("ALERT_ACTION_REMAINS_DISABLED")
    print("NEXT: confirm direct test, then activate Zabbix action")


def direct_test():
    token, chat = read_config()
    check_bot(token, chat)
    target, _, topic = chat.partition(":")
    payload = {"chat_id": target, "text":
        "ANDY OPS | TESTE DE CANAL\nBot e destinatario conectados. "
        "Proxima etapa: evento do Zabbix e recuperacao."}
    if topic: payload["message_thread_id"] = int(topic)
    telegram_api(token, "sendMessage", payload)
    print("TELEGRAM_DIRECT_TEST_ACCEPTED")


def status():
    api = client()
    action = api.call("action.get", {
        "actionids": [ACTION_ID], "output": ["actionid", "name", "status"]
    })
    print("ACTION", "DISABLED" if action and int(action[0]["status"]) else "ENABLED")
    print("PRIVATE_BOT_CONFIG_PRESENT", CONFIG.is_file())
    print("NO_TOKEN_DISCLOSED")


if __name__ == "__main__":
    try:
        modes = {"capture": capture, "configure": configure,
                 "direct-test": direct_test, "status": status}
        if len(sys.argv) != 2 or sys.argv[1] not in modes:
            raise RuntimeError("USAGE: telegram_connect.py capture|configure|direct-test|status")
        modes[sys.argv[1]]()
    except Exception as err:
        print("ERROR",str(err))
        raise SystemExit(1)
