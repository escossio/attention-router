const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { dirname, join } = require("node:path");
const { test } = require("node:test");
const sdk = require("@attention-router/integration-sdk");
const cases = JSON.parse(readFileSync(join(__dirname, "conformance-cases.json"), "utf8")).cases;

test("installed package bundles the exact canonical schema", () => {
  const schemaPath = join(dirname(require.resolve("@attention-router/integration-sdk")), "_schema.json");
  assert.equal(readFileSync(schemaPath, "utf8"),
    readFileSync(join(__dirname, "integration-contract.schema.json"), "utf8"));
});

for (const fixture of cases) {
  test(`wire: ${fixture.id}`, () => {
    const input = structuredClone(fixture.message);
    assert.equal(sdk.isIntegrationContract(input), fixture.wire_valid);
    if (fixture.wire_valid) {
      assert.deepEqual(sdk.validateIntegrationContract(input), fixture.message);
      assert.deepEqual(sdk.parseIntegrationContract(JSON.stringify(input)), fixture.message);
      assert.deepEqual(sdk.parseIntegrationContract(sdk.serializeIntegrationContract(input)), fixture.message);
    } else {
      assert.throws(() => sdk.validateIntegrationContract(input), sdk.ContractValidationError);
      assert.throws(() => sdk.parseIntegrationContract(JSON.stringify(input)), sdk.ContractValidationError);
      assert.throws(() => sdk.serializeIntegrationContract(input), sdk.ContractValidationError);
    }
    assert.deepEqual(input, fixture.message);
  });
}

test("validation does not mutate, normalize, add defaults or retain aliases", () => {
  const original = structuredClone(cases[0].message);
  delete original.artifact_ids;
  const before = structuredClone(original);
  const result = sdk.validateIntegrationContract(original);
  assert.deepEqual(original, before);
  assert.deepEqual(result, before);
  result.source.instance_id = "changed";
  assert.deepEqual(original, before);
});

test("schema returned to callers cannot modify validation", () => {
  const schema = sdk.getIntegrationSchema();
  schema.oneOf.length = 0;
  assert.equal(sdk.getIntegrationSchema().$id, sdk.SCHEMA_ID);
  assert(sdk.isIntegrationContract(cases[0].message));
  assert(!sdk.isIntegrationContract({}));
});

test("rejects native non-JSON values, cycles, holes, accessors and omitted properties", () => {
  const cycle = {}; cycle.self = cycle;
  let getterCalled = false;
  const accessor = { get field() { getterCalled = true; return "value"; } };
  const nonEnumerable = Object.defineProperty({}, "hidden", { value: 1 });
  const extraArray = [1]; extraArray.extra = 2;
  class RewrittenArray extends Array { toJSON() { return ["changed"]; } }
  for (const value of [undefined, NaN, Infinity, 1n, new Date(), () => {}, new Map(),
    cycle, Array(2), accessor, nonEnumerable, extraArray, new RewrittenArray("original"),
    { [Symbol("key")]: 1 }]) {
    const message = { ...structuredClone(cases[0].message), payload_ref: { value } };
    assert.throws(() => sdk.validateIntegrationContract(message), sdk.ContractValidationError);
    assert(!sdk.isIntegrationContract(message));
  }
  assert.equal(getterCalled, false);
});

test("JSON and validation errors do not echo payload values", () => {
  for (const text of ['{"synthetic-canary":', "NaN", "Infinity", "1e999", null]) {
    assert.throws(() => sdk.parseIntegrationContract(text), error => {
      assert(error instanceof sdk.ContractValidationError);
      assert(!String(error).includes("synthetic-canary"));
      assert.equal(error.issues[0].keyword, "json");
      return true;
    });
  }
  const message = { ...structuredClone(cases[0].message), schema_version: "synthetic-canary" };
  assert.throws(() => sdk.serializeIntegrationContract(message), error => {
    assert(!String(error).includes("synthetic-canary"));
    assert(error.issues.every(issue => typeof issue.path === "string" && issue.keyword));
    assert(Object.isFrozen(error.issues));
    return true;
  });
});

test("arbitrary JSON keys are preserved without prototype mutation", () => {
  const message = structuredClone(cases[0].message);
  message.payload_ref = JSON.parse('{"__proto__":{"synthetic":true},"constructor":"data"}');
  const result = sdk.parseIntegrationContract(sdk.serializeIntegrationContract(message));
  assert.deepEqual(result, message);
  assert.equal({}.synthetic, undefined);
});
