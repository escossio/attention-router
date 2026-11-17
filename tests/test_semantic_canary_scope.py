"""Small, synthetic-only acceptance of the existing coordinator's narrow scope."""
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from attention_router.api.v1.semantic_review_preview import build_semantic_review_preview_router
from attention_router.application.semantic_governance import CanaryScope, LabOperation, PolicyJournal
from attention_router.application.semantic_interpretation import SourceEvidence, interpret_offline
from attention_router.infrastructure.models import MemoryClaimRow, OutboxMessageRow
from tests.test_candidate_insights import _governed_lab, _governed_episode


def scoped_lab(session, tmp_path, *, messages=3, audio=1):
    lab, now, grant = _governed_lab(session, tmp_path)
    scope = CanaryScope(conversation_key='synthetic-selected-direct',
        observe_from=now-timedelta(seconds=1), observe_until=now+timedelta(minutes=2),
        max_messages=messages, max_audio_messages=audio)
    # Preserve the original fixture; initialize a separate explicitly scoped journal.
    lab.journal = PolicyJournal(tmp_path/'selected-policy.json')
    grant = grant.model_copy(update={'canary': scope})
    lab.initialize(session, session_token='synthetic-session', grant=grant, now=now)
    return lab, now, grant


def observation(grant, now, key='voice', *, voice=True, outgoing=True):
    return SourceEvidence(tenant_id=grant.tenant_id, source_account=grant.source_account,
        message_id=key, author=grant.owner_actor_key if outgoing else 'synthetic-peer',
        author_basis='OWNER_ACCOUNT_ATTRIBUTED_BY_POLICY_V1' if outgoing else 'EXTERNAL_SENDER',
        origin='UNKNOWN_FROM_ME' if outgoing else 'EXTERNAL_INBOUND', sent_at=now,
        text='Confirmo domingo.', modality='VOICE' if voice else 'TEXT',
        transcription_id='synthetic-stt-reference' if voice else None)


def capture(lab, session, grant, now, source):
    return lab.capture_evidence(session, session_token='synthetic-session', source=source,
        conversation_key=grant.canary.conversation_key, now=now)


def test_scoped_review_context_revoke_and_stop(session, tmp_path):
    from attention_router.application.semantic_interpretation import archived_source
    from attention_router.infrastructure.models import ConversationMessageRow
    lab, now, grant = scoped_lab(session, tmp_path, audio=2)
    mids = []
    for outgoing in (False, True):
        mid, created = capture(lab, session, grant, now,
            observation(grant, now, key=str(outgoing), outgoing=outgoing))
        assert created
        mids.append(mid)
    _governed_episode(session, [session.get(ConversationMessageRow, mid) for mid in mids])
    cids = []
    for mid in mids:
        source = archived_source(session, tenant_id=grant.tenant_id,
                                 source_account=grant.source_account, message_id=mid)
        cid, _ = lab.propose(session, session_token='synthetic-session',
                             value=interpret_offline(source), now=now)
        cids.append(cid)
    app = FastAPI()
    app.include_router(build_semantic_review_preview_router(get_session=lambda: session,
        service=lab, clock=lambda: now))
    client = TestClient(app, headers={'Authorization': 'Bearer synthetic-session'})
    base = '/api/v1/client/semantic-preview'
    assert len(client.get(base+'/candidates').json()) == 2
    reply = client.post(base+'/review', json=dict(candidate_id=cids[1], action='ADMIT',
                                                 decision_ref='synthetic-owner-review'))
    assert reply.status_code == 200
    packet = client.post(base+'/context', json={'query':'domingo'}).json()
    assert {i['source_id'] for i in packet['graph_context']['items']
            if i['source_type'] == 'CONVERSATION_MESSAGE'} == set(mids)
    assert not packet['grants_authority']
    assert all(not c['canonical_truth'] for c in packet['candidate_insights'])
    paused = client.post(base+'/operations', json=dict(request_id='pause', operation='PAUSE_CAPTURE', source_ids=[]))
    assert paused.status_code == 200
    with pytest.raises(ValueError, match='CAPTURE_PAUSED'):
        capture(lab, session, grant, now, observation(grant, now, key='after-pause'))
    assert client.post(base+'/context', json={'query':'domingo'}).status_code == 200
    revoked = client.post(base+'/operations', json=dict(request_id='revoke', operation='REVOKE_CONSENT', source_ids=[]))
    assert revoked.status_code == 200
    assert client.post(base+'/context', json={'query':'domingo'}).status_code == 409
    assert not lab.cache_usable(session, session_token='synthetic-session', cached=packet, query='domingo', now=now)
    assert len(session.scalars(select(ConversationMessageRow)).all()) == 2
    assert not session.scalars(select(MemoryClaimRow)).all()
    assert not session.scalars(select(OutboxMessageRow)).all()


