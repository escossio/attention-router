const http = require('node:http');
const crypto = require('node:crypto');
const { isReady, snapshot } = require('./status');
const { fetchHistoryMessages, listHistoryChats, verifyHistoryHmac } = require('./history');
const { identityFromAliases } = require('./conversation');
const { createOutboundProvenanceLedger } = require('./outbound-provenance');
const fs = require('node:fs');
const path = require('node:path');
const { MessageMedia } = require('whatsapp-web.js');

function loadVoiceMedia(config, payload, MessageMediaClass = MessageMedia) {
  if (!new Set(['audio/mpeg', 'audio/ogg']).has(payload.mime_type)
      || !/^sha256:[0-9a-f]{64}$/.test(payload.media_ref || '')) {
    throw new Error('VOICE_MEDIA_CONTRACT_INVALID');
  }
  const digest = payload.media_ref.slice(7);
  if (digest !== payload.content_sha256 || !Number.isInteger(payload.size_bytes)
      || payload.size_bytes < 1 || payload.size_bytes > config.mediaMaxBytes) {
    throw new Error('VOICE_MEDIA_CONTRACT_INVALID');
  }
  const file = path.join(config.mediaRoot, digest.slice(0, 2), digest);
  const info = fs.lstatSync(file);
  if (!info.isFile() || info.isSymbolicLink() || info.size !== payload.size_bytes) {
    throw new Error('VOICE_MEDIA_FILE_INVALID');
  }
  const bytes = fs.readFileSync(file);
  if (crypto.createHash('sha256').update(bytes).digest('hex') !== digest) {
    throw new Error('VOICE_MEDIA_HASH_MISMATCH');
  }
  return new MessageMediaClass(payload.mime_type, bytes.toString('base64'));
}

function sanitizeOutboundErrorMessage(error) {
  let message = String(error?.message || '')
    .replace(/\s+/g, ' ')
    .trim();

  if (!message) return null;

  message = message
    .replace(/\b\d{5,}(?=@(?:c\.us|lid)\b)/gi, '[redacted]')
    .replace(/https?:\/\/\S+/gi, '[redacted-url]')
    .replace(/\bBearer\s+\S+/gi, 'Bearer [redacted]')
    .replace(/\bsk-[A-Za-z0-9_-]+/g, '[redacted-secret]');

  return message.slice(0, 300);
}

function respondJson(res, code, body) {
  const payload = Buffer.from(JSON.stringify(body));
  res.writeHead(code, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': String(payload.length),
    'cache-control': 'no-store',
  });
  res.end(payload);
}

