const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const vm = require('node:vm');

const { capabilities, decodeHistoryCursor, fetchHistoryMessages, listHistoryChats, mapMessage, signHistoryRequest, verifyHistoryHmac } = require('../src/history');
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
  const client = {
    info: { wid: { _serialized: 'me@c.us' } },
    getChats: async () => { throw new Error('GET_CHATS_CALLED'); },
    pupPage: syntheticChatPage([{
      id: { _serialized: 'contact024@example.com' }, groupMetadata: {}, formattedTitle: 'Family', t: 1700000000,
      get msgs() { throw new Error('MESSAGE_BODY_READ'); },
      get lastReceivedKey() { throw new Error('LAST_MESSAGE_READ'); },
    }], calls),
    getChatById: async (id) => { calls.push(['getChatById', id]); return id === 'contact024@example.com' ? chat : null; },
  };
  for (const method of ['sendMessage', 'sendSeen', 'sendStateTyping', 'sendStateRecording',
    'clearState', 'archiveChat', 'pinChat', 'muteChat', 'deleteMessage', 'react',
    'forward', 'markChatUnread', 'syncHistory', 'sendPresenceAvailable',
    'sendPresenceUnavailable']) {
    client[method] = async () => { throw new Error('MUTATION_CALLED'); };
    chat[method] = async () => { throw new Error('MUTATION_CALLED'); };
  }
  return client;
}

function syntheticChatPage(chats, calls = []) {
  return { evaluate: async (callback) => {
    calls.push(['listChatMetadata']);
    return vm.runInNewContext(`(${callback.toString()})()`, {
      window: { require: (name) => {
        assert.equal(name, 'WAWebCollections');
        return { Chat: { getModelsArray: () => chats } };
      } },
    });
  } };
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
  assert.deepEqual(calls, [['listChatMetadata'], ['getChatById', 'contact024@example.com'], ['fetchMessages', 1001]]);
});

test('history chat listing accepts $1 IDs without reading message fields', async () => {
  const calls = [];
  const client = fakeClient(calls);
  client.pupPage = syntheticChatPage([{
    id: { $1: 'contact024@example.com' }, groupMetadata: null, formattedTitle: 'Direct', t: 1700000000,
    get msgs() { throw new Error('MESSAGE_BODY_READ'); },
    get lastReceivedKey() { throw new Error('LAST_MESSAGE_READ'); },
  }], calls);
  const chats = await listHistoryChats(client);
  assert.equal(chats.length, 1);
  assert.equal(chats[0].external_thread_key, 'contact024@example.com');
  assert.equal(chats[0].thread_type, 'DIRECT');
  assert.deepEqual(calls, [['listChatMetadata']]);
});

test('history chat listing fails closed on missing or duplicate IDs', async () => {
  const client = fakeClient([]);
  for (const source of [[{ id: null }], [{ id: 'same' }, { id: 'same' }]]) {
    client.pupPage = syntheticChatPage(source);
    await assert.rejects(listHistoryChats(client), /HISTORY_CHAT_LIST_INVALID/);
  }
});

test('history HMAC is path-bound and read-only endpoint credentials are verifiable', () => {
  const headers = signHistoryRequest('/internal/history/chats', 'test-history-secret');
  const req = { url: '/internal/history/chats', headers: Object.fromEntries(Object.entries(headers).map(([key, value]) => [key.toLowerCase(), value])) };
  assert.equal(verifyHistoryHmac(req, { historyHmacSecret: 'test-history-secret', historyMaxSkewSeconds: 300 }), true);
  req.url = '/internal/history/chats/other';
  assert.equal(verifyHistoryHmac(req, { historyHmacSecret: 'test-history-secret', historyMaxSkewSeconds: 300 }), false);
  for (const changed of ['?limit=3&cursor=a&max_scan_messages=10', '?limit=2&cursor=b&max_scan_messages=10', '?limit=2&cursor=a&max_scan_messages=11']) {
    const target = '/internal/history/chats/one?limit=2&cursor=a&max_scan_messages=10';
    const signed = signHistoryRequest(target, 'test-history-secret');
    const attempt = { url: `/internal/history/chats/one${changed}`, headers: Object.fromEntries(Object.entries(signed).map(([key, value]) => [key.toLowerCase(), value])) };
    assert.equal(verifyHistoryHmac(attempt, { historyHmacSecret: 'test-history-secret' }), false);
  }
});