@pytest.mark.parametrize('change', ['tenant', 'account', 'conversation', 'before', 'future', 'end'])
def test_selection_denies_outside_scope(session, tmp_path, change):
    lab, now, grant = scoped_lab(session, tmp_path)
    source = observation(grant, now)
    conversation = grant.canary.conversation_key
    if change == 'tenant':
        source = source.model_copy(update={'tenant_id':'synthetic-other-tenant'})
    elif change == 'account':
        source = source.model_copy(update={'source_account':'synthetic-other-account'})
    elif change == 'conversation':
        conversation = 'synthetic-other-direct'
    elif change == 'before':
        source = source.model_copy(update={'sent_at':grant.canary.observe_from-timedelta(seconds=1)})
    elif change == 'future':
        source = source.model_copy(update={'sent_at':now+timedelta(seconds=1)})
    else:
        now = grant.canary.observe_until
    with pytest.raises(ValueError):
        lab.capture_evidence(session, session_token='synthetic-session', source=source,
                             conversation_key=conversation, now=now)


def test_budget_replay_conflict_and_restart(session, tmp_path):
    from attention_router.application.semantic_governance import GovernedSemanticLab
    lab, now, grant = scoped_lab(session, tmp_path, messages=2)
    voice = observation(grant, now)
    mid, created = capture(lab, session, grant, now, voice)
    assert created
    restarted = GovernedSemanticLab(journal=PolicyJournal(lab.journal.path), client_sessions=lab.client_sessions)
    assert capture(restarted, session, grant, now, voice) == (mid, False)
    with pytest.raises(ValueError, match='BUDGET_EXHAUSTED'):
        capture(restarted, session, grant, now, observation(grant, now, key='voice-2'))
    with pytest.raises(ValueError, match='REPLAY_CONFLICT'):
        capture(restarted, session, grant, now, voice.model_copy(update={'text':'Cancelo domingo.'}))
    capture(restarted, session, grant, now, observation(grant, now, key='text', voice=False))
    with pytest.raises(ValueError, match='BUDGET_EXHAUSTED'):
        capture(restarted, session, grant, now, observation(grant, now, key='text-3', voice=False))


@pytest.mark.parametrize('failure', ['owner', 'tenant', 'role', 'policy-lost', 'expired', 'organic'])
def test_canary_cannot_supply_missing_authority(session, tmp_path, failure):
    lab, now, grant = scoped_lab(session, tmp_path)
    source = observation(grant, now)
    authority = lab.client_sessions.authenticated_bootstrap.return_value
    if failure == 'owner':
        authority.human_identity_id = 'synthetic-other-owner'
    elif failure == 'tenant':
        authority.active_tenant_id = 'synthetic-other-tenant'
    elif failure == 'role':
        authority.memberships[0].role = 'MEMBER'
    elif failure == 'policy-lost':
        lab.journal.path.unlink()  # disposable fixture, never an operational policy
    elif failure == 'expired':
        now = max(grant.deadlines.values())
    else:
        source = source.model_copy(update={'lineage':'ORGANIC'})
    with pytest.raises(ValueError):
        capture(lab, session, grant, now, source)


def test_other_conversation_cannot_enter_by_proposal_review_or_export(session, tmp_path):
    from attention_router.application.semantic_interpretation import archived_source, propose_interpretation
    from tests.test_candidate_insights import _language_archive
    lab, now, grant = scoped_lab(session, tmp_path)
    other = _language_archive(session, 'Confirmo domingo.', 'outside')
    session.commit()
    source = archived_source(session, tenant_id=grant.tenant_id, source_account=grant.source_account, message_id=other.id)
    value = interpret_offline(source)
    with pytest.raises(ValueError, match='CANARY_SOURCE_OUTSIDE'):
        lab.propose(session, session_token='synthetic-session', value=value, now=now)
    # Existing unscoped lab candidate must not bypass the scoped owner facade.
    policy = lab._policy(lab.journal.read(), grant, now)
    candidate, _ = propose_interpretation(session, value, policy=policy, now=now)
    session.commit()
    assert lab.candidates(session, session_token='synthetic-session', now=now) == []
    with pytest.raises(ValueError, match='CANARY_SOURCE_OUTSIDE'):
        lab.review(session, session_token='synthetic-session', candidate_id=candidate.id,
                   action='ADMIT', decision_ref='outside', now=now)
    with pytest.raises(ValueError, match='CANARY_SOURCE_OUTSIDE'):
        lab.operate(session, session_token='synthetic-session', request=LabOperation(
            request_id='export', operation='EXPORT_CONTEXT', source_ids=(other.id,)), now=now)
    assert lab.context(session, session_token='synthetic-session', query='domingo', now=now)['graph_context']['items'] == []


