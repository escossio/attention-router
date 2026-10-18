-- AR-1001: contadores de progresso funcional. Somente leitura, janela fixa.
-- Fonte complementar; não substitui o detector e não envia dados ao Zabbix.
BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '3s';
WITH direct_events AS (
    SELECT interaction_id, processed_at, status
    FROM inbound_events
    WHERE source='wwebjs'
      AND received_at >= now() - interval '6 hours'
      AND interaction_id IS NOT NULL
      AND (payload->>'actor_id' LIKE '%@lid' OR payload->>'actor_id' LIKE '%@c.us')
), direct AS (
    SELECT DISTINCT interaction_id FROM direct_events
), decisions AS (
    SELECT a.id, a.interaction_id, a.decision_type, a.status
    FROM agent_decisions a JOIN direct d ON a.interaction_id=d.interaction_id
    WHERE a.created_at >= now() - interval '6 hours'
), outbox AS (
    SELECT o.id, o.status
    FROM outbox_messages o JOIN direct d ON o.interaction_id=d.interaction_id
    WHERE o.created_at >= now() - interval '6 hours'
), grace AS (
    SELECT g.id, g.state
    FROM conversation_response_grace_windows g
    JOIN direct d ON g.anchor_interaction_id=d.interaction_id
    WHERE g.source='wwebjs' AND g.opened_at >= now() - interval '6 hours'
)
SELECT
    (SELECT count(*) FROM direct_events) AS direct_event_count,
    (SELECT count(*) FROM direct_events WHERE status='PROCESSED' AND processed_at IS NOT NULL) AS direct_processed_count,
    (SELECT count(*) FROM decisions) AS decisions_total,
    (SELECT count(*) FROM decisions WHERE decision_type='INSUFFICIENT_CONTEXT' AND status='DRY_RUN') AS decisions_insufficient_dry_run,
    (SELECT count(*) FROM outbox) AS outbox_created_count,
    (SELECT count(*) FROM grace WHERE state='RELEASED') AS grace_released_count,
    (SELECT count(*) FROM grace WHERE state='CANCELED') AS grace_canceled_count;
COMMIT;
