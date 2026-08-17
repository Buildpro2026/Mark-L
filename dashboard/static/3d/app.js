// JARVIS 3D Command Center — real WebGL/Three.js spatial scene.
//
// Reuses the existing backend as the source of truth:
//   GET  /3d/api/overview            → root hierarchy + module summaries
//   GET  /3d/api/module/{id}         → per-Nucleus live data + children/path
//   POST /3d/api/command             → {action:"navigate", nav_action, nucleus_id}
//   WS   /3d/ws                      → {type:"navigate", ...} / {type:"jarvis_state", state}
//
// Nothing here invents backend data — every field rendered in the right
// panel comes straight from a /3d/api/* response; placeholders (e.g.
// Communications) are rendered as explicitly-labeled placeholders.

import * as THREE from "three";
import { OrbitControls } from "/3d/assets/vendor/OrbitControls.js";

// ── Auth — bearer token from the same session the /login page issues ────
// index.html already redirects to /login before this module loads if no
// token exists, so _authToken below is expected to be set by the time any
// of this runs. See dashboard/server.py for the matching server-side check
// on /3d/api/* and /3d/ws.
const _authToken = sessionStorage.getItem("jarvis_token") || "";
function _authFetch(url, opts = {}) {
  opts.headers = Object.assign({}, opts.headers, { Authorization: `Bearer ${_authToken}` });
  return fetch(url, opts);
}

// ── DOM refs ────────────────────────────────────────────────────────────
const canvas       = document.getElementById("three-canvas");
const noWebglEl    = document.getElementById("no-webgl");
const focusTitleEl = document.getElementById("focus-title");
const focusSummaryEl = document.getElementById("focus-summary");
const breadcrumbEl = document.getElementById("breadcrumb");
const revenueProgressEl = document.getElementById("revenue-progress");
const stateDotEl   = document.getElementById("state-dot");
const stateLabelEl = document.getElementById("state-label");
const panelTitleEl = document.getElementById("panel-title");
const panelStatusEl = document.getElementById("panel-status");
const panelDetailsEl = document.getElementById("panel-details");
const panelChildrenEl = document.getElementById("panel-children");
const btnHome = document.getElementById("btn-home");
const btnBack = document.getElementById("btn-back");
const stageEl = document.getElementById("stage");
const connStatusEl = document.getElementById("conn-status");
const connStatusLabelEl = document.getElementById("conn-status-label");
const hudToggleEl = document.getElementById("hud-toggle");
const hudExpandEl = document.getElementById("hud-expand");
const toastStackEl = document.getElementById("toast-stack");

// ── Layout constants ────────────────────────────────────────────────────
const ROOT_RADIUS  = 7.5;
const CHILD_RADIUS = 2.6;
const ORB_RADIUS   = 1.1;
const NODE_RADIUS  = 0.55;
const CHILD_NODE_RADIUS = 0.34;

const STATE_COLORS = {
  idle:        0x4b6b7c,
  listening:   0x00d4ff,
  thinking:    0xb98bff,
  speaking:    0x5cffc4,
  interrupted: 0xff5c6c,
};
const STATE_PULSE_SPEED = {
  idle: 0.6, listening: 1.2, thinking: 2.4, speaking: 2.0, interrupted: 6.0,
};

const NUCLEUS_COLORS = {
  buildpro: 0x00d4ff, ddf: 0x5cffc4, careerrocket: 0xffb85c,
  email: 0x8fb8ff, calendar: 0xff8fd1, files: 0xb98bff,
  reports: 0x9fe6ff, communications: 0x7d8fa6, system: 0xff5c6c,
  personal: 0x8fa8b8,
};

// ── Three.js setup ──────────────────────────────────────────────────────
let renderer, scene, camera, controls, clock;
let orbMesh, orbLight, starField;
const rootGroup = new THREE.Group();
const childGroup = new THREE.Group();
const lineGroup = new THREE.Group();

