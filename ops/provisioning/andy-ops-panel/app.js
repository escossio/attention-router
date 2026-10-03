"use strict";

const state = {
  tab: "compute",
  lastPayload: null,
  timer: null,
  config: null,
};

const observabilitySources = {
  network: {
    key: "network_osi_url",
    frame: "#network-frame",
    status: "#network-state",
    open: "#network-open",
  },
  goaccess: {
    key: "goaccess_url",
    frame: "#goaccess-frame",
    status: "#goaccess-state",
    open: "#goaccess-open",
  },
  dozzle: {
    key: "dozzle_url",
    frame: "#dozzle-frame",
    status: "#dozzle-state",
    open: "#dozzle-open",
  },
};

const $ = (selector) => document.querySelector(selector);

function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return "—";
  let value = Math.max(0, Math.floor(Number(seconds)));
  const hours = Math.floor(value / 3600);
  value %= 3600;
  const minutes = Math.floor(value / 60);
  const secs = value % 60;
  if (hours) return String(hours).padStart(2, "0") + ":" + String(minutes).padStart(2, "0") + ":" + String(secs).padStart(2, "0");
  return String(minutes).padStart(2, "0") + ":" + String(secs).padStart(2, "0");
}

function fmtAge(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 2) return "agora";
  if (seconds < 60) return seconds + "s";
  if (seconds < 3600) return Math.floor(seconds / 60) + "m " + (seconds % 60) + "s";
  return Math.floor(seconds / 3600) + "h " + Math.floor((seconds % 3600) / 60) + "m";
}

