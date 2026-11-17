"""Bounded, read-only archive preview. Caller must authorize owner/tenant access.

No extraction, durable episode, claim, outbox or provider call is made here.
"""
from sqlalchemy import select
from attention_router.application.memory import preview_statement_candidates, preview_event_candidate

from attention_router.application.semantic_episodes import DEFAULT_MAX_INACTIVITY

from attention_router.infrastructure.models import ConversationMessageRow, ConversationThreadRow


def preview_conversation(session, *, tenant_id: str, source_account: str, conversation_key: str,
                         limit: int = 100) -> dict:
    if not tenant_id or not source_account or not 1 <= limit <= 200:
        raise ValueError("INVALID_PREVIEW_SCOPE")
    thread = session.scalar(select(ConversationThreadRow).where(
        ConversationThreadRow.tenant_id == tenant_id,
        ConversationThreadRow.source == "wwebjs",
        ConversationThreadRow.source_account == source_account,
        ConversationThreadRow.external_thread_key == conversation_key,
        ConversationThreadRow.thread_type == "DIRECT",
    ))
    if thread is None:
        return {"status": "NOT_FOUND", "messages": []}
    rows = list(session.scalars(select(ConversationMessageRow).where(
        ConversationMessageRow.tenant_id == tenant_id,
        ConversationMessageRow.source_account == source_account,
        ConversationMessageRow.source == "wwebjs",
        ConversationMessageRow.conversation_id == thread.id,
    ).order_by(ConversationMessageRow.sent_at, ConversationMessageRow.id).limit(limit + 1)))
    items = []
    for row in rows[:limit]:
        meta = row.metadata_json or {}
        automated = meta.get("event_origin") == "ROUTER_AUTOMATED_OUTBOUND_OBSERVED"
        text = row.text if row.sensitivity_class != "SECRET" and row.searchable else None
        items.append({
            "message_id": row.id, "provider_id": row.source_message_id,
            "sent_at": row.sent_at.isoformat(), "direction": row.direction,
            "recipient": conversation_key if row.from_me else "ACCOUNT_OWNER",
            "author": "AUTOMATED" if automated else row.external_sender_key,
            "author_basis": "AUTOMATED" if automated else meta.get("speaker_basis", "UNRESOLVED"),
            "reply_reference": meta.get("reply_reference"),
            "origin": meta.get("event_origin"), "kind": row.message_type,
            "text": text, "content_state": meta.get("content_state"),
            "evidence": {key: meta.get(key) for key in (
                "source_inbound_event_id", "transcription_id", "media_artifact_id")},
            "epistemic_status": "AUTOMATED_OUTPUT" if automated else "DECLARED_NOT_VERIFIED" if text else "UNRESOLVED_CONTENT",
        })
    episodes = []
    associations = []
    by_topic = {}
    for row, item in zip(rows[:limit], items, strict=True):
        candidates = [] if item["author_basis"] in {"AUTOMATED", "UNRESOLVED"} else (
            preview_statement_candidates(item["text"] or "", row.sender_actor_id))
        for predicate, value, confidence, quality in candidates:
            topic = predicate.split(".")[0]
            episode = by_topic.get(topic)
            if episode is None or row.sent_at - episode["last"] > DEFAULT_MAX_INACTIVITY:
                episode = {"basis": "DETERMINISTIC_STATEMENT_PREVIEW", "topic": topic,
                           "confidence": confidence, "message_ids": [], "relations": [],
                           "last": row.sent_at}
                by_topic[topic] = episode
                episodes.append(episode)
            episode["last"] = row.sent_at
            episode["message_ids"].append(row.id)
            associations.append({"predicate": predicate, "value": value,
                "author": item["author"], "confidence": confidence, "quality": quality,
                "epistemic_status": "DECLARED_NOT_VERIFIED", "evidence_message_id": row.id})
        if not candidates:
            unresolved = next((e for e in episodes if e["topic"] == "UNRESOLVED"), None)
            if unresolved is None:
                unresolved = {"basis": "NO_TOPIC_EVIDENCE", "topic": "UNRESOLVED",
                              "confidence": None, "message_ids": [], "relations": []}
                episodes.append(unresolved)
            unresolved["message_ids"].append(row.id)
    references = {entry["provider_id"]: entry["message_id"] for entry in items}
    relations = [{"type": "REPLIES_TO", "from": item["message_id"],
                  "to": references[item["reply_reference"]], "basis": "PROVIDER_QUOTE",
                  "confidence": 1.0} for item in items if item["reply_reference"] in references]
    for episode in episodes:
        episode.pop("last", None)
        episode["relations"] = [r for r in relations if r["from"] in episode["message_ids"]]
    incomplete = any(item["text"] is None for item in items) or len(rows) > limit
    return {"status": "PREVIEW_ONLY", "conversation_id": thread.id, "conversation_key": thread.external_thread_key, "messages": items,
            "context_incomplete": incomplete, "truncated": len(rows) > limit,
            "durable_claims": [], "inferences": [], "episodes": episodes, "associations": associations, "relations": relations}


