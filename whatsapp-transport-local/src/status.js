function nowIso() {
  return new Date().toISOString();
}

function createStatus(config, overrides = {}) {
  return {
    generated_at: nowIso(),
    source_revision: config.sourceRevision,
    service_state: 'starting',
    browser_debug_reachable: false,
    wwebjs_connected: false,
    authenticated_event_seen: false,
    authenticated_identity_match: config.ownerIdentityRef ? null : false,
    owner_identity_configured: Boolean(config.ownerIdentityRef),
    owner_identity_verification: config.ownerIdentityRef ? 'NOT_EVALUATED' : 'NOT_CONFIGURED',
    owner_identity_resolved: false,
    owner_self_identity_present: false,
    owner_command_authority_ready: false,
    owner_command_authority_reason: config.ownerIdentityRef ? 'NOT_VERIFIED' : 'NOT_CONFIGURED',
    owner_authority_last_invalidation_reason: null,
    owner_authority_last_invalidation_at: null,
    ready: false,
    client_state: 'UNKNOWN',
    inbound_forward_enabled: config.inboundForwardEnabled,
    external_delivery_enabled: config.externalDeliveryEnabled,
    inbound_seen_count: 0,
    inbound_spool_pending: 0,
    inbound_spool_sending: 0,
    inbound_spool_sent: 0,
    inbound_spool_quarantine: 0,
    qr_seen: false,
    disconnect_count: 0,
    last_ready_at: null,
    last_disconnect_at: null,
    last_inbound_seen_at: null,
    last_inbound_delivery_at: null,
    last_inbound_error_at: null,
    browser_pid: null,
    page_count: null,
    ...overrides,
  };
}

// Enumerable getters also protect direct status reads from silent WID/Page changes.
// The closure never becomes part of the public JSON.
function attachOwnerAuthorityStatus(status, config, describe) {
  const read = () => {
    const proof = describe();
    const configured = Boolean(config.ownerIdentityRef);
    const verification = configured ? proof.verification : 'NOT_CONFIGURED';
    let reason;
    if (!configured) reason = 'NOT_CONFIGURED';
    else if (status.service_state === 'blocked_page_selection') reason = 'PAGE_TOPOLOGY_INVALID';
    else if (verification === 'UNRESOLVED') reason = 'IDENTITY_UNRESOLVED';
    else if (verification !== 'MATCH') reason = 'NOT_VERIFIED';
    else if (!proof.resolved) reason = 'IDENTITY_UNRESOLVED';
    else if (!proof.identityPresent) reason = 'SELF_IDENTITY_UNAVAILABLE';
    else if (status.client_state !== 'CONNECTED') reason = 'CLIENT_NOT_CONNECTED';
    else if (!status.ready) reason = 'NOT_READY';
    else if (!proof.operational) reason = 'AUTHORITY_NOT_OPERATIONAL';
    else if (!proof.current) reason = 'AUTHORITY_STALE';
    else reason = 'READY';
    return {
      authenticated_identity_match: verification === 'NOT_EVALUATED' ? null : verification === 'MATCH',
      owner_identity_configured: configured,
      owner_identity_verification: verification,
      owner_identity_resolved: configured && proof.resolved,
      owner_self_identity_present: configured && proof.identityPresent,
      owner_command_authority_ready: reason === 'READY',
      owner_command_authority_reason: reason,
    };
  };
  for (const key of [
    'authenticated_identity_match', 'owner_identity_configured', 'owner_identity_verification',
    'owner_identity_resolved', 'owner_self_identity_present',
    'owner_command_authority_ready', 'owner_command_authority_reason',
  ]) {
    Object.defineProperty(status, key, { enumerable: true, configurable: true, get: () => read()[key] });
  }
}

function isReady(status) {
  return (
    status.service_state === 'ready'
    && status.browser_debug_reachable
    && status.wwebjs_connected
    && status.ready
    && status.client_state === 'CONNECTED'
  );
}

function snapshot(status) {
  return JSON.parse(JSON.stringify(status));
}

module.exports = {
  createStatus,
  attachOwnerAuthorityStatus,
  isReady,
  nowIso,
  snapshot,
};
