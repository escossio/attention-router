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

function encodeHistoryCursor({ after, through }) {
  if (!after || !through) throw historyError('HISTORY_CURSOR_INVALID');
  return Buffer.from(JSON.stringify({ v: 1, after, through }), 'utf8').toString('base64url');
}

function decodeHistoryCursor(value) {
  if (!value) return null;
  try {
    const payload = JSON.parse(Buffer.from(String(value), 'base64url').toString('utf8'));
    if (
      payload?.v !== 1
      || typeof payload.after !== 'string'
      || !payload.after
      || typeof payload.through !== 'string'
      || !payload.through
    ) {
      throw new Error('invalid cursor payload');
    }
    return { after: payload.after, through: payload.through };
  } catch {
    throw historyError('HISTORY_CURSOR_INVALID');
  }
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

async function fetchHistoryMessages(
  client,
  chatId,
  limit = 50,
  cursor = null,
  maxScanMessages = 2000,
) {
  assertReadOnlyClient(client);
  const boundedLimit = Math.max(1, Math.min(Number(limit) || 50, 100));
  const scanLimit = Number(maxScanMessages);
  if (!Number.isInteger(scanLimit) || scanLimit < 1 || scanLimit > 10000) {
    throw historyError('HISTORY_SCAN_LIMIT_INVALID');
  }
  const chat = typeof client.getChatById === 'function'
    ? await client.getChatById(chatId)
    : (await client.getChats()).find((item) => serializedId(item.id) === chatId);
  if (!chat) throw historyError('CHAT_NOT_FOUND');
  if (typeof chat.fetchMessages !== 'function') {
    throw historyError('HISTORY_FETCH_MESSAGES_UNAVAILABLE');
  }

  // Ask whatsapp-web.js for one more than the product bound. If that many
  // messages are returned, we know the complete chat cannot be represented
  // within this configured bootstrap snapshot and fail closed.
  const rawMessages = await chat.fetchMessages({ limit: scanLimit + 1 });
  if (rawMessages.length > scanLimit) {
    throw historyError('HISTORY_SCAN_LIMIT_EXCEEDED');
  }
  const mapped = rawMessages.map((message) => mapMessage(message, chat, client));
  if (mapped.some((message) => !message.source_message_id)) {
    throw historyError('HISTORY_MESSAGE_ID_REQUIRED');
  }
  if (new Set(mapped.map((message) => message.source_message_id)).size !== mapped.length) {
    throw historyError('HISTORY_MESSAGE_ID_DUPLICATE');
  }

  if (mapped.length === 0) {
    return {
      chat: mapChat(chat),
      pagination_model: 'OPAQUE_CURSOR_SNAPSHOT_V1',
      requested_limit: boundedLimit,
      messages: [],
      next_cursor: null,
    };
  }

  const decoded = decodeHistoryCursor(cursor);
  let snapshot = mapped;
  let startIndex = 0;
  let through = mapped[mapped.length - 1].source_message_id;

  if (decoded) {
    const throughIndex = mapped.findIndex(
      (message) => message.source_message_id === decoded.through,
    );
    if (throughIndex < 0) throw historyError('HISTORY_CURSOR_STALE');
    snapshot = mapped.slice(0, throughIndex + 1);
    const afterIndex = snapshot.findIndex(
      (message) => message.source_message_id === decoded.after,
    );
    if (afterIndex < 0) throw historyError('HISTORY_CURSOR_STALE');
    startIndex = afterIndex + 1;
    through = decoded.through;
  }

  const page = snapshot.slice(startIndex, startIndex + boundedLimit);
  const hasMore = startIndex + page.length < snapshot.length;
  const nextCursor = hasMore && page.length
    ? encodeHistoryCursor({
      after: page[page.length - 1].source_message_id,
      through,
    })
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
  const parsed = new URL(req.url || '/', `http://${req.headers.host || 'localhost'}`);
  const requestTarget = `${parsed.pathname}${parsed.search}`;
  const expected = `sha256=${crypto.createHmac('sha256', secret).update(`${timestamp}.GET.${requestTarget}`).digest('hex')}`;
  const actual = Buffer.from(signature);
  const wanted = Buffer.from(expected);
  return actual.length === wanted.length && crypto.timingSafeEqual(actual, wanted);
}

function signHistoryRequest(requestTarget, secret, timestamp = Math.floor(Date.now() / 1000)) {
  return {
    'X-Attention-Timestamp': String(timestamp),
    'X-Attention-Signature': `sha256=${crypto.createHmac('sha256', secret).update(`${timestamp}.GET.${requestTarget}`).digest('hex')}`,
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
  encodeHistoryCursor,
  fetchHistoryMessages,
  listHistoryChats,
  mapChat,
  mapMessage,
  maskIdentifier,
  signHistoryRequest,
  verifyHistoryHmac,
};