def resolve_conversation_alias(session, *, tenant_id: str, source_account: str, identifier: str) -> dict:
    """Exact receipt-backed alias lookup; never merge multiple conversations.

    Existing immutable message metadata is the historical evidence. There is no
    guessed mapping or contact-name search. Large archives fail closed at cap.
    """
    if not tenant_id or not source_account or not identifier.endswith(("@lid", "@c.us")):
        raise ValueError("INVALID_ALIAS_SCOPE")
    rows = list(session.scalars(select(ConversationMessageRow).where(
        ConversationMessageRow.tenant_id == tenant_id,
        ConversationMessageRow.source == "wwebjs",
        ConversationMessageRow.source_account == source_account,
    ).order_by(ConversationMessageRow.id).limit(1001)))
    if len(rows) > 1000:
        return {"status": "LOOKUP_LIMIT", "conversation_key": None}
    candidates = set()
    reviews = {}
    for row in rows:
        meta = row.metadata_json or {}
        aliases = meta.get("provider_peer_aliases", [])
        if not isinstance(aliases, list) or any(not isinstance(a, str) for a in aliases):
            continue
        key = tuple(sorted(aliases))
        for decision in meta.get("alias_decisions", []):
            if decision.get("at", "") > reviews.get(key, {}).get("at", ""):
                reviews[key] = decision
    for row in rows:
        aliases = (row.metadata_json or {}).get("provider_peer_aliases", [])
        if isinstance(aliases, list) and all(isinstance(a, str) for a in aliases):
            review = reviews.get(tuple(sorted(aliases)))
            if review and review.get("active") is not True:
                continue
        if not isinstance(aliases, list) or identifier not in aliases:
            continue
        lids = {a for a in aliases if isinstance(a, str) and a.endswith("@lid")}
        phones = {a for a in aliases if isinstance(a, str) and a.endswith("@c.us")}
        if len(lids) != 1 or len(phones) != 1 or len(aliases) != 2:
            return {"status": "AMBIGUOUS", "conversation_key": None}
        thread = session.get(ConversationThreadRow, row.conversation_id)
        if (thread is None or thread.tenant_id != tenant_id or thread.source != "wwebjs"
                or thread.source_account != source_account or thread.thread_type != "DIRECT"
                or thread.external_thread_key != "wwebjs:" + next(iter(lids))):
            return {"status": "AMBIGUOUS", "conversation_key": None}
        candidates.add(thread.external_thread_key)
    return {"status": "MATCH" if len(candidates) == 1 else "AMBIGUOUS" if candidates else "NOT_FOUND",
            "conversation_key": next(iter(candidates)) if len(candidates) == 1 else None}


