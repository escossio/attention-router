"""Actual Node Transport -> signed envelopes -> ingress -> media -> STT -> archive."""
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from attention_router.adapters.internal_ingress import InternalIngressAdapter
from attention_router.application import services
from attention_router.application.context_compiler import compile_graph_context  # noqa: F401
from attention_router.application.semantic_governance import GovernedSemanticLab  # noqa: F401
from attention_router.application.conversation_live_capture import reconcile_live_capture_batch
from attention_router.application.conversation_preview import preview_selected_conversation
from attention_router.application.voice_media import MediaReadyNotification, record_media_notification
from attention_router.application.voice_transcription import (
    process_voice_transcriptions, retry_failed_transcription, VoiceTranscriptionError,
)
from attention_router.config import settings
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure import human_identity_models  # noqa: F401
from attention_router.infrastructure import artifact_sensitive_review_models  # noqa: F401
from attention_router.infrastructure.models import (
    ConversationMessageRow, VoiceTranscriptionRow, OutboxMessageRow, MemoryClaimRow,
)
from attention_router.web.internal_security import verify_internal_signature
from tests.test_owner_reply_grace import _install_canary


@pytest.mark.parametrize('scenario', ['basic', 'natural', 'canary'])
def test_bilateral_transport_to_context(session, monkeypatch, tmp_path, scenario):
    _install_canary(session)
    session.commit()
    monkeypatch.setattr(settings, 'conversation_capture_enabled', True)
    monkeypatch.setattr(settings, 'conversation_capture_start_at', datetime(2026,11,9,tzinfo=UTC) if scenario=='natural' else datetime.now(UTC)-timedelta(minutes=5))
    monkeypatch.setattr(settings, 'conversation_capture_tenant_id', DEFAULT_TENANT_ID)
    monkeypatch.setattr(settings, 'internal_ingress_hmac_secret', 'synthetic-bilateral-hmac-32-bytes')
    monkeypatch.setattr(settings, 'whatsapp_media_root', str(tmp_path/'media'))
    result = subprocess.run(['node', str(Path(__file__).parent/'fixtures/bilateral/transport.cjs'),
                             str(tmp_path), DEFAULT_TENANT_ID, scenario], check=True, capture_output=True, text=True)
    fixture = json.loads(result.stdout)
    assert fixture['downloads'] == (5 if scenario == 'natural' else 3)
    canary = None
    if scenario == 'canary':
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from attention_router.api.v1.semantic_canary_ingress import build_semantic_canary_ingress_router
        from attention_router.infrastructure.models import ActorBindingRow
        from tests.test_semantic_canary_scope import original_canary
        owner = session.scalar(select(ActorBindingRow).where(ActorBindingRow.tenant_id == DEFAULT_TENANT_ID,
            ActorBindingRow.actor_category == 'owner', ActorBindingRow.is_active.is_(True)))
        first_payload = json.loads(fixture['packets'][0]['body'])
        canary, canary_now, _ = original_canary(session, tmp_path, owner=owner.actor_key,
            conversation=first_payload['metadata']['conversation_key'])
        app = FastAPI()
        app.include_router(build_semantic_canary_ingress_router(get_session=lambda: session,
            service=canary, clock=lambda: canary_now))
        ingress = TestClient(app)
    for kind in ('event', 'media'):
        for packet in fixture['packets']:
            if packet['kind'] != kind:
                continue
            headers = packet['headers']
            assert verify_internal_signature(packet['body'].encode(), headers['X-Attention-Timestamp'],
                                             headers['X-Attention-Signature'])[0]
            payload = json.loads(packet['body'])
            if canary:
                suffix = '/events' if kind == 'event' else '/media'
                response = ingress.post('/api/v1/ingress/internal/semantic-canary'+suffix,
                    content=packet['body'], headers=headers)
                assert response.status_code == 202, response.text
                assert response.json()['status'] in {'accepted', 'duplicate'}, response.text
            elif kind == 'event':
                services.receive_normalized_inbound_event(session, InternalIngressAdapter().normalize(payload))
            else:
                if payload['media_kind'] in {'audio', 'ptt'}:
                    record_media_notification(session, MediaReadyNotification.model_validate(payload))
            session.commit()
    artifact_demo = _integrated_artifacts(session, monkeypatch, tmp_path, fixture)
    calls = 0
    def transcribe(path, mime):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise VoiceTranscriptionError("STT_TIMEOUT")
        return {'transcript': path.read_bytes().decode().removeprefix('OggS '),
                'provider': 'synthetic', 'model': 'synthetic', 'request_id': 'synthetic-stt'}
    fake = SimpleNamespace(transcribe=transcribe)
    if canary:
        assert process_voice_transcriptions(session, 'ordinary-worker', client=fake) == 0
    guarded = {'canary_guard':canary.processing_guard} if canary else {}
    assert process_voice_transcriptions(session, 'synthetic-worker', client=fake, **guarded) == 2
    failed = session.scalar(select(VoiceTranscriptionRow).where(VoiceTranscriptionRow.error_code == 'STT_TIMEOUT'))
    assert retry_failed_transcription(session,tenant_id=DEFAULT_TENANT_ID,transcription_id=failed.id)=='PENDING'
    assert retry_failed_transcription(session,tenant_id=DEFAULT_TENANT_ID,transcription_id=failed.id)=='PENDING'
    assert process_voice_transcriptions(session,'synthetic-retry',client=fake, **guarded)==1
    assert calls==3
    reconcile_live_capture_batch(session)
    session.commit()
    view = (canary.conversation(session, session_token='synthetic-session', now=canary_now) if canary
        else preview_selected_conversation(session, tenant_id=DEFAULT_TENANT_ID,
            source_account='synthetic', identifier=fixture['pn']))
    assert len(view['messages']) == (8 if scenario == 'natural' else 6)
    by_id = {m['provider_id']: m for m in view['messages']}
    assert by_id['tx-voice']['origin'] == 'UNKNOWN_FROM_ME'
    assert by_id['tx-voice']['author_basis'] == 'OWNER_ACCOUNT_ATTRIBUTED_BY_POLICY_V1'
    for index, uid in enumerate(('rx-voice', 'tx-voice')):
        assert by_id[uid]['text'] == fixture['texts'][index]
        assert by_id[uid]['evidence']['transcription_id']
        assert by_id[uid]['evidence']['media_artifact_id']
    assert by_id['rx-voice']['origin'] == 'EXTERNAL_INBOUND'
    for offset, uid in enumerate(('rx-voice','tx-voice','rx-text','tx-text','automatic','missing')):
        parsed=datetime.fromisoformat(by_id[uid]['sent_at']).replace(tzinfo=UTC)
        assert int(parsed.timestamp())==fixture['stamps'][offset]
    assert by_id['missing']['content_state']=='MEDIA_NOT_AVAILABLE'
    assert session.scalars(select(OutboxMessageRow)).all()==[]
    assert session.scalars(select(MemoryClaimRow)).all()==[]
    assert by_id['automatic']['author'] == 'AUTOMATED'
    assert by_id['missing']['text'] is None and view['context_incomplete']
    if scenario == 'basic':
        assert {e['topic'] for e in view['episodes']} >= {'professional', 'relationship'}
    assert view['relations'][0]['basis'] == 'PROVIDER_QUOTE'
    assert len(session.scalars(select(ConversationMessageRow)).all()) == (10 if scenario == 'natural' else 6)
    assert len(session.scalars(select(VoiceTranscriptionRow)).all()) == 3
    assert view['durable_claims'] == []
    # Contact identity is derived only from the same proved PN and never changes author evidence.
    from unittest.mock import Mock
    from attention_router.application.google_contacts_preview import ContactsPreviewConnection, CONTACTS_SCOPE
    from attention_router.application.gmail_connection import GoogleTokenGrant
    contacts = ContactsPreviewConnection(DEFAULT_TENANT_ID, 'owner', GoogleTokenGrant('synthetic', None, (CONTACTS_SCOPE,)))
    people = Mock()
    people.search.return_value = {'results': [{'person': {'resourceName':'people/synthetic-peer',
        'names':[{'displayName':'Beatriz Sintética'}],
        'phoneNumbers':[{'canonicalForm':'+'+fixture['pn'].split('@')[0]}]}}]}
    from attention_router.application.google_contacts_preview import ContactsPreviewService
    from attention_router.api.v1.contacts_preview import build_contacts_preview_router
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    authority = Mock()
    authority.authenticated_bootstrap.return_value = SimpleNamespace(
        active_tenant_id=DEFAULT_TENANT_ID, human_identity_id='owner',
        memberships=[SimpleNamespace(tenant_id=DEFAULT_TENANT_ID,role='OWNER',status='ACTIVE')])
    oauth = Mock()
    oauth.exchange_authorization_code.return_value = contacts.grant
    service = ContactsPreviewService(client_sessions=authority,oauth=oauth,people=people,enabled=True)
    app=FastAPI()
    app.include_router(build_contacts_preview_router(get_session=lambda:session,service=service))
    client=TestClient(app,headers={'Authorization':'Bearer cst_'+'s'*43})
    assert client.post('/api/v1/integrations/contacts',json={
        'consent':True,'authorization_code':'synthetic-code'}).status_code==200
    response=client.post('/api/v1/integrations/contacts/conversation',json={
        'source_account':'synthetic','identifier':fixture['pn']})
    assert response.status_code==200
    view=response.json()
    assert view['participants'][1]['contact']['saved_as']=='Beatriz Sintética'
    assert view['participants'][1]['contact']['identity_verified'] is False
    if scenario == 'basic':
        assert len(view['associations'])>=4
    from attention_router.application.conversation_preview import preview_selected_events
    event_view = preview_selected_events(session, tenant_id=DEFAULT_TENANT_ID,
        source_account='synthetic', identifiers=fixture['peers'])
    assert len(event_view['evidence']) == (10 if scenario == 'natural' else 6)
    assert event_view['context_incomplete']
    assert event_view['durable_claims'] == []
    if scenario == 'natural':
        from attention_router.application.conversation_preview import answer_preview_question
        event = event_view['events'][0]
        assert event['current']['state'] == 'CONFIRMED_DECLARATION'
        assert event['current']['event_time']['date'] == '2026-11-15'
        assert event['current']['author'] == fixture['lid']
        assert event['history'][0]['superseded_by'] == by_id['tx-voice']['message_id']
        answer = answer_preview_question(event_view, tenant_id=DEFAULT_TENANT_ID,
            source_account='synthetic', question='Quem confirmou a mudança?', event_id=event['event_id'])
        assert answer['answers'][0]['declarations'][0]['message_id'] == by_id['rx-text']['message_id']
        assert len(event_view['hypotheses']) == 3
        joao = next(m for m in event_view['evidence'].values() if m['provider_id'] == 'joao')
        question = dict(tenant_id=DEFAULT_TENANT_ID, source_account='synthetic',
            question='João estava sabendo da mudança?', event_id=event['event_id'], person=joao['author'])
        before_review = answer_preview_question(event_view, **question)
        assert before_review['status'] == 'NO_EVIDENCE'
        review = dict(tenant_id=DEFAULT_TENANT_ID, source_account='synthetic', decision_ref='synthetic-link',
            action='LINK', message_id=joao['message_id'], target_message_id=by_id['rx-text']['message_id'])
        reviewed = preview_selected_events(session, tenant_id=DEFAULT_TENANT_ID,
            source_account='synthetic', identifiers=fixture['peers'], reviews=[review])
        after_review = answer_preview_question(reviewed, **question)
        assert after_review['status'] == 'SUPPORTED'
        governed = _synthetic_candidate_mirror(session,event_view)
        governed['lifecycle'] = _governed_integrated_lifecycle(session, governed, tmp_path)
        (tmp_path/'integrated-events.json').write_text(json.dumps(
            {'conversation':view, 'events':event_view, 'answer':answer,
             'joao_before_review':before_review, 'reviewed':reviewed, 'joao_after_review':after_review,
             'governed_interpretation':governed, 'artifacts':artifact_demo}, ensure_ascii=False, indent=2, default=str))
    if scenario == 'canary':
        _canary_original_lifecycle(session, tmp_path, view, existing=(canary, canary_now))
    assert not session.new and not session.dirty

    (tmp_path/'synthetic-preview.json').write_text(json.dumps(view,ensure_ascii=False,indent=2))


