"""Derived context only. Never invokes inference, providers, transport or semantic jobs."""
import json
import math
import os
import re
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from sqlalchemy import text
from attention_router.application.first_contact_identity import canonical_peer_hash


def sql(session, query, **params):
    return session.execute(text(query), params)


def peer(value):
    return (value or '').removeprefix('wwebjs:')


def phone(value):
    value = peer(value)
    if re.fullmatch(r'[1-9][0-9]{6,14}@c\.us', value):
        return '+' + value.split('@')[0]
    return None


def provider_mapping(row):
    event = SimpleNamespace(**row)
    if not canonical_peer_hash(event):
        return None
    meta = row['payload']['metadata']
    proof = meta['first_contact_identity']
    if proof['provenance'] != 'PROVIDER_LID_PN_MAPPING':
        return None
    numbers = [phone(v) for v in meta['peer_identifiers'] if phone(v)]
    if len(numbers) != 1:
        return None
    return proof['canonical_peer_id'], numbers[0], meta['source_account']


def resolve_identity(key, mappings, contacts, scope_known=True):
    """Current address-book label, explicitly not historical person verification."""
    identity = peer(key)
    numbers = {phone(identity)} if phone(identity) else set()
    proofs = [e for e in mappings if e['lid'] == identity or e['phone'] in numbers]
    numbers.update(e['phone'] for e in proofs)
    # Detect both one LID -> many phones and one phone -> many LIDs.
    all_proofs = [e for e in mappings if e['phone'] in numbers]
    conflict = len(numbers) > 1 or len({e['lid'] for e in all_proofs}) > 1
    evidence = {'identifier': identity, 'phones': sorted(numbers),
                'provider_event_ids': sorted({e['event_id'] for e in proofs}),
                'label_semantics': 'CURRENT_OWNER_ADDRESS_BOOK_NOT_HISTORICAL_IDENTITY',
                'confidence': 'UNKNOWN', 'contacts': []}
    if not scope_known:
        return 'OWNER_UNKNOWN', evidence
    if conflict:
        return 'IDENTIFIER_CONFLICT', evidence
    if not numbers:
        return 'IDENTITY_UNKNOWN', evidence
    candidates = [c for c in contacts if numbers.intersection(c['phones'])]
    evidence['contacts'] = [{k: c[k] for k in ('connection_id','resource','etag','display_name')} for c in candidates]
    if len(candidates) > 1:
        return 'CONTACT_AMBIGUOUS', evidence
    if not candidates:
        return 'CONTACT_UNKNOWN', evidence
    evidence['confidence'] = 'PROVIDER_MAPPING_EXACT_PHONE' if proofs else 'EXACT_PHONE'
    return 'CONTACT_CONFIRMED', evidence


def select_location(sent_at, observations, window=120, max_accuracy=50):
    eligible = [o for o in observations if 0 <= (sent_at-o['captured_at']).total_seconds() <= window
                and 0 < o['accuracy_m'] <= max_accuracy]
    if not eligible:
        return 'LOCATION_UNKNOWN', None
    if len({o['device_id'] for o in eligible}) != 1:
        return 'LOCATION_DEVICE_AMBIGUOUS', None
    ordered = sorted(eligible, key=lambda o: (o['captured_at'], o['observation_id']))
    latest = ordered[-1]
    if len(ordered) > 1 and all('latitude' in o and 'longitude' in o for o in ordered[-2:]):
        prior = ordered[-2]
        lat1, lat2 = math.radians(prior['latitude']), math.radians(latest['latitude'])
        dl = math.radians(latest['longitude']-prior['longitude'])
        h = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dl/2)**2
        distance = 12742000 * math.asin(min(1, math.sqrt(h)))
        if distance > max(30, prior['accuracy_m']+latest['accuracy_m']):
            return 'LOCATION_MOVEMENT_UNKNOWN', None
    # A later acquisition is never used as a historical position.
    # Window is capped at two minutes; no extrapolation of location or movement.
    return 'OWNER_DEVICE_TEMPORAL', latest


def owner_for(session, tenant):
    owners = sql(session, "SELECT human_identity_id FROM client_tenant_memberships WHERE tenant_id=:t AND role='OWNER' AND status='ACTIVE'", t=tenant).scalars().all()
    return owners[0] if len(owners) == 1 else None


def authorize_owner_scope(session, tenant, account, context_tenant, authorization_ref):
    """Explicit administrative operation; never called by the background reconciler."""
    owner = owner_for(session, tenant)
    if not owner or owner_for(session, context_tenant) != owner or not authorization_ref:
        raise ValueError('CONTEXT_OWNER_SCOPE_NOT_PROVEN')
    if not sql(session,"SELECT 1 FROM conversation_threads WHERE tenant_id=:t AND source='wwebjs' AND source_account=:a LIMIT 1",t=tenant,a=account).first():
        raise ValueError('CONTEXT_ACCOUNT_NOT_FOUND')
    sql(session,"""INSERT INTO conversation_context_owner_scopes
      (tenant_id,source_account,context_tenant_id,owner_id,source_membership_id,context_membership_id,authorization_ref)
      SELECT s.tenant_id,CAST(:a AS varchar),d.tenant_id,s.human_identity_id,s.id,d.id,CAST(:ref AS varchar) FROM client_tenant_memberships s,client_tenant_memberships d
      WHERE s.tenant_id=:t AND d.tenant_id=:c AND s.human_identity_id=:o AND d.human_identity_id=:o
      AND s.role='OWNER' AND d.role='OWNER' AND s.status='ACTIVE' AND d.status='ACTIVE'
      ON CONFLICT DO NOTHING""",t=tenant,a=account,c=context_tenant,o=owner,ref=authorization_ref)
    session.commit()