@pytest.mark.parametrize('change', ['naive', 'zero-window', 'zero-budget', 'audio-budget', 'boolean-budget'])
def test_no_implicit_canary_window_or_budget(change):
    from datetime import UTC, datetime
    now = datetime(2026, 1, 1, tzinfo=UTC)
    values = dict(conversation_key='synthetic-direct', observe_from=now,
        observe_until=now+timedelta(minutes=1), max_messages=2, max_audio_messages=1)
    values.update({'naive':{'observe_from':now.replace(tzinfo=None)},
                   'zero-window':{'observe_until':now}, 'zero-budget':{'max_messages':0},
                   'audio-budget':{'max_audio_messages':3}, 'boolean-budget':{'max_messages':True}}[change])
    with pytest.raises(ValueError):
        CanaryScope(**values)


def original_canary(session, tmp_path, *, owner=None, now=None, conversation='synthetic-original-direct', deadlines=None):
    """Synthetic test content exercises ORGANIC wire lineage without reclassification."""
    from attention_router.application.semantic_canary import CanaryGrant, CanaryJournal, GovernedSemanticCanary
    from attention_router.application.semantic_policy_anchor import AnchoredPolicyJournal, SyntheticFileAuthority
    from attention_router.application.semantic_governance import Layer
    if owner is None:
        base, stamp, old = _governed_lab(session, tmp_path)
        owner = old.owner_actor_key
        authority = base.client_sessions
    else:
        from datetime import UTC, datetime
        from types import SimpleNamespace
        from unittest.mock import Mock
        stamp = datetime.now(UTC)
        authority = Mock()
        from attention_router.core.tenancy import DEFAULT_TENANT_ID
        authority.authenticated_bootstrap.return_value = SimpleNamespace(
            active_tenant_id=DEFAULT_TENANT_ID, human_identity_id='synthetic-owner',
            memberships=[SimpleNamespace(tenant_id=DEFAULT_TENANT_ID, role='OWNER', status='ACTIVE')])
    now = now or stamp
    from attention_router.infrastructure.human_identity_models import HumanIdentityRow
    from attention_router.infrastructure.client_bootstrap_models import ClientTenantMembershipRow
    from attention_router.core.tenancy import DEFAULT_TENANT_ID
    if session.get(HumanIdentityRow, 'synthetic-owner') is None:
        session.add(HumanIdentityRow(id='synthetic-owner', created_at=now))
        session.flush()
    session.add(ClientTenantMembershipRow(id='synthetic-canary-membership',
        human_identity_id='synthetic-owner', tenant_id=DEFAULT_TENANT_ID, role='OWNER',
        status='ACTIVE', created_at=now, updated_at=now))
    session.commit()
    anchor_dir = tmp_path/'independent-canary-authority'
    anchor_dir.mkdir()
    anchor = SyntheticFileAuthority(anchor_dir/'revision.json')
    anchor.initialize_synthetic()
    journal = AnchoredPolicyJournal(tmp_path/'canary.json', anchor, journal_type=CanaryJournal)
    from attention_router.core.tenancy import DEFAULT_TENANT_ID
    grant = CanaryGrant(tenant_id=DEFAULT_TENANT_ID, source_account='synthetic',
        owner_id='synthetic-owner', owner_actor_key=owner, purpose='SEMANTIC_CANARY', lineage='ORGANIC',
        consent=True, policy_version='SYNTHETIC_TEST_POLICY', starts_at=now-timedelta(minutes=5),
        canary=CanaryScope(conversation_key=conversation, observe_from=now-timedelta(minutes=5),
            observe_until=now+timedelta(minutes=5), max_messages=10, max_audio_messages=5),
        deadlines=deadlines or {layer:now+timedelta(hours=1) for layer in Layer.__args__},
        canary_id='synthetic-canary', operational_decision_ref='SYNTHETIC_NOT_OWNER_APPROVAL',
        retention_decision_ref='SYNTHETIC_TTL', backup_decision_ref='SYNTHETIC_BACKUPS')
    service = GovernedSemanticCanary(journal=journal, client_sessions=authority, enabled=True)
    service.initialize(session, session_token='synthetic-session', grant=grant, now=grant.canary.observe_from)
    session.commit()
    return service, now, grant


