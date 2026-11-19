"""Deterministic contextual windows, with source membership and explicit replies."""

import hashlib
import json
import uuid

POLICY = "DIALOGUE_30M_480T_12_V1"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def segment_messages(rows, provider):
    result = []
    scopes = {(r["tenant_id"], r["source_account"], r["conversation_id"]) for r in rows}
    if len(scopes) > 1:
        raise ValueError("SEGMENT_SCOPE_MISMATCH")
    if not rows:
        return result
    rows = sorted(rows, key=lambda r: (r["sent_at"], r["source_message_id"], r["id"]))
    authors = {}
    fragments = []
    first = {}
    for r in rows:
        key = (
            r.get("sender_actor_id")
            or r.get("external_sender_key")
            or ("owner" if r["from_me"] else "unknown:" + r["id"])
        )
        authors.setdefault(key, len(authors) + 1)
        for part, (start, end) in enumerate(provider.fragments(r["index_text"])):
            f = {
                "message": r,
                "part": part,
                "start": start,
                "end": end,
                "role": "PRIMARY",
                "line": f"Autor {authors[key]}: " + r["index_text"][start:end],
            }
            fragments.append(f)
            first.setdefault(r["id"], f)
    current = []
    anchor = None

    def finish():
        nonlocal current, anchor
        if not current:
            return
        content = "\n".join(f["line"] for f in current)
        tenant, account, conversation = next(iter(scopes))
        stable = json.dumps([POLICY, tenant, account, conversation, anchor], ensure_ascii=False)
        provenance = [
            (
                f["message"]["id"],
                f["part"],
                f["start"],
                f["end"],
                f["role"],
                f["message"]["source_fingerprint"],
            )
            for f in current
        ]
        result.append(
            {
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, stable)),
                "text": content,
                "content_hash": digest(content),
                "source_hash": digest(json.dumps(provenance)),
                "members": current[:],
                "start_at": min(f["message"]["sent_at"] for f in current),
                "end_at": max(f["message"]["sent_at"] for f in current),
            }
        )
        current = []
        anchor = None

    for f in fragments:
        if current and (
            (f["message"]["sent_at"] - current[-1]["message"]["sent_at"]).total_seconds() > 1800
            or len(current) >= 12
            or provider.count("\n".join(x["line"] for x in current + [f])) > 480
        ):
            finish()
        if not current:
            anchor = (f["message"]["id"], f["part"])
            reply = first.get(f["message"].get("reply_to_message_id"))
            # Only a stored explicit same-conversation relation can add distant reply context.
            if (
                reply
                and reply["message"]["id"] != f["message"]["id"]
                and reply["message"]["sent_at"] <= f["message"]["sent_at"]
            ):
                if provider.count(reply["line"] + "\n" + f["line"]) <= 480:
                    current.append({**reply, "role": "EXPLICIT_REPLY"})
        current.append(f)
    finish()
    return result
