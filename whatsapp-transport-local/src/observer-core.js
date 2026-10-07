const crypto = require('node:crypto');
const fs = require('node:fs/promises');
const path = require('node:path');

const OBSERVER_SCHEMA_VERSION = 1;
const OBSERVER_PAGE_VERSION = '1';
const OBSERVER_CONSOLE_PREFIX = '**ATTENTION_WA_OBSERVER**';

function parseBoolean(value, fallback) {
  if (value === undefined || value === null || value === '') return fallback;
  const normalized = String(value).trim().toLowerCase();
  if (['1', 'true', 'yes', 'on'].includes(normalized)) return true;
  if (['0', 'false', 'no', 'off'].includes(normalized)) return false;
  return fallback;
}

function parsePositiveInteger(value, fallback) {
  const parsed = Number.parseInt(String(value ?? ''), 10);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : fallback;
}

function normalizeBrowserUrl(value) {
  const candidate = value || 'http://127.0.0.1:9223';
  const url = new URL(candidate);
  if (!['127.0.0.1', 'localhost', '::1'].includes(url.hostname)) {
    throw new Error('observer browser URL must be loopback-only');
  }
  if (!['http:', 'https:'].includes(url.protocol)) {
    throw new Error('observer browser URL must use HTTP or HTTPS');
  }
  return url.toString().replace(/\/$/, '');
}

function createObserverConfig(env = process.env) {
  return {
    browserUrl: normalizeBrowserUrl(env.ATTENTION_WHATSAPP_OBSERVER_BROWSER_URL),
    outputDir: env.ATTENTION_WHATSAPP_OBSERVER_OUTPUT_DIR
      || '/var/lib/attention-router/whatsapp-observer',
    captureBody: parseBoolean(env.ATTENTION_WHATSAPP_OBSERVER_CAPTURE_BODY, true),
    bodyMaxChars: parsePositiveInteger(
      env.ATTENTION_WHATSAPP_OBSERVER_BODY_MAX_CHARS,
      4096,
    ),
    retentionHours: parsePositiveInteger(
      env.ATTENTION_WHATSAPP_OBSERVER_RETENTION_HOURS,
      72,
    ),
    retryMs: parsePositiveInteger(env.ATTENTION_WHATSAPP_OBSERVER_RETRY_MS, 2000),
    cleanupIntervalMs: parsePositiveInteger(
      env.ATTENTION_WHATSAPP_OBSERVER_CLEANUP_INTERVAL_MS,
      15 * 60 * 1000,
    ),
    sourceRevision: env.ATTENTION_WHATSAPP_OBSERVER_SOURCE_SHA || 'unknown',
  };
}

function isWhatsAppWebUrl(value) {
  try {
    return new URL(value).hostname === 'web.whatsapp.com';
  } catch {
    return false;
  }
}

function selectSingleConnected(pageInfos) {
  const connected = pageInfos.filter((item) => item.appState === 'CONNECTED');
  if (connected.length !== 1) {
    return {
      selected: null,
      blocked: true,
      reason: connected.length === 0 ? 'ZERO_CONNECTED_PAGES' : 'MULTIPLE_CONNECTED_PAGES',
      whatsappPageCount: pageInfos.length,
      connectedPageCount: connected.length,
    };
  }
  return {
    selected: connected[0],
    blocked: false,
    reason: null,
    whatsappPageCount: pageInfos.length,
    connectedPageCount: 1,
  };
}

function sha256(value) {
  if (value === null || value === undefined) return null;
  return crypto.createHash('sha256').update(String(value), 'utf8').digest('hex');
}

function textLength(value) {
  if (value === null || value === undefined) return null;
  return Array.from(String(value)).length;
}

function truncateText(value, maximum) {
  if (value === null || value === undefined) return null;
  return Array.from(String(value)).slice(0, maximum).join('');
}

function scalar(value) {
  if (value === null || value === undefined) return null;
  if (typeof value === 'boolean' || typeof value === 'number') return value;
  return String(value);
}

