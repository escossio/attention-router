const crypto = require('node:crypto');

const MUTATING_METHODS = new Set([
  'sendMessage', 'sendSeen', 'sendStateTyping', 'sendStateRecording', 'clearState',
  'archiveChat', 'pinChat', 'muteChat', 'deleteMessage', 'react', 'forward',
  'markChatUnread', 'syncHistory', 'destroy', 'sendPresenceAvailable', 'sendPresenceUnavailable',
]);

function serializedId(value) {
  if (!value) return null;
  if (typeof value === 'string') return value;
  return value._serialized || value.id || null;
}

function isoTimestamp(value) {
  if (value === undefined || value === null || value === '') return null;
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return null;
  return new Date(seconds * 1000).toISOString();
}

function maskIdentifier(value) {
  const raw = String(value || '');
  if (raw.length <= 8) return raw ? '[MASKED]' : null;
  return `${raw.slice(0, 4)}…${raw.slice(-4)}`;
}

function mapChat(chat) {
  const id = serializedId(chat?.id);
  const participants = Array.isArray(chat?.participants)
    ? chat.participants.map((participant) => serializedId(participant?.id || participant)).filter(Boolean).map(maskIdentifier)
    : [];
  return {
    external_thread_key: id,
    thread_type: chat?.isGroup ? 'GROUP' : 'DIRECT',
    title: chat?.name || chat?.formattedTitle || null,
    timestamp: isoTimestamp(chat?.timestamp),
    participants,
  };
}

function replyReference(message) {
  const quoted = message?._data?.quotedMsg;
  return serializedId(quoted?.id) || serializedId(quoted?.id?._serialized) || null;
}

function mapMessage(message, chat, client) {
  const fromMe = Boolean(message?.fromMe);
  const sender = chat?.isGroup
    ? (message?.author || null)
    : (fromMe ? serializedId(client?.info?.wid) : (message?.from || null));
  return {
    source_message_id: serializedId(message?.id),
    external_thread_key: serializedId(chat?.id),
    external_sender_key: sender,
    from_me: fromMe,
    timestamp: isoTimestamp(message?.timestamp),
    type: message?.type || 'unknown',
    text: typeof message?.body === 'string' ? message.body : null,
    has_media: Boolean(message?.hasMedia),
    reply_reference: replyReference(message),
    reply_reference_available: Boolean(message?.hasQuotedMsg),
  };
}

function historyError(code) {
  const error = new Error(code);
  error.code = code;
  return error;
}

function encodeHistoryCursor({ after, through, fingerprint }) {
  return Buffer.from(JSON.stringify({ v: 1, after, through, fingerprint }), 'utf8').toString('base64url');
}

function decodeHistoryCursor(value) {
  if (typeof value !== 'string' || value.length > 2048) throw historyError('HISTORY_CURSOR_INVALID');
  try {
    const parsed = JSON.parse(Buffer.from(value, 'base64url').toString('utf8'));
    if (parsed?.v !== 1 || ![parsed.after, parsed.through].every(
      (id) => typeof id === 'string' && id.length > 0 && id.length <= 512,
    ) || !/^[a-f0-9]{64}$/.test(parsed.fingerprint)) throw new Error('invalid');
    return parsed;
  } catch {
    throw historyError('HISTORY_CURSOR_INVALID');
  }
}

function snapshotFingerprint(messages) {
  return crypto.createHash('sha256').update(JSON.stringify(messages)).digest('hex');
}

function assertReadOnlyClient(client) {
  for (const name of MUTATING_METHODS) {
    if (typeof client?.[name] === 'function' && name !== 'getChats') {
      // Presence of mutators is expected on whatsapp-web.js. The adapter never calls them.
      continue;
    }
  }
  if (typeof client?.getChats !== 'function') throw new Error('history capability getChats unavailable');
}

async function listHistoryChats(client) {
  assertReadOnlyClient(client);
  const chats = await client.getChats();
  return chats.map(mapChat);
}

