"use strict";

const state = {
  tab: "compute",
  lastPayload: null,
  timer: null,
  config: null,
};

const observabilitySources = {
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

function render(payload) {
  state.lastPayload = payload;
  renderNodes(payload.nodes || []);
  renderDispatch(payload);
  renderDispatchHistory(payload.recent || []);
  renderChat(payload.chat || {});

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