function initThree() {
  try {
    renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: false });
  } catch (e) {
    noWebglEl.classList.add("show");
    console.error("[3D] WebGL init failed:", e);
    return false;
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(stageEl.clientWidth, stageEl.clientHeight);
  renderer.setClearColor(0x02060b, 1);

  scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(0x02060b, 0.028);

  camera = new THREE.PerspectiveCamera(52, stageEl.clientWidth / stageEl.clientHeight, 0.1, 500);
  camera.position.set(0, 6.5, 17);

  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.minDistance = 4;
  controls.maxDistance = 40;
  controls.maxPolarAngle = Math.PI * 0.92;
  controls.target.set(0, 0, 0);

  scene.add(new THREE.AmbientLight(0x33475a, 1.1));
  const key = new THREE.PointLight(0x9fe6ff, 2.2, 60);
  key.position.set(10, 12, 8);
  scene.add(key);

  scene.add(rootGroup, childGroup, lineGroup);

  createStarfield();
  createOrb();
  window.addEventListener("resize", onResize);
  clock = new THREE.Clock();
  return true;
}

function createStarfield() {
  const count = 900;
  const positions = new Float32Array(count * 3);
  for (let i = 0; i < count; i++) {
    const r = 40 + Math.random() * 140;
    const theta = Math.random() * Math.PI * 2;
    const phi = Math.acos(2 * Math.random() - 1);
    positions[i * 3]     = r * Math.sin(phi) * Math.cos(theta);
    positions[i * 3 + 1] = r * Math.cos(phi) * 0.5;
    positions[i * 3 + 2] = r * Math.sin(phi) * Math.sin(theta);
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  const mat = new THREE.PointsMaterial({ color: 0x6fa8c9, size: 0.35, transparent: true, opacity: 0.55 });
  starField = new THREE.Points(geo, mat);
  scene.add(starField);
}

function createOrb() {
  // True smooth sphere (not a faceted icosahedron) — the "sun" of the
  // spatial solar system, with the Nucleus planets orbiting it.
  const geo = new THREE.SphereGeometry(ORB_RADIUS, 48, 32);
  const mat = new THREE.MeshStandardMaterial({
    color: STATE_COLORS.idle, emissive: STATE_COLORS.idle, emissiveIntensity: 0.9,
    roughness: 0.25, metalness: 0.15, flatShading: false,
  });
  orbMesh = new THREE.Mesh(geo, mat);
  orbMesh.userData = { kind: "core", id: "jarvis", name: "Jarvis" };
  scene.add(orbMesh);

  orbLight = new THREE.PointLight(STATE_COLORS.idle, 3.2, 14);
  orbLight.position.set(0, 0, 0);
  scene.add(orbLight);

  const label = makeLabelSprite("JARVIS", "#eafcff");
  label.position.set(0, ORB_RADIUS + 0.9, 0);
  orbMesh.add(label);
}

// ── Label sprites (canvas-texture text) ────────────────────────────────
function makeLabelSprite(text, color = "#dff6ff", opts = {}) {
  const scale = opts.scale || 1;
  const canvasEl = document.createElement("canvas");
  const size = 256;
  canvasEl.width = size; canvasEl.height = 64;
  const ctx = canvasEl.getContext("2d");
  ctx.font = `600 ${opts.fontSize || 30}px Inter, Segoe UI, Arial, sans-serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.shadowColor = "rgba(0,0,0,0.85)";
  ctx.shadowBlur = 8;
  ctx.fillStyle = color;
  ctx.fillText(text, size / 2, 32);
  const tex = new THREE.CanvasTexture(canvasEl);
  tex.minFilter = THREE.LinearFilter;
  const mat = new THREE.SpriteMaterial({ map: tex, transparent: true, depthWrite: false });
  const sprite = new THREE.Sprite(mat);
  sprite.scale.set(2.6 * scale, 0.65 * scale, 1);
  return sprite;
}

function ringPositions(count, radius, centerY = 0) {
  const out = [];
  for (let i = 0; i < count; i++) {
    const angle = (i / count) * Math.PI * 2;
    out.push(new THREE.Vector3(Math.cos(angle) * radius, centerY + (i % 2 === 0 ? 0 : 0.35), Math.sin(angle) * radius));
  }
  return out;
}

// ── Nucleus meshes ──────────────────────────────────────────────────────
function makeNucleusMesh(node, radius, color, placeholder = false) {
  // Smooth spheres ("planets") orbiting the Jarvis "sun" — placeholders
  // stay visually distinct via wireframe + transparency, not faceting.
  const geo = new THREE.SphereGeometry(radius, 24, 16);
  const mat = new THREE.MeshStandardMaterial({
    color, emissive: color, emissiveIntensity: placeholder ? 0.25 : 0.55,
    roughness: 0.35, metalness: 0.2, wireframe: placeholder,
    transparent: placeholder, opacity: placeholder ? 0.55 : 1,
  });
  const mesh = new THREE.Mesh(geo, mat);
  mesh.userData = { kind: "nucleus", id: node.id, name: node.name, node, placeholder };
  const label = makeLabelSprite(node.name, placeholder ? "#8fa0ad" : "#eafcff", { scale: 0.85 });
  label.position.set(0, radius + 0.55, 0);
  mesh.add(label);
  return mesh;
}

function clearGroup(group) {
  for (const child of [...group.children]) {
    group.remove(child);
    child.geometry?.dispose?.();
    child.material?.dispose?.();
  }
}

function drawConnection(fromPos, toPos, color = 0x2f5b6e) {
  const geo = new THREE.BufferGeometry().setFromPoints([fromPos, toPos]);
  const mat = new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.55 });
  lineGroup.add(new THREE.Line(geo, mat));
}

let hierarchyRoot = null;
const rootMeshes = new Map();     // id -> mesh (root ring)
let childMeshes = new Map();      // id -> mesh (currently expanded children)
let infoObjects = [];             // spawned data objects (files/deals/etc.)

function buildRootRing(hierarchy) {
  hierarchyRoot = hierarchy;
  clearGroup(rootGroup);
  rootMeshes.clear();
  const children = (hierarchy?.children || []).filter(c => c.id !== "jarvis");
  const positions = ringPositions(children.length, ROOT_RADIUS);
  children.forEach((node, i) => {
    const color = NUCLEUS_COLORS[node.id] ?? 0x8fa8b8;
    const mesh = makeNucleusMesh(node, NODE_RADIUS, color);
    mesh.position.copy(positions[i]);
    rootGroup.add(mesh);
    rootMeshes.set(node.id, mesh);
    drawConnection(new THREE.Vector3(0, 0, 0), positions[i], color);
  });
}

function showChildrenFor(nucleusId, parentPos, children) {
  clearGroup(childGroup);
  childMeshes = new Map();
  clearInfoObjects();
  if (!children || !children.length) return;
  const positions = ringPositions(children.length, CHILD_RADIUS, parentPos.y + 1.6).map(p => p.clone().add(new THREE.Vector3(parentPos.x, 0, parentPos.z)));
  const parentColor = NUCLEUS_COLORS[nucleusId] ?? 0x8fa8b8;
  children.forEach((child, i) => {
    const placeholder = !!child.placeholder;
    const mesh = makeNucleusMesh(child, CHILD_NODE_RADIUS, placeholder ? 0x5a6a78 : parentColor, placeholder);
    mesh.position.copy(positions[i]);
    childGroup.add(mesh);
    childMeshes.set(child.id, mesh);
    drawConnection(parentPos, positions[i], parentColor);
  });
}

function clearInfoObjects() {
  for (const obj of infoObjects) {
    childGroup.remove(obj);
    obj.geometry?.dispose?.();
    obj.material?.dispose?.();
  }
  infoObjects = [];
}

// Spatial representation of real backend data — Files/Reports/Deals/System —
// per the "Information Objects" requirement. Skipped entirely when there is
// no live data (email/calendar not configured) rather than fabricating any.
function spawnInfoObjects(kind, items, centerPos) {
  if (!items || !items.length) return;
  const positions = ringPositions(items.length, CHILD_RADIUS + 1.4, centerPos.y + 1.6)
    .map(p => p.clone().add(new THREE.Vector3(centerPos.x, 0, centerPos.z)));
  items.slice(0, 8).forEach((item, i) => {
    const geo = new THREE.BoxGeometry(0.3, 0.3, 0.3);
    const mat = new THREE.MeshStandardMaterial({ color: 0xffffff, emissive: 0x224455, emissiveIntensity: 0.4, roughness: 0.5 });
    const mesh = new THREE.Mesh(geo, mat);
    mesh.position.copy(positions[i]);
    mesh.userData = { kind: "info", label: item };
    const label = makeLabelSprite(String(item).slice(0, 20), "#bfe9ff", { scale: 0.6, fontSize: 24 });
    label.position.set(0, 0.45, 0);
    mesh.add(label);
    childGroup.add(mesh);
    infoObjects.push(mesh);
  });
}

// ── Camera focus tween ──────────────────────────────────────────────────
let tween = null;
function easeInOutCubic(t) { return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2; }

function flyTo(lookAt, distance = 8) {
  const dir = camera.position.clone().sub(controls.target).normalize();
  if (!isFinite(dir.x)) dir.set(0, 0.35, 1);
  const toPos = lookAt.clone().add(dir.multiplyScalar(distance));
  tween = {
    fromPos: camera.position.clone(), toPos,
    fromTarget: controls.target.clone(), toTarget: lookAt.clone(),
    start: performance.now(), duration: 900,
  };
}

function updateTween() {
  if (!tween) return;
  const t = Math.min(1, (performance.now() - tween.start) / tween.duration);
  const e = easeInOutCubic(t);
  camera.position.lerpVectors(tween.fromPos, tween.toPos, e);
  controls.target.lerpVectors(tween.fromTarget, tween.toTarget, e);
  if (t >= 1) tween = null;
}

// ── Jarvis orb state ────────────────────────────────────────────────────
let currentOrbState = "idle";
let stateHoldUntil = 0;   // interrupted flashes get a minimum visible duration

function setOrbState(state, opts = {}) {
  const now = performance.now();
  if (currentOrbState === "interrupted" && now < stateHoldUntil && !opts.force) return;
  currentOrbState = STATE_COLORS[state] ? state : "idle";
  if (currentOrbState === "interrupted") stateHoldUntil = now + 550;
  const color = STATE_COLORS[currentOrbState];
  orbMesh.material.color.setHex(color);
  orbMesh.material.emissive.setHex(color);
  orbLight.color.setHex(color);
  stateDotEl.style.background = `#${color.toString(16).padStart(6, "0")}`;
  stateDotEl.style.boxShadow = `0 0 10px #${color.toString(16).padStart(6, "0")}`;
  stateLabelEl.textContent = currentOrbState;
}

// ── Navigation state (mirrors dashboard/server.py's apply_navigation) ──
let currentNucleusId = "jarvis";
let backStack = [];
let currentModuleData = null;

function findRootNode(id) {
  return (hierarchyRoot?.children || []).find(c => c.id === id) || null;
}

async function fetchModule(id) {
  const res = await _authFetch(`/3d/api/module/${encodeURIComponent(id)}`);
  if (!res.ok) throw new Error(`module fetch failed: ${res.status}`);
  return res.json();
}

async function focusNucleus(id, { fromServer = false, pushHistory = true } = {}) {
  if (id === "jarvis") return goHome({ fromServer, notify: !fromServer });

  const node = findRootNode(id);
  const mesh = rootMeshes.get(id);
  if (!mesh || !node) return;

  if (pushHistory && currentNucleusId && currentNucleusId !== id) {
    backStack.push(currentNucleusId);
  }
  currentNucleusId = id;

  focusTitleEl.textContent = node.name;
  panelTitleEl.textContent = node.name;
  panelStatusEl.textContent = "Loading…";
  updateBreadcrumb(["Jarvis", node.name]);
  flyTo(mesh.position.clone(), 5.5);

  try {
    const payload = await fetchModule(id);
    currentModuleData = payload;
    const data = payload.data || {};
    focusSummaryEl.textContent = data.summary || `${node.name} nucleus`;
    renderInfoPanel(id, node, data);
    showChildrenFor(id, mesh.position, data.children || []);
    spawnDataObjects(id, data, mesh.position);
  } catch (e) {
    console.error("[3D] module fetch error", e);
    panelStatusEl.textContent = "This Nucleus's data couldn't be loaded right now.";
  }

  if (!fromServer) postNavigate("open", id);
}

function spawnDataObjects(id, data, pos) {
  if (id === "ddf" && Array.isArray(data.top_products)) {
    spawnInfoObjects("deal", data.top_products.map(p => p.name || p.title || "Deal"), pos);
  } else if (id === "files") {
    const items = [...(Array.isArray(data.results) ? data.results : []), ...(Array.isArray(data.recent_files) ? data.recent_files : [])];
    spawnInfoObjects("file", items.slice(0, 6).map(f => (typeof f === "string" ? f : f.name || f.path || "File")), pos);
  } else if (id === "reports" && Array.isArray(data.report_files)) {
    spawnInfoObjects("report", data.report_files, pos);
  } else if (id === "system") {
    const stats = [];
    if (typeof data.cpu_percent !== "undefined") stats.push(`CPU ${data.cpu_percent}%`);
    if (typeof data.ram_percent !== "undefined") stats.push(`RAM ${data.ram_percent}%`);
    if (typeof data.gpu_percent !== "undefined") stats.push(`GPU ${data.gpu_percent}%`);
    spawnInfoObjects("status", stats, pos);
  }
}

async function goHome({ fromServer = false, notify = true } = {}) {
  currentNucleusId = "jarvis";
  backStack = [];
  focusTitleEl.textContent = "Jarvis";
  focusSummaryEl.textContent = "Central nucleus — say a Nucleus name to navigate.";
  panelTitleEl.textContent = "Jarvis";
  updateBreadcrumb(["Jarvis"]);
  clearGroup(childGroup);
  childMeshes = new Map();
  infoObjects = [];
  flyTo(new THREE.Vector3(0, 0, 0), 17);
  try {
    const res = await _authFetch("/3d/api/overview");
    const payload = await res.json();
    panelStatusEl.textContent = payload.summary?.status || "Ready for navigation";
    renderOverviewPanel(payload);
  } catch (e) {
    console.error("[3D] overview fetch error", e);
  }
  if (notify && !fromServer) postNavigate("home", "");
}

async function goBack({ fromServer = false } = {}) {
  const prev = backStack.pop();
  if (!prev) return goHome({ fromServer, notify: !fromServer });
  await focusNucleus(prev, { fromServer, pushHistory: false });
  if (!fromServer) postNavigate("back", "");
}

function updateBreadcrumb(parts) {
  breadcrumbEl.innerHTML = parts.map(p => `<span>${escapeHtml(p)}</span>`).join("");
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ── Right-side info panel rendering (data straight from the API, no invented content) ──
// Business Intelligence + top ranked Opportunities (Phases 3/4/9) — shared
// by the ddf/buildpro/careerrocket business Nuclei panels.
function pushBusinessIntelSection(details, data) {
  const bi = data.business_intelligence;
  if (bi && !bi.error) {
    const counts = Object.entries(bi.counts || {}).filter(([, n]) => n > 0);
    if (counts.length) {
      details.push(item(`<span class="k">Business intel</span>: ${counts.map(([k, n]) => `${n} ${k}`).join(", ")}`));
    }
    if (bi.total_revenue_usd) {
      details.push(item(`<span class="k">Tracked revenue</span>: $${Math.round(bi.total_revenue_usd).toLocaleString()}`));
    }
  }
  const opps = Array.isArray(data.top_opportunities) ? data.top_opportunities : [];
  if (opps.length) {
    details.push(item(`<span class="k">Top opportunities</span>`));
    for (const o of opps) {
      details.push(item(`${escapeHtml(o.title)} — ${o.score}/100 (${escapeHtml(o.opp_type)})`));
    }
  }
}

function renderInfoPanel(id, node, data) {
  panelDetailsEl.innerHTML = "";
  panelChildrenEl.innerHTML = "";

  const details = [];
  if (id === "ddf" && Array.isArray(data.top_products)) {
    panelStatusEl.textContent = data.summary || "Daily Deal Finder data";
    if (!data.top_products.length) details.push(item("No active deals right now"));
    for (const p of data.top_products) details.push(item(`<span class="k">${escapeHtml(p.name || p.title || "Deal")}</span> — ${escapeHtml(p.price ?? p.score ?? "tracked")}`));
    pushBusinessIntelSection(details, data);
  } else if (id === "buildpro" || id === "careerrocket") {
    panelStatusEl.textContent = data.summary || `${node.name} nucleus`;
    pushBusinessIntelSection(details, data);
  } else if (id === "files") {
    panelStatusEl.textContent = "Live filesystem search + recent files";
    const results = Array.isArray(data.results) ? data.results : [];
    const recent = Array.isArray(data.recent_files) ? data.recent_files : [];
    if (!results.length && !recent.length) details.push(item("No file results yet"));
    for (const f of results.slice(0, 5)) details.push(item(`<span class="k">Found</span> ${escapeHtml(typeof f === "string" ? f : f.name || f.path)}`));
    for (const f of recent.slice(0, 5)) details.push(item(`<span class="k">Recent</span> ${escapeHtml(typeof f === "string" ? f : f.name || f.path)}`));
  } else if (id === "reports") {
    panelStatusEl.textContent = data.summary || "System and business reports";
    const sys = data.system_status || {};
    for (const [k, v] of Object.entries(sys)) details.push(item(`<span class="k">${escapeHtml(k)}</span>: ${escapeHtml(v)}`));
    for (const f of (data.report_files || [])) details.push(item(`<span class="k">Report</span> ${escapeHtml(f)}`));
  } else if (id === "email" || id === "calendar") {
    panelStatusEl.textContent = data.status || `${node.name} workflow`;
    details.push(item(data.detail || data.summary || "Not configured yet", !data.configured));
  } else if (id === "communications") {
    // Twilio states: NOT_CONFIGURED / CONFIGURED / CONNECTED / ERROR — only
    // render placeholder (dashed) styling for channels that are actually
    // unconfigured, not for a genuinely live phone/sms channel.
    panelStatusEl.textContent = data.status || "NOT_CONFIGURED";
    for (const [channel, info] of Object.entries(data.channels || {})) {
      const isPlaceholder = !info.status || info.status === "placeholder" || info.status === "NOT_CONFIGURED";
      details.push(item(`<span class="k">${escapeHtml(channel)}</span> [${escapeHtml(info.status || "placeholder")}]: ${escapeHtml(info.detail)}`, isPlaceholder));
    }
    if (Array.isArray(data.missed_calls) && data.missed_calls.length) {
      details.push(item(`<span class="k">Missed calls</span>: ${data.missed_calls.length}`));
    }
    if (Array.isArray(data.history) && data.history.length) {
      for (const h of data.history.slice(0, 5)) {
        const who = h.direction === "outbound" ? h.to_number : h.from_number;
        details.push(item(`${escapeHtml(h.direction)} ${escapeHtml(h.kind)} — ${escapeHtml(who || "unknown")} (${escapeHtml(h.status || "")})`));
      }
    }
  } else if (id === "system") {
    panelStatusEl.textContent = "Live system metrics";
    const SPECIAL_KEYS = ["node", "children", "path", "agents", "strategic_objective", "business_intelligence"];
    for (const [k, v] of Object.entries(data)) {
      if (SPECIAL_KEYS.includes(k)) continue;
      details.push(item(`<span class="k">${escapeHtml(k)}</span>: ${escapeHtml(v)}`));
    }
    // Agent Orchestrator status — rendered as its own group instead of
    // falling into the generic key:value dump above.
    const agents = (data.agents && Array.isArray(data.agents.agents)) ? data.agents.agents : [];
    if (agents.length) {
      details.push(item(`<span class="k">Agents</span>`));
      for (const a of agents) {
        details.push(item(`${escapeHtml(a.name)} — ${escapeHtml(a.status)} (${escapeHtml(a.permission_level)})`));
      }
      if (data.agents.pending_approval_count) {
        details.push(item(`${data.agents.pending_approval_count} task(s) awaiting approval`, true));
      }
    }
    // Strategic objective + cross-business intelligence rollup (Phase 9).
    if (data.strategic_objective && !data.strategic_objective.error) {
      const so = data.strategic_objective;
      details.push(item(
        `<span class="k">Objective</span>: $${Math.round(so.cumulative_revenue_usd).toLocaleString()} of ` +
        `$${Math.round(so.target_amount_usd).toLocaleString()} (${so.progress_pct}%) — ` +
        `stretch by ${escapeHtml(so.stretch_deadline)}, committed by ${escapeHtml(so.committed_deadline)}`
      ));
    }
    pushBusinessIntelSection(details, data);
  } else {
    panelStatusEl.textContent = data.summary || `${node.name} nucleus`;
  }

  panelDetailsEl.innerHTML = details.join("") || `<div class="info-empty">No additional data for this Nucleus yet.</div>`;

  const children = data.children || [];
  panelChildrenEl.innerHTML = children.length
    ? children.map(c => item(c.placeholder ? `${escapeHtml(c.name)} — coming soon` : escapeHtml(c.name), !!c.placeholder)).join("")
    : `<div class="info-empty">No sub-branches.</div>`;
}

function renderOverviewPanel(payload) {
  const modules = payload.modules || [];
  panelDetailsEl.innerHTML = modules.map(m => item(`<span class="k">${escapeHtml(m.title)}</span> — ${escapeHtml(m.status || "Ready")}`)).join("");
  panelChildrenEl.innerHTML = (payload.hierarchy?.children || [])
    .filter(c => c.id !== "jarvis")
    .map(c => item(escapeHtml(c.name)))
    .join("");
  renderRevenueProgress(payload.strategic_objective);
}

// $1,000,000 / 6-12mo strategic objective — HUD progress bar, straight from
// GET /3d/api/overview's strategic_objective field (actions/strategic_objective.py).
function renderRevenueProgress(objective) {
  if (!objective) { revenueProgressEl.innerHTML = ""; return; }
  const pct = Math.max(0, Math.min(100, objective.progress_pct || 0));
  revenueProgressEl.innerHTML = (
    `$${Math.round(objective.cumulative_revenue_usd || 0).toLocaleString()} of ` +
    `$${Math.round(objective.target_amount_usd || 0).toLocaleString()} (${pct}%)` +
    `<div class="bar"><div class="bar-fill" style="width:${pct}%"></div></div>`
  );
}

function item(html, placeholder = false) {
  return `<div class="info-item${placeholder ? " placeholder" : ""}">${html}</div>`;
}

// ── Backend sync: mouse clicks post the same action voice uses ─────────
function postNavigate(navAction, nucleusId) {
  _authFetch("/3d/api/command", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "navigate", nav_action: navAction, nucleus_id: nucleusId }),
  }).catch(() => { /* mouse nav must keep working even if the backend call fails */ });
}

