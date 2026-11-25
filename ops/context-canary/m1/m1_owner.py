"""Isolated test overlay. Keep the application, authority and deployment limits unchanged."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from hashlib import sha256
import json
import logging
from math import ceil
import os
from pathlib import Path
import re
import sqlite3
from time import monotonic, sleep

from experiment import experiment

import httpx
from sqlalchemy import text

from attention_router.application import conversation_evidence_staged
from attention_router.infrastructure.client_bootstrap_models import ClientTenantMembershipRow
from attention_router.infrastructure.models import ConversationMessageRow, VoiceTranscriptionRow
from test_cognitive_observation import ACCOUNT, START, _attachment


DATA=Path('evals/governed-retrieval/cases.json')
CONTROLS={
    'foreign-account': ('A vitrine de safira foi fotografada na quinta-feira.', 'other-account', 'demo'),
    'foreign-tenant': ('A altitude do planador de jade é novecentos metros.', ACCOUNT, 'other'),
}


def trace_failures(monkeypatch):
    from attention_router.api.v1 import client_context_staged

    original=client_context_staged.record_failure
    def record(error):
        name=type(error).__name__
        detail=error.args[0] if error.args else None
        sqlstate=getattr(getattr(error,'orig',None),'sqlstate',None)
        if isinstance(detail,str) and re.fullmatch(r'(CONTEXT|STAGED|CONVERSATION|RELEVANCE)_[A-Z0-9_]+',detail):
            name+=f' {detail}'
        if isinstance(sqlstate,str) and re.fullmatch(r'[0-9A-Z]{5}',sqlstate):
            name+=f' sqlstate={sqlstate}'
        print(f'M1_REJECTION {name}',flush=True)
        original(error)

    monkeypatch.setattr(client_context_staged,'record_failure',record)


def seed(Session, lab, owner_writer, bearer, scope_ref, decision):
    fixture=json.loads(DATA.read_text())
    sources=fixture['sources']
    assert len(sources)==12 and len(fixture['queries'])==16
    source_bodies=dict(sources)
    # Reserve headroom within the 200-segment guard for the original OWNER fixture.
    target = int(os.environ.get("M1_SEGMENTS", "199"))
    for index in range(target-3-len(sources)):
        source_bodies[f'd{index:03}']=f'A remessa de etiquetas do arquivo geral recebeu o protocolo {index}.'
    voice={'s03','s12'}
    ids=lab[0]
    tenant=lab[6]['tenant_id']
    with Session.begin() as db:
        db.add(ClientTenantMembershipRow(
            id='fixture-m1-other-owner',human_identity_id=lab[6]['owner_human_identity_id'],
            tenant_id='other',role='OWNER',status='ACTIVE',created_at=START,updated_at=START))
        for name,body in source_bodies.items():
            is_voice=name in voice
            mid,inbound,_=_attachment(db,'m1-'+name,3,content=body,conversation=f'wwebjs:m1-{name}',
                content_sha256=sha256(name.encode()).hexdigest(),media_kind='ptt' if is_voice else 'image')
            message=db.get(ConversationMessageRow,mid)
            message.message_type='VOICE' if is_voice else 'TEXT'
            message.text=body
            if is_voice:
                db.add(VoiceTranscriptionRow(id=f'm1-transcript:{name}',tenant_id=tenant,inbound_event_id=inbound.id,
                    status='READY',transcript_text=body,attempt_count=1,created_at=START,updated_at=START))
            ids[name]=mid
        for name,(body,account,target) in CONTROLS.items():
            mid,_,_=_attachment(db,'m1-'+name,1,content=body,conversation=f'wwebjs:m1-{name}',
                content_sha256=sha256(name.encode()).hexdigest(),account=account,
                tenant_id=tenant if name=='foreign-account' else target)
            message=db.get(ConversationMessageRow,mid)
            message.message_type='TEXT'
            message.text=body
            ids[name]=mid
    for name in source_bodies:
        assert owner_writer.grant(bearer=bearer,scope_ref=scope_ref,**decision(name)), name
    return fixture, source_bodies, ids.copy(), tenant


def label_stats(values):
    ordered=sorted(values)
    return {f'p{p}':round(1000*ordered[ceil(p*len(ordered)/100)-1],1) for p in (50,95,99)}


def run(client,path,headers,scope_ref,anchor_path,caplog,Session,monkeypatch,world):
    fixture,bodies,ids,tenant=world
    root=Path(os.environ['M1_RESULTS'])
    expected_n=int(os.environ.get('M1_WAVES','6'))
    burst=int(os.environ.get("M1_BURST", "4"))
    target=int(os.environ.get("M1_SEGMENTS", "199"))
    by_ref={ref:name for name,ref in ids.items()}
    cases={row[0]: row for row in fixture['queries']}
    cases['foreign-account-exact']=['foreign-account-exact','other_account',CONTROLS['foreign-account'][0],[]]
    cases['foreign-tenant-exact']=['foreign-tenant-exact','other_tenant',CONTROLS['foreign-tenant'][0],[]]
    order=[*cases,'q04','q09']
    with Session() as db:
        stats=db.execute(text("""SELECT tenant_id,source_account,count(*) FROM conversation_vector_segments
            WHERE state='READY' GROUP BY tenant_id,source_account""")).all()
    shaped={(row[0],row[1]):row[2] for row in stats}
    assert shaped[tenant,ACCOUNT]==target and shaped[tenant,'other-account']>=1 and shaped['other',ACCOUNT]>=1, shaped

    run_observer = experiment(bodies, CONTROLS, order, cases, shaped[tenant,ACCOUNT])

    def request(case, due, *, workload="load"):
        _,category,question,expected=cases[case]
        start=monotonic()
        trace, request_headers = run_observer.offer(headers, workload)
        try:
            reply=client.post(path,headers=request_headers,json={'scope_ref':scope_ref,'question':question})
        except httpx.TimeoutException:
            stop=monotonic()
            run_observer.complete(trace, workload, "client_timeout", stop-due)
            return {'case':case,'category':category,'correlation':None,'status':0,'verdict':'transport-timeout',
                    'expected':expected,'refs':[],'elapsed':stop-due,'service':stop-start}
        stop=monotonic()
        corr=reply.headers.get('x-correlation-id')
        if not corr and reply.status_code==503 and reply.content==b'Service Unavailable':
            run_observer.complete(trace, workload, "transport_saturation", stop-due)
            return {'case':case,'category':category,'correlation':None,'status':503,'verdict':'asgi-overload',
                    'expected':expected,'refs':[],'elapsed':stop-due,'service':stop-start}
        assert corr
        if reply.status_code!=200:
            assert not reply.content, case
            run_observer.complete(trace, workload, "empty_response", stop-due)
            return {'case':case,'category':category,'correlation':corr,'status':reply.status_code,'verdict':'refusal',
                    'expected':expected,'refs':[],'elapsed':stop-due,'service':stop-start}
        result=reply.json()
        assert result['correlation_id']==corr and result['conclusion'] is None
        evidence=result['evidence']
        refs=sorted({by_ref.get(row['source_ref'],'unknown') for row in evidence})
        literals=all(row['excerpt'] in bodies.get(by_ref.get(row['source_ref']), '') and row['observed_at']
                     for row in evidence)
        grounded=set(refs)==set(expected) and literals and result['kind']==('OBSERVATION' if expected else 'INSUFFICIENT_EVIDENCE')
        run_observer.complete(trace, workload, "abstention" if grounded and not expected else
                              "grounded" if grounded else "semantic_mismatch", stop-due)
        return {'case':case,'category':category,'correlation':corr,'status':200,'verdict':'exact' if grounded else 'semantic-mismatch',
                'expected':expected,'refs':refs,'elapsed':stop-due,'service':stop-start}

    rows=[]
    faults={}
    with caplog.at_level(logging.INFO,logger='attention_router.observability.private_context'):
        start=monotonic()+0.1
        with ThreadPoolExecutor(max_workers=4) as pool:
            for wave in range(expected_n):
                wave_start=start+60*wave
                rotated=order[wave:]+order[:wave]
                pending=[]
                for slot,case in enumerate(rotated):
                    # The offered initial burst is a separate lab factor.
                    due=wave_start + 3*max(0,slot-(burst-1))
                    sleep(max(0,due-monotonic()))
                    pending.append(pool.submit(request,case,due))
                current=[future.result() for future in pending]
                run_observer.resources()
                rows.extend(current)
                print('M1_WAVE',wave+1,len(current),sum(row['status']==200 for row in current),flush=True)
        duration=round(monotonic()-start,3)
        root.with_name('healthy-rows.json').write_text(json.dumps(rows,indent=2,sort_keys=True)+'\n')
        with closing(sqlite3.connect(anchor_path,timeout=0)) as blocker:
            blocker.execute('BEGIN EXCLUSIVE')
            try:
                faults['exclusive_anchor']=request('q03',monotonic(),workload="fault")
            finally:
                blocker.rollback()
        faults['anchor_recovery']=request('q03',monotonic(),workload="fault")

        with Session.begin() as db:
            blocked=db.execute(text("""SELECT id FROM context_consent_scopes
                WHERE tenant_id=:tenant AND source_account=:account FOR UPDATE"""),
                {'tenant':tenant,'account':ACCOUNT}).all()
            assert len(blocked)==1
            faults['consent_lock']=request('q03',monotonic(),workload="fault")
        faults['consent_recovery']=request('q03',monotonic(),workload="fault")

        original_compile=conversation_evidence_staged._compile
        def update_after_capture(*args, **kwargs):
            packet=original_compile(*args,**kwargs)
            with Session.begin() as db:
                row=db.get(ConversationMessageRow,ids['s09'])
                row.metadata_json={**row.metadata_json,'m1_synthetic_revision':'changed'}
            return packet

        with monkeypatch.context() as patch:
            patch.setattr(conversation_evidence_staged,'_compile',update_after_capture)
            faults['support_changed']=request('q09',monotonic(),workload="fault")
        faults['support_recovery']=request('q09',monotonic(),workload="fault")

    traced={row['correlation']:[] for row in [*rows,*faults.values()] if row['correlation'] is not None}
    for log in caplog.records:
        if len(log.args)>=2 and log.args[0] in traced:
            traced[log.args[0]].append(log.args[1])
    isolated={'foreign-account','foreign-tenant','unknown'}
    isolation=all(isolated.isdisjoint(row['refs']) for row in rows)
    for row in [*rows,*faults.values()]:
        states=traced.get(row.pop('correlation'),[])
        row['fault_states']=[s for s in states if s in {'EPOCH_INVALIDATED','TIMEOUT','INDEX_FAILURE','DATABASE_FAILURE','DELIVERY_BLOCKED','QUERY_REFUSED','REVOCATION_OBSERVED'}]
        row['elapsed_ms']=round(1000*row['elapsed'],1)
    summary={'indexed_authorized_scope':shaped[tenant,ACCOUNT],'indexed_isolation_controls':2,'waves':expected_n,
             'parallel':4,'burst':burst,'window_cap':32,'offered_per_minute':20,'minutes_observed':round(duration/60,2),
             'http':dict(Counter(r['status'] for r in rows)),'semantics':dict(Counter(r['verdict'] for r in rows)),
             'scope_isolated':isolation,
             'valid_abstentions':sum(not r['expected'] and r['verdict']=='exact' for r in rows),
             'failure_states':dict(Counter(s for r in rows for s in r['fault_states'])),
             'e2e_ms':label_stats([r['elapsed'] for r in rows]),'service_ms':label_stats([r['service'] for r in rows]),
             'faults':{name:{'status':r['status'],'elapsed_ms':r['elapsed_ms'],'verdict':r['verdict'],'states':r['fault_states']} for name,r in faults.items()},
             'rows':rows}
    root.write_text(json.dumps(summary,indent=2,sort_keys=True)+'\n')
    run_observer.finish()
    print('M1_METRICS '+json.dumps({k:v for k,v in summary.items() if k!='rows'},sort_keys=True),flush=True)
    assert isolation
    assert faults['exclusive_anchor']['status']==503 and 'EPOCH_INVALIDATED' in faults['exclusive_anchor']['fault_states']
    assert faults['consent_lock']['status']==503 and 'TIMEOUT' in faults['consent_lock']['fault_states']
    assert faults['support_changed']['status']==503
    assert all(faults[name]['status']==200 for name in ('anchor_recovery','consent_recovery','support_recovery'))
