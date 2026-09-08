// Shared synthetic identities preserve PN/JID equivalence and explicit alias order.
const { ACTOR_A, ACTOR_B } = require('../../tests/fixtures/synthetic-identities.json');
const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const path = require('node:path');

const {
  authenticatedIdentityMatches,
  authenticatedSelfIdentityFromVerification,
  createAuthenticatedSelfAuthorityState,
  detachTransport,
  resolveClientIdentity,
  startTransport,
  verifyConfiguredOwner,
  verifyCurrentOwnerAuthority,
  OWNER_AUTHORITY_REVERIFY_INTERVAL_MS,
} = require('../src/transport');
const { createStatus, isReady, snapshot: statusSnapshot } = require('../src/status');
const { createConfig } = require('../src/config');
const { createInboundBridge } = require('../src/bridge');
const { createServer } = require('../src/server');

const OWNER_PN = ACTOR_A.jid;
const OWNER_LID = ACTOR_A.lid;

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

async function flushMicrotasks() {
  for (let index = 0; index < 50; index += 1) await Promise.resolve();
}

function validVerification(lid = OWNER_LID) {
  return {
    result: 'MATCH',
    expectedPhone: ACTOR_A.e164,
    actualPhone: ACTOR_A.e164,
    identity: {
      result: 'RESOLVED',
      wid: OWNER_PN,
      pn: OWNER_PN,
      lid,
      aliases: [OWNER_PN, lid].sort(),
      canonicalPhone: ACTOR_A.e164,
    },
  };
}

function ownerSelfChat(lid = OWNER_LID) {
  return {
    id: { _serialized: `self-chat-${lid}`, remote: lid },
    fromMe: true,
    from: OWNER_PN,
    to: lid,
    body: 'status',
    type: 'chat',
  };
}

class FakePage extends EventEmitter {
  constructor() { super(); this.frame = {}; }
  mainFrame() { return this.frame; }
  url() { return 'https://web.whatsapp.com/'; }
  async evaluate() { return { state: 'CONNECTED', detectionSource: 'WA_WEB_SOCKET_MODEL' }; }
}

class FakeBrowser extends EventEmitter {
  async pages() { return []; }
  process() { return null; }
}

async function transportAuthorityHarness(options = {}) {
  const page = new FakePage();
  let clientInstance;
  class FakeClient extends EventEmitter {
    constructor() {
      super();
      this.info = { wid: OWNER_PN };
      this.pupPage = page;
      this.pupBrowser = new FakeBrowser();
      this.pupBrowser.pages = async () => [this.pupPage];
      clientInstance = this;
    }

    async getState() { return options.getState ? options.getState() : 'CONNECTED'; }

    async initialize() {}
  }
  const verificationCalls = [];
  const logs = [];
  const normalized = [];
  const verifyOwner = () => {
    const call = deferred();
    verificationCalls.push(call);
    return call.promise;
  };
  const logger = {
    error(event, data) { logs.push({ data, event, level: 'error' }); },
    info(event, data) {
      logs.push({ data, event, level: 'info' });
      options.onLog?.(event, data, clientInstance);
    },
    warn(event, data) { logs.push({ data, event, level: 'warn' }); },
  };
  const config = createConfig({
    BROWSER_DEBUG_URL: 'http://127.0.0.1:9222',
    INBOUND_FORWARD_ENABLED: 'false',
    OWNER_WHATSAPP_IDENTITY_REF: options.ownerIdentityRef === null ? undefined : OWNER_PN,
  });
  const transport = await startTransport(config, logger, {
    createInboundBridge: (...args) => {
      const bridge = createInboundBridge(...args);
      return { handleMessage: async (...call) => {
        const result = await bridge.handleMessage(...call);
        normalized.push(result.normalized);
        return result;
      } };
    },
    Client: FakeClient,
    outboundProvenance: {
      classify: options.classify || (() => ({
        classification: 'OWNER_MANUAL_OUTBOUND_OBSERVED',
        provenance: null,
        reason: 'NO_ROUTER_OUTBOUND_MATCH',
      })),
    },
    prepareAuthenticatedPage: async () => ({
      result: 'SELECTED', page,
      broker: { consumed: () => true, restore() {} }, dispose() {},
    }),
    probeBrowserDebugUrl: async () => true,
    recoverConnectedPage: async () => false,
    verifyConfiguredOwner: verifyOwner,
    ownerAuthorityReverifyIntervalMs: options.ownerAuthorityReverifyIntervalMs,
    ownerAuthorityReverifyTimeoutMs: options.ownerAuthorityReverifyTimeoutMs,
    setInterval: options.setInterval,
    clearInterval: options.clearInterval,
  });
  return {
    client: clientInstance,
    ensureOwnerAuthorityCurrent: transport.ensureOwnerAuthorityCurrent,
    logs,
    normalized,
    status: transport.status,
    verificationCalls,
  };
}

async function classifyObservedMessage(harness, message) {
  const start = harness.logs.length;
  harness.client.emit('message_create', message);
  await flushMicrotasks();
  return harness.logs.slice(start).find((entry) => entry.event === 'from_me_classified')?.data;
}

async function readyStatusHarness() {
  const h = await transportAuthorityHarness();
  h.client.emit('ready');
  await flushMicrotasks();
  h.verificationCalls[0].resolve(validVerification());
  await flushMicrotasks();
  assert.equal(h.status.owner_command_authority_ready, true);
  assert.equal(h.status.owner_command_authority_reason, 'READY');
  return h;
}

test('missing owner configuration is never MATCH and does not authorize commands', async () => {
  const result = await verifyConfiguredOwner(createConfig({}), {});
  assert.deepEqual(result, { result: 'NOT_CONFIGURED', identity: null });
  assert.equal(authenticatedIdentityMatches(createConfig({}), {}), false);
  const h = await transportAuthorityHarness({ ownerIdentityRef: null });
  h.client.emit('ready');
  await flushMicrotasks();
  assert.equal(h.status.owner_identity_configured, false);
  assert.equal(h.status.owner_identity_verification, 'NOT_CONFIGURED');
  assert.equal(h.status.authenticated_identity_match, false);
  assert.equal(h.status.owner_self_identity_present, false);
  assert.equal(h.status.owner_command_authority_ready, false);
  assert.equal(h.status.owner_command_authority_reason, 'NOT_CONFIGURED');
  assert.equal(isReady(h.status), true); // Normal transport traffic remains available.
});

test('verified identity is observable before READY without operational authority', async () => {
  const h = await transportAuthorityHarness();
  assert.equal(h.status.owner_identity_verification, 'NOT_EVALUATED');
  h.client.emit('authenticated');
  h.verificationCalls[0].resolve(validVerification());
  await flushMicrotasks();
  assert.equal(h.status.owner_identity_configured, true);
  assert.equal(h.status.owner_identity_verification, 'MATCH');
  assert.equal(h.status.owner_identity_resolved, true);
  assert.equal(h.status.owner_self_identity_present, true);
  assert.equal(h.status.owner_command_authority_ready, false);
});