function normalizeProjection(value, config) {
  if (!value || typeof value !== 'object') return null;
  return {
    id: scalar(value.id),
    fromMe: typeof value.fromMe === 'boolean' ? value.fromMe : null,
    from: scalar(value.from),
    to: scalar(value.to),
    author: scalar(value.author),
    body: config.captureBody ? truncateText(scalar(value.body), config.bodyMaxChars) : null,
    type: scalar(value.type),
    ack: scalar(value.ack),
    timestamp: scalar(value.timestamp ?? value.t),
    t: scalar(value.t ?? value.timestamp),
    isNewMsg: typeof value.isNewMsg === 'boolean' ? value.isNewMsg : null,
  };
}

function normalizeObserverEvent(raw, config, receivedAt = new Date()) {
  const body = raw.body === null || raw.body === undefined ? null : String(raw.body);
  const caption = raw.caption === null || raw.caption === undefined ? null : String(raw.caption);
  const messageId = scalar(raw.message_id);
  const bodyLength = Number.isSafeInteger(raw.body_length) ? raw.body_length : textLength(body);
  const captionLength = Number.isSafeInteger(raw.caption_length)
    ? raw.caption_length
    : textLength(caption);
  return {
    schema_version: OBSERVER_SCHEMA_VERSION,
    record_type: 'message_event',
    observer_received_at: receivedAt.toISOString(),
    observer_source_revision: config.sourceRevision,
    page_observed_at: scalar(raw.page_observed_at),
    collection_event: scalar(raw.collection_event),
    change_field: scalar(raw.change_field),
    change_value: raw.change_value ?? null,
    message_id: messageId,
    message_id_hash: raw.message_id_hash || sha256(messageId),
    from_me: typeof raw.from_me === 'boolean' ? raw.from_me : null,
    remote: scalar(raw.remote),
    from: scalar(raw.from),
    to: scalar(raw.to),
    author: scalar(raw.author),
    type: scalar(raw.type),
    subtype: scalar(raw.subtype),
    ack: scalar(raw.ack),
    is_new_msg: typeof raw.is_new_msg === 'boolean' ? raw.is_new_msg : null,
    timestamp: scalar(raw.timestamp),
    body_hash: raw.body_hash || sha256(body),
    body_length: bodyLength,
    body: config.captureBody ? truncateText(body, config.bodyMaxChars) : null,
    caption_hash: raw.caption_hash || sha256(caption),
    caption_length: captionLength,
    caption: config.captureBody ? truncateText(caption, config.bodyMaxChars) : null,
    wwebjs_model_available: Boolean(raw.wwebjs_model_available),
    wwebjs_model_error: raw.wwebjs_model_error
      ? truncateText(String(raw.wwebjs_model_error), 512)
      : null,
    wwebjs_model: normalizeProjection(raw.wwebjs_model, config),
  };
}

function jidKind(value) {
  if (!value) return 'unknown';
  const text = String(value);
  if (text.endsWith('@c.us')) return 'pn';
  if (text.endsWith('@lid')) return 'lid';
  if (text.endsWith('@g.us')) return 'group';
  if (text.endsWith('@broadcast')) return 'broadcast';
  return 'other';
}

function maskJid(value) {
  if (!value) return null;
  const text = String(value);
  const separator = text.lastIndexOf('@');
  if (separator < 0) return text.length <= 4 ? '***' : `${text.slice(0, 2)}***${text.slice(-2)}`;
  const local = text.slice(0, separator);
  const domain = text.slice(separator);
  const masked = local.length <= 4 ? '***' : `${local.slice(0, 2)}***${local.slice(-2)}`;
  return `${masked}${domain}`;
}

function dailyLogFileName(date = new Date()) {
  return `events-${date.toISOString().slice(0, 10)}.jsonl`;
}

function currentLogPath(outputDir, date = new Date()) {
  return path.join(outputDir, dailyLogFileName(date));
}