def context_scopes(session, tenant, account, owner):
    grants = sql(session,"""SELECT g.context_tenant_id FROM conversation_context_owner_scopes g
      JOIN client_tenant_memberships s ON s.id=g.source_membership_id AND s.tenant_id=g.tenant_id AND s.human_identity_id=g.owner_id
      JOIN client_tenant_memberships d ON d.id=g.context_membership_id AND d.tenant_id=g.context_tenant_id AND d.human_identity_id=g.owner_id
      WHERE g.tenant_id=:t AND g.source_account=:a AND g.owner_id=:o AND g.revoked_at IS NULL
      AND s.role='OWNER' AND d.role='OWNER' AND s.status='ACTIVE' AND d.status='ACTIVE'""",t=tenant,a=account,o=owner).scalars().all()
    return sorted(set([tenant]+grants))


def dialogue(session, *, tenant_id, source_account, conversation_id, limit=100):
    """Internal caller must authenticate tenant access; no Google call, no inferred replies."""
    return sql(session,"""SELECT * FROM conversation_context_v1 WHERE tenant_id=:t
      AND source='wwebjs' AND source_account=:a AND conversation_id=:c
      ORDER BY sent_at,source_message_id,id LIMIT :lim""",t=tenant_id,a=source_account,c=conversation_id,lim=max(1,min(limit,500))).mappings().all()


