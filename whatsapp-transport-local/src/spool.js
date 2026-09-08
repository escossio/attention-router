const fs = require('node:fs');
const path = require('node:path');
const { stableHash, headersForBody } = require('./hmac');

const RETRY_MS = [2000, 5000, 15000, 30000, 60000];
const pendingDeliveryFlights = new Map();
const inboundDrainFlights = new WeakMap();

function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  fs.chmodSync(dir, 0o700);
}

function ensureSpool(config) {
  for (const dir of [config.inboundSpoolDir, config.inboundPendingDir, config.inboundSendingDir, config.inboundSentDir, config.inboundQuarantineDir].filter(Boolean)) {
    ensureDir(dir);
  }
}

function fileForId(dir, idempotencyKey) {
  return path.join(dir, `${stableHash(idempotencyKey)}.json`);
}

function atomicWriteJson(file, payload) {
  const body = Buffer.from(JSON.stringify(payload));
  const tmp = `${file}.${process.pid}.${Date.now()}.tmp`;
  const fd = fs.openSync(tmp, 'wx', 0o600);
  try {
    fs.writeFileSync(fd, body);
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
  fs.renameSync(tmp, file);
  return body;
}

function enqueuePending(config, normalized, payload = normalized) {
  ensureSpool(config);
  const file = fileForId(config.inboundPendingDir, normalized.identity.idempotency_key);
  if (!fs.existsSync(file)) atomicWriteJson(file, payload);
  return { file, status: 'pending', idempotency_key: normalized.identity.idempotency_key };
}

function listPending(config) {
  ensureSpool(config);
  return fs.readdirSync(config.inboundPendingDir).filter((name) => name.endsWith('.json')).sort().map((name) => path.join(config.inboundPendingDir, name));
}

function moveToQuarantine(config, file) {
  ensureSpool(config);
  const dest = path.join(config.inboundQuarantineDir, path.basename(file));
  fs.renameSync(file, dest);
  return dest;
}

function markSending(config, normalized) {
  ensureSpool(config);
  const from = fileForId(config.inboundPendingDir, normalized.identity.idempotency_key);
  const to = fileForId(config.inboundSendingDir, normalized.identity.idempotency_key);
  atomicWriteJson(to, { ...normalized, status: 'sending', sending_at: new Date().toISOString() });
  if (fs.existsSync(from)) fs.unlinkSync(from);
  return to;
}

function markSent(config, normalized) {
  ensureSpool(config);
  const from = fileForId(config.inboundSendingDir, normalized.identity.idempotency_key);
  const to = fileForId(config.inboundSentDir, normalized.identity.idempotency_key);
  atomicWriteJson(to, { ...normalized, status: 'sent', sent_at: new Date().toISOString() });
  if (fs.existsSync(from)) fs.unlinkSync(from);
  return to;
}

async function deliverPendingFileOnce(config, file, logger, fetchImpl = fetch) {
  if (!fs.existsSync(file)) {
    return { done: false, status: 'missing' };
  }
  const body = fs.readFileSync(file);
  const parsed = JSON.parse(body.toString('utf8'));
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), config.inboundHttpTimeoutMs);
  try {
    const headers = headersForBody({ secret: config.hmacSecret, body, now: Date.now });
    const response = await fetchImpl(config.inboundForwardUrl, {
      method: 'POST',
      headers,
      body,
      signal: controller.signal,
    });
    const text = await response.text();
    let json = null;
    try {
      json = text ? JSON.parse(text) : null;
    } catch {
      json = null;
    }
    if ([200, 201, 202].includes(response.status)) {
      fs.unlinkSync(file);
      return { done: true, status: json?.status || 'accepted', response: json, http_status: response.status };
    }
    if (response.status === 409) {
      moveToQuarantine(config, file);
      return { done: true, status: 'conflict', http_status: response.status };
    }
    if (response.status === 400 || response.status === 422) {
      moveToQuarantine(config, file);
      return { done: true, status: 'bad_payload', http_status: response.status };
    }
    if (response.status === 401 || response.status === 403) {
      return { done: false, stop: true, status: 'auth_failed', http_status: response.status };
    }
    return { done: false, status: 'retry', http_status: response.status };
  } catch (error) {
    return { done: false, status: 'retry', error: String(error?.message || error) };
  } finally {
    clearTimeout(timeout);
  }
}

async function deliverPendingFile(config, file, logger, fetchImpl = fetch) {
  const flightKey = path.resolve(file);
  const active = pendingDeliveryFlights.get(flightKey);
  if (active) return active;
  const flight = deliverPendingFileOnce(config, file, logger, fetchImpl);
  pendingDeliveryFlights.set(flightKey, flight);
  try {
    return await flight;
  } finally {
    if (pendingDeliveryFlights.get(flightKey) === flight) {
      pendingDeliveryFlights.delete(flightKey);
    }
  }
}

async function drainInboundPendingOnce(config, logger, fetchImpl) {
  if (!config.inboundForwardEnabled || !config.inboundForwardUrl || !config.inboundPendingDir) {
    return { attempted: 0, delivered: 0, stop: false, status: 'disabled' };
  }
  let attempted = 0;
  let delivered = 0;
  for (const file of listPending(config)) {
    attempted += 1;
    const result = await deliverPendingFile(config, file, logger, fetchImpl);
    if (result.done) delivered += 1;
    if (result.stop) {
      return { attempted, delivered, stop: true, status: result.status };
    }
  }
  return { attempted, delivered, stop: false, status: 'complete' };
}

async function drainInboundPending(config, logger = console, fetchImpl = fetch) {
  const active = inboundDrainFlights.get(config);
  if (active) return active;
  const flight = drainInboundPendingOnce(config, logger, fetchImpl);
  inboundDrainFlights.set(config, flight);
  try {
    return await flight;
  } finally {
    if (inboundDrainFlights.get(config) === flight) {
      inboundDrainFlights.delete(config);
    }
  }
}

function startInboundPendingDrain(
  config,
  logger = console,
  fetchImpl = fetch,
  intervalMs = 2000,
) {
  let stopped = false;
  let authFailed = false;
  let timer = null;
  const run = async () => {
    if (stopped || authFailed) return { stop: authFailed, status: 'stopped' };
    const result = await drainInboundPending(config, logger, fetchImpl);
    if (result.stop && result.status === 'auth_failed') {
      authFailed = true;
      if (timer) clearInterval(timer);
      logger.error?.('inbound_spool_drain_auth_failed');
    }
    return result;
  };
  const reportFailure = (error) => {
    logger.error?.('inbound_spool_drain_failed', {
      error_class: error?.name || 'Error',
    });
    return { attempted: 0, delivered: 0, stop: false, status: 'error' };
  };
  const startupPromise = run().catch(reportFailure);
  timer = setInterval(() => {
    void run().catch(reportFailure);
  }, intervalMs);
  timer.unref();
  void startupPromise.then(() => {
    if (authFailed && timer) clearInterval(timer);
  });
  return {
    startupPromise,
    isAuthFailed: () => authFailed,
    stop() {
      stopped = true;
      clearInterval(timer);
    },
  };
}

module.exports = {
  RETRY_MS,
  atomicWriteJson,
  drainInboundPending,
  deliverPendingFile,
  enqueuePending,
  ensureSpool,
  fileForId,
  listPending,
  markSending,
  markSent,
  moveToQuarantine,
  startInboundPendingDrain,
};