function createServer(config, status, client = null, deps = {}) {
  const deliveries = deps.deliveries || new Map();
  const now = deps.now || (() => Date.now());
  const outboundProvenance = deps.outboundProvenance || createOutboundProvenanceLedger(config);
  const MessageMediaClass = deps.MessageMedia || MessageMedia;
  const logger = deps.logger || console;
  return http.createServer((req, res) => {
    const url = new URL(req.url || '/', `http://${req.headers.host || `${config.httpHost}:${config.httpPort}`}`);
    if (req.method === 'POST' && url.pathname === config.outboundPath) {
      if (!config.externalDeliveryEnabled || !config.outboundHmacSecret) {
        respondJson(res, 503, { status: 'external_delivery_disabled' });
        return;
      }
      const chunks = [];
      let size = 0;
      req.on('data', (chunk) => {
        size += chunk.length;
        if (size <= config.outboundMaxBodyBytes) chunks.push(chunk);
      });
      req.on('end', async () => {
        if (size > config.outboundMaxBodyBytes) {
          respondJson(res, 413, { status: 'body_too_large' });
          return;
        }
        const raw = Buffer.concat(chunks);
        const timestamp = req.headers['x-attention-timestamp'];
        const signature = req.headers['x-attention-signature'];
        const expected = `sha256=${crypto.createHmac('sha256', config.outboundHmacSecret).update(`${timestamp}.${raw.toString()}`).digest('hex')}`;
        const signatureBuffer = Buffer.from(String(signature || ''));
        const expectedBuffer = Buffer.from(expected);
        if (!timestamp || !signature || signatureBuffer.length !== expectedBuffer.length || !crypto.timingSafeEqual(signatureBuffer, expectedBuffer)) {
          respondJson(res, 401, { status: 'invalid_signature' });
          return;
        }
        let payload;
        try { payload = JSON.parse(raw.toString()); } catch {
          respondJson(res, 400, { status: 'invalid_json' });
          return;
        }
        const key = payload.idempotency_key;
        const destination = payload.external_actor_id || payload.destination;
        const destinationConflict = Boolean(
          payload.external_actor_id && payload.destination
          && payload.external_actor_id !== payload.destination,
        );
        const voice = payload.message_type === 'ptt';
        const payloadValid = voice ? Boolean(payload.media_ref) : typeof payload.text === 'string';
        if (!key || !destination || destinationConflict || !payloadValid || !client || !isReady(status)) {
          respondJson(res, 409, { status: 'transport_not_ready_or_invalid_payload' });
          return;
        }
        if (!outboundProvenance.available) {
          respondJson(res, 503, { status: 'outbound_provenance_unavailable' });
          return;
        }
        if (deliveries.has(key)) {
          respondJson(res, 200, { status: 'already_sent', message_reference: deliveries.get(key), idempotency_key: key });
          return;
        }
        let provenance;
        try {
          const begun = outboundProvenance.begin({
            ...payload,
            destination,
            conversation_identity: identityFromAliases([
              destination,
              ...(payload.peer_identifiers || []),
            ]),
          });
          provenance = begun.record;
          if (begun.replay && provenance.status === 'SENT') {
            respondJson(res, 200, { status: 'already_sent', message_reference: provenance.message_reference, idempotency_key: key });
            return;
          }
        } catch (error) {
          respondJson(res, 409, { status: 'outbound_provenance_rejected', error_class: error?.message || 'ProvenanceError' });
          return;
        }
        try {
          const message = voice
            ? await client.sendMessage(
              destination,
              loadVoiceMedia(config, payload, MessageMediaClass),
              { sendAudioAsVoice: true },
            )
            : await client.sendMessage(destination, payload.text);
          const reference = message?.id?._serialized || message?.id?.id || `accepted-${now()}`;
          outboundProvenance.markSent(provenance, reference);
          deliveries.set(key, reference);
          respondJson(res, 200, { status: 'sent', message_reference: reference, idempotency_key: key });
        } catch (error) {
          logger.error(JSON.stringify({
            ts: new Date().toISOString(),
            event: 'outbound_send_failed',
            message_type: voice ? 'ptt' : 'text',
            error_class: error?.name || 'TransportError',
            error_message: sanitizeOutboundErrorMessage(error),
          }));
          outboundProvenance.markFailed(provenance, error?.name || 'TransportError');
          respondJson(res, 503, { status: 'send_failed', error_class: error?.name || 'TransportError' });
        }
      });
      return;
    }
    if (req.method !== 'GET') {
      res.writeHead(405, { allow: 'GET' });
      res.end();
      return;
    }
    if (config.historyReadOnlyEnabled && url.pathname === config.historyPath) {
      if (!verifyHistoryHmac(req, config) || !client || !isReady(status)) {
        respondJson(res, 503, { status: 'history_not_ready_or_unauthorized' });
        return;
      }
      listHistoryChats(client).then((chats) => respondJson(res, 200, { status: 'ok', chats })).catch(() => respondJson(res, 503, { status: 'history_read_failed' }));
      return;
    }
    if (config.historyReadOnlyEnabled && url.pathname.startsWith(`${config.historyPath}/`)) {
      if (!verifyHistoryHmac(req, config) || !client || !isReady(status)) {
        respondJson(res, 503, { status: 'history_not_ready_or_unauthorized' });
        return;
      }
      const chatId = decodeURIComponent(url.pathname.slice(`${config.historyPath}/`.length));
      const limit = Math.min(Number(url.searchParams.get('limit') || 50), config.historyMaxPageSize);
      fetchHistoryMessages(client, chatId, limit).then((result) => respondJson(res, 200, { status: 'ok', ...result })).catch((error) => respondJson(res, error.code === 'CHAT_NOT_FOUND' ? 404 : 503, { status: 'history_read_failed' }));
      return;
    }
    if (url.pathname === config.livePath) {
      respondJson(res, 200, { status: 'live', service: config.serviceName });
      return;
    }
    if (url.pathname === config.readyPath) {
      respondJson(res, isReady(status) ? 200 : 503, {
        status: isReady(status) ? 'ready' : 'not_ready',
        service: config.serviceName,
      });
      return;
    }
    if (url.pathname === config.statusPath) {
      respondJson(res, 200, snapshot(status));
      return;
    }
    respondJson(res, 404, { status: 'not_found' });
  });
}

module.exports = {
  createServer,
  loadVoiceMedia,
};