def original_message(session, grant, now, *, key='original', text='Confirmo domingo.', voice=False):
    from attention_router.application.memory import archive_incremental_message
    row, _ = archive_incremental_message(session, source='wwebjs', source_account=grant.source_account,
        thread_key=grant.canary.conversation_key, thread_type='DIRECT', source_message_id=key,
        actor_key=grant.owner_actor_key, display_name='Synthetic owner', text=text,
        sent_at=now, tenant_id=grant.tenant_id, lineage_classification='ORGANIC', from_me=True,
        metadata={'speaker_basis':'OWNER_ACCOUNT_ATTRIBUTED_BY_POLICY_V1',
            'event_origin':'UNKNOWN_FROM_ME', 'transcription_id':'synthetic-stt' if voice else None})
    row.message_type = 'VOICE' if voice else 'TEXT'
    session.commit()
    return row


def test_original_message_candidate_review_query_revoke_no_mirror(session, tmp_path):
    from attention_router.application.semantic_interpretation import _utc
    from attention_router.infrastructure.models import ConversationMessageRow
    from attention_router.infrastructure.candidate_insight_models import CandidateInsightRow
    from attention_router.application.memory import search_archive, extract_candidates
    service, now, grant = original_canary(session, tmp_path)
    row = original_message(session, grant, now, voice=True)
    original = (row.id, row.source_message_id, _utc(row.sent_at), row.metadata_json['platform_lineage'])
    app = FastAPI()
    app.include_router(build_semantic_review_preview_router(get_session=lambda: session, service=service,
                        clock=lambda: now, include_processing=True))
    client = TestClient(app, headers={'Authorization':'Bearer synthetic-session'})
    base = '/api/v1/client/semantic-preview'
    first = client.post(base+'/process', json={'message_id':row.id})
    assert first.status_code == 200, first.text
    cid = first.json()['candidate_id']
    assert cid and first.json()['created']
    assert not client.post(base+'/process', json={'message_id':row.id}).json()['created']
    candidate = session.get(CandidateInsightRow, cid)
    assert candidate.provenance['lineage_classification'] == 'ORGANIC'
    assert candidate.proposed_value['source_message_ids'] == [row.id]
    assert len(session.scalars(select(ConversationMessageRow)).all()) == 1
    assert (row.id, row.source_message_id, _utc(row.sent_at), row.metadata_json['platform_lineage']) == original
    assert row.metadata_json['event_origin'] == 'UNKNOWN_FROM_ME'
    assert not row.searchable
    assert not search_archive(session, 'domingo', tenant_id=grant.tenant_id)
    assert not extract_candidates(session, row, None)
    assert client.post(base+'/review', json=dict(candidate_id=cid, action='ADMIT', decision_ref='synthetic-admit')).status_code == 200
    context = client.post(base+'/context', json={'query':'domingo'}).json()
    assert context['candidate_insights'][0]['candidate_id'] == cid
    assert context['graph_context']['items'][0]['source_id'] == row.id
    assert not context['candidate_insights'][0]['canonical_truth'] and not context['grants_authority']
    assert client.post(base+'/review', json=dict(candidate_id=cid, action='REJECT', decision_ref='synthetic-reject')).status_code == 200
    assert not client.post(base+'/context', json={'query':'domingo'}).json()['graph_context']['items']
    assert client.post(base+'/process', json={'message_id':row.id}).status_code == 409
    assert not service.cache_usable(session, session_token='synthetic-session', cached=context, query='domingo', now=now)
    assert not session.scalars(select(MemoryClaimRow)).all()
    assert not session.scalars(select(OutboxMessageRow)).all()


@pytest.mark.parametrize('case', ['disabled', 'unanchored', 'restore', 'scope', 'secret', 'automation', 'missing', 'expired'])
def test_original_canary_negative_boundaries(session, tmp_path, case):
    from attention_router.application.semantic_canary import CanaryJournal
    service, now, grant = original_canary(session, tmp_path)
    row = original_message(session, grant, now)
    if case == 'disabled':
        service.enabled = False
    elif case == 'unanchored':
        service.journal = CanaryJournal(service.journal.path)
    elif case == 'restore':
        before = service.journal.path.read_bytes()
        service.operate(session, session_token='synthetic-session', request=LabOperation(
            request_id='revoke', operation='REVOKE_CONSENT', source_ids=()), now=now)
        service.journal.path.write_bytes(before)
    elif case == 'scope':
        row.source_account = 'synthetic-other-account'
        session.commit()
    elif case == 'secret':
        row.sensitivity_class = 'SECRET'
        session.commit()
    elif case == 'automation':
        row.metadata_json = {**row.metadata_json, 'event_origin':'ROUTER_AUTOMATED_OUTBOUND_OBSERVED', 'speaker_basis':'AUTOMATED'}
        session.commit()
    elif case == 'missing':
        row.text = None
        session.commit()
    else:
        now = max(grant.deadlines.values())
    if case in {'automation', 'missing'}:
        result = service.process_message(session, session_token='synthetic-session', message_id=row.id, now=now)
        assert result['candidate_id'] is None
        assert result['status'] == ('SOURCE_INCOMPLETE' if case == 'missing' else 'AUTOMATED_EVIDENCE_ONLY')
    else:
        with pytest.raises(ValueError):
            service.process_message(session, session_token='synthetic-session', message_id=row.id, now=now)