def preview_selected_conversation(session, *, tenant_id, source_account, identifier, limit=100):
    """Private exact selection by LID or proved phone; no provider/history fetch."""
    import re
    if not isinstance(identifier, str):
        raise ValueError("INVALID_ALIAS_SCOPE")
    peer = identifier.removeprefix("wwebjs:")
    if re.fullmatch(r"\+[1-9][0-9]{7,14}", peer):
        peer = peer[1:] + "@c.us"
    resolved = resolve_conversation_alias(session, tenant_id=tenant_id,
                                        source_account=source_account, identifier=peer)
    if resolved["status"] in {"AMBIGUOUS", "LOOKUP_LIMIT"}:
        return {"status": resolved["status"], "messages": []}
    key = resolved["conversation_key"] or "wwebjs:" + peer
    return preview_conversation(session, tenant_id=tenant_id, source_account=source_account,
                                conversation_key=key, limit=limit)


def set_alias_validity(session, *, tenant_id, source_account, message_id, active, decision_ref):
    """Owner-authorized correction of derived alias use; original evidence stays intact."""
    from datetime import UTC, datetime
    if not isinstance(active, bool) or not isinstance(decision_ref, str) or not 1 <= len(decision_ref) <= 120:
        raise ValueError("INVALID_ALIAS_DECISION")
    row = session.scalar(select(ConversationMessageRow).where(
        ConversationMessageRow.id == message_id, ConversationMessageRow.tenant_id == tenant_id,
        ConversationMessageRow.source_account == source_account, ConversationMessageRow.source == "wwebjs",
    ).with_for_update())
    if row is None or not row.metadata_json.get("provider_peer_aliases"):
        raise ValueError("ALIAS_EVIDENCE_NOT_FOUND")
    meta = dict(row.metadata_json)
    decisions = list(meta.get("alias_decisions", []))
    if any(d["decision_ref"] == decision_ref for d in decisions):
        prior = next(d for d in decisions if d["decision_ref"] == decision_ref)
        if prior["active"] != active:
            raise ValueError("ALIAS_DECISION_CONFLICT")
        return
    if len(decisions) >= 100:
        raise ValueError("ALIAS_REVIEW_LIMIT")
    decisions.append({"active": active, "decision_ref": decision_ref,
                      "at": datetime.now(UTC).isoformat(), "basis": "OWNER_REVIEW"})
    meta["alias_decisions"] = decisions
    row.metadata_json = meta
    session.flush()


def preview_event_time(text, sent_at, timezone):
    """Bounded PT date grammar; interpretation is not proof of occurrence."""
    import re
    from datetime import datetime, timedelta, UTC
    from zoneinfo import ZoneInfo
    from attention_router.application.context_retrieval import _normalize
    instant = datetime.fromisoformat(sent_at)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)  # archive timestamps are UTC
    local = instant.astimezone(ZoneInfo(timezone))
    normalized = _normalize(text)
    explicit = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", normalized)
    weekdays = [day for day in ('segunda', 'terca', 'quarta', 'quinta', 'sexta', 'sabado', 'domingo')
                if re.search(r'\b' + day + r'\b', normalized)]
    relatives = re.findall(r'\b(depois de amanha|anteontem|amanha|ontem|hoje)\b', normalized)
    if re.search(r'\b(passad[oa]|semana que vem|proxima semana)\b', normalized):
        return {'status': 'UNRESOLVED', 'basis': 'UNSUPPORTED_TEMPORAL_QUALIFIER'}
    if len(explicit) > 1 or len(weekdays) > 1 or len(relatives) > 1 or (explicit and relatives) or (weekdays and relatives):
        return {'status': 'AMBIGUOUS', 'expression': text}
    date, basis = None, None
    try:
        if explicit:
            date, basis = datetime.fromisoformat(explicit[0]).date(), 'EXPLICIT_DATE'
        elif relatives:
            offset = {'depois de amanha': 2, 'amanha': 1, 'hoje': 0, 'ontem': -1, 'anteontem': -2}[relatives[0]]
            date, basis = local.date() + timedelta(days=offset), 'RELATIVE_TO_PROVIDER_TIME'
        elif weekdays:
            index = ('segunda', 'terca', 'quarta', 'quinta', 'sexta', 'sabado', 'domingo').index(weekdays[0])
            date = local.date() + timedelta(days=(index - local.weekday()) % 7)
            basis = 'NEXT_WEEKDAY_HYPOTHESIS'
    except ValueError:
        return {'status': 'INVALID_DATE'}
    hours = re.findall(r'\b(?:as|às) (\d{1,2}):(\d{2})\b', normalized)
    if len(hours) > 1 or any(int(h) > 23 or int(m) > 59 for h, m in hours):
        return {'status': 'AMBIGUOUS'}
    return {'status': 'INTERPRETED' if date else 'UNRESOLVED',
            'date': date.isoformat() if date else None,
            'local_time': ':'.join(hours[0]) if hours else None,
            'timezone': timezone, 'basis': basis, 'occurred': False}


