const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { createInboundBridge } = require('../src/bridge');
const { drainMediaNotifications } = require('../src/media');
const {
  drainInboundPending,
  enqueuePending,
  listPending,
  startInboundPendingDrain,
} = require('../src/spool');

const logger = { info() {}, warn() {}, error() {} };

function response(status, payload = {}) {
  return {
    status,
    text: async () => JSON.stringify(payload),
  };
}

function tempConfig(t, overrides = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'attention-ingress-durable-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const spool = path.join(root, 'spool');
  return {
    root,
    config: {
      inboundSpoolDir: spool,
      inboundPendingDir: path.join(spool, 'pending'),
      inboundSendingDir: path.join(spool, 'sending'),
      inboundSentDir: path.join(spool, 'sent'),
      inboundQuarantineDir: path.join(spool, 'quarantine'),
      inboundForwardEnabled: true,
      inboundForwardUrl: 'http://127.0.0.1:18102/api/v1/ingress/internal/events',
      inboundForwardHosts: ['127.0.0.1'],
      inboundHttpTimeoutMs: 200,
      inboundSource: 'wwebjs',
      inboundChannel: 'whatsapp',
      sourceAccount: 'default',
      hmacSecret: 'unit-test-inbound-secret-32-bytes-minimum',
      mediaRoot: path.join(root, 'media'),
      mediaMaxBytes: 5 * 1024 * 1024,
      mediaDownloadTimeoutMs: 200,
      mediaNotificationUrl: 'http://127.0.0.1:18102/internal/whatsapp/media',
      mediaNotificationPendingDir: path.join(spool, 'media-notifications', 'pending'),
      mediaNotificationSendingDir: path.join(spool, 'media-notifications', 'sending'),
      mediaNotificationSentDir: path.join(spool, 'media-notifications', 'sent'),
      mediaNotificationQuarantineDir: path.join(spool, 'media-notifications', 'quarantine'),
      ...overrides,
    },
  };
}

function mediaSpoolConfig(config) {
  return {
    ...config,
    inboundForwardUrl: config.mediaNotificationUrl,
    inboundPendingDir: config.mediaNotificationPendingDir,
    inboundSendingDir: config.mediaNotificationSendingDir,
    inboundSentDir: config.mediaNotificationSentDir,
    inboundQuarantineDir: config.mediaNotificationQuarantineDir,
  };
}

function voiceMessage(downloadMedia, id = 'wamid.voice.outage') {
  return {
    id: { _serialized: id, remote: '5500000000029@c.us' },
    from: '5500000000029@c.us',
    fromMe: false,
    type: 'ptt',
    body: '',
    hasMedia: true,
    timestamp: 1788652800,
    downloadMedia,
  };
}

function normalizedText(id) {
  return {
    schema_version: '1',
    source: 'wwebjs',
    external_event_id: id,
    event_type: 'message',
    message_type: 'chat',
    content: 'Fixture text.',
    has_media: false,
    identity: { idempotency_key: `wwebjs:${id}` },
  };
}

function waitFor(predicate, timeoutMs = 500) {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + timeoutMs;
    const timer = setInterval(() => {
      if (predicate()) {
        clearInterval(timer);
        resolve();
      } else if (Date.now() >= deadline) {
        clearInterval(timer);
        reject(new Error('WAIT_FOR_TIMEOUT'));
      }
    }, 5);
  });
}

test('voice capture survives ingress 503 and recovers without a second download', async (t) => {
  const { config } = tempConfig(t);
  const bytes = Buffer.from('OggS durable voice fixture');
  const digest = crypto.createHash('sha256').update(bytes).digest('hex');
  let downloadCount = 0;
  let recovered = false;
  const inboundBodies = [];
  const mediaBodies = [];
  const fetchImpl = async (url, options) => {
    const body = JSON.parse(Buffer.from(options.body).toString('utf8'));
    if (url === config.mediaNotificationUrl) {
      mediaBodies.push(body);
      return recovered
        ? response(202, { status: 'accepted' })
        : response(425, { status: 'inbound_not_committed' });
    }
    inboundBodies.push(body);
    return recovered
      ? response(202, { status: 'accepted' })
      : response(503, { status: 'unavailable' });
  };
  const bridge = createInboundBridge(config, logger, { fetchImpl });

  const result = await bridge.handleMessage(voiceMessage(async () => {
    downloadCount += 1;
    return { mimetype: 'audio/ogg; codecs=opus', data: bytes.toString('base64') };
  }));

  assert.equal(result.status, 'retry');
  assert.equal(downloadCount, 1);
  assert.equal(listPending(config).length, 1);
  assert.equal(listPending(mediaSpoolConfig(config)).length, 1);
  assert.equal(fs.existsSync(path.join(config.mediaRoot, digest.slice(0, 2), digest)), true);
  assert.equal(mediaBodies[0].external_event_id, inboundBodies[0].external_event_id);
  assert.equal(mediaBodies[0].content_sha256, digest);

  recovered = true;
  const inboundRecovery = await drainInboundPending(config, logger, fetchImpl);
  const mediaRecovery = await drainMediaNotifications(config, logger, fetchImpl);
  assert.equal(inboundRecovery.delivered, 1);
  assert.equal(mediaRecovery, 1);
  assert.equal(listPending(config).length, 0);
  assert.equal(listPending(mediaSpoolConfig(config)).length, 0);
  assert.equal(downloadCount, 1);
  assert.equal(inboundBodies.at(-1).external_event_id, mediaBodies.at(-1).external_event_id);
  assert.equal(mediaBodies.at(-1).content_sha256, digest);
});