def reconcile(session, limit=100):
    if not sql(session, 'SELECT pg_try_advisory_xact_lock(630018)').scalar():
        return 0
    sql(session, "SET LOCAL statement_timeout='5s'")
    sql(session, "SET LOCAL lock_timeout='1s'")
    now = datetime.now(timezone.utc)
    window = max(0, min(120, int(os.getenv('CONVERSATION_LOCATION_WINDOW_SECONDS', '120'))))
    accuracy = max(1, min(100, float(os.getenv('CONVERSATION_LOCATION_MAX_ACCURACY_M', '50'))))
    policy = {'version':1,'location_subject':'OWNER_DEVICE_ONLY','window_seconds':window,
              'max_accuracy_m':accuracy,'future_observation_allowed':False,
              'owner_basis':'UNIQUE_ACTIVE_TENANT_OWNER','movement_extrapolation':False}
    progress = sql(session, "SELECT * FROM conversation_context_progress WHERE name='evidence' FOR UPDATE").mappings().one()
    # A bounded hourly rescan catches transactions committed behind the watermark.
    # Evidence inserts remain idempotent; this never replays ingress or actions.
    if progress['cycles'] >= 119:
        sql(session, "UPDATE conversation_context_progress SET cursor_time=NULL,cursor_id=NULL,cycles=0 WHERE name='evidence'")
        progress = dict(progress,cursor_time=None,cursor_id=None)
    else:
        sql(session, "UPDATE conversation_context_progress SET cycles=cycles+1 WHERE name='evidence'")
    events = sql(session, """SELECT id,tenant_id,source,event_type,payload,lineage_classification,received_at
      FROM inbound_events WHERE source='wwebjs' AND
      (CAST(:stamp AS timestamptz) IS NULL OR (received_at,id) > (:stamp,:cursor)) ORDER BY received_at,id LIMIT 250""",
      stamp=progress['cursor_time'], cursor=progress['cursor_id']).mappings().all()
    for event in events:
        mapping = provider_mapping(dict(event))
        if mapping:
            lid, number, account = mapping
            sql(session, """INSERT INTO conversation_identity_evidence VALUES
              (:id,:tenant,:account,:lid,:phone,:observed,'PROVIDER_LID_PN_MAPPING') ON CONFLICT DO NOTHING""",
                id=event['id'], tenant=event['tenant_id'],account=account,lid=lid,phone=number,observed=event['received_at'])
    if events:
        sql(session, "UPDATE conversation_context_progress SET cursor_time=:stamp,cursor_id=:cursor,updated_at=:now WHERE name='evidence'",
            stamp=events[-1]['received_at'],cursor=events[-1]['id'],now=now)
    # Every cycle checks current contact generations, deletions and name changes.
    threads = sql(session, "SELECT * FROM conversation_threads WHERE source='wwebjs'").mappings().all()
    caches = {}
    for thread in threads:
        scope = (thread['tenant_id'],thread['source_account'])
        if scope not in caches:
            owner = owner_for(session, scope[0])
            scopes = context_scopes(session,scope[0],scope[1],owner)
            mappings = sql(session, 'SELECT * FROM conversation_identity_evidence WHERE tenant_id=:t AND source_account=:a',t=scope[0],a=scope[1]).mappings().all()
            contacts = sql(session, """SELECT g.connection_id,g.resource,g.etag,g.phones,g.person
              FROM google_contacts_cache g JOIN google_contacts_connections c ON c.id=g.connection_id
              WHERE c.tenant_id=ANY(:scopes) AND c.owner_id=:o AND c.status='ACTIVE' AND NOT g.deleted""",scopes=scopes,o=owner).mappings().all() if owner else []
            contacts = [dict(c, display_name=next((n.get('displayName') for n in c['person'].get('names',[]) if n.get('displayName')),None)) for c in contacts]
            caches[scope] = owner,mappings,contacts,scopes
        owner,mappings,contacts,scopes = caches[scope]
        keys = set(sql(session, 'SELECT external_participant_key FROM conversation_participants WHERE conversation_id=:id',id=thread['id']).scalars())
        if thread['thread_type'] == 'DIRECT':
            keys.add(thread['external_thread_key'])
        for key in sorted(keys):
            state, evidence = resolve_identity(key,mappings,contacts,owner is not None)
            evidence['owner_id'] = owner
            evidence['authorized_context_tenants'] = scopes
            prior = sql(session,'SELECT * FROM conversation_identity_links WHERE conversation_id=:id AND participant_key=:key',id=thread['id'],key=key).mappings().first()
            if prior and prior['state'] == state and prior['evidence'] == evidence:
                continue
            revision = prior['revision']+1 if prior else 1
            values = dict(id=thread['id'], key=key,t=scope[0],a=scope[1],o=owner,s=state,e=json.dumps(evidence),r=revision,n=now)
            sql(session, '''INSERT INTO conversation_identity_links VALUES (:id,:key,:t,:a,:o,:s,CAST(:e AS jsonb),:r,:n)
              ON CONFLICT(conversation_id,participant_key) DO UPDATE SET owner_id=EXCLUDED.owner_id,state=EXCLUDED.state,
              evidence=EXCLUDED.evidence,revision=EXCLUDED.revision,updated_at=EXCLUDED.updated_at''',**values)
            sql(session, 'INSERT INTO conversation_identity_link_history VALUES (:id,:key,:r,:s,CAST(:e AS jsonb),:n)',**values)
    messages = sql(session, '''SELECT m.* FROM conversation_messages m LEFT JOIN conversation_message_context c ON c.message_id=m.id
      WHERE m.source='wwebjs' ORDER BY c.reconciled_at NULLS FIRST,m.created_at,m.id LIMIT :limit''',limit=limit).mappings().all()
    for m in messages:
        owner,_,_,scopes = caches[(m['tenant_id'],m['source_account'])]
        event_id = m['metadata'].get('source_inbound_event_id')
        event_id = sql(session, "SELECT id FROM inbound_events WHERE id=:id AND tenant_id=:t AND source=:s AND coalesce(payload->'metadata'->>'source_account','default')=:a",id=event_id,t=m['tenant_id'],s=m['source'],a=m['source_account']).scalar()
        observations = sql(session, '''SELECT * FROM client_location_observations WHERE tenant_id=ANY(:scopes) AND human_identity_id=:o
          AND captured_at<=:sent AND captured_at>=:cutoff ORDER BY captured_at''',scopes=scopes,o=owner,sent=m['sent_at'],cutoff=m['sent_at']-timedelta(seconds=window)).mappings().all() if owner else []
        state, observation = select_location(m['sent_at'],observations,window,accuracy)
        if m['metadata'].get('timestamp_basis') == 'RECEIPT_FALLBACK':
            state, observation = 'LOCATION_TIME_UNKNOWN', None
        sql(session, '''INSERT INTO conversation_message_context (message_id,tenant_id,event_id,owner_id,location_state,device_id,observation_id,delta_seconds,policy,reconciled_at,location_tenant_id) VALUES (:m,:t,:e,:o,:s,:d,:obs,:delta,CAST(:p AS jsonb),:n,:lt)
          ON CONFLICT(message_id) DO UPDATE SET event_id=EXCLUDED.event_id,owner_id=EXCLUDED.owner_id,
          location_state=EXCLUDED.location_state,device_id=EXCLUDED.device_id,observation_id=EXCLUDED.observation_id,
          location_tenant_id=EXCLUDED.location_tenant_id,delta_seconds=EXCLUDED.delta_seconds,policy=EXCLUDED.policy,reconciled_at=EXCLUDED.reconciled_at''',
          m=m['id'],t=m['tenant_id'],e=event_id,o=owner,s=state,d=observation['device_id'] if observation else None,
          obs=observation['observation_id'] if observation else None,lt=observation['tenant_id'] if observation else None,
          delta=(m['sent_at']-observation['captured_at']).total_seconds() if observation else None,p=json.dumps(policy),n=now)
    sql(session, "UPDATE conversation_context_progress SET cycles=cycles+1,updated_at=:n WHERE name='reconcile'",n=now)
    session.commit()
    return len(messages)
