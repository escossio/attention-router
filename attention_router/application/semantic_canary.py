"""Disabled-by-default, owner-supervised adapter over the existing D06/D08 facade.

Works on ORIGINAL archived rows; never copies organic messages as synthetic.
Not registered in web.app or any worker. Activation, TTLs, capture permission and
backup handling require a separate operational decision. No provider calls.
"""
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import select

from attention_router.application.semantic_governance import (
    CanaryScope, GovernedSemanticLab, LabGrant, PolicyJournal,
)
from attention_router.application.semantic_interpretation import (
    SourceEvidence, SyntheticRetention, _propose_from_authorized_sources, _utc,
    interpret_offline,
)
from attention_router.application.semantic_policy_anchor import AnchoredPolicyJournal
from attention_router.application.semantic_sensitivity import translate_sensitivity
from attention_router.domain.cognitive_graph import (
    CognitiveEdge, CognitiveGraphSlice, CognitiveInferenceClass, CognitiveNode,
    CognitiveNodeKind, CognitiveRelationKind,
)
from attention_router.infrastructure.models import ConversationMessageRow


def canary_reference_rules():
    """CAN-RET-01 reference only. No consent, backup coverage or runtime default.

    Callers anchor these rules to original creation times with RetentionSchedule;
    replay and review cannot reset that anchor. Other classes remain explicit.
    """
    from attention_router.application.semantic_retention import RetentionRule
    return tuple(RetentionRule(data_class=kind, purpose=purpose, ttl_seconds=days * 86400,
        decision_ref='CAN-RET-01', expiry_action='BLOCK_AND_SCHEDULE_VERIFIED_DELETION')
        for kind, purpose, days in (
            ('MEDIA', 'SEMANTIC_CANARY', 7), ('MESSAGE', 'SEMANTIC_CANARY', 30),
            ('TRANSCRIPTION', 'SEMANTIC_CANARY', 30),
            ('CANDIDATE', 'SEMANTIC_CANARY_UNREVIEWED', 7),
            ('CANDIDATE', 'SEMANTIC_CANARY_REVIEWED', 30)))


class CanaryGrant(LabGrant):
    purpose: Literal['SEMANTIC_CANARY']
    lineage: Literal['ORGANIC']
    canary: CanaryScope
    canary_id: str = Field(min_length=1, max_length=120)
    operational_decision_ref: str = Field(min_length=1, max_length=120)
    retention_decision_ref: str = Field(min_length=1, max_length=120)
    backup_decision_ref: str = Field(min_length=1, max_length=120)

    @model_validator(mode='after')
    def consent_window(self):
        if (self.starts_at.utcoffset() is None
                or any(t.utcoffset() is None for t in self.deadlines.values())
                or self.canary.observe_from < self.starts_at
                or any(t < self.canary.observe_until for t in self.deadlines.values())):
            raise ValueError('CANARY_WINDOW_EXCEEDS_GRANT')
        return self


class CanaryJournal(PolicyJournal):
    grant_type = CanaryGrant
    format = 'SCOPED_CANARY_GOVERNANCE_V1'


class CanaryRetention(SyntheticRetention):
    purpose: Literal['SEMANTIC_CANARY']


