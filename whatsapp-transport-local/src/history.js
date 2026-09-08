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

async function fetchHistoryMessages(client, chatId, limit = 50) {
  assertReadOnlyClient(client);
  const boundedLimit = Math.max(1, Math.min(Number(limit) || 50, 100));
  const chat = typeof client.getChatById === 'function' ? await client.getChatById(chatId) : (await client.getChats()).find((item) => serializedId(item.id) === chatId);
  if (!chat) {
    const error = new Error('history chat not found');
    error.code = 'CHAT_NOT_FOUND';
    throw error;
  }
  if (typeof chat.fetchMessages !== 'function') throw new Error('history capability fetchMessages unavailable');
  const messages = await chat.fetchMessages({ limit: boundedLimit });
  return {
    chat: mapChat(chat),
    pagination_model: 'LIMIT_ONLY',
    requested_limit: boundedLimit,
    messages: messages.map((message) => mapMessage(message, chat, client)),
  };
}

function verifyHistoryHmac(req, config) {
  const secret = config.historyHmacSecret || config.hmacSecret;
  if (!secret) return false;
  const timestamp = String(req.headers['x-attention-timestamp'] || '');
  const signature = String(req.headers['x-attention-signature'] || '');
  const now = Math.floor(Date.now() / 1000);
  if (!/^\d+$/.test(timestamp) || Math.abs(now - Number(timestamp)) > (config.historyMaxSkewSeconds || 300)) return false;
  const path = new URL(req.url || '/', `http://${req.headers.host || 'localhost'}`).pathname;
  const expected = `sha256=${crypto.createHmac('sha256', secret).update(`${timestamp}.GET.${path}`).digest('hex')}`;
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
    can_paginate_history: 'PARTIAL',
    pagination_model: 'LIMIT_ONLY',
    can_distinguish_from_me: true,
    can_distinguish_direct_vs_group: true,
    can_get_stable_source_message_id: true,
  };
}

module.exports = {
  capabilities,
  fetchHistoryMessages,
  listHistoryChats,
  mapChat,
  mapMessage,
  maskIdentifier,
  signHistoryRequest,
  verifyHistoryHmac,
};
