"""Synthetic V0. CLI over Unix socket; no server, credentials in mounted files."""

import argparse
import json
import re
import unicodedata
import uuid
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


def normalize(text):
    return "".join(
        c for c in unicodedata.normalize("NFD", text.casefold()) if unicodedata.category(c) != "Mn"
    ).strip()


def connect(write=False):
    role = "writer" if write else "reader"
    return psycopg.connect(
        host="/socket",
        dbname="memory_lab",
        user="lab_" + role,
        password=Path("/run/secrets/" + role).read_text().strip(),
        connect_timeout=3,
        options="-c statement_timeout=3000 -c lock_timeout=1000",
        row_factory=dict_row,
    )


# Closed vocabulary, no subjects or fixture values. Interpretation never queries SQL.
PATTERNS = (
    (
        "vehicle.color",
        r"(?:qual (?:e )?a cor do (?:carro|veiculo)|de que cor (?:e|esta) o (?:carro|veiculo)) de (.+)",
    ),
    (
        "employment.company",
        r"(?:onde (.+) trabalha|(?:qual (?:e )?a empresa|em que empresa trabalha) de (.+))",
    ),
)


def interpret(question):
    q = normalize(question).rstrip("?.! ")
    for attribute, pattern in PATTERNS:
        match = re.fullmatch(pattern, q)
        if match:
            return next(x for x in match.groups() if x), attribute
    return None


def register(subject, attribute, value, excerpt, origin="synthetic-manual", expected=None):
    if attribute not in {"vehicle.color", "employment.company"}:
        raise ValueError("UNSUPPORTED_ATTRIBUTE")
    if not all(isinstance(x, str) and x.strip() for x in (subject, value, excerpt, origin)):
        raise ValueError("EMPTY_INFORMATION")
    with connect(True) as db:
        row = db.execute(
            "SELECT id,current_revision FROM information WHERE subject=%s AND attribute=%s FOR UPDATE",
            (subject.strip(), attribute),
        ).fetchone()
        if expected is None and row:
            raise ValueError("ALREADY_REGISTERED_USE_CORRECTION")
        if expected is not None and (not row or row["current_revision"] != expected):
            raise ValueError("REVISION_CONFLICT")
        iid = row["id"] if row else uuid.uuid4()
        number = row["current_revision"] + 1 if row else 1
        eid = uuid.uuid4()
        db.execute(
            "INSERT INTO evidence(id,excerpt,origin) VALUES(%s,%s,%s)", (eid, excerpt, origin)
        )
        if not row:
            db.execute(
                "INSERT INTO information VALUES(%s,%s,%s,%s)",
                (iid, subject.strip(), attribute, number),
            )
        db.execute(
            "INSERT INTO revision(information_id,number,value,evidence_id) VALUES(%s,%s,%s,%s)",
            (iid, number, value, eid),
        )
        if row:
            db.execute("UPDATE information SET current_revision=%s WHERE id=%s", (number, iid))
        return {"information_id": str(iid), "evidence_id": str(eid), "revision": number}


def retrieve(subject, attribute, history=False):
    with connect() as db:
        rows = db.execute(
            """SELECT i.id AS information_id,i.subject,i.attribute,r.number AS revision,
          r.value,e.id AS evidence_id,e.excerpt,e.origin,e.recorded_at,
          r.number=i.current_revision AS current
          FROM information i JOIN revision r ON r.information_id=i.id
          JOIN evidence e ON e.id=r.evidence_id WHERE i.attribute=%s
          AND (%s OR r.number=i.current_revision) ORDER BY i.id,r.number""",
            (attribute, history),
        ).fetchall()
    return [r for r in rows if normalize(r["subject"]) == normalize(subject)]


def present(rows, correlation):
    result = {
        "contract_version": "1",
        "status": "INSUFFICIENT_EVIDENCE",
        "answer": None,
        "evidence": [],
        "correlation_id": correlation,
    }
    if not rows:
        return result
    result["evidence"] = [
        {
            k: str(r[k]) if k in ("evidence_id", "information_id", "recorded_at") else r[k]
            for k in (
                "evidence_id",
                "information_id",
                "revision",
                "excerpt",
                "origin",
                "recorded_at",
            )
        }
        for r in rows
    ]
    if len(rows) != 1:
        result["status"] = "AMBIGUOUS"
        return result
    r = rows[0]
    result["status"] = "ANSWERED"
    result["answer"] = (
        f"O carro de {r['subject']} é {r['value']}."
        if r["attribute"] == "vehicle.color"
        else f"{r['subject']} trabalha na empresa {r['value']}."
    )
    return result


def answer(question):
    correlation = str(uuid.uuid4())
    try:
        query = interpret(question)
        return present(retrieve(*query) if query else [], correlation)
    except psycopg.Error:
        return {
            "contract_version": "1",
            "status": "UNAVAILABLE",
            "answer": None,
            "evidence": [],
            "correlation_id": correlation,
        }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("operation", choices=["register", "correct", "query", "current", "history"])
    p.add_argument("payload", help="JSON object")
    args = p.parse_args()
    data = json.loads(args.payload)
    if args.operation == "query":
        result = answer(data["question"])
    elif args.operation in ("register", "correct"):
        if args.operation == "correct" and "expected" not in data:
            raise ValueError("EXPECTED_REVISION_REQUIRED")
        result = register(**data)
    else:
        result = retrieve(data["subject"], data["attribute"], args.operation == "history")
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
