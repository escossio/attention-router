const http = require('node:http');
const https = require('node:https');

const { Client } = require('whatsapp-web.js');
const puppeteer = require('puppeteer');
const { createStatus, nowIso, isReady, attachOwnerAuthorityStatus } = require('./status');
const { createInboundBridge } = require('./bridge');
const {
  authenticatedSelfAliasSet,
  canonicalIdentity,
  isAuthenticatedOwnerSelfChatMessage,
  kindForId,
} = require('./message-id');
const { compareOwnerIdentity, resolveWhatsAppIdentity } = require('./identity');
const { identityFromAliases, resolveConversationIdentity } = require('./conversation');

const TRUSTED_WHATSAPP_ORIGIN = 'https://web.whatsapp.com';

function isTrustedWhatsAppPageUrl(value) {
  try {
    return new URL(value).origin === TRUSTED_WHATSAPP_ORIGIN;
  } catch {
    return false;
  }
}

function safeState(value) {
  if (value === undefined || value === null) {
    return 'UNKNOWN';
  }
  return String(value);
}

function applyStatusCounters(status, counters = {}) {
  if (typeof counters.inbound_seen_count === 'number') {
    status.inbound_seen_count += counters.inbound_seen_count;
  }
  if (counters.last_inbound_seen_at) {
    status.last_inbound_seen_at = counters.last_inbound_seen_at;
  }
  if (counters.last_inbound_delivery_at) {
    status.last_inbound_delivery_at = counters.last_inbound_delivery_at;
  }
  if (counters.last_inbound_error_at) {
    status.last_inbound_error_at = counters.last_inbound_error_at;
  }
}

function authenticatedIdentityMatches(config, clientInfo) {
  if (!config.ownerIdentityRef) return false;
  return compareOwnerIdentity(
    config.ownerIdentityRef,
    resolveWhatsAppIdentity({ raw: clientInfo?.wid, pn: clientInfo?.pn, lid: clientInfo?.lid }),
  ).result === 'MATCH';
}

async function resolveClientIdentity(client) {
  const clientInfo = client?.info || {};
  let self = {};
  try {
    self = await client?.pupPage?.evaluate(() => {
      const getSerialized = (value) => value?._serialized || value?.serialized || value?.toString?.() || null;
      const me = window.require('WAWebUserPrefsMeUser');
      return {
        pn: getSerialized(me.getMaybeMePnUser?.()),
        lid: getSerialized(me.getMaybeMeLidUser?.()),
      };
    });
  } catch {
    self = {};
  }
  return resolveWhatsAppIdentity({ raw: clientInfo.wid, pn: self.pn, lid: self.lid });
}

async function verifyConfiguredOwner(config, client) {
  if (!config.ownerIdentityRef) return { result: 'NOT_CONFIGURED', identity: null };
  const identity = await resolveClientIdentity(client);
  return { ...compareOwnerIdentity(config.ownerIdentityRef, identity), identity };
}

function authenticatedSelfIdentityFromVerification(verification) {
  if (
    verification?.result !== 'MATCH'
    || verification?.identity?.result !== 'RESOLVED'
  ) {
    return null;
  }
  const candidate = {
    ...verification.identity,
    ownerVerified: true,
  };
  const aliases = authenticatedSelfAliasSet(candidate);
  if (!aliases) return null;
  return { ...candidate, aliases: [...aliases].sort() };
}

const authorityCleanup = new WeakMap();
const OWNER_AUTHORITY_REVERIFY_INTERVAL_MS = 10000;
const OWNER_AUTHORITY_REVERIFY_TIMEOUT_MS = 3000;