async function appendJsonLine(outputDir, record, date = new Date()) {
  const file = currentLogPath(outputDir, date);
  const handle = await fs.open(file, 'a', 0o600);
  try {
    await handle.appendFile(`${JSON.stringify(record)}\n`, 'utf8');
    await handle.chmod(0o600);
  } finally {
    await handle.close();
  }
  return file;
}

async function cleanupExpiredLogs(outputDir, retentionHours, nowMs = Date.now()) {
  const removed = [];
  const cutoff = nowMs - (retentionHours * 60 * 60 * 1000);
  let entries;
  try {
    entries = await fs.readdir(outputDir, { withFileTypes: true });
  } catch (error) {
    if (error?.code === 'ENOENT') return removed;
    throw error;
  }
  for (const entry of entries) {
    if (!entry.isFile() || !/^events-\d{4}-\d{2}-\d{2}\.jsonl$/.test(entry.name)) continue;
    const file = path.join(outputDir, entry.name);
    const metadata = await fs.lstat(file);
    if (metadata.isFile() && metadata.mtimeMs < cutoff) {
      await fs.unlink(file);
      removed.push(file);
    }
  }
  return removed;
}

function createObserverStatus(config) {
  return {
    source_revision: config.sourceRevision,
    service_state: 'STARTING',
    browser_connected: false,
    whatsapp_page_count: 0,
    connected_page_count: 0,
    listener_attached: false,
    app_state: null,
    events_seen: 0,
    last_event_at: null,
    last_attach_at: null,
    last_error_at: null,
    capture_body: config.captureBody,
    retention_hours: config.retentionHours,
    current_log_file: currentLogPath(config.outputDir),
  };
}

function serializeStatus(status, generatedAt = new Date()) {
  return {
    generated_at: generatedAt.toISOString(),
    source_revision: status.source_revision,
    service_state: status.service_state,
    browser_connected: Boolean(status.browser_connected),
    whatsapp_page_count: Number(status.whatsapp_page_count) || 0,
    connected_page_count: Number(status.connected_page_count) || 0,
    listener_attached: Boolean(status.listener_attached),
    app_state: status.app_state || null,
    events_seen: Number(status.events_seen) || 0,
    last_event_at: status.last_event_at || null,
    last_attach_at: status.last_attach_at || null,
    last_error_at: status.last_error_at || null,
    capture_body: Boolean(status.capture_body),
    retention_hours: Number(status.retention_hours),
    current_log_file: status.current_log_file,
  };
}

async function writeStatusAtomic(outputDir, status) {
  const destination = path.join(outputDir, 'status.json');
  const temporary = path.join(
    outputDir,
    `.status.json.${process.pid}.${crypto.randomBytes(6).toString('hex')}.tmp`,
  );
  try {
    await fs.writeFile(temporary, `${JSON.stringify(serializeStatus(status), null, 2)}\n`, {
      encoding: 'utf8',
      mode: 0o600,
      flag: 'wx',
    });
    await fs.chmod(temporary, 0o600);
    await fs.rename(temporary, destination);
    await fs.chmod(destination, 0o600);
  } catch (error) {
    await fs.unlink(temporary).catch(() => {});
    throw error;
  }
  return destination;
}

