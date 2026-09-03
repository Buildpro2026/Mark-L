// JARVIS 3D Command Center — real WebGL/Three.js spatial scene.
//
// Reuses the existing backend as the source of truth:
//   GET  /3d/api/overview            → root hierarchy + module summaries
//   GET  /3d/api/module/{id}         → per-Nucleus live data + children/path
//   GET  /3d/api/approvals           → real PENDING_APPROVAL tasks (AgentOrchestrator)
//   GET  /3d/api/activity            → real recent activity (status_api.activity())
//   POST /3d/api/command             → {action:"navigate"|"chat"|"speak"|"approve_task"|"reject_task"|"system_status", ...}
//                                       "chat" is the real run_chat_turn() bridge — see dashboard/server.py's _handle_3d_chat
//   POST /api/command                → general JARVIS text/voice command relay (paired sessions only)
//   WS   /3d/ws                      → {type:"navigate"} / {type:"jarvis_state", state, label} / {type:"activity", ...} / {type:"notification", text}
//   WS   /ws/phone-audio             → live mic PCM16 → Gemini Live (same channel app.html uses)
//
// Nothing here invents backend data — every field rendered in the right
// panel comes straight from a /3d/api/* response; a Nucleus with no live
// data source (CareerRocket today) is rendered as an explicitly-labeled
// "not connected" placeholder, never fabricated content.

import * as THREE from "three";
import { OrbitControls } from "/3d/assets/vendor/OrbitControls.js";

// ── Auth — same three credentials dashboard/server.py's _3d_auth accepts:
// JARVIS_API_TOKEN, the desktop pairing-key/PIN session, or the /ui cookie.
// login.html/app.html (the existing phone dashboard) already write the
// pairing-key token to sessionStorage — this reuses that exact key/storage
// instead of a localStorage key nothing ever set. A visitor with only the
// /ui cookie (no pairing-key session) has no token here at all, and that's
// fine: the cookie travels automatically on same-origin fetch/WS, so
// _authFetch simply omits the header rather than sending an empty Bearer.
const _authToken = sessionStorage.getItem("jarvis_token") || "";
function _authFetch(url, opts = {}) {
  if (_authToken) {
    opts.headers = Object.assign({}, opts.headers, { Authorization: `Bearer ${_authToken}` });
  }
  return fetch(url, opts);
}

const REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// ── DOM refs ────────────────────────────────────────────────────────────
const canvas        = document.getElementById("three-canvas");
const noWebglEl      = document.getElementById("no-webgl");
const stateDotEl     = document.getElementById("state-dot");
const stateLabelEl   = document.getElementById("state-label");
const panelTitleEl   = document.getElementById("panel-title");
const panelStatusEl  = document.getElementById("panel-status");
const panelDetailsEl = document.getElementById("panel-details");
const panelChildrenEl = document.getElementById("panel-children");
const statGridMount  = document.getElementById("stat-grid-mount");
const gaugeMount     = document.getElementById("gauge-mount");
const btnHome        = document.getElementById("btn-home");
const btnBack        = document.getElementById("btn-back");
const stageEl        = document.getElementById("stage");
const connStatusEl   = document.getElementById("conn-status");
const connStatusLabelEl = document.getElementById("conn-status-label");
const toastStackEl   = document.getElementById("toast-stack");
const breadcrumbEl   = document.getElementById("breadcrumb");
const nucleusListEl  = document.getElementById("nucleus-list");
const activityFeedEl = document.getElementById("activity-feed");
const activityClearEl = document.getElementById("activity-clear");
const objectiveAmountEl = document.getElementById("objective-amount");
const objectiveBarFillEl = document.getElementById("objective-bar-fill");
const approvalsBadgeEl = document.getElementById("approvals-badge");
const approvalsCountEl = document.getElementById("approvals-count");
const shellEl        = document.getElementById("shell");
const railToggleEl   = document.getElementById("rail-toggle");
const infoPanelToggleEl = document.getElementById("info-panel-toggle");
const filesSearchSection = document.getElementById("files-search-section");
const filesSearchInput = document.getElementById("files-search-input");
const filesSearchBtn = document.getElementById("files-search-btn");
const knowledgeSearchSection = document.getElementById("knowledge-search-section");
const knowledgeSearchInput = document.getElementById("knowledge-search-input");
const knowledgeSearchBtn = document.getElementById("knowledge-search-btn");
const knowledgeListBtn = document.getElementById("knowledge-list-btn");
const dockInput      = document.getElementById("dock-input");
const dockSend       = document.getElementById("dock-send");
const dockMic        = document.getElementById("dock-mic");
const dockReplyEl    = document.getElementById("dock-reply");
const dockSpeakToggleEl = document.getElementById("dock-speak-toggle");
const approvalsOverlayEl = document.getElementById("approvals-overlay");
const approvalsListEl = document.getElementById("approvals-list");
const approvalsCloseEl = document.getElementById("approvals-close");

// ── Layout constants ────────────────────────────────────────────────────
const ROOT_RADIUS  = 7.5;
const CHILD_RADIUS = 2.6;
const ORB_RADIUS   = 1.1;
const NODE_RADIUS  = 0.55;
const CHILD_NODE_RADIUS = 0.34;

// Orb states — mirrors dashboard/server.py's _handle_3d_chat broadcasts
// (jarvis_state events) plus the pre-existing voice states. Every state
// here is driven by a real backend event; none is decorative.
const STATE_COLORS = {
  idle:        0x4b6b7c,
  listening:   0x4fd6ff,
  thinking:    0xb98bff,
  executing:   0xffb454,
  speaking:    0x5cffc4,
  waiting_for_approval: 0xffe066,
  success:     0x5cffc4,
  warning:     0xffb454,
  error:       0xff4d4d,
  offline:     0x555b66,
  interrupted: 0xff6b7a,
};
const STATE_PULSE_SPEED = {
  idle: 0.6, listening: 1.2, thinking: 2.4, executing: 2.8, speaking: 2.0,
  waiting_for_approval: 1.5, success: 1.0, warning: 1.4, error: 3.2, offline: 0.15,
  interrupted: 6.0,
};
// States that are a momentary notification rather than an ongoing mode —
// auto-revert to idle after a beat so the orb doesn't get stuck showing
// "success"/"error" forever after a turn finishes.
const TRANSIENT_STATE_MS = { success: 2200, error: 3400, warning: 2800 };

const NUCLEUS_COLORS = {
  buildpro: 0x4fd6ff, ddf: 0x5cffc4, careerrocket: 0xffb454,
  email: 0x8fb8ff, calendar: 0xff8fd1, knowledge: 0xf5e6a8, files: 0xb98bff,
  reports: 0x9fe6ff, communications: 0x7d8fa6, system: 0xff6b7a,
  personal: 0x8fa8b8, hubspot: 0xff7ab8, social: 0x66d9ef,
  company_core: 0xffe66d, // 2026-09-03: Company Core Planet (Section 18)
};

// Display order for the left rail — presentation only; every id below is a
// real actions/nucleus_hierarchy.py node (or, for "agents", a real shortcut
// into the System Nucleus's already-fetched agent_orchestrator data — there
// is no separate Agents endpoint, so this never invents one). "knowledge" is
// the real Obsidian JARVIS Brain vault — distinct from "files" below, which
// is a general filesystem search, not vault-aware. "company_core" added
// 2026-09-03 (Section 18) — real infrastructure-platform health, not a
// second copy of System's health check (see dashboard/server.py's
// _module_company_core, which reuses _integration_health()).
const RAIL_ORDER = [
  "company_core", "buildpro", "ddf", "careerrocket", "email", "calendar", "hubspot", "social",
  "knowledge", "files", "reports", "communications", "system", "personal",
];