function createAuthenticatedSelfAuthorityState(onInvalidated = () => {}) {
  let generation = 0;
  let documentEpoch = 0;
  let operationalEpoch = 0;
  let operational = false;
  let readyToken = null;
  let boundPage = null;
  let navigationListener = null;
  let closeListener = null;
  let boundBrowser = null;
  let disconnectListener = null;
  let pageLost = false;
  let browserLost = false;
  let disposed = false;
  let identity = null;
  let identityToken = null;
  let verificationResult = 'NOT_EVALUATED';
  let identityResolved = false;
  let verificationToken = null;
  let lastNonEmptyWid = null;

  function suspend(preserveReadyProof = false) {
    operationalEpoch += 1;
    operational = false;
    if (!preserveReadyProof) readyToken = null;
  }

  function invalidate(reason = 'PROOF_STALE') {
    suspend();
    generation += 1;
    identity = null;
    identityToken = null;
    verificationResult = 'NOT_EVALUATED';
    identityResolved = false;
    verificationToken = null;
    onInvalidated(reason, generation, nowIso());
    return generation;
  }

  function invalidateDocument(reason) {
    documentEpoch += 1;
    invalidate(reason);
  }

  function unbind() {
    if (navigationListener) boundPage?.off?.('framenavigated', navigationListener);
    if (closeListener) boundPage?.off?.('close', closeListener);
    navigationListener = null;
    closeListener = null;
    boundPage = null;
  }

  function unbindBrowser() {
    if (disconnectListener) boundBrowser?.off?.('disconnected', disconnectListener);
    disconnectListener = null;
    boundBrowser = null;
  }

  function bindBrowser(client) {
    const browser = client?.pupBrowser || null;
    if (browser === boundBrowser) return;
    const replacing = boundBrowser !== null;
    unbindBrowser();
    boundBrowser = browser;
    browserLost = browser?.isConnected?.() === false;
    if (replacing) invalidateDocument('PROOF_STALE');
    if (typeof browser?.on === 'function' && typeof browser?.off === 'function') {
      disconnectListener = () => {
        if (disposed || browser !== boundBrowser || browser !== client?.pupBrowser) return;
        browserLost = true;
        invalidateDocument('BROWSER_DISCONNECTED');
      };
      browser.on('disconnected', disconnectListener);
    }
  }

  function bindPage(client) {
    if (disposed) return;
    bindBrowser(client);
    const page = client?.pupPage || null;
    if (page === boundPage) return;
    const replacing = boundPage !== null;
    unbind();
    boundPage = page;
    pageLost = page?.isClosed?.() === true;
    if (replacing) invalidateDocument('PROOF_STALE');
    if (typeof page?.on === 'function' && typeof page?.off === 'function'
      && typeof page?.mainFrame === 'function') {
      navigationListener = (frame) => {
        if (disposed || page !== boundPage || page !== client?.pupPage) return;
        if (frame === page.mainFrame()) invalidateDocument('MAIN_FRAME_NAVIGATION');
      };
      closeListener = () => {
        if (disposed || page !== boundPage || page !== client?.pupPage) return;
        pageLost = true;
        invalidateDocument('PAGE_CLOSED');
      };
      page.on('framenavigated', navigationListener);
      page.on('close', closeListener);
    }
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    unbind();
    unbindBrowser();
    invalidateDocument('DETACHED');
  }

  function beginVerification(client) {
    bindPage(client);
    const wid = canonicalIdentity(client?.info?.wid);
    if (!wid) return null;
    if (lastNonEmptyWid && lastNonEmptyWid !== wid) invalidate('WID_CHANGED');
    lastNonEmptyWid = wid;
    if (identity || verificationToken || readyToken) invalidate('PROOF_STALE');
    else {
      suspend();
      generation += 1;
    }
    const token = Object.freeze({
      generation,
      documentEpoch,
      page: client?.pupPage || null,
      browser: client?.pupBrowser || null,
      wid,
    });
    return token;
  }

  function isVerificationCurrent(token, client) {
    const currentWid = canonicalIdentity(client?.info?.wid);
    return Boolean(
      token
      && !disposed && !pageLost && !browserLost
      && token.generation === generation
      && token.documentEpoch === documentEpoch
      && token.page === (client?.pupPage || null)
      && token.browser === (client?.pupBrowser || null)
      && token.wid && currentWid && token.wid === currentWid,
    );
  }

  function publish(token, client, candidate, verification = {}) {
    if (!isVerificationCurrent(token, client)) return false;
    suspend();
    identity = candidate
      ? Object.freeze({ ...candidate, aliases: Object.freeze([...candidate.aliases]) })
      : null;
    identityToken = identity ? token : null;
    verificationResult = ['NOT_CONFIGURED', 'MATCH', 'MISMATCH', 'UNRESOLVED', 'ERROR']
      .includes(verification.result) ? verification.result : 'NOT_EVALUATED';
    identityResolved = verification.identity?.result === 'RESOLVED';
    verificationToken = token;
    return true;
  }

  function markReadyConnected(token, client) {
    if (!isVerificationCurrent(token, client)) return false;
    if (!navigationListener) return false;
    readyToken = token;
    operational = true;
    return true;
  }

  function restoreConnected(client) {
    if (!isVerificationCurrent(readyToken, client)) return false;
    if (!navigationListener) return false;
    operational = true;
    return true;
  }

  function snapshot(client = null) {
    if (!identity) return null;
    if (client && !isVerificationCurrent(identityToken, client)) return null;
    return Object.freeze({
      generation,
      documentEpoch,
      operationalEpoch,
      operational,
      identity,
      page: identityToken.page,
      browser: identityToken.browser,
      wid: identityToken.wid,
    });
  }

  function isSnapshotCurrent(candidate, client = null) {
    const currentWid = client ? canonicalIdentity(client?.info?.wid) : null;
    return Boolean(
      candidate
      && !disposed && !pageLost && !browserLost
      && candidate.operational === true
      && operational
      && candidate.operationalEpoch === operationalEpoch
      && candidate.documentEpoch === documentEpoch
      && candidate.generation === generation
      && identity !== null
      && candidate.identity === identity
      && candidate.page === identityToken?.page
      && candidate.browser === identityToken?.browser
      && candidate.wid === identityToken?.wid
      && (
        !client
        || (
          candidate.page === (client?.pupPage || null)
          && candidate.browser === (client?.pupBrowser || null)
          && candidate.wid && currentWid && candidate.wid === currentWid
        )
      ),
    );
  }

  function describe(client) {
    const verificationCurrent = Boolean(
      verificationToken && isVerificationCurrent(verificationToken, client),
    );
    const value = snapshot(client);
    const current = isSnapshotCurrent(value, client);
    return {
      identityPresent: verificationCurrent && Boolean(identity),
      operational: value?.operational === true,
      current,
      verification: verificationCurrent ? verificationResult : 'NOT_EVALUATED',
      resolved: verificationCurrent && identityResolved,
    };
  }

  function recoveryBlockReason(client) {
    if (disposed) return 'DETACHED';
    if (pageLost && boundPage === client?.pupPage) return 'PAGE_CLOSED';
    if (browserLost && boundBrowser === client?.pupBrowser) return 'BROWSER_DISCONNECTED';
    return null;
  }

  return {
    beginVerification,
    describe,
    bindPage,
    dispose,
    documentEpoch: () => documentEpoch,
    generation: () => generation,
    invalidate,
    isSnapshotCurrent,
    isVerificationCurrent,
    publish,
    recoveryBlockReason,
    markReadyConnected,
    restoreConnected,
    suspend,
    snapshot,
  };
}

async function verifyCurrentOwnerAuthority(
  config,
  client,
  authorityState,
  verifyOwner = verifyConfiguredOwner,
) {
  const token = authorityState.beginVerification(client);
  if (!token) return { current: false, missingWid: true, token: null };
  const staleResult = () => {
    if (token.generation === authorityState.generation()) authorityState.invalidate('PROOF_STALE');
    return { current: false, stale: true, token };
  };
  let verification;
  try {
    verification = await verifyOwner(config, client);
  } catch (error) {
    if (!authorityState.isVerificationCurrent(token, client)) {
      return staleResult();
    }
    authorityState.publish(token, client, null, { result: 'ERROR' });
    return { current: true, error, token };
  }
  if (!authorityState.isVerificationCurrent(token, client)) {
    return staleResult();
  }
  const authenticatedSelfIdentity = authenticatedSelfIdentityFromVerification(verification);
  if (!authorityState.publish(token, client, authenticatedSelfIdentity, verification)) {
    return staleResult();
  }
  return {
    authenticatedSelfIdentity,
    current: true,
    token,
    verification,
  };
}

