(() => {
  "use strict";

  const state = {
    token: "",
    connected: false,
    snapshot: null,
    policies: [],
    capabilities: [],
    approvals: [],
    intents: [],
    scenarios: [],
  };

  const titles = {
    overview: "Visão geral",
    scenarios: "Cenários",
    approvals: "Autorizações",
    capabilities: "Capacidades",
    rules: "Regras de autorização",
    grants: "Permissões",
  };

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => [...document.querySelectorAll(selector)];

  const escapeHtml = (value) =>
    String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");

  const firstDefined = (...values) =>
    values.find((value) => value !== undefined && value !== null);

  function toast(message, kind = "info") {
    const element = $("#toast");
    element.textContent = message;
    element.classList.toggle("error", kind === "error");
    element.classList.add("visible");
    window.clearTimeout(toast.timer);
    toast.timer = window.setTimeout(() => element.classList.remove("visible"), 3200);
  }

  function authHeaders() {
    return state.token ? { Authorization: `Bearer ${state.token}` } : {};
  }

  async function fetchJson(path, { authenticated = true } = {}) {
    const headers = { Accept: "application/json" };
    if (authenticated) Object.assign(headers, authHeaders());

    const response = await fetch(path, {
      headers,
      credentials: "same-origin",
    });

    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`;
      try {
        const body = await response.json();
        detail = body.detail || detail;
      } catch {
        // Keep HTTP fallback.
      }
      throw new Error(`${path}: ${detail}`);
    }

    return response.json();
  }

  async function loadScenarios() {
    const payload = await fetchJson("/static/capability-lab-scenarios.json", {
      authenticated: false,
    });
    state.scenarios = Array.isArray(payload) ? payload : [];
    renderScenarios();
    renderMetrics();
  }

  async function loadRuntime() {
    const calls = [
      fetchJson("/api/v1/admin/platform/operations/snapshot"),
      fetchJson("/api/v1/admin/policies"),
      fetchJson("/api/v1/admin/platform/matrix"),
      fetchJson("/api/v1/private/response-reviews?review_status=PENDING"),
      fetchJson("/api/v1/private/execution-intents?intent_status=PENDING"),
    ];

    const [snapshot, policies, capabilities, approvalResponse, intentResponse] =
      await Promise.all(calls);

    state.snapshot = snapshot;
    state.policies = Array.isArray(policies) ? policies : [];
    state.capabilities = Array.isArray(capabilities) ? capabilities : [];
    state.approvals = Array.isArray(approvalResponse?.items) ? approvalResponse.items : [];
    state.intents = Array.isArray(intentResponse?.items) ? intentResponse.items : [];
    state.connected = true;

    renderAll();
  }

  function renderAll() {
    renderConnection();
    renderMetrics();
    renderRuntime();
    renderScenarios();
    renderApprovals();
    renderCapabilities();
  }

  function renderConnection() {
    const element = $("#connection-state");
    if (state.connected) {
      element.textContent = "Conectado. A credencial existe somente na memória desta página.";
      element.classList.add("connected");
      $("#connect-button").textContent = "Atualizar";
    } else {
      element.textContent = "Somente memória da página; nada vai para localStorage.";
      element.classList.remove("connected");
      $("#connect-button").textContent = "Conectar";
    }
  }

  function renderMetrics() {
    $("#metric-scenarios").textContent = String(state.scenarios.length);
    $("#metric-approvals").textContent = state.connected ? String(state.approvals.length) : "—";
    $("#metric-policies").textContent = state.connected
      ? String(state.policies.filter((item) => firstDefined(item.is_active, true) !== false).length)
      : "—";
    $("#metric-capabilities").textContent = state.connected
      ? String(state.capabilities.length)
      : "—";
    $("#metric-intents").textContent = state.connected ? String(state.intents.length) : "—";
    $("#nav-scenario-count").textContent = String(state.scenarios.length);
    $("#nav-approval-count").textContent = state.connected ? String(state.approvals.length) : "—";
  }

  function renderRuntime() {
    const pill = $("#runtime-state");
    const provenance = $("#runtime-provenance");
    if (!state.connected) return;

    const readiness = Array.isArray(state.snapshot?.readiness) ? state.snapshot.readiness : [];
    let value = "UNKNOWN";

    if (state.snapshot?.stale) {
      value = "BLOCKED";
    } else if (readiness.length && readiness.every((item) => item.state === "READY")) {
      value = "READY";
    } else if (
      readiness.some((item) => ["BLOCKED", "NOT_READY", "STALE"].includes(item.state))
    ) {
      value = "BLOCKED";
    } else if (readiness.length) {
      value = "DEGRADED";
    }

    pill.textContent = value;
    pill.className = `state-pill ${value.toLowerCase()}`;

    const runtimeRevision =
      state.snapshot?.runtime_provenance?.runtime_revision ||
      state.snapshot?.runtime_provenance?.source_revision ||
      "unknown";
    provenance.textContent = `runtime ${runtimeRevision}`;
  }

  function renderScenarios() {
    const container = $("#scenario-list");
    if (!container) return;

    if (!state.scenarios.length) {
      container.innerHTML =
        '<div class="empty-state">Nenhum cenário sintético carregado.</div>';
      return;
    }

    container.innerHTML = state.scenarios
      .map((item) => {
        const decision = firstDefined(item.simulated_human_decision, "NONE");
        const grant = firstDefined(item.expected_grant_mode, "NONE");
        return `
          <article class="record">
            <div class="record-main">
              <span class="record-kicker">synthetic / ${escapeHtml(item.scenario_id)}</span>
              <h3>${escapeHtml(item.title)}</h3>
              <p>${escapeHtml(item.request_text)}</p>
              <small>
                capability: ${escapeHtml(item.capability_key)} · decisão simulada:
                ${escapeHtml(decision)} · grant esperado: ${escapeHtml(grant)}
              </small>
            </div>
            <div class="record-state">${escapeHtml(item.expected_resolution)}</div>
          </article>
        `;
      })
      .join("");
  }

  function approvalTitle(item) {
    return firstDefined(
      item.edited_response_text,
      item.proposed_response_text,
      item.effective_response_snapshot,
      item.reason,
      "Revisão humana pendente",
    );
  }

  function renderApprovals() {
    const container = $("#approval-list");
    if (!state.connected) {
      container.innerHTML =
        '<div class="empty-state">Conecte-se para carregar as revisões pendentes.</div>';
      return;
    }
    if (!state.approvals.length) {
      container.innerHTML =
        '<div class="empty-state">Nenhuma revisão humana pendente no fluxo atual.</div>';
      return;
    }

    container.innerHTML = state.approvals
      .map((item) => {
        const id = firstDefined(item.id, item.review_id, "unknown");
        const decisionId = firstDefined(
          item.agent_decision_id,
          item.interaction_id,
          "sem correlação",
        );
        const status = firstDefined(item.status, item.review_status, "PENDING");
        return `
          <article class="record">
            <div class="record-main">
              <span class="record-kicker">review / ${escapeHtml(id)}</span>
              <h3>${escapeHtml(approvalTitle(item))}</h3>
              <p>correlação: ${escapeHtml(decisionId)}</p>
              <small>Esta fila ainda representa Response Review, não AuthorizationRequest.</small>
            </div>
            <div class="record-state">${escapeHtml(status)}</div>
          </article>
        `;
      })
      .join("");
  }

  function capabilityTitle(item) {
    return firstDefined(
      item.capability_key,
      item.canonical_key,
      item.identity,
      item.name,
      item.key,
      "capability",
    );
  }

  function renderCapabilities() {
    const container = $("#capability-list");
    if (!state.connected) {
      container.innerHTML =
        '<div class="empty-state">Conecte-se para carregar a matrix atual.</div>';
      return;
    }
    if (!state.capabilities.length) {
      container.innerHTML =
        '<div class="empty-state">Nenhuma capability de plataforma foi anunciada.</div>';
      return;
    }

    container.innerHTML = state.capabilities
      .map((item) => {
        const title = capabilityTitle(item);
        const status = firstDefined(
          item.authorization_status,
          item.status,
          item.state,
          item.authorized === true ? "AUTHORIZED" : "OBSERVED",
        );
        const source = firstDefined(item.device_id, item.source, item.provider, "platform");
        return `
          <article class="record">
            <div class="record-main">
              <span class="record-kicker">platform capability</span>
              <h3>${escapeHtml(title)}</h3>
              <p>origem: ${escapeHtml(source)}</p>
              <small>Não confundir com GovernedCapability do Control Plane.</small>
            </div>
            <div class="record-state">${escapeHtml(status)}</div>
          </article>
        `;
      })
      .join("");
  }

  function activateView(name) {
    if (!titles[name]) return;

    $$(".nav-item").forEach((button) => {
      button.classList.toggle("active", button.dataset.view === name);
    });
    $$(".view").forEach((panel) => {
      panel.classList.toggle("active", panel.dataset.viewPanel === name);
    });
    $("#page-title").textContent = titles[name];
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  $$(".nav-item").forEach((button) => {
    button.addEventListener("click", () => activateView(button.dataset.view));
  });

  $("#connect-button").addEventListener("click", async () => {
    const input = $("#admin-token");
    state.token = input.value.trim();
    input.value = "";

    $("#connect-button").disabled = true;
    $("#connect-button").textContent = "Carregando…";

    try {
      await loadRuntime();
      toast("Capability Lab conectado ao runtime.");
    } catch (error) {
      state.connected = false;
      renderConnection();
      $("#runtime-state").textContent = "ERRO";
      $("#runtime-state").className = "state-pill blocked";
      $("#runtime-provenance").textContent = "falha ao consultar runtime";
      toast(error.message || "Falha ao conectar.", "error");
    } finally {
      $("#connect-button").disabled = false;
      if (!state.connected) $("#connect-button").textContent = "Conectar";
    }
  });

  renderConnection();
  renderMetrics();
  loadScenarios().catch((error) => {
    toast(error.message || "Falha ao carregar cenários sintéticos.", "error");
  });
})();
