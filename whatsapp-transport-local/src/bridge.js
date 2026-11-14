const { normalizeInboundMessage } = require('./inbound');
const { enqueuePending, deliverPendingFile } = require('./spool');
const { captureInboundMedia } = require('./media');
const {
  authenticatedSelfAliasSet,
  canonicalIdentity,
  isAuthenticatedOwnerSelfChatMessage,
} = require('./message-id');
const { createNoopTransportTracing } = require('./observability');

function isLocalForwardTarget(url, allowedHosts = ['127.0.0.1', 'localhost', '::1']) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'http:' && allowedHosts.includes(parsed.hostname);
  } catch {
    return false;
  }
}

function tracingResult(result) {
  if (result?.forwarded === true) return 'DELIVERED';
  if (result?.status === 'auth_failed') return 'AUTH_FAILED';
  if (result?.status === 'conflict') return 'CONFLICT';
  if (result?.status === 'bad_payload') return 'BAD_PAYLOAD';
  if (result?.status === 'retry') return 'RETRY';
  if (result?.status === 'missing') return 'MISSING';
  if (String(result?.status || '').startsWith('ignored')) return 'IGNORED';
  return 'BLOCKED';
}

const UNATTRIBUTED_OUTBOUND_MEDIA_REASONS = new Set([
  'UNMATCHED_MEDIA_FROM_ME',
  'UNMATCHED_MEDIA_REFERENCE',
]);

function eligibleUnattributedOutboundMedia(config, message, normalized, options = {}) {
  if (
    config.conversationCaptureUnattributedOutboundMediaEnabled !== true
    || config.whatsappArtifactIngestionEnabled !== true
    || !config.tenantId || config.tenantId !== normalized?.tenant_id
    || config.inboundForwardEnabled !== true
    || typeof config.hmacSecret !== 'string' || config.hmacSecret.length < 32
    || !config.mediaNotificationUrl
    || !isLocalForwardTarget(config.mediaNotificationUrl, config.inboundForwardHosts)
    || !message || message.fromMe !== true || message.__ownerSelfChat === true
    || !normalized || !normalized.has_media
    || normalized.source !== 'wwebjs' || normalized.channel !== 'whatsapp'
    || !['image', 'document'].includes(String(normalized.message_type || '').toLowerCase())
    || normalized.event_origin !== 'UNKNOWN_FROM_ME'
    || normalized.owner_authenticated !== false
    || normalized.metadata?.from_me !== true
    || normalized.metadata?.from_me_classification !== 'UNKNOWN_FROM_ME'
    || normalized.metadata?.final_from_me_classification !== 'UNKNOWN_FROM_ME'
    || normalized.metadata?.conversation_state !== 'READY'
    || normalized.metadata?.is_group === true
    || !['c.us', 'lid'].includes(normalized.metadata?.peer_id_kind)
    || normalized.metadata?.owner_self_chat === true
    || normalized.metadata?.authenticated_owner_self_chat_target === true
    || normalized.metadata?.outbound_provenance != null
    || options.authenticatedSelfAuthorityCurrent !== true
    || !UNATTRIBUTED_OUTBOUND_MEDIA_REASONS.has(message.__fromMeClassificationReason)
    || message.__fromMeClassificationReason !== normalized.metadata?.from_me_classification_reason
    || isAuthenticatedOwnerSelfChatMessage(message, options.authenticatedSelfIdentity)
  ) return false;

  // The owner account must be cryptographically associated with the
  // currently authenticated WhatsApp identity. This does NOT establish
  // the human author of the message.
  const verified = authenticatedSelfAliasSet(options.authenticatedSelfIdentity);
  if (!verified) return false;
  const ownerWid = canonicalIdentity(options.clientInfo?.wid);
  const observedSender = canonicalIdentity(message.from);
  if (!ownerWid || !observedSender
      || !verified.has(ownerWid) || !verified.has(observedSender)) {
    return false;
  }
  // Only genuine account-originated message IDs in individual chats.
  const remote = canonicalIdentity(message?.id?.remote);
  const serialized = message?.id?._serialized;
  const idFromMe = message?.id?.fromMe === true
    || (message?.id?.fromMe == null
      && typeof serialized === 'string' && serialized.startsWith('true_'));
  return Boolean(remote && ['@c.us', '@lid'].some(suffix => remote.endsWith(suffix))
    && idFromMe);
}

