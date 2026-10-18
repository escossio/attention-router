-- AR-1001: candidato de enriquecimento SEM segunda sonda ou segundo ciclo SQL.
-- Primeiras sete colunas preservam a ordem e as definições do detector legado.
-- Colunas 8-14 são métricas adicionais. Não instalar sem rollout separado.
BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '3s';
WITH direct AS (
    SELECT DISTINCT interaction_id, received_at
    FROM inbound_events
    WHERE source='wwebjs'
      AND received_at >= now() - interval '6 hours'
      AND (payload->>'actor_id' LIKE '%@lid'
           OR payload->>'actor_id' LIKE '%@c.us')
), direct_unique AS (
    SELECT DISTINCT interaction_id
    FROM direct WHERE interaction_id IS NOT NULL
), failure AS (
    SELECT a.event_type, a.created_at, a.interaction_id
    FROM audit_events a
    JOIN direct d ON d.interaction_id=a.interaction_id
    WHERE a.created_at >= now() - interval '6 hours'
      AND (a.event_type IN ('behavior.unavailable','conversation.blocked',
                           'execution.dispatch_failed','outbox.failed',
                           'decision.error','execution.error')
           OR a.event_type LIKE '%.failed')
), success AS (
    SELECT max(o.completed_at) AS last_delivery
    FROM outbox_messages o
    JOIN inbound_events i ON i.interaction_id=o.interaction_id
    WHERE o.status='DONE' AND o.completed_at IS NOT NULL
      AND o.destination IN ('local_transport','wwebjs')
      AND o.action_type IN ('agent_execution_text','agent_execution_voice')
      AND i.source='wwebjs'
      AND (i.payload->>'actor_id' LIKE '%@lid'
           OR i.payload->>'actor_id' LIKE '%@c.us')
), direct_events AS (
    SELECT i.interaction_id, i.processed_at, i.status
    FROM inbound_events i
    WHERE i.source='wwebjs'
      AND i.received_at >= now() - interval '6 hours'
      AND i.interaction_id IS NOT NULL
      AND (i.payload->>'actor_id' LIKE '%@lid'
           OR i.payload->>'actor_id' LIKE '%@c.us')
), decisions AS (
    SELECT a.id, a.decision_type, a.status
    FROM agent_decisions a
    JOIN direct_unique d ON a.interaction_id=d.interaction_id
    WHERE a.created_at >= now() - interval '6 hours'
), outbox_new AS (
    SELECT o.id
    FROM outbox_messages o
    JOIN direct_unique d ON o.interaction_id=d.interaction_id
    WHERE o.created_at >= now() - interval '6 hours'
), grace AS (
    SELECT g.id, g.state
    FROM conversation_response_grace_windows g
    JOIN direct_unique d ON g.anchor_interaction_id=d.interaction_id
    WHERE g.source='wwebjs'
      AND g.opened_at >= now() - interval '6 hours'
)
SELECT
    (SELECT count(DISTINCT interaction_id) FROM direct) AS legacy_direct_6h,
    (SELECT count(DISTINCT interaction_id) FROM failure WHERE event_type='behavior.unavailable') AS legacy_no_safe_6h,
    (SELECT count(DISTINCT interaction_id) FROM failure WHERE event_type='conversation.blocked') AS legacy_blueprint_6h,
    (SELECT count(DISTINCT interaction_id) FROM failure
        WHERE event_type NOT IN ('behavior.unavailable','conversation.blocked')) AS legacy_other_failed_6h,
    coalesce((SELECT extract(epoch FROM max(created_at))::bigint FROM failure),0) AS legacy_latest_failure_epoch,
    coalesce((SELECT extract(epoch FROM last_delivery)::bigint FROM success),0) AS legacy_last_agent_done_epoch,
    coalesce((SELECT extract(epoch FROM max(received_at))::bigint FROM direct),0) AS legacy_latest_direct_epoch,
    (SELECT count(*) FROM direct_events) AS direct_event_count,
    (SELECT count(*) FROM direct_events WHERE status='PROCESSED' AND processed_at IS NOT NULL) AS direct_processed_count,
    (SELECT count(*) FROM decisions) AS decisions_total,
    (SELECT count(*) FROM decisions WHERE decision_type='INSUFFICIENT_CONTEXT' AND status='DRY_RUN') AS decisions_insufficient_dry_run,
    (SELECT count(*) FROM outbox_new) AS outbox_created_count,
    (SELECT count(*) FROM grace WHERE state='RELEASED') AS grace_released_count,
    (SELECT count(*) FROM grace WHERE state='CANCELED') AS grace_canceled_count;
COMMIT;
