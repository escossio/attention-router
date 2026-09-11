import assert from "node:assert/strict";
import { test } from "node:test";
import { SCHEMA_ID, getIntegrationSchema, isIntegrationContract } from "@attention-router/integration-sdk";

test("installed package supports named ESM imports", () => {
  assert.equal(getIntegrationSchema().$id, SCHEMA_ID);
  assert.equal(isIntegrationContract({}), false);
});
