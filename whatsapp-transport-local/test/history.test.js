const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');

const { capabilities, fetchHistoryMessages, listHistoryChats, signHistoryRequest, verifyHistoryHmac } = require('../src/history');
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
    can_paginate_history: 'PARTIAL',
    pagination_model: 'LIMIT_ONLY',
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
  assert.deepEqual(calls, [['getChats'], ['getChatById', 'contact024@example.com'], ['fetchMessages', 10]]);
});

test('history HMAC is path-bound and read-only endpoint credentials are verifiable', () => {
  const headers = signHistoryRequest('/internal/history/chats', 'test-history-secret');
  const req = { url: '/internal/history/chats', headers: Object.fromEntries(Object.entries(headers).map(([key, value]) => [key.toLowerCase(), value])) };
  assert.equal(verifyHistoryHmac(req, { historyHmacSecret: 'test-history-secret', historyMaxSkewSeconds: 300 }), true);
  req.url = '/internal/history/chats/other';
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
