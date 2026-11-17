"""Synthetic delta over the preserved bilateral/governance suites."""
from datetime import UTC, datetime, timedelta
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from attention_router.application.semantic_governance import LabOperation
from attention_router.application.semantic_policy_anchor import AnchoredPolicyJournal, SyntheticFileAuthority
from attention_router.application.semantic_retention import RetentionRule, RetentionSchedule
from tests.test_candidate_insights import _governed_lab, _language_archive, _governed_episode


def anchored_lab(session, tmp_path):
    lab, stamp, grant = _governed_lab(session, tmp_path)
    authority_path = tmp_path/'independent-authority'
    authority_path.mkdir()
    authority = SyntheticFileAuthority(authority_path/'anchor.json')
    authority.initialize_synthetic()
    state = lab.journal.read()
    state['revision'] = 0
    lab.journal = AnchoredPolicyJournal(lab.journal.path, authority)
    with lab.journal.locked():
        lab.journal.write(state)
    return lab, stamp, grant


def test_anchor_blocks_restore_missing_and_corrupt_authority(session, tmp_path):
    lab, stamp, _ = anchored_lab(session, tmp_path)
    before = lab.journal.path.read_bytes()
    lab.operate(session, session_token='synthetic-session', request=LabOperation(
        request_id='revoke', operation='REVOKE_CONSENT', source_ids=()), now=stamp)
    lab.journal.path.write_bytes(before)
    with pytest.raises(ValueError, match='ROLLBACK'):
        lab.context(session, session_token='synthetic-session', query='domingo', now=stamp)
    authority = lab.journal.authority
    authority.path.write_text('{broken')
    with pytest.raises(ValueError, match='AUTHORITY_UNAVAILABLE'):
        lab.journal.read()
    authority.path.unlink()  # only disposable synthetic fixture
    with pytest.raises(ValueError, match='AUTHORITY_UNAVAILABLE'):
        lab.journal.read()


def test_anchor_recovers_only_exact_prepared_payload(session, tmp_path, monkeypatch):
    import attention_router.application.semantic_policy_anchor as module
    lab, stamp, _ = anchored_lab(session, tmp_path)
    original = module.os.replace
    def interrupted(source, destination):
        if source == lab.journal.staged.path:
            raise OSError('synthetic crash after CAS')
        return original(source, destination)
    with monkeypatch.context() as patch:
        patch.setattr(module.os, 'replace', interrupted)
        with pytest.raises(OSError):
            lab.operate(session, session_token='synthetic-session', request=LabOperation(
                request_id='revoke', operation='REVOKE_CONSENT', source_ids=()), now=stamp)
    assert lab.journal.read()['revoked']
    with pytest.raises(ValueError, match='CONSENT_OR_RETENTION'):
        lab.context(session, session_token='synthetic-session', query='domingo', now=stamp)
    # Retrying pending operation reconciles the existing DB audit and receipt.
    assert lab.operate(session, session_token='synthetic-session', request=LabOperation(
        request_id='revoke', operation='REVOKE_CONSENT', source_ids=()), now=stamp)['status'] == 'COMPLETE'


def test_anchor_cas_and_separate_domain(session, tmp_path):
    lab, _, _ = anchored_lab(session, tmp_path)
    authority = lab.journal.authority
    current = authority.read()
    with pytest.raises(ValueError, match='CAS_CONFLICT'):
        authority.advance(dict(revision=0, digest=None), dict(revision=1, digest='x'*64))
    assert authority.read() == current
    with pytest.raises(ValueError, match='SEPARATE_RESTORE_DOMAIN'):
        AnchoredPolicyJournal(authority.path.parent/'journal.json', authority)


def schedule():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return RetentionSchedule(tenant_id='synthetic', source_account='account', owner_id='owner',
        version='fixture-only', lineage='SYNTHETIC', effective_at=now-timedelta(days=1),
        expires_at=now+timedelta(days=2), rules=(RetentionRule(data_class='MEDIA', purpose='STT',
            ttl_seconds=3600, decision_ref='fixture-not-owner-policy',
            expiry_action='BLOCK_AND_SCHEDULE_VERIFIED_DELETION'),)), now


def test_retention_class_purpose_and_derived_deadlines():
    policy, now = schedule()
    kwargs = dict(tenant_id='synthetic', source_account='account', owner_id='owner', data_class='MEDIA',
                  purpose='STT', created_at=now, now=now, consent=True)
    assert policy.deadline(**kwargs) == now+timedelta(hours=1)
    assert policy.deadline(**kwargs, source_deadlines=(now+timedelta(minutes=5),)) == now+timedelta(minutes=5)
    for change in ({'tenant_id':'other'}, {'source_account':'other'}, {'owner_id':'other'},
                   {'consent':False}, {'purpose':'MEMORY'}, {'data_class':'BACKUP'},
                   {'now':now+timedelta(hours=1)}, {'now':now+timedelta(days=3)},
                   {'created_at':now+timedelta(minutes=1)}, {'now':now.replace(tzinfo=None)}):
        with pytest.raises(ValueError):
            policy.deadline(**(kwargs | change))