def test_canary_capture_is_quarantined_before_commit_without_action_queue(session, tmp_path, monkeypatch):
    from tests.test_conversation_live_capture import inbound, STAMP
    from attention_router.config import settings
    from attention_router.infrastructure.models import ConversationMessageRow, InteractionRow, QueueRow
    event = inbound('synthetic-canary-original', 'Proponho viagem domingo.',
                    metadata={'source_account':'synthetic', 'is_group':False, 'peer_id_kind':'lid'})
    service, now, grant = original_canary(session, tmp_path, now=STAMP,
                                        conversation=event.metadata['conversation_key'])
    monkeypatch.setattr(settings, 'conversation_capture_enabled', True)
    monkeypatch.setattr(settings, 'conversation_capture_start_at', grant.starts_at)
    monkeypatch.setattr(settings, 'conversation_capture_tenant_id', grant.tenant_id)
    first = service.observe_trusted(session, event=event, now=now)
    row = session.get(ConversationMessageRow, first['message_id'])
    assert not row.searchable and row.metadata_json['semantic_canary_id'] == grant.canary_id
    assert row.source_message_id == event.external_event_id
    assert row.metadata_json['platform_lineage']['classification'] == 'ORGANIC'
    assert service.observe_trusted(session, event=event, now=now)['status'] == 'duplicate'
    assert not session.scalars(select(InteractionRow)).all()
    assert not session.scalars(select(QueueRow)).all()
    assert not session.scalars(select(OutboxMessageRow)).all()
    response = service.process_message(session, session_token='synthetic-session', message_id=row.id, now=now)
    assert response['candidate_id']
    service.operate(session, session_token='synthetic-session', request=LabOperation(
        request_id='stop', operation='REVOKE_CONSENT', source_ids=()), now=now)
    with pytest.raises(ValueError, match='CONSENT_OR_RETENTION'):
        service.observe_trusted(session, event=event, now=now)


def test_canary_internal_route_requires_existing_hmac_and_explicit_tenant(session, tmp_path):
    from attention_router.api.v1.semantic_canary_ingress import build_semantic_canary_ingress_router
    import hashlib
    import hmac
    import time
    from attention_router.config import settings
    service, now, _ = original_canary(session, tmp_path)
    app = FastAPI()
    app.include_router(build_semantic_canary_ingress_router(get_session=lambda: session, service=service))
    client = TestClient(app)
    route = '/api/v1/ingress/internal/semantic-canary/events'
    assert client.post(route, json={}).status_code == 401
    timestamp = str(int(time.time()))
    body = b'{}'
    signature = 'sha256='+hmac.new(settings.internal_ingress_hmac_secret.encode(),
        timestamp.encode()+b'.'+body, hashlib.sha256).hexdigest()
    headers = {'X-Attention-Timestamp':timestamp, 'X-Attention-Signature':signature,
               'Content-Type':'application/json'}
    assert client.post(route, content=body, headers=headers).status_code == 422
    assert client.post(route, content=b'{"tenant_id":"synthetic-other"}', headers=headers).status_code == 401


def test_canary_synthetic_authority_cannot_activate_outside_test(session, tmp_path, monkeypatch):
    from attention_router.config import settings
    service, now, _ = original_canary(session, tmp_path)
    monkeypatch.setattr(settings, 'app_env', 'production')
    with pytest.raises(ValueError, match='SYNTHETIC_AUTHORITY_FORBIDDEN'):
        service.context(session, session_token='synthetic-session', query='domingo', now=now)


