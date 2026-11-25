"use strict";
(() => {
  const host = document.getElementById("tcpbrain-panel");
  if (!host) return;
  const view = document.getElementById("network-view");
  const nf = new Intl.NumberFormat("pt-BR");
  const labels = {
    NO_RECORDED_DROPS: "Sem descartes registrados",
    DROPS_RECORDED: "Descartes registrados",
    NO_CARRIER: "Sem enlace",
    ISSUES_RECORDED: "Falhas acumuladas",
    BACKPRESSURE_RECORDED: "Pressão na distribuição",
    NO_ERRORS_RECORDED: "Sem falhas registradas",
    UNVERIFIED: "Não certificada",
    LEGACY_UNVERIFIED: "Legado: cobertura não certificada",
    BOUNDED_COVERAGE: "Janela de observação válida",
    INCOMPLETE_INPUTS: "Fontes incompletas",
    INCONCLUSIVE: "Inconclusiva", UNKNOWN: "Desconhecida",
    FRESH: "Atual", STALE: "Desatualizada", INVALID: "Inválida",
    UNAVAILABLE: "Indisponível"
  };
  const tones = {
    NO_RECORDED_DROPS: "good", NO_ERRORS_RECORDED: "good",
    BOUNDED_COVERAGE: "good", FRESH: "good",
    ISSUES_RECORDED: "warn", BACKPRESSURE_RECORDED: "warn",
    UNVERIFIED: "warn", LEGACY_UNVERIFIED: "warn", INCONCLUSIVE: "warn",
    DROPS_RECORDED: "bad", NO_CARRIER: "bad", INCOMPLETE_INPUTS: "bad",
    STALE: "bad", INVALID: "bad", UNAVAILABLE: "bad"
  };
  function node(tag, text, cls) {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = text;
    if (cls) el.className = cls;
    return el;
  }
  function number(value, digits = 0) {
    return typeof value === "number" && Number.isFinite(value)
      ? nf.format(Number(value.toFixed(digits))) : "—";
  }
  function rate(value) { return typeof value === "number" ? number(value/1e6, 2) + " Mbps" : "—"; }
  function age(value) { return typeof value === "number" ? number(value, 1) + " s" : "—"; }
  function uptime(v) {
    if (typeof v !== "number") return "—";
    return number(Math.floor(v/3600))+"h "+number(Math.floor(v%3600/60))+"min";
  }
  function status(key) { return node("span", labels[key] || "Desconhecida", "tb-state "+(tones[key] || "neutral")); }
  function metric(parent, title, value) {
    const div=node("div",undefined,"tb-metric");
    div.append(node("span",title,"tb-label"),node("strong",value));
    parent.append(div);
  }
  function capture(name, s) {
    const box=node("article",undefined,"tb-card");
    const head=node("div",undefined,"tb-card-head");
    head.append(node("h3",name),status(s.telemetry));
    box.append(head);
    const health=node("div",undefined,"tb-health");
    const a=node("div"); a.append(node("span","CAPTURA","tb-label"),status(s.capture_health));
    const b=node("div"); b.append(node("span","DISTRIBUIÇÃO","tb-label"),status(s.delivery_health));
    health.append(a,b);box.append(health);
    const grid=node("div",undefined,"tb-metrics");
    metric(grid,"Tráfego atual",rate(s.current_bps));
    metric(grid,"Pico registrado",rate(s.peak_bps));
    metric(grid,"Pacotes capturados",number(s.packets_received));
    metric(grid,"Descarte AF_PACKET",number(s.socket_drop_count));
    metric(grid,"RX drop / missed",number(s.interface_rx_drop_delta)+" / "+number(s.interface_rx_missed_delta));
    metric(grid,"Falhas de assinante",number(s.subscriber_drops));
    metric(grid,"Overflow de fila",number(s.subscriber_queue_overflows));
    metric(grid,"Fila atual / pico (bytes)",number(s.subscriber_queued_bytes)+" / "+number(s.subscriber_queue_peak_bytes));
    metric(grid,"Backpressure",number(s.subscriber_backpressure));
    metric(grid,"Link",typeof s.link_speed_mbps==="number"?number(s.link_speed_mbps)+" Mbps":"—");
    box.append(grid,node("p","Amostra "+age(s.status_age_seconds)+" · processo "+uptime(s.uptime_seconds)+" · "+(s.mode==="LEGACY"?"telemetria legada":s.mode==="V2_COUNTERS"?"contadores V2":"versão desconhecida"),"tb-muted"));
    return box;
  }
  function render(payload) {
    if (!payload || payload.schema_version!=="tcpbrain.mgmt.v1" || !payload.sources || !payload.flow) throw new Error("invalid");
    const body=host.querySelector(".tb-body");body.replaceChildren();
    const grid=node("div",undefined,"tb-captures");
    grid.append(capture("MIRROR",payload.sources.mirror||{}),capture("TRUNK",payload.sources.trunk||{}));
    body.append(grid);
    const f=payload.flow, box=node("article",undefined,"tb-card");
    const head=node("div",undefined,"tb-card-head");head.append(node("h3","CORRELAÇÃO E COBERTURA"),status(f.observation_health));
    box.append(head);
    const metrics=node("div",undefined,"tb-metrics");
    metric(metrics,"Fontes conectadas",Array.isArray(f.inputs_connected)?number(f.inputs_connected.length)+" / 2":"—");
    metric(metrics,"Eventos recebidos",number(f.input_events));
    metric(metrics,"Fluxos ativos",number(f.active_flows));
    metric(metrics,"Encaminhamentos observados",number(f.forward_ok_total));
    metric(metrics,"Egressos não observados",number(f.forward_missing_total));
    metric(metrics,"Observações inconclusivas",number(f.observation_inconclusive_total));
    box.append(metrics);
    if (f.continuity && Object.keys(f.continuity).length) {
      const c=node("div",undefined,"tb-metrics");
      for (const n of ["mirror","trunk"]) {
        const t=f.continuity[n]||{}, w=(f.coverage||{})[n]||{};
        metric(c,n.toUpperCase()+" · lacunas",number(t.gaps));
        metric(c,n.toUpperCase()+" · reconexões",number(t.disconnects));
        metric(c,n.toUpperCase()+" · watermark",age(w.watermark_age_seconds));
      }
      box.append(c);
    }
    if (Array.isArray(f.forward_pairs) && f.forward_pairs.length) {
      box.append(node("h4","Encaminhamento observado por par VLAN"));
      const table=node("table",undefined,"tb-table"),thead=node("thead"),tr=node("tr");
      for (const title of ["Origem","Destino","Observações"]) {
        const th=node("th",title);th.scope="col";tr.append(th);
      }
      thead.append(tr);table.append(thead);
      const tbody=node("tbody");
      for (const row of f.forward_pairs) {
        const tr=node("tr");
        tr.append(node("td",number(row.from_vlan)),node("td",number(row.to_vlan)),node("td",number(row.count)));
        tbody.append(tr);
      }
      table.append(tbody);box.append(table);
    }
    box.append(node("p","Contadores acumulados por época de processo. Ausência de descarte registrado não comprova captura integral. Egresso não observado não confirma falha do roteador.","tb-muted"));
    body.append(box);
    host.querySelector(".tb-refresh").textContent="Atualização "+new Date(payload.sampled_at_ms).toLocaleTimeString("pt-BR")+" · leitura de status";
  }
  let busy=false;
  let lastSuccess=0;
  function clearStale() {
    if (!lastSuccess || Date.now()-lastSuccess>5000) {
      host.querySelector(".tb-body").replaceChildren(node("p","Aguardando telemetria atual. Estado desconhecido.","tb-muted"));
      host.querySelector(".tb-refresh").textContent="Sem atualização válida";
    }
  }
  async function refresh() {
    if (busy || !view.classList.contains("is-active") || document.hidden) return;
    clearStale();
    busy=true;
    try {
      const r=await fetch("/api/tcpbrain/status",{cache:"no-store",signal:AbortSignal.timeout(4000)});
      if(!r.ok) throw new Error("unavailable");
      render(await r.json());
      lastSuccess=Date.now();
    } catch (_) {
      lastSuccess=0;
      host.querySelector(".tb-body").replaceChildren(node("p","Telemetria indisponível. Estado desconhecido.","tb-state bad"));
      host.querySelector(".tb-refresh").textContent="Sem atualização válida";
    } finally {busy=false;}
  }
  window.setInterval(refresh,2000);
  document.querySelectorAll(".tab").forEach(b=>b.addEventListener("click",()=>window.setTimeout(refresh,0)));
  document.addEventListener("visibilitychange",refresh);
  refresh();
})();
