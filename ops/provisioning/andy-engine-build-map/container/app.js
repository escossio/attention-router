"use strict";

const DATA_URL = "engine-map.json";
const VALID_STATUSES = new Set(["COMPLETED", "CURRENT", "READY", "BLOCKED", "PENDING", "NOT_STARTED"]);
const VALID_MILESTONES = new Set([
  null,
  "REAL_INTERACTION_READY",
  "FIRST_CONTROLLED_EXECUTION",
  "ENGINE_TARGET_COMPLETE",
]);

const state = {
  data: null,
  stagesById: new Map(),
  filter: "all",
};

const elements = {
  tree: document.querySelector("#tree"),
  treeCanvas: document.querySelector("#tree-canvas"),
  dependencyLayer: document.querySelector("#dependency-layer"),
  currentStage: document.querySelector("#current-stage"),
  metricCompleted: document.querySelector("#metric-completed"),
  metricBlocked: document.querySelector("#metric-blocked"),
  metricRemaining: document.querySelector("#metric-remaining"),
  metricTotal: document.querySelector("#metric-total"),
  demoNotice: document.querySelector("#demo-notice"),
  errorPanel: document.querySelector("#error-panel"),
  drawer: document.querySelector("#detail-drawer"),
  drawerBackdrop: document.querySelector("#drawer-backdrop"),
  drawerClose: document.querySelector("#drawer-close"),
};

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function validateData(data) {
  assert(data && typeof data === "object", "O arquivo de dados deve conter um objeto JSON.");
  assert(data.project && typeof data.project.name === "string", "project.name é obrigatório.");
  assert(Array.isArray(data.stages) && data.stages.length > 0, "stages deve ser uma lista não vazia.");

  const ids = new Set();
  for (const stage of data.stages) {
    assert(typeof stage.id === "string" && stage.id.length > 0, "Toda etapa precisa de id.");
    assert(!ids.has(stage.id), `ID duplicado: ${stage.id}`);
    ids.add(stage.id);
    assert(typeof stage.name === "string", `${stage.id}: name é obrigatório.`);
    assert(VALID_STATUSES.has(stage.status), `${stage.id}: status inválido (${stage.status}).`);
    assert(stage.parent === null || typeof stage.parent === "string", `${stage.id}: parent inválido.`);
    assert(Array.isArray(stage.depends_on), `${stage.id}: depends_on deve ser uma lista.`);
    assert(Array.isArray(stage.unlocks), `${stage.id}: unlocks deve ser uma lista.`);
    assert(VALID_MILESTONES.has(stage.milestone ?? null), `${stage.id}: milestone inválido.`);
  }

  assert(ids.has(data.project.current_stage), "project.current_stage não referencia uma etapa existente.");
  for (const stage of data.stages) {
    if (stage.parent !== null) assert(ids.has(stage.parent), `${stage.id}: parent inexistente (${stage.parent}).`);
    for (const dependency of stage.depends_on) {
      assert(ids.has(dependency), `${stage.id}: dependência inexistente (${dependency}).`);
      assert(dependency !== stage.id, `${stage.id}: uma etapa não pode depender de si mesma.`);
    }
    for (const unlocked of stage.unlocks) {
      assert(ids.has(unlocked), `${stage.id}: unlock inexistente (${unlocked}).`);
    }
  }

  const currentStages = data.stages.filter((stage) => stage.status === "CURRENT");
  assert(currentStages.length === 1, "Deve existir exatamente uma etapa com status CURRENT.");
  assert(currentStages[0].id === data.project.current_stage, "CURRENT diverge de project.current_stage.");
}

function byId(id) {
  return state.stagesById.get(id);
}

function text(value, fallback = "—") {
  if (Array.isArray(value)) return value.length ? value.join(", ") : fallback;
  return value === null || value === undefined || value === "" ? fallback : String(value);
}

