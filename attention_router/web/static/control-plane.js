(() => {
  "use strict";

  const state = {
    token: "",
    connected: false,
    snapshot: null,
    scenarioEngine: null,
    policies: [],
    capabilities: [],
    approvals: [],
    intents: [],
    scenarios: [],
    probeReports: {},
  };

  const titles = {
    overview: "Visão geral",
    scenarios: "Cenários",
    approvals: "Autorizações",
    capabilities: "Capacidades",
    rules: "Authority",
    grants: "Grants",
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

  async function loadProbeReports() {
    const entries = await Promise.all(
      state.scenarios.map(async (scenario) => {
        const base = "/api/v1/admin/platform/operations/capability-lab/probe/";
        const path = `${base}${encodeURIComponent(scenario.scenario_id)}`;
        try {
          return [scenario.scenario_id, await fetchJson(path)];
        } catch (error) {
          return [
            scenario.scenario_id,
            { error: error.message || "T0 probe unavailable" },
          ];
        }
      }),
    );
    state.probeReports = Object.fromEntries(entries);
  }

  async function loadRuntime() {
    if (!state.scenarios.length) await loadScenarios();

    const calls = [
      fetchJson("/api/v1/admin/platform/operations/snapshot"),
      fetchJson("/api/v1/admin/platform/operations/capability-lab/scenario-engine"),
      fetchJson("/api/v1/admin/policies"),
      fetchJson("/api/v1/admin/platform/matrix"),
      fetchJson("/api/v1/private/response-reviews?review_status=PENDING"),
      fetchJson("/api/v1/private/execution-intents?intent_status=PENDING"),
    ];

    const [
      snapshot,
      scenarioEngine,
      policies,
      capabilities,
      approvalResponse,
      intentResponse,
    ] = await Promise.all(calls);

    state.snapshot = snapshot;
    state.scenarioEngine = scenarioEngine;
    state.policies = Array.isArray(policies) ? policies : [];
    state.capabilities = Array.isArray(capabilities) ? capabilities : [];
    state.approvals = Array.isArray(approvalResponse?.items) ? approvalResponse.items : [];
    state.intents = Array.isArray(intentResponse?.items) ? intentResponse.items : [];
    await loadProbeReports();
    state.connected = true;

    renderAll();
  }

  function renderAll() {
    renderConnection();
    renderMetrics();
    renderRuntime();
    renderScenarios();
    renderScenarioEngine();
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

  function capabilityTitle(item) {
    return firstDefined(
      item.capability,
      item.capability_key,
      item.canonical_key,
      item.identity,
      item.name,
      item.key,
      "capability",
    );
  }

  function canonicalCapability(capabilityKey) {
    return state.capabilities.find((item) => capabilityTitle(item) === capabilityKey) || null;
  }

  function registryState(capabilityKey) {
    if (!state.connected) {
      return { label: "NOT OBSERVED", detail: "conecte o runtime para consultar o registry" };
    }

    const capability = canonicalCapability(capabilityKey);
    if (!capability) {
      return { label: "UNREGISTERED", detail: "não existe no registry canônico observado" };
    }

    const status = firstDefined(capability.state, capability.status, "REGISTERED");
    const provider = firstDefined(
      capability.bound_provider,
      capability.required_provider,
      "internal / none",
    );
    return {
      label: status,
      detail: `provider: ${provider}`,
    };
  }

  function engineScenario(scenarioKey) {
    const scenarios = Array.isArray(state.scenarioEngine?.scenarios)
      ? state.scenarioEngine.scenarios
      : [];
    return scenarios.find((item) => item.scenario_key === scenarioKey) || null;
  }

  function probeView(scenarioId) {
    if (!state.connected) {
      return {
        label: "NOT PROBED",
        detail: "conecte o runtime para executar o probe T0 read-only",
      };
    }

    const report = state.probeReports[scenarioId];
    if (!report) return { label: "NOT PROBED", detail: "probe sem resultado" };
    if (report.error) return { label: "PROBE ERROR", detail: report.error };

    const probe = report.probe || {};
    const comparison = report.comparison || {};
    const status = firstDefined(comparison.status, "INCOMPLETE");
    const observed = firstDefined(probe.authority_result, "UNKNOWN");
    const reason = firstDefined(probe.reason_code, "reason unavailable");
    const resolution = firstDefined(probe.resolution_status, "UNKNOWN");
    const durable = probe.durable_evidence === true ? "YES" : "NO";
    return {
      label: `T0 ${status}`,
      detail: `observado: ${observed} / ${reason} · runtime: ${resolution} · evidência durável: ${durable} · ${report.certification || "EPHEMERAL_ONLY"}`,
    };
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
        const registry = registryState(item.capability_key);
        const probe = probeView(item.scenario_id);
        const binding = item.engine_scenario_key || null;
        const engine = binding ? engineScenario(binding) : null;
        let bindingState = "UNBOUND";
        if (binding && !state.connected) bindingState = "BOUND / NOT OBSERVED";
        if (binding && state.connected && !engine) bindingState = "BOUND / MISSING";
        if (binding && engine && !engine.latest_run) bindingState = "BOUND / INCOMPLETE";
        if (binding && engine?.latest_run) bindingState = `BOUND / ${engine.latest_run.status}`;

        return `
          <article class="record">
            <div class="record-main">
              <span class="record-kicker">feature hypothesis / ${escapeHtml(item.scenario_id)}</span>
              <h3>${escapeHtml(item.title)}</h3>
              <p>${escapeHtml(item.request_text)}</p>
              <small>
                T0 esperado: ${escapeHtml(item.expected_resolution)} ·
                T1 simulado: ${escapeHtml(decision)} · T2 esperado: ${escapeHtml(grant)}
              </small>
              <small>
                capability: ${escapeHtml(item.capability_key)} · registry:
                ${escapeHtml(registry.label)} · ${escapeHtml(registry.detail)}
              </small>
              <small>
                probe T0: ${escapeHtml(probe.detail)}
              </small>
              <small>
                engine binding: ${escapeHtml(binding || "none")} · ${escapeHtml(bindingState)} ·
                sem binding/evidência durável, o resultado não é certificação
              </small>
            </div>
            <div class="record-state">${escapeHtml(probe.label)}</div>
          </article>
        `;
      })
      .join("");
  }

  function renderScenarioEngine() {
    const metrics = $("#scenario-engine-metrics");
    const container = $("#scenario-engine-list");
    if (!metrics || !container) return;

    if (!state.connected || !state.scenarioEngine) {
      metrics.innerHTML = `
        <div><small>Autoridade</small><strong>OBSERVATION ONLY</strong></div>
        <div><small>Cenários registrados</small><strong>—</strong></div>
        <div><small>Runs recentes</small><strong>—</strong></div>
        <div><small>Mutação</small><strong>NONE</strong></div>
      `;
      container.innerHTML =
        '<div class="empty-state">Conecte-se para observar o Scenario Engine persistido.</div>';
      return;
    }

    metrics.innerHTML = `
      <div><small>Autoridade</small><strong>${escapeHtml(state.scenarioEngine.authority)}</strong></div>
      <div><small>Cenários registrados</small><strong>${escapeHtml(state.scenarioEngine.registered_scenario_count)}</strong></div>
      <div><small>Runs recentes</small><strong>${escapeHtml(state.scenarioEngine.recent_run_count)}</strong></div>
      <div><small>Read-only</small><strong>${state.scenarioEngine.read_only === true ? "YES" : "NO"}</strong></div>
    `;

    const runs = Array.isArray(state.scenarioEngine.recent_runs)
      ? state.scenarioEngine.recent_runs
      : [];
    if (!runs.length) {
      container.innerHTML =
        '<div class="empty-state">Nenhum ScenarioRun persistido foi observado neste tenant.</div>';
      return;
    }

    container.innerHTML = runs
      .map((run) => {
        const evidenceCount = Array.isArray(run.evidence_refs) ? run.evidence_refs.length : 0;
        const stepTotal = firstDefined(run.step_summary?.total, 0);
        return `
          <article class="record">
            <div class="record-main">
              <span class="record-kicker">scenario engine / ${escapeHtml(run.run_id)}</span>
              <h3>${escapeHtml(run.scenario_key || "scenario não resolvido")}</h3>
              <small>
                versão: ${escapeHtml(run.scenario_version)} · steps: ${escapeHtml(stepTotal)} ·
                evidence refs: ${escapeHtml(evidenceCount)} · cleanup: ${escapeHtml(run.cleanup_state)}
              </small>
            </div>
            <div class="record-state">${escapeHtml(run.status)}</div>
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
              <small>Response Review ainda não é HumanExecutionAuthorization.</small>
            </div>
            <div class="record-state">${escapeHtml(status)}</div>
          </article>
        `;
      })
      .join("");
  }

  function renderCapabilities() {
    const container = $("#capability-list");
    if (!state.connected) {
      container.innerHTML =
        '<div class="empty-state">Conecte-se para carregar o registry de capabilities.</div>';
      return;
    }
    if (!state.capabilities.length) {
      container.innerHTML =
        '<div class="empty-state">Nenhuma capability canônica foi observada.</div>';
      return;
    }

    container.innerHTML = state.capabilities
      .map((item) => {
        const title = capabilityTitle(item);
        const status = firstDefined(item.state, item.status, "OBSERVED");
        const provider = firstDefined(item.bound_provider, item.required_provider, "internal / none");
        const sensitivity = firstDefined(item.sensitivity, "unknown");
        const effect = item.side_effect === true ? "side effect" : "no side effect";
        return `
          <article class="record">
            <div class="record-main">
              <span class="record-kicker">canonical runtime capability</span>
              <h3>${escapeHtml(title)}</h3>
              <p>provider: ${escapeHtml(provider)}</p>
              <small>
                sensitivity: ${escapeHtml(sensitivity)} · ${escapeHtml(effect)} ·
                este é o registry existente que o Lab deve exercitar, não duplicar
              </small>
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
      state.scenarioEngine = null;
      state.probeReports = {};
      renderConnection();
      renderScenarios();
      renderScenarioEngine();
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
  renderScenarioEngine();
  loadScenarios().catch((error) => {
    toast(error.message || "Falha ao carregar cenários sintéticos.", "error");
  });
})();
