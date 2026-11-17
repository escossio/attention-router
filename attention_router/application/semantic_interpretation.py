"""Offline language-to-V2E adapter. Not wired into runtime or personal ingestion.

Reuses archive evidence, preview extraction, V2E lifecycle and V2F retrieval.
No provider, canonical fact, graph edge, episode or execution is created here.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from attention_router.application.context_retrieval import _normalize
from attention_router.application.conversation_preview import preview_event_time
from attention_router.application.memory import preview_event_candidate
from attention_router.infrastructure.hashing import stable_hash
from attention_router.application.semantic_sensitivity import translate_sensitivity

Action = Literal['PROPOSES', 'COUNTERPROPOSES', 'CHANGES', 'CONFIRMS', 'CANCELS',
                 'AGREES', 'DISAGREES', 'DENIES', 'QUESTIONS', 'ANSWERS',
                 'REPORTS', 'ACKNOWLEDGES', 'MENTIONS', 'UNKNOWN']


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class SourceEvidence(Contract):
    tenant_id: str
    source_account: str
    message_id: str
    author: str
    author_basis: str
    origin: str
    sent_at: datetime
    text: str = Field(max_length=12000)
    event_ref: str | None = None
    reply_to: str | None = None
    modality: Literal['TEXT', 'VOICE'] = 'TEXT'
    transcription_id: str | None = None
    sensitivity: Literal['NORMAL', 'PRIVATE', 'SECRET'] = 'PRIVATE'
    # A typed source is not a processing permit. The laboratory loader below
    # still rejects ORGANIC; a governed, explicitly scoped adapter must admit it.
    lineage: Literal['SYNTHETIC', 'ORGANIC'] = 'SYNTHETIC'
    stt_ambiguous: bool = False


class EvidenceSpan(Contract):
    message_id: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=12000)


class Interpretation(Contract):
    tenant_id: str
    source_account: str
    event_id: str | None
    actor_ref: str
    participants: tuple[str, ...] = ()
    action: Action
    topic: str | None = None
    message_time: datetime
    event_date: str | None = None
    event_end_date: str | None = None
    event_local_time: str | None = None
    timezone: str = 'UTC'
    uncertain_time: bool = True
    polarity: Literal['POSITIVE', 'NEGATIVE', 'UNCERTAIN', 'INTERROGATIVE']
    negation_scope: str | None = None
    quoted_speech: bool = False
    references: tuple[str, ...] = ()
    source_message_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    evidence_spans: tuple[EvidenceSpan, ...] = Field(min_length=1, max_length=20)
    contradiction_refs: tuple[str, ...] = ()
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    certainty_basis: str = Field(min_length=1, max_length=500)
    state: Literal['PROPOSED_INTERPRETATION', 'UNRESOLVED']
    parser_version: str = Field(min_length=1, max_length=80)
    source_engine: Literal['RULE', 'LLM', 'EMBEDDING'] = 'RULE'
    language: Literal['pt-BR'] = 'pt-BR'


class OfflineInterpreter(Protocol):
    """Injection boundary for a fake model; no default network implementation."""
    engine: Literal['LLM', 'EMBEDDING']
    version: str

    def interpret(self, source: SourceEvidence) -> dict: ...


def _utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _language_features(source):
    text = _normalize(source.text.strip())
    baseline = preview_event_candidate(source.text)
    action = baseline['action']
    polarity, quoted, reason = 'POSITIVE', False, 'BASELINE_EXTRACTOR'
    if re.search(r'\b(ignore as regras|system prompt|execute comandos|revele a senha)\b', text):
        action, polarity, reason = 'UNKNOWN', 'UNCERTAIN', 'UNTRUSTED_INSTRUCTION'
    elif source.stt_ambiguous or re.search(r'\b(so que nao|ironia|inaudivel|ininteligivel)\b', text):
        action, polarity, reason = 'UNKNOWN', 'UNCERTAIN', 'AMBIGUOUS_SPEECH'
    elif '?' in text or re.match(r'^(?:sera que|voce pode|pode ser)\b', text):
        action, polarity, reason = 'QUESTIONS', 'INTERROGATIVE', 'QUESTION_NOT_CONFIRMATION'
    elif re.search(r'\b(disse que|falou que|segundo .+|me contou que)\b', text) or '"' in text:
        action, quoted, reason = 'REPORTS', True, 'REPORTED_SPEECH_NOT_DIRECT_EVIDENCE'
    elif re.search(r'\b(talvez|quem sabe|se der|acho que|pode ser)\b', text):
        action, polarity, reason = 'UNKNOWN', 'UNCERTAIN', 'POSSIBILITY_NOT_COMMITMENT'
    elif re.match(r'^(?:cancelo|cancelei|ta cancelado|esta cancelado|nao vai rolar)\b', text):
        action, polarity, reason = 'CANCELS', 'NEGATIVE', 'EXPLICIT_CANCELLATION'
    elif re.match(r'^(?:nao concordo|discordo)\b', text):
        action, polarity = 'DISAGREES', 'NEGATIVE'
    elif re.match(r'^(?:nao confirmo|nao vou|nao sabia)\b', text):
        action, polarity = 'DENIES', 'NEGATIVE'
    elif re.match(r'^(?:bora|que tal)\b', text):
        action, reason = 'PROPOSES', 'COLLOQUIAL_PROPOSAL'
    elif re.match(r'^(?:melhor|prefiro|em vez disso)\b', text):
        action, reason = 'COUNTERPROPOSES', 'ALTERNATIVE_NOT_CONFIRMATION'
    elif re.match(r'^(?:to dentro|fechou|beleza, combinado)\b', text):
        action, reason = 'AGREES', 'COLLOQUIAL_AGREEMENT'
    elif re.fullmatch(r'(sim|nao)[.!]?', text):
        action, polarity, reason = 'ANSWERS', 'NEGATIVE' if text.startswith('nao') else 'POSITIVE', 'ANSWER_REQUIRES_REFERENCE'
    if action == 'CONTESTS':
        action, polarity = 'DISAGREES', 'NEGATIVE'
    if action == 'MENTIONS':
        action, reason = 'UNKNOWN', 'NO_SUPPORTED_INTERPRETATION'
    return action, polarity, quoted, reason, baseline


def interpret_offline(source: SourceEvidence, *, timezone='UTC', provider: OfflineInterpreter | None = None):
    if source.sensitivity == 'SECRET' or source.origin == 'ROUTER_AUTOMATED_OUTBOUND_OBSERVED':
        raise ValueError('INTERPRETATION_SOURCE_FORBIDDEN')
    if source.author_basis in {'AUTOMATED', 'UNRESOLVED', ''} or not source.text:
        raise ValueError('INTERPRETATION_SOURCE_UNRESOLVED')
    if provider is not None:
        if provider.engine not in {'LLM','EMBEDDING'}:
            raise ValueError('INTERPRETER_ENGINE_UNSUPPORTED')
        raw = provider.interpret(source)
        return Interpretation.model_validate({**raw,'source_engine':provider.engine,'parser_version':provider.version})
    action, polarity, quoted, reason, baseline = _language_features(source)
    time = preview_event_time(baseline['temporal_text'], source.sent_at.isoformat(), timezone)
    end_date = None
    from zoneinfo import ZoneInfo
    normalized = _normalize(baseline['temporal_text'])
    if re.search(r'\bdaqui a (duas|2) semanas\b', normalized):
        time = {'date':(_utc(source.sent_at).astimezone(ZoneInfo(timezone)).date()+timedelta(days=14)).isoformat(),
                'basis':'RELATIVE_TO_PROVIDER_TIME'}
    interval = re.search(r'\bde (\d{4}-\d{2}-\d{2}) a (\d{4}-\d{2}-\d{2})\b', normalized)
    if interval:
        start, end = (datetime.fromisoformat(part).date() for part in interval.groups())
        if end < start:
            raise ValueError('INTERPRETATION_TEMPORAL_INTERVAL_INVALID')
        time, end_date = {'date':start.isoformat(),'basis':'EXPLICIT_DATE'}, end.isoformat()
    return Interpretation(tenant_id=source.tenant_id, source_account=source.source_account,
        event_id=source.event_ref, actor_ref=source.author, action=action, topic=baseline['topic'],
        message_time=source.sent_at, event_date=time.get('date'), event_end_date=end_date, event_local_time=time.get('local_time'),
        timezone=timezone, uncertain_time=time.get('basis') != 'EXPLICIT_DATE', polarity=polarity,
        negation_scope=source.text if polarity == 'NEGATIVE' else None, quoted_speech=quoted,
        references=(source.reply_to,) if source.reply_to else (), source_message_ids=(source.message_id,),
        evidence_spans=(EvidenceSpan(message_id=source.message_id,start=0,end=len(source.text),text=source.text),),
        confidence=0.0 if action == 'UNKNOWN' else 0.7, certainty_basis=reason,
        state='UNRESOLVED' if action == 'UNKNOWN' or action == 'ANSWERS' and not source.reply_to else 'PROPOSED_INTERPRETATION',
        parser_version='PTBR_STRUCTURED_V1')


def validate_interpretation(value: Interpretation, sources: dict[str, SourceEvidence]):
    """Reject forged structure; validation is not certification of semantic truth."""
    value = Interpretation.model_validate(value.model_dump())
    refs = set(value.source_message_ids) | set(value.references) | set(value.contradiction_refs)
    if refs - sources.keys():
        raise ValueError('INTERPRETATION_EVIDENCE_NOT_FOUND')
    selected = [sources[mid] for mid in refs]
    if any((s.tenant_id, s.source_account) != (value.tenant_id, value.source_account) for s in selected):
        raise ValueError('INTERPRETATION_SCOPE_MISMATCH')
    if any(s.sensitivity == 'SECRET' for s in selected):
        raise ValueError('INTERPRETATION_SECRET')
    primary = sources[value.source_message_ids[0]]
    if primary.origin == 'ROUTER_AUTOMATED_OUTBOUND_OBSERVED' or primary.author_basis in {'AUTOMATED','UNRESOLVED',''}:
        raise ValueError('INTERPRETATION_AUTOMATION_OR_UNKNOWN')
    if value.actor_ref != primary.author or set(value.participants) - {s.author for s in selected}:
        raise ValueError('INTERPRETATION_IDENTITY_MISMATCH')
    if value.event_id != primary.event_ref or _utc(value.message_time) != _utc(primary.sent_at):
        raise ValueError('INTERPRETATION_EVENT_OR_TIME_FORGED')
    if set(value.references) - ({primary.reply_to} if primary.reply_to else set()):
        raise ValueError('INTERPRETATION_REFERENCE_FORGED')
    if {s.message_id for s in value.evidence_spans} != set(value.source_message_ids):
        raise ValueError('INTERPRETATION_SPANS_REQUIRED')
    for span in value.evidence_spans:
        source = sources[span.message_id]
        if span.end > len(source.text) or source.text[span.start:span.end] != span.text:
            raise ValueError('INTERPRETATION_SPAN_FORGED')
    baseline = interpret_offline(primary, timezone=value.timezone)
    learned_extension = (value.source_engine in {'LLM','EMBEDDING'}
                         and baseline.certainty_basis == 'NO_SUPPORTED_INTERPRETATION')
    if (value.state != baseline.state and not learned_extension) or value.confidence > 0.95:
        raise ValueError('INTERPRETATION_UNJUSTIFIED_CERTAINTY')
    if any(getattr(value, key) != getattr(baseline, key) for key in ('event_date','event_end_date','event_local_time')):
        raise ValueError('INTERPRETATION_TEMPORAL_ANCHOR_REQUIRED')
    if not value.uncertain_time and baseline.uncertain_time:
        raise ValueError('INTERPRETATION_UNJUSTIFIED_TIME_CERTAINTY')
    if baseline.action in {'QUESTIONS','REPORTS','UNKNOWN','DENIES','CANCELS','DISAGREES'} and value.action != baseline.action and not learned_extension:
        raise ValueError('INTERPRETATION_FALSE_POSITIVE')
    if value.polarity != baseline.polarity or value.quoted_speech != baseline.quoted_speech:
        raise ValueError('INTERPRETATION_POLARITY_OR_QUOTATION')
    if value.action == 'ACKNOWLEDGES' and baseline.action != 'ACKNOWLEDGES':
        raise ValueError('INTERPRETATION_KNOWLEDGE_WITHOUT_EVIDENCE')
    return value


class SyntheticRetention(Contract):
    """Explicit lab policy, not D06 approval. No default period or consent."""
    tenant_id: str
    source_account: str
    purpose: Literal['SYNTHETIC_EVALUATION']
    consent: bool
    expires_at: datetime
    tombstones: frozenset[str] = frozenset()

    def require(self, source_ids, tenant_id, account, now):
        if (not self.consent or (tenant_id,account) != (self.tenant_id,self.source_account)
                or _utc(now) >= _utc(self.expires_at) or set(source_ids) & self.tombstones):
            raise ValueError('SYNTHETIC_POLICY_DENIED')


def archived_source(session, *, tenant_id, source_account, message_id):
    from attention_router.infrastructure.models import ConversationMessageRow
    row = session.get(ConversationMessageRow, message_id)
    if row is None or (row.tenant_id,row.source_account) != (tenant_id,source_account):
        raise ValueError('INTERPRETATION_ARCHIVE_SCOPE')
    meta = row.metadata_json or {}
    if meta.get('platform_lineage', {}).get('classification') != 'SYNTHETIC':
        raise ValueError('INTERPRETATION_SYNTHETIC_ONLY')
    sensitivity = translate_sensitivity(row.sensitivity_class)
    if not row.searchable or sensitivity.candidate_class == 'SECRET':
        raise ValueError('INTERPRETATION_SOURCE_FORBIDDEN')
    return SourceEvidence(tenant_id=tenant_id,source_account=source_account,message_id=row.id,
        author=row.external_sender_key or '',author_basis=meta.get('speaker_basis','UNRESOLVED'),
        origin=meta.get('event_origin','UNKNOWN'),sent_at=row.sent_at,text=row.text or '',
        event_ref=meta.get('reviewed_event_ref'),reply_to=row.reply_to_message_id,
        modality='VOICE' if row.message_type in {'VOICE','AUDIO'} else 'TEXT',
        transcription_id=meta.get('transcription_id'), sensitivity=sensitivity.candidate_class,
        stt_ambiguous=meta.get('stt_ambiguous',False))


def propose_interpretation(session, value: Interpretation, *, policy: SyntheticRetention, now: datetime):
    refs = set(value.source_message_ids) | set(value.references) | set(value.contradiction_refs)
    policy.require(refs,value.tenant_id,value.source_account,now)
    sources = {mid:archived_source(session,tenant_id=value.tenant_id,
                source_account=value.source_account,message_id=mid) for mid in refs}
    return _propose_from_authorized_sources(session, value, sources=sources, policy=policy, now=now)


def _propose_from_authorized_sources(session, value, *, sources, policy, now, provenance=None):
    """Internal adapter seam; caller must authorize ALL sources under its fence.

    Reuses V2E, never creates another proposal store or canonical memory.
    """
    from attention_router.application.candidate_insights import CandidateEvidenceInput, propose_candidate_insight
    from attention_router.infrastructure.candidate_insight_models import CandidateInsightRow
    refs = set(value.source_message_ids) | set(value.references) | set(value.contradiction_refs)
    policy.require(refs, value.tenant_id, value.source_account, now)
    validate_interpretation(value,sources)
    lineages = {source.lineage for source in sources.values()}
    if len(lineages) != 1:
        raise ValueError('INTERPRETATION_MIXED_LINEAGE')
    lineage = next(iter(lineages))
    if lineage != 'SYNTHETIC' and policy.purpose != 'SEMANTIC_CANARY':
        raise ValueError('INTERPRETATION_SYNTHETIC_ONLY')
    if value.state != 'PROPOSED_INTERPRETATION' or value.action in {'UNKNOWN','QUESTIONS','ANSWERS','REPORTS'}:
        raise ValueError('INTERPRETATION_NOT_ADMISSIBLE_AS_CANDIDATE')
    predicate = 'context.language_event.' + stable_hash([value.source_account, value.event_id or value.source_message_ids[0]])[:24]
    previous = session.scalars(select(CandidateInsightRow).where(
        CandidateInsightRow.tenant_id==value.tenant_id, CandidateInsightRow.subject_id==value.actor_ref,
        CandidateInsightRow.predicate==predicate, CandidateInsightRow.state.in_(['REJECTED','SUPERSEDED'])))
    for row in previous:
        if set(row.provenance.get('source_message_ids',[])) == set(value.source_message_ids):
            raise ValueError('INTERPRETATION_REJECTED_EVIDENCE_REPLAY')
    evidence = [CandidateEvidenceInput('CONVERSATION_MESSAGE',mid,mid,value.confidence,
        'CONTRADICTION' if mid in value.contradiction_refs else 'SUPPORT',
        {'lineage_classification':lineage}) for mid in sorted(refs)]
    return propose_candidate_insight(session,tenant_id=value.tenant_id,insight_type='STATE_PROPOSAL',
        subject_type='ACTOR',subject_id=value.actor_ref,predicate=predicate,
        proposed_value=value.model_dump(mode='json'),source_engine=value.source_engine,confidence=value.confidence,
        evidence=evidence,sensitivity_class='PRIVATE',provenance={
            'parser_version':value.parser_version,'source_message_ids':list(value.source_message_ids),
            **(provenance or {}),
            'lineage_classification':lineage,'purpose':policy.purpose,
            'retention_expires_at':policy.expires_at.isoformat()},now=now)


def compile_interpretation_context(session, *, tenant_id, source_account, query, policy, now):
    """Graph evidence and review-only candidates stay in separate output fields."""
    from attention_router.application.context_compiler import compile_graph_context
    from attention_router.infrastructure.candidate_insight_models import CandidateInsightRow, CandidateInsightEvidenceRow
    policy.require([],tenant_id,source_account,now)
    packet = compile_graph_context(session,tenant_id,query,now=now)
    message_ids = {i.source_id for i in packet.items if i.source_type=='CONVERSATION_MESSAGE'} - policy.tombstones
    candidates = []
    if message_ids:
        rows = session.scalars(select(CandidateInsightRow).join(CandidateInsightEvidenceRow,
            CandidateInsightEvidenceRow.candidate_id==CandidateInsightRow.id).where(
            CandidateInsightRow.tenant_id==tenant_id,CandidateInsightRow.state.in_(['PROPOSED','NEEDS_REVIEW','ADMITTED']),
            CandidateInsightRow.sensitivity_class!='SECRET',CandidateInsightEvidenceRow.tenant_id==tenant_id,
            CandidateInsightEvidenceRow.source_ref.in_(message_ids)).distinct().limit(21)).all()
        for row in rows[:20]:
            payload = row.proposed_value
            if (payload.get('source_account') != source_account or row.provenance.get('lineage_classification') != 'SYNTHETIC'
                    or not row.provenance.get('retention_expires_at')):
                continue
            refs = set(payload.get('source_message_ids',[])) | set(payload.get('contradiction_refs',[])) | set(payload.get('references',[]))
            if refs & policy.tombstones or _utc(now) >= _utc(datetime.fromisoformat(row.provenance['retention_expires_at'])):
                continue
            candidates.append({'candidate_id':row.id,'state':row.state,'interpretation':payload,
                               'canonical_truth':False,'evidence_ids':sorted(refs)})
    # A deletion fence removes paths through tombstoned evidence as well.
    result = packet.internal_payload()
    safe_ids = set()
    for mid in message_ids:
        try:
            archived_source(session,tenant_id=tenant_id,source_account=source_account,message_id=mid)
            safe_ids.add(mid)
        except ValueError:
            continue
    result['items'] = [i for i in result['items'] if i['source_type']=='CONVERSATION_MESSAGE' and i['source_id'] in safe_ids and
        not any(step['from_node_id'].removeprefix('message:') in policy.tombstones or
                step['to_node_id'].removeprefix('message:') in policy.tombstones for step in i['path'])]
    result['seed_node_ids'] = []  # evidence paths explain selection without exposing unrelated seed inventory
    return {'graph_context':result,'candidate_insights':candidates,'grants_authority':False,
            'policy_fingerprint':stable_hash(policy.model_dump(mode='json')),
            'cache_expires_at':policy.expires_at.isoformat()}


def project_interpretations(values, sources):
    """Chronological proposal history; exact reviewed event references only."""
    events = {}
    for value in sorted(values,key=lambda v:(_utc(v.message_time),v.source_message_ids)):
        validate_interpretation(value,sources)
        if not value.event_id:
            continue
        event = events.setdefault(value.event_id,{'event_id':value.event_id,'history':[],'current':None})
        item = value.model_dump(mode='json')
        event['history'].append(item)
        states = {'PROPOSES':'PROPOSED','COUNTERPROPOSES':'COUNTERPROPOSED','CHANGES':'CHANGED',
                  'CONFIRMS':'CONFIRMED_DECLARATION','CANCELS':'CANCELLED_DECLARATION',
                  'DISAGREES':'CONTESTED','DENIES':'CONTESTED','AGREES':'AGREEMENT_DECLARATION'}
        if value.state == 'PROPOSED_INTERPRETATION' and value.action in states:
            event['current'] = {'state':'CONTRADICTION_REQUIRES_REVIEW' if value.contradiction_refs else states[value.action],
                                'interpretation':item,'canonical_truth':False}
    return {'events':list(events.values()),'durable_facts':[]}


def restore_synthetic_policy(snapshot: SyntheticRetention, current: SyntheticRetention):
    """Restore barrier: old backups cannot relax current consent/expiry/deletion."""
    if (snapshot.tenant_id,snapshot.source_account) != (current.tenant_id,current.source_account):
        raise ValueError('RESTORE_SCOPE_MISMATCH')
    return current.model_copy(update={'consent':snapshot.consent and current.consent,
        'expires_at':min(_utc(snapshot.expires_at),_utc(current.expires_at)),
        'tombstones':snapshot.tombstones | current.tombstones})


def purge_synthetic_derivatives(session, *, policy: SyntheticRetention):
    """Purge only this adapter's synthetic candidates, fenced by explicit tombstones.

    Raw archive/episodes are outside this operation. Caller retains the deletion
    fence outside backups before committing; this is a lab contract, not D06.
    """
    from attention_router.infrastructure.candidate_insight_models import CandidateInsightRow, CandidateInsightEvidenceRow
    rows = list(session.scalars(select(CandidateInsightRow).where(CandidateInsightRow.tenant_id==policy.tenant_id)))
    selected = {r.id:r for r in rows if r.provenance.get('lineage_classification')=='SYNTHETIC'
                and r.proposed_value.get('source_account')==policy.source_account
                and (set(r.proposed_value.get('source_message_ids',[])) |
                     set(r.proposed_value.get('references',[])) |
                     set(r.proposed_value.get('contradiction_refs',[]))) & policy.tombstones}
    # Do not silently sever correction lineage. All descendants must be fenced too.
    if any(r.supersedes_insight_id in selected and r.id not in selected for r in rows):
        raise ValueError('PURGE_CORRECTION_LINEAGE_REQUIRES_SCOPE')
    count = len(selected)
    while selected:
        parents = {r.supersedes_insight_id for r in selected.values()}
        leaves = [r for r in selected.values() if r.id not in parents]
        if not leaves:
            raise ValueError('PURGE_LINEAGE_CYCLE')
        for row in leaves:
            for evidence in session.scalars(select(CandidateInsightEvidenceRow).where(
                    CandidateInsightEvidenceRow.tenant_id==policy.tenant_id,
                    CandidateInsightEvidenceRow.candidate_id==row.id)):
                session.delete(evidence)
            session.flush()
            session.delete(row)
            session.flush()
            selected.pop(row.id)
    return count


def synthetic_cache_usable(packet, *, policy: SyntheticRetention, now, session=None):
    """No cache store is introduced; consumers must revalidate every cached packet."""
    try:
        policy.require([],policy.tenant_id,policy.source_account,now)
    except ValueError:
        return False
    from attention_router.infrastructure.candidate_insight_models import CandidateInsightRow
    for item in packet.get('candidate_insights',[]):
        if session is None:
            return False
        row = session.get(CandidateInsightRow,item['candidate_id'],populate_existing=True)
        if (row is None or row.tenant_id != policy.tenant_id or row.state != item['state']
                or row.state not in {'PROPOSED','NEEDS_REVIEW','ADMITTED'}
                or _utc(now) >= _utc(datetime.fromisoformat(row.provenance['retention_expires_at']))):
            return False
    return (packet.get('policy_fingerprint') == stable_hash(policy.model_dump(mode='json'))
            and packet.get('cache_expires_at') is not None
            and _utc(now) < _utc(datetime.fromisoformat(packet['cache_expires_at'])))
