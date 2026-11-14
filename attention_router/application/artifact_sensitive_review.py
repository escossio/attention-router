"""Classify visual knowledge for owner approval, without granting execution.

The original image and its model result remain in the existing Artifact Plane.
Only category and references are persisted here: no PAN, CVV, document number,
secret, raw OCR or sensitive preview is copied into approval records.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.domain.models import now_utc
from attention_router.infrastructure.artifact_models import ArtifactRow, ArtifactUnderstandingRow
from attention_router.infrastructure.models import InboundEventRow
from attention_router.infrastructure.artifact_sensitive_review_models import ArtifactSensitiveReviewRow
from attention_router.infrastructure.client_bootstrap_models import ClientTenantMembershipRow


_REVIEW_PREFIX = "srev_"
_REVIEW_VALIDITY_DAYS = 30

# Prioritize specific categories. Match known visual/OCR vocabulary only, not
# content that is merely accompanied by a random long numeric string.
_PAYMENT_CARD = re.compile(
    r"\b(?:cart[aã]o\s+(?:de\s+)?(?:cr[eé]dito|d[eé]bito|banc[aá]rio)"
    r"|credit card|debit card|cardholder|mastercard|visa\s+(?:credit|debit)"
    r"|cvv\b|cvc\b|c[oó]digo de seguran[cç]a do cart[aã]o)\b", re.I,
)
_IDENTITY = re.compile(
    r"\b(?:carteira\s+(?:de\s+)?identidade|documento\s+de\s+identifica[cç][aã]o"
    r"|passaporte|passport|registro geral|carteira\s+de\s+motorista"
    r"|carteira nacional de habilita[cç][aã]o"
    r"|cpf\s*[:#]|rg\s*[:#]|cnh\s*[:#])\b", re.I,
)
_ACCESS = re.compile(
    r"\b(?:chave\s+privada|private key|senha\s+de\s+acesso"
    r"|password\s*[:#]|api[\s_-]?key\s*[:#]|token\s+de\s+autentica[cç][aã]o"
    r"|c[oó]digo\s+de\s+recupera[cç][aã]o)\b", re.I,
)
_CONTACT = re.compile(
    r"\b(?:cart[aã]o\s+de\s+visitas?|business card|contact card)\b", re.I,
)


def classify_sensitive_artifact(
    *,
    summary: str | None,
    extracted_text: str | None,
    visual_description: str | None,
    key_facts: list[Any] | None,
) -> str | None:
    """Conservative v1 categorization, NOT a proof of absence of secrets.

    The source remains untrusted, and classification alone never authorizes
    retrieval, disclosure, indexing or an action.
    """
    parts = (summary or "", extracted_text or "", visual_description or "")
    facts = [x for x in (key_facts or []) if isinstance(x, str)]
    text = " ".join((*parts, *facts[:24]))[:40000]
    for pattern, category in (
        (_PAYMENT_CARD, "PAYMENT_CARD"),
        (_IDENTITY, "IDENTITY_DOCUMENT"),
        (_ACCESS, "ACCESS_SECRET"),
        (_CONTACT, "CONTACT_CARD"),
    ):
        if pattern.search(text):
            return category
    return None


def _unique_active_owner(
    session: Session,
    *,
    tenant_id: str,
) -> str | None:
    human_ids = list(session.scalars(
        select(ClientTenantMembershipRow.human_identity_id).where(
            ClientTenantMembershipRow.tenant_id == tenant_id,
            ClientTenantMembershipRow.role == "OWNER",
            ClientTenantMembershipRow.status == "ACTIVE",
        )
    ).all())
    return human_ids[0] if len(human_ids) == 1 else None


def register_artifact_sensitive_review(
    session: Session,
    *,
    understanding: ArtifactUnderstandingRow,
    timestamp: datetime | None = None,
) -> ArtifactSensitiveReviewRow | None:
    """Create one review for a READY image. No provider I/O and no raw copy."""
    from attention_router.config import settings

    if (
        not settings.artifact_sensitive_review_enabled
        or understanding.status != "READY"
        or understanding.content_kind != "IMAGE"
        or not understanding.artifact_id
        or not understanding.inbound_event_id
    ):
        return None

    start = settings.artifact_sensitive_review_start_at
    if start is None or start.tzinfo is None or start.utcoffset() is None:
        return None
    event = session.get(InboundEventRow, understanding.inbound_event_id)
    artifact = session.get(ArtifactRow, understanding.artifact_id)
    if (
        event is None or artifact is None
        or (
            event.received_at.replace(tzinfo=UTC)
            if event.received_at.tzinfo is None else event.received_at.astimezone(UTC)
        ) < start.astimezone(UTC)
        or event.tenant_id != understanding.tenant_id
        or artifact.tenant_id != understanding.tenant_id
        or event.source != "wwebjs"
        or event.lineage_classification != "ORGANIC"
        or event.event_type != "message"
        or artifact.status != "AVAILABLE"
        or artifact.artifact_kind != "IMAGE"
    ):
        return None

    existing = session.scalar(select(ArtifactSensitiveReviewRow).where(
        ArtifactSensitiveReviewRow.tenant_id == understanding.tenant_id,
        ArtifactSensitiveReviewRow.understanding_id == understanding.id,
    ))
    if existing is not None:
        if (
            existing.artifact_id != understanding.artifact_id
            or existing.source_inbound_event_id != understanding.inbound_event_id
        ):
            raise ValueError("ARTIFACT_REVIEW_SOURCE_CONFLICT")
        return existing

    category = classify_sensitive_artifact(
        summary=understanding.summary_text,
        extracted_text=understanding.extracted_text,
        visual_description=understanding.visual_description,
        key_facts=understanding.key_facts_json,
    )
    if category is None:
        return None
    stamp = timestamp or now_utc()
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("ARTIFACT_REVIEW_AWARE_TIME_REQUIRED")
    stamp = stamp.astimezone(UTC)
    owner = _unique_active_owner(session, tenant_id=understanding.tenant_id)
    from attention_router.domain.models import new_id

    row = ArtifactSensitiveReviewRow(
        id=_REVIEW_PREFIX + new_id().replace("-", "")[:32],
        tenant_id=understanding.tenant_id,
        artifact_id=understanding.artifact_id,
        understanding_id=understanding.id,
        source_inbound_event_id=understanding.inbound_event_id,
        category=category,
        state="PENDING" if owner else "UNASSIGNED",
        expected_owner_human_identity_id=owner,
        decision_human_identity_id=None,
        decision_device_id=None,
        decision_client_session_id=None,
        decided_at=None,
        issued_at=stamp,
        expires_at=stamp + timedelta(days=_REVIEW_VALIDITY_DAYS),
    )
    session.add(row)
    session.flush()
    return row


__all__ = [
    "ArtifactSensitiveReviewRow",
    "classify_sensitive_artifact",
    "register_artifact_sensitive_review",
]