function fmtTime(value) {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function pct(value) {
  if (value === null || value === undefined) return null;
  const n = Number(value);
  return Number.isFinite(n) ? Math.max(0, Math.min(100, n)) : null;
}

function buildBlocks(value, count, className) {
  const holder = document.createElement("div");
  holder.className = className;
  const number = pct(value);
  const active = number === null ? 0 : Math.round((number / 100) * count);
  for (let i = 0; i < count; i += 1) {
    const cell = document.createElement("span");
    cell.className = className === "cpu-blocks" ? "cpu-block" : "core-cell";
    if (i < active) {
      cell.classList.add("on");
      if (className === "cpu-blocks" && number >= 90) cell.classList.add("critical");
      else if (className === "cpu-blocks" && number >= 75) cell.classList.add("hot");
    }
    holder.append(cell);
  }
  return holder;
}

function nodeCard(node) {
  const article = document.createElement("article");
  article.className = "node-card " + (node.reachable ? String(node.state || "idle").toLowerCase() : "offline");

  const head = document.createElement("div");
  head.className = "node-head";
  const title = document.createElement("div");
  title.innerHTML = '<span class="node-name"></span><span class="node-role"></span>';
  title.querySelector(".node-name").textContent = node.label;
  title.querySelector(".node-role").textContent = node.role;

  const pill = document.createElement("span");
  const displayState = node.reachable ? (node.state || "IDLE") : "OFFLINE";
  pill.className = "state-pill " + displayState.toLowerCase();
  pill.textContent = displayState;
  head.append(title, pill);

  const overall = document.createElement("div");
  overall.className = "cpu-overall";
  const overallValue = pct(node.cpu_percent);
  const cpuText = document.createElement("strong");
  cpuText.textContent = overallValue === null ? "—" : Math.round(overallValue) + "%";
  const cpuLabel = document.createElement("span");
  cpuLabel.textContent = "CPU TOTAL";
  overall.append(cpuText, cpuLabel);

  const blocks = buildBlocks(overallValue, 20, "cpu-blocks");

  const cores = document.createElement("div");
  cores.className = "core-grid";
  if (Array.isArray(node.cores) && node.cores.length) {
    node.cores.forEach((core, index) => {
      const row = document.createElement("div");
      row.className = "core-line";
      const label = document.createElement("span");
      label.className = "core-label";
      label.textContent = "C" + index;
      const bar = buildBlocks(core, 10, "core-bar");
      const value = document.createElement("span");
      value.className = "core-value";
      value.textContent = Math.round(core) + "%";
      row.append(label, bar, value);
      cores.append(row);
    });
  } else {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = node.reachable ? "Aguardando segunda amostra de CPU…" : "Sem telemetria";
    cores.append(empty);
  }

  const meta = document.createElement("div");
  meta.className = "node-meta";
  const temp = document.createElement("div");
  temp.innerHTML = "<small>TEMPERATURA</small><strong></strong>";
  temp.querySelector("strong").textContent = node.temperature_c === null || node.temperature_c === undefined ? "N/A" : Number(node.temperature_c).toFixed(1) + " °C";
  const sample = document.createElement("div");
  sample.innerHTML = "<small>AMOSTRA</small><strong></strong>";
  sample.querySelector("strong").textContent = node.sample_ms ? node.sample_ms + " ms" : "—";
  meta.append(temp, sample);

  const task = document.createElement("div");
  task.className = "task-box " + (node.task ? "" : "idle");
  const taskTitle = document.createElement("span");
  taskTitle.className = "task-title";
  taskTitle.textContent = node.task ? node.task.label : "Nenhum trabalho atribuído";
  const taskTime = document.createElement("span");
  taskTime.className = "task-time";
  taskTime.textContent = node.task ? fmtDuration(node.task.elapsed_seconds) : "IDLE";
  task.append(taskTitle, taskTime);

  article.append(head, overall, blocks, cores, meta, task);
  return article;
}

function renderNodes(nodes) {
  const grid = $("#node-grid");
  grid.replaceChildren();
  (nodes || []).forEach((node) => grid.append(nodeCard(node)));
}

function renderDispatch(payload) {
  const dispatch = payload.dispatch;
  const title = $("#dispatch-title");
  const meta = $("#dispatch-meta");

  ["ci01", "ci02", "ci03"].forEach((id) => {
    const el = $("#flow-" + id);
    el.className = "flow-node";
    const node = (payload.nodes || []).find((item) => item.id === id);
    if (node && node.task) el.classList.add("running");
    else if (node && !node.reachable) el.classList.add("fail");
  });

  if (!dispatch) {
    title.textContent = "Nenhum trabalho distribuído em execução";
    meta.textContent = "AGT aguardando demanda";
    return;
  }

  if (dispatch.status === "RUNNING") {
    title.textContent = String(dispatch.suite || "job").toUpperCase() + " · " + (dispatch.sha_short || "sem SHA");
    meta.textContent = "Em execução · " + fmtDuration(dispatch.elapsed_seconds);
  } else {
    title.textContent = "Último despacho: " + String(dispatch.status || "—");
    meta.textContent = (dispatch.sha_short || "sem SHA") + " · " + fmtDuration(dispatch.elapsed_seconds);
  }
}

function renderDispatchHistory(recent) {
  const root = $("#dispatch-history");
  root.replaceChildren();
  if (!recent || !recent.length) {
    root.innerHTML = '<div class="empty">Nenhum despacho registrado.</div>';
    return;
  }
  recent.forEach((item) => {
    const row = document.createElement("div");
    row.className = "history-row";
    const statusClass = item.status === "PASS" ? "good" : (item.status === "FAIL" ? "bad" : "warn");
    const workers = Object.entries(item.workers || {}).map(([name, info]) => {
      const result = info && info.status ? info.status : "—";
      return name.toUpperCase() + ":" + result;
    }).join(" · ");
    const cells = [
      fmtTime(item.started_at),
      item.sha_short || "—",
      item.suite || "—",
      fmtDuration(item.wall_seconds),
      workers || (item.status || "—"),
    ];
    cells.forEach((value, index) => {
      const span = document.createElement("span");
      span.textContent = value;
      if (index === 1 || index === 3) span.classList.add("mono");
      if (index === 4) span.classList.add(statusClass);
      row.append(span);
    });
    root.append(row);
  });
}

function renderChat(chat) {
  chat = chat || {};
  const pluginState = $("#plugin-state");
  pluginState.textContent = chat.state || "UNKNOWN";
  pluginState.className = "plugin-state " + String(chat.state || "offline").toLowerCase();

  const last = chat.last_call;
  $("#chat-last-tool").textContent = last ? last.tool : "Nenhuma chamada";
  $("#chat-last-summary").textContent = last ? last.summary : "Sem histórico disponível.";
  $("#chat-last-age").textContent = fmtAge(chat.last_activity_age_seconds);
  $("#chat-last-duration").textContent = last && last.duration_ms !== null && last.duration_ms !== undefined ? last.duration_ms + " ms" : "—";
  $("#chat-last-result").textContent = last ? last.status : "—";
  $("#chat-last-result").className = last && last.status === "ERROR" ? "bad" : "good";

  const sessions = Array.isArray(chat.active_sessions) ? chat.active_sessions : [];
  $("#session-count").textContent = sessions.length + (sessions.length === 1 ? " sessão ativa" : " sessões ativas");
  const sessionRoot = $("#active-sessions");
  sessionRoot.replaceChildren();
  if (!sessions.length) {
    sessionRoot.innerHTML = '<div class="empty">Nenhum processo de console filho ativo neste instante.</div>';
  } else {
    sessions.forEach((session) => {
      const item = document.createElement("div");
      item.className = "session-item";
      const top = document.createElement("strong");
      top.textContent = "PID " + session.pid + " · " + fmtDuration(session.elapsed_seconds);
      const summary = document.createElement("span");
      summary.textContent = session.summary;
      item.append(top, summary);
      sessionRoot.append(item);
    });
  }

  const history = $("#tool-history");
  history.replaceChildren();
  const calls = Array.isArray(chat.recent_calls) ? chat.recent_calls : [];
  if (!calls.length) {
    history.innerHTML = '<div class="empty">Histórico do plugin indisponível.</div>';
    return;
  }
  calls.forEach((call) => {
    const row = document.createElement("div");
    row.className = "tool-row";
    const values = [
      fmtTime(call.timestamp),
      call.tool || "—",
      call.summary || "—",
      call.duration_ms === null || call.duration_ms === undefined ? "—" : call.duration_ms + " ms",
      call.status || "—",
    ];
    values.forEach((value, index) => {
      const span = document.createElement("span");
      span.textContent = value;
      if (index === 0 || index === 3) span.classList.add("mono");
      if (index === 4) span.classList.add(call.status === "ERROR" ? "bad" : "good");
      row.append(span);
    });
    history.append(row);
  });
}

function renderTransport(obs) {
  obs = obs || {};
  const browser = obs.browser || {};
  const transport = obs.transport || {};
  const observer = obs.observer || {};
  const api = obs.api || {};
  const derived = obs.derived || {};
  const severity = String(derived.severity || "UNKNOWN");

  const overall = $("#transport-overall-state");
  if (obs.enabled === false) {
    overall.textContent = "DESABILITADO";
    overall.className = "source-state ready";
    [
      "#wa-page-state", "#wa-browser-unit", "#wa-browser-debug", "#wa-page-count",
      "#wa-auth-state", "#wa-has-synced", "#wa-sync-handler", "#wa-client-state",
      "#wa-transport-unit", "#wa-transport-ready", "#wa-wwebjs-connected",
      "#wa-transport-pid", "#wa-authority-state", "#wa-authority-verification",
      "#wa-authority-reason", "#wa-authority-invalidation", "#wa-authority-invalidation-at",
      "#wa-flow-state", "#wa-observer-state", "#wa-observer-age", "#wa-inbound-count",
      "#wa-api-ready", "#wa-recovery-gate", "#wa-recovery-reason"
    ].forEach((selector) => {
      const element = $(selector);
      if (element) element.textContent = "—";
    });
    $("#wa-divergence-title").textContent = "Observabilidade de transport desabilitada";
    const divergenceRoot = $("#wa-divergence-list");
    divergenceRoot.replaceChildren();
    divergenceRoot.innerHTML = '<div class="empty">Ative por configuração privada do host.</div>';
    const timeline = $("#wa-timeline");
    timeline.replaceChildren();
    timeline.innerHTML = '<div class="empty">Transport observability desabilitado.</div>';
    return;
  }
  overall.textContent = severity === "OK" ? "COERENTE" : severity;
  overall.className = "source-state " + (
    severity === "OK" ? "live" : severity === "CRITICAL" ? "offline" : "ready"
  );

  $("#wa-page-state").textContent = derived.page_state || "UNKNOWN";
  $("#wa-browser-unit").textContent = browser.active ? "ACTIVE" : String(browser.active_state || "UNKNOWN").toUpperCase();
  $("#wa-browser-debug").textContent = browser.debug_reachable ? "REACHABLE" : "UNREACHABLE";
  $("#wa-page-count").textContent = browser.whatsapp_page_count ?? "—";
  $("#wa-auth-state").textContent = browser.auth_state || "—";
  $("#wa-has-synced").textContent = browser.has_synced === true ? "TRUE" : browser.has_synced === false ? "FALSE" : "—";
  $("#wa-sync-handler").textContent = browser.sync_handler_present === true ? "PRESENT" : browser.sync_handler_present === false ? "ABSENT" : "—";

  $("#wa-client-state").textContent = transport.client_state || "UNKNOWN";
  $("#wa-transport-unit").textContent = transport.active ? "ACTIVE" : String(transport.active_state || "UNKNOWN").toUpperCase();
  $("#wa-transport-ready").textContent = transport.ready === true ? "READY" : "NOT READY";
  $("#wa-wwebjs-connected").textContent = transport.wwebjs_connected === true ? "CONNECTED" : "NOT CONNECTED";
  $("#wa-transport-pid").textContent = transport.pid || "—";

  const authorityReady = transport.owner_command_authority_ready === true;
  $("#wa-authority-state").textContent = authorityReady ? "AUTHORIZED" : "NOT AUTHORIZED";
  $("#wa-authority-verification").textContent = transport.owner_identity_verification || "—";
  $("#wa-authority-reason").textContent = transport.owner_command_authority_reason || "—";
  $("#wa-authority-invalidation").textContent = transport.owner_authority_last_invalidation_reason || "—";
  $("#wa-authority-invalidation-at").textContent = fmtTime(transport.owner_authority_last_invalidation_at);

  const flowReady = transport.ready === true && api.ready === true;
  $("#wa-flow-state").textContent = flowReady ? "FLOW READY" : "FLOW DEGRADED";
  $("#wa-observer-state").textContent = observer.active ? (observer.fresh ? "ACTIVE / FRESH" : "ACTIVE") : "OFFLINE";
  $("#wa-observer-age").textContent = fmtAge(observer.age_seconds);
  $("#wa-inbound-count").textContent = transport.inbound_seen_count ?? "—";
  $("#wa-api-ready").textContent = api.ready ? "READY" : "NOT READY";

  const divergences = Array.isArray(derived.divergences) ? derived.divergences : [];
  $("#wa-divergence-title").textContent = divergences.length
    ? divergences.length + (divergences.length === 1 ? " divergência detectada" : " divergências detectadas")
    : "Nenhuma divergência detectada";
  const divergenceRoot = $("#wa-divergence-list");
  divergenceRoot.replaceChildren();
  if (!divergences.length) {
    const item = document.createElement("div");
    item.className = "divergence-item good";
    item.textContent = "As fontes independentes observadas estão coerentes.";
    divergenceRoot.append(item);
  } else {
    divergences.forEach((entry) => {
      const item = document.createElement("div");
      item.className = "divergence-item " + (entry.severity === "CRITICAL" ? "bad" : "warn");
      const code = document.createElement("strong");
      code.textContent = entry.code || "DIVERGENCE";
      const detail = document.createElement("span");
      detail.textContent = entry.detail || "—";
      item.append(code, detail);
      divergenceRoot.append(item);
    });
  }

  const gate = derived.recovery_gate || {};
  const gateEl = $("#wa-recovery-gate");
  gateEl.textContent = gate.state || "UNKNOWN";
  gateEl.className = "source-state " + (
    gate.state === "NOT_NEEDED" || gate.state === "ELIGIBLE" ? "live"
      : gate.state === "BLOCKED" ? "offline" : "ready"
  );
  $("#wa-recovery-reason").textContent = gate.reason || "—";

  const timeline = $("#wa-timeline");
  timeline.replaceChildren();
  const events = Array.isArray(obs.timeline) ? obs.timeline : [];
  if (!events.length) {
    timeline.innerHTML = '<div class="empty">Sem eventos relevantes no journal.</div>';
  } else {
    events.forEach((event) => {
      const row = document.createElement("div");
      row.className = "timeline-row";
      const time = document.createElement("span");
      time.className = "mono";
      time.textContent = fmtTime(event.timestamp);
      const message = document.createElement("span");
      message.textContent = event.message || "—";
      row.append(time, message);
      timeline.append(row);
    });
  }
}

function fmtMs(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  const ms = Math.max(0, Number(value));
  if (ms < 1000) return Math.round(ms) + "ms";
  if (ms < 60000) return (ms / 1000).toFixed(ms < 10000 ? 2 : 1) + "s";
  return fmtDuration(ms / 1000);
}

function traceTone(value) {
  const v = String(value || "UNKNOWN").toUpperCase();
  if (["COMPLETED", "DONE", "IGNORED"].includes(v)) return "good";
  if (["FAILED", "BLOCKED"].includes(v)) return "bad";
  if (["ACTIVE", "WAITING", "WAITING_GRACE", "WAITING_WORKER", "DECIDED", "INGESTED", "RECEIVED"].includes(v)) return "warn";
  return "muted";
}

function renderMessageTraces(tracePayload) {
  const latestRoot = $("#trace-latest");
  const listRoot = $("#trace-list");
  const badge = $("#trace-state");
  if (!latestRoot || !listRoot || !badge) return;

  latestRoot.replaceChildren();
  listRoot.replaceChildren();
  if (tracePayload.enabled === false) {
    badge.textContent = "DESABILITADO";
    badge.className = "source-state ready";
    $("#trace-title").textContent = "Message tracing desabilitado";
    $("#trace-updated").textContent = "—";
    latestRoot.innerHTML = '<div class="empty">Ative por configuração privada do host.</div>';
    return;
  }
  const traces = Array.isArray(tracePayload.traces) ? tracePayload.traces : [];
  $("#trace-updated").textContent = tracePayload.generated_at ? "Atualizado " + fmtTime(tracePayload.generated_at) : "—";

  if (tracePayload.ok === false) {
    badge.textContent = "TRACE ERROR";
    badge.className = "source-state offline";
    $("#trace-title").textContent = "Tracer indisponível";
    latestRoot.innerHTML = '<div class="empty">Falha ao consultar o tracer read-only.</div>';
    return;
  }
  if (!traces.length) {
    badge.textContent = "SEM FLUXOS";
    badge.className = "source-state ready";
    $("#trace-title").textContent = "Nenhuma mensagem recente";
    latestRoot.innerHTML = '<div class="empty">Nenhum inbound disponível para correlação.</div>';
    return;
  }

  const latest = traces[0];
  const tone = traceTone(latest.overall_state);
  badge.textContent = latest.overall_state || "UNKNOWN";
  badge.className = "source-state " + (tone === "good" ? "live" : tone === "bad" ? "offline" : "ready");
  $("#trace-title").textContent = "Trace " + (latest.correlation_short || "—") + " · " + (latest.overall_state || "UNKNOWN");

  const meta = document.createElement("div");
  meta.className = "trace-meta";
  const total = latest.duration_ms ?? latest.age_ms;
  const corr = document.createElement("span");
  corr.className = "mono";
  corr.textContent = latest.correlation_short || "—";
  const when = document.createElement("span");
  when.textContent = fmtTime(latest.received_at);
  const totalEl = document.createElement("span");
  totalEl.textContent = (latest.duration_ms !== null && latest.duration_ms !== undefined ? "TOTAL " : "ABERTO ") + fmtMs(total);
  meta.append(corr, when, totalEl);
  latestRoot.append(meta);

  const rail = document.createElement("div");
  rail.className = "trace-rail";
  (latest.stages || []).forEach((stage) => {
    const item = document.createElement("div");
    item.className = "trace-stage " + String(stage.state || "unknown").toLowerCase();
    const dot = document.createElement("span");
    dot.className = "trace-dot";
    const body = document.createElement("div");
    body.className = "trace-stage-body";
    const head = document.createElement("div");
    head.className = "trace-stage-head";
    const label = document.createElement("strong");
    label.textContent = stage.label || stage.key || "—";
    const timing = document.createElement("span");
    timing.className = "mono";
    timing.textContent = stage.elapsed_ms === null || stage.elapsed_ms === undefined ? "—" : "+" + fmtMs(stage.elapsed_ms);
    head.append(label, timing);
    const detail = document.createElement("small");
    detail.textContent = (stage.state || "UNKNOWN") + (stage.detail ? " · " + stage.detail : "");
    body.append(head, detail);
    item.append(dot, body);
    rail.append(item);
  });
  latestRoot.append(rail);

  traces.slice(1).forEach((trace) => {
    const row = document.createElement("div");
    row.className = "trace-row";
    const whenEl = document.createElement("span");
    whenEl.className = "mono";
    whenEl.textContent = fmtTime(trace.received_at);
    const corrEl = document.createElement("span");
    corrEl.className = "mono";
    corrEl.textContent = trace.correlation_short || "—";
    const current = document.createElement("span");
    current.textContent = trace.current_stage || "—";
    const status = document.createElement("strong");
    status.className = traceTone(trace.overall_state);
    status.textContent = trace.overall_state || "UNKNOWN";
    const elapsed = document.createElement("span");
    elapsed.className = "mono";
    elapsed.textContent = fmtMs(trace.duration_ms ?? trace.age_ms);
    row.append(whenEl, corrEl, current, status, elapsed);
    listRoot.append(row);
  });
}

function render(payload) {
  state.lastPayload = payload;
  renderNodes(payload.nodes || []);
  renderDispatch(payload);
  renderDispatchHistory(payload.recent || []);
  renderChat(payload.chat || {});
  renderTransport(payload.transport_observability || {});
  renderMessageTraces(payload.message_traces || {});

  $("#live-label").textContent = "AO VIVO";
  $("#last-update").textContent = "Atualizado " + fmtTime(payload.generated_at);
  $(".pulse").classList.remove("offline");
  $("#error-panel").hidden = true;
}

function configureObservability(config) {
  state.config = config || {};
  Object.entries(observabilitySources).forEach(([name, source]) => {
    const url = state.config[source.key];
    const status = $(source.status);
    const link = $(source.open);

    if (!url) {
      status.textContent = "NÃO CONFIGURADO";
      status.className = "source-state offline";
      link.hidden = true;
      return;
    }

    link.href = url;
    link.hidden = false;
    status.textContent = "PRONTO";
    status.className = "source-state ready";

    const frame = $(source.frame);
    if (!frame.dataset.loadBound) {
      frame.addEventListener("load", () => {
        status.textContent = "AO VIVO";
        status.className = "source-state live";
      });
      frame.dataset.loadBound = "1";
    }

    if (state.tab === name) ensureObservabilityLoaded(name);
  });
}

function ensureObservabilityLoaded(name) {
  const source = observabilitySources[name];
  if (!source || !state.config) return;

  const url = state.config[source.key];
  if (!url) return;

  const frame = $(source.frame);
  if (!frame.dataset.loaded) {
    $(source.status).textContent = "CARREGANDO";
    $(source.status).className = "source-state loading";
    frame.src = url;
    frame.dataset.loaded = "1";
  }
}

async function loadConfig() {
  try {
    const response = await fetch("/api/config?ts=" + Date.now(), { cache: "no-store" });
    if (!response.ok) throw new Error("HTTP " + response.status);
    const payload = await response.json();
    configureObservability(payload.observability || {});
  } catch (_error) {
    configureObservability({});
  }
}

function setTab(tab) {
  state.tab = tab;
  document.querySelectorAll(".tab").forEach((candidate) => {
    candidate.classList.toggle("is-active", candidate.dataset.tab === tab);
  });
  document.querySelectorAll(".view").forEach((view) => {
    view.classList.toggle("is-active", view.id === tab + "-view");
  });
  ensureObservabilityLoaded(tab);
}

async function refresh() {
  try {
    const response = await fetch("/api/status?ts=" + Date.now(), { cache: "no-store" });
    if (!response.ok) throw new Error("HTTP " + response.status);
    render(await response.json());
  } catch (error) {
    $("#live-label").textContent = "SEM TELEMETRIA";
    $(".pulse").classList.add("offline");
    const panel = $("#error-panel");
    panel.hidden = false;
    panel.textContent = "Falha ao atualizar painel: " + error.message;
  }
}

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => setTab(button.dataset.tab));
});

loadConfig();
refresh();
state.timer = window.setInterval(refresh, 1500);