class GovernedSemanticCanary(GovernedSemanticLab):
    """Same V2E review and policy fence; narrower source/retrieval adapters.

    Enrollment is explicit and owner-authenticated. Deployment must quarantine
    selected live messages BEFORE legacy readers run; enrollment is not a
    retrospective claim that earlier consumers never saw an existing archive.
    """
    grant_type = CanaryGrant
    policy_type = CanaryRetention
    audit_origin = 'SEMANTIC_CANARY'

    def __init__(self, *, journal, client_sessions, enabled=False):
        super().__init__(journal=journal, client_sessions=client_sessions)
        self.enabled = enabled is True

    def _enabled(self):
        if not self.enabled:
            raise ValueError('SEMANTIC_CANARY_DISABLED')
        if not isinstance(self.journal, AnchoredPolicyJournal):
            raise ValueError('CANARY_REQUIRES_CURRENT_REVOCATION_AUTHORITY')
        from attention_router.application.semantic_policy_anchor import SyntheticFileAuthority
        from attention_router.config import settings
        if isinstance(self.journal.authority, SyntheticFileAuthority) and settings.app_env != 'test':
            raise ValueError('CANARY_SYNTHETIC_AUTHORITY_FORBIDDEN_OUTSIDE_TESTS')

    def _policy_deadline(self, grant):
        # Source/reviewed access outlives unreviewed proposals, not their evidence.
        return min(grant.deadlines[k] for k in
                   ('ARCHIVE', 'TRANSCRIPTION', 'EPISODE_RELATION', 'INDEX_CACHE_BACKUP'))

    def _after_admission(self, row, grant, decision_ref, now):
        # Only explicit owner ADMIT reaches this hook, under the current fence.
        provenance = dict(row.provenance)
        original = provenance['retention_expires_at']
        reviewed = provenance.get('reviewed_retention_expires_at', original)
        deadline = min(_utc(datetime.fromisoformat(reviewed)), _utc(self._policy_deadline(grant)))
        if 'retention_review_decision_ref' in provenance:
            deadline = min(deadline, _utc(datetime.fromisoformat(original)))
        provenance.setdefault('unreviewed_retention_expires_at', original)
        provenance.setdefault('retention_review_decision_ref', decision_ref)
        provenance.setdefault('retention_reviewed_at', now.isoformat())
        provenance['retention_expires_at'] = deadline.isoformat()
        row.provenance = provenance

    def initialize(self, session, *, session_token, grant, now):
        self._enabled()
        return super().initialize(session, session_token=session_token, grant=grant, now=now)

    @contextmanager
    def _current(self, session, token, now):
        self._enabled()
        with super()._current(session, token, now) as current:
            yield current

    def capture_evidence(self, *args, **kwargs):
        raise ValueError('CANARY_REUSES_ORIGINAL_INGRESS_NO_SYNTHETIC_COPY')

    @contextmanager
    def _transport_current(self, session, now):
        """Internal HMAC caller only; consent was created by ClientSession owner.

        Recheck current ownership, not a cached transport assertion. No session
        token is stored in a journal or forwarded to WhatsApp Transport.
        """
        from attention_router.infrastructure.client_bootstrap_models import ClientTenantMembershipRow
        from attention_router.application.candidate_insights import _require_owner_decision
        self._enabled()
        with self.journal.locked():
            if session.new or session.dirty or session.deleted:
                raise ValueError('GOVERNANCE_REQUIRES_CLEAN_UNIT_OF_WORK')
            session.expire_all()
            state = self.journal.read()
            grant = self.grant_type.model_validate(state['grant'])
            owners = session.scalars(select(ClientTenantMembershipRow).where(
                ClientTenantMembershipRow.tenant_id == grant.tenant_id,
                ClientTenantMembershipRow.role == 'OWNER',
                ClientTenantMembershipRow.status == 'ACTIVE')).all()
            if len(owners) != 1 or owners[0].human_identity_id != grant.owner_id:
                raise ValueError('GOVERNANCE_OWNER_REQUIRED')
            _require_owner_decision(session, tenant_id=grant.tenant_id,
                                    decision_actor_key=grant.owner_actor_key)
            self._policy(state, grant, now, capture=True)
            yield state, grant

    def observe_trusted(self, session, *, event, now):
        """Original ingress/archive, quarantined BEFORE commit, no decision queue.

        Only the separately mounted, existing-HMAC-protected canary route calls
        this method. Normal WhatsApp ingress and sending are unchanged.
        """
        from attention_router.application.conversation_live_capture import capture_live_receipt
        from attention_router.infrastructure.repository import create_inbound_event
        from attention_router.infrastructure.hashing import stable_hash
        from attention_router.infrastructure.models import InboundEventRow
        with self._transport_current(session, now) as (state, grant):
            meta = event.metadata
            if (event.tenant_id != grant.tenant_id or event.source != 'wwebjs'
                    or meta.get('source_account') != grant.source_account
                    or meta.get('conversation_key') != grant.canary.conversation_key
                    or meta.get('conversation_state') != 'READY'
                    or (meta.get('is_group') is not None and meta.get('is_group') is not False)
                    or meta.get('peer_id_kind') not in {'lid', 'c.us'}
                    or meta.get('owner_self_chat') is True or event.owner_authenticated
                    or not grant.canary.observe_from <= _utc(event.received_at) <= _utc(now)):
                raise ValueError('CANARY_OBSERVATION_SCOPE_DENIED')
            receipt = session.scalar(select(InboundEventRow).where(
                InboundEventRow.tenant_id == grant.tenant_id, InboundEventRow.source == event.source,
                InboundEventRow.external_event_id == event.external_event_id))
            duplicate = receipt is not None
            if receipt is not None:
                if receipt.payload_hash != stable_hash(event.normalized_payload) or receipt.interaction_id:
                    raise ValueError('CANARY_INGRESS_REPLAY_CONFLICT')
                old = session.scalar(select(ConversationMessageRow).where(
                    ConversationMessageRow.tenant_id == grant.tenant_id,
                    ConversationMessageRow.metadata_json['source_inbound_event_id'].as_string() == receipt.id))
                if old is None or (old.metadata_json or {}).get('semantic_canary_id') != grant.canary_id:
                    raise ValueError('CANARY_PREEXISTING_RECEIPT_NOT_ADOPTED')
            else:
                receipt = create_inbound_event(session, event.source, event.external_event_id,
                    event.event_type, event.normalized_payload, event.correlation_id,
                    event.tenant_id, event.received_at)
            captured = capture_live_receipt(session, receipt)
            if not captured.message_id:
                session.rollback()
                raise ValueError('CANARY_ARCHIVE_CAPTURE_DENIED')
            self._policy(state, grant, now, capture=True).require(
                [captured.message_id], grant.tenant_id, grant.source_account, now)
            row, _ = self._quarantine(session, grant, captured.message_id, now)
            receipt.status = 'PROCESSED'
            receipt.processed_at = now
            allowed = (row.message_type == 'VOICE' and meta.get('has_media') is True
                and row.metadata_json.get('speaker_basis') in {
                    'EXTERNAL_SENDER', 'REPRESENTED_OWNER', 'OWNER_ACCOUNT_ATTRIBUTED_BY_POLICY_V1'})
            session.commit()
            return dict(status='duplicate' if duplicate else 'accepted', interaction_id=None,
                correlation_id=event.correlation_id, canary_media_allowed=allowed,
                message_id=row.id, grants_authority=False)

    @contextmanager
    def processing_guard(self, session, *, event_id, now=None):
        """Bounded media/STT unit of work under the same effective-revocation lock."""
        from attention_router.infrastructure.models import InboundEventRow
        now = now or datetime.now(UTC)
        with self._transport_current(session, now) as (state, grant):
            event = session.get(InboundEventRow, event_id, populate_existing=True)
            if event is None or event.tenant_id != grant.tenant_id:
                raise ValueError('CANARY_EVENT_SCOPE_DENIED')
            row = session.scalar(select(ConversationMessageRow).where(
                ConversationMessageRow.tenant_id == grant.tenant_id,
                ConversationMessageRow.metadata_json['source_inbound_event_id'].as_string() == event_id))
            if row is None:
                raise ValueError('CANARY_EVENT_NOT_ARCHIVED')
            self._validate_source_scope(session, grant, [row.id])
            self._policy(state, grant, now, capture=True).require(
                [row.id], grant.tenant_id, grant.source_account, now)
            if _utc(now) >= _utc(grant.deadlines['MEDIA']):
                raise ValueError('CANARY_MEDIA_RETENTION_EXPIRED')
            yield {'media_expires_at': grant.deadlines['MEDIA'],
                   'processing_until': min(grant.canary.observe_until, *grant.deadlines.values())}

    def enroll(self, session, *, session_token, message_id, now):
        """Quarantine one original row, without changing ID/time/authorship/lineage.

        Call only inside the selected capture boundary for operational use.
        It does not download media, reopen an old cursor, or authorize STT.
        """
        with self._current(session, session_token, now) as (state, grant):
            policy = self._policy(state, grant, now, capture=True)
            policy.require([message_id], grant.tenant_id, grant.source_account, now)
            row, created = self._quarantine(session, grant, message_id, now)
            session.commit()
            return dict(message_id=row.id, provider_id=row.source_message_id, enrolled=True,
                        created=created, grants_authority=False)

    def _quarantine(self, session, grant, message_id, now):
        super()._validate_source_scope(session, grant, [message_id])
        row = session.get(ConversationMessageRow, message_id, populate_existing=True)
        metadata = dict(row.metadata_json or {})
        existing = metadata.get('semantic_canary_id')
        if existing not in {None, grant.canary_id}:
            raise ValueError('CANARY_ENROLLMENT_CONFLICT')
        if _utc(row.sent_at) > _utc(now):
            raise ValueError('CANARY_SOURCE_FROM_FUTURE')
        if existing is None:
            from attention_router.infrastructure.models import MemoryCandidateRow, MemoryEvidenceRow
            from attention_router.infrastructure.semantic_episode_models import SemanticEpisodeMembershipRow
            from attention_router.infrastructure.candidate_insight_models import CandidateInsightEvidenceRow
            derived = (
                session.scalar(select(MemoryCandidateRow.id).where(MemoryCandidateRow.message_id == row.id).limit(1))
                or session.scalar(select(MemoryEvidenceRow.id).where(MemoryEvidenceRow.conversation_message_id == row.id).limit(1))
                or session.scalar(select(SemanticEpisodeMembershipRow.id).where(
                    SemanticEpisodeMembershipRow.tenant_id == grant.tenant_id,
                    SemanticEpisodeMembershipRow.member_type == 'CONVERSATION_MESSAGE',
                    SemanticEpisodeMembershipRow.member_ref == row.id).limit(1))
                or session.scalar(select(CandidateInsightEvidenceRow.id).where(
                    CandidateInsightEvidenceRow.tenant_id == grant.tenant_id,
                    CandidateInsightEvidenceRow.evidence_type == 'CONVERSATION_MESSAGE',
                    CandidateInsightEvidenceRow.source_ref == row.id).limit(1)))
            if derived:
                raise ValueError('CANARY_SOURCE_ALREADY_DERIVED')
        rows = session.scalars(select(ConversationMessageRow).where(
            ConversationMessageRow.tenant_id == grant.tenant_id,
            ConversationMessageRow.source_account == grant.source_account,
            ConversationMessageRow.metadata_json['semantic_canary_id'].as_string() == grant.canary_id
        ).limit(grant.canary.max_messages + 1)).all()
        if existing is None and (len(rows) >= grant.canary.max_messages or
                row.message_type in {'VOICE', 'AUDIO'} and sum(
                    item.message_type in {'VOICE', 'AUDIO'} for item in rows) >= grant.canary.max_audio_messages):
            raise ValueError('CANARY_CAPTURE_BUDGET_EXHAUSTED')
        metadata['semantic_canary_id'] = grant.canary_id
        metadata['semantic_canary_until'] = grant.canary.observe_until.isoformat()
        row.metadata_json = metadata
        row.searchable = False
        return row, existing is None

    def _validate_source_scope(self, session, grant, ids):
        super()._validate_source_scope(session, grant, ids)
        for mid in ids:
            row = session.get(ConversationMessageRow, mid, populate_existing=True)
            if (row.metadata_json or {}).get('semantic_canary_id') != grant.canary_id or row.searchable:
                raise ValueError('CANARY_SOURCE_NOT_QUARANTINED')

    def _sources(self, session, grant, ids):
        self._validate_source_scope(session, grant, ids)
        sources = {}
        for mid in ids:
            row = session.get(ConversationMessageRow, mid, populate_existing=True)
            meta = row.metadata_json or {}
            sensitivity = translate_sensitivity(row.sensitivity_class).candidate_class
            if sensitivity == 'SECRET':
                raise ValueError('INTERPRETATION_SOURCE_FORBIDDEN')
            sources[mid] = SourceEvidence(tenant_id=grant.tenant_id, source_account=grant.source_account,
                message_id=row.id, author=row.external_sender_key or '',
                author_basis=meta.get('speaker_basis', 'UNRESOLVED'), origin=meta.get('event_origin', 'UNKNOWN'),
                sent_at=row.sent_at, text=row.text or '', event_ref=meta.get('reviewed_event_ref'),
                reply_to=row.reply_to_message_id, modality='VOICE' if row.message_type in {'VOICE','AUDIO'} else 'TEXT',
                transcription_id=meta.get('transcription_id'), sensitivity=sensitivity,
                lineage='ORGANIC', stt_ambiguous=meta.get('stt_ambiguous', False))
        return sources

    def propose(self, session, *, session_token, value, now):
        with self._current(session, session_token, now) as (state, grant):
            access = self._policy(state, grant, now, capture=True)
            policy = access.model_copy(update={'expires_at': min(access.expires_at,
                grant.deadlines['CANDIDATE'], grant.deadlines['INTERPRETATION'])})
            refs = set(value.source_message_ids) | set(value.references) | set(value.contradiction_refs)
            sources = self._sources(session, grant, refs)
            row, created = _propose_from_authorized_sources(session, value, sources=sources,
                policy=policy, now=now, provenance={'semantic_canary_id':grant.canary_id,
                    'operational_decision_ref':grant.operational_decision_ref,
                    'reviewed_retention_expires_at':access.expires_at.isoformat()})
            session.commit()
            return row.id, created

    def interpret(self, session, *, session_token, message_id, now, timezone='UTC'):
        with self._current(session, session_token, now) as (state, grant):
            self._policy(state, grant, now, capture=True).require(
                [message_id], grant.tenant_id, grant.source_account, now)
            source = self._sources(session, grant, [message_id])[message_id]
            return interpret_offline(source, timezone=timezone)

    def process_message(self, session, *, session_token, message_id, now):
        """Small supervised entry point; every stage rechecks owner and policy.

        Failure after enrollment leaves the source quarantined, not readable by
        old consumers. Retry uses the same archive row and V2E semantic key.
        """
        self.enroll(session, session_token=session_token, message_id=message_id, now=now)
        # The normal worker excludes quarantined messages. The authenticated
        # owner drives at most this one existing STT job, using the same client.
        row = session.get(ConversationMessageRow, message_id, populate_existing=True)
        event_id = (row.metadata_json or {}).get('source_inbound_event_id')
        if not row.text and event_id and row.message_type in {'VOICE', 'AUDIO'}:
            from attention_router.application.voice_transcription import process_voice_transcriptions
            process_voice_transcriptions(session, 'semantic-canary', limit=1, event_ids=(event_id,),
                canary_guard=lambda db, event_id: self.processing_guard(db, event_id=event_id, now=now))
        with self._current(session, session_token, now) as (state, grant):
            self._policy(state, grant, now, capture=True).require(
                [message_id], grant.tenant_id, grant.source_account, now)
            source = self._sources(session, grant, [message_id])[message_id]
            if source.origin == 'ROUTER_AUTOMATED_OUTBOUND_OBSERVED':
                return dict(status='AUTOMATED_EVIDENCE_ONLY', message_id=message_id, candidate_id=None)
            if not source.text:
                return dict(status='SOURCE_INCOMPLETE', message_id=message_id, candidate_id=None)
            value = interpret_offline(source)
        if value.state == 'UNRESOLVED' or value.action in {'UNKNOWN', 'QUESTIONS', 'ANSWERS', 'REPORTS'}:
            return dict(status='INTERPRETATION_REQUIRES_REVIEW', message_id=message_id,
                        candidate_id=None, interpretation=value.model_dump(mode='json'))
        cid, created = self.propose(session, session_token=session_token, value=value, now=now)
        return dict(status='CANDIDATE_PROPOSED', message_id=message_id, candidate_id=cid, created=created)

    def _candidate_belongs(self, row, grant):
        return (super()._candidate_belongs(row, grant)
                and row.provenance.get('semantic_canary_id') == grant.canary_id)

    def conversation(self, session, *, session_token, now):
        from attention_router.application.conversation_preview import preview_conversation
        with self._current(session, session_token, now) as (state, grant):
            policy = self._policy(state, grant, now)
            def authorize(mid):
                try:
                    policy.require([mid], grant.tenant_id, grant.source_account, now)
                    self._sources(session, grant, [mid])
                    return True
                except ValueError:
                    return False
            return preview_conversation(session, tenant_id=grant.tenant_id,
                source_account=grant.source_account, conversation_key=grant.canary.conversation_key,
                limit=min(grant.canary.max_messages, 200), source_authorizer=authorize)

    def _context_projection(self, session, grant, safe, now):
        sources = self._sources(session, grant, safe)
        nodes = tuple(CognitiveNode(node_id='message:'+s.message_id, tenant_id=grant.tenant_id,
            kind=CognitiveNodeKind.MESSAGE, source_type='CONVERSATION_MESSAGE', source_id=s.message_id,
            label=s.text, valid_from=s.sent_at, attributes={'sensitivity_class':s.sensitivity,
                'event_origin':s.origin, 'author_basis':s.author_basis, 'author':s.author,
                'modality':s.modality, 'transcription_id':s.transcription_id},
            provenance={'semantic_canary_id':grant.canary_id, 'lineage_classification':s.lineage})
            for s in sources.values())
        edges = tuple(CognitiveEdge(edge_id='reply:'+s.message_id, tenant_id=grant.tenant_id,
            source_node_id='message:'+s.message_id, target_node_id='message:'+s.reply_to,
            relation_kind=CognitiveRelationKind.EXPLICIT_RELATION, semantic_relation='REPLIES_TO',
            inference_class=CognitiveInferenceClass.EXPLICIT,
            provenance={'basis':'PROVIDER_REPLY_REFERENCE'}) for s in sources.values() if s.reply_to in safe)
        return CognitiveGraphSlice(tenant_id=grant.tenant_id, nodes=nodes, edges=edges, generated_at=now)

    def _purge_episodes(self, session, grant, ids):
        return 0  # No durable episode created by this adapter; no organic purge implied.

    def _purge_candidates(self, session, *, policy):
        return 0  # Fence now; report retained rows through VERIFY_DELETION. No real purge authorized.

    def consumer_read(self, *args, **kwargs):
        raise ValueError('CANARY_LEGACY_CONSUMERS_NOT_REGISTERED')
