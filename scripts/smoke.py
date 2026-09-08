import json
import os
import urllib.request


BASE = "http://127.0.0.1:18100"


def post(path: str, payload: dict) -> dict:
    headers = {"content-type": "application/json"}
    if path.startswith("/api/v1/admin/") and os.environ.get("ADMIN_TOKEN"):
        headers["authorization"] = f"Bearer {os.environ['ADMIN_TOKEN']}"
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read())


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=10) as response:
        return json.loads(response.read())


def scenario(name: str, payload: dict) -> None:
    result = post("/api/v1/ingress/synthetic/events", payload)
    print(
        f"{name}: status={result['status']} state={result['state']} "
        f"source={result['source']} interaction={result['interaction_id']}"
    )


if __name__ == "__main__":
    print(get("/health/ready"))
    scenario(
        "mae",
        {
            "schema_version": "synthetic-1",
            "synthetic_event_id": "smoke-mae",
            "event_type": "message",
            "contact_id": "contact_mae",
            "contact_name": "Mãe Sintética",
            "relationship_category": "family_core",
            "text": "Fixture sintética: urgência familiar.",
        },
    )
    scenario(
        "recrutador",
        {
            "schema_version": "synthetic-1",
            "synthetic_event_id": "smoke-recrutador",
            "event_type": "call",
            "contact_id": "unknown_recruiter",
            "contact_name": "Recrutador Sintético",
            "relationship_category": "unknown",
            "active_context": "interview",
            "text": "Fixture sintética: entrevista.",
        },
    )
    scenario(
        "desconhecido",
        {
            "schema_version": "synthetic-1",
            "synthetic_event_id": "smoke-desconhecido",
            "event_type": "message",
            "contact_id": "unknown_contact",
            "contact_name": "Contato Sintético",
            "relationship_category": "unknown",
            "text": "Fixture sintética: contato geral.",
        },
    )