async function pageObserverInstaller(options) {
  const markerKey = '__attentionRouterWhatsAppObserver';
  const root = window;
  const serialized = (value) => {
    if (value === null || value === undefined) return null;
    if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
      return value;
    }
    try {
      return value._serialized || value.serialized || value.toString?.() || null;
    } catch {
      return null;
    }
  };
  const read = (model, name) => {
    if (!model) return null;
    try {
      if (model[name] !== undefined) return model[name];
      if (typeof model.get === 'function') return model.get(name);
      return model.attributes?.[name] ?? null;
    } catch {
      return null;
    }
  };
  const appState = () => {
    const primary = root.AuthStore?.AppState?.state;
    if (primary) return String(primary);
    try {
      return String(root.require('WAWebSocketModel')?.Socket?.state || 'UNKNOWN');
    } catch {
      return 'UNKNOWN';
    }
  };
  const text = (value) => (value === null || value === undefined ? null : String(value));
  const length = (value) => (value === null ? null : Array.from(value).length);
  const truncate = (value, maximum) => (
    value === null ? null : Array.from(value).slice(0, maximum).join('')
  );
  const digest = async (value) => {
    if (value === null) return null;
    try {
      const bytes = new TextEncoder().encode(value);
      const result = await root.crypto.subtle.digest('SHA-256', bytes);
      return Array.from(new Uint8Array(result))
        .map((byte) => byte.toString(16).padStart(2, '0'))
        .join('');
    } catch {
      return null;
    }
  };
  const projection = (model) => {
    if (!model) return null;
    const modelId = read(model, 'id');
    return {
      id: serialized(modelId),
      fromMe: read(model, 'fromMe') ?? read(modelId, 'fromMe') ?? null,
      from: serialized(read(model, 'from')),
      to: serialized(read(model, 'to')),
      author: serialized(read(model, 'author')),
      body: options.captureBody
        ? truncate(text(read(model, 'body')), options.bodyMaxChars)
        : null,
      type: serialized(read(model, 'type')),
      ack: serialized(read(model, 'ack')),
      timestamp: serialized(read(model, 'timestamp') ?? read(model, 't')),
      t: serialized(read(model, 't') ?? read(model, 'timestamp')),
      isNewMsg: read(model, 'isNewMsg') ?? null,
    };
  };
  const attachedInfo = (reused, marker, Msg) => {
    let me = null;
    try {
      me = root.require('WAWebUserPrefsMeUser');
    } catch {
      me = null;
    }
    return {
      ok: true,
      reused,
      attached_at: marker.attached_at,
      self_pn: serialized(me?.getMaybeMePnUser?.()),
      self_lid: serialized(me?.getMaybeMeLidUser?.()),
      app_state: appState(),
      page_url: root.location?.href || null,
      msg_count: Number(Msg.length ?? Msg.models?.length ?? 0),
      wwebjs_available: Boolean(root.WWebJS),
      get_message_model_available: typeof root.WWebJS?.getMessageModel === 'function',
      on_add_message_event_available: typeof root.onAddMessageEvent === 'function',
    };
  };

  let Msg;
  try {
    Msg = root.require('WAWebCollections')?.Msg;
  } catch (error) {
    return { ok: false, error: String(error?.message || error) };
  }
  if (!Msg || typeof Msg.on !== 'function') {
    return { ok: false, error: 'WAWebCollections.Msg.on unavailable' };
  }

  const existing = root[markerKey];
  if (
    existing?.version === options.version
    && existing?.prefix === options.prefix
    && existing?.capture_body === options.captureBody
    && existing?.body_max_chars === options.bodyMaxChars
    && existing?.collection === Msg
    && typeof existing?.handler === 'function'
  ) {
    return attachedInfo(true, existing, Msg);
  }
  if (
    existing?.collection
    && typeof existing.collection.off === 'function'
    && typeof existing.handler === 'function'
  ) {
    existing.collection.off('all', existing.handler);
  }

  const emit = async (collectionEvent, args) => {
    if (
      collectionEvent !== 'add'
      && collectionEvent !== 'remove'
      && collectionEvent !== 'change'
      && !String(collectionEvent).startsWith('change:')
    ) return;
    const message = args[0];
    if (!message) return;
    const id = read(message, 'id');
    const messageId = text(serialized(id));
    const body = text(read(message, 'body'));
    const caption = text(read(message, 'caption'));
    let wwebjsModel = null;
    let wwebjsError = null;
    const wwebjsAvailable = typeof root.WWebJS?.getMessageModel === 'function';
    if (wwebjsAvailable) {
      try {
        wwebjsModel = projection(root.WWebJS.getMessageModel(message));
      } catch (error) {
        wwebjsError = String(error?.message || error);
      }
    }
    const [messageIdHash, bodyHash, captionHash] = await Promise.all([
      digest(messageId),
      digest(body),
      digest(caption),
    ]);
    const changeValue = serialized(args[1]);
    const payload = {
      page_observed_at: new Date().toISOString(),
      collection_event: String(collectionEvent),
      change_field: String(collectionEvent).startsWith('change:')
        ? String(collectionEvent).slice('change:'.length)
        : null,
      change_value: typeof changeValue === 'string'
        ? truncate(changeValue, 512)
        : changeValue,
      message_id: messageId,
      message_id_hash: messageIdHash,
      from_me: read(message, 'fromMe') ?? read(id, 'fromMe') ?? null,
      remote: serialized(read(id, 'remote')),
      from: serialized(read(message, 'from')),
      to: serialized(read(message, 'to')),
      author: serialized(read(message, 'author')),
      type: serialized(read(message, 'type')),
      subtype: serialized(read(message, 'subtype')),
      ack: serialized(read(message, 'ack')),
      is_new_msg: read(message, 'isNewMsg') ?? null,
      timestamp: serialized(read(message, 'timestamp') ?? read(message, 't')),
      body_hash: bodyHash,
      body_length: length(body),
      body: options.captureBody ? truncate(body, options.bodyMaxChars) : null,
      caption_hash: captionHash,
      caption_length: length(caption),
      caption: options.captureBody ? truncate(caption, options.bodyMaxChars) : null,
      wwebjs_model_available: wwebjsAvailable,
      wwebjs_model_error: wwebjsError,
      wwebjs_model: wwebjsModel,
    };
    root.console.log(`${options.prefix}${JSON.stringify(payload)}`);
  };
  const handler = (eventName, ...args) => {
    void emit(eventName, args);
  };
  Msg.on('all', handler);
  const marker = {
    version: options.version,
    prefix: options.prefix,
    capture_body: options.captureBody,
    body_max_chars: options.bodyMaxChars,
    attached_at: new Date().toISOString(),
    collection: Msg,
    handler,
  };
  root[markerKey] = marker;
  return attachedInfo(false, marker, Msg);
}