test('inbound delivery starts without waiting for voice download', async (t) => {
  const { config } = tempConfig(t);
  let inboundStarted = false;
  let downloadStarted = false;
  let releaseDownload;
  const download = new Promise((resolve) => { releaseDownload = resolve; });
  const bridge = createInboundBridge(config, logger, {
    fetchImpl: async (url) => {
      if (url === config.inboundForwardUrl) inboundStarted = true;
      return url === config.inboundForwardUrl
        ? response(503)
        : response(425);
    },
  });

  const handling = bridge.handleMessage(voiceMessage(() => {
    downloadStarted = true;
    return download;
  }, 'wamid.voice.concurrent'));
  assert.equal(inboundStarted, true);
  assert.equal(downloadStarted, true);
  releaseDownload({
    mimetype: 'audio/ogg',
    data: Buffer.from('OggS concurrent').toString('base64'),
  });
  await handling;
});

test('startup drain recovers an inbound file that predates drainer initialization', async (t) => {
  const { config } = tempConfig(t);
  enqueuePending(config, normalizedText('wamid.text.startup'));
  assert.equal(listPending(config).length, 1);
  let calls = 0;

  const runtimeDrain = startInboundPendingDrain(config, logger, async () => {
    calls += 1;
    return response(202, { status: 'accepted' });
  }, 20);
  t.after(() => runtimeDrain.stop());
  const startup = await runtimeDrain.startupPromise;

  assert.equal(startup.delivered, 1);
  assert.equal(calls, 1);
  assert.equal(listPending(config).length, 0);
});

test('background drain automatically recovers text inbound after outage', async (t) => {
  const { config } = tempConfig(t);
  const bridge = createInboundBridge(config, logger, {
    fetchImpl: async () => response(503),
  });
  const first = await bridge.handleMessage({
    id: { _serialized: 'wamid.text.background', remote: '5500000000029@c.us' },
    from: '5500000000029@c.us',
    fromMe: false,
    type: 'chat',
    body: 'Fixture text.',
    hasMedia: false,
    timestamp: 1788652800,
  });
  assert.equal(first.status, 'retry');
  assert.equal(listPending(config).length, 1);
  let attempts = 0;
  let accepted = 0;
  const runtimeDrain = startInboundPendingDrain(config, logger, async () => {
    attempts += 1;
    if (attempts === 1) return response(503);
    accepted += 1;
    return response(202, { status: 'accepted' });
  }, 15);
  t.after(() => runtimeDrain.stop());

  await runtimeDrain.startupPromise;
  await waitFor(() => listPending(config).length === 0);
  assert.equal(attempts, 2);
  assert.equal(accepted, 1);
});

test('handleMessage and background drain share one file delivery flight', async (t) => {
  const { config } = tempConfig(t);
  let fetchCalls = 0;
  let releasePost;
  const post = new Promise((resolve) => { releasePost = resolve; });
  const fetchImpl = async () => {
    fetchCalls += 1;
    return post;
  };
  const bridge = createInboundBridge(config, logger, { fetchImpl });
  const handling = bridge.handleMessage({
    id: { _serialized: 'wamid.text.single-flight', remote: '5500000000029@c.us' },
    from: '5500000000029@c.us',
    fromMe: false,
    type: 'chat',
    body: 'Fixture text.',
    hasMedia: false,
    timestamp: 1788652800,
  });
  assert.equal(fetchCalls, 1);
  const draining = drainInboundPending(config, logger, fetchImpl);
  assert.equal(fetchCalls, 1);
  releasePost(response(202, { status: 'accepted' }));

  const [handled, drained] = await Promise.all([handling, draining]);
  assert.equal(handled.status, 'delivered');
  assert.equal(drained.delivered, 1);
  assert.equal(fetchCalls, 1);
  assert.equal(listPending(config).length, 0);
});

test('auth failure stops automatic drain and preserves every pending file', async (t) => {
  for (const status of [401, 403]) {
    const { config } = tempConfig(t);
    enqueuePending(config, normalizedText(`wamid.auth.${status}.a`));
    enqueuePending(config, normalizedText(`wamid.auth.${status}.b`));
    let calls = 0;
    const runtimeDrain = startInboundPendingDrain(config, logger, async () => {
      calls += 1;
      return response(status);
    }, 10);
    t.after(() => runtimeDrain.stop());

    const startup = await runtimeDrain.startupPromise;
    await new Promise((resolve) => setTimeout(resolve, 35));
    assert.equal(startup.status, 'auth_failed');
    assert.equal(runtimeDrain.isAuthFailed(), true);
    assert.equal(calls, 1);
    assert.equal(listPending(config).length, 2);
  }
});

test('disabled forwarding and invalid target never start media capture', async (t) => {
  for (const overrides of [
    { inboundForwardEnabled: false },
    { inboundForwardUrl: 'http://example.invalid/inbound' },
  ]) {
    const { config } = tempConfig(t, overrides);
    let downloads = 0;
    const bridge = createInboundBridge(config, logger, {
      fetchImpl: async () => {
        throw new Error('fetch must not run');
      },
    });
    const result = await bridge.handleMessage(voiceMessage(async () => {
      downloads += 1;
      return { mimetype: 'audio/ogg', data: 'T2dnUw==' };
    }, `wamid.voice.blocked.${overrides.inboundForwardEnabled === false ? 'off' : 'target'}`));

    assert.match(result.status, /^blocked/);
    assert.equal(downloads, 0);
    assert.equal(fs.existsSync(config.mediaRoot), false);
  }
});
