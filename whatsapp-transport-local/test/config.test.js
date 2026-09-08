const test = require('node:test');
const assert = require('node:assert/strict');

const {
  createConfig,
  normalizeBrowserDebugUrl,
  normalizeInboundForwardUrl,
  parseBoolean,
} = require('../src/config');
const { startTransport } = require('../src/transport');

test('parseBoolean defaults fail closed', () => {
  assert.equal(parseBoolean(undefined, false), false);
  assert.equal(parseBoolean('false', true), false);
  assert.equal(parseBoolean('true', false), true);
  assert.equal(parseBoolean('invalid', false), false);
});

test('normalizeBrowserDebugUrl rejects non-local hosts', () => {
  assert.equal(normalizeBrowserDebugUrl({ BROWSER_DEBUG_URL: 'http://192.0.2.7:9222' }), null);
  assert.equal(normalizeBrowserDebugUrl({ BROWSER_DEBUG_URL: 'http://127.0.0.1:9222' }), 'http://127.0.0.1:9222');
});

test('inbound forward URL allows only the configured AGT ingress host', () => {
  const env = {
    INBOUND_FORWARD_URL: 'http://192.0.2.6:18102/api/v1/ingress/internal/events',
    LOCAL_INBOUND_FORWARD_ALLOWED_HOST: '192.0.2.6',
  };
  assert.equal(normalizeInboundForwardUrl(env), env.INBOUND_FORWARD_URL);
  assert.equal(
    normalizeInboundForwardUrl({ INBOUND_FORWARD_URL: 'http://192.0.2.7:18102/api/v1/ingress/internal/events' }),
    null,
  );
});

test('createConfig defaults are fail closed', () => {
  const config = createConfig({});
  assert.equal(config.inboundForwardEnabled, false);
  assert.equal(config.externalDeliveryEnabled, false);
  assert.equal(config.httpHost, '127.0.0.1');
  assert.equal(config.httpPort, 18103);
  assert.equal(config.browserDebugUrl, null);
});

test('startTransport blocks when browser debug url is unavailable', async () => {
  const config = createConfig({});
  const result = await startTransport(config, { warn() {}, error() {}, info() {} });
  assert.equal(result.client, null);
  assert.equal(result.isReady(), false);
  assert.equal(result.status.service_state, 'blocked');
  assert.equal(result.status.client_state, 'NO_BROWSER_DEBUG_URL');
});