// ── Three.js setup ──────────────────────────────────────────────────────
let renderer, scene, camera, controls, clock;
let orbMesh, orbLight, orbGlow, orbRim, starField;
const rootGroup = new THREE.Group();
const childGroup = new THREE.Group();
const lineGroup = new THREE.Group();      // root nuclei's orbit connectors — geometry updated in place each frame, never cleared by navigation
const childLineGroup = new THREE.Group(); // rebuilt every showChildrenFor() call, same lifecycle as childGroup

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
  renderer.setClearColor(0x03070d, 1);

  scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(0x03070d, 0.026);

  camera = new THREE.PerspectiveCamera(52, stageEl.clientWidth / stageEl.clientHeight, 0.1, 500);
  camera.position.set(0, 6.5, 17);

  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.minDistance = 4;
  controls.maxDistance = 40;
  controls.maxPolarAngle = Math.PI * 0.92;
  controls.target.set(0, 0, 0);

  // Executive lighting: a cool ambient fill plus a brighter key light and a
  // dim rim light from the opposite side, so the orb reads as a lit sphere
  // with real depth rather than a flat glowing disc.
  scene.add(new THREE.AmbientLight(0x2e4256, 0.9));
  const key = new THREE.PointLight(0x9fe6ff, 2.6, 60);
  key.position.set(10, 12, 8);
  scene.add(key);
  const rim = new THREE.PointLight(0x4a3a6e, 1.1, 50);
  rim.position.set(-12, -4, -10);
  scene.add(rim);

  scene.add(rootGroup, childGroup, lineGroup, childLineGroup);

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
  const mat = new THREE.PointsMaterial({ color: 0x6fa8c9, size: 0.32, transparent: true, opacity: 0.5 });
  starField = new THREE.Points(geo, mat);
  scene.add(starField);
}

function createOrb() {
  // True smooth sphere (not a faceted icosahedron) — the "sun" of the
  // spatial solar system, with the Nucleus planets orbiting it. A CEO-facing
  // focal point, not a game asset: restrained material, no cartoon shading.
  const geo = new THREE.SphereGeometry(ORB_RADIUS, 64, 40);
  const mat = new THREE.MeshStandardMaterial({
    color: STATE_COLORS.idle, emissive: STATE_COLORS.idle, emissiveIntensity: 0.85,
    roughness: 0.3, metalness: 0.25, flatShading: false,
  });
  orbMesh = new THREE.Mesh(geo, mat);
  orbMesh.userData = { kind: "core", id: "jarvis", name: "Jarvis" };
  scene.add(orbMesh);

  orbGlow = makeGlowSprite(STATE_COLORS.idle, ORB_RADIUS * 5);
  scene.add(orbGlow);

  // Faint outer rim sprite — extra depth/polish, additive so it never
  // competes with the primary glow's color.
  orbRim = makeGlowSprite(0x1a2a3a, ORB_RADIUS * 7.5);
  orbRim.material.opacity = 0.35;
  scene.add(orbRim);

  orbLight = new THREE.PointLight(STATE_COLORS.idle, 3.4, 14);
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

// Evenly-spaced points on a sphere (golden-angle/Fibonacci-sphere method) —
// nuclei genuinely surround the orb in 3D instead of sitting on one flat
// plane. Returns unit-length direction vectors (not yet scaled by radius)
// so callers can animate radius/rotation independently — see updateOrbits().
const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));
function sphereDirections(count) {
  if (count <= 0) return [];
  if (count === 1) return [new THREE.Vector3(0, 0, 1)];
  const out = [];
  for (let i = 0; i < count; i++) {
    const y = 1 - (i / (count - 1)) * 2;          // 1 → -1
    const r = Math.sqrt(Math.max(0, 1 - y * y));
    const theta = GOLDEN_ANGLE * i;
    out.push(new THREE.Vector3(Math.cos(theta) * r, y, Math.sin(theta) * r));
  }
  return out;
}

// Cheap fake-bloom: an additive-blended radial-gradient sprite behind a
// mesh reads as a glow without a real postprocessing bloom pass (which
// would need EffectComposer/RenderPass/UnrealBloomPass vendored — more
// weight than this scene needs for the effect it buys).
let _glowTexture = null;
function _getGlowTexture() {
  if (_glowTexture) return _glowTexture;
  const size = 128;
  const canvasEl = document.createElement("canvas");
  canvasEl.width = size; canvasEl.height = size;
  const ctx = canvasEl.getContext("2d");
  const grad = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  grad.addColorStop(0, "rgba(255,255,255,0.8)");
  grad.addColorStop(0.4, "rgba(255,255,255,0.33)");
  grad.addColorStop(1, "rgba(255,255,255,0)");
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, size, size);
  _glowTexture = new THREE.CanvasTexture(canvasEl);
  return _glowTexture;
}
function makeGlowSprite(hexColor, scale = 1) {
  const mat = new THREE.SpriteMaterial({
    map: _getGlowTexture(), color: hexColor, transparent: true,
    depthWrite: false, blending: THREE.AdditiveBlending,
  });
  const sprite = new THREE.Sprite(mat);
  sprite.scale.set(scale, scale, 1);
  return sprite;
}

// Root nuclei slowly revolve around the orb like electrons around a
// nucleus — each mesh keeps its fixed sphereDirections() slot but that
// slot itself rotates around Y over time. Recomputing the (small) set of
// connector lines each frame is cheap at this node count (< 10). Disabled
// under prefers-reduced-motion — nuclei stay at their initial positions.
const ORBIT_SPEED_ROOT = 0.045; // radians/sec — a full revolution takes ~2.3 minutes
function updateOrbits(t) {
  if (REDUCED_MOTION) return;
  for (const mesh of rootGroup.children) {
    const orbit = mesh.userData.orbit;
    if (!orbit) continue;
    if (expandedRootId && mesh.userData.id === expandedRootId) continue; // frozen — see expandedRootId comment above
    const angle = t * ORBIT_SPEED_ROOT + orbit.phase;
    const cos = Math.cos(angle), sin = Math.sin(angle);
    const x = orbit.dir.x * cos - orbit.dir.z * sin;
    const z = orbit.dir.x * sin + orbit.dir.z * cos;
    mesh.position.set(x * orbit.radius, orbit.dir.y * orbit.radius, z * orbit.radius);
    if (orbit.line) {
      orbit.line.geometry.dispose();
      orbit.line.geometry = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(0, 0, 0), mesh.position]);
    }
    if (orbit.glow) orbit.glow.position.copy(mesh.position);
  }
}

// ── Nucleus meshes ──────────────────────────────────────────────────────
function makeNucleusMesh(node, radius, color, placeholder = false) {
  const geo = new THREE.SphereGeometry(radius, 28, 18);
  const mat = new THREE.MeshStandardMaterial({
    color, emissive: color, emissiveIntensity: placeholder ? 0.22 : 0.5,
    roughness: 0.4, metalness: 0.18, wireframe: placeholder,
    transparent: placeholder, opacity: placeholder ? 0.5 : 1,
  });
  const mesh = new THREE.Mesh(geo, mat);
  mesh.userData = { kind: "nucleus", id: node.id, name: node.name, node, placeholder, baseEmissive: placeholder ? 0.22 : 0.5 };
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

function drawConnection(fromPos, toPos, color = 0x2f5b6e, group = lineGroup) {
  const geo = new THREE.BufferGeometry().setFromPoints([fromPos, toPos]);
  const mat = new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.5 });
  const line = new THREE.Line(geo, mat);
  group.add(line);
  return line;
}

let hierarchyRoot = null;
const rootMeshes = new Map();     // id -> mesh (root sphere)
let childMeshes = new Map();      // id -> mesh (currently expanded children)
let infoObjects = [];             // spawned data objects (files/deals/etc.)

// 2026-09-03 fix (Lee's autonomous-CEO spec, Section 19): the id of the
// root nucleus whose children are currently shown, or null. showChildrenFor()
// bakes each child mesh's position and every connector line in
// childLineGroup from a ONE-TIME SNAPSHOT of the parent's world position
// (see showChildrenFor below) — but updateOrbits() keeps moving every root
// nucleus, including the one currently expanded, every single frame with
// no exception. The parent sphere kept visibly drifting away from its own
// (frozen) children and connector lines while its Nucleus was open, and
// worse the longer you stayed on it — exactly the "connector lines
// detach/float" symptom, most noticeable after Planet->Star->back->another
// Star navigation because by then the drift had had time to accumulate.
// Fix: freeze (skip re-positioning) the currently-expanded root nucleus in
// updateOrbits() so it stays exactly where its children/connectors were
// drawn relative to; every OTHER root nucleus keeps orbiting normally in
// the background. Cleared on goHome()/focusing a different nucleus.
let expandedRootId = null;