def correlate_preview_events(messages, *, tenant_id, source_account, timezone='UTC', reviews=(), person_aliases=()):
    """Projection of archive evidence, never a persistent memory admission.

    Cross-conversation joins require an explicit event reference in the source
    text. A quote inherits an event only from an earlier, scoped message. Shared
    words/time alone never merge events. Review operations affect this projection.
    """
    import re
    from datetime import datetime, UTC
    from attention_router.application.context_retrieval import _normalize
    if not tenant_id or not source_account or len(messages) > 1000 or len(reviews) > 100:
        raise ValueError('INVALID_EVENT_SCOPE')
    if any(m.get('tenant_id') != tenant_id or m.get('source_account') != source_account for m in messages):
        raise ValueError('EVENT_SCOPE_MISMATCH')
    if len(person_aliases) > 100:
        raise ValueError('PERSON_ALIAS_LIMIT')
    alias_index = {}
    for alias in person_aliases:
        if (alias.get('tenant_id'), alias.get('source_account')) != (tenant_id, source_account):
            raise ValueError('PERSON_ALIAS_SCOPE_MISMATCH')
        if (alias.get('basis') != 'OWNER_REVIEWED_ALIAS' or not alias.get('evidence_ref')
                or not isinstance(alias.get('label'), str) or not 1 <= len(alias['label']) <= 100
                or not alias.get('actor_id')):
            raise ValueError('PERSON_ALIAS_EVIDENCE_REQUIRED')
        alias_index.setdefault(_normalize(alias['label']), []).append(alias)
    evidence = {m['message_id']: dict(m) for m in messages}
    if len(evidence) != len(messages):
        raise ValueError('DUPLICATE_EVENT_EVIDENCE')
    excluded, redacted, reviewed_links, review_ids = set(), set(), {}, {}
    for review in reviews:
        if review.get('tenant_id') != tenant_id or review.get('source_account') != source_account:
            raise ValueError('REVIEW_SCOPE_MISMATCH')
        ref, target = review['decision_ref'], review['message_id']
        if target not in evidence or review['action'] not in {'REVOKE', 'RESTORE', 'EXCLUDE', 'LINK', 'UNLINK'}:
            raise ValueError('INVALID_EVENT_REVIEW')
        if ref in review_ids and review_ids[ref] != review:
            raise ValueError('REVIEW_REPLAY_CONFLICT')
        if ref in review_ids:
            continue
        review_ids[ref] = review
        if review['action'] == 'LINK':
            destination = review.get('target_message_id')
            if destination not in evidence or destination == target:
                raise ValueError('INVALID_EVENT_LINK')
            reviewed_links[target] = destination
        elif review['action'] == 'UNLINK':
            reviewed_links.pop(target, None)
        elif review['action'] == 'RESTORE':
            excluded.discard(target)
            redacted.discard(target)
        else:
            excluded.add(target)
            if review['action'] == 'EXCLUDE':
                redacted.add(target)
    def timestamp(m):
        dt = datetime.fromisoformat(m['sent_at'])
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    ordered = sorted(messages, key=lambda m: (timestamp(m), m['message_id']))
    events, assigned, unresolved, hypotheses = {}, {}, [], []
    for m in ordered:
        mid, text = m['message_id'], m.get('text')
        if (mid in excluded or not text or m.get('author_basis') in {None, 'UNRESOLVED', 'AUTOMATED'}
                or m.get('origin') == 'ROUTER_AUTOMATED_OUTBOUND_OBSERVED'):
            unresolved.append(mid)
            continue
        normalized = _normalize(text)
        refs = re.findall(r'\[evento:([a-z0-9_-]{1,80})\]', normalized)
        quote = reviewed_links.get(mid, m.get('reply_message_id'))
        parent = assigned.get(quote)
        if mid in reviewed_links and parent is None:
            raise ValueError('EVENT_LINK_TARGET_UNAVAILABLE')
        if len(set(refs)) > 1 or (refs and parent and refs[0] != parent):
            unresolved.append(mid)
            continue
        key = refs[0] if refs else parent
        candidate = preview_event_candidate(text)
        trip = candidate['topic'] == 'travel'
        if key is None:
            if not trip:
                unresolved.append(mid)
                continue
            key = 'unresolved:' + mid
        event = events.setdefault(key, {'event_id': key, 'topic': 'travel' if trip else 'UNRESOLVED',
            'basis': 'EXPLICIT_REFERENCE' if refs else 'PROVIDER_QUOTE' if parent else 'UNRESOLVED_IDENTITY',
            'history': [], 'participants': [], 'relations': [], 'current': None,
            'destinations': [], 'conversations': [], 'hypotheses': [], 'mentioned_participants': []})
        if trip:
            event['topic'] = 'travel'
        mentions = []
        for label, aliases in alias_index.items():
            if not re.search(r'(?<!\w)' + re.escape(label) + r'(?!\w)', normalized):
                continue
            actors = sorted({alias['actor_id'] for alias in aliases})
            mentions.append({'label': label, 'actor_ids': actors,
                'status': 'EXACT_REVIEWED_ALIAS' if len(actors) == 1 else 'AMBIGUOUS',
                'evidence_message_id': mid, 'alias_evidence_refs': sorted({a['evidence_ref'] for a in aliases})})
            if len(actors) == 1 and actors[0] not in event['mentioned_participants']:
                event['mentioned_participants'].append(actors[0])
        action = candidate['action']
        when = preview_event_time(candidate['temporal_text'], m['sent_at'], timezone)
        if candidate['destination'] and candidate['destination'] not in event['destinations']:
            event['destinations'].append(candidate['destination'])
        conversation = m.get('conversation_key')
        if conversation not in event['conversations']:
            event['conversations'].append(conversation)
        entry = {'message_id': mid, 'author': m['author'], 'sent_at': m['sent_at'],
                 'action': action, 'event_time': when, 'epistemic_status': 'DECLARED_NOT_VERIFIED',
                 'confidence': 1.0 if refs or parent else None,
                 'association_basis': 'OWNER_REVIEW' if mid in reviewed_links and parent else
                     'EXPLICIT_REFERENCE' if refs else 'PROVIDER_QUOTE' if parent else 'UNRESOLVED_IDENTITY',
                 'extraction_method': candidate['method'], 'superseded_by': None, 'mentions': mentions}
        previous = event['current']
        if previous and action in {'CHANGES', 'CONFIRMS', 'CONTESTS', 'ACKNOWLEDGES'}:
            event['relations'].append({'type': action, 'from': mid, 'to': previous['message_id'],
                                      'evidence_ids': [previous['message_id'], mid]})
        if action in {'PROPOSES', 'CHANGES', 'CONFIRMS', 'CONTESTS'}:
            state = 'CONTESTED' if action == 'CONTESTS' else 'CONFIRMED_DECLARATION' if action == 'CONFIRMS' else 'PROPOSED'
            if when['status'] != 'INTERPRETED' and action != 'CONTESTS':
                state = 'UNRESOLVED'
            if action == 'CONFIRMS' and previous and any(when.get(k) != previous['event_time'].get(k) for k in ('date', 'local_time', 'timezone')):
                state = 'CONFLICT'
            # Equal provider times cannot establish an ordering between conflicting declarations.
            if previous and timestamp(m) == timestamp(evidence[previous['message_id']]) and when != previous['event_time']:
                state = 'CONFLICT'
            if previous and action == 'CHANGES' and state == 'PROPOSED':
                for prior in event['history']:
                    if prior['message_id'] == previous['message_id']:
                        prior['superseded_by'] = mid
            event['current'] = {**entry, 'state': state}
        event['history'].append(entry)
        if m['author'] not in event['participants']:
            event['participants'].append(m['author'])
        assigned[mid] = key
    # Candidate links never merge state: evidence for identity of an event must
    # remain distinguishable from evidence that a person made a declaration.
    event_list = list(events.values())
    hypotheses_truncated = False
    for index, left in enumerate(event_list):
        if len(hypotheses) >= 100:
            hypotheses_truncated = True
            break
        for right in event_list[index + 1:]:
            if len(hypotheses) >= 100:
                hypotheses_truncated = True
                break
            shared_destinations = set(left['destinations']) & set(right['destinations'])
            left_dates = {h['event_time'].get('date') for h in left['history']} - {None}
            right_dates = {h['event_time'].get('date') for h in right['history']} - {None}
            shared_dates = left_dates & right_dates
            shared_people = set(left['participants'] + left['mentioned_participants']) & set(right['participants'] + right['mentioned_participants'])
            if not shared_destinations or not shared_dates:
                continue
            # At least destination + date; participants are additional evidence,
            # never a reason to collapse two trips or claim another person's knowledge.
            ids = [h['message_id'] for e in (left, right) for h in e['history']]
            hypothesis = {'type': 'POSSIBLY_SAME_EVENT', 'from': left['event_id'],
                'to': right['event_id'], 'status': 'HYPOTHESIS', 'confidence': 'UNREVIEWED',
                'signals': {'destinations': sorted(shared_destinations), 'dates': sorted(shared_dates),
                            'participants': sorted(shared_people)}, 'evidence_ids': ids}
            hypotheses.append(hypothesis)
            left['hypotheses'].append(hypothesis)
            right['hypotheses'].append(hypothesis)
    visible_evidence = {key: value for key, value in evidence.items() if key not in redacted}
    return {'status': 'PREVIEW_ONLY', 'tenant_id': tenant_id, 'source_account': source_account,
            'events': event_list, 'evidence': visible_evidence, 'unresolved': unresolved,
            'hypotheses': hypotheses, 'hypotheses_truncated': hypotheses_truncated,
            'excluded_evidence_ids': sorted(redacted),
            'context_incomplete': bool(unresolved) or hypotheses_truncated, 'reviews': list(review_ids.values()), 'durable_claims': []}