for (const result of ['MISMATCH', 'UNRESOLVED', 'ERROR']) {
  test(`verification ${result} is observable without a false identity match`, async () => {
    const h = await transportAuthorityHarness();
    h.client.emit('ready');
    await flushMicrotasks();
    if (result === 'ERROR') h.verificationCalls[0].reject(new Error('verification failed'));
    else h.verificationCalls[0].resolve({ result, identity: { result: 'UNRESOLVED' } });
    await flushMicrotasks();
    assert.equal(h.status.owner_identity_verification, result);
    assert.equal(h.status.authenticated_identity_match, false);
    assert.equal(h.status.owner_identity_resolved, false);
    assert.equal(h.status.owner_command_authority_ready, false);
  });
}

for (const [transition, expectedReason] of Object.entries({
  navigation: 'MAIN_FRAME_NAVIGATION',
  close: 'PAGE_CLOSED',
  browser_disconnect: 'BROWSER_DISCONNECTED',
  qr: 'QR',
  auth_failure: 'AUTH_FAILURE',
  disconnected: 'STRONG_CLIENT_STATE',
  strong_state: 'STRONG_CLIENT_STATE',
  detach: 'DETACHED',
})) {
  test(`${transition} immediately removes public owner authority`, async () => {
    const h = await readyStatusHarness();
    if (transition === 'navigation') h.client.pupPage.emit('framenavigated', h.client.pupPage.mainFrame());
    else if (transition === 'close') h.client.pupPage.emit('close');
    else if (transition === 'browser_disconnect') h.client.pupBrowser.emit('disconnected');
    else if (transition === 'strong_state') h.client.emit('change_state', 'UNPAIRED');
    else if (transition === 'detach') await detachTransport(h.client);
    else h.client.emit(transition);
    assert.equal(h.status.owner_command_authority_ready, false);
    assert.equal(h.status.owner_self_identity_present, false);
    assert.equal(h.status.owner_identity_resolved, false);
    assert.notEqual(h.status.authenticated_identity_match, true);
    assert.equal(h.status.owner_authority_last_invalidation_reason, expectedReason);
    assert.match(h.status.owner_authority_last_invalidation_at, /^\d{4}-\d{2}-\d{2}T/);
    h.client.emit('change_state', 'CONNECTED');
    assert.equal(h.status.owner_command_authority_ready, false);
  });
}

for (const state of ['OPENING', 'TIMEOUT']) {
  test(`${state} suspends public authority and same-proof CONNECTED restores it`, async () => {
    const h = await readyStatusHarness();
    h.client.emit('change_state', state);
    assert.equal(h.status.owner_self_identity_present, true);
    assert.equal(h.status.owner_command_authority_ready, false);
    h.client.emit('change_state', 'CONNECTED');
    assert.equal(h.status.owner_command_authority_ready, true);
    assert.equal(h.status.owner_command_authority_reason, 'READY');
  });
}

test('public status checks the real snapshot even if transport readiness remains green', async () => {
  const h = await readyStatusHarness();
  h.client.info.wid = '99000000016@c.us';
  assert.equal(h.status.ready, true);
  assert.equal(h.status.owner_command_authority_ready, false);
  assert.equal(h.status.owner_self_identity_present, false);
  assert.notEqual(h.status.authenticated_identity_match, true);
});

test('READY topology rejection blocks public authority without closing pages', async () => {
  const h = await transportAuthorityHarness();
  h.client.pupBrowser.pages = async () => [h.client.pupPage, new FakePage()];
  h.client.emit('ready');
  await flushMicrotasks();
  assert.equal(h.status.owner_command_authority_ready, false);
  assert.equal(h.status.owner_command_authority_reason, 'PAGE_TOPOLOGY_INVALID');
});

test('public status never serializes identity values, aliases or private authority objects', async () => {
  const h = await readyStatusHarness();
  const serialized = JSON.stringify(statusSnapshot(h.status));
  for (const forbidden of [OWNER_PN, OWNER_LID, validVerification().identity.canonicalPhone,
    'canonicalPhone', 'aliases', 'ownerIdentityRef', 'pupPage', 'pupBrowser']) {
    assert.equal(serialized.includes(forbidden), false, 'private identity data must not be serialized');
  }
  assert.equal(JSON.parse(serialized).owner_command_authority_ready, true);
});

test('new verification hides old authority until its current READY proof completes', async () => {
  const h = await readyStatusHarness();
  h.client.emit('ready');
  await flushMicrotasks();
  assert.equal(h.status.owner_identity_verification, 'NOT_EVALUATED');
  assert.equal(h.status.owner_self_identity_present, false);
  assert.equal(h.status.owner_command_authority_ready, false);
  assert.equal(h.status.owner_authority_last_invalidation_reason, 'PROOF_STALE');
  h.verificationCalls[1].resolve(validVerification());
  await flushMicrotasks();
  assert.equal(h.status.owner_command_authority_ready, true);
});

test('new READY restores authority status after strong invalidation', async () => {
  const h = await readyStatusHarness();
  h.client.emit('change_state', 'UNPAIRED');
  h.client.emit('change_state', 'CONNECTED');
  assert.equal(h.status.owner_command_authority_ready, false);
  h.client.emit('ready');
  await flushMicrotasks();
  h.verificationCalls.at(-1).resolve(validVerification());
  await flushMicrotasks();
  assert.equal(h.status.owner_command_authority_ready, true);
});

test('MATCH with resolved WID but unavailable authoritative PN exposes missing self identity', async () => {
  const h = await transportAuthorityHarness();
  h.client.emit('ready');
  await flushMicrotasks();
  const verification = validVerification();
  verification.identity.pn = null;
  h.verificationCalls[0].resolve(verification);
  await flushMicrotasks();
  assert.equal(h.status.owner_identity_verification, 'MATCH');
  assert.equal(h.status.owner_identity_resolved, true);
  assert.equal(h.status.owner_self_identity_present, false);
  assert.equal(h.status.owner_command_authority_reason, 'SELF_IDENTITY_UNAVAILABLE');
  assert.equal(h.status.owner_command_authority_ready, false);
  h.client.info.wid = '99000000016@c.us';
  assert.equal(h.status.owner_identity_verification, 'NOT_EVALUATED');
  assert.equal(h.status.owner_identity_resolved, false);
});

test('late verification after navigation cannot republish green public status', async () => {
  const h = await transportAuthorityHarness();
  h.client.emit('ready');
  await flushMicrotasks();
  h.client.pupPage.emit('framenavigated', h.client.pupPage.mainFrame());
  h.verificationCalls[0].resolve(validVerification());
  await flushMicrotasks();
  assert.equal(h.status.owner_identity_verification, 'NOT_EVALUATED');
  assert.equal(h.status.owner_self_identity_present, false);
  assert.equal(h.status.owner_command_authority_ready, false);
});

