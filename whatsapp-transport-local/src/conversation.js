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

function firstContactIdentity(peer, mappings = null) {
  const lid = /^[0-9]+@lid$/;
  const pn = /^[0-9]+@c\.us$/;
  if (typeof peer !== 'string' || (!lid.test(peer) && !pn.test(peer))) return null;
  if (mappings !== null) {
    if (!Array.isArray(mappings)) return null;
    if (mappings.length) {
      const lids = new Set();
      const phones = new Set();
      for (const mapping of mappings) {
        if (!mapping || typeof mapping !== 'object' || Array.isArray(mapping)
            || typeof mapping.lid !== 'string' || !lid.test(mapping.lid)
            || (mapping.pn != null && (typeof mapping.pn !== 'string' || !pn.test(mapping.pn)))) return null;
        if (mapping.lid !== peer && mapping.pn !== peer) return null;
        lids.add(mapping.lid);
        if (mapping.pn != null) phones.add(mapping.pn);
      }
      if (lids.size !== 1 || phones.size > 1) return null;
      return {
        contract_version: 'provider-lid-v1', state: 'VERIFIED',
        canonical_peer_id: [...lids][0], provenance: 'PROVIDER_LID_PN_MAPPING',
      };
    }
  }
  return lid.test(peer) ? {
    contract_version: 'provider-lid-v1', state: 'VERIFIED',
    canonical_peer_id: peer, provenance: 'DIRECTIONAL_LID',
  } : null;
}

async function resolveConversationIdentity(message, client = null, options = {}) {
  const peer = directionalPeerId(message);
  if (!peer) return identityFromAliases([]);
  const aliases = [peer];
  let firstContactMappings = null;
  if (typeof client?.getContactLidAndPhone === 'function') {
    try {
      const mappings = await client.getContactLidAndPhone([peer]);
      firstContactMappings = mappings;
      for (const mapping of mappings || []) {
        aliases.push(mapping?.lid, mapping?.pn);
      }
    } catch {
      // A provider alias lookup is an enrichment. The directional peer remains
      // usable, but a LID/PN mismatch will fail to correlate instead of guessing.
    }
  }
  const identity = identityFromAliases(aliases);
  if (options.firstContactEnabled === true) {
    const verified = firstContactIdentity(peer, firstContactMappings);
    if (verified) identity.first_contact_identity = verified;
  }
  return identity;
}

module.exports = {
  directionalPeerId,
  firstContactIdentity,
  identifierKind,
  identityFromAliases,
  normalizeAliases,
  resolveConversationIdentity,
  serializedId,
};