def query_preview_events(view, *, tenant_id, source_account, person=None, event_id=None,
                         topic=None, action=None, evidence_id=None, since=None, until=None, person_role='AUTHOR'):
    """Structured semantic retrieval; caller supplies resolved identities, not guessed names."""
    from datetime import datetime, UTC
    def instant(value):
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    if (view['tenant_id'], view['source_account']) != (tenant_id, source_account):
        raise ValueError('EVENT_SCOPE_MISMATCH')
    if person_role not in {'AUTHOR', 'MENTIONED'}:
        raise ValueError('INVALID_PERSON_ROLE')
    topic = {'viagem': 'travel', 'passeio': 'travel', 'excursão': 'travel', 'planejamento': 'travel'}.get(topic, topic)
    results = []
    for event in view['events']:
        if event_id and event['event_id'] != event_id or topic and event['topic'] != topic:
            continue
        matches = [h for h in event['history'] if
                   (not person or (h['author'] == person if person_role == 'AUTHOR' else
                       any(m['status'] == 'EXACT_REVIEWED_ALIAS' and person in m['actor_ids'] for m in h.get('mentions', [])))) and (not action or h['action'] == action)
                   and (not evidence_id or h['message_id'] == evidence_id)
                   and (not since or instant(h['sent_at']) >= instant(since))
                   and (not until or instant(h['sent_at']) <= instant(until))]
        if matches:
            results.append({'event_id': event['event_id'], 'current': event['current'], 'matches': matches,
                            'hypotheses': event.get('hypotheses', []), 'relations': event['relations'],
                            'evidence_ids': sorted({h['message_id'] for h in matches} |
                                                   ({event['current']['message_id']} if event['current'] else set()))})
    return {'status': 'SUPPORTED' if results else 'NO_EVIDENCE', 'results': results,
            'context_incomplete': view['context_incomplete']}


