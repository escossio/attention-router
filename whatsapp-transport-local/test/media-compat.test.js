const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { captureInboundMedia, captureVoiceMedia } = require('../src/media');
const { resolveMessageLookupId } = require('../src/message-id');

function fixture(t, overrides = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'media-compat-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const spool = path.join(root, 'spool');
  return {
    hmacSecret: 'synthetic-media-secret', inboundHttpTimeoutMs: 100,
    mediaRoot: path.join(root, 'media'), mediaMaxBytes: 5 * 1024 * 1024,
    artifactMediaMaxBytes: 32 * 1024 * 1024,
    mediaDownloadTimeoutMs: 30, mediaNotificationUrl: 'http://127.0.0.1/media',
    mediaNotificationPendingDir: path.join(spool, 'pending'), mediaNotificationSendingDir: path.join(spool, 'sending'),
    mediaNotificationSentDir: path.join(spool, 'sent'), mediaNotificationQuarantineDir: path.join(spool, 'quarantine'),
    ...overrides,
  };
}

function normalized(overrides = {}) {
  return {
    tenant_id: '00000000-0000-4000-8000-000000000001',
    source: 'wwebjs', external_event_id: 'synthetic', message_type: 'ptt',
    identity: { idempotency_key: 'synthetic' }, ...overrides,
  };
}

function message(id, downloadMedia) {
  return { id, client: {}, hasMedia: true, fromMe: false, type: 'ptt', downloadMedia };
}

function okFetch() { return async () => ({ status: 202, text: async () => '{"status":"accepted"}' }); }

test('DOLLAR1 shim calls download once and does not mutate original', async (t) => {
  const id = { fromMe: false, remote: 'synthetic@lid', id: 'ABC', $1: 'false_synthetic@lid_ABC' };
  let calls = 0; let seen;
  const original = message(id, async function downloadMedia() { calls += 1; seen = this.id._serialized; return { mimetype: 'audio/ogg', data: Buffer.from('ogg').toString('base64') }; });
  const result = await captureVoiceMedia(fixture(t), original, normalized(), { info() {}, warn() {} }, okFetch());
  assert.equal(result.status, 'media_ready_notified'); assert.equal(calls, 1); assert.equal(seen, id.$1); assert.equal(id._serialized, undefined);
});

test('reconstructs direct @lid and legacy serialized takes priority', () => {
  assert.deepEqual(resolveMessageLookupId({ fromMe: false, remote: 'x@lid', id: 'i' }), { lookupId: 'false_x@lid_i', strategy: 'RECONSTRUCTED_DIRECT' });
  assert.deepEqual(resolveMessageLookupId({ fromMe: false, remote: 'x@lid', id: 'i', $1: 'legacy' }), { lookupId: 'legacy', strategy: 'DOLLAR1' });
  assert.deepEqual(resolveMessageLookupId({ fromMe: false, remote: 'contact038@example.com', id: 'i' }), { lookupId: null, strategy: 'UNAVAILABLE' });
});

test('reconstructed direct id is supplied once and groups fail closed', async (t) => {
  let seen; let calls = 0;
  const direct = message({ fromMe: false, remote: 'x@lid', id: 'i' }, async function () {
    calls += 1; seen = this.id._serialized;
    return { mimetype: 'audio/ogg', data: Buffer.from('voice').toString('base64') };
  });
  const directResult = await captureVoiceMedia(fixture(t), direct, normalized(), { info() {}, warn() {} }, okFetch());
  assert.equal(directResult.status, 'media_ready_notified'); assert.equal(seen, 'false_x@lid_i'); assert.equal(calls, 1);
  let groupCalls = 0;
  const group = message({ fromMe: false, remote: 'contact038@example.com', id: 'i' }, async () => { groupCalls += 1; });
  const groupResult = await captureVoiceMedia(fixture(t), group, normalized(), { info() {}, warn() {} }, okFetch());
  assert.equal(groupResult.status, 'media_capture_failed'); assert.equal(groupCalls, 0);
});

