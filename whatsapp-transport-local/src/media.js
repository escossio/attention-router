const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const { deliverPendingFile, enqueuePending, listPending } = require('./spool');
const { resolveMessageLookupId } = require('./message-id');

const ALLOWED_MIME_TYPES = new Set(['audio/ogg', 'audio/mpeg', 'audio/mp4']);

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

async function captureVoiceMedia(config, message, normalized, logger = console, fetchImpl = fetch) {
  if (!config.mediaRoot || !config.mediaNotificationUrl) return { status: 'media_capture_disabled' };
  const base = {
    tenant_id: normalized.tenant_id,
    source: normalized.source,
    external_event_id: normalized.external_event_id,
    media_kind: normalized.message_type,
  };
  const resolvedId = resolveMessageLookupId(message?.id);
  let downloadPhase = true;
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
    try {
      media = await Promise.race([message.downloadMedia.call(compatReceiver), timeoutPromise]);
    } finally {
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
    if (!ALLOWED_MIME_TYPES.has(mimeType)) throw new Error('UNSUPPORTED_MEDIA_MIME');
    const bytes = decodeBase64Strict(media?.data, config.mediaMaxBytes);
    const stored = atomicStore(config.mediaRoot, bytes);
    const payload = {
      ...base,
      media_ref: `sha256:${stored.digest}`,
      content_sha256: stored.digest,
      mime_type: mimeType,
      size_bytes: bytes.length,
      capture_status: 'READY',
    };
    const delivery = await notify(config, normalized, payload, logger, fetchImpl);
    return { status: delivery.done ? 'media_ready_notified' : 'media_notification_pending', delivery };
  } catch (error) {
    logger.warn?.('voice_media_download_failed', {
      media_kind: normalized.message_type,
      id_strategy: resolvedId.strategy,
      failure_class: error?.message === 'MESSAGE_ID_UNAVAILABLE'
        ? 'MESSAGE_ID_UNAVAILABLE' : failureClass(error),
    });
    const payload = { ...base, capture_status: 'FAILED' };
    const delivery = await notify(config, normalized, payload, logger, fetchImpl);
    return { status: 'media_capture_failed', error_code: error?.message || 'MEDIA_CAPTURE_FAILED', delivery };
  }
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

module.exports = {
  ALLOWED_MIME_TYPES, atomicStore, captureVoiceMedia, decodeBase64Strict,
  drainMediaNotifications,
};
