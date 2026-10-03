const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');

const {
  capabilities,
  decodeHistoryCursor,
  fetchHistoryMessages,
  listHistoryChats,
  signHistoryRequest,
  verifyHistoryHmac,
} = require('../src/history');
const { createServer } = require('../src/server');

function fakeClient(calls) {
  const chat = {
    id: { _serialized: 'contact024@example.com' },
    isGroup: true,
    name: 'Family',
    participants: [{ id: { _serialized: 'person-a@c.us' } }],
    fetchMessages: async ({ limit }) => {
      calls.push(['fetchMessages', limit]);
      return [{
        id: { _serialized: 'message-1' },
        fromMe: false,
        author: 'person-a@c.us',
        from: 'contact024@example.com',
        to: 'me@c.us',
        timestamp: 1700000000,
        body: 'hello',
        type: 'chat',
        hasMedia: false,
        hasQuotedMsg: true,
        _data: { quotedMsg: { id: { _serialized: 'message-0' } } },
      }];
    },
  };
  return {
    info: { wid: { _serialized: 'me@c.us' } },
    getChats: async () => { calls.push(['getChats']); return [chat]; },
    getChatById: async (id) => { calls.push(['getChatById', id]); return id === 'contact024@example.com' ? chat : null; },
    sendMessage: async () => { throw new Error('MUTATION_CALLED'); },
    sendSeen: async () => { throw new Error('MUTATION_CALLED'); },
  };
}

test('history capabilities are explicit for whatsapp-web.js v1.34.7', () => {
  assert.deepEqual(capabilities(), {
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
  });
});

test('history adapter only calls read APIs and preserves group sender', async () => {
  const calls = [];
  const client = fakeClient(calls);
  const chats = await listHistoryChats(client);
  const result = await fetchHistoryMessages(client, 'contact024@example.com', 10);
  assert.equal(chats[0].thread_type, 'GROUP');
  assert.equal(result.messages[0].external_sender_key, 'person-a@c.us');
  assert.equal(result.messages[0].source_message_id, 'message-1');
  assert.equal(result.messages[0].reply_reference, 'message-0');
  assert.deepEqual(calls, [['getChats'], ['getChatById', 'contact024@example.com'], ['fetchMessages', 2001]]);
});

test('history HMAC binds path and query parameters', () => {
  const target = '/internal/history/chats/contact?limit=2&cursor=abc';
  const headers = signHistoryRequest(target, 'test-history-secret');
  const req = { url: target, headers: Object.fromEntries(Object.entries(headers).map(([key, value]) => [key.toLowerCase(), value])) };
  assert.equal(verifyHistoryHmac(req, { historyHmacSecret: 'test-history-secret', historyMaxSkewSeconds: 300 }), true);
  req.url = '/internal/history/chats/contact?limit=2&cursor=other';
  assert.equal(verifyHistoryHmac(req, { historyHmacSecret: 'test-history-secret', historyMaxSkewSeconds: 300 }), false);
});

test('history endpoint is authenticated, bounded, and read-only', async () => {
  const calls = [];
  const server = createServer(
    { historyReadOnlyEnabled: true, historyPath: '/internal/history/chats', historyHmacSecret: 'test-history-secret', historyMaxSkewSeconds: 300, livePath: '/live', readyPath: '/ready', statusPath: '/status', outboundPath: '/internal/send', externalDeliveryEnabled: false },
    { service_state: 'ready', browser_debug_reachable: true, wwebjs_connected: true, ready: true, client_state: 'CONNECTED' },
    fakeClient(calls),
  );
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  try {
    const path = '/internal/history/chats';
    const headers = signHistoryRequest(path, 'test-history-secret');
    const response = await new Promise((resolve, reject) => {
      const request = http.request({ host: '127.0.0.1', port: server.address().port, path, headers }, (res) => {
        let body = '';
        res.on('data', (chunk) => { body += chunk; });
        res.on('end', () => resolve({ statusCode: res.statusCode, body }));
      });
      request.on('error', reject);
      request.end();
    });
    assert.equal(response.statusCode, 200);
    assert.equal(JSON.parse(response.body).chats[0].thread_type, 'GROUP');
    assert.deepEqual(calls, [['getChats']]);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});


function historyMessages(count) {
  return Array.from({ length: count }, (_, index) => ({
    id: { _serialized: `message-${index + 1}` },
    fromMe: false,
    author: 'person-a@c.us',
    from: 'contact024@example.com',
    timestamp: 1700000000 + index,
    body: `message ${index + 1}`,
    type: 'chat',
    hasMedia: false,
    hasQuotedMsg: false,
    _data: {},
  }));
}

function pagedClient(messages, calls = []) {
  const chat = {
    id: { _serialized: 'contact024@example.com' },
    isGroup: true,
    name: 'Family',
    participants: [],
    fetchMessages: async ({ limit }) => {
      calls.push(['fetchMessages', limit]);
      return messages.slice(Math.max(0, messages.length - limit));
    },
  };
  return {
    calls,
    chat,
    info: { wid: { _serialized: 'me@c.us' } },
    getChats: async () => [chat],
    getChatById: async () => chat,
  };
}

test('history cursor freezes upper bound and pages oldest to newest', async () => {
  const messages = historyMessages(6);
  const client = pagedClient(messages);

  const first = await fetchHistoryMessages(client, 'contact024@example.com', 2, null, 10);
  assert.deepEqual(first.messages.map((item) => item.source_message_id), ['message-1', 'message-2']);
  assert.ok(first.next_cursor);
  assert.equal(decodeHistoryCursor(first.next_cursor).through, 'message-6');

  messages.push(...historyMessages(1).map((item) => ({
    ...item,
    id: { _serialized: 'message-7' },
    timestamp: 1700000006,
    body: 'message 7',
  })));

  const second = await fetchHistoryMessages(client, 'contact024@example.com', 2, first.next_cursor, 10);
  assert.deepEqual(second.messages.map((item) => item.source_message_id), ['message-3', 'message-4']);
  const third = await fetchHistoryMessages(client, 'contact024@example.com', 2, second.next_cursor, 10);
  assert.deepEqual(third.messages.map((item) => item.source_message_id), ['message-5', 'message-6']);
  assert.equal(third.next_cursor, null);
});

test('history pagination fails closed on oversized or stale snapshots', async () => {
  const oversized = pagedClient(historyMessages(4));
  await assert.rejects(
    fetchHistoryMessages(oversized, 'contact024@example.com', 2, null, 3),
    /HISTORY_SCAN_LIMIT_EXCEEDED/,
  );

  const messages = historyMessages(4);
  const client = pagedClient(messages);
  const first = await fetchHistoryMessages(client, 'contact024@example.com', 2, null, 10);
  messages.pop();
  await assert.rejects(
    fetchHistoryMessages(client, 'contact024@example.com', 2, first.next_cursor, 10),
    /HISTORY_CURSOR_STALE/,
  );
});