def _canary_original_lifecycle(session, tmp_path, view, existing=None):
    """No mirrored archive: both real-pipeline fixture transcripts retain their IDs."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from attention_router.api.v1.semantic_review_preview import build_semantic_review_preview_router
    from attention_router.application.memory import search_archive
    from attention_router.infrastructure.models import ActorBindingRow
    from tests.test_semantic_canary_scope import original_canary
    session.commit()
    owner = session.scalar(select(ActorBindingRow).where(ActorBindingRow.tenant_id == DEFAULT_TENANT_ID,
        ActorBindingRow.actor_category == 'owner', ActorBindingRow.is_active.is_(True)))
    if existing:
        service, now = existing
        grant = service.grant_type.model_validate(service.journal.read()['grant'])
    else:
        service, now, grant = original_canary(session, tmp_path, owner=owner.actor_key,
                                             conversation=view['conversation_key'])
    app = FastAPI()
    app.include_router(build_semantic_review_preview_router(get_session=lambda: session,
        service=service, clock=lambda: now, include_processing=True))
    client = TestClient(app, headers={'Authorization':'Bearer synthetic-session'})
    base = '/api/v1/client/semantic-preview'
    receipts = {}
    for message in view['messages']:
        response = client.post(base+'/process', json={'message_id':message['message_id']})
        assert response.status_code == 200, response.text
        receipts[message['provider_id']] = response.json()
    assert receipts['automatic']['status'] == 'AUTOMATED_EVIDENCE_ONLY'
    assert receipts['missing']['status'] == 'SOURCE_INCOMPLETE'
    inventory = client.get(base+'/candidates').json()
    by_provider = {m['provider_id']:m for m in view['messages']}
    for uid, origin in [('rx-voice','EXTERNAL_INBOUND'), ('tx-voice','UNKNOWN_FROM_ME')]:
        cid = receipts[uid]['candidate_id']
        candidate = next(c for c in inventory if c['candidate_id'] == cid)
        assert candidate['interpretation']['source_message_ids'] == [by_provider[uid]['message_id']]
        evidence = candidate['evidence'][0]
        assert evidence['lineage'] == 'ORGANIC' and evidence['origin'] == origin
        assert evidence['transcription_id'] == by_provider[uid]['evidence']['transcription_id']
        assert evidence['modality'] == 'VOICE'
        if uid == 'tx-voice':
            assert evidence['author_basis'] == 'OWNER_ACCOUNT_ATTRIBUTED_BY_POLICY_V1'
        assert client.post(base+'/review', json=dict(candidate_id=cid, action='ADMIT',
            decision_ref='synthetic-review-'+uid)).status_code == 200
    view = client.get(base+'/conversation').json()
    before = client.post(base+'/context', json={'query':'viagem domingo'}).json()
    evidence_ids = {item['source_id'] for item in before['graph_context']['items']}
    assert {by_provider[uid]['message_id'] for uid in ('rx-voice','tx-voice')} <= evidence_ids
    assert len(session.scalars(select(ConversationMessageRow)).all()) == len(view['messages'])
    assert not search_archive(session, 'viagem domingo', tenant_id=grant.tenant_id)
    assert not session.scalars(select(OutboxMessageRow)).all()
    assert not session.scalars(select(MemoryClaimRow)).all()
    paused = client.post(base+'/operations', json=dict(request_id='stop', operation='PAUSE_CAPTURE', source_ids=[]))
    assert paused.status_code == 200
    assert client.post(base+'/process', json={'message_id':by_provider['rx-voice']['message_id']}).status_code == 409
    revoked = client.post(base+'/operations', json=dict(request_id='revoke', operation='REVOKE_CONSENT', source_ids=[]))
    assert revoked.status_code == 200
    assert client.post(base+'/context', json={'query':'domingo'}).status_code == 409
    assert not service.cache_usable(session, session_token='synthetic-session', cached=before, query='viagem domingo', now=now)
    (tmp_path/'minimal-canary-original-evidence.json').write_text(json.dumps(dict(
        synthetic_content=True, original_archive_ids=True, conversation=view, processing=receipts,
        candidates=inventory, before_revocation=before, after_revocation='ACCESS_BLOCKED',
        new_outbox_rows=0, canonical_promotion=False, production_activation=False), indent=2, default=str))



def _synthetic_candidate_mirror(session, event_view):
    """Test-only rematerialization: live capture deliberately accepts ORGANIC only.

    Original ingress/archive rows are untouched. The explicit synthetic copy
    preserves provider IDs, authors and STT references for the offline V2E gate.
    """
    from attention_router.application.memory import archive_incremental_message
    from attention_router.application.semantic_interpretation import (
        archived_source, interpret_offline, propose_interpretation, compile_interpretation_context, SyntheticRetention,
    )
    from attention_router.infrastructure.semantic_episode_models import SemanticEpisodeMembershipRow
    from attention_router.domain.models import new_id
    from tests.test_candidate_insights import _episode
    resource, episode, _, _ = _episode(session, 'synthetic-transport-language')
    stamp = datetime.now(UTC)
    policy = SyntheticRetention(tenant_id=DEFAULT_TENANT_ID,source_account='synthetic-interpretation',
        purpose='SYNTHETIC_EVALUATION',consent=True,expires_at=stamp+timedelta(hours=1))
    proposals=[]
    for message in event_view['evidence'].values():
        if not message['text'] or message['author_basis']=='AUTOMATED':
            continue
        event = next((e for e in event_view['events'] if any(h['message_id']==message['message_id'] for h in e['history'])),None)
        if not event:
            continue
        row,_=archive_incremental_message(session,source='wwebjs',source_account=policy.source_account,
            thread_key=message['conversation_key'],thread_type='DIRECT',source_message_id=message['provider_id'],
            actor_key=message['author'],display_name='Synthetic participant',text=message['text'],
            sent_at=datetime.fromisoformat(message['sent_at']),from_me=message['direction']=='OUTBOUND',
            tenant_id=DEFAULT_TENANT_ID,lineage_classification='SYNTHETIC',scenario_id='transport-language',
            scenario_run_id='synthetic-run',metadata={'speaker_basis':message['author_basis'],
                'event_origin':message['origin'],'transcription_id':message['evidence']['transcription_id'],
                'reviewed_event_ref':event['event_id'],'synthetic_original_message_id':message['message_id']})
        row.message_type=message['kind']
        session.flush()
        source=archived_source(session,tenant_id=DEFAULT_TENANT_ID,source_account=policy.source_account,message_id=row.id)
        value=interpret_offline(source)
        candidate,_=propose_interpretation(session,value,policy=policy,now=stamp)
        session.add(SemanticEpisodeMembershipRow(id=new_id(),tenant_id=DEFAULT_TENANT_ID,episode_id=episode.id,
            member_type='CONVERSATION_MESSAGE',member_ref=row.id,association_reason='EXPLICIT_SEMANTIC_KEY',
            association_source='SYNTHETIC_OWNER_REVIEW',confidence=1.0,ambiguous=False,
            observed_at=row.sent_at,metadata_json={'lineage_classification':'SYNTHETIC'},created_at=stamp))
        proposals.append({'candidate_id':candidate.id,'state':candidate.state,'interpretation':value.model_dump(mode='json'),
                          'original_message_id':message['message_id']})
    session.flush()
    packet=compile_interpretation_context(session,tenant_id=DEFAULT_TENANT_ID,source_account=policy.source_account,
                                         query=resource.canonical_name,policy=policy,now=stamp)
    assert packet['candidate_insights']
    assert any(p['interpretation']['action']=='CONFIRMS' for p in proposals)
    assert any(p['interpretation']['action']=='CHANGES' for p in proposals)
    assert session.scalars(select(OutboxMessageRow)).all()==[]
    assert session.scalars(select(MemoryClaimRow)).all()==[]
    return {'proposals':proposals,'context':packet,'lineage':'SYNTHETIC','canonical_truth':False}


def _governed_integrated_lifecycle(session, mirrored, tmp_path):
    from unittest.mock import Mock
    from attention_router.application.semantic_governance import PolicyJournal, LabGrant, Layer, LabOperation
    from attention_router.infrastructure.models import ActorBindingRow
    from tests.test_candidate_insights import _governed_episode
    stamp = datetime.now(UTC)
    session.commit()
    owner = session.scalar(select(ActorBindingRow).where(ActorBindingRow.tenant_id==DEFAULT_TENANT_ID,
        ActorBindingRow.actor_category=='owner',ActorBindingRow.is_active.is_(True)))
    authority = Mock()
    authority.authenticated_bootstrap.return_value = SimpleNamespace(active_tenant_id=DEFAULT_TENANT_ID,
        human_identity_id='synthetic-owner',memberships=[SimpleNamespace(
            tenant_id=DEFAULT_TENANT_ID,role='OWNER',status='ACTIVE')])
    lab = GovernedSemanticLab(journal=PolicyJournal(tmp_path/'integrated-policy.json'),client_sessions=authority)
    grant = LabGrant(tenant_id=DEFAULT_TENANT_ID,source_account='synthetic-interpretation',
        owner_id='synthetic-owner',owner_actor_key=owner.actor_key,purpose='SYNTHETIC_EVALUATION',
        lineage='SYNTHETIC',consent=True,policy_version='SYNTHETIC_FIXTURE_ONLY',starts_at=stamp,
        deadlines={layer:stamp+timedelta(hours=1) for layer in Layer.__args__})
    lab.initialize(session,session_token='synthetic',grant=grant,now=stamp)
    groups = {}
    for proposal in mirrored['proposals']:
        value = proposal['interpretation']
        groups.setdefault(value['event_id'],[]).extend(value['source_message_ids'])
    for index,ids in enumerate(groups.values()):
        _governed_episode(session,[session.get(ConversationMessageRow,mid) for mid in ids],key='integrated-governed-'+str(index))
    before = lab.context(session,session_token='synthetic',query='viagem',now=stamp)
    assert before['candidate_insights']
    inventory = lab.candidates(session,session_token='synthetic',now=stamp)
    sources = [s for c in inventory for s in c['evidence']]
    assert any(s['modality']=='VOICE' and s['origin']=='UNKNOWN_FROM_ME' for s in sources)
    assert any(s['modality']=='VOICE' and s['origin']=='EXTERNAL_INBOUND' for s in sources)
    assert all(s['transcription_id'] for s in sources if s['modality']=='VOICE')
    cid = inventory[0]['candidate_id']
    lab.review(session,session_token='synthetic',candidate_id=cid,action='ADMIT',decision_ref='integrated-admit',now=stamp)
    lab.review(session,session_token='synthetic',candidate_id=cid,action='REJECT',decision_ref='integrated-reject',now=stamp)
    assert not lab.cache_usable(session,session_token='synthetic',cached=before,query='viagem',now=stamp)
    ids = tuple(sorted({s['message_id'] for s in sources}))
    lab.operate(session,session_token='synthetic',request=LabOperation(
        request_id='integrated-delete',operation='DELETE_DERIVATIVES',source_ids=ids),now=stamp)
    after = lab.context(session,session_token='synthetic',query='viagem',now=stamp)
    assert after['candidate_insights']==[] and after['graph_context']['items']==[]
    verified = lab.operate(session,session_token='synthetic',request=LabOperation(
        request_id='integrated-verify',operation='VERIFY_DELETION',source_ids=ids),now=stamp)
    return dict(before=before,review_inventory=inventory,after=after,deletion=verified,
        audit=lab.history(session,session_token='synthetic',now=stamp),grants_authority=False)

def _integrated_artifacts(session, monkeypatch, tmp_path, fixture):
    """Same committed packets feed the existing canonical Artifact path as STT."""
    from attention_router.application.whatsapp_artifacts import stage_whatsapp_artifact_notification
    from attention_router.application.artifact_understanding import (
        ArtifactUnderstandingResult, ArtifactUnderstandingProviderResult,
        process_artifact_understandings, register_artifact_understanding_notification,
    )
    from attention_router.infrastructure.artifact_models import ArtifactReceiptRow, ArtifactRow
    from attention_router.infrastructure.artifact_sensitive_review_models import ArtifactSensitiveReviewRow
    from tests.test_artifact_understanding import _enable
    _enable(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, 'artifact_sensitive_review_enabled', True)
    monkeypatch.setattr(settings, 'artifact_sensitive_review_start_at', datetime.now(UTC)-timedelta(days=1))
    expected = {}
    for packet in fixture['packets']:
        if packet['kind'] != 'media':
            continue
        notice = MediaReadyNotification.model_validate_json(packet['body'])
        staged = stage_whatsapp_artifact_notification(session, notice)
        if staged is None:
            continue
        repeated = stage_whatsapp_artifact_notification(session, notice)
        assert repeated.registration.receipt.id == staged.registration.receipt.id
        receipt = staged.registration.receipt
        if notice.external_event_id.startswith('tx-'):
            assert receipt.sender_actor_id is None
        expected[notice.external_event_id] = staged.registration.artifact.id
        register_artifact_understanding_notification(session, notice, artifact_id=receipt.artifact_id)
    session.commit()

    class SyntheticVision:
        def analyze(self, data, *, mime_type, filename):
            assert data.startswith(b'synthetic ')
            return ArtifactUnderstandingProviderResult(
                analysis=ArtifactUnderstandingResult(
                    summary='Cartão de visita inteiramente sintético' if mime_type=='image/png' else 'Documento sintético',
                    extracted_text='', visual_description='', key_facts=[], language='pt-BR', text_truncated=False),
                provider='synthetic', model='offline-fixture', request_reference='synthetic-artifact-request')
    process_artifact_understandings(session, 'synthetic-combined-artifact', provider=SyntheticVision())
    session.commit()
    reconcile_live_capture_batch(session)
    session.commit()
    for uid, artifact_id in expected.items():
        message = session.scalar(select(ConversationMessageRow).where(ConversationMessageRow.source_message_id==uid))
        assert message.metadata_json['artifact_id'] == artifact_id
        assert message.metadata_json['attachment_state'] == 'READY'
        if uid in {'tx-image', 'tx-document'}:
            assert message.metadata_json['artifact_understanding_status'] == 'READY'
            assert message.metadata_json['event_origin'] == 'UNKNOWN_FROM_ME'
            assert message.metadata_json['speaker_basis'] == 'UNRESOLVED'
            assert message.text is None and message.external_sender_key is None
    assert len(session.scalars(select(ArtifactRow)).all()) == len(set(expected.values()))
    assert len(session.scalars(select(ArtifactReceiptRow)).all()) == len(expected)
    reviews = session.scalars(select(ArtifactSensitiveReviewRow)).all()
    assert len(reviews) == (1 if 'tx-image' in expected else 0)
    for review in reviews:
        assert review.state == 'UNASSIGNED' and review.category == 'CONTACT_CARD'
        assert review.artifact_id == expected['tx-image']
    return {'artifacts_by_provider_id':expected, 'review_states':[r.state for r in reviews],
            'human_attribution_for_visual_media':False, 'canonical_admission':False}