@pytest.mark.parametrize('ttl', [0, -1, True, '3600', 3155760001])
def test_retention_rejects_invalid_ttl(ttl):
    with pytest.raises(ValueError):
        RetentionRule(data_class='MEDIA', purpose='STT', ttl_seconds=ttl,
                      decision_ref='fixture', expiry_action='BLOCK_AND_SCHEDULE_VERIFIED_DELETION')


@pytest.mark.parametrize('consumer', ['ARCHIVE', 'LEXICAL', 'PERSONAL_CONTEXT', 'GRAPH'])
def test_consumer_revalidates_and_blocks_deleted_paths(session, tmp_path, consumer):
    lab, stamp, grant = anchored_lab(session, tmp_path)
    row = _language_archive(session, 'Confirmo domingo.', 'consumer-source')
    _governed_episode(session, [row])
    before = lab.consumer_read(session, session_token='synthetic-session', consumer=consumer, query='domingo', now=stamp)
    assert row.id in json.dumps(before, default=str)
    if consumer == 'ARCHIVE':
        assert before['result'][0]['message_id'] == row.id
    if consumer == 'GRAPH':
        assert any(n['source_id'] == row.id for n in before['result']['nodes'])
    lab.operate(session, session_token='synthetic-session', request=LabOperation(
        request_id='hide', operation='HIDE_CONTEXT', source_ids=(row.id,)), now=stamp)
    after = lab.consumer_read(session, session_token='synthetic-session', consumer=consumer, query='domingo', now=stamp)
    assert 'Confirmo domingo' not in json.dumps(after, default=str)
    assert not after['evidence_context']['graph_context']['items']
    lab.journal.path.unlink()
    with pytest.raises(ValueError, match='ROLLBACK'):
        lab.consumer_read(session, session_token='synthetic-session', consumer=consumer, query='domingo', now=stamp)


def test_preview_api_review_and_export_synthetic(session, tmp_path):
    from attention_router.api.v1.semantic_review_preview import build_semantic_review_preview_router
    from attention_router.application.semantic_interpretation import archived_source, interpret_offline
    lab, stamp, grant = anchored_lab(session, tmp_path)
    row = _language_archive(session, 'Confirmo domingo.', 'api-source')
    source = archived_source(session, tenant_id=grant.tenant_id, source_account=grant.source_account, message_id=row.id)
    cid, _ = lab.propose(session, session_token='synthetic-session', value=interpret_offline(source), now=stamp)
    app = FastAPI()
    app.include_router(build_semantic_review_preview_router(get_session=lambda: session, service=lab, clock=lambda: stamp))
    client = TestClient(app)
    path = '/api/v1/client/semantic-preview'
    assert client.get(path+'/candidates').status_code == 401
    client.headers['Authorization'] = 'Bearer synthetic-session'
    candidates = client.get(path+'/candidates')
    assert candidates.headers['cache-control'] == 'no-store'
    assert candidates.json()[0]['candidate_id'] == cid
    assert candidates.json()[0]['evidence'][0]['message_id'] == row.id
    before = client.post(path+'/context', json={'query':'domingo'}).json()
    for action in ('ADMIT', 'REJECT'):
        response = client.post(path+'/review', json=dict(candidate_id=cid, action=action, decision_ref=action))
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'COMPLETE'
    assert client.post(path+'/review', json=dict(candidate_id=cid, action='PROMOTE', decision_ref='bad')).status_code == 422
    assert client.post(path+'/context', json={'query':'domingo'}).json()['graph_context']['items'] == []
    assert client.post(path+'/operations', json=dict(request_id='export', operation='EXPORT_CONTEXT', source_ids=[row.id])).status_code == 409
    history = client.get(path+'/history').json()
    assert history['reviews']['REJECT']['status'] == 'COMPLETE'
    demo = dict(before=before, review_history=history, after=client.post(path+'/context',json={'query':'domingo'}).json(),
                canonical_promotion=False, live_registration=False)
    (tmp_path/'precert-review-demo.json').write_text(json.dumps(demo, indent=2, default=str))
    (tmp_path/'semantic-review-preview.openapi.json').write_text(json.dumps(app.openapi(),indent=2))