function calculateMetrics() {
  const total = state.data.stages.length;
  const completed = state.data.stages.filter((stage) => stage.status === "COMPLETED").length;
  const blocked = state.data.stages.filter((stage) => stage.status === "BLOCKED").length;
  elements.metricCompleted.textContent = `${completed} / ${total}`;
  elements.metricBlocked.textContent = String(blocked);
  elements.metricRemaining.textContent = String(total - completed);
  elements.metricTotal.textContent = String(total);

  const current = byId(state.data.project.current_stage);
  elements.currentStage.textContent = `${current.id} — ${current.name}`;
  elements.demoNotice.hidden = state.data.project.demo !== true;
}

function collectCurrentPath() {
  const visible = new Set();
  const forwardVisited = new Set();
  const visitBackward = (id) => {
    if (!id || visible.has(id)) return;
    visible.add(id);
    const stage = byId(id);
    visitBackward(stage.parent);
    stage.depends_on.forEach(visitBackward);
  };
  const visitForward = (id) => {
    if (!id || forwardVisited.has(id)) return;
    forwardVisited.add(id);
    visible.add(id);
    byId(id).unlocks.forEach((nextId) => {
      visitBackward(nextId);
      visitForward(nextId);
    });
  };
  const currentId = state.data.project.current_stage;
  visitBackward(currentId);
  visitForward(currentId);
  return visible;
}

function visibleStageIds() {
  const all = state.data.stages;
  if (state.filter === "completed") {
    return new Set(all.filter((stage) => stage.status === "COMPLETED").map((stage) => stage.id));
  }
  if (state.filter === "pending") {
    return new Set(all.filter((stage) => ["PENDING", "BLOCKED", "NOT_STARTED"].includes(stage.status)).map((stage) => stage.id));
  }
  if (state.filter === "current") return collectCurrentPath();
  return new Set(all.map((stage) => stage.id));
}

function createNode(stage) {
  const card = document.createElement("button");
  card.type = "button";
  card.className = "node-card";
  card.dataset.stageId = stage.id;
  card.dataset.status = stage.status;
  if (stage.milestone) card.dataset.milestone = stage.milestone;
  card.setAttribute("aria-label", `${stage.id}, ${stage.name}, estado ${stage.status}`);

  const id = document.createElement("span");
  id.className = "node-id";
  id.textContent = stage.id;

  const status = document.createElement("span");
  status.className = "node-status";
  status.textContent = stage.status;

  const name = document.createElement("span");
  name.className = "node-name";
  name.textContent = stage.name;

  const description = document.createElement("span");
  description.className = "node-description";
  description.textContent = text(stage.description);

  const dependencies = document.createElement("span");
  dependencies.className = "node-deps";
  dependencies.textContent = stage.depends_on.length
    ? `DEPENDS ON: ${stage.depends_on.join(" · ")}`
    : "DEPENDS ON: NONE";

  card.append(id, status, name, description, dependencies);
  card.addEventListener("click", () => openDetails(stage));
  return card;
}

function buildBranch(stage, childrenByParent, visible) {
  const item = document.createElement("li");
  item.append(createNode(stage));

  const children = (childrenByParent.get(stage.id) || []).filter((child) => visible.has(child.id));
  if (children.length) {
    const list = document.createElement("ul");
    for (const child of children) list.append(buildBranch(child, childrenByParent, visible));
    item.append(list);
  }
  return item;
}

function renderTree() {
  const visible = visibleStageIds();
  elements.tree.replaceChildren();

  if (visible.size === 0) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "Nenhuma etapa corresponde a este filtro.";
    elements.tree.append(empty);
    drawDependencies(visible);
    return;
  }

  const childrenByParent = new Map();
  for (const stage of state.data.stages) {
    if (!childrenByParent.has(stage.parent)) childrenByParent.set(stage.parent, []);
    childrenByParent.get(stage.parent).push(stage);
  }

  const roots = state.data.stages.filter((stage) => {
    if (!visible.has(stage.id)) return false;
    return stage.parent === null || !visible.has(stage.parent);
  });

  const rootList = document.createElement("ul");
  for (const root of roots) rootList.append(buildBranch(root, childrenByParent, visible));
  elements.tree.append(rootList);

  requestAnimationFrame(() => drawDependencies(visible));
}