function pageObserverMarkerState(version, prefix, captureBody, bodyMaxChars) {
  const marker = window.__attentionRouterWhatsAppObserver;
  return Boolean(
    marker
    && marker.version === version
    && marker.prefix === prefix
    && marker.capture_body === captureBody
    && marker.body_max_chars === bodyMaxChars
    && typeof marker.handler === 'function',
  );
}

function removePageObserver() {
  const marker = window.__attentionRouterWhatsAppObserver;
  if (!marker) return false;
  if (marker.collection && typeof marker.collection.off === 'function' && typeof marker.handler === 'function') {
    marker.collection.off('all', marker.handler);
  }
  delete window.__attentionRouterWhatsAppObserver;
  return true;
}

function withTimeout(promise, milliseconds, fallback) {
  let timer;
  return Promise.race([
    promise,
    new Promise((resolve) => {
      timer = setTimeout(() => resolve(fallback), milliseconds);
    }),
  ]).finally(() => clearTimeout(timer));
}

class ObserverService {
  constructor({ config, puppeteer, logger = console }) {
    this.config = config;
    this.puppeteer = puppeteer;
    this.logger = logger;
    this.status = createObserverStatus(config);
    this.browser = null;
    this.selectedPage = null;
    this.consoleHandler = (message) => this.handleConsole(message);
    this.stopping = false;
    this.writeChain = Promise.resolve();
    this.nextCleanupAt = 0;
    this.lastOperationalSignatures = new Map();
    this.wake = null;
    this.activeOperation = null;
  }

  operational(event, details = {}, level = 'log', dedupeKey = null) {
    const signature = dedupeKey === null ? null : `${event}:${dedupeKey}`;
    if (signature && this.lastOperationalSignatures.get(event) === signature) return;
    if (signature) this.lastOperationalSignatures.set(event, signature);
    const method = typeof this.logger[level] === 'function' ? level : 'log';
    this.logger[method](JSON.stringify({
      ts: new Date().toISOString(),
      event,
      ...details,
    }));
  }

  noteError(error, state = 'ERROR') {
    const message = String(error?.message || error);
    this.status.last_error_at = new Date().toISOString();
    this.status.service_state = state;
    this.operational('observer_error', { error: message }, 'error', `${state}:${message}`);
  }