function buildRootRing(hierarchy) {
  hierarchyRoot = hierarchy;
  clearGroup(rootGroup);
  clearGroup(lineGroup);
  rootMeshes.clear();
  expandedRootId = null;
  const children = (hierarchy?.children || []).filter(c => c.id !== "jarvis");
  const dirs = sphereDirections(children.length);
  children.forEach((node, i) => {
    const color = NUCLEUS_COLORS[node.id] ?? 0x8fa8b8;
    const mesh = makeNucleusMesh(node, NODE_RADIUS, color);
    const dir = dirs[i];
    mesh.position.set(dir.x * ROOT_RADIUS, dir.y * ROOT_RADIUS, dir.z * ROOT_RADIUS);
    const glow = makeGlowSprite(color, NODE_RADIUS * 4.2);
    glow.position.copy(mesh.position);
    rootGroup.add(glow);
    const line = drawConnection(new THREE.Vector3(0, 0, 0), mesh.position, color);
    mesh.userData.orbit = { dir, radius: ROOT_RADIUS, phase: (i / Math.max(1, children.length)) * Math.PI * 2, line, glow };
    rootGroup.add(mesh);
    rootMeshes.set(node.id, mesh);
  });
  buildRailList(children);
}

function showChildrenFor(nucleusId, parentPos, children) {
  clearGroup(childGroup);
  clearGroup(childLineGroup);
  childMeshes = new Map();
  clearInfoObjects();
  if (!children || !children.length) return;
  const dirs = sphereDirections(children.length);
  const parentColor = NUCLEUS_COLORS[nucleusId] ?? 0x8fa8b8;
  children.forEach((child, i) => {
    const placeholder = !!child.placeholder;
    const mesh = makeNucleusMesh(child, CHILD_NODE_RADIUS, placeholder ? 0x5a6a78 : parentColor, placeholder);
    const dir = dirs[i];
    mesh.position.set(
      parentPos.x + dir.x * CHILD_RADIUS,
      parentPos.y + dir.y * CHILD_RADIUS,
      parentPos.z + dir.z * CHILD_RADIUS
    );
    childGroup.add(mesh);
    childMeshes.set(child.id, mesh);
    drawConnection(parentPos, mesh.position, parentColor, childLineGroup);
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
  const capped = items.slice(0, 8);
  const dirs = sphereDirections(capped.length);
  const radius = CHILD_RADIUS + 1.4;
  capped.forEach((item, i) => {
    const geo = new THREE.BoxGeometry(0.3, 0.3, 0.3);
    const mat = new THREE.MeshStandardMaterial({ color: 0xffffff, emissive: 0x224455, emissiveIntensity: 0.4, roughness: 0.5 });
    const mesh = new THREE.Mesh(geo, mat);
    const dir = dirs[i];
    mesh.position.set(centerPos.x + dir.x * radius, centerPos.y + 1.6 + dir.y * radius, centerPos.z + dir.z * radius);
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
  if (REDUCED_MOTION) {
    // Jump directly instead of tweening — no motion, just the end state.
    const dir = camera.position.clone().sub(controls.target).normalize();
    if (!isFinite(dir.x)) dir.set(0, 0.35, 1);
    camera.position.copy(lookAt.clone().add(dir.multiplyScalar(distance)));
    controls.target.copy(lookAt);
    tween = null;
    return;
  }
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
let _transientRevertTimer = null;

function setOrbState(state, opts = {}) {
  const now = performance.now();
  if (currentOrbState === "interrupted" && now < stateHoldUntil && !opts.force) return;
  currentOrbState = STATE_COLORS[state] ? state : "idle";
  if (currentOrbState === "interrupted") stateHoldUntil = now + 550;
  const color = STATE_COLORS[currentOrbState];
  orbMesh.material.color.setHex(color);
  orbMesh.material.emissive.setHex(color);
  orbLight.color.setHex(color);
  orbGlow.material.color.setHex(color);
  const hex = `#${color.toString(16).padStart(6, "0")}`;
  stateDotEl.style.background = hex;
  stateDotEl.style.boxShadow = `0 0 10px ${hex}`;
  stateLabelEl.textContent = opts.label || currentOrbState.replace(/_/g, " ");

  // "success"/"error"/"warning" are momentary — revert to idle after a
  // beat rather than sticking forever once the real event that caused
  // them has passed.
  if (_transientRevertTimer) { clearTimeout(_transientRevertTimer); _transientRevertTimer = null; }
  const revertMs = TRANSIENT_STATE_MS[currentOrbState];
  if (revertMs) {
    const stateAtSchedule = currentOrbState;
    _transientRevertTimer = setTimeout(() => {
      if (currentOrbState === stateAtSchedule) setOrbState("idle");
    }, revertMs);
  }
}

// ── Navigation state (mirrors dashboard/server.py's apply_navigation) ──
let currentNucleusId = "jarvis";
let backStack = [];
let currentModuleData = null;

function findRootNode(id) {
  return (hierarchyRoot?.children || []).find(c => c.id === id) || null;
}

async function fetchModule(id, query = "", note = "") {
  const params = new URLSearchParams();
  if (query) params.set("query", query);
  if (note) params.set("note", note);
  const qs = params.toString() ? `?${params.toString()}` : "";
  const res = await _authFetch(`/3d/api/module/${encodeURIComponent(id)}${qs}`);
  if (res.status === 401) { _redirectToLogin(); throw new Error("unauthorized"); }
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
  expandedRootId = id;  // freeze this nucleus's orbit — see the comment by its declaration

  panelTitleEl.textContent = node.name;
  panelStatusEl.textContent = "Loading…";
  updateBreadcrumb(["Jarvis", node.name]);
  updateRailActive(id);
  flyTo(mesh.position.clone(), 5.5);

  try {
    const payload = await fetchModule(id);
    currentModuleData = payload;
    const data = payload.data || {};
    renderInfoPanel(id, node, data);
    showChildrenFor(id, mesh.position, data.children || []);
    spawnDataObjects(id, data, mesh.position);
  } catch (e) {
    if (e.message !== "unauthorized") {
      console.error("[3D] module fetch error", e);
      panelStatusEl.textContent = "This Nucleus's data couldn't be loaded right now.";
    }
  }

  if (!fromServer) postNavigate("open", id);
  logActivity(`Opened ${node.name}`);
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
  expandedRootId = null;  // resume normal orbiting for every root nucleus
  backStack = [];
  panelTitleEl.textContent = "Jarvis";
  updateBreadcrumb(["Jarvis"]);
  updateRailActive(null);
  clearGroup(childGroup);
  childMeshes = new Map();
  infoObjects = [];
  flyTo(new THREE.Vector3(0, 0, 0), 17);
  filesSearchSection.style.display = "none";
  knowledgeSearchSection.style.display = "none";
  statGridMount.innerHTML = "";
  gaugeMount.innerHTML = "";
  try {
    const res = await _authFetch("/3d/api/overview");
    if (res.status === 401) return _redirectToLogin();
    const payload = await res.json();
    panelStatusEl.textContent = payload.summary?.status || "Ready for navigation";
    renderOverviewPanel(payload);
    renderRevenueProgress(payload.strategic_objective);
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

// ── Left rail: Nucleus list + active highlight ──────────────────────────
function buildRailList(children) {
  const byId = new Map(children.map(c => [c.id, c]));
  const ordered = [
    ...RAIL_ORDER.map(id => byId.get(id)).filter(Boolean),
    ...children.filter(c => !RAIL_ORDER.includes(c.id)),
  ];
  nucleusListEl.innerHTML = ordered.map(node => {
    const color = NUCLEUS_COLORS[node.id] ?? 0x8fa8b8;
    const hex = `#${color.toString(16).padStart(6, "0")}`;
    return `<li class="rail-item" data-id="${escapeHtml(node.id)}"><span class="swatch" style="background:${hex}"></span>${escapeHtml(node.name)}</li>`;
  }).join("") + `<li class="rail-item" data-id="__agents"><span class="swatch" style="background:#ff6b7a"></span>Agents<span class="tag">in System</span></li>`;

  nucleusListEl.querySelectorAll(".rail-item").forEach(el => {
    el.addEventListener("click", () => {
      const id = el.dataset.id;
      if (id === "__agents") { focusNucleus("system"); return; }
      focusNucleus(id);
    });
  });
}

function updateRailActive(id) {
  nucleusListEl.querySelectorAll(".rail-item").forEach(el => {
    el.classList.toggle("active", el.dataset.id === id || (id === "system" && el.dataset.id === "__agents"));
  });
}

// ── Activity feed — real WS events only, capped so it stays a feed, not a log dump ──
const ACTIVITY_CAP = 50;
function logActivity(text, opts = {}) {
  if (activityFeedEl.querySelector(".activity-empty")) activityFeedEl.innerHTML = "";
  const li = document.createElement("li");
  if (opts.priority) li.classList.add("evt-priority");
  else if (opts.notification) li.classList.add("evt-notification");
  const time = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  li.innerHTML = `<span class="t">${time}</span>${escapeHtml(text)}`;
  activityFeedEl.appendChild(li);
  while (activityFeedEl.children.length > ACTIVITY_CAP) activityFeedEl.removeChild(activityFeedEl.firstChild);
}
activityClearEl.addEventListener("click", () => {
  activityFeedEl.innerHTML = `<li class="activity-empty">No activity yet.</li>`;
});
activityFeedEl.innerHTML = `<li class="activity-empty">No activity yet.</li>`;

// ── $1M strategic objective — top bar, straight from GET /3d/api/overview's
// strategic_objective field (actions/strategic_objective.py). ────────────
function renderRevenueProgress(objective) {
  if (!objective || typeof objective.progress_pct === "undefined") {
    objectiveAmountEl.textContent = "—";
    objectiveBarFillEl.style.width = "0%";
    return;
  }
  const pct = Math.max(0, Math.min(100, objective.progress_pct || 0));
  const cum = Math.round(objective.cumulative_revenue_usd || 0);
  const target = Math.round(objective.target_amount_usd || 0);
  objectiveAmountEl.textContent = `$${cum.toLocaleString()} / $${target.toLocaleString()} (${pct}%)`;
  objectiveBarFillEl.style.width = `${pct}%`;
}

// ── Pending agent approvals — top bar badge, polled from the already-real
// /3d/api/module/system endpoint's agents.pending_approval_count field. ──
async function refreshApprovalsBadge() {
  try {
    const payload = await fetchModule("system");
    const n = payload?.data?.agents?.pending_approval_count || 0;
    approvalsCountEl.textContent = String(n);
    approvalsBadgeEl.classList.toggle("has-pending", n > 0);
  } catch (_) { /* best-effort — never blocks the rest of the UI */ }
}

// ── Approval center — real AgentOrchestrator PENDING_APPROVAL tasks via
// GET /3d/api/approvals, approve/deny via POST /3d/api/command. 'reason' is
// the task's real description, 'risk' the agent's real permission_level —
// nothing here is invented (see dashboard/server.py's /3d/api/approvals). ──
async function openApprovalsPanel() {
  approvalsOverlayEl.hidden = false;
  approvalsListEl.innerHTML = `<div class="info-empty">Loading…</div>`;
  try {
    const res = await _authFetch("/3d/api/approvals");
    if (res.status === 401) { _redirectToLogin(); return; }
    const payload = await res.json();
    renderApprovalsList(payload.approvals || []);
  } catch (e) {
    approvalsListEl.innerHTML = `<div class="info-empty">Couldn't load approvals — check the connection.</div>`;
  }
}

function renderApprovalsList(approvals) {
  if (!approvals.length) {
    approvalsListEl.innerHTML = `<div class="info-empty">No tasks awaiting approval.</div>`;
    return;
  }
  approvalsListEl.innerHTML = approvals.map(a => `
    <div class="approval-card">
      <div class="approval-row"><span class="k">Action</span>: ${escapeHtml(a.description || "")}</div>
      <div class="approval-row"><span class="k">Agent</span>: ${escapeHtml(a.agent_name || a.agent_id || "")}</div>
      <div class="approval-row"><span class="k">System</span>: ${escapeHtml(a.system || "")}</div>
      <div class="approval-row"><span class="k">Risk</span>: ${escapeHtml(a.risk || "unknown")}</div>
      <div class="approval-row"><span class="k">Requested</span>: ${a.created_ts ? new Date(a.created_ts * 1000).toLocaleString() : "?"}</div>
      <div class="approval-actions">
        <button class="approve-btn" type="button" data-action="approve" data-task-id="${escapeHtml(a.id)}">Approve</button>
        <button class="deny-btn" type="button" data-action="deny" data-task-id="${escapeHtml(a.id)}">Deny</button>
      </div>
    </div>
  `).join("");
}

approvalsListEl.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-task-id]");
  if (!btn) return;
  const taskId = btn.dataset.taskId;
  const action = btn.dataset.action === "approve" ? "approve_task" : "reject_task";
  btn.closest(".approval-card")?.querySelectorAll("button").forEach(b => b.disabled = true);
  try {
    const res = await _authFetch("/3d/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, task_id: taskId }),
    });
    const payload = await res.json();
    if (!payload.ok) {
      showToast(payload.error || "That action failed.");
    } else {
      showToast(action === "approve_task" ? "Task approved." : "Task denied.");
    }
  } catch (e) {
    showToast("Approval action failed — check the connection.");
  }
  openApprovalsPanel();
  refreshApprovalsBadge();
});

approvalsCloseEl.addEventListener("click", () => { approvalsOverlayEl.hidden = true; });
approvalsOverlayEl.addEventListener("click", (e) => { if (e.target === approvalsOverlayEl) approvalsOverlayEl.hidden = true; });
approvalsBadgeEl.addEventListener("click", openApprovalsPanel);

// ── Right-side info panel rendering (data straight from the API, no invented content) ──
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

function statCard(value, label) {
  return `<div class="stat-card"><div class="v">${escapeHtml(value)}</div><div class="l">${escapeHtml(label)}</div></div>`;
}

function gaugeRow(label, value) {
  const v = Math.max(0, Math.min(100, Number(value) || 0));
  const warn = v >= 85 ? " warn" : "";
  return `<div class="gauge"><div class="gauge-row"><span>${escapeHtml(label)}</span><span>${v}%</span></div><div class="gauge-track"><div class="gauge-fill${warn}" style="width:${v}%"></div></div></div>`;
}

function renderInfoPanel(id, node, data) {
  panelDetailsEl.innerHTML = "";
  panelChildrenEl.innerHTML = "";
  statGridMount.innerHTML = "";
  gaugeMount.innerHTML = "";
  filesSearchSection.style.display = (id === "files" || id === "personal-files" || id === "personal-documents") ? "" : "none";
  knowledgeSearchSection.style.display = id === "knowledge" ? "" : "none";

  const details = [];
  if (id === "ddf" && Array.isArray(data.top_products)) {
    panelStatusEl.textContent = data.summary || "Daily Deal Finder data";
    if (!data.top_products.length) details.push(item("No active deals right now"));
    for (const p of data.top_products) details.push(item(`<span class="k">${escapeHtml(p.name || p.title || "Deal")}</span> — ${escapeHtml(p.price ?? p.score ?? "tracked")}`));
    pushBusinessIntelSection(details, data);
  } else if (id === "buildpro") {
    panelStatusEl.textContent = data.summary || "BuildPro Recruiting — live pipeline";
    const rec = data.buildpro_recruiting;
    if (rec) {
      statGridMount.innerHTML = `<div class="stat-grid">
        ${statCard(rec.candidate_count ?? 0, "Candidates")}
        ${statCard(rec.client_count ?? 0, "Clients")}
        ${statCard(rec.active_jobs ?? 0, "Active jobs")}
        ${statCard(rec.qualified_matches ?? 0, "Qualified matches")}
      </div>`;
      for (const m of (rec.highest_match_scores || [])) {
        details.push(item(`<span class="k">${escapeHtml(m.candidate_name || "Candidate")}</span> → ${escapeHtml(m.job_title || "Job")} — ${escapeHtml(m.match_score ?? "?")}`));
      }
    }
    const fu = data.buildpro_followups;
    if (fu) {
      const cCount = (fu.candidates || []).length, clCount = (fu.clients || []).length;
      if (cCount || clCount) details.push(item(`<span class="k">Follow-ups due</span>: ${cCount} candidate(s), ${clCount} client(s)`));
    }
    pushBusinessIntelSection(details, data);
  } else if (id === "careerrocket") {
    // No live data source wired up yet (dashboard/server.py's _module_data
    // has no case for "careerrocket") — stay honest instead of inventing content.
    panelStatusEl.textContent = "Not connected yet";
    panelDetailsEl.innerHTML = `<div class="unavailable-card"><span class="tag">No live data source</span><br/>${escapeHtml(data.summary || "CareerRocket Pro has no connected data source yet.")}</div>`;
    panelChildrenEl.innerHTML = (data.children || []).length
      ? data.children.map(c => item(c.placeholder ? `${escapeHtml(c.name)} — coming soon` : escapeHtml(c.name), !!c.placeholder)).join("")
      : `<div class="info-empty">No sub-branches.</div>`;
    return;
  } else if (id === "knowledge") {
    // JARVIS Brain — every field below came straight from ObsidianVault
    // (list_notes/search_notes/read_note via _module_knowledge()); nothing
    // here is fabricated, including the empty/not-found/unconfigured cases.
    if (data.note) {
      panelStatusEl.textContent = data.note.found ? "Reading from the JARVIS Brain" : "Not found in the vault";
      panelDetailsEl.innerHTML = data.note.found
        ? `<div class="brain-note">
            <div class="brain-note-back" data-brain-back="1">‹ Back to Brain list</div>
            <div class="path">${escapeHtml(data.note.path)}</div>
            <pre>${escapeHtml(data.note.content || "")}</pre>
          </div>`
        : `<div class="unavailable-card"><span class="tag">Not found</span><br/>${escapeHtml(data.summary || "")}</div>`;
      panelChildrenEl.innerHTML = "";
      return;
    }
    panelStatusEl.textContent = data.summary || "JARVIS Brain";
    if (Array.isArray(data.results)) {
      if (!data.results.length) details.push(item("No notes match that search."));
      for (const r of data.results) {
        details.push(brainItem(r.path, r.snippet ? ` — …${escapeHtml(r.snippet)}…` : ""));
      }
    } else {
      const notes = Array.isArray(data.notes) ? data.notes : [];
      if (!notes.length) details.push(item(data.configured ? "The vault is empty." : "No JARVIS Brain vault configured.", !data.configured));
      for (const n of notes) details.push(brainItem(n));
    }
    panelChildrenEl.innerHTML = `<div class="info-empty">No sub-branches — browse notes above.</div>`;
    panelDetailsEl.innerHTML = details.join("");
    return;
  } else if (id === "files" || id === "personal-files" || id === "personal-documents") {
    panelStatusEl.textContent = "Live filesystem search + recent files";
    const results = Array.isArray(data.results) ? data.results : [];
    const recent = Array.isArray(data.recent_files) ? data.recent_files : [];
    if (!results.length && !recent.length) details.push(item("No file results yet — try a search above."));
    for (const f of results.slice(0, 20)) details.push(item(`<span class="k">Found</span> ${escapeHtml(typeof f === "string" ? f : f.name || f.path)}`));
    for (const f of recent.slice(0, 10)) details.push(item(`<span class="k">Recent</span> ${escapeHtml(typeof f === "string" ? f : f.name || f.path)}`));
  } else if (id === "reports") {
    panelStatusEl.textContent = data.summary || "System and business reports";
    const sys = data.system_status || {};
    for (const [k, v] of Object.entries(sys)) details.push(item(`<span class="k">${escapeHtml(k)}</span>: ${escapeHtml(v)}`));
    for (const f of (data.report_files || [])) details.push(item(`<span class="k">Report</span> ${escapeHtml(typeof f === "string" ? f : f.name)}`));
  } else if (id === "email" || id === "personal-email") {
    // 2026-09-03 fix (Lee's autonomous-CEO spec, Sections 4 & 12): this
    // used to say "Live content retrieval isn't wired into this view
    // yet" even after dashboard/server.py's _module_email() was already
    // fixed to return real messages — the backend had the data, this
    // view just never rendered it. Every row here is real: sender,
    // subject, the 7-category classification with WHY (category_reason),
    // and a genuine "OPEN SOURCE EMAIL" deep link into Gmail (never a
    // fabricated URL — shows "SOURCE UNAVAILABLE" honestly if the
    // backend couldn't build one for a given message).
    if (!data.configured) {
      panelStatusEl.textContent = "Not connected";
      details.push(item(data.note || "Not configured yet — connect via the standard JARVIS Google auth flow.", true));
    } else {
      const messages = Array.isArray(data.messages) ? data.messages : [];
      panelStatusEl.textContent = data.summary || `${messages.length} message(s)`;
      if (!messages.length) details.push(item(data.note || "No messages in view."));
      for (const m of messages.slice(0, 25)) {
        const openLink = m.permalink && m.permalink !== "SOURCE UNAVAILABLE"
          ? ` <a href="${escapeHtml(m.permalink)}" target="_blank" rel="noopener" class="email-open-link">OPEN SOURCE EMAIL</a>`
          : ` <span class="tag">SOURCE UNAVAILABLE</span>`;
        details.push(item(
          `<span class="k">${escapeHtml(m.category || m.classification || "uncategorized")}</span> ` +
          `${escapeHtml(m.subject || "(no subject)")} — ${escapeHtml(m.sender || "unknown sender")}` +
          `${m.unread ? " <span class=\"tag\">unread</span>" : ""}` +
          `${m.has_attachments ? " <span class=\"tag\">attachment</span>" : ""}` +
          openLink
        ));
      }
    }
  } else if (id === "calendar" || id === "personal-calendar") {
    // Connection status only — no live event retrieval wired up yet
    // (calendar_integration.py exists but this module never calls it;
    // a genuine remaining gap, not glossed over).
    panelStatusEl.textContent = data.configured ? "Connected" : "Not connected";
    details.push(item(data.status?.error || (data.configured ? "Calendar is authorized. Live event retrieval isn't wired into this view yet." : "Not configured yet — connect via the standard JARVIS Google auth flow."), !data.configured));
  } else if (id === "personal-contacts") {
    panelStatusEl.textContent = data.configured ? (data.summary || "Connected") : "Not connected";
    const contacts = Array.isArray(data.contacts) ? data.contacts : [];
    if (!contacts.length) details.push(item(data.note || "No contact/call history yet.", !data.configured));
    for (const c of contacts.slice(0, 20)) {
      const who = c.direction === "outbound" ? c.to_number : c.from_number;
      details.push(item(`${escapeHtml(c.direction || "")} ${escapeHtml(c.kind || "")} — ${escapeHtml(who || "unknown")} (${escapeHtml(c.status || "")})`));
    }
  } else if (id === "personal-tasks" || id === "personal-alerts") {
    panelStatusEl.textContent = "No live data source";
    panelDetailsEl.innerHTML = `<div class="unavailable-card"><span class="tag">No live data source</span><br/>${escapeHtml(data.note || "")}</div>`;
    panelChildrenEl.innerHTML = `<div class="info-empty">No sub-branches.</div>`;
    return;
  } else if (id === "company_core") {
    // 2026-09-03 (Lee's spec, Section 18): real, live-checked platform
    // health (dashboard/server.py's _module_company_core, which reuses
    // the same _integration_health() check System already uses) — a
    // star is shown "NOT CONNECTED" honestly, never glossed as healthy.
    const stars = Array.isArray(data.stars) ? data.stars : [];
    panelStatusEl.textContent = data.summary || `${stars.length} platform(s)`;
    statGridMount.innerHTML = `<div class="stat-grid">${statCard(stars.filter(s => s.connected).length, "Connected")}${statCard(stars.length, "Total platforms")}</div>`;
    for (const s of stars) {
      details.push(item(`<span class="k">${escapeHtml(s.name)}</span> — ${s.connected ? "CONNECTED" : "NOT CONNECTED"} (${escapeHtml(s.status)})`, !s.connected));
    }
  } else if (id === "communications" || id === "personal-communications") {
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
    const gauges = [];
    if (typeof data.cpu_percent !== "undefined") gauges.push(gaugeRow("CPU", data.cpu_percent));
    if (typeof data.ram_percent !== "undefined") gauges.push(gaugeRow("RAM", data.ram_percent));
    if (typeof data.gpu_percent !== "undefined") gauges.push(gaugeRow("GPU", data.gpu_percent));
    if (gauges.length) gaugeMount.innerHTML = gauges.join("");

    const SPECIAL_KEYS = ["node", "children", "path", "agents", "strategic_objective", "business_intelligence", "cpu_percent", "ram_percent", "gpu_percent"];
    for (const [k, v] of Object.entries(data)) {
      if (SPECIAL_KEYS.includes(k)) continue;
      details.push(item(`<span class="k">${escapeHtml(k)}</span>: ${escapeHtml(v)}`));
    }
    const agents = (data.agents && Array.isArray(data.agents.agents)) ? data.agents.agents : [];
    if (agents.length) {
      details.push(item(`<span class="k">Agents</span>`));
      for (const a of agents) {
        details.push(item(`${escapeHtml(a.name)} — ${escapeHtml(a.status)} (${escapeHtml(a.permission_level)})`));
      }
      if (data.agents.pending_approval_count) {
        details.push(item(`${data.agents.pending_approval_count} task(s) awaiting approval`, false, true));
      }
    }
    if (data.strategic_objective && !data.strategic_objective.error) {
      const so = data.strategic_objective;
      details.push(item(
        `<span class="k">Objective</span>: $${Math.round(so.cumulative_revenue_usd).toLocaleString()} of ` +
        `$${Math.round(so.target_amount_usd).toLocaleString()} (${so.progress_pct}%) — ` +
        `stretch by ${escapeHtml(so.stretch_deadline)}, committed by ${escapeHtml(so.committed_deadline)}`
      ));
    }
    pushBusinessIntelSection(details, data);
  } else if (id === "hubspot") {
    // Real HubSpot module (actions/hubspot_integration.py via
    // dashboard/server.py's _module_hubspot) — NOT AVAILABLE reported
    // honestly rather than inventing CRM data when unconfigured.
    const st = data.status || {};
    panelStatusEl.textContent = st.verified ? "Connected" : (st.status || "NOT_CONFIGURED");
    if (!st.verified) {
      panelDetailsEl.innerHTML = `<div class="unavailable-card"><span class="tag">${escapeHtml(data.note || "NOT AVAILABLE")}</span><br/>${escapeHtml(st.detail || "")}</div>`;
      panelChildrenEl.innerHTML = `<div class="info-empty">No sub-branches.</div>`;
      return;
    }
    const contacts = Array.isArray(data.recent_contacts) ? data.recent_contacts : [];
    const companies = Array.isArray(data.recent_companies) ? data.recent_companies : [];
    statGridMount.innerHTML = `<div class="stat-grid">${statCard(contacts.length, "Recent contacts")}${statCard(companies.length, "Recent companies")}</div>`;
    if (!contacts.length && !companies.length) details.push(item("No recent HubSpot records."));
    for (const c of contacts.slice(0, 10)) {
      const p = c.properties || {};
      const name = [p.firstname, p.lastname].filter(Boolean).join(" ") || "(no name)";
      details.push(item(`<span class="k">Contact</span> ${escapeHtml(name)} — ${escapeHtml(p.email || "no email")}`));
    }
    for (const c of companies.slice(0, 10)) {
      const p = c.properties || {};
      details.push(item(`<span class="k">Company</span> ${escapeHtml(p.name || c.id || "(unnamed)")}`));
    }
  } else if (id === "social") {
    // Real Buffer module (actions/buffer_integration.py via
    // dashboard/server.py's _module_social) — channel list + live GraphQL
    // schema introspection for scheduling capabilities. Never renders a
    // token — the backend already strips it before this ever arrives.
    const st = data.status || {};
    panelStatusEl.textContent = st.verified ? "Connected" : (st.status || "NOT_CONFIGURED");
    if (!st.verified) {
      panelDetailsEl.innerHTML = `<div class="unavailable-card"><span class="tag">${escapeHtml(data.note || "NOT AVAILABLE")}</span></div>`;
      panelChildrenEl.innerHTML = `<div class="info-empty">No sub-branches.</div>`;
      return;
    }
    const channels = Array.isArray(data.channels) ? data.channels : [];
    statGridMount.innerHTML = `<div class="stat-grid">${statCard(channels.length, "Connected channels")}</div>`;
    if (!channels.length) details.push(item("No channels connected in Buffer."));
    for (const c of channels) {
      const disconnected = !!c.isDisconnected;
      details.push(item(
        `<span class="k">${escapeHtml(c.displayName || c.name || "Channel")}</span> — ${escapeHtml(c.service || "?")}${disconnected ? " [disconnected]" : ""}`,
        disconnected
      ));
    }
    const caps = data.scheduling_capabilities?.capabilities;
    if (caps && Object.keys(caps).length) {
      details.push(item(`<span class="k">Scheduling capabilities (live schema)</span>`));
      const LABELS = {
        create_post: "Create", retrieve_posts: "Retrieve", update_post: "Update",
        delete_post: "Delete", post_status_check: "Status check",
      };
      for (const [k, v] of Object.entries(caps)) {
        details.push(item(`${escapeHtml(LABELS[k] || k)}: ${v ? "supported" : "not supported"}`, !v));
      }
    } else if (data.scheduling_capabilities?.status) {
      details.push(item(`Scheduling capabilities: ${escapeHtml(data.scheduling_capabilities.status)}`, true));
    }
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
}

function item(html, placeholder = false, priority = false) {
  const cls = ["info-item"];
  if (placeholder) cls.push("placeholder");
  if (priority) cls.push("priority");
  return `<div class="${cls.join(" ")}">${html}</div>`;
}

// A clickable JARVIS Brain note/result row — path is a real vault-relative
// path from ObsidianVault.list_notes()/search_notes(), never invented.
function brainItem(path, extra = "") {
  return `<div class="info-item" data-note-path="${escapeHtml(path)}" style="cursor:pointer;"><span class="k">${escapeHtml(path)}</span>${extra}</div>`;
}

// Delegated click handling for Brain note rows and the "back to list" link
// inside a read note — registered once rather than per-render.
panelDetailsEl.addEventListener("click", (e) => {
  const back = e.target.closest("[data-brain-back]");
  if (back) { focusNucleus("knowledge"); return; }
  const row = e.target.closest("[data-note-path]");
  if (row) openBrainNote(row.dataset.notePath);
});

// ── JARVIS Brain — real ObsidianVault list/search/read via /3d/api/module/knowledge ──
async function openBrainNote(path) {
  try {
    const payload = await fetchModule("knowledge", "", path);
    currentModuleData = payload;
    const node = findRootNode("knowledge") || { id: "knowledge", name: "JARVIS Brain" };
    renderInfoPanel("knowledge", node, payload.data || {});
  } catch (e) {
    if (e.message !== "unauthorized") showToast("Couldn't open that Brain note.");
  }
}

async function runKnowledgeSearch() {
  const q = knowledgeSearchInput.value.trim();
  if (!q) return;
  try {
    const payload = await fetchModule("knowledge", q);
    currentModuleData = payload;
    const node = findRootNode("knowledge") || { id: "knowledge", name: "JARVIS Brain" };
    renderInfoPanel("knowledge", node, payload.data || {});
  } catch (e) {
    if (e.message !== "unauthorized") showToast("Brain search failed.");
  }
}
knowledgeSearchBtn.addEventListener("click", runKnowledgeSearch);
knowledgeSearchInput.addEventListener("keydown", (e) => { if (e.key === "Enter") runKnowledgeSearch(); });
knowledgeListBtn.addEventListener("click", () => focusNucleus("knowledge"));

// ── Files search — real /3d/api/module/files?query= endpoint, no fake results ──
async function runFilesSearch() {
  const q = filesSearchInput.value.trim();
  try {
    const payload = await fetchModule("files", q);
    currentModuleData = payload;
    const node = findRootNode("files") || { id: "files", name: "Files" };
    renderInfoPanel("files", node, payload.data || {});
  } catch (e) {
    if (e.message !== "unauthorized") showToast("File search failed.");
  }
}
filesSearchBtn.addEventListener("click", runFilesSearch);
filesSearchInput.addEventListener("keydown", (e) => { if (e.key === "Enter") runFilesSearch(); });

// ── Backend sync: mouse clicks post the same action voice uses ─────────
function postNavigate(navAction, nucleusId) {
  _authFetch("/3d/api/command", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "navigate", nav_action: navAction, nucleus_id: nucleusId }),
  }).catch(() => { /* mouse nav must keep working even if the backend call fails */ });
}

function _redirectToLogin() {
  location.replace("/login?next=" + encodeURIComponent(location.pathname));
}

// ── Connection state + notifications ────────────────────────────────────
function setConnStatus(state) {
  connStatusEl.classList.remove("connected", "reconnecting");
  connStatusEl.classList.add(state);
  connStatusLabelEl.textContent = state === "connected" ? "live" : "reconnecting…";
}

function showToast(text, opts = {}) {
  const el = document.createElement("div");
  el.className = "toast" + (opts.priority ? " priority" : "");
  el.textContent = text;
  toastStackEl.appendChild(el);
  setTimeout(() => el.remove(), 6000);
  while (toastStackEl.children.length > 4) toastStackEl.removeChild(toastStackEl.firstChild);
}

// ── Live push channel: voice navigation, JARVIS state, notifications ────
let wsReconnectDelay = 1000;
function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  // Browsers can't set custom headers on a WebSocket handshake, so the
  // pairing-key session token travels as a query param here (may be empty —
  // in that case the same-origin /ui cookie, sent automatically, is what
  // authenticates the connection server-side).
  const ws = new WebSocket(`${proto}://${location.host}/3d/ws?token=${encodeURIComponent(_authToken)}`);

  ws.onopen = () => {
    setConnStatus("connected");
    wsReconnectDelay = 1000;
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
      setOrbState(msg.state, { label: msg.label });
    } else if (msg.type === "activity") {
      // Real tool/chat/approval events from _handle_3d_chat's on_status/
      // on_tool_event bridge (dashboard/server.py) — never fabricated.
      const kind = msg.kind || "";
      const isError = kind.includes("error");
      const isApproval = kind === "approval_requested";
      const source = msg.source ? `[${msg.source}] ` : "";
      logActivity(`${source}${msg.message || ""}`, { notification: isError, priority: isApproval });
      if (isApproval) refreshApprovalsBadge();
    } else if (msg.type === "notification") {
      const isPriority = /approval|pending|urgent/i.test(msg.text || "");
      showToast(msg.text || "", { priority: isPriority });
      logActivity(msg.text || "Notification", { notification: true, priority: isPriority });
      if (isPriority) refreshApprovalsBadge();
    }
  };
  ws.onclose = () => {
    setConnStatus("reconnecting");
    setTimeout(connectWS, wsReconnectDelay);
    wsReconnectDelay = Math.min(wsReconnectDelay * 1.6, 15000);
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
  if (hovered && hovered !== next) {
    hovered.scale.set(1, 1, 1);
    if (hovered.userData?.kind === "nucleus") hovered.material.emissiveIntensity = hovered.userData.baseEmissive ?? 0.5;
  }
  if (next) {
    next.scale.set(1.15, 1.15, 1.15);
    if (next.userData?.kind === "nucleus") next.material.emissiveIntensity = (next.userData.baseEmissive ?? 0.5) + 0.35;
    canvas.style.cursor = "pointer";
  } else {
    canvas.style.cursor = "default";
  }
  hovered = next;
}

