const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');
const { createConfig } = require('../src/config');
const {
  inspectWhatsAppPageState, selectCanonicalConnectedWhatsAppPage, prepareAuthenticatedPage,
  armExistingPageAttach, waitForBrowserDebug, startTransport, detachTransport,
} = require('../src/transport');

const logger = { info() {}, warn() {}, error() {} };
const url = 'http://127.0.0.1:9222';
function clock() {
  let time = 0;
  return { now: () => time, sleep: async (ms) => { time += ms; }, timeoutMs: 1000, intervalMs: 250 };
}
class Page extends EventEmitter {
  constructor(state = 'CONNECTED', injected = false) {
    super(); this.state = state; this.injected = injected; this.closed = false;
    this.frame = {}; this.closeCalls = 0;
  }
  mainFrame() { return this.frame; }
  url() { return 'https://web.whatsapp.com/'; }
  isClosed() { return this.closed; }
  async close() { this.closeCalls += 1; this.closed = true; }
  async evaluate(fn) {
    return vm.runInNewContext('(' + fn.toString() + ')()', { window: {
      require: () => ({ Socket: { state: this.state } }),
      ...(this.injected ? { AuthStore: { AppState: { state: 'CONNECTED' } }, WWebJS: {} } : {}),
    } });
  }
}
class Browser extends EventEmitter {
  constructor(pages) { super(); this.pageList = pages; this.newCalls = 0; this.disconnectCalls = 0; }
  async pages() { return this.pageList; }
  async newPage() { this.newCalls += 1; const page = new Page(); this.pageList.push(page); return page; }
  disconnect() { this.disconnectCalls += 1; this.emit('disconnected'); }
  isConnected() { return true; }
}
function fixture(pages = [new Page()]) {
  const browser = new Browser(pages);
  let connectCalls = 0;
  const puppeteerModule = { connect: async () => { connectCalls += 1; return browser; } };
  return { browser, puppeteerModule, connectCalls: () => connectCalls };
}

for (const injected of [false, true]) {
  test(`native CONNECTED page selected with injected=${injected}`, async () => {
    const page = new Page('CONNECTED', injected);
    const result = await selectCanonicalConnectedWhatsAppPage([page]);
    assert.equal(result.result, 'SELECTED');
    assert.equal(result.page, page);
    assert.equal(result.detectionSource, 'WA_WEB_SOCKET_MODEL');
    assert.equal(result.whatsappPageCount, 1);
    assert.equal(result.connectedPageCount, 1);
  });
}
test('native OPENING cannot be overridden by stale injected CONNECTED', async () => {
  const result = await selectCanonicalConnectedWhatsAppPage([new Page('OPENING', true)]);
  assert.equal(result.result, 'NO_CONNECTED_PAGE');
});
test('injected authoritative fallback works when native module is unavailable', async () => {
  const page = new Page();
  page.evaluate = (fn) => Promise.resolve(vm.runInNewContext('(' + fn.toString() + ')()', {
    window: { require() { throw new Error('unavailable'); }, AuthStore: { AppState: { state: 'CONNECTED' } } },
  }));
  assert.equal((await inspectWhatsAppPageState(page)).detectionSource, 'AUTH_STORE');
});
for (const states of [[], ['OPENING'], ['CONNECTED', 'CONNECTED'], ['CONNECTED', 'OPENING']]) {
  test(`selection fails closed for states ${JSON.stringify(states)}`, async () => {
    const f = fixture(states.map(s => new Page(s)));
    const original = f.puppeteerModule.connect;
    const result = await prepareAuthenticatedPage(url, logger, { ...clock(), puppeteerModule: f.puppeteerModule });
    assert.notEqual(result.result, 'SELECTED');
    assert.equal(f.browser.newCalls, 0);
    assert.equal(f.puppeteerModule.connect, original);
    assert.equal(f.browser.disconnectCalls, 1);
  });
}
test('inspection exception fails closed without arming a patch', async () => {
  const page = new Page(); page.evaluate = async () => { throw new Error('synthetic'); };
  const f = fixture([page]);
  const result = await prepareAuthenticatedPage(url, logger, { ...clock(), puppeteerModule: f.puppeteerModule });
  assert.equal(result.result, 'PAGE_INSPECTION_FAILED');
  assert.equal(f.browser.newCalls, 0);
});
test('page wait converges from OPENING to native CONNECTED', async () => {
  const page = new Page('OPENING'); const f = fixture([page]); const c = clock();
  const result = await prepareAuthenticatedPage(url, logger, {
    ...c, puppeteerModule: f.puppeteerModule, sleep: async ms => { await c.sleep(ms); page.state = 'CONNECTED'; },
  });
  assert.equal(result.result, 'SELECTED');
  result.dispose();
});

