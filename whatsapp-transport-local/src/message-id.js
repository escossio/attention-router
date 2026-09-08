const { stableHash } = require('./hmac');
const { directionalPeerId } = require('./conversation');

function stringValue(value) {
  return typeof value === 'string' && value.trim() ? value : null;
}

function resolveMessageLookupId(id) {
  if (!id || typeof id !== 'object') return { lookupId: null, strategy: 'UNAVAILABLE' };
  const serialized = stringValue(id._serialized);
  if (serialized) return { lookupId: serialized, strategy: 'SERIALIZED' };
  const dollar1 = stringValue(id.$1);
  if (dollar1) return { lookupId: dollar1, strategy: 'DOLLAR1' };
  if (typeof id.fromMe !== 'boolean') return { lookupId: null, strategy: 'UNAVAILABLE' };
  const remote = stringValue(id.remote?._serialized) || stringValue(id.remote);
  const messageId = stringValue(id.id);
  if (!remote || !messageId || (!remote.endsWith('@c.us') && !remote.endsWith('@lid'))) {
    return { lookupId: null, strategy: 'UNAVAILABLE' };
  }
  return { lookupId: `${id.fromMe}_${remote}_${messageId}`, strategy: 'RECONSTRUCTED_DIRECT' };
}

function nativeCanonicalId(id) {
  if (!id || typeof id !== 'object' || typeof id.toString !== 'function') return null;
  const value = stringValue(id.toString());
  if (!value || value === '[object Object]') return null;
  return value;
}

function fallbackMessageId(id) {
  if (!id || typeof id !== 'object') return null;
  const messageId = stringValue(id.id);
  const remote = stringValue(id.remote?._serialized) || stringValue(id.remote);
  if (!messageId || !remote || typeof id.fromMe !== 'boolean') return null;
  return `wwebjs:${id.fromMe ? 'from-me' : 'inbound'}:${remote}:${messageId}`;
}

function extractMessageId(result) {
  try {
    return (
      stringValue(result?.id?._serialized)
      || stringValue(result?._data?.id?._serialized)
      || fallbackMessageId(result?.id)
      || fallbackMessageId(result?._data?.id)
      || nativeCanonicalId(result?.id)
      || nativeCanonicalId(result?._data?.id)
      || null
    );
  } catch {
    return null;
  }
}

function hashIdentityParts({ sourceMessageId, canonicalMessageId, idempotencyKey }) {
  return stableHash(JSON.stringify({ sourceMessageId, canonicalMessageId, idempotencyKey }));
}

function serializedId(value) {
  if (!value) return null;
  if (typeof value === 'string') return value;
  return value._serialized || value.id || null;
}

function canonicalIdentity(value) {
  const serialized = serializedId(value);
  return serialized ? serialized.trim().toLowerCase() : null;
}

function authenticatedSelfAliasSet(authenticatedSelfIdentity) {
  if (authenticatedSelfIdentity?.ownerVerified !== true) return null;
  if (!Array.isArray(authenticatedSelfIdentity.aliases)) return null;
  try {
    const pn = canonicalIdentity(authenticatedSelfIdentity.pn);
    const lid = authenticatedSelfIdentity.lid === null || authenticatedSelfIdentity.lid === undefined
      ? null
      : canonicalIdentity(authenticatedSelfIdentity.lid);
    const wid = authenticatedSelfIdentity.wid === null || authenticatedSelfIdentity.wid === undefined
      ? null
      : canonicalIdentity(authenticatedSelfIdentity.wid);
    if (!pn?.endsWith('@c.us')) return null;
    if (lid !== null && !lid.endsWith('@lid')) return null;
    if (wid !== null && wid !== pn && wid !== lid) return null;
    const expected = new Set([wid, pn, lid].filter(Boolean));
    const aliases = new Set(authenticatedSelfIdentity.aliases.map(canonicalIdentity));
    if (
      aliases.size !== expected.size
      || [...aliases].some((alias) => !expected.has(alias))
    ) {
      return null;
    }
    return aliases;
  } catch {
    return null;
  }
}

function isAuthenticatedOwnerSelfChatMessage(message, authenticatedSelfIdentity = null) {
  if (!message || message.fromMe !== true) return false;
  const aliases = authenticatedSelfAliasSet(authenticatedSelfIdentity);
  if (!aliases) return false;
  try {
    const peer = canonicalIdentity(directionalPeerId(message));
    if (!peer || !aliases.has(peer)) return false;
    const endpoints = [];
    for (const value of [message.from, message.to, message?.id?.remote]) {
      if (value === null || value === undefined) continue;
      const endpoint = canonicalIdentity(value);
      if (!endpoint) return false;
      endpoints.push(endpoint);
    }
    return endpoints.length > 0 && endpoints.every((endpoint) => aliases.has(endpoint));
  } catch {
    return false;
  }
}

function kindForId(value) {
  const id = serializedId(value);
  if (!id) return 'unknown';
  if (id.includes('@lid')) return 'lid';
  if (id.includes('@c.us')) return 'c.us';
  if (id.includes('@broadcast')) return 'broadcast';
  if (id.includes('@newsletter')) return 'newsletter';
  return 'other';
}

module.exports = {
  resolveMessageLookupId,
  extractMessageId,
  hashIdentityParts,
  nativeCanonicalId,
  fallbackMessageId,
  stringValue,
  serializedId,
  canonicalIdentity,
  authenticatedSelfAliasSet,
  isAuthenticatedOwnerSelfChatMessage,
  kindForId,
};