  queueStatusWrite() {
    this.writeChain = this.writeChain
      .then(() => writeStatusAtomic(this.config.outputDir, this.status))
      .catch((error) => {
        this.status.last_error_at = new Date().toISOString();
        this.operational(
          'observer_error',
          { error: `status write failed: ${String(error?.message || error)}` },
          'error',
          `status:${String(error?.message || error)}`,
        );
      });
    return this.writeChain;
  }

  queueRecord(record, countEvent = false) {
    this.writeChain = this.writeChain.then(async () => {
      const now = new Date();
      this.status.current_log_file = await appendJsonLine(this.config.outputDir, record, now);
      if (countEvent) {
        this.status.events_seen += 1;
        this.status.last_event_at = record.observer_received_at;
      }
      await writeStatusAtomic(this.config.outputDir, this.status);
    }).catch((error) => {
      this.noteError(`event persistence failed: ${String(error?.message || error)}`);
    });
    return this.writeChain;
  }

  handleConsole(message) {
    let text;
    try {
      text = message.text();
    } catch {
      return;
    }
    if (!text.startsWith(OBSERVER_CONSOLE_PREFIX)) return;
    let raw;
    try {
      raw = JSON.parse(text.slice(OBSERVER_CONSOLE_PREFIX.length));
    } catch (error) {
      this.noteError(`observer console JSON invalid: ${String(error?.message || error)}`);
      void this.queueStatusWrite();
      return;
    }
    const record = normalizeObserverEvent(raw, this.config, new Date());
    void this.queueRecord(record, true);
    this.operational('observer_event', {
      collection_event: record.collection_event,
      message_id_hash: record.message_id_hash?.slice(0, 12) || null,
      from_me: record.from_me,
      remote_kind: jidKind(record.remote),
      from_kind: jidKind(record.from),
      to_kind: jidKind(record.to),
      type: record.type,
      ack: record.ack,
      is_new_msg: record.is_new_msg,
      body_preview: this.config.captureBody ? truncateText(record.body, 160) : null,
    });
  }

  async connectBrowser() {
    if (this.browser?.isConnected?.()) return true;
    try {
      const browser = await this.puppeteer.connect({ browserURL: this.config.browserUrl });
      if (this.stopping) {
        browser.disconnect();
        return false;
      }
      this.browser = browser;
      this.status.browser_connected = true;
      browser.once?.('disconnected', () => {
        if (this.browser === browser) {
          this.browser = null;
          this.selectedPage = null;
          this.status.browser_connected = false;
          this.status.listener_attached = false;
          this.status.service_state = 'BROWSER_UNAVAILABLE';
          this.operational('observer_browser_disconnected');
          void this.queueStatusWrite();
        }
      });
      this.operational(
        'observer_browser_connected',
        { browser_url: '[loopback]' },
        'log',
        String(Date.now()),
      );
      return true;
    } catch (error) {
      this.browser = null;
      this.status.browser_connected = false;
      this.status.listener_attached = false;
      this.status.service_state = 'BROWSER_UNAVAILABLE';
      this.status.last_error_at = new Date().toISOString();
      this.operational(
        'observer_browser_disconnected',
        { error: String(error?.message || error) },
        'warn',
        String(error?.message || error),
      );
      return false;
    }
  }

  async pageInfo(page) {
    const appState = await withTimeout(
      page.evaluate(() => {
        const primary = window.AuthStore?.AppState?.state;
        if (primary) return String(primary);
        try {
          return String(window.require('WAWebSocketModel')?.Socket?.state || 'UNKNOWN');
        } catch {
          return 'UNKNOWN';
        }
      }).catch(() => 'UNAVAILABLE'),
      Math.min(this.config.retryMs, 1500),
      'TIMEOUT',
    );
    return { page, url: page.url(), appState };
  }

  attachNodeConsole(page) {
    if (this.selectedPage === page) return;
    if (this.selectedPage) this.selectedPage.off?.('console', this.consoleHandler);
    this.selectedPage = page;
    page.on('console', this.consoleHandler);
  }

