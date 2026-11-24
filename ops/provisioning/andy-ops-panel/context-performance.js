"use strict";

(() => {
  const view = document.querySelector("#context-performance-view");
  if (!view) return;
  const find = (id) => view.querySelector("#context-" + id);
  const select = find("run");
  const baseline = find("baseline");
  const trace = find("trace-select");
  const empty = "Indisponível";
  const number = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 2 });
  let mode = "live";
  let loading = false;
  const lookup = (id) => view.querySelector('[data-context-mode="' + id + '"]');

  const element = (tag, value, css) => {
    const item = document.createElement(tag);
    if (value !== undefined) item.textContent = value;
    if (css) item.className = css;
    return item;
  };
  const fmt = (value) => value === null || value === undefined ? empty :
    typeof value === "number" ? number.format(value) : String(value);
  const choose = (control, rows, first) => {
    const current = control.value;
    const values = [ ...(first ? [first] : []), ...rows ];
    if (Array.from(control.options).map((option) => option.value).join() !== values.map((x) => x.id).join()) {
      control.replaceChildren(...values.map((x) => {
        const option = element("option", x.label);
        option.value = x.id;
        return option;
      }));
      if (values.some((x) => x.id === current)) control.value = current;
    }
  };

  function table(destination, title, columns, records, message = empty) {
    const card = element("section", undefined, "history-panel context-table");
    card.append(element("h2", title));
    if (!records?.length) {
      card.append(element("p", message, "muted"));
      destination.replaceChildren(card);
      return;
    }
    const grid = element("table");
    const head = grid.createTHead().insertRow();
    for (const [, label] of columns) {
      const cell = element("th", label);
      cell.scope = "col";
      head.append(cell);
    }
    const body = grid.createTBody();
    for (const record of records) {
      const row = body.insertRow();
      for (const [key, , format] of columns) {
        const value = record[key];
        const rendered = format && value != null ? format(value) : fmt(value);
        const cell = row.insertCell();
        if (rendered instanceof Node) cell.append(rendered);
        else cell.textContent = rendered;
      }
    }
    card.append(grid);
    destination.replaceChildren(card);
  }
  const duration = (nanos) => fmt(nanos / 1e6) + " ms";
  const time = (nanos) => new Date(nanos / 1e6).toLocaleTimeString("pt-BR");
  const percent = (value) => fmt(value) + "%";
  const yesNo = (value) => value ? "Sim" : "Não";

  function pickTrace(identifier) {
    const button = element("button", identifier.slice(0, 12), "tab");
    button.type = "button";
    button.title = identifier;
    button.addEventListener("click", () => {
      if (!Array.from(trace.options).some((option) => option.value === identifier)) {
        const option = new Option(identifier.slice(0, 12), identifier);
        trace.add(option);
      }
      trace.value = identifier;
      lookup("trace").click();
    });
    return button;
  }

  function renderPool(payload) {
    const summary = payload.pool_summary?.[0];
    table(find("pool-summary"), "Pool PostgreSQL · amostras e contagens observadas", [
      ["capacity", "Capacidade"], ["occupied", "Ocupadas"], ["free", "Livres"],
      ["waiting", "Aquisições abertas"], ["timeouts", "Timeouts de aquisição"],
      ["sampler_state", "Amostragem PostgreSQL"], ["lock_samples", "Amostras Lock"],
      ["pool_lock_timeouts", "Locks com timeout"],
    ], summary?.capacity != null ? [summary] : [], "Ciclo do pool indisponível nesta execução.");
    table(find("pool-open"), "Em aberto · fase inferida pelos eventos do trace", [
      ["state", "Estado observado"], ["age_ms", "Idade ms"], ["phase", "Fase inferida"],
      ["driver_active", "SQL ativo", yesNo], ["sql_count", "Operações SQL"],
      ["outside_cursor_ms", "Fora do cursor ms"], ["lock_samples", "Amostras Lock"],
      ["disconnected", "Cliente desconectado", yesNo], ["trace", "Trace", pickTrace],
    ], payload.pool_open, summary?.capacity != null ? "Nenhuma aquisição ou posse aberta na última amostra." : empty);
    table(find("pool-distribution"), "Distribuição do ciclo da conexão · fora do cursor inclui transações e espera não decomposta", [
      ["measure", "Medida"], ["samples", "Amostras"], ["p50_ms", "p50 ms"],
      ["p95_ms", "p95 ms"], ["p99_ms", "p99 ms"], ["max_ms", "Máximo ms"],
    ], payload.pool_distribution);
  }

  function renderWorker(payload) {
    const worker = payload.metadata?.find((row) => row.field === "worker")?.value;
    const node = state.lastPayload?.nodes?.find((host) => host.id.toLowerCase() === worker?.toLowerCase());
    const box = find("worker");
    box.replaceChildren();
    if (node) box.append(nodeCard(node));
    const note = element("small", node ?
      "COMPUTE reutilizado · última coleta " + fmtTime(state.lastPayload.generated_at) +
      " · memória do host indisponível nesta fonte" : "COMPUTE do worker indisponível", "muted");
    note.dataset.contextUnavailable = "true";
    box.append(note);
  }

  function renderLive(payload) {
    const summary = payload.summary?.[0] || {};
    const serverObserved = Boolean(payload.components?.length);
    const metrics = [
      ["Ofertas", summary.offered], ["Admitidas", serverObserved ? summary.admitted : null],
      ["Em andamento", serverObserved ? summary.in_progress : null],
      ["Aguardando executor", serverObserved ? summary.executor_waiting : null],
      ["Threads trabalhando", serverObserved ? summary.worker_active : null],
      ["Concluídas no cliente", summary.completed],
      ["Disponibilidade entre conclusões elegíveis (%)", summary.availability_pct],
      ["Exatidão entre HTTP 200 (%)", summary.grounding_pct],
      ["Respostas com evidência", summary.grounded],
      ["Abstenções legítimas", summary.abstained],
      ["Recusas por autoridade", serverObserved ? summary.legitimate_refusals : null],
      ["Timeout do cliente", summary.client_timeout], ["HTTP 503 vazio", summary.server_empty],
      ["Desconexão observada", serverObserved ? summary.disconnects : null],
      ["Canceladas", serverObserved ? summary.cancelled : null],
      ["Latência p50 (ms)", summary.p50_ms], ["Latência p95 (ms)", summary.p95_ms], ["Latência p99 (ms)", summary.p99_ms],
      ["Conclusões/min", summary.throughput_per_min],
    ];
    find("health").replaceChildren(...metrics.map(([title, value]) => {
      const card = element("article", undefined, "chat-card");
      card.append(element("span", title, "section-label"), element("strong", fmt(value)));
      return card;
    }));
    renderWorker(payload);
    table(find("components"), "Etapas • tempo inclusivo e espera observada", [
      ["stage", "Componente"], ["kind", "Medida"], ["samples", "Amostras"], ["p50_ms", "p50 ms"],
      ["p95_ms", "p95 ms"], ["p99_ms", "p99 ms"], ["driver_ms", "SQL agregado ms"],
    ], payload.components);
    const latest = new Map((payload.capacity || []).map((row) => [row.resource, row]));
    table(find("capacity"), "Ocupação observada", [
      ["resource", "Recurso"], ["used", "Ocupado na amostra"], ["limit", "Limite"], ["waiting", "Fila observada"],
    ], Array.from(latest.values()).map((row) => row.resource === "pool_capacity" ? {...row, waiting:null} : row));
    renderPool(payload);
    table(find("outcomes"), "Desfechos do servidor", [["outcome", "Motivo"], ["count", "Consultas"]], payload.outcomes);
  }

  function renderTrace(payload) {
    const allTraces = [...(payload.traces || []), ...(payload.fault_traces || []).map(
      (row) => ({...row, induced: true}),
    )];
    const traces = allTraces.map((row) => ({
      id: row.trace, label: (row.induced ? "Falha induzida · " : "") +
        row.trace.slice(0, 12) + " · " + fmt(row.server_ms) + " ms · " + row.result,
    }));
    const known = new Set(traces.map((row) => row.id));
    for (const row of payload.pool_open || []) {
      if (row.trace && !known.has(row.trace)) {
        known.add(row.trace);
        traces.push({id: row.trace, label: "Em andamento · " + row.trace.slice(0, 12)});
      }
    }
    choose(trace, traces, {id: "", label: "Escolha uma consulta"});
    table(find("critical"), "Caminho observado", [
      ["trace", "Trace ID"], ["result", "Resultado"], ["server_ms", "Servidor ms"],
      ["observed_wait_ms", "Espera observada ms"], ["unknown_or_processing_ms", "Processamento ou lacuna ms"],
    ], allTraces.filter((row) => row.trace === trace.value),
    traces.length ? "Escolha uma consulta para exibir o caminho." : "Spans do servidor indisponíveis para esta execução.");
    table(find("timeline"), "Decomposição temporal e logs correlacionados", [
      ["event", "Evento"], ["stage", "Etapa"], ["timestamp_ns", "Hora", time],
      ["duration_ns", "Duração inclusiva", duration],
      ["exclusive_observed_ns", "Exclusivo observado", duration], ["outcome", "Resultado"],
      ["correlation", "Correlation ID"],
      ["sql_count", "Operações SQL"],
    ], payload.timeline);
    table(find("pool-trace"), "Ciclo das conexões deste trace", [
      ["event", "Evento"], ["pool_lease", "Posse", (value) => value.slice(0, 12)],
      ["timestamp_ns", "Hora", time], ["duration_ns", "Duração observada", duration],
      ["pool_sql_count", "Operações SQL"], ["pool_begins", "Inícios"],
      ["pool_commits", "Commits"], ["pool_rollbacks", "Rollbacks"],
      ["pool_outside_cursor_ns", "Fora do cursor", duration],
      ["pool_after_disconnect_ns", "Retida após desconexão", duration], ["outcome", "Resultado"],
    ], payload.pool_trace);
    const link = find("tempo");
    link.hidden = true;
    const grafanaUrl = state.config?.context_tempo_url || state.config?.network_osi_url;
    if (trace.value && grafanaUrl) {
      try {
        const url = new URL("/explore", grafanaUrl);
        if (url.protocol !== "http:" && url.protocol !== "https:") return;
        url.searchParams.set("schemaVersion", "1");
        url.searchParams.set("panes", JSON.stringify({
          context: {datasource:"roc-tempo", queries:[{refId:"A", queryType:"traceql", query:trace.value}]},
        }));
        link.href = url.href;
        link.hidden = false;
      } catch {}
    }
  }

  function renderCompare(payload) {
    const compatibility = payload.comparability?.[0];
    const indicator = find("comparability");
    indicator.textContent = compatibility?.comparable ? "AMBIENTES EQUIVALENTES" :
      "COMPARABILIDADE LIMITADA" + (compatibility?.differences ? " · " + compatibility.differences : "");
    indicator.className = "source-state " + (compatibility?.comparable ? "ready" : "offline");
    table(find("summary-comparison"), "Comparação de disponibilidade e latência", [
      ["metric", "Indicador"], ["selected", "Selecionado"], ["baseline", "Baseline"],
      ["delta", "Δ absoluto"], ["delta_pct", "Δ relativo", percent],
    ], payload.comparison);
    const old = new Map((payload.baseline_distribution || []).map((row) => [row.upper_ms, row.cumulative]));
    table(find("distribution"), "Distribuição acumulada", [
      ["upper_ms", "Limite ms"], ["cumulative", "Selecionado"], ["baseline", "Baseline"],
    ], (payload.distribution || []).map((row) => ({...row, baseline: old.get(row.upper_ms)})));
    table(find("stage-comparison"), "Espera e processamento por componente", [
      ["stage", "Componente"], ["kind", "Medida"], ["selected_ms", "p95 selecionado ms"],
      ["baseline_ms", "p95 baseline ms"], ["delta_ms", "Δ p95 ms"],
    ], payload.component_comparison, "Comparação de estágios indisponível quando a baseline não tem spans.");
    const a = payload.resources?.at(-1);
    const b = payload.baseline_resources?.at(-1);
    const resources = ["cpu_seconds","rss_peak_kib","input_blocks","output_blocks"].map((metric) => ({
      metric, selected:a?.[metric], baseline:b?.[metric],
      delta:a && b ? a[metric]-b[metric] : null, delta_pct:a && b && b[metric] ? 100*(a[metric]-b[metric])/b[metric] : null,
    }));
    table(find("resource-comparison"), "Recursos do processo da bancada", [
      ["metric", "Indicador"], ["selected", "Selecionado"], ["baseline", "Baseline"],
      ["delta", "Δ absoluto"], ["delta_pct", "Δ relativo", percent],
    ], resources);
    table(find("identities"), "Identidade experimental", [["field", "Campo"], ["value", "Valor"]], payload.metadata);
  }

  async function request(endpoint) {
    const response = await fetch("/api/context-performance/" + endpoint, {cache:"no-store"});
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.state);
    return payload;
  }

  function status(label, appearance, detail) {
    find("state").textContent = label;
    find("state").className = "source-state " + appearance;
    find("freshness").textContent = detail;
  }

  async function refresh() {
    if (state.tab !== "context-performance" || loading) return;
    loading = true;
    try {
      const listed = await request("runs");
      choose(select, listed.runs, listed.runs.length ? null : {id:"",label:"Aguardando execução"});
      choose(baseline, listed.baselines);
      if (!select.value) {
        status("AGUARDANDO", "loading", "Aguardando primeira execução contextual");
        return;
      }
      const params = new URLSearchParams({run:select.value, baseline:baseline.value||"historical"});
      if (trace.value && mode === "trace") params.set("trace",trace.value);
      const payload = await request("view?" + params);
      if (params.get("run") !== select.value || state.tab !== "context-performance") return;
      const summary = payload.summary?.[0];
      const finished = payload.run_state === "complete";
      const hasEvent = Boolean(summary?.latest_event_ns);
      const stale = !finished && summary?.latest_event_age_ms > 30000;
      const live = hasEvent && !stale && !finished;
      status(finished ? "CONCLUÍDO" : live ? "AO VIVO" : stale ? "SEM ATUALIZAÇÃO" : "AGUARDANDO",
             finished ? "ready" : live ? "live" : stale ? "offline" : "loading",
             hasEvent ? "Último evento " + time(summary.latest_event_ns) +
             " · idade na origem " + fmt(summary.latest_event_age_ms) + " ms" : "Atualização ainda indisponível");
      ({live:renderLive,trace:renderTrace,compare:renderCompare})[mode](payload);
      view.dataset.run = params.get("run");
      view.dataset.offered = String(summary?.offered ?? "");
      view.dataset.completed = String(summary?.completed ?? "");
      view.dataset.latestEventNs = String(summary?.latest_event_ns ?? "");
    } catch (error) {
      status("INDISPONÍVEL","offline", error.message === "NOT_CONFIGURED" ?
             "Fonte contextual ainda não configurada neste ambiente" : "Falha ao atualizar a fonte contextual");
    } finally {
      loading = false;
    }
  }

  for (const item of ["live","trace","compare"]) {
    lookup(item).addEventListener("click", () => {
      mode = item;
      for (const other of ["live","trace","compare"]) {
        const active = mode === other;
        find(other).hidden = !active;
        lookup(other).classList.toggle("is-active", active);
        lookup(other).setAttribute("aria-pressed", String(active));
      }
      refresh();
    });
  }
  select.addEventListener("change", () => { trace.value = ""; refresh(); });
  baseline.addEventListener("change", refresh);
  trace.addEventListener("change", refresh);
  find("compute").addEventListener("click", () => setTab("compute"));
  document.querySelector('[data-tab="context-performance"]').addEventListener("click", refresh);
  window.setInterval(refresh, 1500);
})();
