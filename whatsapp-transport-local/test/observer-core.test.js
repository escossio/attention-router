const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');

const {
  OBSERVER_CONSOLE_PREFIX,
  OBSERVER_PAGE_VERSION,
  ObserverService,
  cleanupExpiredLogs,
  createObserverConfig,
  createObserverStatus,
  dailyLogFileName,
  normalizeObserverEvent,
  pageObserverInstaller,
  selectSingleConnected,
  serializeStatus,
  sha256,
} = require('../src/observer-core');

function config(overrides = {}) {
  return {
    ...createObserverConfig({
      ATTENTION_WHATSAPP_OBSERVER_OUTPUT_DIR: '/tmp/attention-observer-test',
      ATTENTION_WHATSAPP_OBSERVER_SOURCE_SHA: 'test-revision',
    }),
    ...overrides,
  };
}

function rawEvent(overrides = {}) {
  return {
    collection_event: 'add',
    message_id: 'true_123@c.us_ABC',
    from_me: true,
    remote: '123@lid',
    from: '123@c.us',
    to: '123@lid',
    type: 'chat',
    ack: 3,
    is_new_msg: true,
    timestamp: 1788566400,
    body: 'diagnostic body',
    caption: 'diagnostic caption',
    wwebjs_model_available: true,
    wwebjs_model: {
      id: 'true_123@c.us_ABC',
      fromMe: true,
      from: '123@c.us',
      to: '123@lid',
      body: 'diagnostic body',
      type: 'chat',
      ack: 3,
      t: 1788566400,
      isNewMsg: true,
      ignored_giant_object: { media: Buffer.alloc(32) },
    },
    ...overrides,
  };
}

test('exactly one CONNECTED WhatsApp page is selected', () => {
  const pages = [
    { appState: 'OPENING', page: { id: 1 } },
    { appState: 'CONNECTED', page: { id: 2 } },
  ];
  const result = selectSingleConnected(pages);
  assert.equal(result.blocked, false);
  assert.equal(result.selected, pages[1]);
  assert.equal(result.connectedPageCount, 1);
});

test('zero CONNECTED WhatsApp pages blocks selection', () => {
  const result = selectSingleConnected([{ appState: 'OPENING' }]);
  assert.equal(result.blocked, true);
  assert.equal(result.reason, 'ZERO_CONNECTED_PAGES');
  assert.equal(result.selected, null);
});

test('more than one CONNECTED WhatsApp page blocks selection', () => {
  const result = selectSingleConnected([
    { appState: 'CONNECTED' },
    { appState: 'CONNECTED' },
  ]);
  assert.equal(result.blocked, true);
  assert.equal(result.reason, 'MULTIPLE_CONNECTED_PAGES');
  assert.equal(result.connectedPageCount, 2);
});

test('observer reuses a Puppeteer browser exposing connected property', async () => {
  let connections = 0;
  const browser = { connected: true, once() {} };
  const service = new ObserverService({
    config: config(),
    puppeteer: { async connect() { connections += 1; return browser; } },
    logger: { log() {} },
  });
  assert.equal(await service.connectBrowser(), true);
  assert.equal(await service.connectBrowser(), true);
  assert.equal(connections, 1);
});

test('add events are normalized to the bounded diagnostic schema', () => {
  const record = normalizeObserverEvent(rawEvent(), config(), new Date('2026-09-05T12:00:00Z'));
  assert.equal(record.schema_version, 1);
  assert.equal(record.collection_event, 'add');
  assert.equal(record.message_id_hash, sha256('true_123@c.us_ABC'));
  assert.equal(record.from_me, true);
  assert.equal(record.wwebjs_model.id, 'true_123@c.us_ABC');
  assert.equal('ignored_giant_object' in record.wwebjs_model, false);
});

test('change:* events preserve the specific collection event and field', () => {
  const record = normalizeObserverEvent(rawEvent({
    collection_event: 'change:ack',
    change_field: 'ack',
    change_value: 3,
  }), config());
  assert.equal(record.collection_event, 'change:ack');
  assert.equal(record.change_field, 'ack');
  assert.equal(record.change_value, 3);
});

test('body capture true persists bounded clear text and hashes', () => {
  const record = normalizeObserverEvent(rawEvent(), config({ captureBody: true }));
  assert.equal(record.body, 'diagnostic body');
  assert.equal(record.caption, 'diagnostic caption');
  assert.equal(record.body_hash, sha256('diagnostic body'));
  assert.equal(record.body_length, 15);
});

test('body capture false keeps hashes and lengths without clear text', () => {
  const record = normalizeObserverEvent(rawEvent(), config({ captureBody: false }));
  assert.equal(record.body, null);
  assert.equal(record.caption, null);
  assert.equal(record.wwebjs_model.body, null);
  assert.equal(record.body_hash, sha256('diagnostic body'));
  assert.equal(record.body_length, 15);
  assert.equal(record.caption_hash, sha256('diagnostic caption'));
  assert.equal(record.caption_length, 18);
});

test('body and caption are truncated to the configured character limit', () => {
  const record = normalizeObserverEvent(
    rawEvent({ body: 'abcdef', caption: 'uvwxyz' }),
    config({ captureBody: true, bodyMaxChars: 4 }),
  );
  assert.equal(record.body, 'abcd');
  assert.equal(record.caption, 'uvwx');
  assert.equal(record.wwebjs_model.body, 'diag');
  assert.equal(record.body_length, 6);
  assert.equal(record.body_hash, sha256('abcdef'));
});