  async detachSelectedPage(removeMarker = false) {
    const page = this.selectedPage;
    if (!page) return;
    page.off?.('console', this.consoleHandler);
    this.selectedPage = null;
    if (removeMarker && !page.isClosed?.()) {
      await page.evaluate(removePageObserver).catch(() => {});
    }
  }

  async attachObserver(page) {
    this.status.service_state = 'ATTACHING';
    this.status.listener_attached = false;
    await this.queueStatusWrite();
    this.attachNodeConsole(page);
    const result = await page.evaluate(pageObserverInstaller, {
      version: OBSERVER_PAGE_VERSION,
      prefix: OBSERVER_CONSOLE_PREFIX,
      captureBody: this.config.captureBody,
      bodyMaxChars: this.config.bodyMaxChars,
    });
    if (!result?.ok) throw new Error(result?.error || 'page observer attach failed');
    const receivedAt = new Date();
    const record = {
      schema_version: OBSERVER_SCHEMA_VERSION,
      record_type: 'observer_attached',
      observer_received_at: receivedAt.toISOString(),
      observer_source_revision: this.config.sourceRevision,
      listener_reused: Boolean(result.reused),
      listener_version: OBSERVER_PAGE_VERSION,
      listener_attached_at: result.attached_at,
      self_pn: result.self_pn,
      self_pn_kind: jidKind(result.self_pn),
      self_lid: result.self_lid,
      self_lid_kind: jidKind(result.self_lid),
      app_state: result.app_state,
      page_url: result.page_url,
      msg_count: result.msg_count,
      wwebjs_available: Boolean(result.wwebjs_available),
      get_message_model_available: Boolean(result.get_message_model_available),
      on_add_message_event_available: Boolean(result.on_add_message_event_available),
    };
    this.status.service_state = 'READY';
    this.status.listener_attached = true;
    this.status.app_state = result.app_state;
    this.status.last_attach_at = receivedAt.toISOString();
    await this.queueRecord(record, false);
    this.operational('observer_attached', {
      listener_reused: record.listener_reused,
      self_pn: maskJid(record.self_pn),
      self_lid: maskJid(record.self_lid),
      self_pn_kind: record.self_pn_kind,
      self_lid_kind: record.self_lid_kind,
      app_state: record.app_state,
      msg_count: record.msg_count,
      wwebjs_available: record.wwebjs_available,
      get_message_model_available: record.get_message_model_available,
      on_add_message_event_available: record.on_add_message_event_available,
    });
  }

  async reconcile() {
    if (!(await this.connectBrowser())) {
      await this.queueStatusWrite();
      return;
    }
    let pages;
    try {
      pages = await this.browser.pages();
    } catch (error) {
      this.noteError(error, 'BROWSER_UNAVAILABLE');
      this.status.browser_connected = false;
      this.status.listener_attached = false;
      if (this.browser?.isConnected?.()) this.browser.disconnect();
      this.browser = null;
      await this.queueStatusWrite();
      return;
    }
    const whatsappPages = pages.filter((page) => isWhatsAppWebUrl(page.url()));
    const pageInfos = [];
    for (const page of whatsappPages) pageInfos.push(await this.pageInfo(page));
    const selection = selectSingleConnected(pageInfos);
    this.status.whatsapp_page_count = selection.whatsappPageCount;
    this.status.connected_page_count = selection.connectedPageCount;
    if (selection.blocked) {
      await this.detachSelectedPage(true);
      this.status.service_state = 'BLOCKED_PAGE_SELECTION';
      this.status.listener_attached = false;
      this.status.app_state = pageInfos.length === 1 ? pageInfos[0].appState : null;
      this.operational('observer_page_selection_blocked', {
        reason: selection.reason,
        whatsapp_page_count: selection.whatsappPageCount,
        connected_page_count: selection.connectedPageCount,
      }, 'warn', `${selection.reason}:${selection.whatsappPageCount}:${selection.connectedPageCount}`);
      await this.queueStatusWrite();
      return;
    }

    const { page, appState } = selection.selected;
    this.status.app_state = appState;
    let markerAttached = false;
    if (this.selectedPage === page && this.status.listener_attached) {
      markerAttached = await page.evaluate(
        pageObserverMarkerState,
        OBSERVER_PAGE_VERSION,
        OBSERVER_CONSOLE_PREFIX,
        this.config.captureBody,
        this.config.bodyMaxChars,
      ).catch(() => false);
    }
    if (!markerAttached) {
      if (this.selectedPage && this.selectedPage !== page) await this.detachSelectedPage(true);
      this.operational('observer_reattaching', { app_state: appState });
      try {
        await this.attachObserver(page);
      } catch (error) {
        this.status.listener_attached = false;
        this.noteError(error, 'ATTACHING');
        await this.queueStatusWrite();
        return;
      }
    } else {
      this.status.service_state = 'READY';
      this.status.listener_attached = true;
      await this.queueStatusWrite();
    }
  }

