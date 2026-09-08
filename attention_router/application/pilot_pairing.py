from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from attention_router.config import settings
from attention_router.infrastructure.models import InboundEventRow


DEFAULT_STATE_DIR = Path(".runtime/andy_real_pilot_pairing")
DEFAULT_WINDOW_SECONDS = 600
PAIRING_VERSION = 1


class PairingError(RuntimeError):
    pass


@dataclass(frozen=True)
class PairingPaths:
    root: Path
    state: Path
    lock: Path


def pairing_paths(root: Path | None = None) -> PairingPaths:
    base = root or DEFAULT_STATE_DIR
    return PairingPaths(base, base / "state.json", base / "pairing.lock")


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def write_private_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    os.chmod(path, 0o600)


def read_state(paths: PairingPaths) -> dict[str, Any] | None:
    if not paths.state.exists():
        return None
    return json.loads(paths.state.read_text(encoding="utf-8"))


class PairingLock:
    def __init__(self, paths: PairingPaths) -> None:
        self.paths = paths
        self._fh = None

    def __enter__(self):
        ensure_private_dir(self.paths.root)
        self._fh = self.paths.lock.open("a+")
        os.chmod(self.paths.lock, 0o600)
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PairingError("pairing_state_locked") from exc
        return self

    def __exit__(self, *_):
        if self._fh:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def as_aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def new_secret() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def challenge_hmac(secret: str, challenge: str) -> str:
    return hmac.new(secret.encode(), challenge.encode(), hashlib.sha256).hexdigest()


def stable_hmac(secret: str, value: str) -> str:
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()


def pseudonym(secret: str, actor_id: str) -> str:
    return f"pilot_actor_{stable_hmac(secret, actor_id)[:16]}"


def normalize_text(value: str) -> str:
    return " ".join(value.strip().split())


def active_state(state: dict[str, Any] | None, at: datetime | None = None) -> bool:
    if not state or state.get("status") not in {"armed", "claimed"}:
        return False
    return parse_dt(state["expires_at"]) > (at or now_utc())


def inbound_watermark(session: Session) -> dict[str, Any]:
    row = session.execute(
        select(func.max(InboundEventRow.received_at), func.count(InboundEventRow.id)).where(
            InboundEventRow.source == settings.internal_ingress_source,
            InboundEventRow.event_type == "message",
        )
    ).one()
    max_received_at, count = row
    return {
        "opened_after": iso(now_utc()),
        "watermark_received_at": iso(as_aware(max_received_at)) if max_received_at else None,
        "watermark_count": int(count or 0),
    }


def arm_pairing(
    session: Session,
    paths: PairingPaths,
    *,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    at: datetime | None = None,
) -> tuple[dict[str, Any], str]:
    current_time = at or now_utc()
    if window_seconds <= 0 or window_seconds > 3600:
        raise PairingError("invalid_pairing_window")
    with PairingLock(paths):
        existing = read_state(paths)
        if active_state(existing, current_time):
            raise PairingError("active_pairing_window_exists")
        secret = new_secret()
        challenge = new_secret()
        watermark = inbound_watermark(session)
        opened_at = current_time
        expires_at = opened_at + timedelta(seconds=window_seconds)
        state = {
            "version": PAIRING_VERSION,
            "status": "armed",
            "challenge_hmac": challenge_hmac(secret, challenge),
            "pairing_secret": secret,
            "opened_at": iso(opened_at),
            "expires_at": iso(expires_at),
            "window_seconds": window_seconds,
            "watermark": watermark,
            "claimed_actor_hmac": None,
            "claimed_actor_pseudonym": None,
            "claimed_event_hmac": None,
            "confirmation_hmac": None,
            "confirmed_at": None,
            "cancel_reason": None,
        }
        write_private_json(paths.state, state)
        public_state = dict(state)
        public_state.pop("pairing_secret", None)
        public_state["challenge_hmac"] = "stored"
        return public_state, challenge


def _event_is_after_watermark(event: InboundEventRow, state: dict[str, Any]) -> bool:
    watermark = state.get("watermark") or {}
    opened_at = parse_dt(state["opened_at"])
    received_at = as_aware(event.received_at)
    if received_at <= opened_at:
        return False
    watermark_received_at = watermark.get("watermark_received_at")
    if watermark_received_at and received_at <= parse_dt(watermark_received_at):
        return False
    return True


def _event_from_me(event: InboundEventRow) -> bool | None:
    payload = event.payload or {}
    metadata = payload.get("metadata") or {}
    value = metadata.get("from_me")
    if value is None:
        value = metadata.get("fromMe")
    if value is None:
        return None
    return bool(value)


