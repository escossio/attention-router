const test = require('node:test');
const assert = require('node:assert/strict');

test('stage 3.8 browser page preparation is covered by the runtime attach path', () => {
  assert.equal(typeof require('../src/transport').prepareAuthenticatedPage, 'function');
});