@pytest.mark.parametrize('during_call', [False, True])
def test_canary_stt_deadline_discards_late_result(session, tmp_path, monkeypatch, during_call):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from tests.test_whatsapp_voice_flow import setup_direct, voice_event, attach_media, services
    from attention_router.application import voice_transcription as worker
    from attention_router.infrastructure.models import VoiceTranscriptionRow
    from attention_router.config import settings
    setup_direct(session, monkeypatch)
    event = voice_event()
    received = services.receive_normalized_inbound_event(session, event)
    attach_media(session, monkeypatch, tmp_path, {**received, 'external_event_id':event.external_event_id})
    from attention_router.infrastructure.models import ActorBindingRow
    owner = session.scalar(select(ActorBindingRow).where(ActorBindingRow.actor_category == 'owner'))
    service, now, grant = original_canary(session, tmp_path, owner=owner.actor_key)
    row = original_message(session, grant, now, text='', voice=True)
    row.metadata_json = {**row.metadata_json, 'semantic_canary_id':grant.canary_id,
                        'source_inbound_event_id':received['inbound_event_id']}
    session.commit()
    monkeypatch.setattr(settings, 'conversation_capture_enabled', False)
    clock = [now]
    monkeypatch.setattr(worker, 'now_utc', lambda: clock[0])
    calls = []
    deadline = now+timedelta(seconds=1) if during_call else now
    @contextmanager
    def guard(db, event_id):
        yield {'processing_until':deadline, 'media_expires_at':now+timedelta(hours=1)}
    def transcribe(*_):
        calls.append(True)
        clock[0] = deadline
        return dict(transcript='synthetic late result', provider='synthetic', model='fixture', request_id='fixture')
    worker.process_voice_transcriptions(session, 'fixture', client=SimpleNamespace(transcribe=transcribe), canary_guard=guard)
    result = session.scalar(select(VoiceTranscriptionRow))
    assert result.status == 'FAILED' and result.error_code == 'CANARY_PROCESSING_EXPIRED'
    assert result.transcript_text is None and len(calls) == int(during_call)


def test_canary_expired_pending_capture_has_terminal_state(session, tmp_path):
    from attention_router.application.conversation_live_capture import expire_pending_capture
    service, now, grant = original_canary(session, tmp_path)
    row = original_message(session, grant, now, text='', voice=True)
    service.enroll(session, session_token='synthetic-session', message_id=row.id, now=now)
    row.metadata_json = {**row.metadata_json, 'capture_contract':'LIVE_CONVERSATION_ARCHIVE_V0',
        'content_state':'PENDING_TRANSCRIPTION', 'source_received_at':now.isoformat(),
        'source_inbound_event_id':'synthetic-missing-receipt'}
    session.commit()
    assert expire_pending_capture(session, now=grant.canary.observe_until) == 1
    assert row.metadata_json['content_state'] == 'CANARY_PROCESSING_EXPIRED'
    assert row.text == '' and not row.searchable
    assert expire_pending_capture(session, now=grant.canary.observe_until) == 0


@pytest.mark.parametrize('reviewed', [False, True])
def test_can_ret_01_separate_retention_and_no_replay_extension(session, tmp_path, reviewed):
    from datetime import UTC, datetime
    from attention_router.application.semantic_canary import canary_reference_rules
    from attention_router.application.semantic_governance import Layer
    from attention_router.application.semantic_retention import RetentionSchedule
    from attention_router.infrastructure.candidate_insight_models import CandidateInsightRow
    now = datetime(2026, 1, 1, tzinfo=UTC)
    schedule = RetentionSchedule(tenant_id='synthetic', source_account='synthetic', owner_id='synthetic',
        version='CAN-RET-01', lineage='SYNTHETIC', effective_at=now, expires_at=now+timedelta(days=40),
        rules=canary_reference_rules())
    def deadline(kind, purpose='SEMANTIC_CANARY', at=now):
        return schedule.deadline(tenant_id='synthetic', source_account='synthetic', owner_id='synthetic',
            data_class=kind, purpose=purpose, created_at=now, now=at, consent=True)
    assert deadline('MEDIA') == now+timedelta(days=7)
    assert deadline('TRANSCRIPTION') == deadline('MESSAGE') == now+timedelta(days=30)
    pending = deadline('CANDIDATE', 'SEMANTIC_CANARY_UNREVIEWED')
    admitted = deadline('CANDIDATE', 'SEMANTIC_CANARY_REVIEWED')
    assert pending == now+timedelta(days=7) and admitted == now+timedelta(days=30)
    assert deadline('MESSAGE', at=now+timedelta(days=10)) == deadline('MESSAGE')
    with pytest.raises(ValueError, match='RULE_MISSING'):
        deadline('BACKUP')  # Reference policy does not presume backup coverage.
    # All other class deadlines below are explicit synthetic fixture values.
    deadlines = {layer:admitted for layer in Layer.__args__}
    deadlines.update(MEDIA=deadline('MEDIA'), CANDIDATE=pending, INTERPRETATION=pending)
    service, _, grant = original_canary(session, tmp_path, now=now, deadlines=deadlines)
    row = original_message(session, grant, now)
    result = service.process_message(session, session_token='synthetic-session', message_id=row.id, now=now)
    candidate = session.get(CandidateInsightRow, result['candidate_id'])
    original = candidate.provenance['retention_expires_at']
    assert datetime.fromisoformat(original) == pending
    assert not service.process_message(session, session_token='synthetic-session', message_id=row.id,
                                       now=now+timedelta(minutes=1))['created']
    assert candidate.provenance['retention_expires_at'] == original
    if reviewed:
        for decision, at in [('synthetic-admit', now), ('synthetic-admit', now+timedelta(days=2)),
                             ('synthetic-repeat', now+timedelta(days=8))]:
            service.review(session, session_token='synthetic-session', candidate_id=candidate.id,
                action='ADMIT', decision_ref=decision, now=at)
            assert datetime.fromisoformat(candidate.provenance['retention_expires_at']) == admitted
        assert candidate.provenance['unreviewed_retention_expires_at'] == original
    at = now+timedelta(days=8)
    candidates = service.candidates(session, session_token='synthetic-session', now=at)
    assert bool(candidates) is reviewed
    context = service.context(session, session_token='synthetic-session', query='domingo', now=at)
    assert bool(context['candidate_insights']) is reviewed
    # The original evidence has its independent 30-day access even if a proposal expires.
    assert service.conversation(session, session_token='synthetic-session', now=at)['messages'][0]['text']
    if not reviewed:
        with pytest.raises(ValueError, match='CANDIDATE_EXPIRED'):
            service.review(session, session_token='synthetic-session', candidate_id=candidate.id,
                action='ADMIT', decision_ref='too-late', now=at)
    with pytest.raises(ValueError, match='RETENTION_DENIED'):
        service.context(session, session_token='synthetic-session', query='domingo', now=admitted)
    assert row.text and not row.searchable  # access fence, not a real purge
    assert not session.scalars(select(MemoryClaimRow)).all()