function isAuthorityContextCurrent(authorityContext) {
  if (typeof authorityContext?.isCurrent !== 'function') return false;
  try {
    return authorityContext.isCurrent() === true;
  } catch {
    return false;
  }
}

function failClosedStaleOwnerCommand(result, authorityCurrent) {
  if (result?.classification !== 'OWNER_COMMAND' || authorityCurrent) return result;
  return {
    classification: 'UNKNOWN_FROM_ME',
    reason: 'AUTHORITY_GENERATION_STALE',
    provenance: null,
    authenticatedOwnerSelfChat: false,
  };
}

const AUTHORITY_INVALIDATING_STATES = new Set([
  'CONFLICT',
  'DEPRECATED_VERSION',
  'DISCONNECTED',
  'PAIRING',
  'PROXYBLOCK',
  'SESSION_NOT_AUTHENTICATED',
  'SMB_TOS_BLOCK',
  'TOS_BLOCK',
  'UNLAUNCHED',
  'UNPAIRED',
  'UNPAIRED_IDLE',
]);

function probeBrowserDebugUrl(browserDebugUrl, timeoutMs = 1500) {
  return new Promise((resolve) => {
    if (!browserDebugUrl) {
      resolve(false);
      return;
    }
    let url;
    try {
      url = new URL(browserDebugUrl);
    } catch {
      resolve(false);
      return;
    }
    const client = url.protocol === 'https:' ? https : http;
    const req = client.request(
      {
        method: 'GET',
        hostname: url.hostname,
        port: url.port,
        path: '/json/version',
        timeout: timeoutMs,
      },
      (res) => {
        res.resume();
        resolve(res.statusCode === 200);
      },
    );
    req.on('timeout', () => {
      req.destroy();
      resolve(false);
    });
    req.on('error', () => resolve(false));
    req.end();
  });
}

const startupSleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function boundedOperation(operation, timeoutMs = 1500) {
  let timer;
  try {
    return await Promise.race([
      Promise.resolve().then(operation),
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('STARTUP_OPERATION_TIMEOUT')), timeoutMs); }),
    ]);
  } finally { clearTimeout(timer); }
}

async function waitForBrowserDebug(browserURL, logger = console, options = {}) {
  const { now = Date.now, sleep = startupSleep, timeoutMs = 60000,
    intervalMs = 500, probe = probeBrowserDebugUrl } = options;
  const deadline = now() + timeoutMs;
  logger.info?.('browser_debug_wait_started');
  do {
    if (await boundedOperation(() => probe(browserURL), Math.max(1, Math.min(2000, deadline - now()))).catch(() => false)) {
      logger.info?.('browser_debug_available');
      return true;
    }
    if (now() >= deadline) break;
    await sleep(Math.min(intervalMs, deadline - now()));
  } while (now() < deadline);
  return false;
}

async function inspectWhatsAppPageState(page) {
  try {
    return await boundedOperation(() => page.evaluate(() => {
      let nativeState;
      try { nativeState = window.require?.('WAWebSocketModel')?.Socket?.state; } catch {}
      const injectedState = window.AuthStore?.AppState?.state;
      if (typeof nativeState === 'string') {
        // A native non-CONNECTED state must not be overridden by an old injected value.
        return { state: nativeState, detectionSource: 'WA_WEB_SOCKET_MODEL' };
      }
      if (typeof injectedState === 'string') return { state: injectedState, detectionSource: 'AUTH_STORE' };
      return { state: null, detectionSource: null };
    }));
  } catch { return { state: null, detectionSource: null, error: true }; }
}

async function selectCanonicalConnectedWhatsAppPage(pages) {
  const whatsappPages = pages.filter((page) => isTrustedWhatsAppPageUrl(page.url()));
  const inspections = await Promise.all(whatsappPages.map(inspectWhatsAppPageState));
  const connectedPageCount = inspections.filter((s) => s.state === 'CONNECTED' && !s.error).length;
  const counts = { whatsappPageCount: whatsappPages.length, connectedPageCount };
  if (whatsappPages.length > 1) return { result: 'MULTIPLE_WHATSAPP_PAGES', ...counts };
  if (!whatsappPages.length) return { result: 'NO_WHATSAPP_PAGE', ...counts };
  if (inspections[0].error || !inspections[0].state) return { result: 'PAGE_INSPECTION_FAILED', ...counts };
  if (connectedPageCount !== 1) return { result: 'NO_CONNECTED_PAGE', ...counts };
  return { result: 'SELECTED', page: whatsappPages[0], detectionSource: inspections[0].detectionSource, ...counts };
}

const activeAttachBrokers = new WeakSet();