function onPointerClick() {
  if (!hovered) return;
  const { kind, id } = hovered.userData;
  if (kind === "core") goHome();
  else if (kind === "nucleus") focusNucleus(id);
}

// ── Bottom command dock: text + nav parsing + free-text relay ──────────
// Nav phrasing mirrors the existing navigate_command_center Gemini tool
// (core/headless/tool_registry.py) and the on-screen voice hint — this is
// a client-side shortcut into the exact same focusNucleus/goBack/goHome
// calls a mouse click already uses, not a new navigation pathway.
function tryParseNavCommand(text) {
  const t = text.trim().toLowerCase();
  if (!t) return false;
  if (/^(go\s+)?home$/.test(t) || t === "go to jarvis") { goHome(); return true; }
  if (/^(go\s+)?back$/.test(t)) { goBack(); return true; }
  const m = t.match(/^(?:open|go to|show|show me)\s+(.+)$/);
  if (m && hierarchyRoot) {
    const query = m[1].trim();
    const children = (hierarchyRoot.children || []).filter(c => c.id !== "jarvis");
    const match = children.find(c => c.name.toLowerCase() === query)
      || children.find(c => c.name.toLowerCase().includes(query) || query.includes(c.name.toLowerCase()))
      || (query.includes("ddf") ? children.find(c => c.id === "ddf") : null);
    if (match) { focusNucleus(match.id); return true; }
  }
  return false;
}

