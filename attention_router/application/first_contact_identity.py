"""Presentation identity is provider evidence, never owner/capability authority."""

import hashlib
import re
import secrets

from attention_router.application.direct_conversation import direct_conversation_eligibility
from attention_router.config import settings

_LID = re.compile(r"[0-9]{1,64}@lid\Z")
_PN = re.compile(r"[0-9]{1,64}@c\.us\Z")


def canonical_peer_hash(event) -> str | None:
    """Validate the provider attestation already authenticated by ingress HMAC."""
    if not direct_conversation_eligibility(event).eligible:
        return None
    metadata = (event.payload or {}).get("metadata") or {}
    evidence = metadata.get("first_contact_identity")
    if not isinstance(evidence, dict) or (
        evidence.get("contract_version") != "provider-lid-v1"
        or evidence.get("state") != "VERIFIED"
    ):
        return None
    canonical = evidence.get("canonical_peer_id")
    if not isinstance(canonical, str) or not _LID.fullmatch(canonical):
        return None
    peer = event.payload.get("external_actor_id") or event.payload.get("actor_id")
    aliases = metadata.get("peer_identifiers") or []
    if (
        not isinstance(aliases, list)
        or any(not isinstance(x, str) for x in aliases)
        or len(set(aliases)) != len(aliases)
        or canonical not in aliases
        or peer not in aliases
        or len([x for x in aliases if _LID.fullmatch(x)]) != 1
        or len([x for x in aliases if _PN.fullmatch(x)]) > 1
        or any(not (_LID.fullmatch(x) or _PN.fullmatch(x)) for x in aliases)
        or metadata.get("conversation_key") != f"wwebjs:{canonical}"
    ):
        return None
    provenance = evidence.get("provenance")
    if not (
        (provenance == "DIRECTIONAL_LID" and peer == canonical)
        or (
            provenance == "PROVIDER_LID_PN_MAPPING"
            and isinstance(peer, str)
            and (_LID.fullmatch(peer) or _PN.fullmatch(peer))
        )
    ):
        return None
    return hashlib.sha256(canonical.encode()).hexdigest()


def presentation_scope(event, *, visitor_default: bool, binding):
    """Fail closed for introduction only; never change ordinary visitor routing."""
    from attention_router.application.first_contact import FirstContactScope

    metadata = (event.payload or {}).get("metadata") or {}
    if not (
        settings.first_contact_enabled
        and visitor_default and binding is None
        and event.tenant_id == settings.first_contact_tenant_id
        and metadata.get("source_account") == settings.first_contact_source_account
    ):
        return None
    digest = canonical_peer_hash(event)
    allowed = settings.first_contact_canary_peer_sha256
    if (
        digest is None or not isinstance(allowed, str)
        or not re.fullmatch(r"[0-9a-f]{64}", allowed)
        or not secrets.compare_digest(digest, allowed)
    ):
        return None
    return FirstContactScope(
        tenant_id=event.tenant_id,
        source="wwebjs",
        channel="whatsapp",
        source_account=metadata["source_account"],
        canonical_peer_hash=digest,
    )