def test_can01_consent_receipt_binds_scope_and_blocks_retroactive_source(session, tmp_path):
    from attention_router.infrastructure.hashing import stable_hash
    service, now, grant = original_canary(session, tmp_path)
    state = service.journal.read()
    receipt = state['consent_receipt']
    assert receipt['grant_digest'] == stable_hash(grant.model_dump(mode='json'))
    assert receipt['owner_id'] == grant.owner_id and receipt['grants_authority'] is False
    assert 'token' not in str(receipt)
    with pytest.raises(ValueError, match='POLICY_ALREADY_EXISTS'):
        service.initialize(session, session_token='synthetic-session', grant=grant, now=now)
    assert service.journal.read() == state
    # Consent occurs after the proposed window begins: old source is still denied.
    state['consent_receipt']['authorized_at'] = now.isoformat()
    with service.journal.locked():
        service.journal.write(state)
    old = original_message(session, grant, now-timedelta(seconds=1), key='pre-consent')
    with pytest.raises(ValueError, match='BEFORE_CONSENT'):
        service.enroll(session, session_token='synthetic-session', message_id=old.id, now=now)
    session.rollback()
    assert old.searchable
    state = service.journal.read()
    state['grant']['canary']['max_messages'] = 11
    with service.journal.locked():
        service.journal.write(state)
    with pytest.raises(ValueError, match='CONSENT_RECEIPT_INVALID'):
        service.candidates(session, session_token='synthetic-session', now=now)


@pytest.mark.parametrize('change', ['minutes', 'messages', 'audio'])
def test_can01_scope_never_silently_expands(session, tmp_path, change):
    from attention_router.application.semantic_canary import CanaryGrant
    _, now, grant = original_canary(session, tmp_path)
    value = grant.model_dump()
    value['canary'].update({'minutes': {'observe_until': now+timedelta(minutes=56)},
        'messages': {'max_messages': 21}, 'audio': {'max_audio_messages': 7}}[change])
    with pytest.raises(ValueError, match='MAXIMUM_SCOPE_EXCEEDED'):
        CanaryGrant.model_validate(value)