// ── Real chat bridge — POST /3d/api/command {action:"chat"} straight into
// run_chat_turn() (see dashboard/server.py's _handle_3d_chat). This is the
// Phase 2 priority-1 fix: the dock now shows JARVIS's actual reply, not
// "Sent." Progress/tool/approval state arrives separately over /3d/ws
// (jarvis_state / activity messages) while this call is in flight. ───────
let chatHistory = [];
const MAX_CLIENT_HISTORY = 20;
let ttsEnabled = false;

function renderDockReply(text, opts = {}) {
  dockReplyEl.textContent = text;
  dockReplyEl.classList.toggle("pending", !!opts.pending);
  dockReplyEl.classList.toggle("error", !!opts.error);
  dockReplyEl.hidden = false;
}

async function submitDockCommand() {
  const text = dockInput.value.trim();
  if (!text) return;
  dockInput.value = "";
  if (tryParseNavCommand(text)) return;

  logActivity(`You: "${text}"`);
  renderDockReply("Thinking…", { pending: true });

  try {
    const res = await _authFetch("/3d/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "chat", text, history: chatHistory }),
    });
    if (res.status === 401) { _redirectToLogin(); return; }
    const payload = await res.json();
    if (!payload.ok) {
      renderDockReply(payload.error || "JARVIS couldn't process that.", { error: true });
      return;
    }
    const result = payload.result || {};
    const reply = result.reply || "(no reply)";
    renderDockReply(reply, { error: !!result.error });
    chatHistory.push({ role: "user", text });
    chatHistory.push({ role: "model", text: reply });
    while (chatHistory.length > MAX_CLIENT_HISTORY) chatHistory.shift();
    if (ttsEnabled && !result.error) speakText(reply);
  } catch (e) {
    renderDockReply("Command failed — check the connection.", { error: true });
    setOrbState("error", { label: "Connection error" });
  }
}
dockSend.addEventListener("click", submitDockCommand);
dockInput.addEventListener("keydown", (e) => { if (e.key === "Enter") submitDockCommand(); });

