-- Manual forensic aggregates, not a new collector. No identities or message text.
-- Run only against the authorized operational database; keep output private.
BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '3s';
SET LOCAL TIME ZONE 'UTC';

SELECT 'retention_bounds' AS evidence, source, count(*) AS events,
       min(received_at) AS first_received, max(received_at) AS last_received
FROM inbound_events GROUP BY source;

WITH delivered AS (
    SELECT o.id, o.completed_at, i.source, i.tenant_id,
           i.payload->>'actor_id' AS peer,
           i.lineage_classification, i.payload->>'event_origin' AS origin,
           i.payload#>>'{metadata,from_me}' AS from_me,
           a.actor_binding_id, a.semantic_source, a.response_source,
           a.agent_blueprint_id, e.authorization_source
    FROM outbox_messages o
    JOIN agent_execution_intents e ON e.id=o.execution_intent_id
    JOIN agent_decisions a ON a.id=e.agent_decision_id
    JOIN inbound_events i ON i.id=a.event_id
    WHERE o.status='DONE'
      AND o.action_type IN ('agent_execution_text','agent_execution_voice')
)
SELECT 'delivered_paths' AS evidence, source, lineage_classification, origin,
       from_me, actor_binding_id IS NOT NULL AS binding_present,
       semantic_source, response_source, authorization_source,
       count(*) AS delivered, count(DISTINCT peer) AS technical_peer_count,
       count(DISTINCT tenant_id) AS tenant_count,
       min(completed_at) AS first_done, max(completed_at) AS last_done
FROM delivered GROUP BY 2,3,4,5,6,7,8,9;

SELECT 'october_deliveries' AS evidence, o.completed_at::date AS utc_day,
       count(*) AS delivered, min(o.completed_at) AS first_done,
       max(o.completed_at) AS last_done,
       bool_and(a.actor_binding_id IS NOT NULL) AS all_bound,
       bool_and(a.agent_blueprint_id IS NOT NULL) AS all_blueprint,
       bool_and(a.policy_version_id IS NOT NULL) AS all_policy,
       bool_and(a.semantic_source='OPENAI_AGENTS_SDK') AS all_agent_sdk,
       bool_and(e.authorization_source='POLICY_AUTONOMY') AS all_policy_autonomy
FROM outbox_messages o
JOIN agent_execution_intents e ON e.id=o.execution_intent_id
JOIN agent_decisions a ON a.id=e.agent_decision_id
WHERE o.status='DONE' AND o.action_type='agent_execution_text'
  AND o.completed_at >= '2026-10-03T00:00:00Z'
GROUP BY 2 ORDER BY 2;

SELECT 'first_unconfigured' AS evidence, count(*) AS occurrences,
       min(created_at) AS first_retained, max(created_at) AS last_retained
FROM audit_events
WHERE event_type='conversation.blocked'
  AND payload->>'reason_code'='CONVERSATION_BLUEPRINT_UNCONFIGURED';

SELECT 'latest_agent_decisions' AS evidence, a.created_at, a.decision_type,
       a.status, u.reason_code,
       a.actor_binding_id IS NOT NULL AS binding_present,
       a.agent_blueprint_id IS NOT NULL AS blueprint_present
FROM agent_decisions a
LEFT JOIN autonomy_evaluations u ON u.agent_decision_id=a.id
WHERE a.semantic_source='OPENAI_AGENTS_SDK'
ORDER BY a.created_at DESC LIMIT 3;

SELECT 'last_respond_done' AS evidence, max(a.created_at) AS decision_at,
       max(o.completed_at) AS completed_at
FROM agent_decisions a
JOIN agent_execution_intents e ON e.agent_decision_id=a.id
JOIN outbox_messages o ON o.execution_intent_id=e.id
WHERE a.decision_type='RESPOND' AND o.status='DONE';

COMMIT;
