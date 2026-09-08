const { normalizeInboundMessage } = require('./inbound');
const { enqueuePending, deliverPendingFile } = require('./spool');
const { captureVoiceMedia } = require('./media');

function isLocalForwardTarget(url, allowedHosts = ['127.0.0.1', 'localhost', '::1']) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'http:' && allowedHosts.includes(parsed.hostname);
  } catch {
    return false;
  }
}

function createInboundBridge(config, logger = console, deps = {}) {
  const fetchImpl = deps.fetchImpl || fetch;
  const onCounters = deps.onCounters || (() => {});

  async function handleMessage(message, options = {}) {
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
    const normalized = normalizeInboundMessage(message, {
      source: config.inboundSource,
      channel: config.inboundChannel,
      sourceAccount: config.sourceAccount,
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
    const wirePayload = Object.fromEntries(
      Object.entries(normalized).filter(([key]) => key !== 'identity'),
    );
    const pending = enqueuePending(config, normalized, wirePayload);
    const deliveryPromise = deliverPendingFile(config, pending.file, logger, fetchImpl);
    const mediaPromise = !fromMe
      && ['ptt', 'audio'].includes(normalized.message_type)
      && normalized.has_media
      ? captureVoiceMedia(config, message, normalized, logger, fetchImpl)
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

  return {
    handleMessage,
  };
}

module.exports = {
  createInboundBridge,
  isLocalForwardTarget,
};
