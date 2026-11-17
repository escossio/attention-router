const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const { deliverPendingFile, enqueuePending, listPending } = require('./spool');
const { stableHash } = require('./hmac');
const captureFlights = new Map();

const { resolveMessageLookupId } = require('./message-id');

const ALLOWED_MIME_TYPES = new Set(['audio/ogg', 'audio/mpeg', 'audio/mp4']);
const GENERIC_MIME_RE = /^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}\/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$/i;

function isVoiceKind(value) {
  return ['ptt', 'audio'].includes(String(value || '').toLowerCase());
}

function decodeBase64Strict(value, maxBytes) {
  if (typeof value !== 'string' || value.length === 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(value)) {
    throw new Error('INVALID_MEDIA_BASE64');
  }
  const bytes = Buffer.from(value, 'base64');
  const canonical = value.replace(/=+$/, '');
  if (bytes.toString('base64').replace(/=+$/, '') !== canonical) throw new Error('INVALID_MEDIA_BASE64');
  if (!bytes.length || bytes.length > maxBytes) throw new Error('INVALID_MEDIA_SIZE');
  return bytes;
}

function atomicStore(root, bytes) {
  const digest = crypto.createHash('sha256').update(bytes).digest('hex');
  const directory = path.join(root, digest.slice(0, 2));
  const target = path.join(directory, digest);
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  if (fs.existsSync(target)) {
    const info = fs.lstatSync(target);
    if (!info.isFile() || info.isSymbolicLink()) throw new Error('MEDIA_TARGET_NOT_REGULAR');
    const existing = fs.readFileSync(target);
    if (existing.length !== bytes.length || crypto.createHash('sha256').update(existing).digest('hex') !== digest) {
      throw new Error('MEDIA_HASH_MISMATCH');
    }
    return { digest, target };
  }
  const temporary = path.join(directory, `.${digest}.${process.pid}.${Date.now()}.tmp`);
  const fd = fs.openSync(temporary, 'wx', 0o600);
  try {
    fs.writeFileSync(fd, bytes);
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
  fs.renameSync(temporary, target);
  return { digest, target };
}

function mediaNotificationConfig(config) {
  return {
    ...config,
    inboundForwardUrl: config.mediaNotificationUrl,
    inboundPendingDir: config.mediaNotificationPendingDir,
    inboundSendingDir: config.mediaNotificationSendingDir,
    inboundSentDir: config.mediaNotificationSentDir,
    inboundQuarantineDir: config.mediaNotificationQuarantineDir,
  };
}

async function notify(config, normalized, payload, logger, fetchImpl) {
  const spoolConfig = mediaNotificationConfig(config);
  const envelope = {
    identity: { idempotency_key: `${normalized.identity.idempotency_key}:media` },
  };
  const pending = enqueuePending(spoolConfig, envelope, payload);
  return deliverPendingFile(spoolConfig, pending.file, logger, fetchImpl);
}

async function captureMediaAttempt(config, message, normalized, logger = console, fetchImpl = fetch, checkpoint = () => {}) {
  if (!config.mediaRoot || !config.mediaNotificationUrl) return { status: 'media_capture_disabled' };
  const base = {
    tenant_id: normalized.tenant_id,
    source: normalized.source,
    external_event_id: normalized.external_event_id,
    media_kind: normalized.message_type,
  };
  const resolvedId = resolveMessageLookupId(message?.id);
  let downloadPhase = true;
  let readyCheckpointed = false;
  const failureClass = (error) => {
    if (resolvedId.strategy === 'UNAVAILABLE') return 'MESSAGE_ID_UNAVAILABLE';
    if (error?.message === 'MEDIA_DOWNLOAD_TIMEOUT') return 'DOWNLOAD_TIMEOUT';
    if (error?.message === 'DOWNLOAD_EMPTY') return 'DOWNLOAD_EMPTY';
    if (!downloadPhase) return 'UNKNOWN';
    if (error) return 'DOWNLOAD_REJECTED';
    return 'UNKNOWN';
  };
  try {
    if (!resolvedId.lookupId) throw new Error('MESSAGE_ID_UNAVAILABLE');
    logger.info?.('voice_media_download_started', {
      media_kind: normalized.message_type,
      id_strategy: resolvedId.strategy,
    });
    const compatReceiver = Object.create(message);
    compatReceiver.id = { ...(message.id || {}), _serialized: resolvedId.lookupId };
    let timeout;
    const timeoutPromise = new Promise((_, reject) => {
      timeout = setTimeout(() => reject(new Error('MEDIA_DOWNLOAD_TIMEOUT')), config.mediaDownloadTimeoutMs);
    });
    let media;
    let active = true;
    try {
      // Retry only rejected downloads. A timed-out provider call cannot be
      // canceled safely, so never overlap it with another download.
      const download = async () => {
        for (;;) {
          if (!active) throw new Error('MEDIA_DOWNLOAD_TIMEOUT');
          checkpoint();
          try { return await message.downloadMedia.call(compatReceiver); }
          catch (error) {
            if (!['ECONNRESET', 'EAI_AGAIN', 'ETIMEDOUT', 'MEDIA_TEMPORARILY_UNAVAILABLE'].includes(error?.code)) throw error;
          }
        }
      };
      media = await Promise.race([download(), timeoutPromise]);
    } finally {
      active = false;
      if (timeout) clearTimeout(timeout);
    }
    if (!media) throw new Error('DOWNLOAD_EMPTY');
    logger.info?.('voice_media_download_succeeded', {
      media_kind: normalized.message_type,
      id_strategy: resolvedId.strategy,
    });
    downloadPhase = false;
    const mimeType = String(media?.mimetype || '').split(';', 1)[0].toLowerCase();
    logger.info?.('voice_media_mime_observed', {
      mime_type: mimeType || 'missing',
      media_kind: normalized.message_type,
    });
    const voice = isVoiceKind(normalized.message_type);
    if (voice) {
      if (!ALLOWED_MIME_TYPES.has(mimeType)) throw new Error('UNSUPPORTED_MEDIA_MIME');
    } else if (!GENERIC_MIME_RE.test(mimeType)) {
      throw new Error('UNSUPPORTED_MEDIA_MIME');
    }
    const maxBytes = voice
      ? config.mediaMaxBytes
      : config.artifactMediaMaxBytes;
    const bytes = decodeBase64Strict(media?.data, maxBytes);
    const stored = atomicStore(config.mediaRoot, bytes);
    const filename = typeof media?.filename === 'string' && media.filename.trim()
      ? media.filename.trim().slice(0, 512)
      : null;
    const payload = {
      ...base,
      media_ref: 'sha256:' + stored.digest,
      content_sha256: stored.digest,
      mime_type: mimeType,
      size_bytes: bytes.length,
      original_filename: filename,
      capture_status: 'READY',
    };
    checkpoint(payload);
    readyCheckpointed = true;
    const delivery = await notify(config, normalized, payload, logger, fetchImpl);
    return { status: delivery.done ? 'media_ready_notified' : 'media_notification_pending', delivery };
  } catch (error) {
    // Notification/spool failure must not replace a durable READY receipt.
    // Replay retries notification using that same receipt, without downloading.
    if (readyCheckpointed) throw error;
    logger.warn?.('voice_media_download_failed', {
      media_kind: normalized.message_type,
      id_strategy: resolvedId.strategy,
      failure_class: error?.message === 'MESSAGE_ID_UNAVAILABLE'
        ? 'MESSAGE_ID_UNAVAILABLE' : failureClass(error),
    });
    const payload = { ...base, capture_status: error?.message === 'DOWNLOAD_EMPTY' ? 'MISSING' : 'FAILED' };
    checkpoint(payload);
    const delivery = await notify(config, normalized, payload, logger, fetchImpl);
    return { status: 'media_capture_failed', error_code: error?.message || 'MEDIA_CAPTURE_FAILED', delivery };
  }
}

async function captureInboundMedia(config, message, normalized, logger = console, fetchImpl = fetch) {
  if (!config.mediaRoot || !config.mediaNotificationUrl) return { status: 'media_capture_disabled' };
  const scope = JSON.stringify([normalized.tenant_id, normalized.source,
    normalized.metadata?.source_account, normalized.external_event_id]);
  const directory = path.join(config.mediaRoot, '.capture-receipts');
  fs.mkdirSync(directory, {recursive: true, mode: 0o700});
  const file = path.join(directory, stableHash(scope) + '.json');
  if (captureFlights.has(file)) return captureFlights.get(file);
  const run = async () => {
    const fingerprint = stableHash(JSON.stringify([scope, normalized.message_type,
      normalized.occurred_at, normalized.metadata?.conversation_key]));
    let state = {fingerprint, attempts: 0, payload: null};
    if (fs.existsSync(file)) {
      state = JSON.parse(fs.readFileSync(file, 'utf8'));
      if (state.fingerprint !== fingerprint) throw new Error('MEDIA_CAPTURE_SCOPE_CONFLICT');
    }
    const checkpoint = payload => {
      if (payload) state.payload = payload;
      else {
        if (state.attempts >= 3) throw new Error('MEDIA_ATTEMPTS_EXHAUSTED');
        state.attempts++;
      }
      const tmp = file + '.tmp';
      fs.writeFileSync(tmp, JSON.stringify(state), {mode: 0o600});
      fs.renameSync(tmp, file);
    };
    if (state.payload) {
      const delivery = await notify(config, normalized, state.payload, logger, fetchImpl);
      return {status: 'media_receipt_replayed', delivery};
    }
    return captureMediaAttempt(config, message, normalized, logger, fetchImpl, checkpoint);
  };
  const flight = run().finally(() => captureFlights.delete(file));
  captureFlights.set(file, flight);
  return flight;
}

async function drainMediaNotifications(config, logger = console, fetchImpl = fetch) {
  if (!config.mediaNotificationUrl || !config.mediaNotificationPendingDir) return 0;
  const spoolConfig = mediaNotificationConfig(config);
  let delivered = 0;
  for (const file of listPending(spoolConfig)) {
    const result = await deliverPendingFile(spoolConfig, file, logger, fetchImpl);
    if (result.done) delivered += 1;
    if (result.stop) break;
  }
  return delivered;
}

async function captureVoiceMedia(config, message, normalized, logger = console, fetchImpl = fetch) {
  return captureInboundMedia(config, message, normalized, logger, fetchImpl);
}

module.exports = {
  ALLOWED_MIME_TYPES,
  GENERIC_MIME_RE,
  atomicStore,
  captureInboundMedia,
  captureVoiceMedia,
  decodeBase64Strict,
  drainMediaNotifications,
  isVoiceKind,
};
