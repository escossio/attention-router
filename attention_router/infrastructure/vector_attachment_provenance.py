"""Projection used by the passive vector service and the 0072 schema provisioner."""

# Bind the projection to the existing vector scope. Expose only the identifiers
# needed to compare the original observation and the receipt's deadline.
VECTOR_ATTACHMENT_PROVENANCE = """
    SELECT m.id AS message_id, m.metadata->>'artifact_receipt_id' AS receipt_id,
           m.metadata->>'artifact_id' AS artifact_id, m.source_message_id,
           m.metadata->>'source_inbound_event_id' AS inbound_id, stt.id AS transcription_id,
           r.metadata::jsonb ? 'context_expires_at' AS has_expiry,
           r.metadata->>'context_expires_at' AS expires_at
    FROM public.conversation_messages m
    JOIN public.conversation_threads t ON t.id = m.conversation_id
      AND t.tenant_id = m.tenant_id AND t.source_account = m.source_account AND t.source = m.source
    JOIN public.artifact_receipts r ON r.id = m.metadata->>'artifact_receipt_id'
      AND r.tenant_id = m.tenant_id AND r.source_account = m.source_account
      AND r.source_channel = 'channel.whatsapp'
      AND r.external_receipt_id = pg_catalog.concat_ws(':', 'whatsapp', m.source_message_id, 'media')
    JOIN public.artifacts a ON a.id = r.artifact_id AND a.tenant_id = r.tenant_id
      AND a.id = m.metadata->>'artifact_id' AND a.status = 'AVAILABLE'
    LEFT JOIN public.inbound_events i ON i.id = m.metadata->>'source_inbound_event_id'
      AND i.tenant_id = m.tenant_id AND i.source = m.source
      AND i.external_event_id = m.source_message_id
      AND i.payload->'metadata'->>'source_account' = m.source_account
    LEFT JOIN public.voice_transcriptions stt ON stt.inbound_event_id = i.id
      AND stt.tenant_id = i.tenant_id AND stt.status = 'READY'
    WHERE m.tenant_id = pg_catalog.current_setting('andy.vector_tenant', true)
      AND m.source_account = pg_catalog.current_setting('andy.vector_account', true)
      AND m.source = 'wwebjs' AND m.searchable AND m.sensitivity_class <> 'SECRET'
      AND m.metadata->>'attachment_state' = 'READY'
      AND (m.message_type = 'TEXT' OR
           (m.message_type = 'VOICE' AND a.artifact_kind = 'AUDIO' AND stt.id IS NOT NULL))
      AND pg_catalog.jsonb_typeof(r.metadata::jsonb) = 'object'
      AND (r.metadata::jsonb->'context_evidence_withdrawn') IS DISTINCT FROM 'true'::jsonb
      AND (SELECT count(DISTINCT h.human_identity_id) FROM public.client_tenant_memberships h
           WHERE h.tenant_id = m.tenant_id AND h.role = 'OWNER' AND h.status = 'ACTIVE') = 1
"""

CREATE_VECTOR_ATTACHMENT_PROVENANCE = (
    "CREATE VIEW public.andy_vector_attachment_provenance WITH (security_barrier=true) AS "
    + VECTOR_ATTACHMENT_PROVENANCE
)
