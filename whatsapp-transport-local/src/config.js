const { URL } = require('node:url');
const path = require('node:path');

function parseBoolean(value, fallback = false) {
  if (value === undefined || value === null || value === '') {
    return fallback;
  }
  if (typeof value === 'boolean') {
    return value;
  }
  const normalized = String(value).trim().toLowerCase();
  if (['1', 'true', 'yes', 'on'].includes(normalized)) {
    return true;
  }
  if (['0', 'false', 'no', 'off'].includes(normalized)) {
    return false;
  }
  return fallback;
}

function parseInteger(value, fallback) {
  if (value === undefined || value === null || value === '') {
    return fallback;
  }
  const parsed = Number.parseInt(String(value), 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function isLocalHost(hostname) {
  return hostname === '127.0.0.1' || hostname === 'localhost' || hostname === '::1';
}

function inboundForwardHosts(env) {
  return [
    '127.0.0.1',
    'localhost',
    '::1',
    env.LOCAL_INBOUND_FORWARD_ALLOWED_HOST || '192.0.2.6',
  ];
}

function normalizeInboundForwardUrl(env) {
  const explicitUrl = env.INBOUND_FORWARD_URL || env.LOCAL_INBOUND_FORWARD_URL;
  if (!explicitUrl) {
    return null;
  }
  try {
    const url = new URL(explicitUrl);
    if (url.protocol !== 'http:' || !inboundForwardHosts(env).includes(url.hostname)) {
      return null;
    }
    return url.toString().replace(/\/$/, '');
  } catch {
    return null;
  }
}

function normalizeBrowserDebugUrl(env) {
  const explicitUrl = env.BROWSER_DEBUG_URL || env.CHROME_REMOTE_DEBUGGING_URL;
  if (explicitUrl) {
    try {
      const url = new URL(explicitUrl);
      if (!isLocalHost(url.hostname)) {
        return null;
      }
      return url.toString().replace(/\/$/, '');
    } catch {
      return null;
    }
  }
  const port = parseInteger(env.BROWSER_DEBUG_PORT || env.CHROME_REMOTE_DEBUGGING_PORT, null);
  if (!port) {
    return null;
  }
  const host = env.BROWSER_DEBUG_HOST || env.CHROME_REMOTE_DEBUGGING_HOST || '127.0.0.1';
  if (!isLocalHost(host)) {
    return null;
  }
  return `http://${host}:${port}`;
}

function createConfig(env = process.env) {
  const browserURL = normalizeBrowserDebugUrl(env);
  const inboundSpoolDir = env.LOCAL_INBOUND_SPOOL_DIR || '';
  return {
    httpHost: env.LOCAL_TRANSPORT_HTTP_HOST || '127.0.0.1',
    httpPort: parseInteger(env.LOCAL_TRANSPORT_HTTP_PORT, 18103),
    browserDebugUrl: browserURL,
    inboundForwardEnabled: parseBoolean(env.INBOUND_FORWARD_ENABLED, false),
    inboundForwardUrl: normalizeInboundForwardUrl(env),
    inboundForwardHosts: inboundForwardHosts(env),
    externalDeliveryEnabled: parseBoolean(env.EXTERNAL_DELIVERY_ENABLED, false),
    hmacSecret: env.INTERNAL_INGRESS_HMAC_SECRET || '',
    outboundHmacSecret: env.LOCAL_OUTBOUND_HMAC_SECRET || env.INTERNAL_INGRESS_HMAC_SECRET || '',
    outboundMaxBodyBytes: parseInteger(env.LOCAL_OUTBOUND_MAX_BODY_BYTES, 16 * 1024),
    outboundPath: env.LOCAL_OUTBOUND_PATH || '/internal/send',
    statusPath: env.LOCAL_TRANSPORT_STATUS_PATH || '/status',
    livePath: env.LOCAL_TRANSPORT_LIVE_PATH || '/live',
    readyPath: env.LOCAL_TRANSPORT_READY_PATH || '/ready',
    serviceName: env.LOCAL_TRANSPORT_SERVICE_NAME || 'attention-router-local-whatsapp-transport',
    sourceRevision: env.ATTENTION_TRANSPORT_SOURCE_SHA || 'unknown',
    ownerIdentityRef: env.OWNER_WHATSAPP_IDENTITY_REF || null,
    inboundSpoolDir,
    inboundPendingDir: env.LOCAL_INBOUND_PENDING_DIR || '',
    inboundSendingDir: env.LOCAL_INBOUND_SENDING_DIR || '',
    inboundSentDir: env.LOCAL_INBOUND_SENT_DIR || '',
    inboundQuarantineDir: env.LOCAL_INBOUND_QUARANTINE_DIR || '',
    inboundHttpTimeoutMs: parseInteger(env.LOCAL_INBOUND_HTTP_TIMEOUT_MS, 5000),
    inboundMaxSkewSeconds: parseInteger(env.LOCAL_INBOUND_MAX_SKEW_SECONDS, 300),
    inboundSource: env.LOCAL_INBOUND_SOURCE || 'wwebjs',
    inboundChannel: env.LOCAL_INBOUND_CHANNEL || 'whatsapp',
    sourceAccount: env.LOCAL_SOURCE_ACCOUNT || 'default',
    mediaRoot: env.WHATSAPP_MEDIA_ROOT || '/var/lib/attention-router/whatsapp-media',
    mediaMaxBytes: parseInteger(env.WHATSAPP_MEDIA_MAX_BYTES, 5 * 1024 * 1024),
    mediaDownloadTimeoutMs: parseInteger(env.LOCAL_MEDIA_DOWNLOAD_TIMEOUT_MS, 15000),
    mediaNotificationUrl: env.LOCAL_MEDIA_NOTIFICATION_URL || (
      normalizeInboundForwardUrl(env)
        ? new URL('/internal/whatsapp/media', normalizeInboundForwardUrl(env)).toString()
        : null
    ),
    mediaNotificationPendingDir: env.LOCAL_MEDIA_NOTIFICATION_PENDING_DIR || (
      inboundSpoolDir ? path.join(inboundSpoolDir, 'media-notifications', 'pending') : ''
    ),
    mediaNotificationSendingDir: env.LOCAL_MEDIA_NOTIFICATION_SENDING_DIR || (
      inboundSpoolDir ? path.join(inboundSpoolDir, 'media-notifications', 'sending') : ''
    ),
    mediaNotificationSentDir: env.LOCAL_MEDIA_NOTIFICATION_SENT_DIR || (
      inboundSpoolDir ? path.join(inboundSpoolDir, 'media-notifications', 'sent') : ''
    ),
    mediaNotificationQuarantineDir: env.LOCAL_MEDIA_NOTIFICATION_QUARANTINE_DIR || (
      inboundSpoolDir ? path.join(inboundSpoolDir, 'media-notifications', 'quarantine') : ''
    ),
    outboundProvenanceDir: env.LOCAL_OUTBOUND_PROVENANCE_DIR || (
      inboundSpoolDir ? path.join(inboundSpoolDir, 'outbound-provenance') : ''
    ),
    outboundProvenanceCorrelationMs: parseInteger(
      env.LOCAL_OUTBOUND_PROVENANCE_CORRELATION_MS,
      120 * 1000,
    ),
    historyReadOnlyEnabled: parseBoolean(env.HISTORY_READ_ONLY_ENABLED, false),
    historyPath: env.LOCAL_HISTORY_PATH || '/internal/history/chats',
    historyHmacSecret: env.LOCAL_HISTORY_HMAC_SECRET || env.INTERNAL_INGRESS_HMAC_SECRET || '',
    historyMaxSkewSeconds: parseInteger(env.LOCAL_HISTORY_MAX_SKEW_SECONDS, 300),
    historyMaxPageSize: parseInteger(env.LOCAL_HISTORY_MAX_PAGE_SIZE, 100),
  };
}

module.exports = {
  createConfig,
  isLocalHost,
  normalizeBrowserDebugUrl,
  normalizeInboundForwardUrl,
  inboundForwardHosts,
  parseBoolean,
  parseInteger,
};