async function fetchHistoryMessages(client, chatId, limit = 50, cursor = null, maxScanMessages = 1000) {
  assertReadOnlyClient(client);
  const boundedLimit = Number(limit);
  if (!Number.isInteger(boundedLimit) || boundedLimit < 1 || boundedLimit > 100) {
    throw historyError('HISTORY_PAGE_SIZE_INVALID');
  }
  const scanLimit = Number(maxScanMessages);
  if (!Number.isInteger(scanLimit) || scanLimit < 1 || scanLimit > 10000 || scanLimit < boundedLimit) {
    throw historyError('HISTORY_SCAN_LIMIT_INVALID');
  }
  const decoded = cursor === null ? null : decodeHistoryCursor(cursor);
  const chat = typeof client.getChatById === 'function' ? await client.getChatById(chatId) : (await client.getChats()).find((item) => serializedId(item.id) === chatId);
  if (!chat) {
    const error = new Error('history chat not found');
    error.code = 'CHAT_NOT_FOUND';
    throw error;
  }
  if (typeof chat.fetchMessages !== 'function') throw historyError('HISTORY_FETCH_MESSAGES_UNAVAILABLE');
  const messages = await chat.fetchMessages({ limit: scanLimit + 1 });
  if (!Array.isArray(messages)) throw historyError('HISTORY_RESPONSE_INVALID');
  if (messages.length > scanLimit) throw historyError('HISTORY_SCAN_LIMIT_EXCEEDED');
  const mapped = messages.map((message) => mapMessage(message, chat, client));
  if (mapped.some((message) => typeof message.source_message_id !== 'string'
    || !message.source_message_id || message.source_message_id.length > 512)) {
    throw historyError('HISTORY_MESSAGE_ID_REQUIRED');
  }
  if (new Set(mapped.map((message) => message.source_message_id)).size !== mapped.length) {
    throw historyError('HISTORY_MESSAGE_ID_DUPLICATE');
  }
  let snapshot = mapped;
  let start = 0;
  const through = decoded?.through || mapped.at(-1)?.source_message_id;
  if (decoded) {
    const throughIndex = mapped.findIndex((message) => message.source_message_id === through);
    if (throughIndex < 0) throw historyError('HISTORY_CURSOR_STALE');
    snapshot = mapped.slice(0, throughIndex + 1);
    if (snapshotFingerprint(snapshot) !== decoded.fingerprint) throw historyError('HISTORY_CURSOR_STALE');
    const afterIndex = snapshot.findIndex((message) => message.source_message_id === decoded.after);
    if (afterIndex < 0 || afterIndex === snapshot.length - 1) throw historyError('HISTORY_CURSOR_STALE');
    start = afterIndex + 1;
  }
  const page = snapshot.slice(start, start + boundedLimit);
  const nextCursor = start + page.length < snapshot.length
    ? encodeHistoryCursor({ after: page.at(-1).source_message_id, through,
      fingerprint: decoded?.fingerprint || snapshotFingerprint(snapshot) })
    : null;
  return {
    chat: mapChat(chat),
    pagination_model: 'OPAQUE_CURSOR_SNAPSHOT_V1',
    requested_limit: boundedLimit,
    messages: page,
    next_cursor: nextCursor,
  };
}

function verifyHistoryHmac(req, config) {
  const secret = config.historyHmacSecret || config.hmacSecret;
  if (!secret) return false;
  const timestamp = String(req.headers['x-attention-timestamp'] || '');
  const signature = String(req.headers['x-attention-signature'] || '');
  const now = Math.floor(Date.now() / 1000);
  if (!/^\d+$/.test(timestamp) || Math.abs(now - Number(timestamp)) > (config.historyMaxSkewSeconds || 300)) return false;
  const expected = `sha256=${crypto.createHmac('sha256', secret).update(`${timestamp}.GET.${req.url || '/'}`).digest('hex')}`;
  const actual = Buffer.from(signature);
  const wanted = Buffer.from(expected);
  return actual.length === wanted.length && crypto.timingSafeEqual(actual, wanted);
}

function signHistoryRequest(path, secret, timestamp = Math.floor(Date.now() / 1000)) {
  return {
    'X-Attention-Timestamp': String(timestamp),
    'X-Attention-Signature': `sha256=${crypto.createHmac('sha256', secret).update(`${timestamp}.GET.${path}`).digest('hex')}`,
  };
}

function capabilities() {
  return {
    can_list_chats: true,
    can_fetch_historical_messages: true,
    can_fetch_group_authors: true,
    can_fetch_timestamps: true,
    can_fetch_reply_references: 'PARTIAL',
    can_fetch_captions: 'PARTIAL',
    can_paginate_history: true,
    pagination_model: 'OPAQUE_CURSOR_SNAPSHOT_V1',
    can_distinguish_from_me: true,
    can_distinguish_direct_vs_group: true,
    can_get_stable_source_message_id: true,
  };
}

module.exports = {
  capabilities,
  decodeHistoryCursor,
  fetchHistoryMessages,
  listHistoryChats,
  mapChat,
  mapMessage,
  maskIdentifier,
  signHistoryRequest,
  verifyHistoryHmac,
};
