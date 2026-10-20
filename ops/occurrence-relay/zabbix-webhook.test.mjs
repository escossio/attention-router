import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const source = readFileSync(new URL("./zabbix-webhook.js", import.meta.url), "utf8");
const execWebhook = new Function("value", "HttpRequest", source);

function params(overrides = {}) {
  return {
    endpoint: "https://relay.invalid/v1/zabbix/events",
    secret: "synthetic-protected-token-value-1234567890123456",
    event_source: "0",
    event_id: "10123",
    event_recovery_id: "*UNKNOWN*",
    event_ts: "1792252800",
    recovery_ts: "*UNKNOWN*",
    host_id: "2001",
    trigger_id: "333",
    event_name: "Synthetic port alert",
    severity: "4",
    ...overrides,
  };
}

function execute(config = params(), responseCode = 201) {
  let last = null;
  const requestMock = class {
    headers = [];
    addHeader(value) { this.headers.push(value); }
    post(url, body) {
      last = { url, body: JSON.parse(body), headers: this.headers };
      return "{}";
    }
    getStatus() { return responseCode; }
  };
  const result = JSON.parse(execWebhook(JSON.stringify(config), requestMock));
  return { result, request: last };
}

test("problem event maps stable external ID and Zabbix equipment", () => {
  const { result, request } = execute();
  assert.equal(result.status, "accepted");
  assert.equal(request.body.event_id, "problem:10123");
  assert.equal(request.body.status, "PROBLEM");
  assert.equal(request.body.asset_ref, "zabbix:host:2001");
  assert.equal(request.body.signal_code, "ZBX_TRIGGER:333");
  assert.equal(request.body.severity, 4);
  assert.equal(request.body.occurred_at, new Date(1792252800 * 1000).toISOString());
  assert.equal(request.body.problem_event_id, undefined);
  assert.equal(request.headers.filter(x => x.startsWith("Authorization: ")).length, 1);
});

test("recovery receives distinct event ID and references problem", () => {
  const { request } = execute(params({
    event_recovery_id: "10124",
    recovery_ts: "1792253000",
  }), 200);
  assert.equal(request.body.event_id, "recovery:10124");
  assert.equal(request.body.problem_event_id, "problem:10123");
  assert.equal(request.body.status, "RECOVERY");
  assert.equal(request.body.occurred_at, new Date(1792253000 * 1000).toISOString());
});

test("unknown recovery id remains problem", () => {
  assert.equal(execute(params({ event_recovery_id: "*UNKNOWN*" })).request.body.status, "PROBLEM");
});

test("rejects unsupported source, endpoint, missing host and severity", () => {
  for (const value of [
    { event_source: "1" },
    { endpoint: "http://127.0.0.1:18123/v1/zabbix/events" },
    { host_id: "{HOST.ID}" },
    { severity: "99" },
    { event_id: "" },
    { event_recovery_id: "1234", recovery_ts: "*UNKNOWN*" },
  ]) {
    assert.throws(() => execute(params(value)));
  }
});

test("does not treat failed HTTP as successful delivery", () => {
  assert.throws(() => execute(params(), 503), /OCCURRENCE_RELAY_DID_NOT_ACCEPT/);
});