test('HTTP status serves current sanitized authority, not a cached ready flag', async (t) => {
  const h = await readyStatusHarness();
  const server = createServer(createConfig({}), h.status);
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => {
    server.closeAllConnections();
    return new Promise((resolve) => server.close(resolve));
  });
  const url = `http://127.0.0.1:${server.address().port}/status`;
  const before = await (await fetch(url)).json();
  assert.equal(before.owner_command_authority_ready, true);
  assert.equal(before.owner_command_authority_reason, 'READY');
  h.client.pupPage.emit('close');
  const after = await (await fetch(url)).json();
  assert.equal(after.owner_command_authority_ready, false);
  assert.equal(after.owner_self_identity_present, false);
  for (const body of [before, after]) {
    for (const forbidden of [OWNER_PN, OWNER_LID, 'canonicalPhone', 'aliases', 'ownerIdentityRef']) {
      assert.equal(JSON.stringify(body).includes(forbidden), false, 'HTTP status must not leak identity');
    }
  }
});

test('whatsapp-web.js destroy closes the shared browser in the installed version', () => {
  const clientSource = fs.readFileSync(
    path.join(__dirname, '..', 'node_modules', 'whatsapp-web.js', 'src', 'Client.js'),
    'utf8',
  );
  assert.match(clientSource, /async destroy\(\)[\s\S]*browser\.close\(\)/);
  assert.doesNotMatch(clientSource, /async destroy\(\)[\s\S]*browser\.disconnect\(\)/);
});

test('detachTransport never closes the shared browser', async () => {
  let pageCloseCalls = 0;
  let browserCloseCalls = 0;
  let browserDisconnectCalls = 0;
  let listenersRemoved = false;

  const client = {
    pupPage: {
      isClosed: () => false,
      close: async () => {
        pageCloseCalls += 1;
      },
    },
    pupBrowser: {
      isConnected: () => true,
      disconnect: () => {
        browserDisconnectCalls += 1;
      },
      close: async () => {
        browserCloseCalls += 1;
      },
    },
    removeAllListeners: () => {
      listenersRemoved = true;
    },
  };

  await detachTransport(client, { warn() {} });

  assert.equal(pageCloseCalls, 0);
  assert.equal(browserDisconnectCalls, 1);
  assert.equal(browserCloseCalls, 0);
  assert.equal(listenersRemoved, false);
});

test('ready requires connected client state', () => {
  const config = createConfig({});
  assert.equal(
    isReady(
      createStatus(config, {
        service_state: 'ready',
        browser_debug_reachable: true,
        wwebjs_connected: true,
        ready: true,
        client_state: 'CONNECTED',
      }),
    ),
    true,
  );
  assert.equal(
    isReady(
      createStatus(config, {
        service_state: 'ready',
        browser_debug_reachable: true,
        wwebjs_connected: true,
        ready: true,
        client_state: 'UNKNOWN',
      }),
    ),
    false,
  );
});

test('configured owner identity is checked against authenticated transport identity', () => {
  const config = createConfig({ OWNER_WHATSAPP_IDENTITY_REF: ACTOR_A.nationalJid });
  assert.equal(authenticatedIdentityMatches(config, { wid: { _serialized: ACTOR_A.jid } }), true);
  assert.equal(authenticatedIdentityMatches(config, { wid: { _serialized: ACTOR_B.jid } }), false);
});

test('client identity captures exact authoritative PN and LID from the authenticated page', async () => {
  const client = {
    info: { wid: { _serialized: ACTOR_A.jid } },
    pupPage: {
      evaluate: async () => ({
        pn: ACTOR_A.jid,
        lid: ACTOR_A.lid,
      }),
    },
  };

  const identity = await resolveClientIdentity(client);
  assert.equal(identity.result, 'RESOLVED');
  assert.equal(identity.wid, ACTOR_A.jid);
  assert.equal(identity.pn, ACTOR_A.jid);
  assert.equal(identity.lid, ACTOR_A.lid);
  assert.deepEqual(identity.aliases, [ACTOR_A.lid, ACTOR_A.jid]);
});

test('owner mismatch cannot produce an authorized authenticated self identity', async () => {
  const config = createConfig({ OWNER_WHATSAPP_IDENTITY_REF: ACTOR_A.nationalJid });
  const client = {
    info: { wid: { _serialized: ACTOR_B.jid } },
    pupPage: {
      evaluate: async () => ({
        pn: ACTOR_B.jid,
        lid: '9900000040@lid',
      }),
    },
  };

  const verification = await verifyConfiguredOwner(config, client);
  assert.equal(verification.result, 'MISMATCH');
  assert.equal(authenticatedSelfIdentityFromVerification(verification), null);
});

test('only a configured owner match authorizes the captured self aliases', async () => {
  const config = createConfig({ OWNER_WHATSAPP_IDENTITY_REF: ACTOR_A.nationalJid });
  const client = {
    info: { wid: { _serialized: ACTOR_A.jid } },
    pupPage: {
      evaluate: async () => ({
        pn: ACTOR_A.jid,
        lid: ACTOR_A.lid,
      }),
    },
  };

  const verification = await verifyConfiguredOwner(config, client);
  const authenticatedSelfIdentity = authenticatedSelfIdentityFromVerification(verification);
  assert.equal(verification.result, 'MATCH');
  assert.equal(authenticatedSelfIdentity.ownerVerified, true);
  assert.deepEqual(authenticatedSelfIdentity.aliases, [ACTOR_A.lid, ACTOR_A.jid]);
  assert.equal(authenticatedSelfIdentityFromVerification({ result: 'MATCH', identity: null }), null);
});

test('owner match without an authoritative page PN remains unauthorized', async () => {
  const config = createConfig({ OWNER_WHATSAPP_IDENTITY_REF: ACTOR_A.nationalJid });
  const client = {
    info: { wid: { _serialized: ACTOR_A.jid } },
    pupPage: {
      evaluate: async () => ({
        pn: null,
        lid: ACTOR_A.lid,
      }),
    },
  };

  const verification = await verifyConfiguredOwner(config, client);
  assert.equal(verification.result, 'MATCH');
  assert.equal(authenticatedSelfIdentityFromVerification(verification), null);
});

