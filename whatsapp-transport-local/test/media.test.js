const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { ALLOWED_MIME_TYPES, atomicStore, decodeBase64Strict } = require('../src/media');
const { loadVoiceMedia } = require('../src/server');

test('voice media store is content-addressed and rejects invalid base64 and size', (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'attention-media-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const bytes = decodeBase64Strict(Buffer.from('voice').toString('base64'), 10);
  const stored = atomicStore(root, bytes);
  assert.equal(stored.digest, crypto.createHash('sha256').update(bytes).digest('hex'));
  assert.equal(stored.target, path.join(root, stored.digest.slice(0, 2), stored.digest));
  assert.throws(() => decodeBase64Strict('not base64!', 10), /INVALID_MEDIA_BASE64/);
  assert.throws(() => decodeBase64Strict(Buffer.alloc(11).toString('base64'), 10), /INVALID_MEDIA_SIZE/);
});

test('Ogg container is supported while standalone Opus remains fail-closed', () => {
  assert.equal(ALLOWED_MIME_TYPES.has('audio/ogg'), true);
  assert.equal(ALLOWED_MIME_TYPES.has('audio/opus'), false);
});

test('last mile loads only verified regular voice media by media_ref', (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'attention-media-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const bytes = Buffer.from('ID3 voice fixture');
  const stored = atomicStore(root, bytes);
  class FakeMessageMedia {
    constructor(mimetype, data) { this.mimetype = mimetype; this.data = data; }
  }
  const media = loadVoiceMedia({ mediaRoot: root, mediaMaxBytes: 1024 }, {
    media_ref: `sha256:${stored.digest}`,
    content_sha256: stored.digest,
    mime_type: 'audio/mpeg',
    size_bytes: bytes.length,
  }, FakeMessageMedia);
  assert.equal(media.mimetype, 'audio/mpeg');
  assert.deepEqual(Buffer.from(media.data, 'base64'), bytes);
  const ogg = Buffer.from('OggS voice fixture');
  const oggStored = atomicStore(root, ogg);
  const oggMedia = loadVoiceMedia({ mediaRoot: root, mediaMaxBytes: 1024 }, {
    media_ref: `sha256:${oggStored.digest}`, content_sha256: oggStored.digest,
    mime_type: 'audio/ogg', size_bytes: ogg.length,
  }, FakeMessageMedia);
  assert.equal(oggMedia.mimetype, 'audio/ogg');
  assert.deepEqual(Buffer.from(oggMedia.data, 'base64'), ogg);
  fs.writeFileSync(stored.target, 'tampered');
  assert.throws(() => loadVoiceMedia({ mediaRoot: root, mediaMaxBytes: 1024 }, {
    media_ref: `sha256:${stored.digest}`, content_sha256: stored.digest,
    mime_type: 'audio/mpeg', size_bytes: bytes.length,
  }, FakeMessageMedia), /VOICE_MEDIA_FILE_INVALID|VOICE_MEDIA_HASH_MISMATCH/);
});