def test_can01_actual_app_mounts_preserve_defaults_and_authentication(session, tmp_path, monkeypatch):
    from attention_router.web import app as public
    from attention_router.web.internal_ingress_app import create_internal_ingress_app
    service, now, _ = original_canary(session, tmp_path)
    prefix = '/api/v1/client/semantic-preview'
    assert not any(getattr(r, 'path', '').startswith(prefix) for r in public.app.routes)
    ordinary = create_internal_ingress_app()
    assert not any('semantic-canary' in getattr(r, 'path', '') for r in ordinary.routes)
    internal = create_internal_ingress_app(canary_service=service)
    assert TestClient(internal).post('/api/v1/ingress/internal/semantic-canary/events', json={}).status_code == 401
    preview = FastAPI()
    with pytest.raises(ValueError, match='EXISTING_CLIENT_SESSION'):
        public.mount_semantic_canary_review(preview, service=service)
    monkeypatch.setattr(public, 'client_session_service', service.client_sessions)
    public.mount_semantic_canary_review(preview, service=service)
    preview.dependency_overrides[public.get_session] = lambda: session
    client = TestClient(preview)
    assert client.get(prefix+'/candidates').status_code == 401
    client.headers['Authorization'] = 'Bearer synthetic-session'
    assert client.get(prefix+'/candidates').status_code == 200
    assert client.get(prefix+'/candidates').headers['cache-control'] == 'no-store'
    assert prefix+'/process' in preview.openapi()['paths']
    assert prefix+'/conversation' in preview.openapi()['paths']
    with pytest.raises(ValueError, match='ALREADY_MOUNTED'):
        public.mount_semantic_canary_review(preview, service=service)
    service.enabled = False
    assert client.get(prefix+'/candidates').status_code == 409
    assert not session.scalars(select(OutboxMessageRow)).all()


def test_can01_preconsent_ingress_creates_no_receipt(session, tmp_path):
    from tests.test_conversation_live_capture import inbound, STAMP
    from attention_router.infrastructure.models import InboundEventRow
    event = inbound('synthetic-before-consent', 'Proponho viagem domingo.',
        metadata={'source_account': 'synthetic', 'is_group': False, 'peer_id_kind': 'lid'})
    service, now, _ = original_canary(session, tmp_path, now=STAMP,
                                    conversation=event.metadata['conversation_key'])
    state = service.journal.read()
    state['consent_receipt']['authorized_at'] = (now+timedelta(seconds=1)).isoformat()
    with service.journal.locked():
        service.journal.write(state)
    with pytest.raises(ValueError, match='OBSERVATION_SCOPE_DENIED'):
        service.observe_trusted(session, event=event, now=now+timedelta(seconds=2))
    assert not session.scalars(select(InboundEventRow)).all()


@pytest.mark.parametrize('processing', [False, True])
def test_can01_unprocessed_media_expiry_uses_existing_cleanup(session, tmp_path, monkeypatch, processing):
    from tests.test_whatsapp_voice_flow import setup_direct, voice_event, attach_media
    from attention_router.application import services
    from attention_router.application.conversation_live_capture import expire_pending_capture
    from attention_router.application.voice_media import cleanup_expired_media
    from attention_router.infrastructure.media_store import MediaStore
    from attention_router.infrastructure.models import ActorBindingRow, VoiceTranscriptionRow
    from attention_router.config import settings
    setup_direct(session, monkeypatch)
    event = voice_event()
    received = services.receive_normalized_inbound_event(session, event)
    artifact = attach_media(session, monkeypatch, tmp_path/'synthetic-media', {
        **received, 'external_event_id': event.external_event_id})
    owner = session.scalar(select(ActorBindingRow).where(ActorBindingRow.actor_category == 'owner'))
    service, now, grant = original_canary(session, tmp_path, owner=owner.actor_key)
    row = original_message(session, grant, now, text='', voice=True)
    service.enroll(session, session_token='synthetic-session', message_id=row.id, now=now)
    row.metadata_json = {**row.metadata_json, 'capture_contract':'LIVE_CONVERSATION_ARCHIVE_V0',
        'content_state':'PENDING_TRANSCRIPTION', 'source_received_at':now.isoformat(),
        'source_inbound_event_id':artifact.inbound_event_id}
    expiry = grant.deadlines['MEDIA']
    artifact.expires_at = expiry
    transcription = session.scalar(select(VoiceTranscriptionRow))
    transcription.status = 'PROCESSING' if processing else 'PENDING'
    session.commit()
    monkeypatch.setattr(settings, 'conversation_capture_tenant_id', grant.tenant_id)
    assert expire_pending_capture(session, now=grant.canary.observe_until) == (0 if processing else 1)
    from attention_router.application.semantic_interpretation import _utc
    assert _utc(artifact.expires_at) == expiry
    if processing:
        assert artifact.terminal_at is None
        return
    assert transcription.status == 'FAILED'
    assert artifact.terminal_at is not None
    media_path = MediaStore(tmp_path/'synthetic-media', settings.whatsapp_media_max_bytes).path_for_digest(artifact.content_sha256)
    assert media_path.exists()
    monkeypatch.setattr('attention_router.application.voice_media.now_utc', lambda: expiry)
    assert cleanup_expired_media(session) == 1
    assert not media_path.exists()
    assert cleanup_expired_media(session) == 0
    assert row.text == '' and not row.searchable
