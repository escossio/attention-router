"use strict";
/* AR-1001 — observação read-only da ocorrência existente no Zabbix ROC. */
(() => {
  const tab = document.querySelector('.tab[data-tab="ar1001"]');
  const view = document.getElementById("ar1001-view");
  if (!tab || !view) return;

  const labels = {
    "andy.reply.probe_ok": "Integridade da sonda",
    "andy.reply.functional_stalled": "Falha de resposta ativa",
    "andy.reply.direct_6h": "Interações diretas distintas",
    "andy.reply.direct_event_6h": "Entradas diretas",
    "andy.reply.direct_processed_6h": "Entradas processadas",
    "andy.reply.decision_total_6h": "Decisões produzidas",
    "andy.reply.decisions_dry_run_6h": "Decisões sem contexto / dry-run",
    "andy.reply.no_safe_6h": "Interações com behavior.unavailable",
    "andy.reply.blueprint_6h": "Interações com conversation.blocked",
    "andy.reply.other_failed_6h": "Outras falhas de auditoria",
    "andy.reply.outbox_new_6h": "Saídas criadas na outbox",
    "andy.reply.grace_released_6h": "Janelas de resposta liberadas",
    "andy.reply.grace_canceled_6h": "Janelas de resposta canceladas",
    "andy.reply.latest_failure_epoch": "Última falha de auditoria (epoch)",
    "andy.reply.last_agent_done_epoch": "Última resposta automática DONE (epoch)",
  };
  const domains = {
    collector_integrity: "Coleta / integridade", physical: "L1 — Enlace físico",
    data_link: "L2 — Domínio Ethernet/VLAN", network: "L3 — IP e alcançabilidade",
    transport: "L4 — TCP e sessão", runtime: "Contêiner e execução",
    application_http: "L7 — HTTP/API", tracing: "OpenTelemetry / tracing",
    application_flow: "Fluxo funcional da Andy", persistence: "Persistência e estado",
    delivery: "Outbox e entrega",
  };
  const statuses = {
    OBSERVED_HEALTHY: "MEDIDO · SAUDÁVEL",
    OBSERVED_FAILURE: "FALHA OBSERVADA",
    NO_RELATED_ANOMALY: "SEM ANOMALIA NA MEDIÇÃO",
    UNKNOWN: "INDETERMINADO",
    NOT_OBSERVED: "NÃO CORRELACIONADO",
    NOT_APPLICABLE: "NÃO APLICÁVEL",
  };
  const contextLabels = {
    "andy.summary.domains": "Domínios monitorados",
    "andy.summary.reachable": "Domínios alcançáveis",
    "andy.summary.complete": "Vizinhanças completas",
    "andy.summary.incomplete": "Vizinhanças incompletas",
    "andy.topology.consistent": "Consistência da topologia",
    "andy.tempo.query_ok": "Consulta à API Tempo",
    "andy.tempo.log_probe_ok": "Sonda de logs Tempo",
  };
  const contextAssessments = {
    NETWORK_OSI_AGGREGATE: "NETWORK / OSI — panorama geral",
    TEMPO_QUERY_INTERFACE: "Tempo — API de consulta",
    TEMPO_LOG_INSPECTION: "Tempo — inspeção dos logs",
  };
  const interesting = [
    "andy.reply.direct_6h", "andy.reply.direct_processed_6h",
    "andy.reply.decisions_dry_run_6h", "andy.reply.outbox_new_6h",
  ];
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const isoTime = text => {
    const d = new Date(text);
    return Number.isFinite(d.getTime()) ? d.toLocaleString("pt-BR") : "—";
  };
  const measure = value => value && value.fresh === true && Number.isSafeInteger(value.value)
    ? String(value.value) : "—";
  const badgeClass = state =>
    state === "OBSERVED_FAILURE" ? "failure" :
    state === "OBSERVED_HEALTHY" || state === "NO_RELATED_ANOMALY" ? "healthy" : "unknown";

  function renderCard(payload) {
    const root = document.getElementById("ar1001-content");
    root.replaceChildren();
    const status = payload.status || "UNKNOWN";
    const header = el("section", "ficha-ar1001-head");
    const title = el("div");
    title.append(el("span", "ficha-ar1001-overline", "AR-1001 · FICHA DE OCORRÊNCIA"),
      el("h2", "", "Andy — ausência de resposta automática"),
      el("p", "", "Fonte: ROC Zabbix · Janela das métricas: 6 horas · Sem diagnóstico de causa raiz"));
    const chips = el("div", "ficha-ar1001-chips");
    chips.append(el("strong", "ficha-ar1001-chip " + badgeClass(status), statuses[status] || "INDETERMINADO"),
      el("span", "", "Consulta: " + isoTime(payload.observed_at)));
    const triggers = payload.trigger_states || {};
    if (triggers.ANDY_WA_AUTO_REPLY_STALLED === 1) {
      chips.append(el("span", "ficha-ar1001-open-event", "Zabbix: problema original aberto"));
    } else if (triggers.ANDY_WA_AUTO_REPLY_STALLED === 0) {
      chips.append(el("span", "ficha-ar1001-open-event", "Zabbix: trigger funcional em OK"));
    }
    header.append(title, chips);
    root.append(header);

    const metrics = payload.metrics || {};
    const main = el("section", "ficha-ar1001-counters");
    interesting.forEach(key => {
      const box = el("article", "ficha-ar1001-counter");
      box.append(el("span", "", labels[key]), el("strong", "", measure(metrics[key])),
                 el("small", "", metrics[key] && metrics[key].fresh ? "Dado observado no Zabbix" : "Dado ausente ou expirado"));
      main.append(box);
    });
    root.append(main);

    // Context is intentionally separate from incident facets. A healthy
    // general OSI snapshot is not evidence that an Andy message path is OK.
    const context = payload.general_context || {};
    const ctxHeading = el("div", "ficha-ar1001-section-title");
    ctxHeading.append(
      el("h3", "", "Contexto geral · NETWORK/OSI e Tempo"),
      el("p", "", "Outras medições existentes no Zabbix. Não estão correlacionadas à interação AR-1001 e não determinam a causa.")
    );
    root.append(ctxHeading);
    const ctxRoot = el("section", "ficha-ar1001-context");
    const ctxStates = el("div", "ficha-ar1001-context-states");
    const assessments = Array.isArray(context.assessments) ? context.assessments : [];
    assessments.forEach(item => {
      if (!Object.prototype.hasOwnProperty.call(contextAssessments, item.source)) return;
      const line = el("div", "ficha-ar1001-context-row");
      line.append(el("span", "", contextAssessments[item.source]),
                  el("strong", "ficha-ar1001-chip " + badgeClass(item.state),
                     statuses[item.state] || "INDETERMINADO"));
      ctxStates.append(line);
    });
    if (!ctxStates.children.length) {
      ctxStates.append(el("p", "ficha-ar1001-disclaimer", "Dados gerais não disponíveis."));
    }
    ctxRoot.append(ctxStates);
    const ctxMetrics = el("div", "ficha-ar1001-context-metrics");
    const sensors = context.sensors || {};
    Object.keys(contextLabels).forEach(key => {
      const v = sensors[key];
      const line = el("div", "ficha-ar1001-context-value");
      line.append(el("span", "", contextLabels[key]),
                  el("strong", "", measure(v)),
                  el("small", "", v && v.fresh ? "Amostra geral · " + isoTime(v.sampled_at)
                    : "Sem amostra recente"));
      ctxMetrics.append(line);
    });
    ctxRoot.append(ctxMetrics);
    ctxRoot.append(el("p", "ficha-ar1001-disclaimer",
      "Contexto não vinculado ao incidente. Oito domínios alcançáveis não significam fluxo WhatsApp funcional; API Tempo disponível não comprova trace correspondente."));
    root.append(ctxRoot);

    const intro = el("div", "ficha-ar1001-section-title");
    intro.append(el("h3", "", "Roteiro de troubleshooting · evidências por domínio"),
      el("p", "", "A leitura segue o caminho de dependências, mas não presume que uma camada esteja íntegra sem medições vinculadas à ocorrência."));
    root.append(intro);

    const domainsRoot = el("section", "ficha-ar1001-domains");
    (payload.facets || []).forEach(f => {
      if (!Object.prototype.hasOwnProperty.call(domains, f.dimension)) return;
      const line = el("article", "ficha-ar1001-domain");
      line.append(el("span", "ficha-ar1001-domain-name", domains[f.dimension]),
        el("strong", "ficha-ar1001-chip " + badgeClass(f.state), statuses[f.state] || "INDETERMINADO"),
        el("small", "", String(f.reason_code || "SEM_EVIDENCIA")));
      domainsRoot.append(line);
    });
    root.append(domainsRoot);

    const heading = el("div", "ficha-ar1001-section-title");
    heading.append(el("h3", "", "Legenda e 15 medições do Zabbix"),
      el("p", "", "Contadores de auditoria podem contar a mesma interação em mais de uma categoria. Estado 0 na outbox não prova, isoladamente, uma falha de entrega."));
    root.append(heading);
    const table = el("div", "ficha-ar1001-all");
    Object.keys(labels).forEach(key => {
      const v = metrics[key];
      const line = el("div", "ficha-ar1001-measure");
      line.append(el("span", "", labels[key]), el("strong", "", measure(v)),
        el("small", "", v && v.fresh ? isoTime(v.sampled_at) : "AUSENTE / EXPIRADO"));
      table.append(line);
    });
    root.append(table);
    root.append(el("p", "ficha-ar1001-disclaimer",
      payload.note || "Dados não disponíveis. Não inferir sucesso a partir de ausência de telemetria."));
  }

  let busy = false;
  async function update() {
    if (busy || !tab.classList.contains("is-active")) return;
    busy = true;
    try {
      const response = await fetch("/api/ar1001", {cache: "no-store"});
      if (!response.ok) throw new Error("STATUS_" + response.status);
      const data = await response.json();
      if (data.code !== "AR-1001") throw new Error("INVALID_CONTRACT");
      renderCard(data);
    } catch (_error) {
      renderCard({
        code: "AR-1001", status: "UNKNOWN",
        facets: [{dimension: "collector_integrity", state: "UNKNOWN",
                  reason_code: "FICHA_UNAVAILABLE"}],
        metrics: {}, note: "Consulta indisponível. Nenhum estado foi presumido.",
      });
    } finally {
      busy = false;
    }
  }
  tab.addEventListener("click", update);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) update(); });
  window.setInterval(update, 30000);
})();