for (const scenario of [
  {
    name: 'late authenticated verification is discarded after disconnected',
    phase: 'authenticated',
    invalidate: (client) => client.emit('disconnected', 'LOGOUT'),
    expectedServiceState: 'disconnected',
  },
  {
    name: 'late ready verification is discarded after disconnected',
    phase: 'ready',
    invalidate: (client) => client.emit('disconnected', 'LOGOUT'),
    expectedServiceState: 'disconnected',
  },
  {
    name: 'late authenticated verification is discarded after auth failure',
    phase: 'authenticated',
    invalidate: (client) => client.emit('auth_failure', 'fixture failure'),
    expectedServiceState: 'auth_failure',
  },
  {
    name: 'late ready verification is discarded after session-not-authenticated state',
    phase: 'ready',
    invalidate: (client) => client.emit('change_state', 'SESSION_NOT_AUTHENTICATED'),
    expectedServiceState: 'session_not_authenticated',
  },
  {
    name: 'late authenticated verification is discarded after QR invalidation',
    phase: 'authenticated',
    invalidate: (client) => client.emit('qr', 'fixture qr'),
    expectedServiceState: 'session_not_authenticated',
  },
  {
    name: 'late ready verification is discarded after unpaired state',
    phase: 'ready',
    invalidate: (client) => client.emit('change_state', 'UNPAIRED'),
    expectedServiceState: 'session_not_authenticated',
  },
]) {
  test(scenario.name, async () => {
    const harness = await transportAuthorityHarness();
    harness.client.emit(scenario.phase);
    if (scenario.phase === 'ready') await flushMicrotasks();
    assert.equal(harness.verificationCalls.length, 1);
    scenario.invalidate(harness.client);
    harness.verificationCalls[0].resolve(validVerification());
    await flushMicrotasks();

    assert.equal(harness.status.service_state, scenario.expectedServiceState);
    assert.equal(harness.status.ready, false);
    assert.equal(harness.status.authenticated_identity_match, null);
    const classification = await classifyObservedMessage(harness, ownerSelfChat());
    assert.notEqual(classification.classification, 'OWNER_COMMAND');
  });
}

test('a newer verification makes an older pending verification stale', async () => {
  const authorityState = createAuthenticatedSelfAuthorityState();
  const client = { info: { wid: OWNER_PN }, pupPage: {} };
  const first = deferred();
  const second = deferred();
  const pendingFirst = verifyCurrentOwnerAuthority({}, client, authorityState, () => first.promise);
  const pendingSecond = verifyCurrentOwnerAuthority({}, client, authorityState, () => second.promise);

  first.resolve(validVerification('9900000015@lid'));
  const firstResult = await pendingFirst;
  assert.equal(firstResult.current, false);
  assert.equal(firstResult.stale, true);
  assert.equal(authorityState.snapshot(), null);

  second.resolve(validVerification('9900000018@lid'));
  const secondResult = await pendingSecond;
  assert.equal(secondResult.current, true);
  assert.deepEqual(authorityState.snapshot().identity.aliases, [OWNER_PN, '9900000018@lid'].sort());
});

test('older verification cannot overwrite a newer committed authority', async () => {
  const authorityState = createAuthenticatedSelfAuthorityState();
  const client = { info: { wid: OWNER_PN }, pupPage: new FakePage() };
  const first = deferred();
  const second = deferred();
  const pendingFirst = verifyCurrentOwnerAuthority({}, client, authorityState, () => first.promise);
  const pendingSecond = verifyCurrentOwnerAuthority({}, client, authorityState, () => second.promise);

  second.resolve(validVerification('9900000018@lid'));
  const secondResult = await pendingSecond;
  assert.equal(secondResult.current, true);
  authorityState.markReadyConnected(secondResult.token, client);
  const committed = authorityState.snapshot();
  first.resolve(validVerification('9900000015@lid'));
  assert.equal((await pendingFirst).current, false);

  assert.equal(authorityState.isSnapshotCurrent(committed), true);
  assert.deepEqual(authorityState.snapshot().identity.aliases, [OWNER_PN, '9900000018@lid'].sort());
});

test('newer ready callback remains authoritative after older authenticated callback finishes', async () => {
  const harness = await transportAuthorityHarness();
  harness.client.emit('authenticated');
  harness.client.emit('ready');
  await flushMicrotasks();
  assert.equal(harness.verificationCalls.length, 2);

  harness.verificationCalls[1].resolve(validVerification('9900000018@lid'));
  await flushMicrotasks();
  assert.equal(harness.status.service_state, 'ready');
  harness.verificationCalls[0].resolve(validVerification('9900000015@lid'));
  await flushMicrotasks();

  assert.equal(harness.status.service_state, 'ready');
  assert.equal(harness.status.authenticated_identity_match, true);
  assert.equal(
    (await classifyObservedMessage(harness, ownerSelfChat('9900000018@lid'))).classification,
    'OWNER_COMMAND',
  );
  assert.notEqual(
    (await classifyObservedMessage(harness, ownerSelfChat('9900000015@lid'))).classification,
    'OWNER_COMMAND',
  );
});

test('page change during owner verification fails closed', async () => {
  const authorityState = createAuthenticatedSelfAuthorityState();
  const client = { info: { wid: OWNER_PN }, pupPage: { name: 'page-a' } };
  const verification = deferred();
  const pending = verifyCurrentOwnerAuthority({}, client, authorityState, () => verification.promise);
  client.pupPage = { name: 'page-b' };
  verification.resolve(validVerification());

  const result = await pending;
  assert.equal(result.current, false);
  assert.equal(result.stale, true);
  assert.ok(authorityState.generation() >= 2);
  assert.equal(authorityState.snapshot(), null);
});

test('client wid change during owner verification fails closed', async () => {
  const authorityState = createAuthenticatedSelfAuthorityState();
  const client = { info: { wid: OWNER_PN }, pupPage: {} };
  const verification = deferred();
  const pending = verifyCurrentOwnerAuthority({}, client, authorityState, () => verification.promise);
  client.info.wid = ACTOR_B.jid;
  verification.resolve(validVerification());

  const result = await pending;
  assert.equal(result.current, false);
  assert.equal(result.stale, true);
  assert.ok(authorityState.generation() >= 2);
  assert.equal(authorityState.snapshot(), null);
});

test('committed authority snapshot remains bound to its page and client wid', async () => {
  for (const changeClient of [
    (client) => { client.pupPage = { name: 'page-b' }; },
    (client) => { client.info.wid = ACTOR_B.jid; },
  ]) {
    const authorityState = createAuthenticatedSelfAuthorityState();
    const client = { info: { wid: OWNER_PN }, pupPage: new FakePage() };
    const result = await verifyCurrentOwnerAuthority(
      {},
      client,
      authorityState,
      async () => validVerification(),
    );
    assert.equal(result.current, true);
    authorityState.markReadyConnected(result.token, client);
    const committed = authorityState.snapshot(client);
    assert.equal(authorityState.isSnapshotCurrent(committed, client), true);

    changeClient(client);

    assert.equal(authorityState.isSnapshotCurrent(committed, client), false);
    assert.equal(authorityState.snapshot(client), null);
  }
});

test('a valid reconnect generation can authorize a new exact self-chat', async () => {
  const harness = await transportAuthorityHarness();
  harness.client.emit('ready');
  await flushMicrotasks();
  harness.verificationCalls[0].resolve(validVerification());
  await flushMicrotasks();
  assert.equal((await classifyObservedMessage(harness, ownerSelfChat())).classification, 'OWNER_COMMAND');

  harness.client.emit('disconnected', 'LOGOUT');
  assert.equal(harness.status.authenticated_identity_match, null);
  harness.client.pupPage = new FakePage();
  harness.client.emit('ready');
  await flushMicrotasks();
  harness.verificationCalls[1].resolve(validVerification('9900000018@lid'));
  await flushMicrotasks();

  assert.equal(harness.status.service_state, 'ready');
  assert.equal(
    (await classifyObservedMessage(harness, ownerSelfChat('9900000018@lid'))).classification,
    'OWNER_COMMAND',
  );
});

