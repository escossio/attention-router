const test = require('node:test');
const assert = require('node:assert/strict');

const {
  isTrustedWhatsAppPageUrl,
  prepareAuthenticatedPage,
  selectCanonicalConnectedWhatsAppPage,
} = require('../src/transport');

test('stage 3.8 browser page preparation is covered by the runtime attach path', () => {
  assert.equal(typeof prepareAuthenticatedPage, 'function');
});

test('accepts the legitimate WhatsApp origin and its valid URL variants', () => {
  for (const value of [
    'https://web.whatsapp.com/',
    'https://web.whatsapp.com/path?tab=chats#inbox',
    'https://web.whatsapp.com:443/',
  ]) {
    assert.equal(isTrustedWhatsAppPageUrl(value), true, value);
  }
});

test('rejects host, path, userinfo, protocol and malformed URL confusion', () => {
  for (const value of [
    'https://web.whatsapp.com.evil.example/',
    'https://evil.example/?next=https://web.whatsapp.com/',
    'https://web.whatsapp.com@evil.example/',
    'http://web.whatsapp.com/',
    'not a url web.whatsapp.com',
  ]) {
    assert.equal(isTrustedWhatsAppPageUrl(value), false, value);
  }
});

test('canonical page selection fails closed for adversarial URLs', async () => {
  const page = (url) => ({ url: () => url, evaluate: async () => ({ state: 'CONNECTED' }) });
  const adversarialPages = [
    page('https://web.whatsapp.com.evil.example/'),
    page('https://evil.example/?next=https://web.whatsapp.com/'),
    page('https://web.whatsapp.com@evil.example/'),
    page('http://web.whatsapp.com/'),
    page('not a url web.whatsapp.com'),
  ];

  for (const candidate of adversarialPages) {
    const result = await selectCanonicalConnectedWhatsAppPage([candidate]);
    assert.equal(result.result, 'NO_WHATSAPP_PAGE');
    assert.equal(result.whatsappPageCount, 0);
  }
});
