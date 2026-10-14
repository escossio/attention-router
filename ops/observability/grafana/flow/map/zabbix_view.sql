-- This file contains no passwords or private inventory snapshots.
-- Apply only after runtime review, with a transaction and a dedicated least-privilege role.
-- Never grant base Zabbix tables to the map reader.
CREATE OR REPLACE VIEW public.roc_flow_map_v1_inventory
WITH (security_barrier=true) AS
SELECT
  CASE
    WHEN i.key_ LIKE 'andy.docker.container[%'
    THEN 'container'
    ELSE 'attachment'
  END::text AS kind,
  hist.observed_at::bigint AS clock,
  jsonb_build_object(
    'entity_key', hist.val->>'entity_key',
    'state', CASE WHEN i.key_ LIKE 'andy.docker.container[%'
      THEN hist.val->>'state' ELSE NULL END,
    'health', CASE WHEN i.key_ LIKE 'andy.docker.container[%'
      THEN hist.val->>'health' ELSE NULL END,
    'name', CASE WHEN i.key_ LIKE 'andy.docker.container[%'
      THEN hist.val->>'name' ELSE NULL END,
    'host_name', CASE WHEN i.key_ LIKE 'andy.docker.container[%'
      THEN hist.val->>'host_name' ELSE NULL END,
    'ip', CASE WHEN i.key_ LIKE 'andy.docker.attachment[%'
      THEN hist.val->>'ip' ELSE NULL END,
    'network', CASE WHEN i.key_ LIKE 'andy.docker.attachment[%'
      THEN hist.val->>'network' ELSE NULL END
  ) AS value
FROM items AS i
JOIN hosts AS h ON h.hostid = i.hostid
JOIN LATERAL (
  SELECT hh.clock AS observed_at, hh.value::jsonb AS val
  FROM history_text AS hh
  WHERE hh.itemid = i.itemid
  ORDER BY hh.clock DESC
  LIMIT 1
) AS hist ON true
WHERE h.host = 'Andy Engine - AGT01'
  AND i.status = 0
  AND (
    i.key_ LIKE 'andy.docker.container[%'
    OR i.key_ LIKE 'andy.docker.attachment[%'
  )
  AND hist.val->>'entity_key' IN (
    'andy-whatsapp-runtime/transport/1',
    'attention-router/ingress/1',
    'attention-router-live-flow-stage4/worker/1'
  );
REVOKE ALL ON public.roc_flow_map_v1_inventory FROM PUBLIC;
-- The installer may grant SELECT on this view to the dedicated non-admin role.