function drawDependencies(visible) {
  const svg = elements.dependencyLayer;
  svg.querySelectorAll("path.dependency").forEach((path) => path.remove());
  const canvasRect = elements.treeCanvas.getBoundingClientRect();
  const width = Math.max(elements.treeCanvas.scrollWidth, elements.treeCanvas.clientWidth);
  const height = Math.max(elements.treeCanvas.scrollHeight, elements.treeCanvas.clientHeight);
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);

  for (const stage of state.data.stages) {
    if (!visible.has(stage.id)) continue;
    const target = elements.tree.querySelector(`[data-stage-id="${CSS.escape(stage.id)}"]`);
    if (!target) continue;

    for (const dependencyId of stage.depends_on) {
      if (!visible.has(dependencyId) || dependencyId === stage.parent) continue;
      const source = elements.tree.querySelector(`[data-stage-id="${CSS.escape(dependencyId)}"]`);
      if (!source) continue;

      const sourceRect = source.getBoundingClientRect();
      const targetRect = target.getBoundingClientRect();
      const x1 = sourceRect.right - canvasRect.left;
      const y1 = sourceRect.top + sourceRect.height / 2 - canvasRect.top;
      const x2 = targetRect.left - canvasRect.left;
      const y2 = targetRect.top + targetRect.height / 2 - canvasRect.top;
      const bend = Math.max(42, Math.abs(x2 - x1) * .42);

      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.classList.add("dependency");
      path.dataset.from = dependencyId;
      path.dataset.to = stage.id;
      path.setAttribute("d", `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`);
      svg.append(path);
    }
  }
}

function setDetail(id, value) {
  document.querySelector(id).textContent = text(value);
}

function openDetails(stage) {
  setDetail("#detail-id", stage.id);
  setDetail("#detail-name", stage.name);
  setDetail("#detail-description", stage.description);
  setDetail("#detail-parent", stage.parent);
  setDetail("#detail-milestone", stage.milestone);
  setDetail("#detail-dependencies", stage.depends_on);
  setDetail("#detail-unlocks", stage.unlocks);
  setDetail("#detail-criteria", stage.completion_criteria);
  setDetail("#detail-completion", stage.completion_summary);
  const pill = document.querySelector("#detail-status");
  pill.textContent = stage.status;
  pill.dataset.status = stage.status;
  elements.drawer.classList.add("is-open");
  elements.drawer.setAttribute("aria-hidden", "false");
  elements.drawerBackdrop.hidden = false;
  elements.drawerClose.focus();
}

function closeDetails() {
  elements.drawer.classList.remove("is-open");
  elements.drawer.setAttribute("aria-hidden", "true");
  elements.drawerBackdrop.hidden = true;
}

function bindControls() {
  document.querySelectorAll(".filter-button").forEach((button) => {
    button.addEventListener("click", () => {
      state.filter = button.dataset.filter;
      document.querySelectorAll(".filter-button").forEach((candidate) => {
        candidate.classList.toggle("is-active", candidate === button);
      });
      renderTree();
    });
  });
  elements.drawerClose.addEventListener("click", closeDetails);
  elements.drawerBackdrop.addEventListener("click", closeDetails);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeDetails();
  });
  window.addEventListener("resize", () => {
    if (state.data) requestAnimationFrame(() => drawDependencies(visibleStageIds()));
  });
}

async function loadMap() {
  try {
    const response = await fetch(`${DATA_URL}?v=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`Falha HTTP ${response.status} ao carregar ${DATA_URL}.`);
    const data = await response.json();
    validateData(data);
    state.data = data;
    state.stagesById = new Map(data.stages.map((stage) => [stage.id, stage]));
    document.title = `${data.project.name} — Build & Capability Map`;
    calculateMetrics();
    renderTree();
  } catch (error) {
    elements.errorPanel.hidden = false;
    elements.errorPanel.textContent = `ENGINE MAP NÃO CARREGADO\n${error.message}`;
    elements.tree.textContent = "Não foi possível renderizar a árvore.";
    console.error(error);
  }
}

bindControls();
loadMap();