// ── Cartesia TTS playback — POST /3d/api/command {action:"speak"}, which
// calls the exact same synthesize_reply_audio() the browser /ui uses (see
// core/headless/ui.py) — no second voice provider. Toggle defaults off;
// honest toast if no TTS provider is configured on this deployment. ─────
async function speakText(text) {
  try {
    const res = await _authFetch("/3d/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "speak", text }),
    });
    if (res.status === 401) { _redirectToLogin(); return; }
    const payload = await res.json();
    const result = payload.result || {};
    if (!payload.ok || !result.configured) {
      if (ttsEnabled) showToast("Voice output isn't configured on this server.");
      return;
    }
    if (!result.ok) {
      showToast("Voice synthesis failed.");
      return;
    }
    const audio = new Audio(`data:${result.mime_type};base64,${result.audio_base64}`);
    setOrbState("speaking", { label: "Speaking..." });
    const backToIdle = () => { if (currentOrbState === "speaking") setOrbState("idle"); };
    audio.onended = backToIdle;
    audio.onerror = backToIdle;
    await audio.play();
  } catch (e) {
    showToast("Voice playback failed.");
  }
}
dockSpeakToggleEl.addEventListener("click", () => {
  ttsEnabled = !ttsEnabled;
  dockSpeakToggleEl.classList.toggle("active", ttsEnabled);
  dockSpeakToggleEl.title = ttsEnabled ? "Voice replies: on" : "Voice replies: off";
  showToast(ttsEnabled ? "Voice replies enabled" : "Voice replies disabled");
});

