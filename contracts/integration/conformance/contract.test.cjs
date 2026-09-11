const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const Ajv2020 = require('ajv/dist/2020').default;
const addFormats = require('ajv-formats');

const read = (name) => JSON.parse(fs.readFileSync(path.join(__dirname, '../v1', name), 'utf8'));
const schema = read('integration-contract.schema.json');
const corpus = read('conformance-cases.json');
const ajv = new Ajv2020({
  strict: true,
  allErrors: true,
  coerceTypes: false,
  useDefaults: false,
  removeAdditional: false,
});
// Pydantic includes an OpenAPI annotation. The standard oneOf/const keywords
// enforce discrimination; this annotation must never replace those constraints.
ajv.addKeyword({ keyword: 'discriminator', schemaType: 'object', valid: true });
addFormats(ajv, { mode: 'full' });
const validate = ajv.compile(schema);

test('shared corpus contains unique cases for every message family', () => {
  assert.equal(corpus.schema_id, schema.$id);
  assert.equal(new Set(corpus.cases.map((item) => item.id)).size, corpus.cases.length);
  assert.deepEqual(
    new Set(corpus.cases.filter((item) => item.wire_valid).map((item) => item.message.contract_type)),
    new Set(Object.keys(schema.discriminator.mapping)),
  );
});

for (const item of corpus.cases) {
  test(`wire: ${item.id}`, () => {
    const message = structuredClone(item.message);
    assert.equal(validate(message), item.wire_valid, JSON.stringify(validate.errors));
    assert.deepEqual(message, item.message, 'validation must not mutate or coerce input');
  });
}
