const { stableHash } = require('./hmac');

function serializedId(value) {
  if (!value) return null;
  if (typeof value === 'string') return value.trim() || null;
  return value._serialized || value.id || null;
}

function identifierKind(value) {
  const identifier = serializedId(value);
  if (!identifier) return 'unknown';
  if (identifier.endsWith('@lid')) return 'lid';
  if (identifier.endsWith('@c.us')) return 'c.us';
  return 'other';
}

function directionalPeerId(message) {
  const primary = message?.fromMe ? message?.to : message?.from;
  return serializedId(primary) || serializedId(message?.id?.remote) || null;
}

function normalizeAliases(values) {
  return [...new Set(values.map(serializedId).filter(Boolean))].sort();
}

function identityFromAliases(aliases) {
  const normalized = normalizeAliases(aliases);
  const lid = normalized.find((value) => identifierKind(value) === 'lid');
  const phone = normalized.find((value) => identifierKind(value) === 'c.us');
  const primary = lid || phone || (normalized.length === 1 ? normalized[0] : null);
  if (!primary) {
    return {
      state: 'AMBIGUOUS',
      reason: normalized.length ? 'MULTIPLE_UNMAPPED_PEERS' : 'CONVERSATION_PEER_MISSING',
      conversation_key: null,
      peer_id: null,
      peer_identifiers: normalized,
    };
  }
  return {
    state: 'READY',
    reason: 'CONVERSATION_PEER_RESOLVED',
    conversation_key: `wwebjs:${primary}`,
    conversation_key_hash: stableHash(`wwebjs:${primary}`),
    peer_id: primary,
    peer_identifiers: normalized,
  };
}

async function resolveConversationIdentity(message, client = null) {
  const peer = directionalPeerId(message);
  if (!peer) return identityFromAliases([]);
  const aliases = [peer];
  if (typeof client?.getContactLidAndPhone === 'function') {
    try {
      const mappings = await client.getContactLidAndPhone([peer]);
      for (const mapping of mappings || []) {
        aliases.push(mapping?.lid, mapping?.pn);
      }
    } catch {
      // A provider alias lookup is an enrichment. The directional peer remains
      // usable, but a LID/PN mismatch will fail to correlate instead of guessing.
    }
  }
  return identityFromAliases(aliases);
}

module.exports = {
  directionalPeerId,
  identifierKind,
  identityFromAliases,
  normalizeAliases,
  resolveConversationIdentity,
  serializedId,
};