function armExistingPageAttach(puppeteerModule, browser, page, browserURL, logger = console) {
  if (activeAttachBrokers.has(puppeteerModule)) throw new Error('ATTACH_BROKER_ALREADY_ARMED');
  const originalConnect = puppeteerModule.connect;
  const originalNewPageDescriptor = Object.getOwnPropertyDescriptor(browser, 'newPage');
  let connected = false;
  let consumed = false;
  let restored = false;
  activeAttachBrokers.add(puppeteerModule);
  function restore() {
    if (restored) return;
    restored = true;
    puppeteerModule.connect = originalConnect;
    if (originalNewPageDescriptor) Object.defineProperty(browser, 'newPage', originalNewPageDescriptor);
    else delete browser.newPage;
    activeAttachBrokers.delete(puppeteerModule);
    logger.info?.('existing_page_attach_restored');
  }
  try {
    const interceptConnect = async (options) => {
      if (options?.browserURL !== browserURL) return originalConnect.call(puppeteerModule, options);
      if (restored) throw new Error('ATTACH_BROKER_RESTORED');
      connected = true;
      puppeteerModule.connect = originalConnect;
      try {
        const acquireExistingPage = async () => {
          try {
            if (restored || page.isClosed?.()) throw new Error('CANONICAL_PAGE_UNAVAILABLE');
            const current = await selectCanonicalConnectedWhatsAppPage(await boundedOperation(() => browser.pages()));
            if (current.result !== 'SELECTED' || current.page !== page) throw new Error('CANONICAL_PAGE_SELECTION_CHANGED');
            consumed = true;
            logger.info?.('existing_page_attach_consumed');
            return page;
          } finally { restore(); }
        };
        browser.newPage = acquireExistingPage;
        if (browser.newPage !== acquireExistingPage) throw new Error('NEW_PAGE_INTERCEPT_UNAVAILABLE');
      } catch (error) { restore(); throw error; }
      return browser;
    };
    puppeteerModule.connect = interceptConnect;
    if (puppeteerModule.connect !== interceptConnect) throw new Error('CONNECT_INTERCEPT_UNAVAILABLE');
    logger.info?.('existing_page_attach_armed');
  } catch (error) { restore(); throw error; }
  return { restore, consumed: () => connected && consumed };
}

async function prepareAuthenticatedPage(browserDebugUrl, logger = console, options = {}) {
  const { puppeteerModule = puppeteer, now = Date.now, sleep = startupSleep,
    timeoutMs = 60000, intervalMs = 500 } = options;
  let browser;
  try {
    if (activeAttachBrokers.has(puppeteerModule)) return { result: 'ATTACH_BROKER_ALREADY_ARMED' };
    if (puppeteerModule === puppeteer) {
      const wwebjsPath = require.resolve('whatsapp-web.js');
      const peerPuppeteer = require(require.resolve('puppeteer', { paths: [require('node:path').dirname(wwebjsPath)] }));
      if (peerPuppeteer !== puppeteerModule) return { result: 'PUPPETEER_MODULE_NOT_SHARED' };
    }
    // A late connect result is detached as well, without touching the external Page.
    let expired = false;
    const connecting = Promise.resolve().then(() => puppeteerModule.connect({ browserURL: browserDebugUrl, protocolTimeout: 60000 }))
      .then((connected) => { if (expired) connected.disconnect(); return connected; });
    try { browser = await boundedOperation(() => connecting, 5000); }
    catch (error) { expired = true; throw error; }
    const deadline = now() + timeoutMs;
    let selection;
    do {
      selection = await selectCanonicalConnectedWhatsAppPage(await boundedOperation(() => browser.pages()));
      logger.info?.('whatsapp_page_selection', {
        whatsapp_page_count: selection.whatsappPageCount, connected_page_count: selection.connectedPageCount,
        selection_result: selection.result, detection_source: selection.detectionSource || null,
      });
      if (selection.result === 'SELECTED') {
        const broker = armExistingPageAttach(puppeteerModule, browser, selection.page, browserDebugUrl, logger);
        let disposed = false;
        return { ...selection, broker, browser, dispose: () => {
          if (disposed) return;
          disposed = true;
          broker.restore(); browser.disconnect();
        } };
      }
      if (selection.result === 'MULTIPLE_WHATSAPP_PAGES' || now() >= deadline) break;
      await sleep(Math.min(intervalMs, deadline - now()));
    } while (now() < deadline);
    browser.disconnect();
    return selection;
  } catch {
    browser?.disconnect();
    return { result: 'PAGE_INSPECTION_FAILED' };
  }
}