// ── Connection state + notifications ────────────────────────────────────
function setConnStatus(state) {
  // state: "connected" | "reconnecting"
  connStatusEl.classList.remove("connected", "reconnecting");
  connStatusEl.classList.add(state);
  connStatusLabelEl.textContent = state === "connected" ? "live" : "reconnecting…";
}

function showToast(text) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = text;
  toastStackEl.appendChild(el);
  setTimeout(() => el.remove(), 6000);
  // Cap visible toasts so a burst of events doesn't fill the screen.
  while (toastStackEl.children.length > 4) toastStackEl.removeChild(toastStackEl.firstChild);
}

// ── Live push channel: voice navigation, JARVIS state, notifications ────
let wsReconnectDelay = 1000;
function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  // Browsers can't set custom headers on a WebSocket handshake, so the
  // session token travels as a query param here instead — checked
  // server-side before the connection is accepted (dashboard/server.py).
  const ws = new WebSocket(`${proto}://${location.host}/3d/ws?token=${encodeURIComponent(_authToken)}`);

  ws.onopen = () => {
    setConnStatus("connected");
    wsReconnectDelay = 1000;   // reset backoff on a successful connect
  };
  ws.onmessage = (evt) => {
    let msg;
    try { msg = JSON.parse(evt.data); } catch { return; }
    if (msg.type === "navigate") {
      if (msg.action === "home") goHome({ fromServer: true });
      else if (msg.action === "back") goBack({ fromServer: true });
      else if (msg.nucleus_id && msg.nucleus_id !== currentNucleusId) {
        focusNucleus(msg.nucleus_id, { fromServer: true });
      }
    } else if (msg.type === "jarvis_state") {
      setOrbState(msg.state);
    } else if (msg.type === "notification") {
      showToast(msg.text || "");
    }
  };
  ws.onclose = () => {
    setConnStatus("reconnecting");
    setTimeout(connectWS, wsReconnectDelay);
    wsReconnectDelay = Math.min(wsReconnectDelay * 1.6, 15000);   // backoff, capped at 15s
  };
  ws.onerror = () => ws.close();
}

