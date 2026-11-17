// Optional routing/permission adapter. Same ingress, HMAC, spool and media engine.
const path = require('node:path');
const crypto = require('node:crypto');
const { headersForBody } = require('./hmac');

function canaryConfig(config) {
  if (config.semanticCanaryEnabled !== true && !config.semanticCanaryConversationKey && !config.semanticCanaryIngressUrl) return null;
  let url;
  try { url = new URL(config.semanticCanaryIngressUrl); } catch { throw new Error('CANARY_CONFIGURATION_INVALID'); }
  if (!config.tenantId || !config.sourceAccount
      || !/^wwebjs:[0-9]+@(lid|c\.us)$/.test(config.semanticCanaryConversationKey || '')
      || url.protocol !== 'http:' || !(config.inboundForwardHosts || []).includes(url.hostname)
      || url.username || url.password || url.search || url.hash) throw new Error('CANARY_CONFIGURATION_INVALID');
  if (config.firstContactEnabled === true && !config.semanticCanaryConversationKey.endsWith('@lid')) {
    throw new Error('CANARY_FIRST_CONTACT_REQUIRES_CANONICAL_LID');
  }
  const base = url.toString().replace(/\/$/, '');
  const selected = { ...config, semanticCanarySelected: true,
    inboundForwardUrl: `${base}/events`, mediaNotificationUrl: `${base}/media` };
  // Separate queues prevent a paused/disabled canary from replaying through
  // normal ingress and accidentally creating a response/decision queue.
  for (const key of ['inboundSpoolDir', 'inboundPendingDir', 'inboundSendingDir', 'inboundSentDir',
    'inboundQuarantineDir', 'mediaNotificationPendingDir', 'mediaNotificationSendingDir',
    'mediaNotificationSentDir', 'mediaNotificationQuarantineDir']) {
    if (!config[key]) throw new Error('CANARY_SPOOL_CONFIGURATION_REQUIRED');
    selected[key] = path.join(config[key], 'semantic-canary');
  }
  return selected;
}

function selectedConversation(config, event) {
  return Boolean(config.semanticCanaryConversationKey) && event.tenant_id === config.tenantId
    && event.metadata?.source_account === config.sourceAccount
    && event.metadata?.conversation_key === config.semanticCanaryConversationKey
    && event.metadata?.owner_self_chat !== true && event.owner_authenticated !== true;
}

async function authorizeCanaryMedia(config, payload, fetchImpl) {
  const body = Buffer.from(JSON.stringify(payload));
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), config.inboundHttpTimeoutMs);
  try {
    const response = await fetchImpl(config.inboundForwardUrl, { method: 'POST', body,
      headers: headersForBody({ secret: config.hmacSecret, body, now: Date.now }), signal: controller.signal });
    if (![200, 201, 202].includes(response.status)) return false;
    const value = JSON.parse(await response.text());
    return ['accepted', 'duplicate'].includes(value.status)
      && value.canary_media_allowed === true && value.grants_authority === false;
  } catch { return false; }
  finally { clearTimeout(timer); }
}

// Called only after the existing First Contact scope/identity verification.
// Selection survives feature-off: never reopen automated presentation on rollback.
function blocksFirstContact(config, scope) {
  const key = config.semanticCanaryConversationKey;
  if (!key) return config.semanticCanaryEnabled === true; // incomplete opt-in fails closed
  if (!/^wwebjs:[0-9]+@lid$/.test(key)) return true;
  return scope.tenant_id === config.tenantId && scope.source_account === config.sourceAccount
    && scope.canonical_peer_hash === crypto.createHash('sha256').update(key.slice(7)).digest('hex');
}

module.exports = { canaryConfig, selectedConversation, authorizeCanaryMedia, blocksFirstContact };