test('qr transitions to session not authenticated in transport status model', () => {
  const config = createConfig({});
  const status = createStatus(config);
  status.qr_seen = true;
  status.ready = false;
  status.wwebjs_connected = false;
  status.service_state = 'session_not_authenticated';
  status.client_state = 'SESSION_NOT_AUTHENTICATED';

  assert.equal(status.qr_seen, true);
  assert.equal(status.ready, false);
  assert.equal(status.service_state, 'session_not_authenticated');
  assert.equal(status.client_state, 'SESSION_NOT_AUTHENTICATED');
});

test('outbound path is explicit and remains capability-gated', () => {
  const files = [
    path.join(__dirname, '..', 'src', 'index.js'),
    path.join(__dirname, '..', 'src', 'server.js'),
    path.join(__dirname, '..', 'src', 'status.js'),
    path.join(__dirname, '..', 'src', 'transport.js'),
    path.join(__dirname, '..', 'src', 'bridge.js'),
    path.join(__dirname, '..', 'src', 'spool.js'),
    path.join(__dirname, '..', 'src', 'hmac.js'),
    path.join(__dirname, '..', 'src', 'inbound.js'),
    path.join(__dirname, '..', 'src', 'message-id.js'),
  ];
  const forbidden = ['reply(', 'forward(', 'process_outbox', 'dispatch_outbox'];
  const source = files.map((file) => fs.readFileSync(file, 'utf8')).join('\n');
  for (const token of forbidden) {
    assert.doesNotMatch(source, new RegExp(token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  }
});

test('message_create remains the only operational event seam', () => {
  const transportSource = fs.readFileSync(
    path.join(__dirname, '..', 'src', 'transport.js'),
    'utf8',
  );
  assert.equal((transportSource.match(/onClient\('message_create'/g) || []).length, 1);
  assert.doesNotMatch(transportSource, /WAWebCollections|Msg\.on\(/);
});

async function completeReady(harness) {
  const before = harness.verificationCalls.length;
  harness.client.emit('ready');
  await flushMicrotasks();
  if (harness.verificationCalls.length > before) {
    harness.verificationCalls.at(-1).resolve(validVerification());
  }
  await flushMicrotasks();
}

function assertNoOwnerPayload(payload) {
  assert.ok(payload);
  assert.notEqual(payload.event_origin, 'OWNER_COMMAND');
  assert.equal(payload.owner_authenticated, false);
  assert.equal(payload.metadata.owner_self_chat, false);
  assert.equal(payload.metadata.authenticated_owner_self_chat_target, false);
  assert.notEqual(payload.metadata.final_from_me_classification, 'OWNER_COMMAND');
}

test('authenticated identity alone never enables pre-ready Owner Command', async () => {
  const h = await transportAuthorityHarness();
  h.client.emit('authenticated');
  h.verificationCalls[0].resolve(validVerification());
  await flushMicrotasks();
  assert.equal(h.status.authenticated_identity_match, true);
  assert.equal(h.status.ready, false);
  assert.equal((await classifyObservedMessage(h, ownerSelfChat())).classification, 'UNKNOWN_FROM_ME');
  assertNoOwnerPayload(h.normalized.at(-1));
});

test('bounded reverify waits for current CONNECTED state before replacing proof', async () => {
  const state = deferred();
  let stateCalls = 0;
  const h = await transportAuthorityHarness({ getState: () => {
    stateCalls += 1;
    return stateCalls <= 2 ? 'CONNECTED' : state.promise;
  } });
  await completeReady(h);
  assert.equal(h.status.ready, true);
  h.client.emit('ready');
  await flushMicrotasks();
  assert.equal(h.status.ready, true);
  state.resolve('CONNECTED');
  await flushMicrotasks();
  h.verificationCalls.at(-1).resolve(validVerification());
  await flushMicrotasks();
  assert.equal((await classifyObservedMessage(h, ownerSelfChat())).classification, 'OWNER_COMMAND');
});

for (const state of ['CONNECTED', 'DISCONNECTED', 'UNPAIRED', 'UNPAIRED_IDLE',
  'SESSION_NOT_AUTHENTICATED', 'PAIRING', 'CONFLICT', 'OPENING', 'TIMEOUT',
  'UNKNOWN', null, {}, 'PROXYBLOCK', 'TOS_BLOCK', 'SMB_TOS_BLOCK', 'DEPRECATED_VERSION', 'UNLAUNCHED']) {
  test(`queried state ${JSON.stringify(state)} uses exact CONNECTED authority gate`, async () => {
    const h = await transportAuthorityHarness({ getState: () => state });
    await completeReady(h);
    const result = await classifyObservedMessage(h, ownerSelfChat());
    assert.equal(h.status.ready, state === 'CONNECTED');
    if (state === 'CONNECTED') {
      assert.equal(result.classification, 'OWNER_COMMAND');
    } else {
      assert.notEqual(result.classification, 'OWNER_COMMAND');
      assertNoOwnerPayload(h.normalized.at(-1));
      h.client.emit('change_state', 'CONNECTED');
      assert.equal(h.status.ready, false); // No previous READY proof in this generation.
      assert.notEqual((await classifyObservedMessage(h, ownerSelfChat())).classification, 'OWNER_COMMAND');
      if (!['OPENING', 'TIMEOUT', 'UNKNOWN', null].includes(state) && typeof state === 'string') {
        assert.equal(h.status.authenticated_identity_match, null);
      }
    }
  });
}

for (const mode of ['throw', 'reject']) {
  test(`getState ${mode} fails closed`, async () => {
    const h = await transportAuthorityHarness({ getState: () => {
      if (mode === 'throw') throw new Error('synthetic getState failure');
      return Promise.reject(new Error('synthetic getState failure'));
    } });
    await completeReady(h);
    assert.equal(h.status.ready, false);
    await classifyObservedMessage(h, ownerSelfChat());
    assertNoOwnerPayload(h.normalized.at(-1));
  });
}

for (const phase of ['authenticated', 'ready']) {
  test(`${phase} rechecks token after helper resolves before callback writes`, async () => {
    const h = await transportAuthorityHarness();
    h.client.emit(phase);
    if (phase === 'ready') await flushMicrotasks();
    h.verificationCalls[0].promise.then(() => h.client.emit('disconnected', 'LOGOUT'));
    h.verificationCalls[0].resolve(validVerification());
    await flushMicrotasks();
    assert.equal(h.status.service_state, 'disconnected');
    assert.equal(h.status.ready, false);
    assert.equal(h.status.authenticated_identity_match, null);
    await classifyObservedMessage(h, ownerSelfChat());
    assertNoOwnerPayload(h.normalized.at(-1));
  });
}

test('main frame reload invalidates same Page/WID, subframe preserves authority', async () => {
  const h = await transportAuthorityHarness();
  await completeReady(h);
  const page = h.client.pupPage;
  page.emit('framenavigated', {});
  assert.equal((await classifyObservedMessage(h, ownerSelfChat())).classification, 'OWNER_COMMAND');
  page.emit('framenavigated', page.mainFrame());
  assert.equal(h.client.pupPage, page);
  assert.equal(h.client.info.wid, OWNER_PN);
  assert.equal(h.status.ready, false);
  assert.equal(h.status.authenticated_identity_match, null);
  const logStart = h.logs.length;
  h.client.emit('message_create', ownerSelfChat());
  await flushMicrotasks();
  h.verificationCalls.at(-1).resolve(validVerification());
  await flushMicrotasks();
  assert.equal(h.logs.slice(logStart).find(e => e.event === 'from_me_classified').data.classification, 'OWNER_COMMAND');
  assert.equal((await classifyObservedMessage(h, ownerSelfChat())).classification, 'OWNER_COMMAND');
});

test('page lifecycle binding is unique and removes only owned listeners on replacement/dispose', async () => {
  const state = createAuthenticatedSelfAuthorityState();
  const page = new FakePage();
  const foreign = () => {};
  page.on('framenavigated', foreign);
  const client = { pupPage: page, info: { wid: OWNER_PN } };
  const result = await verifyCurrentOwnerAuthority({}, client, state, async () => validVerification());
  state.markReadyConnected(result.token, client);
  const old = state.snapshot(client);
  state.bindPage(client);
  assert.equal(page.listenerCount('framenavigated'), 2);
  const next = new FakePage();
  client.pupPage = next;
  state.bindPage(client);
  assert.deepEqual(page.listeners('framenavigated'), [foreign]);
  assert.equal(state.isSnapshotCurrent(old, client), false);
  assert.equal(next.listenerCount('framenavigated'), 1);
  state.dispose();
  assert.equal(next.listenerCount('framenavigated'), 0);
});

test('verified identity cannot become operational without a bound page lifecycle', async () => {
  const state = createAuthenticatedSelfAuthorityState();
  const client = { pupPage: {}, info: { wid: OWNER_PN } };
  const result = await verifyCurrentOwnerAuthority({}, client, state, async () => validVerification());
  assert.equal(result.current, true);
  assert.equal(state.markReadyConnected(result.token, client), false);
  assert.equal(state.isSnapshotCurrent(state.snapshot(client), client), false);
});

for (const loss of [
  { name: 'page close', target: 'pupPage', event: 'close' },
  { name: 'browser disconnect', target: 'pupBrowser', event: 'disconnected' },
]) {
  test(`${loss.name} invalidates the old snapshot immediately`, async () => {
    const state = createAuthenticatedSelfAuthorityState();
    const client = { pupPage: new FakePage(), pupBrowser: new FakeBrowser(), info: { wid: OWNER_PN } };
    const result = await verifyCurrentOwnerAuthority({}, client, state, async () => validVerification());
    assert.equal(state.markReadyConnected(result.token, client), true);
    const old = state.snapshot(client);
    assert.equal(state.isSnapshotCurrent(old, client), true);
    client[loss.target].emit(loss.event);
    assert.equal(state.isSnapshotCurrent(old, client), false);
    assert.equal(state.snapshot(client), null);
    assert.equal(state.restoreConnected(client), false);
    // A late READY using the lost infrastructure must not resurrect authority.
    const late = await verifyCurrentOwnerAuthority({}, client, state, async () => validVerification());
    assert.equal(late.current, false);
  });

  test(`${loss.name} disables status and subsequent Owner Command`, async () => {
    const h = await transportAuthorityHarness();
    await completeReady(h);
    h.client[loss.target].emit(loss.event);
    assert.equal(h.status.ready, false);
    assert.equal(h.status.wwebjs_connected, false);
    assert.equal(h.status.authenticated_identity_match, null);
    if (loss.target === 'pupBrowser') assert.equal(h.status.browser_debug_reachable, false);
    await classifyObservedMessage(h, ownerSelfChat());
    assertNoOwnerPayload(h.normalized.at(-1));
  });

  test(`${loss.name} during provenance downgrades to UNKNOWN_FROM_ME`, async () => {
    const provenance = deferred();
    const h = await transportAuthorityHarness({ classify: () => provenance.promise });
    await completeReady(h);
    const pending = classifyObservedMessage(h, ownerSelfChat());
    h.client[loss.target].emit(loss.event);
    provenance.resolve({ classification: 'OWNER_MANUAL_OUTBOUND_OBSERVED' });
    const result = await pending;
    assert.equal(result.classification, 'UNKNOWN_FROM_ME');
    assert.equal(result.reason, 'AUTHORITY_GENERATION_STALE');
    assertNoOwnerPayload(h.normalized.at(-1));
  });

  test(`${loss.name} before bridge prevents Owner Command in normalized payload`, async () => {
    const h = await transportAuthorityHarness({ onLog: (event, data, client) => {
      if (event === 'from_me_classified') queueMicrotask(() => client[loss.target].emit(loss.event));
    } });
    await completeReady(h);
    assert.equal((await classifyObservedMessage(h, ownerSelfChat())).classification, 'OWNER_COMMAND');
    assertNoOwnerPayload(h.normalized.at(-1));
    assert.equal(h.normalized.at(-1).event_origin, 'UNKNOWN_FROM_ME');
  });

  test(`late ${loss.name} from replaced infrastructure cannot invalidate new authority`, async () => {
    const h = await transportAuthorityHarness();
    await completeReady(h);
    const old = h.client[loss.target];
    const foreign = () => {};
    old.on(loss.event, foreign);
    h.client[loss.target] = loss.target === 'pupPage' ? new FakePage() : new FakeBrowser();
    if (loss.target === 'pupBrowser') h.client.pupBrowser.pages = async () => [h.client.pupPage];
    await completeReady(h);
    assert.deepEqual(old.listeners(loss.event), [foreign]);
    old.emit(loss.event);
    assert.equal(h.status.ready, true);
    assert.equal((await classifyObservedMessage(h, ownerSelfChat())).classification, 'OWNER_COMMAND');
  });
}

test('external detach preserves page and foreign listeners before disconnecting browser', async () => {
  const h = await transportAuthorityHarness();
  await completeReady(h);
  const page = h.client.pupPage;
  const browser = h.client.pupBrowser;
  const foreign = () => {};
  h.client.on('message_create', foreign);
  page.on('framenavigated', foreign);
  page.on('close', foreign);
  browser.on('disconnected', foreign);
  let pageClose = 0;
  let browserDisconnect = 0;
  let browserClose = 0;
  page.isClosed = () => false;
  browser.isConnected = () => true;
  page.close = async () => {
    assert.deepEqual(page.listeners('framenavigated'), [foreign]);
    assert.deepEqual(page.listeners('close'), [foreign]);
    assert.deepEqual(browser.listeners('disconnected'), [foreign]);
    pageClose += 1;
    page.emit('close');
  };
  browser.disconnect = () => {
    assert.deepEqual(page.listeners('framenavigated'), [foreign]);
    assert.deepEqual(page.listeners('close'), [foreign]);
    assert.deepEqual(browser.listeners('disconnected'), [foreign]);
    browserDisconnect += 1; browser.emit('disconnected');
  };
  browser.close = () => { browserClose += 1; };
  const logStart = h.logs.length;
  await detachTransport(h.client);
  assert.deepEqual(h.client.listeners('message_create'), [foreign]);
  assert.equal(pageClose, 0);
  assert.equal(browserDisconnect, 1);
  assert.equal(browserClose, 0);
  assert.deepEqual(h.logs.slice(logStart).filter(e => e.event === 'owner_authority_invalidated')
    .map(e => e.data.reason), ['DETACHED']);
  assert.equal(h.status.ready, false);
});

for (const count of [0, 1, 2]) {
  test(`READY validates ${count} WhatsApp pages before enabling operational authority`, async () => {
    const h = await transportAuthorityHarness();
    let closed = 0;
    h.client.pupPage.close = async () => { closed += 1; };
    h.client.pupBrowser.pages = async () => count === 0 ? []
      : count === 1 ? [h.client.pupPage] : [h.client.pupPage, new FakePage()];
    await completeReady(h);
    assert.equal(h.status.ready, count === 1);
    const result = await classifyObservedMessage(h, ownerSelfChat());
    if (count === 1) assert.equal(result.classification, 'OWNER_COMMAND');
    else {
      assert.equal(h.status.service_state, 'blocked_page_selection');
      assert.notEqual(result.classification, 'OWNER_COMMAND');
      assertNoOwnerPayload(h.normalized.at(-1));
      h.client.emit('change_state', 'CONNECTED');
      assert.equal(h.status.ready, false);
    }
    assert.equal(closed, 0);
  });
}

test('disposed authority cannot rebind infrastructure through a pending callback', async () => {
  const state = createAuthenticatedSelfAuthorityState();
  const client = { pupPage: new FakePage(), pupBrowser: new FakeBrowser(), info: { wid: OWNER_PN } };
  const verification = deferred();
  const pending = verifyCurrentOwnerAuthority({}, client, state, () => verification.promise);
  state.dispose();
  verification.resolve(validVerification());
  assert.equal((await pending).current, false);
  assert.equal(client.pupPage.listenerCount('framenavigated'), 0);
  assert.equal(client.pupPage.listenerCount('close'), 0);
  assert.equal(client.pupBrowser.listenerCount('disconnected'), 0);
});

test('same-page navigation changes document epoch and rejects an old verification token', async () => {
  const state = createAuthenticatedSelfAuthorityState();
  const page = new FakePage();
  const client = { pupPage: page, info: { wid: OWNER_PN } };
  const pending = deferred();
  const verification = verifyCurrentOwnerAuthority({}, client, state, () => pending.promise);
  const epoch = state.documentEpoch();
  page.emit('framenavigated', page.mainFrame());
  assert.equal(state.documentEpoch(), epoch + 1);
  pending.resolve(validVerification());
  assert.equal((await verification).current, false);
  assert.equal(state.snapshot(client), null);
});

test('navigation during provenance downgrades the pending command', async () => {
  const provenance = deferred();
  const h = await transportAuthorityHarness({ classify: () => provenance.promise });
  await completeReady(h);
  const pending = classifyObservedMessage(h, ownerSelfChat());
  h.client.pupPage.emit('framenavigated', h.client.pupPage.mainFrame());
  provenance.resolve({ classification: 'OWNER_MANUAL_OUTBOUND_OBSERVED', provenance: null });
  const result = await pending;
  assert.equal(result.classification, 'UNKNOWN_FROM_ME');
  assert.equal(result.reason, 'AUTHORITY_GENERATION_STALE');
  assertNoOwnerPayload(h.normalized.at(-1));
});

test('navigation between classification and bridge invalidates normalized authority', async () => {
  const h = await transportAuthorityHarness({ onLog: (event, data, client) => {
    if (event === 'from_me_classified') {
      queueMicrotask(() => client.pupPage.emit('framenavigated', client.pupPage.mainFrame()));
    }
  } });
  await completeReady(h);
  const result = await classifyObservedMessage(h, ownerSelfChat());
  assert.equal(result.classification, 'OWNER_COMMAND'); // Before the navigation microtask.
  assertNoOwnerPayload(h.normalized.at(-1));
  assert.equal(h.normalized.at(-1).event_origin, 'UNKNOWN_FROM_ME');
});

for (const transient of ['OPENING', 'TIMEOUT']) {
  test(`${transient} suspends and CONNECTED restores only established ready proof`, async () => {
    const h = await transportAuthorityHarness();
    await completeReady(h);
    h.client.emit('change_state', transient);
    assert.equal(h.status.authenticated_identity_match, true);
    assert.equal(h.status.ready, false);
    assert.equal((await classifyObservedMessage(h, ownerSelfChat())).classification, 'UNKNOWN_FROM_ME');
    h.client.emit('change_state', 'CONNECTED');
    assert.equal((await classifyObservedMessage(h, ownerSelfChat())).classification, 'OWNER_COMMAND');
    h.client.emit('change_state', 'UNPAIRED');
    h.client.emit('change_state', 'CONNECTED');
    assert.equal(h.status.ready, false);
    await flushMicrotasks();
    h.verificationCalls.at(-1).resolve(validVerification());
    await flushMicrotasks();
    assert.equal((await classifyObservedMessage(h, ownerSelfChat())).classification, 'OWNER_COMMAND');
  });
}

test('suspend then restore during provenance never revives an in-flight snapshot', async () => {
  const provenance = deferred();
  const h = await transportAuthorityHarness({ classify: () => provenance.promise });
  await completeReady(h);
  const pending = classifyObservedMessage(h, ownerSelfChat());
  h.client.emit('change_state', 'TIMEOUT');
  h.client.emit('change_state', 'CONNECTED');
  provenance.resolve({ classification: 'OWNER_MANUAL_OUTBOUND_OBSERVED' });
  assert.equal((await pending).classification, 'UNKNOWN_FROM_ME');
  assertNoOwnerPayload(h.normalized.at(-1));
});

for (const event of ['disconnected', 'navigation', 'transient']) {
  test(`pending getState cannot overwrite newer ${event}`, async () => {
    const query = deferred();
    let stateCalls = 0;
    const h = await transportAuthorityHarness({ getState: () => {
      stateCalls += 1;
      return stateCalls <= 2 ? 'CONNECTED' : query.promise;
    } });
    await completeReady(h);
    h.client.emit('ready');
    await flushMicrotasks();
    if (event === 'navigation') h.client.pupPage.emit('framenavigated', h.client.pupPage.mainFrame());
    else if (event === 'transient') h.client.emit('change_state', 'TIMEOUT');
    else h.client.emit('disconnected', 'LOGOUT');
    const expected = h.status.service_state;
    query.resolve('CONNECTED');
    await flushMicrotasks();
    assert.equal(h.status.service_state, expected);
    assert.equal(h.status.ready, false);
  });
}

test('CONNECTED self-heals invalidated authority without another ready event', async () => {
  const h = await transportAuthorityHarness();
  await completeReady(h);
  h.client.pupPage.emit('framenavigated', h.client.pupPage.mainFrame());
  assert.equal(h.status.owner_command_authority_ready, false);
  const before = h.verificationCalls.length;

  h.client.emit('change_state', 'CONNECTED');
  await flushMicrotasks();
  assert.equal(h.verificationCalls.length, before + 1);
  h.verificationCalls.at(-1).resolve(validVerification());
  await flushMicrotasks();

  assert.equal(h.status.owner_command_authority_ready, true);
  assert.equal(h.status.owner_command_authority_reason, 'READY');
});

test('message_create self-heals authority before classifying an exact owner self-chat', async () => {
  const h = await transportAuthorityHarness();
  await completeReady(h);
  h.client.pupPage.emit('framenavigated', h.client.pupPage.mainFrame());
  const start = h.logs.length;

  h.client.emit('message_create', ownerSelfChat());
  await flushMicrotasks();
  h.verificationCalls.at(-1).resolve(validVerification());
  await flushMicrotasks();

  const classified = h.logs.slice(start).find((entry) => entry.event === 'from_me_classified');
  assert.equal(classified.data.classification, 'OWNER_COMMAND');
  assert.equal(h.normalized.at(-1).owner_authenticated, true);
});

test('message_create reverify failure never produces Owner Command', async () => {
  const h = await transportAuthorityHarness();
  await completeReady(h);
  h.client.pupPage.emit('framenavigated', h.client.pupPage.mainFrame());
  const start = h.logs.length;

  h.client.emit('message_create', ownerSelfChat());
  await flushMicrotasks();
  h.verificationCalls.at(-1).resolve({ result: 'MISMATCH', identity: { result: 'RESOLVED' } });
  await flushMicrotasks();

  const classified = h.logs.slice(start).find((entry) => entry.event === 'from_me_classified');
  assert.notEqual(classified.data.classification, 'OWNER_COMMAND');
  assertNoOwnerPayload(h.normalized.at(-1));
});

test('message_create reverify timeout remains fail-closed', async () => {
  const h = await transportAuthorityHarness({ ownerAuthorityReverifyTimeoutMs: 5 });
  await completeReady(h);
  h.client.pupPage.emit('framenavigated', h.client.pupPage.mainFrame());
  const start = h.logs.length;

  h.client.emit('message_create', ownerSelfChat());
  await new Promise((resolve) => setTimeout(resolve, 15));
  await flushMicrotasks();

  const classified = h.logs.slice(start).find((entry) => entry.event === 'from_me_classified');
  assert.notEqual(classified.data.classification, 'OWNER_COMMAND');
  assertNoOwnerPayload(h.normalized.at(-1));
});

test('periodic authority recovery is 10s single-flight and is cleaned up on detach', async () => {
  const timers = [];
  const cleared = [];
  const h = await transportAuthorityHarness({
    setInterval(callback, milliseconds) {
      const timer = { callback, milliseconds, unrefCalled: false, unref() { this.unrefCalled = true; } };
      timers.push(timer);
      return timer;
    },
    clearInterval(timer) { cleared.push(timer); },
  });
  assert.equal(OWNER_AUTHORITY_REVERIFY_INTERVAL_MS, 10000);
  assert.equal(timers[0].milliseconds, 10000);
  assert.equal(timers[0].unrefCalled, true);
  await completeReady(h);
  h.client.pupPage.emit('framenavigated', h.client.pupPage.mainFrame());
  const before = h.verificationCalls.length;

  timers[0].callback();
  timers[0].callback();
  await flushMicrotasks();
  assert.equal(h.verificationCalls.length, before + 1);
  h.verificationCalls.at(-1).resolve(validVerification());
  await flushMicrotasks();
  assert.equal(h.status.owner_command_authority_ready, true);

  await detachTransport(h.client);
  assert.deepEqual(cleared, timers);
});

test('status reads are observational and never change authority generation', async () => {
  const state = createAuthenticatedSelfAuthorityState();
  const client = { info: { wid: OWNER_PN }, pupPage: new FakePage(), pupBrowser: new FakeBrowser() };
  const result = await verifyCurrentOwnerAuthority({}, client, state, async () => validVerification());
  state.markReadyConnected(result.token, client);
  client.info.wid = '99000000016@c.us';
  const generation = state.generation();

  assert.equal(state.describe(client).current, false);
  assert.equal(state.describe(client).current, false);
  assert.equal(state.generation(), generation);
});

test('temporary missing WID fails closed and same WID can reverify', async () => {
  const h = await transportAuthorityHarness();
  await completeReady(h);
  const lastInvalidation = h.status.owner_authority_last_invalidation_at;
  h.client.info.wid = null;

  assert.equal(h.status.owner_command_authority_ready, false);
  const result = await classifyObservedMessage(h, ownerSelfChat());
  assert.notEqual(result.classification, 'OWNER_COMMAND');
  assertNoOwnerPayload(h.normalized.at(-1));
  assert.equal(h.status.owner_authority_last_invalidation_at, lastInvalidation);

  h.client.info.wid = OWNER_PN;
  const recovery = h.ensureOwnerAuthorityCurrent('test_same_wid');
  await flushMicrotasks();
  h.verificationCalls.at(-1).resolve(validVerification());
  assert.equal((await recovery).ready, true);
  assert.equal(h.status.owner_command_authority_ready, true);
});

test('different non-empty WID strongly invalidates and requires a new mismatch-safe proof', async () => {
  const h = await transportAuthorityHarness();
  await completeReady(h);
  h.client.info.wid = '99000000016@c.us';
  const recovery = h.ensureOwnerAuthorityCurrent('test_changed_wid');
  await flushMicrotasks();
  h.verificationCalls.at(-1).resolve({ result: 'MISMATCH', identity: { result: 'RESOLVED' } });
  assert.equal((await recovery).ready, false);
  assert.equal(h.status.owner_command_authority_ready, false);
  assert.equal(h.status.owner_authority_last_invalidation_reason, 'WID_CHANGED');
  const invalidation = h.logs.find((entry) => (
    entry.event === 'owner_authority_invalidated' && entry.data.reason === 'WID_CHANGED'
  ));
  assert.ok(invalidation);
  assert.deepEqual(Object.keys(invalidation.data).sort(), ['generation', 'reason', 'timestamp']);
});

for (const loss of [
  { name: 'PAGE_CLOSED', emit: (h) => h.client.pupPage.emit('close') },
  { name: 'BROWSER_DISCONNECTED', emit: (h) => h.client.pupBrowser.emit('disconnected') },
]) {
  test(`${loss.name} does not recover on the dead object`, async () => {
    const h = await transportAuthorityHarness();
    await completeReady(h);
    loss.emit(h);
    const before = h.verificationCalls.length;
    const result = await h.ensureOwnerAuthorityCurrent('test_dead_object');
    assert.equal(result.ready, false);
    assert.equal(result.reason, loss.name);
    assert.equal(h.verificationCalls.length, before);
    assert.equal(h.status.owner_command_authority_ready, false);
  });
}