// ── Raycasting: hover + click on nuclei ─────────────────────────────────
const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();
let hovered = null;

function onPointerMove(e) {
  const rect = canvas.getBoundingClientRect();
  pointer.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
  pointer.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);
  const targets = [orbMesh, ...rootGroup.children, ...childGroup.children].filter(m => m.userData?.kind === "core" || m.userData?.kind === "nucleus");
  const hits = raycaster.intersectObjects(targets, false);
  const next = hits[0]?.object || null;
  if (hovered && hovered !== next) hovered.scale.set(1, 1, 1);
  if (next) { next.scale.set(1.15, 1.15, 1.15); canvas.style.cursor = "pointer"; }
  else { canvas.style.cursor = "default"; }
  hovered = next;
}

function onPointerClick() {
  if (!hovered) return;
  const { kind, id } = hovered.userData;
  if (kind === "core") goHome();
  else if (kind === "nucleus") focusNucleus(id);
}

// ── Resize + animate ─────────────────────────────────────────────────────
function onResize() {
  const w = stageEl.clientWidth, h = stageEl.clientHeight;
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  renderer.setSize(w, h);
}

function animate() {
  requestAnimationFrame(animate);
  const t = clock.getElapsedTime();
  const speed = STATE_PULSE_SPEED[currentOrbState] ?? 1;
  const pulse = 1 + Math.sin(t * speed) * 0.06;
  orbMesh.scale.setScalar(pulse);
  orbMesh.rotation.y += 0.0025 * (currentOrbState === "thinking" ? 3 : 1);
  orbLight.intensity = 2.6 + Math.sin(t * speed) * 0.8;
  starField.rotation.y += 0.00006;
  updateTween();
  controls.update();
  renderer.render(scene, camera);
}

// ── Boot ──────────────────────────────────────────────────────────────
async function boot() {
  if (!initThree()) return;
  canvas.addEventListener("pointermove", onPointerMove);
  canvas.addEventListener("click", onPointerClick);
  btnHome.addEventListener("click", () => goHome());
  btnBack.addEventListener("click", () => goBack());
  hudToggleEl.addEventListener("click", () => {
    const open = hudExpandEl.classList.toggle("open");
    hudToggleEl.classList.toggle("open", open);
    hudToggleEl.setAttribute("aria-expanded", String(open));
  });

  try {
    const res = await _authFetch("/3d/api/overview");
    const payload = await res.json();
    buildRootRing(payload.hierarchy);
    panelStatusEl.textContent = payload.summary?.status || "Ready for navigation";
    renderOverviewPanel(payload);
  } catch (e) {
    console.error("[3D] overview load failed", e);
    panelStatusEl.textContent = "Could not reach the JARVIS dashboard backend.";
  }

  connectWS();
  animate();
}

boot();