function createInboundBridge(config, logger = console, deps = {}) {
  const fetchImpl = deps.fetchImpl || fetch;
  const onCounters = deps.onCounters || (() => {});
  const observability = deps.observability || createNoopTransportTracing();

  async function handleMessageOperation(message, options = {}, receiveSpan = null) {
    const receivedAt = new Date().toISOString();
    const fromMe = Boolean(message?.fromMe);
    const ownerSelfChat = Boolean(message?.__ownerSelfChat);
    const fromMeClassification = message?.__fromMeClassification;
    const allowedObservation = [
      'OWNER_MANUAL_OUTBOUND_OBSERVED',
      'ROUTER_AUTOMATED_OUTBOUND_OBSERVED',
      'UNKNOWN_FROM_ME',
    ].includes(fromMeClassification);
    if (fromMe && !ownerSelfChat && !allowedObservation) {
      return { status: 'ignored_from_me', normalized: null, forwarded: false };
    }
    if (!fromMe && ownerSelfChat) {
      return { status: 'ignored_self_chat_signal', normalized: null, forwarded: false };
    }
    if (config.inboundForwardEnabled && !config.tenantId) {
      onCounters({ last_inbound_error_at: new Date().toISOString() });
      return { status: 'blocked_missing_tenant', normalized: null, forwarded: false };
    }
    const normalized = normalizeInboundMessage(message, {
      tenantId: config.tenantId,
      source: config.inboundSource,
      channel: config.inboundChannel,
      sourceAccount: config.sourceAccount,
      conversationCaptureOwnerTextEnabled: config.conversationCaptureOwnerTextEnabled === true,
      clientInfo: options.clientInfo,
      authenticatedSelfIdentity: options.authenticatedSelfIdentity,
      authenticatedSelfAuthorityCurrent: options.authenticatedSelfAuthorityCurrent === true,
      receivedAt,
    });
    onCounters({
      inbound_seen_count: 1,
      last_inbound_seen_at: new Date().toISOString(),
    });
    if (!config.inboundForwardEnabled) {
      return { status: 'blocked', normalized, forwarded: false };
    }
    if (!config.inboundForwardUrl || !isLocalForwardTarget(config.inboundForwardUrl, config.inboundForwardHosts)) {
      return { status: 'blocked_invalid_target', normalized, forwarded: false };
    }
    const unattributedAccountMedia = eligibleUnattributedOutboundMedia(
      config, message, normalized, options,
    );
    if (unattributedAccountMedia) {
      // Recorded as ACCOUNT-ORIGINATED EVIDENCE only. Never a command,
      // human-attributed owner message or an execution authorization.
      normalized.metadata.account_outbound_media_capture_basis =
        'VERIFIED_ACCOUNT_UNATTRIBUTED';
    }
    const wirePayload = Object.fromEntries(
      Object.entries(normalized).filter(([key]) => key !== 'identity'),
    );
    const pending = enqueuePending(config, normalized, wirePayload);
    const deliveryPromise = deliverPendingFile(
      config,
      pending.file,
      logger,
      fetchImpl,
      { observability, parentContext: receiveSpan?.context || null },
    );
    const isVoice = ['ptt', 'audio'].includes(
      String(normalized.message_type || '').toLowerCase(),
    );
    // The owner observation still cannot authorize actions. This opt-in
    // allows only proven manual direct-chat voice evidence to enter the same
    // HMAC-protected media/STT path used for inbound voice.
    const manualOwnerVoice = (
      fromMe && isVoice && normalized.has_media
      && config.conversationCaptureOwnerVoiceEnabled === true
      && normalized.event_origin === 'OWNER_MANUAL_OUTBOUND_OBSERVED'
      && normalized.owner_authenticated === false
      && normalized.metadata?.from_me_classification === 'OWNER_MANUAL_OUTBOUND_OBSERVED'
      && normalized.metadata?.final_from_me_classification === 'OWNER_MANUAL_OUTBOUND_OBSERVED'
      && normalized.metadata?.conversation_state === 'READY'
      && normalized.metadata?.is_group !== true
    );
    const shouldCaptureMedia = (
      (!fromMe && normalized.has_media
        && (isVoice || config.whatsappArtifactIngestionEnabled === true))
      || manualOwnerVoice
      || unattributedAccountMedia
    );
    const mediaPromise = shouldCaptureMedia
      ? captureInboundMedia(config, message, normalized, logger, fetchImpl)
      : Promise.resolve(null);
    const [delivery, media] = await Promise.all([deliveryPromise, mediaPromise]);
    if (delivery.done && (delivery.status === 'accepted' || delivery.status === 'duplicate')) {
      onCounters({ last_inbound_delivery_at: new Date().toISOString() });
      return { status: 'delivered', normalized, forwarded: true, delivery, media };
    }
    if (delivery.status === 'auth_failed') {
      onCounters({ last_inbound_error_at: new Date().toISOString() });
    }
    return { status: delivery.status, normalized, forwarded: false, delivery, media };
  }

  async function handleMessage(message, options = {}) {
    return observability.withSpan('transport.receive', null, async (receiveSpan) => {
      const result = await handleMessageOperation(message, options, receiveSpan);
      receiveSpan.setResult(tracingResult(result));
      return result;
    });
  }

  return {
    handleMessage,
  };
}

module.exports = {
  createInboundBridge,
  isLocalForwardTarget,
};