test('broker reuses exact page and restores connect/newPage after one acquisition', async () => {
  const page = new Page(); const f = fixture([page]);
  const originalConnect = f.puppeteerModule.connect, originalNewPage = f.browser.newPage;
  const result = await prepareAuthenticatedPage(url, logger, { ...clock(), puppeteerModule: f.puppeteerModule });
  const attached = await f.puppeteerModule.connect({ browserURL: url });
  assert.equal(f.puppeteerModule.connect, originalConnect);
  assert.equal(await attached.newPage(), page);
  assert.equal(f.browser.newCalls, 0);
  assert.equal(f.browser.pageList.length, 1);
  assert.equal(f.browser.newPage, originalNewPage);
  assert.equal(Object.hasOwn(f.browser, 'newPage'), false);
  assert.equal(result.broker.consumed(), true);
  await f.puppeteerModule.connect({ browserURL: url });
  assert.equal(f.connectCalls(), 2); // preparation plus second normal connect
  await f.browser.newPage();
  assert.equal(f.browser.newCalls, 1);
});
test('broker revalidates topology at acquisition and restores on inspection error', async () => {
  for (const failure of ['extra-page', 'inspection']) {
    const page = new Page(); const f = fixture([page]);
    const original = f.puppeteerModule.connect, originalNew = f.browser.newPage;
    const broker = armExistingPageAttach(f.puppeteerModule, f.browser, page, url, logger);
    const browser = await f.puppeteerModule.connect({ browserURL: url });
    if (failure === 'extra-page') f.browser.pageList.push(new Page());
    else page.evaluate = async () => { throw new Error('synthetic'); };
    await assert.rejects(browser.newPage(), /CANONICAL_PAGE_SELECTION_CHANGED/);
    assert.equal(broker.consumed(), false);
    assert.equal(f.puppeteerModule.connect, original);
    assert.equal(f.browser.newPage, originalNew);
    assert.equal(f.browser.newCalls, 0);
  }
});
test('unpatchable newPage fails without falling back to creating a page', async () => {
  const f = fixture();
  Object.defineProperty(f.browser, 'newPage', { value: f.browser.newPage, writable: false, configurable: false });
  const original = f.puppeteerModule.connect;
  const broker = armExistingPageAttach(f.puppeteerModule, f.browser, f.browser.pageList[0], url, logger);
  await assert.rejects(f.puppeteerModule.connect({ browserURL: url }), /NEW_PAGE_INTERCEPT_UNAVAILABLE/);
  assert.equal(f.puppeteerModule.connect, original);
  assert.equal(broker.consumed(), false);
  assert.equal(f.browser.newCalls, 0);
});
test('unpatchable connect fails before Client can acquire a new page', () => {
  const f = fixture();
  const original = f.puppeteerModule.connect;
  Object.defineProperty(f.puppeteerModule, 'connect', { value: original, writable: false });
  assert.throws(() => armExistingPageAttach(f.puppeteerModule, f.browser, f.browser.pageList[0], url, logger),
    /CONNECT_INTERCEPT_UNAVAILABLE/);
  assert.equal(f.puppeteerModule.connect, original);
  assert.equal(f.browser.newCalls, 0);
});
for (const phase of ['before-connect', 'before-newPage', 'closed-page']) {
  test(`broker restores both functions after ${phase} failure`, async () => {
    const page = new Page(); const f = fixture([page]);
    const original = f.puppeteerModule.connect, originalNew = f.browser.newPage;
    const broker = armExistingPageAttach(f.puppeteerModule, f.browser, page, url, logger);
    if (phase !== 'before-connect') await f.puppeteerModule.connect({ browserURL: url });
    if (phase === 'closed-page') {
      page.closed = true;
      await assert.rejects(f.browser.newPage(), /CANONICAL_PAGE_UNAVAILABLE/);
    } else broker.restore();
    assert.equal(f.puppeteerModule.connect, original);
    assert.equal(f.browser.newPage, originalNew);
    assert.equal(f.browser.newCalls, 0);
  });
}