def matching_events(session: Session, state: dict[str, Any]) -> list[InboundEventRow]:
    secret = state["pairing_secret"]
    opened_at = parse_dt(state["opened_at"])
    expires_at = parse_dt(state["expires_at"])
    events = session.scalars(
        select(InboundEventRow)
        .where(
            InboundEventRow.source == settings.internal_ingress_source,
            InboundEventRow.event_type == "message",
            InboundEventRow.received_at > opened_at,
            InboundEventRow.received_at <= expires_at,
        )
        .order_by(InboundEventRow.received_at)
    ).all()
    matches: list[InboundEventRow] = []
    for event in events:
        if not _event_is_after_watermark(event, state):
            continue
        from_me = _event_from_me(event)
        if from_me is None:
            raise PairingError("from_me_unavailable")
        if from_me:
            continue
        payload = event.payload or {}
        actor_id = str(payload.get("external_actor_id") or payload.get("actor_id") or "")
        if not actor_id:
            raise PairingError("actor_id_unavailable")
        content = str(payload.get("content") or "")
        if hmac.compare_digest(challenge_hmac(secret, normalize_text(content)), state["challenge_hmac"]):
            matches.append(event)
    return matches


def claim_pairing(session: Session, paths: PairingPaths, *, at: datetime | None = None) -> dict[str, Any]:
    current_time = at or now_utc()
    with PairingLock(paths):
        state = read_state(paths)
        if not state or state.get("status") != "armed":
            raise PairingError("no_armed_pairing")
        if parse_dt(state["expires_at"]) <= current_time:
            state["status"] = "expired"
            write_private_json(paths.state, state)
            raise PairingError("pairing_window_expired")
        matches = matching_events(session, state)
        actor_hmacs: dict[str, InboundEventRow] = {}
        for event in matches:
            payload = event.payload or {}
            actor_id = str(payload.get("external_actor_id") or payload.get("actor_id") or "")
            actor_hmacs.setdefault(stable_hmac(state["pairing_secret"], actor_id), event)
        if not actor_hmacs:
            return {"status": "armed", "matches": 0}
        if len(actor_hmacs) > 1:
            state["status"] = "aborted"
            state["cancel_reason"] = "multiple_matching_actors"
            write_private_json(paths.state, state)
            raise PairingError("multiple_matching_actors")
        actor_hmac, event = next(iter(actor_hmacs.items()))
        actor_id = str((event.payload or {}).get("external_actor_id") or (event.payload or {}).get("actor_id") or "")
        confirmation = new_secret()
        state["status"] = "claimed"
        state["claimed_actor_hmac"] = actor_hmac
        state["claimed_actor_pseudonym"] = pseudonym(state["pairing_secret"], actor_id)
        state["claimed_event_hmac"] = stable_hmac(state["pairing_secret"], event.external_event_id)
        state["confirmation_hmac"] = stable_hmac(state["pairing_secret"], confirmation)
        write_private_json(paths.state, state)
        return {
            "status": "claimed",
            "actor_pseudonym": state["claimed_actor_pseudonym"],
            "confirmation_token": confirmation,
        }


def confirm_pairing(
    paths: PairingPaths,
    *,
    confirmation_token: str,
    consent_adult: bool,
    consent_ai: bool,
    consent_single_use: bool,
    at: datetime | None = None,
) -> dict[str, Any]:
    with PairingLock(paths):
        state = read_state(paths)
        if not state or state.get("status") != "claimed":
            raise PairingError("no_claimed_pairing")
        if not (consent_adult and consent_ai and consent_single_use):
            state["status"] = "canceled"
            state["cancel_reason"] = "consent_missing"
            write_private_json(paths.state, state)
            raise PairingError("consent_missing")
        expected = state.get("confirmation_hmac") or ""
        if not hmac.compare_digest(stable_hmac(state["pairing_secret"], confirmation_token), expected):
            raise PairingError("invalid_confirmation")
        state["status"] = "confirmed"
        state["confirmed_at"] = iso(at or now_utc())
        state["confirmation_hmac"] = None
        write_private_json(paths.state, state)
        return {"status": "confirmed", "actor_pseudonym": state["claimed_actor_pseudonym"]}


def cancel_pairing(paths: PairingPaths, *, reason: str = "operator_cancel") -> dict[str, Any]:
    with PairingLock(paths):
        state = read_state(paths)
        if not state:
            return {"status": "none"}
        state["status"] = "canceled"
        state["cancel_reason"] = reason[:80]
        write_private_json(paths.state, state)
        return {"status": "canceled"}


def public_status(paths: PairingPaths, *, at: datetime | None = None) -> dict[str, Any]:
    state = read_state(paths)
    if not state:
        return {"status": "none"}
    public = {
        "status": state.get("status"),
        "opened_at": state.get("opened_at"),
        "expires_at": state.get("expires_at"),
        "watermark": state.get("watermark"),
        "actor_pseudonym": state.get("claimed_actor_pseudonym"),
        "active": active_state(state, at),
        "cancel_reason": state.get("cancel_reason"),
    }
    return public