test('rejection, timeout and empty are classified without retry', async (t) => {
  for (const [downloadMedia, expected, timeout] of [
    [async function () { throw new Error('boom'); }, 'DOWNLOAD_REJECTED', 30],
    [() => new Promise(() => {}), 'DOWNLOAD_TIMEOUT', 10],
    [async () => undefined, 'DOWNLOAD_EMPTY', 30],
  ]) {
    let calls = 0; const logs = [];
    const msg = message({ fromMe: false, remote: 'x@c.us', id: 'i' }, async function () { calls += 1; return downloadMedia.apply(this, arguments); });
    const result = await captureVoiceMedia(fixture(t, { mediaDownloadTimeoutMs: timeout }), msg, normalized(), { info() {}, warn(event, data) { logs.push({ event, data }); } }, okFetch());
    assert.equal(calls, 1); assert.equal(result.status, 'media_capture_failed'); assert.equal(logs.at(-1).data.failure_class, expected);
  }
});

test('complete compatible media path reaches READY', async (t) => {
  const msg = message({ fromMe: false, remote: 'x@lid', id: 'i', $1: 'false_x@lid_i' }, async () => ({ mimetype: 'audio/ogg', data: Buffer.from('voice').toString('base64') }));
  const result = await captureVoiceMedia(fixture(t), msg, normalized(), { info() {}, warn() {} }, okFetch());
  assert.equal(result.status, 'media_ready_notified');
});

test('generic image capture uses Artifact bound and preserves filename metadata', async (t) => {
  const bodies = [];
  const config = fixture(t, {
    mediaMaxBytes: 2,
    artifactMediaMaxBytes: 1024,
  });
  const bytes = Buffer.from('synthetic image bytes');
  const msg = message(
    { fromMe: false, remote: 'x@lid', id: 'img', $1: 'false_x@lid_img' },
    async () => ({
      mimetype: 'image/png',
      filename: '../../photo.png',
      data: bytes.toString('base64'),
    }),
  );
  msg.type = 'image';
  const result = await captureInboundMedia(
    config,
    msg,
    normalized({ message_type: 'image' }),
    { info() {}, warn() {} },
    async (_url, options) => {
      bodies.push(JSON.parse(Buffer.from(options.body).toString('utf8')));
      return { status: 202, text: async () => '{"status":"accepted"}' };
    },
  );

  assert.equal(result.status, 'media_ready_notified');
  assert.equal(bodies.length, 1);
  assert.equal(bodies[0].tenant_id, normalized().tenant_id);
  assert.equal(bodies[0].mime_type, 'image/png');
  assert.equal(bodies[0].original_filename, '../../photo.png');
  assert.equal(bodies[0].size_bytes, bytes.length);
  assert.equal(bodies[0].capture_status, 'READY');
});

test('generic document capture rejects malformed MIME without fabricating READY', async (t) => {
  const bodies = [];
  const msg = message(
    { fromMe: false, remote: 'x@lid', id: 'doc', $1: 'false_x@lid_doc' },
    async () => ({
      mimetype: 'not-a-mime',
      filename: 'report.bin',
      data: Buffer.from('doc').toString('base64'),
    }),
  );
  msg.type = 'document';
  const result = await captureInboundMedia(
    fixture(t),
    msg,
    normalized({ message_type: 'document' }),
    { info() {}, warn() {} },

    async (_url, options) => {
      bodies.push(JSON.parse(Buffer.from(options.body).toString('utf8')));
      return { status: 202, text: async () => '{"status":"accepted"}' };
    },
  );
  assert.equal(result.status, 'media_capture_failed');
  assert.equal(bodies.length, 1);
  assert.equal(bodies[0].capture_status, 'FAILED');
  assert.equal(bodies[0].media_ref, undefined);
  assert.equal(bodies[0].content_sha256, undefined);
});