test('debug bounded retry recovers after several unavailable attempts', async () => {
  let attempts = 0;
  assert.equal(await waitForBrowserDebug(url, logger, { ...clock(), probe: async () => ++attempts === 3 }), true);
  assert.equal(attempts, 3);
});
test('debug deadline blocks Client construction', async () => {
  let constructions = 0;
  const result = await startTransport(createConfig({ BROWSER_DEBUG_URL: url }), logger, {
    startupWaitOptions: clock(), probeBrowserDebugUrl: async () => false,
    Client: class { constructor() { constructions += 1; } },
  });
  assert.equal(result.status.service_state, 'browser_unavailable');
  assert.equal(result.client, null);
  assert.equal(constructions, 0);
});
for (const failure of [false, { result: 'NO_CONNECTED_PAGE' }, { result: 'MULTIPLE_WHATSAPP_PAGES' }, 'throw']) {
  test(`preparation failure ${JSON.stringify(failure)} blocks construction and initialize`, async () => {
    let constructions = 0;
    const result = await startTransport(createConfig({ BROWSER_DEBUG_URL: url }), logger, {
      probeBrowserDebugUrl: async () => true,
      prepareAuthenticatedPage: async () => { if (failure === 'throw') throw new Error('synthetic'); return failure; },
      Client: class { constructor() { constructions += 1; } },
    });
    assert.equal(result.status.service_state, 'blocked_page_selection');
    assert.equal(result.client, null);
    assert.equal(constructions, 0);
  });
}

for (const failure of ['constructor', 'initialize', 'no-consumption']) {
  test(`startup ${failure} restores actual broker without creating a page`, async () => {
    const f = fixture(); const original = f.puppeteerModule.connect;
    class Client extends EventEmitter {
      constructor() { super(); if (failure === 'constructor') throw new Error('synthetic'); }
      async initialize() { if (failure === 'initialize') throw new Error('synthetic'); }
    }
    const operation = startTransport(createConfig({ BROWSER_DEBUG_URL: url }), logger, {
      Client, probeBrowserDebugUrl: async () => true,
      prepareAuthenticatedPage: (u, l) => prepareAuthenticatedPage(u, l, { ...clock(), puppeteerModule: f.puppeteerModule }),
    });
    if (failure === 'constructor') await assert.rejects(operation);
    else { const result = await operation; await assert.rejects(result.initPromise); }
    assert.equal(f.puppeteerModule.connect, original);
    assert.equal(f.browser.newCalls, 0);
  });
}

test('startup late debug recovery attaches one existing page with no fallback creation', async () => {
  const f = fixture(); let attempts = 0;
  class Client extends EventEmitter {
    async initialize() {
      this.pupBrowser = await f.puppeteerModule.connect({ browserURL: url });
      this.pupPage = await this.pupBrowser.newPage();
    }
  }
  const result = await startTransport(createConfig({ BROWSER_DEBUG_URL: url }), logger, {
    Client, startupWaitOptions: clock(), probeBrowserDebugUrl: async () => ++attempts === 3,
    prepareAuthenticatedPage: (u, l) => prepareAuthenticatedPage(u, l, { ...clock(), puppeteerModule: f.puppeteerModule }),
  });
  await result.initPromise;
  assert.equal(attempts, 3);
  assert.equal(result.client.pupPage, f.browser.pageList[0]);
  assert.equal(f.browser.newCalls, 0);
  await detachTransport(result.client, logger);
  assert.equal(f.browser.pageList[0].closeCalls, 0);
  assert.equal(f.browser.disconnectCalls, 1);
});