def preview_selected_events(session, *, tenant_id, source_account, identifiers, timezone='UTC', reviews=(), person_aliases=()):
    """Read-only application service over the same scoped archive/alias contracts."""
    if not 1 <= len(identifiers) <= 5 or len(set(identifiers)) != len(identifiers):
        raise ValueError('INVALID_EVENT_SELECTION')
    messages = {}
    incomplete = False
    for identifier in identifiers:
        view = preview_selected_conversation(session, tenant_id=tenant_id,
                    source_account=source_account, identifier=identifier, limit=200)
        if view['status'] != 'PREVIEW_ONLY':
            raise ValueError('EVENT_SELECTION_UNRESOLVED')
        incomplete |= view['context_incomplete']
        references = {m['provider_id']: m['message_id'] for m in view['messages']}
        for message in view['messages']:
            messages[message['message_id']] = {**message, 'tenant_id': tenant_id,
                'source_account': source_account, 'conversation_key': view['conversation_key'],
                'reply_message_id': references.get(message['reply_reference'])}
    result = correlate_preview_events(list(messages.values()), tenant_id=tenant_id,
                    source_account=source_account, timezone=timezone, reviews=reviews, person_aliases=person_aliases)
    result['context_incomplete'] |= incomplete
    return result


def answer_preview_question(view, *, tenant_id, source_account, question,
                            event_id=None, person=None):
    """Internal query adapter. Names must already be resolved by the caller.

    Answers are structured declarations, not generated factual prose. Hypotheses
    remain separate; a mention is never evidence that somebody knew or agreed.
    """
    from attention_router.application.context_retrieval import preview_question_intent
    # Validate scope even for unsupported input.
    if (view['tenant_id'], view['source_account']) != (tenant_id, source_account):
        raise ValueError('EVENT_SCOPE_MISMATCH')
    intent = preview_question_intent(question)
    if intent is None:
        return {'status': 'UNSUPPORTED_QUESTION', 'evidence_ids': []}
    if intent == 'ACKNOWLEDGES' and not person:
        return {'status': 'PERSON_SELECTION_REQUIRED', 'evidence_ids': []}
    result = query_preview_events(view, tenant_id=tenant_id, source_account=source_account,
        event_id=event_id, person=person, action=intent if intent in {'CONFIRMS', 'CHANGES', 'ACKNOWLEDGES'} else None)
    answers = []
    for item in result['results']:
        event = next(e for e in view['events'] if e['event_id'] == item['event_id'])
        statements = ([item['current']] if intent == 'CURRENT' and item['current'] else
                      [] if intent == 'CURRENT' else item['matches'])
        if statements:
            answers.append({'event_id': event['event_id'], 'declarations': statements,
                'evidence_ids': sorted({h['message_id'] for h in event['history']}),
                'hypotheses': item['hypotheses'], 'epistemic_status': 'DECLARED_NOT_VERIFIED'})
    return {'status': 'SUPPORTED' if answers else 'NO_EVIDENCE', 'intent': intent,
            'answers': answers, 'context_incomplete': result['context_incomplete'],
            'evidence_ids': sorted({mid for a in answers for mid in a['evidence_ids']})}
