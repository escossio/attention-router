const { extractMessageId, hashIdentityParts, isAuthenticatedOwnerSelfChatMessage, kindForId } = require('./message-id');
const { directionalPeerId, identityFromAliases } = require('./conversation');

function isoFromUnixSeconds(value) {
  if (value === undefined || value === null || value === '') return null;
  const ms = Number(value) * 1000;
  if (!Number.isFinite(ms)) return null;
  const stamp = new Date(ms);
  return Number.isNaN(stamp.getTime()) ? null : stamp.toISOString();
}

function normalizeInboundMessage(message, options = {}) {
  const source = options.source || 'wwebjs';
  const channel = options.channel || 'whatsapp';
  const finalFromMeClassification = message?.__finalFromMeClassification || null;
  const authenticatedSelfChatTarget = Boolean(
    options.authenticatedSelfAuthorityCurrent === true
    && isAuthenticatedOwnerSelfChatMessage(message, options.authenticatedSelfIdentity),
  );
  const ownerSelfChat = Boolean(
    finalFromMeClassification === 'OWNER_COMMAND'
    && message?.__ownerSelfChat === true
    && authenticatedSelfChatTarget,
  );
  const externalEventId = extractMessageId(message);
  const authenticatedWid = options.clientInfo?.wid || null;
  const from = ownerSelfChat ? (authenticatedWid || message?.from || null) : (message?.from || null);
  const externalActorId = typeof from === 'string' ? from : (from?._serialized || from?.id || null);
  const type = String(message?.type || 'unknown');
  const text = typeof message?.body === 'string' ? message.body : '';
  const hasMedia = Boolean(message?.hasMedia);
  const conversation = message?.__conversationIdentity || identityFromAliases([directionalPeerId(message)]);
  const proposedFromMeClassification = finalFromMeClassification || message?.__fromMeClassification || null;
  const fromMeClassification = proposedFromMeClassification === 'OWNER_COMMAND' && !ownerSelfChat
    ? 'UNKNOWN_FROM_ME'
    : proposedFromMeClassification;
  const normalizedFinalFromMeClassification = finalFromMeClassification === 'OWNER_COMMAND' && !ownerSelfChat
    ? 'UNKNOWN_FROM_ME'
    : finalFromMeClassification;
  const isOwnerObservation = Boolean(message?.fromMe) && !ownerSelfChat && Boolean(fromMeClassification);
  return {
    schema_version: '1',
    source,
    external_event_id: externalEventId,
    event_type: 'message',
    occurred_at: isoFromUnixSeconds(message?.timestamp),
    received_at: options.receivedAt || new Date().toISOString(),
    external_actor_id: isOwnerObservation ? (conversation.peer_id || externalActorId) : externalActorId,
    channel,
    message_type: type,
    content: isOwnerObservation ? '' : text,
    has_media: hasMedia,
    metadata: {
      provider: 'wwebjs',
      source_account: options.sourceAccount || 'default',
      message_type: type,
      from_me: Boolean(message?.fromMe),
      owner_self_chat: ownerSelfChat,
      authenticated_owner_self_chat_target: authenticatedSelfChatTarget,
      authenticated_owner: ownerSelfChat ? authenticatedWid : null,
      conversation_key: conversation.conversation_key,
      conversation_key_hash: conversation.conversation_key_hash,
      conversation_state: conversation.state,
      conversation_reason: conversation.reason,
      peer_identifiers: conversation.peer_identifiers,
      peer_id_kind: kindForId(conversation.peer_id),
      from_me_classification: fromMeClassification,
      final_from_me_classification: normalizedFinalFromMeClassification,
      outbound_provenance: message?.__outboundProvenance || null,
      has_media: hasMedia,
      input_modality: ['ptt', 'audio'].includes(type.toLowerCase()) ? 'voice' : 'text',
      media_state: ['ptt', 'audio'].includes(type.toLowerCase()) ? (hasMedia ? 'PENDING' : 'MISSING') : null,
    },
    event_origin: ownerSelfChat ? 'OWNER_COMMAND' : (fromMeClassification || 'EXTERNAL_INBOUND'),
    owner_authenticated: ownerSelfChat,
    identity: {
      source_message_id: externalEventId,
      canonical_message_id: externalEventId,
      idempotency_key: externalEventId ? `${source}:${externalEventId}` : null,
      identity_hash: hashIdentityParts({
        sourceMessageId: externalEventId,
        canonicalMessageId: externalEventId,
        idempotencyKey: externalEventId ? `${source}:${externalEventId}` : null,
      }),
    },
  };
}

module.exports = {
  normalizeInboundMessage,
};
