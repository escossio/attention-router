"""SYNTHETIC-only D06/D08 laboratory coordinator over V2E/V2F.

The policy journal is outside database backups. It is NOT a memory store,
production consent service, retention default, or activation path. The service
owns its DB transactions. All readers/writers must use this boundary in the lab.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import Field
from sqlalchemy import select

from attention_router.application.semantic_interpretation import (
    Contract, SyntheticRetention, _utc, archived_source, propose_interpretation,
    purge_synthetic_derivatives,
)
from attention_router.application.semantic_sensitivity import translate_sensitivity
from attention_router.infrastructure.candidate_insight_models import CandidateInsightRow
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.repository import audit

Layer = Literal['ARCHIVE', 'MEDIA', 'TRANSCRIPTION', 'IDENTITY', 'INTERPRETATION',
                'CANDIDATE', 'EPISODE_RELATION', 'DURABLE_MEMORY', 'INDEX_CACHE_BACKUP']
Operation = Literal['PAUSE_CAPTURE', 'REVOKE_CONSENT', 'HIDE_CONTEXT', 'REJECT_INFERENCE',
                    'DELETE_DERIVATIVES', 'EXPORT_CONTEXT', 'VERIFY_DELETION']


class LabGrant(Contract):
    tenant_id: str
    source_account: str
    owner_id: str
    owner_actor_key: str
    purpose: Literal['SYNTHETIC_EVALUATION']
    lineage: Literal['SYNTHETIC']
    consent: Literal[True]
    policy_version: str = Field(min_length=1)
    starts_at: datetime
    # No productive period defaults. Each layer has an explicit fixture deadline.
    deadlines: dict[Layer, datetime]


class LabOperation(Contract):
    request_id: str = Field(min_length=1, max_length=120)
    operation: Operation
    source_ids: tuple[str, ...] = Field(max_length=200)


class PolicyJournal:
    """Single-host, multiprocess durable lab fence; fail closed on loss/corruption.

    Lock file never replaced; JSON is atomically replaced and fsynced. Put both
    files OUTSIDE the database backup domain. Does not defend against a host
    administrator rolling back both journal and data: deployment gate remains.
    """
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def locked(self):
        with self.path.with_suffix('.lock').open('a', encoding='utf-8') as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield self
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def read(self):
        try:
            envelope = json.loads(self.path.read_text())
            state = envelope['state']
            if stable_hash(state) != envelope['checksum'] or state['format'] != 'SYNTHETIC_GOVERNANCE_V1':
                raise ValueError()
            LabGrant.model_validate(state['grant'])
            for key in ('deleted', 'hidden', 'rejected', 'operations', 'reviews', 'revision'):
                if key not in state:
                    raise ValueError()
            return state
        except (OSError, ValueError, KeyError, TypeError):
            raise ValueError('CURRENT_POLICY_UNAVAILABLE') from None

    def write(self, state):
        state['revision'] += 1
        payload = json.dumps({'state': state, 'checksum': stable_hash(state)}, sort_keys=True)
        temporary = self.path.with_suffix('.pending')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        fd = os.open(self.path.parent, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class GovernedSemanticLab:
    """Authenticated owner-only facade. Never registered in API or worker."""
    def __init__(self, *, journal: PolicyJournal, client_sessions):
        self.journal = journal
        self.client_sessions = client_sessions

    def _owner(self, session, token, grant, now):
        authority = self.client_sessions.authenticated_bootstrap(session, session_token=token, now=now)
        owners = [m for m in authority.memberships if m.tenant_id == grant.tenant_id
                  and m.role == 'OWNER' and m.status == 'ACTIVE']
        if (authority.active_tenant_id != grant.tenant_id or authority.human_identity_id != grant.owner_id
                or len(owners) != 1):
            raise ValueError('GOVERNANCE_OWNER_REQUIRED')
        from attention_router.application.candidate_insights import _require_owner_decision
        _require_owner_decision(session, tenant_id=grant.tenant_id, decision_actor_key=grant.owner_actor_key)

    def initialize(self, session, *, session_token, grant: LabGrant, now):
        grant = LabGrant.model_validate(grant.model_dump())
        self._owner(session, session_token, grant, now)
        if set(grant.deadlines) != set(Layer.__args__) or any(_utc(t) <= _utc(grant.starts_at) for t in grant.deadlines.values()):
            raise ValueError('EXPLICIT_LAYER_DEADLINES_REQUIRED')
        with self.journal.locked():
            if self.journal.path.exists():
                raise ValueError('POLICY_ALREADY_EXISTS')
            self.journal.write(dict(format='SYNTHETIC_GOVERNANCE_V1', grant=grant.model_dump(mode='json'),
                revision=0, revoked=False, paused=False, deleted=[], hidden=[], rejected=[], operations={}, reviews={}))

    @contextmanager
    def _current(self, session, token, now):
        with self.journal.locked():
            if session.new or session.dirty or session.deleted:
                raise ValueError('GOVERNANCE_REQUIRES_CLEAN_UNIT_OF_WORK')
            session.expire_all()  # classification/session changes cannot hide in an identity-map cache
            state = self.journal.read()
            grant = LabGrant.model_validate(state['grant'])
            self._owner(session, token, grant, now)
            yield state, grant

    def _policy(self, state, grant, now, *, capture=False):
        # Earliest relevant deadline is conservative; archive/media are independently inventoried.
        deadline = min(grant.deadlines[k] for k in ('ARCHIVE', 'TRANSCRIPTION', 'INTERPRETATION',
                                                   'CANDIDATE', 'EPISODE_RELATION', 'INDEX_CACHE_BACKUP'))
        if state['revoked'] or _utc(now) < _utc(grant.starts_at) or _utc(now) >= _utc(deadline):
            raise ValueError('GOVERNANCE_CONSENT_OR_RETENTION_DENIED')
        if capture and state['paused']:
            raise ValueError('GOVERNANCE_CAPTURE_PAUSED')
        return SyntheticRetention(tenant_id=grant.tenant_id, source_account=grant.source_account,
            purpose=grant.purpose, consent=True, expires_at=deadline,
            tombstones=frozenset(state['deleted'] + state['hidden'] + state['rejected']))

    def _sources(self, session, grant, ids):
        return {mid: archived_source(session, tenant_id=grant.tenant_id,
                    source_account=grant.source_account, message_id=mid) for mid in ids}

    def _validate_source_scope(self, session, grant, ids):
        from attention_router.infrastructure.models import ConversationMessageRow
        for mid in ids:
            row = session.get(ConversationMessageRow, mid, populate_existing=True)
            if row is None or (row.tenant_id, row.source_account) != (grant.tenant_id, grant.source_account):
                raise ValueError('INTERPRETATION_ARCHIVE_SCOPE')
            if (row.metadata_json or {}).get('platform_lineage', {}).get('classification') != 'SYNTHETIC':
                raise ValueError('INTERPRETATION_SYNTHETIC_ONLY')

    def propose(self, session, *, session_token, value, now):
        with self._current(session, session_token, now) as (state, grant):
            policy = self._policy(state, grant, now, capture=True)
            row, created = propose_interpretation(session, value, policy=policy, now=now)
            session.commit()  # lock held through commit; revocation linearizes after this write
            return row.id, created

    def capture_evidence(self, session, *, session_token, source, conversation_key, now):
        """Exercise PAUSE_CAPTURE at the existing archive boundary, lab only.

        Input is an already classified synthetic observation, not a WhatsApp
        authentication mechanism. No downloader, STT or production ingress hook.
        """
        from attention_router.application.semantic_interpretation import SourceEvidence
        from attention_router.application.memory import archive_incremental_message
        source = SourceEvidence.model_validate(source.model_dump())
        with self._current(session, session_token, now) as (state, grant):
            policy = self._policy(state, grant, now, capture=True)
            policy.require([source.message_id], source.tenant_id, source.source_account, now)
            fingerprint = stable_hash(source.model_dump(mode='json'))
            row, created = archive_incremental_message(session, source='wwebjs',
                source_account=source.source_account, thread_key=conversation_key, thread_type='DIRECT',
                source_message_id=source.message_id, actor_key=source.author, display_name='Synthetic participant',
                text=source.text, sent_at=source.sent_at, tenant_id=source.tenant_id,
                lineage_classification='SYNTHETIC', scenario_id='governance-lab', scenario_run_id='governance-capture',
                from_me=source.origin in {'UNKNOWN_FROM_ME', 'OWNER_MANUAL_OUTBOUND_OBSERVED', 'ROUTER_AUTOMATED_OUTBOUND_OBSERVED'},
                metadata={'speaker_basis': source.author_basis, 'event_origin': source.origin,
                          'synthetic_observation_hash': fingerprint,
                          'reviewed_event_ref': source.event_ref, 'transcription_id': source.transcription_id})
            policy.require([row.id], source.tenant_id, source.source_account, now)
            if not created:
                if row.metadata_json.get('synthetic_observation_hash') != fingerprint:
                    session.rollback()
                    raise ValueError('GOVERNANCE_CAPTURE_REPLAY_CONFLICT')
                return row.id, False
            row.message_type = source.modality
            # Inherit requested protection as well as the archive's own secret detector.
            if row.sensitivity_class != 'SECRET':
                row.sensitivity_class = source.sensitivity
                row.searchable = source.sensitivity != 'SECRET'
            if row.sensitivity_class == 'SECRET':
                row.text = row.normalized_text = '[REDACTED_SECRET]'
            session.commit()
            return row.id, created

    def _candidate(self, session, grant, candidate_id):
        row = session.get(CandidateInsightRow, candidate_id, populate_existing=True)
        if (row is None or row.tenant_id != grant.tenant_id
                or row.provenance.get('lineage_classification') != 'SYNTHETIC'
                or row.proposed_value.get('source_account') != grant.source_account):
            raise ValueError('GOVERNANCE_CANDIDATE_SCOPE')
        return row

    @staticmethod
    def _refs(row):
        payload = row.proposed_value
        return set(payload.get('source_message_ids', [])) | set(payload.get('references', [])) | set(payload.get('contradiction_refs', []))

    def review(self, session, *, session_token, candidate_id, action, decision_ref, now, replacement_id=None):
        from attention_router.application.candidate_insights import (
            admit_candidate_insight, reject_candidate_insight, supersede_candidate_insight,
        )
        if action not in {'ADMIT', 'REJECT', 'CONTEST', 'REVOKE_ASSOCIATION', 'SUPERSEDE', 'CORRECT'}:
            raise ValueError('GOVERNANCE_REVIEW_ACTION_INVALID')
        with self._current(session, session_token, now) as (state, grant):
            fingerprint = stable_hash([candidate_id, action, replacement_id])
            old = state['reviews'].get(decision_ref)
            if old and old['fingerprint'] != fingerprint:
                raise ValueError('GOVERNANCE_REPLAY_CONFLICT')
            if old and old['status'] == 'COMPLETE':
                return old
            row = self._candidate(session, grant, candidate_id)
            refs = self._refs(row)
            self._validate_source_scope(session, grant, refs)
            if action == 'ADMIT':
                self._sources(session, grant, refs)
                policy = self._policy(state, grant, now)
                policy.require(refs, grant.tenant_id, grant.source_account, now)
                if _utc(now) >= _utc(datetime.fromisoformat(row.provenance['retention_expires_at'])):
                    raise ValueError('GOVERNANCE_CANDIDATE_EXPIRED')
            if action in {'SUPERSEDE', 'CORRECT'}:
                replacement = self._candidate(session, grant, replacement_id)
                policy = self._policy(state, grant, now)
                policy.require(self._refs(replacement), grant.tenant_id, grant.source_account, now)
                self._sources(session, grant, self._refs(replacement))
            # Fence first; a DB failure cannot make rejected/superseded sources usable.
            if action != 'ADMIT':
                state['rejected'] = sorted(set(state['rejected']) | refs)
            record = dict(fingerprint=fingerprint, candidate_id=candidate_id, action=action,
                          replacement_id=replacement_id, status='PENDING', at=now.isoformat(),
                          owner_id=grant.owner_id, source_ids=sorted(refs))
            state['reviews'][decision_ref] = record
            self.journal.write(state)
            kwargs = dict(session=session, candidate_id=candidate_id, decision_actor_key=grant.owner_actor_key,
                          decision_ref=decision_ref, now=now)
            try:
                if action == 'ADMIT':
                    admit_candidate_insight(**kwargs)
                elif action in {'SUPERSEDE', 'CORRECT'}:
                    supersede_candidate_insight(**kwargs, replacement_id=replacement_id)
                else:
                    reject_candidate_insight(**kwargs)
                audit(session, None, 'semantic_lab.owner_review',
                      {'decision_ref': decision_ref, 'action': action, 'candidate_id': candidate_id},
                      tenant_id=grant.tenant_id, origin='SYNTHETIC_GOVERNANCE', created_at=now)
                session.commit()
            except Exception:
                session.rollback()
                raise
            record['status'] = 'COMPLETE'
            self.journal.write(state)
            return record

    def operate(self, session, *, session_token, request: LabOperation, now):
        request = LabOperation.model_validate(request.model_dump())
        with self._current(session, session_token, now) as (state, grant):
            old = state['operations'].get(request.request_id)
            fingerprint = stable_hash(request.model_dump(mode='json'))
            if old and old['fingerprint'] != fingerprint:
                raise ValueError('GOVERNANCE_REPLAY_CONFLICT')
            # Never cache/export previous content or stale deletion verification on replay.
            if old and old['status'] == 'COMPLETE' and request.operation not in {'EXPORT_CONTEXT', 'VERIFY_DELETION'}:
                return old
            if request.operation not in {'PAUSE_CAPTURE', 'REVOKE_CONSENT'} and not request.source_ids:
                raise ValueError('GOVERNANCE_EXPLICIT_SCOPE_REQUIRED')
            self._validate_source_scope(session, grant, request.source_ids)
            if request.operation == 'EXPORT_CONTEXT':
                policy = self._policy(state, grant, now)
                policy.require(request.source_ids, grant.tenant_id, grant.source_account, now)
                sources = self._sources(session, grant, request.source_ids)
            field = {'HIDE_CONTEXT': 'hidden', 'REJECT_INFERENCE': 'rejected', 'DELETE_DERIVATIVES': 'deleted'}.get(request.operation)
            if field:
                state[field] = sorted(set(state[field]) | set(request.source_ids))
            if request.operation == 'PAUSE_CAPTURE':
                state['paused'] = True
            if request.operation == 'REVOKE_CONSENT':
                state['revoked'] = True
            record = dict(operation=request.operation, fingerprint=fingerprint, status='PENDING',
                          source_ids=list(request.source_ids), at=now.isoformat(), owner_id=grant.owner_id)
            state['operations'][request.request_id] = record
            self.journal.write(state)
            if request.operation == 'DELETE_DERIVATIVES':
                policy = SyntheticRetention(tenant_id=grant.tenant_id, source_account=grant.source_account,
                    purpose=grant.purpose, consent=False, expires_at=now, tombstones=frozenset(request.source_ids))
                try:
                    record['candidate_rows_purged'] = purge_synthetic_derivatives(session, policy=policy)
                    record['episodes_purged'] = self._purge_episodes(session, grant, request.source_ids)
                except Exception:
                    session.rollback()
                    raise
            audit(session, None, 'semantic_lab.policy_operation',
                  {'request_id': request.request_id, 'operation': request.operation,
                   'source_count': len(request.source_ids)}, tenant_id=grant.tenant_id,
                  origin='SYNTHETIC_GOVERNANCE', created_at=now)
            session.commit()
            record['status'] = 'COMPLETE'
            self.journal.write(state)
            if request.operation == 'EXPORT_CONTEXT':
                return dict(record, lineage='SYNTHETIC', canonical_truth=False,
                            sources=[s.model_dump(mode='json') for s in sources.values()],
                            export_hash=stable_hash([s.model_dump(mode='json') for s in sources.values()]),
                            omissions=['MEDIA_BYTES', 'BACKUPS', 'CANONICAL_MEMORY'])
            if request.operation == 'VERIFY_DELETION':
                return self._verify(session, state, grant, request.source_ids)
            return record

    def _purge_episodes(self, session, grant, ids):
        """Only whole synthetic episodes; mixed/lineage/shared structures stay fenced."""
        from attention_router.infrastructure.semantic_episode_models import SemanticEpisodeRow, SemanticEpisodeMembershipRow
        from attention_router.infrastructure.candidate_insight_models import CandidateInsightEvidenceRow
        rows = session.scalars(select(SemanticEpisodeRow).where(SemanticEpisodeRow.tenant_id == grant.tenant_id)).all()
        count = 0
        for ep in rows:
            members = session.scalars(select(SemanticEpisodeMembershipRow).where(
                SemanticEpisodeMembershipRow.tenant_id == grant.tenant_id,
                SemanticEpisodeMembershipRow.episode_id == ep.id)).all()
            if (ep.provenance.get('lineage_classification') != 'SYNTHETIC' or not members
                    or any(m.member_type != 'CONVERSATION_MESSAGE' or m.member_ref not in ids for m in members)
                    or any(ep.id in [r.supersedes_episode_id, r.split_from_episode_id, *(r.merged_from_episode_ids or [])] for r in rows)
                    or session.scalar(select(CandidateInsightEvidenceRow.id).where(
                        CandidateInsightEvidenceRow.tenant_id == grant.tenant_id,
                        CandidateInsightEvidenceRow.evidence_type == 'SEMANTIC_EPISODE',
                        CandidateInsightEvidenceRow.source_ref == ep.id).limit(1))):
                continue
            for member in members:
                session.delete(member)
            session.flush()
            session.delete(ep)
            session.flush()
            count += 1
        return count

    def _verify(self, session, state, grant, ids):
        from attention_router.infrastructure.semantic_episode_models import SemanticEpisodeMembershipRow
        rows = session.scalars(select(CandidateInsightRow).where(CandidateInsightRow.tenant_id == grant.tenant_id)).all()
        remaining = [r.id for r in rows if r.proposed_value.get('source_account') == grant.source_account and self._refs(r) & set(ids)]
        episodes = list(session.scalars(select(SemanticEpisodeMembershipRow.episode_id).where(
            SemanticEpisodeMembershipRow.tenant_id == grant.tenant_id,
            SemanticEpisodeMembershipRow.member_type == 'CONVERSATION_MESSAGE',
            SemanticEpisodeMembershipRow.member_ref.in_(ids))).unique())
        return dict(status='PURGE_PARTIAL' if remaining or episodes else 'BACKUP_EXPIRY_PENDING',
                    use_blocked=set(ids) <= set(state['deleted']), remaining_candidates=remaining,
                    remaining_episodes=episodes,
                    retained=['ARCHIVE', 'MEDIA_UNVERIFIED', 'TRANSCRIPTIONS', 'BACKUPS_UNVERIFIED', 'MINIMAL_AUDIT'],
                    physical_deletion_complete=False)

    def candidates(self, session, *, session_token, now):
        """Owner review inventory, bounded; terminal evidence shown only on explicit review.

        Deleted/hidden/SECRET sources are not exposed even in this inventory.
        Rejected candidates remain inspectable as rejected, never retrieval truth.
        """
        with self._current(session, session_token, now) as (state, grant):
            self._policy(state, grant, now)
            rows = session.scalars(select(CandidateInsightRow).where(
                CandidateInsightRow.tenant_id == grant.tenant_id).order_by(CandidateInsightRow.id).limit(501)).all()
            if len(rows) > 500:
                raise ValueError('GOVERNANCE_CANDIDATE_LIMIT')
            result = []
            for row in rows:
                if row.proposed_value.get('source_account') != grant.source_account or row.provenance.get('lineage_classification') != 'SYNTHETIC':
                    continue
                refs = self._refs(row)
                if refs & set(state['deleted'] + state['hidden']) or translate_sensitivity(row.sensitivity_class).candidate_class == 'SECRET':
                    continue
                if not row.provenance.get('retention_expires_at') or _utc(now) >= _utc(datetime.fromisoformat(row.provenance['retention_expires_at'])):
                    continue
                try:
                    sources = self._sources(session, grant, refs)
                except ValueError:
                    continue
                result.append(dict(candidate_id=row.id, state=row.state, decision_ref=row.decision_ref,
                    canonical_truth=False, interpretation=row.proposed_value,
                    evidence=[s.model_dump(mode='json') for s in sources.values()]))
            return result

    def context(self, session, *, session_token, query, now):
        from attention_router.application.context_compiler import compile_graph_context
        from attention_router.infrastructure.models import ConversationMessageRow
        from attention_router.infrastructure.semantic_episode_models import SemanticEpisodeMembershipRow, SemanticEpisodeRow
        with self._current(session, session_token, now) as (state, grant):
            policy = self._policy(state, grant, now)
            denied = set(policy.tombstones)
            for decision in state['reviews'].values():
                if decision['status'] != 'COMPLETE':
                    denied.update(decision['source_ids'])
            rows = session.scalars(select(CandidateInsightRow).where(CandidateInsightRow.tenant_id == grant.tenant_id)).all()
            scoped = [r for r in rows if r.proposed_value.get('source_account') == grant.source_account
                      and r.provenance.get('lineage_classification') == 'SYNTHETIC']
            for row in scoped:
                if (row.state in {'REJECTED', 'SUPERSEDED'} or translate_sensitivity(row.sensitivity_class).candidate_class == 'SECRET'
                        or not row.provenance.get('retention_expires_at')
                        or _utc(now) >= _utc(datetime.fromisoformat(row.provenance['retention_expires_at']))):
                    denied |= self._refs(row)
            safe = set()
            safe_text = {}
            messages = session.scalars(select(ConversationMessageRow).where(
                ConversationMessageRow.tenant_id == grant.tenant_id,
                ConversationMessageRow.source_account == grant.source_account).limit(501)).all()
            if len(messages) > 500:
                raise ValueError('GOVERNANCE_SOURCE_LIMIT')
            for row in messages:
                if row.id not in denied:
                    try:
                        self._sources(session, grant, [row.id])
                        safe.add(row.id)
                        safe_text[row.id] = row.text or ''
                    except ValueError:
                        pass
            # Episode label/metadata are derived: exclude the whole episode if ANY
            # member is unauthorized, ambiguous, from another account, or expired.
            episodes = set()
            for ep in session.scalars(select(SemanticEpisodeRow).where(SemanticEpisodeRow.tenant_id == grant.tenant_id).limit(201)):
                members = session.scalars(select(SemanticEpisodeMembershipRow).where(
                    SemanticEpisodeMembershipRow.tenant_id == grant.tenant_id,
                    SemanticEpisodeMembershipRow.episode_id == ep.id)).all()
                if (members and ep.state in {'ACTIVE', 'CLOSED'}
                        and translate_sensitivity(ep.sensitivity_class).candidate_class != 'SECRET'
                        and all(m.member_type == 'CONVERSATION_MESSAGE' and not m.ambiguous and m.member_ref in safe for m in members)):
                    episodes.add(ep.id)
            def authorize(node):
                return (node.source_type == 'CONVERSATION_MESSAGE' and node.source_id in safe
                        or node.source_type == 'SEMANTIC_EPISODE' and node.source_id in episodes)
            packet = compile_graph_context(session, grant.tenant_id, query, now=now,
                                           node_authorizer=authorize, authorized_source_text=safe_text,
                                           token_budget=8000, item_budget=50)
            found = {i.source_id for i in packet.items if i.source_type == 'CONVERSATION_MESSAGE'}
            candidates = []
            for row in scoped:
                refs = self._refs(row)
                if refs and refs <= safe and refs & found and row.state in {'PROPOSED', 'NEEDS_REVIEW', 'ADMITTED'}:
                    candidates.append(dict(candidate_id=row.id, state=row.state, canonical_truth=False,
                        interpretation=row.proposed_value, evidence_ids=sorted(refs), decision_ref=row.decision_ref))
            from attention_router.application.semantic_interpretation import Interpretation, project_interpretations
            evidence_ids = {mid for candidate in candidates for mid in candidate['evidence_ids']}
            sources = self._sources(session, grant, evidence_ids)
            events = project_interpretations(
                [Interpretation.model_validate(c['interpretation']) for c in candidates], sources)
            result = dict(graph_context=packet.internal_payload(), candidate_insights=candidates,
                          event_context=events, context_incomplete=bool(denied or safe - found),
                          tenant_id=grant.tenant_id, source_account=grant.source_account,
                          grants_authority=False, policy_revision=state['revision'],
                          cache_expires_at=policy.expires_at.isoformat(),
                          decisions=list(state['reviews'].values()))
            result = json.loads(json.dumps(result, default=str))
            result['context_fingerprint'] = stable_hash(result)
            return result

    def cache_usable(self, session, *, session_token, cached, query, now):
        # Recompile, do not trust cached graph paths, status, classification or policy.
        try:
            fresh = self.context(session, session_token=session_token, query=query, now=now)
        except (ValueError, RuntimeError):
            return False
        # generated_at and recency scores change; compare content/state and revision.
        def identity(packet):
            return stable_hash([packet['tenant_id'], packet['source_account'], packet['policy_revision'], packet['candidate_insights'],
                [(i['source_id'], i['label'], i['attributes'], i['path']) for i in packet['graph_context']['items']]])
        return identity(fresh) == identity(cached)

    def history(self, session, *, session_token, now):
        with self._current(session, session_token, now) as (state, grant):
            return dict(revision=state['revision'], reviews=state['reviews'], operations=state['operations'])

    @staticmethod
    def canonical_promotion(*args, **kwargs):
        raise ValueError('D08_CANONICAL_PROMOTION_DISABLED')
