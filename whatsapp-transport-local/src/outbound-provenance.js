const fs = require('node:fs');
const path = require('node:path');

const { stableHash } = require('./hmac');
const { identityFromAliases, normalizeAliases } = require('./conversation');
const { extractMessageId } = require('./message-id');

const AUTOMATED_FINGERPRINT_STATUSES = new Set(['SENDING', 'SENT']);
const UNCERTAIN_FINGERPRINT_STATUSES = new Set(['FAILED']);

function atomicWriteJson(file, value) {
  const temporary = `${file}.${process.pid}.${Date.now()}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify(value), { encoding: 'utf8', mode: 0o600 });
  fs.renameSync(temporary, file);
}

function createOutboundProvenanceLedger(config, deps = {}) {
  const directory = config.outboundProvenanceDir || '';
  const clock = deps.now || (() => new Date());
  const io = deps.fs || fs;
  const correlationMs = config.outboundProvenanceCorrelationMs || 120 * 1000;

  function ensureAvailable() {
    if (!directory) return false;
    io.mkdirSync(directory, { recursive: true, mode: 0o700 });
    return true;
  }

  function recordPath(idempotencyKey) {
    return path.join(directory, `${stableHash(idempotencyKey)}.json`);
  }

  function read(file) {
    return JSON.parse(io.readFileSync(file, 'utf8'));
  }

  function write(record) {
    ensureAvailable();
    atomicWriteJson(recordPath(record.idempotency_key), record);
    return record;
  }

  function all() {
    if (!ensureAvailable()) return [];
    return io.readdirSync(directory)
      .filter((name) => name.endsWith('.json'))
      .map((name) => {
        try { return read(path.join(directory, name)); } catch { return null; }
      })
      .filter(Boolean);
  }

  function begin(payload) {
    if (!ensureAvailable()) throw new Error('OUTBOUND_PROVENANCE_UNAVAILABLE');
    const identity = payload.conversation_identity || identityFromAliases([payload.destination]);
    if (identity.state !== 'READY') throw new Error('OUTBOUND_CONVERSATION_IDENTITY_AMBIGUOUS');
    const aliases = normalizeAliases([
      ...(identity.peer_identifiers || []),
      ...(payload.peer_identifiers || []),
      payload.destination,
    ]);
    const messageType = payload.message_type === 'ptt' ? 'ptt' : 'text';
    const textHash = messageType === 'text' ? stableHash(String(payload.text)) : null;
    const fingerprint = stableHash(JSON.stringify({
      idempotency_key: payload.idempotency_key,
      conversation_key: identity.conversation_key,
      peer_identifiers: aliases,
      text_hash: textHash,
      message_type: messageType,
      artifact_sha256: payload.content_sha256 || null,
      mime_type: payload.mime_type || null,
      size_bytes: payload.size_bytes || null,
    }));
    const file = recordPath(payload.idempotency_key);
    if (io.existsSync(file)) {
      const existing = read(file);
      if (existing.fingerprint !== fingerprint) throw new Error('OUTBOUND_IDEMPOTENCY_CONFLICT');
      return { record: existing, replay: true };
    }
    const now = clock().toISOString();
    const record = {
      schema_version: '1',
      idempotency_key: payload.idempotency_key,
      execution_intent_id: payload.execution_intent_id || null,
      outbox_id: payload.outbox_id || null,
      conversation_key: identity.conversation_key,
      peer_identifiers: aliases,
      text_hash: textHash,
      message_type: messageType,
      media_kind: messageType === 'ptt' ? 'VOICE_NOTE' : null,
      artifact_sha256: payload.content_sha256 || null,
      mime_type: payload.mime_type || null,
      size_bytes: payload.size_bytes || null,
      fingerprint,
      started_at: now,
      status: 'SENDING',
      message_reference: null,
      observed_message_ids: [],
      updated_at: now,
    };
    return { record: write(record), replay: false };
  }

  function update(record, values) {
    return write({ ...record, ...values, updated_at: clock().toISOString() });
  }

  function markSent(record, messageReference) {
    return update(record, { status: 'SENT', message_reference: messageReference || record.message_reference || null });
  }

  function markFailed(record, reason = 'SEND_FAILED') {
    return update(record, { status: 'FAILED', failure_reason: reason });
  }

  function classify(message, conversationIdentity) {
    if (!ensureAvailable() || conversationIdentity?.state !== 'READY') {
      return { classification: 'UNKNOWN_FROM_ME', reason: 'CONVERSATION_IDENTITY_AMBIGUOUS', provenance: null };
    }
    const messageId = extractMessageId(message);
    const aliases = new Set(conversationIdentity.peer_identifiers || []);
    const textHash = stableHash(String(message?.body || ''));
    const records = all();
    const exact = messageId
      ? records.filter((record) => record.message_reference === messageId || (record.observed_message_ids || []).includes(messageId))
      : [];
    const isMedia = Boolean(message?.hasMedia) || ['ptt', 'audio'].includes(String(message?.type || '').toLowerCase());
    const nowMs = clock().getTime();
    const fingerprintMatches = records.filter((record) => (
        (
          AUTOMATED_FINGERPRINT_STATUSES.has(record.status)
          || UNCERTAIN_FINGERPRINT_STATUSES.has(record.status)
        )
        && record.text_hash === textHash
        && record.peer_identifiers.some((identifier) => aliases.has(identifier))
    ));
    const freshFingerprintMatches = fingerprintMatches.filter((record) => (
      Number.isFinite(Date.parse(record.updated_at || record.started_at))
      && nowMs - Date.parse(record.updated_at || record.started_at) <= correlationMs
    ));
    if (isMedia) {
      if (exact.length === 1) {
        const record = exact[0];
        return {
          classification: 'ROUTER_AUTOMATED_OUTBOUND_OBSERVED',
          reason: 'MESSAGE_REFERENCE_MATCH',
          provenance: {
            idempotency_key: record.idempotency_key,
            execution_intent_id: record.execution_intent_id,
            outbox_id: record.outbox_id,
          },
        };
      }
      if (exact.length > 1) {
        return { classification: 'UNKNOWN_FROM_ME', reason: 'MULTIPLE_OUTBOUND_PROVENANCE_MATCHES', provenance: null };
      }
      const mediaRecords = records.filter((record) => (
        record.message_type === 'ptt'
        && record.peer_identifiers.some((identifier) => aliases.has(identifier))
        && Number.isFinite(Date.parse(record.updated_at || record.started_at))
        && nowMs - Date.parse(record.updated_at || record.started_at) <= correlationMs
      ));
      return {
        classification: 'UNKNOWN_FROM_ME',
        reason: mediaRecords.length ? 'MEDIA_REFERENCE_PENDING' : 'UNMATCHED_MEDIA_FROM_ME',
        provenance: null,
      };
    }
    const candidates = exact.length ? exact : freshFingerprintMatches;
    if (candidates.length > 1) {
      return { classification: 'UNKNOWN_FROM_ME', reason: 'MULTIPLE_OUTBOUND_PROVENANCE_MATCHES', provenance: null };
    }
    if (candidates.length === 0) {
      if (fingerprintMatches.length > 0) {
        return { classification: 'UNKNOWN_FROM_ME', reason: 'STALE_OUTBOUND_PROVENANCE_MATCH', provenance: null };
      }
      return { classification: 'OWNER_MANUAL_OUTBOUND_OBSERVED', reason: 'NO_ROUTER_OUTBOUND_MATCH', provenance: null };
    }
    const record = candidates[0];
    if (UNCERTAIN_FINGERPRINT_STATUSES.has(record.status)) {
      return { classification: 'UNKNOWN_FROM_ME', reason: 'FAILED_OUTBOUND_PROVENANCE_MATCH', provenance: null };
    }
    if (messageId && !(record.observed_message_ids || []).includes(messageId)) {
      update(record, { observed_message_ids: [...(record.observed_message_ids || []), messageId] });
    }
    return {
      classification: 'ROUTER_AUTOMATED_OUTBOUND_OBSERVED',
      reason: exact.length ? 'MESSAGE_REFERENCE_MATCH' : 'UNIQUE_INFLIGHT_PROVENANCE_MATCH',
      provenance: {
        idempotency_key: record.idempotency_key,
        execution_intent_id: record.execution_intent_id,
        outbox_id: record.outbox_id,
      },
    };
  }

  async function classifyBounded(message, conversationIdentity) {
    let result = classify(message, conversationIdentity);
    if (result.reason !== 'MEDIA_REFERENCE_PENDING') return result;
    const deadline = Date.now() + Math.min(correlationMs, 500);
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 10));
      result = classify(message, conversationIdentity);
      if (result.reason !== 'MEDIA_REFERENCE_PENDING') return result;
    }
    return { classification: 'UNKNOWN_FROM_ME', reason: 'UNMATCHED_MEDIA_REFERENCE', provenance: null };
  }

  return {
    available: Boolean(directory),
    begin,
    classify,
    classifyBounded,
    markFailed,
    markSent,
    records: all,
  };
}

module.exports = { createOutboundProvenanceLedger };