def test_legacy_claims_cannot_leak_through_governed_lexical_or_graph(session, tmp_path):
    from tests.test_personal_context import _memory_actor, _claim
    from attention_router.application.context_retrieval import retrieve_personal_context
    from attention_router.application.personal_context import build_personal_context
    lab, stamp, grant = anchored_lab(session, tmp_path)
    actor = _memory_actor(session, grant.tenant_id, grant.owner_actor_key)
    claim = _claim(session, actor, 'planning.trip', 'domingo unpublished synthetic claim')
    session.commit()
    assert build_personal_context(session, grant.tenant_id, now=stamp).claims
    assert retrieve_personal_context(session, grant.tenant_id, 'domingo').items
    for consumer in ('LEXICAL', 'PERSONAL_CONTEXT', 'GRAPH', 'MEMORY_CLAIMS', 'ENTITY_MEMORY', 'MEMORY_EVIDENCE'):
        result = lab.consumer_read(session, session_token='synthetic-session', consumer=consumer,
                                   query='domingo', now=stamp,
                                   subject_id=claim.id if consumer == 'MEMORY_EVIDENCE' else actor.id)
        assert claim.id not in json.dumps(result, default=str)
        assert 'unpublished synthetic claim' not in json.dumps(result, default=str)
    with pytest.raises(ValueError, match='CONSUMER_NOT_REGISTERED'):
        lab.consumer_read(session, session_token='synthetic-session', consumer='COGNITIVE_RUNTIME', query='domingo', now=stamp)


def test_preview_api_real_client_session_revocation(session, tmp_path):
    from tests.client.test_client_session_service import seed, keypair, sign, settings, NOW, HUMAN_ID, TENANT_ID
    from tests.test_candidate_insights import _owner
    from attention_router.application.client_session import ClientSessionService
    from attention_router.application.semantic_governance import GovernedSemanticLab, LabGrant, Layer
    from attention_router.api.v1.semantic_review_preview import build_semantic_review_preview_router
    from attention_router.infrastructure.client_session_models import ClientSessionRow
    private, spki, public = keypair()
    seed(session, spki)
    owner = _owner(session, tenant_id=TENANT_ID)
    sessions = ClientSessionService(settings=settings())
    challenge = sessions.start_session(session, public_key_spki_b64url=public, requested_tenant_id=TENANT_ID, now=NOW)
    issued = sessions.complete_session(session, session_challenge_id=challenge.session_challenge_id,
        device_signature_b64url=sign(private, challenge.challenge_b64url), now=NOW+timedelta(seconds=1))
    session.commit()
    stamp = NOW+timedelta(seconds=2)
    authority_dir = tmp_path/'authority'
    authority_dir.mkdir()
    authority = SyntheticFileAuthority(authority_dir/'anchor.json')
    authority.initialize_synthetic()
    lab = GovernedSemanticLab(journal=AnchoredPolicyJournal(tmp_path/'policy.json', authority), client_sessions=sessions)
    grant = LabGrant(tenant_id=TENANT_ID, source_account='synthetic', owner_id=HUMAN_ID,
        owner_actor_key=owner.actor_key, purpose='SYNTHETIC_EVALUATION', lineage='SYNTHETIC', consent=True,
        policy_version='fixture', starts_at=NOW, deadlines={k:NOW+timedelta(hours=1) for k in Layer.__args__})
    lab.initialize(session, session_token=issued.session_token, grant=grant, now=stamp)
    app = FastAPI()
    app.include_router(build_semantic_review_preview_router(get_session=lambda: session, service=lab, clock=lambda: stamp))
    client = TestClient(app)
    path = '/api/v1/client/semantic-preview/candidates'
    client.headers['Authorization'] = 'Bearer invalid'
    assert client.get(path).status_code == 401
    client.headers['Authorization'] = 'Bearer '+issued.session_token
    assert client.get(path).status_code == 200
    session.get(ClientSessionRow, issued.session_id).state = 'REVOKED'
    session.get(ClientSessionRow, issued.session_id).revoked_at = stamp
    session.commit()
    assert client.get(path).status_code == 401
    assert issued.session_token not in lab.journal.path.read_text()


def test_retention_policy_does_not_enable_organic_or_duplicate_rules():
    policy, _ = schedule()
    for change in ({'lineage':'ORGANIC'}, {'rules':[policy.rules[0],policy.rules[0]]},
                   {'effective_at':policy.effective_at.replace(tzinfo=None)},
                   {'expires_at':policy.effective_at}):
        with pytest.raises(ValueError):
            RetentionSchedule.model_validate(policy.model_dump() | change)


def test_cache_checks_events_authority_and_decisions_not_only_graph(session, tmp_path):
    from copy import deepcopy
    from attention_router.infrastructure.hashing import stable_hash
    lab, stamp, _ = anchored_lab(session, tmp_path)
    cached = lab.context(session, session_token='synthetic-session', query='domingo', now=stamp)
    assert lab.cache_usable(session, session_token='synthetic-session', cached=cached, query='domingo', now=stamp)
    for field, value in [('event_context', {'events':[{'fabricated':True}]}),
                         ('grants_authority', True), ('decisions', [{'action':'ADMIT'}])]:
        forged = deepcopy(cached)
        forged[field] = value
        assert not lab.cache_usable(session, session_token='synthetic-session', cached=forged, query='domingo', now=stamp)
        forged['context_fingerprint'] = stable_hash({k:v for k,v in forged.items() if k != 'context_fingerprint'})
        assert not lab.cache_usable(session, session_token='synthetic-session', cached=forged, query='domingo', now=stamp)
    for malformed in ({}, None, []):
        assert not lab.cache_usable(session, session_token='synthetic-session', cached=malformed, query='domingo', now=stamp)