async function recoverConnectedPage(client, status, logger = console) {
  const deadline = Date.now() + 60000;
  while (Date.now() < deadline && !status.ready) {
    const browser = client.pupBrowser;
    if (browser?.pages) {
      const pages = await browser.pages().catch(() => []);
      for (const page of pages) {
        if (!isTrustedWhatsAppPageUrl(page.url())) continue;
        const connected = await page.evaluate(() => ({
          connected: window.AuthStore?.AppState?.state === 'CONNECTED',
          handler: typeof window.onAppStateHasSyncedEvent === 'function',
        })).catch(() => null);
        if (connected?.connected && connected.handler) {
          client.pupPage = page;
          await page.evaluate(() => window.onAppStateHasSyncedEvent()).catch((error) => {
            logger.warn?.('connected page recovery failed', { error: String(error?.message || error) });
          });
          return true;
        }
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  return false;
}

async function detachTransport(client, logger = console, { pageOwnership = 'external' } = {}) {
  if (!client) {
    return;
  }
  authorityCleanup.get(client)?.();
  authorityCleanup.delete(client);
  if (pageOwnership !== 'external') throw new Error('UNSUPPORTED_PAGE_OWNERSHIP');
  const browser = client.pupBrowser;
  try {
    if (browser?.isConnected?.()) {
      browser.disconnect?.();
    }
  } catch (error) {
    logger.warn?.('browser disconnect failed during detach', { error: String(error?.message || error) });
  }
}

async function classifyFromMeMessage(
  message,
  client,
  outboundProvenance,
  authenticatedSelfIdentity = null,
  authorityContext = null,
) {
  const authenticatedSelfChat = Boolean(
    isAuthorityContextCurrent(authorityContext)
    && isAuthenticatedOwnerSelfChatMessage(message, authenticatedSelfIdentity),
  );
  const conversationIdentity = authenticatedSelfChat
    ? identityFromAliases(authenticatedSelfIdentity.aliases)
    : await resolveConversationIdentity(message, client);
  message.__conversationIdentity = conversationIdentity;
  if (!outboundProvenance) {
    return { classification: 'UNKNOWN_FROM_ME', reason: 'OUTBOUND_PROVENANCE_UNAVAILABLE', provenance: null };
  }
  if (typeof outboundProvenance.classifyBounded === 'function') {
    return outboundProvenance.classifyBounded(message, conversationIdentity);
  }
  return outboundProvenance.classify(message, conversationIdentity);
}

async function classifyFinalFromMeMessage(
  message,
  client,
  outboundProvenance,
  authenticatedSelfIdentity = null,
  authorityContext = null,
) {
  const authenticatedOwnerSelfChatAtStart = Boolean(
    isAuthorityContextCurrent(authorityContext)
    && isAuthenticatedOwnerSelfChatMessage(message, authenticatedSelfIdentity),
  );
  const provenance = await classifyFromMeMessage(
    message,
    client,
    outboundProvenance,
    authenticatedSelfIdentity,
    authorityContext,
  );
  const authorityCurrent = isAuthorityContextCurrent(authorityContext);
  const authenticatedOwnerSelfChat = Boolean(
    authorityCurrent
    && isAuthenticatedOwnerSelfChatMessage(message, authenticatedSelfIdentity),
  );
  if (
    provenance.classification === 'OWNER_MANUAL_OUTBOUND_OBSERVED'
    && isAuthenticatedOwnerSelfChatMessage(message, authenticatedSelfIdentity)
    && (!authenticatedOwnerSelfChatAtStart || !authorityCurrent)
  ) {
    return {
      classification: 'UNKNOWN_FROM_ME',
      reason: authenticatedOwnerSelfChatAtStart ? 'AUTHORITY_GENERATION_STALE' : 'AUTHORITY_NOT_OPERATIONAL',
      provenance: null,
      authenticatedOwnerSelfChat: false,
    };
  }
  if (
    provenance.classification === 'OWNER_MANUAL_OUTBOUND_OBSERVED'
    && authenticatedOwnerSelfChat
  ) {
    return {
      classification: 'OWNER_COMMAND',
      reason: 'AUTHENTICATED_OWNER_SELF_CHAT',
      provenance: null,
      authenticatedOwnerSelfChat: true,
    };
  }
  return {
    ...provenance,
    authenticatedOwnerSelfChat,
  };
}

async function startTransport(config, logger = console, deps = {}) {
  const status = createStatus(config);
  status.service_state = 'attaching';
  const inboundBridge = (deps.createInboundBridge || createInboundBridge)(config, logger, {
    onCounters: (counters) => applyStatusCounters(status, counters),
  });
  const outboundProvenance = deps.outboundProvenance;
  const probeDebugUrl = deps.probeBrowserDebugUrl || probeBrowserDebugUrl;
  const preparePage = deps.prepareAuthenticatedPage || prepareAuthenticatedPage;
  const verifyOwner = deps.verifyConfiguredOwner || verifyConfiguredOwner;
  const ClientClass = deps.Client || Client;

  if (!config.browserDebugUrl) {
    status.service_state = 'blocked';
    status.client_state = 'NO_BROWSER_DEBUG_URL';
    logger.warn?.('browser debug url missing or invalid');
    return {
      client: null,
      status,
      initPromise: Promise.resolve(),
      isReady: () => isReady(status),
    };
  }

  status.browser_debug_reachable = await waitForBrowserDebug(config.browserDebugUrl, logger, {
    ...deps.startupWaitOptions, probe: probeDebugUrl,
  });
  if (!status.browser_debug_reachable) {
    status.service_state = 'browser_unavailable';
    status.client_state = 'BROWSER_DEBUG_UNREACHABLE';
    return {
      client: null,
      status,
      initPromise: Promise.resolve(),
      isReady: () => isReady(status),
    };
  }

  const preparation = await preparePage(config.browserDebugUrl, logger, deps.startupWaitOptions)
    .catch(() => ({ result: 'PAGE_INSPECTION_FAILED' }));
  if (preparation?.result !== 'SELECTED' || !preparation.broker) {
    preparation?.dispose?.();
    status.service_state = 'blocked_page_selection';
    status.client_state = preparation?.result || 'PAGE_PREPARATION_FAILED';
    if (config.ownerIdentityRef) status.owner_command_authority_reason = 'PAGE_TOPOLOGY_INVALID';
    return { client: null, status, initPromise: Promise.resolve(), isReady: () => false };
  }

  let client;
  try { client = new ClientClass({
    puppeteer: { browserURL: config.browserDebugUrl },
    userAgent: false,
    takeoverOnConflict: false,
    qrMaxRetries: 0,
    authTimeoutMs: 0,
  }); } catch (error) { preparation.dispose(); throw error; }
  // This process owns only its CDP connection, never the canonical external Page.
  const clientListeners = [];
  const onClient = (event, handler) => {
    clientListeners.push([event, handler]);
    client.on(event, handler);
  };
  const authorityState = createAuthenticatedSelfAuthorityState((reason, generation, timestamp) => {
    status.ready = false;
    status.wwebjs_connected = false;
    status.service_state = 'session_not_authenticated';
    if (reason === 'BROWSER_DISCONNECTED') status.browser_debug_reachable = false;
    status.owner_authority_last_invalidation_reason = reason;
    status.owner_authority_last_invalidation_at = timestamp;
    logger.info?.('owner_authority_invalidated', { reason, generation, timestamp });
  });
  attachOwnerAuthorityStatus(status, config, () => authorityState.describe(client));
  const reverifyIntervalMs = deps.ownerAuthorityReverifyIntervalMs
    || OWNER_AUTHORITY_REVERIFY_INTERVAL_MS;
  const reverifyTimeoutMs = deps.ownerAuthorityReverifyTimeoutMs
    || OWNER_AUTHORITY_REVERIFY_TIMEOUT_MS;
  const scheduleInterval = deps.setInterval || setInterval;
  const cancelInterval = deps.clearInterval || clearInterval;
  let authorityReverifyInFlight = null;
  let authorityReverifyTimer = null;
  authorityCleanup.set(client, () => {
    if (authorityReverifyTimer) cancelInterval(authorityReverifyTimer);
    authorityReverifyTimer = null;
    authorityState.dispose();
    for (const [event, handler] of clientListeners) client.off(event, handler);
    preparation.broker.restore();
    if (!client.pupBrowser) preparation.dispose();
  });
  let stateObservation = 0;
  const invalidateAuthority = (reason) => {
    authorityState.invalidate(reason);
  };

  const setAuthorityUnavailable = (serviceState = 'connected') => {
    status.ready = false;
    status.wwebjs_connected = status.client_state === 'CONNECTED';
    status.service_state = serviceState;
  };

  const ensureOwnerAuthorityCurrent = (trigger = 'maintenance', force = false) => {
    const existing = authorityState.snapshot(client);
    if (!force && authorityState.isSnapshotCurrent(existing, client)) {
      return Promise.resolve({ ready: true, reason: 'CURRENT' });
    }
    if (authorityReverifyInFlight) return authorityReverifyInFlight;
    authorityReverifyInFlight = (async () => {
      const observationAtStart = stateObservation;
      const documentEpochAtStart = authorityState.documentEpoch();
      if (trigger === 'message_create' && status.client_state !== 'CONNECTED') {
        return { ready: false, reason: 'CLIENT_NOT_CONNECTED' };
      }
      const recoveryBlockReason = authorityState.recoveryBlockReason(client);
      if (recoveryBlockReason) return { ready: false, reason: recoveryBlockReason };
      const page = client?.pupPage;
      const browser = client?.pupBrowser;
      if (!page || page.isClosed?.() === true) {
        setAuthorityUnavailable('blocked_page_selection');
        return { ready: false, reason: 'PAGE_CLOSED' };
      }
      if (!browser || browser.isConnected?.() === false) {
        status.browser_debug_reachable = false;
        setAuthorityUnavailable('browser_unavailable');
        return { ready: false, reason: 'BROWSER_DISCONNECTED' };
      }
      if (!canonicalIdentity(client?.info?.wid)) {
        authorityState.suspend();
        setAuthorityUnavailable();
        return { ready: false, reason: 'WID_UNAVAILABLE' };
      }

      const clientState = await boundedOperation(() => client.getState(), reverifyTimeoutMs);
      if (
        stateObservation !== observationAtStart
        || authorityState.documentEpoch() !== documentEpochAtStart
      ) {
        return { ready: false, reason: 'CLIENT_STATE_CHANGED' };
      }
      if (clientState !== 'CONNECTED') {
        setAuthorityUnavailable('session_not_authenticated');
        return { ready: false, reason: 'CLIENT_NOT_CONNECTED' };
      }
      const topology = await boundedOperation(
        async () => selectCanonicalConnectedWhatsAppPage(await client.pupBrowser.pages()),
        reverifyTimeoutMs,
      );
      if (
        stateObservation !== observationAtStart
        || authorityState.documentEpoch() !== documentEpochAtStart
      ) {
        return { ready: false, reason: 'CLIENT_STATE_CHANGED' };
      }
      status.page_count = topology.whatsappPageCount;
      if (topology.result !== 'SELECTED' || topology.page !== client.pupPage) {
        setAuthorityUnavailable('blocked_page_selection');
        return { ready: false, reason: 'PAGE_TOPOLOGY_INVALID' };
      }
      if (!config.ownerIdentityRef) {
        status.client_state = 'CONNECTED';
        status.ready = true;
        status.wwebjs_connected = true;
        status.browser_debug_reachable = true;
        status.service_state = 'ready';
        status.last_ready_at = nowIso();
        return { ready: true, reason: 'NOT_CONFIGURED' };
      }

      let outcome;
      try {
        outcome = await boundedOperation(
          () => verifyCurrentOwnerAuthority(config, client, authorityState, verifyOwner),
          reverifyTimeoutMs,
        );
      } catch (error) {
        invalidateAuthority('PROOF_STALE');
        logger.warn?.('owner_authority_reverify_failed', {
          trigger,
          reason: error?.message === 'STARTUP_OPERATION_TIMEOUT' ? 'TIMEOUT' : 'ERROR',
        });
        return { ready: false, reason: 'VERIFY_FAILED' };
      }
      if (stateObservation !== observationAtStart) {
        return { ready: false, reason: 'CLIENT_STATE_CHANGED' };
      }
      if (
        !outcome.current
        || outcome.error
        || outcome.verification?.result !== 'MATCH'
        || outcome.verification?.identity?.result !== 'RESOLVED'
        || !outcome.authenticatedSelfIdentity
        || !authorityState.isVerificationCurrent(outcome.token, client)
      ) {
        setAuthorityUnavailable();
        return { ready: false, reason: outcome.verification?.result || 'VERIFY_FAILED' };
      }

      const finalState = await boundedOperation(() => client.getState(), reverifyTimeoutMs);
      const finalTopology = await boundedOperation(
        async () => selectCanonicalConnectedWhatsAppPage(await client.pupBrowser.pages()),
        reverifyTimeoutMs,
      );
      status.page_count = finalTopology.whatsappPageCount;
      if (stateObservation !== observationAtStart) {
        return { ready: false, reason: 'CLIENT_STATE_CHANGED' };
      }
      if (
        authorityState.recoveryBlockReason(client)
        || finalState !== 'CONNECTED'
        || finalTopology.result !== 'SELECTED'
        || finalTopology.page !== client.pupPage
        || !authorityState.isVerificationCurrent(outcome.token, client)
        || !authorityState.markReadyConnected(outcome.token, client)
      ) {
        invalidateAuthority('PROOF_STALE');
        setAuthorityUnavailable(finalState === 'CONNECTED' ? 'blocked_page_selection' : 'session_not_authenticated');
        return { ready: false, reason: 'PROOF_STALE' };
      }
      status.client_state = 'CONNECTED';
      status.ready = true;
      status.wwebjs_connected = true;
      status.browser_debug_reachable = true;
      status.service_state = 'ready';
      status.last_ready_at = nowIso();
      logger.info?.('owner_authority_reverified', { trigger });
      return { ready: true, reason: 'MATCH' };
    })().catch((error) => {
      logger.warn?.('owner_authority_reverify_failed', {
        trigger,
        reason: error?.message === 'STARTUP_OPERATION_TIMEOUT' ? 'TIMEOUT' : 'ERROR',
      });
      return { ready: false, reason: 'RECOVERY_FAILED' };
    }).finally(() => {
      authorityReverifyInFlight = null;
    });
    return authorityReverifyInFlight;
  };

  // Both queried and event-driven states follow exactly the same policy.
  const observeClientState = (state, readyProof = null) => {
    stateObservation += 1;
    status.client_state = typeof state === 'string' ? state : 'UNKNOWN';
    if (status.client_state === 'CONNECTED') {
      const ready = readyProof
        ? authorityState.markReadyConnected(readyProof, client)
        : authorityState.restoreConnected(client);
      status.ready = ready;
      status.wwebjs_connected = true;
      status.service_state = ready ? 'ready' : 'connected';
      if (ready) {
        status.browser_debug_reachable = true;
        status.last_ready_at = nowIso();
      }
      return ready;
    }
    if (AUTHORITY_INVALIDATING_STATES.has(status.client_state)) {
      invalidateAuthority('STRONG_CLIENT_STATE');
    } else {
      authorityState.suspend(['OPENING', 'TIMEOUT'].includes(status.client_state));
    }
    status.ready = false;
    status.wwebjs_connected = false;
    status.service_state = status.client_state === 'DISCONNECTED'
      ? 'disconnected' : 'session_not_authenticated';
    if (status.client_state === 'DISCONNECTED') status.browser_debug_reachable = false;
    if (status.client_state === 'SESSION_NOT_AUTHENTICATED') status.qr_seen = true;
    return false;
  };

  onClient('qr', () => {
    invalidateAuthority('QR');
    status.qr_seen = true;
    status.ready = false;
    status.wwebjs_connected = false;
    status.authenticated_event_seen = false;
    status.ready = false;
    status.service_state = 'session_not_authenticated';
    status.client_state = 'SESSION_NOT_AUTHENTICATED';
  });

  onClient('authenticated', async () => {
    status.ready = false;
    const outcome = await verifyCurrentOwnerAuthority(config, client, authorityState, verifyOwner);
    if (!outcome.current || !authorityState.isVerificationCurrent(outcome.token, client)) {
      logger.info?.('owner_authority_verification_discarded', {
        phase: 'authenticated', reason: 'AUTHORITY_GENERATION_STALE',
      });
      return;
    }
    if (outcome.error) {
      status.client_state = 'AUTHENTICATED_IDENTITY_VERIFICATION_FAILED';
      status.service_state = 'error';
      status.ready = false;
      logger.error?.('authenticated identity verification failed', {
        error: String(outcome.error?.message || outcome.error),
      });
      return;
    }
    const verification = outcome.verification;
    const matches = verification.result === 'MATCH';
    if (verification.result === 'UNRESOLVED') {
      status.client_state = 'AUTHENTICATED_IDENTITY_UNRESOLVED';
      status.service_state = 'error';
      status.ready = false;
      return;
    }
    if (!matches && verification.result !== 'NOT_CONFIGURED') {
      status.client_state = 'AUTHENTICATED_IDENTITY_MISMATCH';
      status.service_state = 'error';
      status.ready = false;
      return;
    }
    status.authenticated_event_seen = true;
    status.service_state = 'authenticated';
  });

  onClient('ready', () => { void ensureOwnerAuthorityCurrent('ready', true); });

  onClient('auth_failure', (message) => {
    invalidateAuthority('AUTH_FAILURE');
    status.service_state = 'auth_failure';
    status.client_state = 'AUTH_FAILURE';
    status.ready = false;
    status.wwebjs_connected = false;
    logger.error?.('auth_failure', { message: String(message).slice(0, 160) });
  });

  onClient('change_state', (state) => {
    const restored = observeClientState(state);
    if (state === 'CONNECTED' && !restored) void ensureOwnerAuthorityCurrent('connected');
  });

  onClient('loading_screen', (percent, message) => {
    if (status.ready && status.wwebjs_connected) {
      return;
    }
    status.client_state = `LOADING_${String(percent)}`;
    logger.info?.('loading_screen', { percent });
  });

  onClient('disconnected', (reason) => {
    stateObservation += 1;
    invalidateAuthority('STRONG_CLIENT_STATE');
    status.disconnect_count += 1;
    status.last_disconnect_at = nowIso();
    status.ready = false;
    status.wwebjs_connected = false;
    status.browser_debug_reachable = false;
    status.service_state = 'disconnected';
    status.client_state = safeState(reason) || 'DISCONNECTED';
  });

  authorityReverifyTimer = scheduleInterval(() => {
    if (status.client_state !== 'CONNECTED' || !config.ownerIdentityRef) return;
    const current = authorityState.snapshot(client);
    if (!authorityState.isSnapshotCurrent(current, client)) {
      void ensureOwnerAuthorityCurrent('periodic');
    }
  }, reverifyIntervalMs);
  authorityReverifyTimer.unref?.();

  onClient('message', (message) => {
    if (message?.fromMe) {
      return;
    }
    const messageAuthority = authorityState.snapshot(client);
    resolveConversationIdentity(message, client).then((conversationIdentity) => {
      message.__conversationIdentity = conversationIdentity;
      return inboundBridge.handleMessage(message, {
        clientInfo: client?.info || {},
        authenticatedSelfIdentity: messageAuthority?.identity || null,
        authenticatedSelfAuthorityCurrent: authorityState.isSnapshotCurrent(messageAuthority, client),
      });
    }).then((result) => {
      logger.info?.('inbound_message_observed', {
        status: result.status,
        forwarded: Boolean(result.forwarded),
      });
    }).catch((error) => {
      status.last_inbound_error_at = nowIso();
      logger.error?.('inbound_message_error', { error: String(error?.message || error) });
    }).catch((error) => {
      status.last_inbound_error_at = nowIso();
      logger.error?.('inbound_message_error', { error: String(error?.message || error) });
    });
  });

  onClient('message_create', (message) => {
    if (!message?.fromMe) {
      logger.info?.('message_create_probe', {
        event_name: 'message_create', from_me: false,
        from_kind: kindForId(message?.from), to_kind: kindForId(message?.to),
        chat_id_kind: kindForId(message?.chatId || message?.id?.remote || message?._data?.to),
        authenticated_wid_kind: kindForId(client?.info?.wid),
        same_serialized_id: false, same_user_part_if_safely_comparable: false,
        is_self_chat_result: false, self_chat_rejection_reason: 'NOT_FROM_ME',
        owner_candidate: false, bridge_forward_attempted: false,
      });
      return;
    }
    let messageAuthority = authorityState.snapshot(client);
    const classifyWithAuthority = () => {
      const authorityContext = {
        isCurrent: () => authorityState.isSnapshotCurrent(messageAuthority, client),
      };
      return classifyFinalFromMeMessage(
        message,
        client,
        outboundProvenance,
        messageAuthority?.identity || null,
        authorityContext,
      );
    };
    const classification = authorityState.isSnapshotCurrent(messageAuthority, client)
      ? classifyWithAuthority()
      : ensureOwnerAuthorityCurrent('message_create').then(() => {
        messageAuthority = authorityState.snapshot(client);
        return classifyWithAuthority();
      });
    const classify = classification.then((result) => {
      const currentResult = failClosedStaleOwnerCommand(
        result,
        authorityState.isSnapshotCurrent(messageAuthority, client),
      );
      message.__finalFromMeClassification = currentResult.classification;
      message.__fromMeClassification = currentResult.classification;
      message.__ownerSelfChat = currentResult.classification === 'OWNER_COMMAND';
      message.__authenticatedOwnerSelfChatTarget = currentResult.authenticatedOwnerSelfChat;
      message.__outboundProvenance = currentResult.provenance;
      logger.info?.('message_create_probe', {
        event_name: 'message_create', from_me: true,
        from_kind: kindForId(message?.from), to_kind: kindForId(message?.to),
        chat_id_kind: kindForId(message?.chatId || message?.id?.remote || message?._data?.to),
        authenticated_wid_kind: kindForId(client?.info?.wid),
        same_serialized_id: Boolean(String(message?.from || '') === String(client?.info?.wid || '')),
        same_user_part_if_safely_comparable: Boolean(String(message?.from || '') === String(client?.info?.wid || '')),
        is_self_chat_result: currentResult.authenticatedOwnerSelfChat,
        self_chat_rejection_reason: currentResult.authenticatedOwnerSelfChat ? null : 'AUTH_ID_MISMATCH_OR_TARGET_MISMATCH',
        owner_candidate: currentResult.classification === 'OWNER_COMMAND', bridge_forward_attempted: true,
        final_classification: currentResult.classification,
        classification_reason: currentResult.reason,
      });
      logger.info?.('from_me_classified', {
        classification: currentResult.classification,
        reason: currentResult.reason,
      });
      return currentResult;
    });
    classify.then((result) => {
      const authorityCurrent = authorityState.isSnapshotCurrent(messageAuthority, client);
      const bridgeResult = failClosedStaleOwnerCommand(result, authorityCurrent);
      if (bridgeResult !== result) {
        message.__finalFromMeClassification = bridgeResult.classification;
        message.__fromMeClassification = bridgeResult.classification;
        message.__ownerSelfChat = false;
        message.__authenticatedOwnerSelfChatTarget = false;
        message.__outboundProvenance = bridgeResult.provenance;
      }
      return inboundBridge.handleMessage(message, {
        clientInfo: client?.info || {},
        authenticatedSelfIdentity: messageAuthority?.identity || null,
        authenticatedSelfAuthorityCurrent: authorityCurrent,
      });
    }).then((result) => {
      logger.info?.('inbound_message_observed', { status: result.status, forwarded: Boolean(result.forwarded) });
    }).catch((error) => {
      status.last_inbound_error_at = nowIso();
      logger.error?.('inbound_message_error', { error: String(error?.message || error) });
    });
  });

  const initPromise = Promise.resolve().then(() => client.initialize()).then(() => {
    if (!preparation.broker.consumed()) throw new Error('EXISTING_PAGE_ATTACH_NOT_CONSUMED');
  }).catch((error) => {
    authorityCleanup.get(client)?.();
    authorityCleanup.delete(client);
    status.service_state = 'error';
    status.client_state = 'INITIALIZE_FAILED';
    logger.error?.('initialize failed', { error: String(error?.message || error) });
    preparation.dispose();
    throw error;
  }).finally(() => preparation.broker.restore());

  return {
    client,
    status,
    initPromise,
    isReady: () => isReady(status),
    ensureOwnerAuthorityCurrent,
  };
}

module.exports = {
  authenticatedIdentityMatches,
  authenticatedSelfIdentityFromVerification,
  createAuthenticatedSelfAuthorityState,
  resolveClientIdentity,
  verifyConfiguredOwner,
  verifyCurrentOwnerAuthority,
  OWNER_AUTHORITY_REVERIFY_INTERVAL_MS,
  detachTransport,
  prepareAuthenticatedPage,
  isTrustedWhatsAppPageUrl,
  inspectWhatsAppPageState,
  selectCanonicalConnectedWhatsAppPage,
  armExistingPageAttach,
  waitForBrowserDebug,
  probeBrowserDebugUrl,
  recoverConnectedPage,
  classifyFromMeMessage,
  classifyFinalFromMeMessage,
  startTransport,
};