test('daily JSONL filename uses UTC consistently', () => {
  assert.equal(dailyLogFileName(new Date('2026-09-05T23:59:59-03:00')), 'events-2026-09-06.jsonl');
});

test('retention removes only expired events-*.jsonl regular files', async (t) => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'wa-observer-retention-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const expired = path.join(directory, 'events-2026-09-01.jsonl');
  const current = path.join(directory, 'events-2026-09-05.jsonl');
  const unrelated = path.join(directory, 'notes.jsonl');
  await Promise.all([
    fs.writeFile(expired, '{}\n'),
    fs.writeFile(current, '{}\n'),
    fs.writeFile(unrelated, '{}\n'),
  ]);
  const now = new Date('2026-09-05T12:00:00Z');
  await fs.utimes(expired, new Date('2026-09-01T00:00:00Z'), new Date('2026-09-01T00:00:00Z'));
  await fs.utimes(current, now, now);
  await fs.utimes(unrelated, new Date('2026-09-01T00:00:00Z'), new Date('2026-09-01T00:00:00Z'));

  const removed = await cleanupExpiredLogs(directory, 72, now.getTime());
  assert.deepEqual(removed, [expired]);
  assert.equal(await fs.readFile(current, 'utf8'), '{}\n');
  assert.equal(await fs.readFile(unrelated, 'utf8'), '{}\n');
});

test('status serialization exposes the stable operational contract', () => {
  const status = createObserverStatus(config({ captureBody: false, retentionHours: 48 }));
  status.service_state = 'READY';
  status.browser_connected = true;
  status.whatsapp_page_count = 1;
  status.connected_page_count = 1;
  status.listener_attached = true;
  status.app_state = 'CONNECTED';
  const serialized = serializeStatus(status, new Date('2026-09-05T12:00:00Z'));
  assert.deepEqual(
    {
      state: serialized.service_state,
      browser: serialized.browser_connected,
      pages: serialized.whatsapp_page_count,
      connected: serialized.connected_page_count,
      listener: serialized.listener_attached,
      capture: serialized.capture_body,
      retention: serialized.retention_hours,
    },
    {
      state: 'READY',
      browser: true,
      pages: 1,
      connected: 1,
      listener: true,
      capture: false,
      retention: 48,
    },
  );
});

test('compatible page marker is reused without adding a duplicate listener', async (t) => {
  const previousWindow = global.window;
  t.after(() => { global.window = previousWindow; });
  let onCalls = 0;
  let offCalls = 0;
  const Msg = {
    length: 4,
    on(event, handler) {
      assert.equal(event, 'all');
      onCalls += 1;
      this.handler = handler;
    },
    off() { offCalls += 1; },
  };
  global.window = {
    require(name) {
      if (name === 'WAWebCollections') return { Msg };
      if (name === 'WAWebUserPrefsMeUser') return {};
      throw new Error('module unavailable');
    },
    AuthStore: { AppState: { state: 'CONNECTED' } },
    WWebJS: {},
    location: { href: 'https://web.whatsapp.com/' },
  };
  const options = {
    version: OBSERVER_PAGE_VERSION,
    prefix: OBSERVER_CONSOLE_PREFIX,
    captureBody: true,
    bodyMaxChars: 4096,
  };
  const first = await pageObserverInstaller(options);
  const second = await pageObserverInstaller(options);
  assert.equal(first.reused, false);
  assert.equal(second.reused, true);
  assert.equal(onCalls, 1);
  assert.equal(offCalls, 0);
});

test('shutdown disconnects only the Puppeteer connection', async (t) => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'wa-observer-shutdown-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  let disconnectCalls = 0;
  let browserCloseCalls = 0;
  let pageCloseCalls = 0;
  let pageOffCalls = 0;
  const service = new ObserverService({
    config: config({ outputDir: directory }),
    puppeteer: {},
    logger: { log() {}, warn() {}, error() {} },
  });
  service.browser = {
    isConnected: () => true,
    disconnect: () => { disconnectCalls += 1; },
    close: () => { browserCloseCalls += 1; },
  };
  service.selectedPage = {
    off: () => { pageOffCalls += 1; },
    close: () => { pageCloseCalls += 1; },
  };
  await service.shutdown();
  assert.equal(disconnectCalls, 1);
  assert.equal(browserCloseCalls, 0);
  assert.equal(pageCloseCalls, 0);
  assert.equal(pageOffCalls, 1);
});

test('observer runtime has no send, Router forwarding, or page lifecycle calls', async () => {
  const sourceDirectory = path.join(__dirname, '..', 'src');
  const runtime = await Promise.all([
    fs.readFile(path.join(sourceDirectory, 'observer.js'), 'utf8'),
    fs.readFile(path.join(sourceDirectory, 'observer-core.js'), 'utf8'),
  ]).then((parts) => parts.join('\n'));
  const forbidden = [
    ['send', 'Message('].join(''),
    ['onAdd', 'MessageEvent('].join(''),
    ['inboundBridge', '.handleMessage('].join(''),
    ['page', '.close('].join(''),
    ['browser', '.close('].join(''),
    ['browser', '.newPage('].join(''),
    ['page', '.reload('].join(''),
  ];
  for (const call of forbidden) assert.equal(runtime.includes(call), false, call);
});