// ── Mic — same PCM16 → /ws/phone-audio pipeline app.html already uses.
// Gated on the pairing-key token for the same reason as the text relay:
// /ws/phone-audio only accepts that credential today. ───────────────────
let _voiceWs = null, _audioCtx = null, _micStream = null, _audioNode = null;

function _micIdle() {
  dockMic.innerHTML = "🎤";
  dockMic.title = "Voice — tap to speak";
  dockMic.classList.remove("recording");
}

function _f32toPcm16(f32, srcRate) {
  let s = f32;
  if (srcRate !== 16000) {
    const ratio = srcRate / 16000;
    const len = Math.round(f32.length / ratio);
    s = new Float32Array(len);
    for (let i = 0; i < len; i++) s[i] = f32[Math.min(Math.round(i * ratio), f32.length - 1)];
  }
  const out = new Int16Array(s.length);
  for (let i = 0; i < s.length; i++) out[i] = Math.max(-32768, Math.min(32767, Math.round(s[i] * 32768)));
  return out.buffer;
}

async function startMic() {
  if (!navigator.mediaDevices?.getUserMedia) {
    showToast("This browser can't access the microphone (needs HTTPS or localhost).");
    return;
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
  } catch (e) {
    showToast(e.name === "NotAllowedError" ? "Microphone permission denied." : `Mic error: ${e.message}`);
    return;
  }

  let ctx;
  try { ctx = new AudioContext({ sampleRate: 16000 }); } catch (_) { ctx = new AudioContext(); }
  if (ctx.state === "suspended") await ctx.resume();

  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/phone-audio?token=${encodeURIComponent(_authToken)}`);
  ws.binaryType = "arraybuffer";

  ws.onopen = async () => {
    const rate = ctx.sampleRate;
    const src = ctx.createMediaStreamSource(stream);
    const wCode = `class J extends AudioWorkletProcessor{process(i){const c=i[0]?.[0];if(c)this.port.postMessage(c.slice());return true;}}registerProcessor('j',J);`;
    try {
      const burl = URL.createObjectURL(new Blob([wCode], { type: "application/javascript" }));
      await ctx.audioWorklet.addModule(burl);
      URL.revokeObjectURL(burl);
      const nd = new AudioWorkletNode(ctx, "j");
      let pbuf = [], plen = 0;
      nd.port.onmessage = e => {
        const chunk = new Int16Array(_f32toPcm16(e.data, rate));
        pbuf.push(chunk); plen += chunk.length;
        if (plen >= 1024) {
          const out = new Int16Array(plen); let off = 0;
          for (const c of pbuf) { out.set(c, off); off += c.length; }
          if (ws.readyState === 1) ws.send(out.buffer);
          pbuf = []; plen = 0;
        }
      };
      src.connect(nd);
      _audioNode = nd;
    } catch (_) {
      const sp = ctx.createScriptProcessor(4096, 1, 1);
      sp.onaudioprocess = e => { if (ws.readyState === 1) ws.send(_f32toPcm16(e.inputBuffer.getChannelData(0), rate)); };
      src.connect(sp); sp.connect(ctx.destination);
      _audioNode = sp;
    }
    dockMic.innerHTML = "⏹";
    dockMic.title = "Tap to stop";
    dockMic.classList.add("recording");
    showToast("🎤 Voice live");
    logActivity("Voice input started");
  };
  ws.onclose = () => stopMic();
  ws.onerror = () => { showToast("Voice connection failed."); stopMic(); };

  _voiceWs = ws; _audioCtx = ctx; _micStream = stream;
}

function stopMic() {
  if (_audioNode) { try { _audioNode.disconnect(); } catch (_) {} _audioNode = null; }
  if (_audioCtx) { try { _audioCtx.close(); } catch (_) {} _audioCtx = null; }
  if (_micStream) { _micStream.getTracks().forEach(t => t.stop()); _micStream = null; }
  if (_voiceWs) { const w = _voiceWs; _voiceWs = null; if (w.readyState < 2) w.close(); }
  _micIdle();
}

if (!_authToken) {
  dockMic.disabled = true;
  dockMic.title = "Voice input needs a paired session (pair a device from the phone dashboard)";
} else {
  dockMic.addEventListener("click", () => { if (_voiceWs) stopMic(); else startMic(); });
}

// ── Rail / panel collapse toggles — dispatch a real 'resize' event so the
// already-tested onResize() handler keeps the canvas/camera in sync with
// whatever box the grid gives the stage after the CSS transition. ──────
function _syncCanvasSize() {
  window.dispatchEvent(new Event("resize"));
  setTimeout(() => window.dispatchEvent(new Event("resize")), 260);
}
railToggleEl.addEventListener("click", () => {
  const collapsed = shellEl.classList.toggle("rail-collapsed");
  railToggleEl.setAttribute("aria-expanded", String(!collapsed));
  _syncCanvasSize();
});
infoPanelToggleEl.addEventListener("click", () => {
  const collapsed = shellEl.classList.toggle("panel-collapsed");
  infoPanelToggleEl.setAttribute("aria-expanded", String(!collapsed));
  _syncCanvasSize();
});

// ── Resize + animate ─────────────────────────────────────────────────────
function onResize() {
  const w = stageEl.clientWidth, h = stageEl.clientHeight;
  if (!w || !h) return;
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  renderer.setSize(w, h);
}

function animate() {
  requestAnimationFrame(animate);
  const t = clock.getElapsedTime();

  const speed = STATE_PULSE_SPEED[currentOrbState] ?? 1;
  const pulse = REDUCED_MOTION ? 1 : 1 + Math.sin(t * speed) * 0.06;
  orbMesh.scale.setScalar(pulse);
  if (!REDUCED_MOTION) orbMesh.rotation.y += 0.0025 * (currentOrbState === "thinking" ? 3 : 1);
  orbLight.intensity = 2.8 + (REDUCED_MOTION ? 0 : Math.sin(t * speed) * 0.8);
  if (!REDUCED_MOTION) starField.rotation.y += 0.00006;
  updateOrbits(t);
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

  try {
    const res = await _authFetch("/3d/api/overview");
    if (res.status === 401) return _redirectToLogin();
    const payload = await res.json();
    buildRootRing(payload.hierarchy);
    panelStatusEl.textContent = payload.summary?.status || "Ready for navigation";
    renderOverviewPanel(payload);
    renderRevenueProgress(payload.strategic_objective);
  } catch (e) {
    console.error("[3D] overview load failed", e);
    panelStatusEl.textContent = "Could not reach the JARVIS dashboard backend.";
  }

  refreshApprovalsBadge();
  setInterval(refreshApprovalsBadge, 30000);
  loadRecentActivity();

  connectWS();
  animate();
}

// ── Load real recent activity on open — GET /3d/api/activity, which reuses
// status_api.activity() (agent events + audit log + proactive triggers) —
// so the feed shows real history instead of resetting empty every load. ──
async function loadRecentActivity() {
  try {
    const res = await _authFetch("/3d/api/activity?limit=20");
    if (!res.ok) return;
    const payload = await res.json();
    const events = (payload.activity || []).slice().reverse();
    if (!events.length) return;
    activityFeedEl.innerHTML = "";
    for (const e of events) {
      const isError = (e.kind || "").includes("fail") || (e.kind || "").includes("error");
      logActivity(`[${e.source || "system"}] ${e.message || e.kind || ""}`, { notification: isError });
    }
  } catch (_) { /* best-effort — an empty feed is a fine fallback */ }
}

boot();
