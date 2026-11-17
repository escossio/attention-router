#!/usr/bin/env python3
"""Create a DISABLED tag-scoped Zabbix Telegram action with recovery operation."""
from provision import client

ACTION_NAME = "ANDY OPS | CPU and Tempo -> Telegram (problem + recovery)"
TELEGRAM_MEDIATYPE = "97"
ADMIN_USER = "1"


def main():
    api = client()
    found = api.call("action.get", {
        "filter": {"name": ACTION_NAME}, "output": ["actionid", "name", "status"]
    })
    if found:
        print("ACTION_ALREADY_CONFIGURED",found[0]["actionid"],
              "STATUS_DISABLED" if int(found[0]["status"]) else "STATUS_ENABLED")
        return
    msg = {
        "default_msg": 0,
        "mediatypeid": TELEGRAM_MEDIATYPE,
        "subject": "[ANDY OPS] PROBLEMA {EVENT.NAME}",
        "message": "Alerta: {EVENT.NAME}\nHost: {HOST.NAME}\n"
                   "Data: {EVENT.DATE} {EVENT.TIME}\n"
                   "Severidade: {EVENT.SEVERITY}\n"
                   "Detalhes: {EVENT.OPDATA}\n"
                   "Zabbix: https://zabbix.escossio.com/\n",
    }
    rec = {
        "default_msg": 0,
        "mediatypeid": TELEGRAM_MEDIATYPE,
        "subject": "[ANDY OPS] RESOLVIDO {EVENT.NAME}",
        "message": "RECUPERADO: {EVENT.NAME}\nHost: {HOST.NAME}\n"
                   "Resolvido: {EVENT.RECOVERY.DATE} {EVENT.RECOVERY.TIME}\n"
                   "Duracao: {EVENT.DURATION}\n"
                   "Zabbix: https://zabbix.escossio.com/\n",
    }
    result = api.call("action.create", {
        "name": ACTION_NAME,
        "eventsource": 0,
        "status": 1,
        "esc_period": "1m",
        "filter": {"evaltype": 0, "conditions": [
            {"conditiontype": 25, "operator": 0, "value": "ops_alert"}]},
        "operations": [{
            "operationtype": 0, "esc_step_from": 1, "esc_step_to": 1,
            "opmessage_usr": [{"userid": ADMIN_USER}],
            "opmessage": msg,
        }],
        "recovery_operations": [{
            "operationtype": 0, "opmessage_usr": [{"userid": ADMIN_USER}],
            "opmessage": rec,
        }],
    })
    print("TELEGRAM_ACTION_CREATED_DISABLED",result["actionids"][0])
    print("NO_MESSAGES_WILL_BE_SENT_UNTIL_MEDIA_TYPE_USER_DESTINATION_AND_ACTION_ARE_ENABLED")


if __name__ == "__main__":
    main()