function pagedClient(messages, calls = []) {
  const chat = { id: { _serialized: 'chat-a' }, isGroup: true, name: 'Synthetic',
    fetchMessages: async ({ limit }) => { calls.push(['fetchMessages', limit]); return messages.slice(-limit); } };
  return { getChats: async () => [chat], getChatById: async () => chat, calls };
}

function messages(count) {
  return Array.from({ length: count }, (_, i) => ({ id: { _serialized: `m${i + 1}` },
    timestamp: 1700000000 + i, body: `synthetic ${i + 1}`, type: 'chat', fromMe: false }));
}

test('three pages freeze the first upper bound across append and use one read each', async () => {
  const source = messages(6);
  const client = pagedClient(source);
  const first = await fetchHistoryMessages(client, 'chat-a', 2, null, 10);
  assert.deepEqual(first.messages.map((item) => item.source_message_id), ['m1', 'm2']);
  assert.equal(decodeHistoryCursor(first.next_cursor).through, 'm6');
  source.push(messages(7)[6]);
  const second = await fetchHistoryMessages(client, 'chat-a', 2, first.next_cursor, 10);
  const third = await fetchHistoryMessages(client, 'chat-a', 2, second.next_cursor, 10);
  assert.deepEqual(second.messages.map((item) => item.source_message_id), ['m3', 'm4']);
  assert.deepEqual(third.messages.map((item) => item.source_message_id), ['m5', 'm6']);
  assert.equal(third.next_cursor, null);
  assert.deepEqual(client.calls, Array(3).fill(['fetchMessages', 11]));
});

test('stale, oversized, missing and duplicate IDs fail closed', async () => {
  const source = messages(6);
  const client = pagedClient(source);
  const first = await fetchHistoryMessages(client, 'chat-a', 2, null, 10);
  source.splice(5, 1);
  await assert.rejects(fetchHistoryMessages(client, 'chat-a', 2, first.next_cursor, 10), /HISTORY_CURSOR_STALE/);
  source.push(messages(6)[5]);
  source.splice(1, 1);
  await assert.rejects(fetchHistoryMessages(client, 'chat-a', 2, first.next_cursor, 10), /HISTORY_CURSOR_STALE/);
  await assert.rejects(fetchHistoryMessages(client, 'chat-a', 2, null, 4), /HISTORY_SCAN_LIMIT_EXCEEDED/);
  source[0].id = null;
  await assert.rejects(fetchHistoryMessages(client, 'chat-a', 2, null, 10), /HISTORY_MESSAGE_ID_REQUIRED/);
  source[0].id = source[1].id;
  await assert.rejects(fetchHistoryMessages(client, 'chat-a', 2, null, 10), /HISTORY_MESSAGE_ID_DUPLICATE/);
});

test('history above 100 yields every frozen ID exactly once', async () => {
  const source = messages(120);
  const client = pagedClient(source);
  const seen = [];
  let cursor = null;
  do {
    const page = await fetchHistoryMessages(client, 'chat-a', 20, cursor, 200);
    seen.push(...page.messages.map((item) => item.source_message_id));
    cursor = page.next_cursor;
  } while (cursor);
  assert.deepEqual(seen, source.map((message) => message.id._serialized));
  assert.equal(new Set(seen).size, 120);
  assert.equal(client.calls.length, 6);
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
    assert.deepEqual(calls, [['listChatMetadata']]);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});


test('history mapping preserves direct sender identity and fromMe owner identity', () => {
  const client = { info: { wid: { _serialized: 'owner@c.us' } } };
  const chat = {
    id: { _serialized: 'contact@c.us' },
    isGroup: false,
    name: 'Direct',
  };

  const inbound = mapMessage(
    {
      id: { _serialized: 'direct-in' },
      fromMe: false,
      from: 'contact@c.us',
      timestamp: 1700000000,
      body: 'hello',
      type: 'chat',
      hasMedia: false,
      hasQuotedMsg: false,
    },
    chat,
    client,
  );
  const outbound = mapMessage(
    {
      id: { _serialized: 'direct-out' },
      fromMe: true,
      from: 'owner@c.us',
      timestamp: 1700000001,
      body: 'hi',
      type: 'chat',
      hasMedia: false,
      hasQuotedMsg: false,
    },
    chat,
    client,
  );

  assert.equal(inbound.external_sender_key, 'contact@c.us');
  assert.equal(inbound.from_me, false);
  assert.equal(inbound.source_message_id, 'direct-in');
  assert.equal(outbound.external_sender_key, 'owner@c.us');
  assert.equal(outbound.from_me, true);
  assert.equal(outbound.source_message_id, 'direct-out');
});
