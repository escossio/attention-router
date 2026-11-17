"""D06-A configuration contract. No productive defaults or purge implementation.

The decision reference records approval externally; it is not itself consent.
Synthetic fixtures exercise this contract without authorizing personal data.
"""
from datetime import datetime, timedelta
from typing import Literal

from pydantic import Field, model_validator

from attention_router.application.semantic_interpretation import Contract

DataClass = Literal['MESSAGE', 'MEDIA', 'TRANSCRIPTION', 'CONTACT', 'INTERPRETATION',
                    'CANDIDATE', 'EPISODE', 'REVIEWED_MEMORY', 'INDEX', 'CACHE',
                    'LOG', 'EXPORT', 'BACKUP']


class RetentionRule(Contract):
    data_class: DataClass
    purpose: str = Field(min_length=1, max_length=120)
    ttl_seconds: int = Field(strict=True, gt=0, le=3155760000)
    decision_ref: str = Field(min_length=1, max_length=120)
    expiry_action: Literal['BLOCK_AND_SCHEDULE_VERIFIED_DELETION']


class RetentionSchedule(Contract):
    tenant_id: str = Field(min_length=1)
    source_account: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    lineage: Literal['SYNTHETIC']
    effective_at: datetime
    expires_at: datetime
    rules: tuple[RetentionRule, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode='after')
    def explicit_policy(self):
        if (self.effective_at.utcoffset() is None or self.expires_at.utcoffset() is None
                or self.expires_at <= self.effective_at):
            raise ValueError('RETENTION_POLICY_TIME_INVALID')
        keys = [(r.data_class, r.purpose) for r in self.rules]
        if len(set(keys)) != len(keys):
            raise ValueError('RETENTION_DUPLICATE_RULE')
        return self

    def deadline(self, *, tenant_id, source_account, owner_id, data_class, purpose,
                 created_at: datetime, now: datetime, consent: bool,
                 source_deadlines: tuple[datetime, ...] = ()) -> datetime:
        if (not consent or (tenant_id, source_account, owner_id) !=
                (self.tenant_id, self.source_account, self.owner_id)):
            raise ValueError('RETENTION_SCOPE_OR_CONSENT_DENIED')
        stamps = (created_at, now, *source_deadlines)
        if any(s.utcoffset() is None for s in stamps) or created_at > now:
            raise ValueError('RETENTION_TIME_INVALID')
        if not self.effective_at <= now < self.expires_at:
            raise ValueError('RETENTION_POLICY_EXPIRED')
        rule = next((r for r in self.rules if (r.data_class, r.purpose) == (data_class, purpose)), None)
        if rule is None:
            raise ValueError('RETENTION_RULE_MISSING')
        deadline = min(created_at + timedelta(seconds=rule.ttl_seconds),
                       self.expires_at, *source_deadlines)
        if now >= deadline:
            raise ValueError('RETENTION_DATA_EXPIRED')
        return deadline