  async cleanupLogs() {
    try {
      const removed = await cleanupExpiredLogs(
        this.config.outputDir,
        this.config.retentionHours,
      );
      if (removed.length > 0) {
        this.operational('observer_retention_cleanup', { files_removed: removed.length });
      }
    } catch (error) {
      this.noteError(`retention cleanup failed: ${String(error?.message || error)}`);
    }
  }

  waitForRetry() {
    if (this.stopping) return Promise.resolve();
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.wake = null;
        resolve();
      }, this.config.retryMs);
      this.wake = () => {
        clearTimeout(timer);
        this.wake = null;
        resolve();
      };
    });
  }

  async start() {
    await fs.mkdir(this.config.outputDir, { recursive: true, mode: 0o700 });
    await fs.chmod(this.config.outputDir, 0o700);
    this.operational('observer_starting', {
      source_revision: this.config.sourceRevision,
      capture_body: this.config.captureBody,
      retention_hours: this.config.retentionHours,
    });
    await this.cleanupLogs();
    this.nextCleanupAt = Date.now() + this.config.cleanupIntervalMs;
    await this.queueStatusWrite();
    while (!this.stopping) {
      this.activeOperation = this.reconcile().catch(async (error) => {
        this.noteError(error);
        await this.queueStatusWrite();
      });
      await this.activeOperation;
      this.activeOperation = null;
      if (Date.now() >= this.nextCleanupAt) {
        await this.cleanupLogs();
        this.nextCleanupAt = Date.now() + this.config.cleanupIntervalMs;
      }
      await this.waitForRetry();
    }
  }

  async shutdown(signal = 'SIGTERM') {
    if (this.stopping) return;
    this.stopping = true;
    this.wake?.();
    this.operational('observer_stopping', { signal });
    await this.activeOperation?.catch(() => {});
    await this.detachSelectedPage(false);
    if (this.browser?.isConnected?.()) this.browser.disconnect();
    this.browser = null;
    this.status.browser_connected = false;
    this.status.listener_attached = false;
    this.status.service_state = 'STOPPED';
    await this.writeChain;
    await writeStatusAtomic(this.config.outputDir, this.status).catch((error) => {
      this.operational(
        'observer_error',
        { error: `shutdown status write failed: ${String(error?.message || error)}` },
        'error',
      );
    });
  }
}

module.exports = {
  OBSERVER_CONSOLE_PREFIX,
  OBSERVER_PAGE_VERSION,
  OBSERVER_SCHEMA_VERSION,
  ObserverService,
  appendJsonLine,
  cleanupExpiredLogs,
  createObserverConfig,
  createObserverStatus,
  currentLogPath,
  dailyLogFileName,
  isWhatsAppWebUrl,
  jidKind,
  maskJid,
  normalizeObserverEvent,
  pageObserverInstaller,
  pageObserverMarkerState,
  removePageObserver,
  selectSingleConnected,
  serializeStatus,
  sha256,
  truncateText,
  writeStatusAtomic,
};
