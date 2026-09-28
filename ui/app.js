/* Aura client — one page, one event stream, no framework, no build step.
   Everything the backend publishes lands here and the DOM reflects it. */

"use strict";

/* ── tiny helpers ─────────────────────────────────────────────────────── */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, body) {
  const opts = body
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : {};
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}

function toast(text, ms = 2600) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = text;
  $("toasts").appendChild(el);
  setTimeout(() => { el.classList.add("leaving"); setTimeout(() => el.remove(), 300); }, ms);
}

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/* ── global state ─────────────────────────────────────────────────────── */

const S = {
  state: "starting",
  session: null,
  proposal: null,
  liveRows: {},
  historyCount: 0,
  activityUnread: 0,
  demo: false,
};

const STATE_TEXT = {
  starting:  ["Starting", ""],
  disabled:  ["Paused", ""],
  armed:     ["Ready", "Say your wake phrase — or tap the orb"],
  capturing: ["Listening…", "Go ahead"],
  transcribing: ["One moment…", ""],
  planning:  ["Thinking…", ""],
  proposing: ["Your call", "Review the plan below"],
  executing: ["On it…", ""],
  responding: ["Done", ""],
};

/* ── orb: canvas ring + waveform ──────────────────────────────────────── */

const orb = { c: $("orb-canvas"), t: 0, level: 0, targetLevel: 0.18, mode: "idle" };

function drawOrb() {
  const ctx = orb.c.getContext("2d");
  const W = orb.c.width, H = orb.c.height, cx = W / 2, cy = H / 2;
  orb.t += 0.016;
  orb.level += (orb.targetLevel - orb.level) * 0.08;
  ctx.clearRect(0, 0, W, H);

  const accent = orb.mode === "attention" ? [255, 159, 10] : orb.mode === "capture" ? [48, 209, 88] : [10, 132, 255];
  const accent2 = orb.mode === "attention" ? [255, 214, 10] : [94, 92, 230];

  for (let ring = 0; ring < 3; ring++) {
    const r = 150 - ring * 22;
    const a0 = orb.t * (0.6 + ring * 0.35) + ring * 2.1;
    const alpha = (0.5 - ring * 0.13) * (0.5 + orb.level);
    const grad = ctx.createConicGradient ? ctx.createConicGradient(a0, cx, cy) : null;
    if (grad) {
      grad.addColorStop(0, `rgba(${accent2},0)`);
      grad.addColorStop(0.25, `rgba(${accent},${alpha})`);
      grad.addColorStop(0.5, `rgba(${accent2},0)`);
      grad.addColorStop(0.75, `rgba(${accent},${alpha * 0.6})`);
      grad.addColorStop(1, `rgba(${accent2},0)`);
      ctx.strokeStyle = grad;
    } else {
      ctx.strokeStyle = `rgba(${accent},${alpha})`;
    }
    ctx.lineWidth = 2 - ring * 0.4;
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.stroke();
  }

  const bars = 48;
  for (let i = 0; i < bars; i++) {
    const ang = (i / bars) * Math.PI * 2;
    const wobble =
      Math.sin(orb.t * 3 + i * 0.55) * 0.5 +
      Math.sin(orb.t * 7.3 + i * 1.7) * 0.5;
    const len = 6 + Math.abs(wobble) * 26 * orb.level;
    const r1 = 62, r2 = 62 + len;
    ctx.strokeStyle = `rgba(${accent},${0.25 + 0.55 * orb.level})`;
    ctx.lineWidth = 2.4;
    ctx.lineCap = "round";
    ctx.beginPath();
    ctx.moveTo(cx + Math.cos(ang) * r1, cy + Math.sin(ang) * r1);
    ctx.lineTo(cx + Math.cos(ang) * r2, cy + Math.sin(ang) * r2);
    ctx.stroke();
  }
  requestAnimationFrame(drawOrb);
}
requestAnimationFrame(drawOrb);

function setOrbMode(mode) {
  orb.mode = mode;
  const orbEl = $("orb");
  orbEl.classList.toggle("is-capturing", mode === "capture");
  orbEl.classList.toggle("is-attention", mode === "attention");
  orb.targetLevel =
    mode === "capture" ? 0.9 : mode === "busy" ? 0.55 : mode === "attention" ? 0.4 : 0.18;
}

/* ── status / views ───────────────────────────────────────────────────── */

function setStatus(state) {
  S.state = state;
  const [label, sub] = STATE_TEXT[state] || [state, ""];
  $("status-label").textContent = label;
  $("orb-title").textContent = label === "Ready" ? "Say your wake phrase" : label;
  $("orb-sub").textContent = sub || "or tap the orb — then just talk";

  const dot = $("status-dot");
  dot.className = "status-dot";
  if (state === "armed" || state === "responding") dot.classList.add("is-armed");
  else if (["capturing", "planning", "transcribing", "executing"].includes(state))
    dot.classList.add("is-busy");
  else if (state === "proposing") dot.classList.add("is-attention");

  setOrbMode(
    state === "capturing" ? "capture" :
    state === "proposing" ? "attention" :
    ["planning", "transcribing", "executing"].includes(state) ? "busy" : "idle");

  if (state === "armed") {
    $("caption").hidden = true;
    if (S.proposal) hideProposal();
  }
}

function showView(name) {
  document.body.dataset.view = name;
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("is-active"));
  $(`view-${name}`).classList.add("is-active");
  document.querySelectorAll(".nav-item").forEach((n) =>
    n.classList.toggle("is-active", n.dataset.nav === name));
  if (name === "activity") { S.activityUnread = 0; updateActivityBadge(); }
}

document.querySelectorAll(".nav-item").forEach((n) =>
  n.addEventListener("click", (e) => { e.preventDefault(); showView(n.dataset.nav); }));

/* Deep links: #setup / #onboarding (the menu-bar shell opens #onboarding on
   first run). Keep the hash and the view in sync. */
const VIEW_ALIASES = { onboarding: "setup" };

function viewFromHash() {
  const h = location.hash.replace("#", "");
  return VIEW_ALIASES[h] || h;
}

window.addEventListener("hashchange", () => {
  const v = viewFromHash();
  if ($(`view-${v}`)) showView(v);
});

function updateActivityBadge() {
  const el = $("activity-count");
  el.hidden = !(S.activityUnread > 0);
  el.textContent = S.activityUnread;
}

/* ── overview: caption, composer, chips ───────────────────────────────── */

function showCaption(text) {
  const el = $("caption");
  el.textContent = text;
  el.hidden = false;
  el.style.animation = "none"; void el.offsetWidth; el.style.animation = "";
}

function showTranscriptEcho(text) {
  const el = $("transcript-echo");
  el.textContent = `“${text}”`;
  el.hidden = false;
}

$("composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("command-input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  try {
    await api("/api/input", { text });
    showTranscriptEcho(text);
  } catch { toast("Aura isn't responding."); }
});

document.querySelectorAll(".chip").forEach((chip) =>
  chip.addEventListener("click", () => {
    $("command-input").value = chip.dataset.cmd;
    $("composer").requestSubmit();
  }));

$("orb").addEventListener("click", async () => {
  try { await api("/api/trigger", {}); }
  catch { toast("Aura isn't responding."); }
});

/* ── proposal (confirm before run) ────────────────────────────────────── */

function showProposal(p) {
  S.proposal = p;
  const box = $("proposal");
  const n = p.actions.length;
  $("proposal-count").textContent = `${n} action${n > 1 ? "s" : ""}`;
  $("proposal-reason").textContent =
    "Aura checks before it touches anything. Take a look — then decide.";
  $("proposal-actions").innerHTML = p.actions.map((a) => `
    <li class="proposal-action">
      <span class="risk-pill risk-confirm">${esc(a.verdict)}</span>
      <span class="skill">${esc(a.skill)}</span>
      <span class="args">${esc(JSON.stringify(a.args))}</span>
      <span class="why">${esc(a.why || "")}</span>
    </li>`).join("");
  box.hidden = false;
}

function hideProposal() { $("proposal").hidden = true; S.proposal = null; }

async function resolve(answer) {
  if (!S.proposal) return;
  const token = S.proposal.token;
  hideProposal();
  try { await api(answer === "confirm" ? "/api/confirm" : "/api/cancel", { token }); }
  catch { toast("Aura isn't responding."); }
}

$("btn-confirm").addEventListener("click", () => resolve("confirm"));
$("btn-cancel").addEventListener("click", () => resolve("cancel"));

document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault(); $("command-input").focus();
  }
  if (e.key === "Escape" && S.proposal) resolve("cancel");
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && S.proposal) { e.preventDefault(); resolve("confirm"); }
});

/* ── live action rows ─────────────────────────────────────────────────── */

function startLiveRow(index, skill) {
  const row = document.createElement("div");
  row.className = "action-row";
  row.innerHTML = `
    <span class="spinner"></span>
    <svg class="mark mark-ok" viewBox="0 0 20 20"><path d="M8 14.5 3.5 10l1.4-1.4L8 11.7l7.1-7.1L16.5 6 8 14.5Z"/></svg>
    <svg class="mark mark-fail" viewBox="0 0 20 20"><path d="m10 8.6 3.5-3.6 1.4 1.4-3.5 3.6 3.5 3.6-1.4 1.4-3.5-3.6L6.5 15l-1.4-1.4L8.6 10 5.1 6.4l1.4-1.4L10 8.6Z"/></svg>
    <span>${esc(skill)}</span>`;
  $("live-actions").appendChild(row);
  $("live-actions").hidden = false;
  S.liveRows[index] = row;
}

function endLiveRow(index, ok) {
  const row = S.liveRows[index];
  if (!row) return;
  row.classList.add(ok ? "ok" : "fail");
  setTimeout(() => {
    row.style.transition = "opacity 600ms"; row.style.opacity = "0";
    setTimeout(() => row.remove(), 650);
  }, 1600);
}

/* ── timeline (activity) ──────────────────────────────────────────────── */

function timelineItem(ev, prepend = false) {
  const plan = ev.plan || {};
  const actions = plan.actions || [];
  const el = document.createElement("div");
  el.className = "tl-item";
  const actionPills = actions.map((a) => `<span class="tl-action ${a.risk === "confirm" ? "blocked" : "ok"}">${esc(a.skill)}</span>`).join("");
  el.innerHTML = `
    <div class="tl-head">
      <span class="tl-time">${fmtTime(ev.ts)}</span>
      <span class="tl-transcript">${esc(ev.transcript)}</span>
      <span class="tl-lat">${ev.total_ms ?? 0} ms</span>
    </div>
    <div class="tl-reply">${esc(ev.reply || "")}</div>
    ${actionPills ? `<div class="tl-actions">${actionPills}</div>` : ""}
    <div class="tl-foot">
      <button class="fb-btn" data-fb="good" title="Aura got it right">It worked</button>
      <button class="fb-btn" data-fb="bad" title="Aura got it wrong — teach it">Teach Aura</button>
      <input class="tl-note" placeholder="When I say this, do that…" style="display:none">
      <button class="fb-btn" data-fb="send-note" style="display:none">Send</button>
    </div>`;

  el.querySelectorAll("[data-fb]").forEach((btn) => btn.addEventListener("click", async () => {
    const kind = btn.dataset.fb;
    if (kind === "send-note") {
      const note = el.querySelector(".tl-note").value.trim();
      if (!note) return;
      await api("/api/correct", {
        transcript: ev.transcript, skill: actions[0]?.skill || "",
        verdict: "bad", note,
      });
      toast("Learned. Aura will remember that.");
      return;
    }
    el.querySelectorAll("[data-fb]").forEach((b) => b.classList.remove("is-on"));
    btn.classList.add("is-on");
    if (kind === "bad") {
      el.querySelector(".tl-note").style.display = "";
      el.querySelector('[data-fb="send-note"]').style.display = "";
      el.querySelector(".tl-note").focus();
    } else {
      el.querySelector(".tl-note").style.display = "none";
      el.querySelector('[data-fb="send-note"]').style.display = "none";
      await api("/api/correct", { transcript: ev.transcript, skill: actions[0]?.skill || "", verdict: "good" });
      toast("Noted — that goes into Aura's next lesson.");
    }
  }));

  const list = $("timeline");
  prepend ? list.prepend(el) : list.append(el);
  return el;
}

async function loadHistory() {
  try {
    const { events } = await api("/api/history");
    $("timeline").innerHTML = "";
    events.forEach((ev) => timelineItem(ev));
    S.historyCount = events.length;
    updateActivityBadge();
  } catch { /* server warming up */ }
}

/* ── skills ───────────────────────────────────────────────────────────── */

async function loadSkills() {
  try {
    const { skills } = await api("/api/skills");
    $("skills-sub").textContent =
      `${skills.length} skills. Each one declared, reviewed, and yours to inspect.`;
    $("skill-grid").innerHTML = skills.map((s) => `
      <div class="skill-card">
        <span class="risk-pill risk-${esc(s.risk)}">${esc(s.risk)}</span>
        <div class="skill-name">${esc(s.name)}</div>
        <div class="skill-desc">${esc(s.description)}</div>
        ${s.examples[0] ? `<div class="skill-ex">“${esc(s.examples[0])}”</div>` : ""}
      </div>`).join("");
  } catch { /* ignore */ }
}

/* ── settings ─────────────────────────────────────────────────────────── */

function renderSettings(st) {
  const rows = (items) => items.map(([k, help, value, control]) => `
    <div class="setting-row">
      <div><div class="setting-label">${k}</div><div class="setting-help">${help}</div></div>
      ${control || `<div class="setting-value">${esc(value)}</div>`}
    </div>`).join("");

  const t = (on) => `<button class="toggle ${on ? "is-on" : ""}" disabled aria-label="toggle"></button>`;
  $("settings-body").innerHTML = `
    <div class="settings-grid">
      <div class="card"><h2>How Aura runs</h2>${rows([
        ["Profile", "On your Mac this is always the real thing", st.profile],
        ["Execution", st.bridge === "mac" ? "Full access" : "Simulated (developer build)", st.bridge],
        ["Wake mode", st.wake_mode === "openwakeword" ? "Always listening" : "When I tap", st.wake_mode],
        ["Your data", "History, preferences, lessons — plain files you can read", st.data_dir],
      ])}</div>
      <div class="card"><h2>Voice</h2>${rows([
        ["Speak replies", "Aura answers out loud", "", t(st.tts_enabled)],
        ["Understanding", `${st.planner.model}, running locally`, st.planner.model],
      ])}</div>
      <div class="card"><h2>Decisions</h2>${rows([
        ["Decision engine", st.laya.backend === "RealLayaBackend" ? "Laya decision model" : "Built-in rules (upgradeable)", st.laya.backend],
        ["Confidence floor", "Below this, Aura asks instead of acting", st.laya.confidence ?? "—"],
        ["Lessons learned", "Every confirmation and correction, ready for the next lesson",
          `${st.laya.examples.total} total · ${st.laya.examples.confirmed} confirmed · ${st.laya.examples.corrected} corrected`],
      ])}</div>
    </div>`;
}

/* ── setup (permissions & readiness) ──────────────────────────────────── */

let lastAutomationTest = null;
let installing = false;

async function loadPermissions() {
  try {
    renderSetup(await api("/api/permissions"));
  } catch { /* server warming up */ }
}

function statusPill(state) {
  const cls = state === "ready" ? "risk-safe" : state === "action" ? "risk-confirm" : "risk-unknown";
  const label = state === "ready" ? "Ready" : state === "action" ? "Action needed" : "—";
  return `<span class="risk-pill ${cls}">${label}</span>`;
}

const SETUP_ICONS = {
  mic: `<svg viewBox="0 0 20 20"><path d="M10 2a2.5 2.5 0 0 1 2.5 2.5v6a2.5 2.5 0 0 1-5 0v-6A2.5 2.5 0 0 1 10 2Zm-6 8.5a6 6 0 0 0 5 5.9V19h2v-2.6a6 6 0 0 0 5-5.9h-1.7a4.3 4.3 0 0 1-8.6 0H4Z"/></svg>`,
  shield: `<svg viewBox="0 0 20 20"><path d="M10 2 4 4.5v5c0 4 2.6 7 6 8.5 3.4-1.5 6-4.5 6-8.5v-5L10 2Zm-1.2 11L6 10.2l1.4-1.4 1.4 1.4 3.8-3.8L14 7.8 8.8 13Z"/></svg>`,
  bolt: `<svg viewBox="0 0 20 20"><path d="M11.5 2 4 11.5h4.2L7 18l7.6-9.5h-4.3L11.5 2Z"/></svg>`,
  chip: `<svg viewBox="0 0 20 20"><path d="M7 7h6v6H7V7Zm2-5h2v3H9V2Zm0 13h2v3H9v-3ZM2 9h3v2H2V9Zm13 0h3v2h-3V9ZM4.5 3.1l1.4 1.4-1.4 1.4-1.4-1.4 1.4-1.4Zm9.6 9.6 1.4 1.4-1.4 1.4-1.4-1.4 1.4-1.4ZM15.5 3.1l1.4 1.4-1.4 1.4-1.4-1.4 1.4-1.4ZM4.5 12.7l1.4 1.4-1.4 1.4-1.4-1.4 1.4-1.4Z"/></svg>`,
};

function setupCard({ icon, title, status, body, buttons = [], result }) {
  const btns = buttons.map((b) =>
    `<button class="fb-btn" data-setup="${esc(b.action)}" data-arg="${esc(b.arg || "")}">${esc(b.label)}</button>`).join("");
  const resultCls = result ? (result.status === "ok" ? "ok" : result.status === "unavailable" ? "" : "bad") : "";
  return `
  <div class="card">
    <div class="setup-head">
      <span class="setup-icon">${SETUP_ICONS[icon] || ""}</span>
      <div class="setup-title"><h2>${title}</h2>${statusPill(status)}</div>
    </div>
    <p class="muted setup-body">${body}</p>
    ${result ? `<div class="setup-result ${resultCls}">${esc(result.message)}</div>` : ""}
    ${btns ? `<div class="setup-buttons">${btns}</div>` : ""}
  </div>`;
}

function renderSetup(p) {
  const demo = p.bridge !== "mac" && p.platform !== "mac";
  const mic = demo ? "unknown" : p.microphone ? "ready" : "action";
  const ax = demo ? "unknown"
    : p.accessibility === true ? "ready" : p.accessibility === false ? "action" : "unknown";
  const whisper = demo ? "unknown" : p.whisper_cpp ? "ready" : "action";
  const planner = demo ? "unknown" : p.planner_server ? "ready" : "action";

  const done = [mic, ax, whisper, planner].filter((s) => s === "ready").length;
  $("setup-progress").innerHTML = demo
    ? `<div class="progress-row"><strong>Developer build</strong>
       <span class="muted">On your Mac, this panel walks you through each permission —
       with live checks, once.</span></div>`
    : `<div class="progress-row"><strong>${done} of 4 ready</strong>
       <span class="muted">${done === 4 ? "Aura is ready. Say the word." : "Each step takes under a minute."}</span></div>
       <div class="progress"><div class="progress-fill" style="width:${(done / 4) * 100}%"></div></div>`;

  const refresh = [{ action: "refresh", label: "Check again" }];
  const openBtn = (arg) => demo ? [] :
    [{ action: "open", arg, label: "Open System Settings" }];

  const cards = [
    setupCard({
      icon: "mic", title: "Microphone", status: mic,
      body: "Aura hears you only through this. What you say is transcribed on this Mac and never leaves it.",
      buttons: demo ? [] : [...openBtn("microphone"), ...refresh],
    }),
    setupCard({
      icon: "shield", title: "Accessibility", status: ax,
      body: "Lets Aura see and act inside your apps — the same permission Voice Control uses. Aura reads structured labels, never screenshots.",
      buttons: demo ? [] : [...openBtn("accessibility"), ...refresh],
    }),
    setupCard({
      icon: "bolt", title: "Automation", status: ax,
      body: "macOS asks once, per app, the first time Aura acts for you. That prompt is a feature — run the test to see it.",
      buttons: demo ? [] : [{ action: "test", label: "Send a test event" }, ...openBtn("automation")],
      result: lastAutomationTest,
    }),
    setupCard({
      icon: "chip", title: "Speech model", status: whisper,
      body: p.whisper_cpp
        ? "Ready. Your voice is transcribed on this Mac — on the Neural Engine where available."
        : "A compact speech model (about 150 MB) that turns your voice into text, entirely on this Mac.",
      buttons: demo ? [] : (p.whisper_cpp ? refresh
        : [{ action: "install", label: "Download and set up" }, ...refresh]),
    }),
    setupCard({
      icon: "chip", title: "Aura's mind", status: planner,
      body: p.planner_server
        ? `Connected — ${esc(p.model)} is answering, right on this Mac.`
        : "A small language model that understands your requests. Get Ollama from " +
          "<a href='https://ollama.com/download' target='_blank' rel='noopener'>ollama.com</a> — " +
          "once it's running, Aura finds it on its own.",
      buttons: demo ? [] : refresh,
    }),
  ];
  $("setup-grid").innerHTML = cards.join("");
}

async function runInstall() {
  if (installing) return;
  installing = true;
  toast("Setting things up — this can take a few minutes.");
  try { await api("/api/setup/install", {}); } catch { toast("Aura isn't responding."); }
}

$("setup-grid") && $("setup-grid").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-setup]");
  if (!btn) return;
  const action = btn.dataset.setup;
  if (action === "refresh") { loadPermissions(); return; }
  if (action === "install") { runInstall(); return; }
  if (action === "open") {
    try {
      await api("/api/permissions/open", { target: btn.dataset.arg });
      toast("System Settings is open — allow Aura, then check again.");
    } catch { toast("Couldn't open System Settings."); }
    return;
  }
  if (action === "test") {
    btn.disabled = true; btn.textContent = "Testing…";
    try {
      lastAutomationTest = await api("/api/permissions/test_automation", {});
      const s = lastAutomationTest.status;
      toast(s === "ok" ? "Automation is working."
        : s === "denied" ? "macOS asked for permission — allow it, then check again."
        : "Not available on this machine.");
    } catch { lastAutomationTest = { status: "bad", message: "Aura isn't responding." }; }
    loadPermissions();
  }
});

function showSetupProgress(d) {
  let list = $("setup-live");
  if (!list) {
    list = document.createElement("div");
    list.id = "setup-live";
    list.className = "card";
    list.innerHTML = "<h2>Setting up Aura</h2><div class='install-list'></div>";
    $("setup-progress").after(list);
  }
  list.hidden = false;
  const rows = list.querySelector(".install-list");
  let row = rows.querySelector(`[data-step="${d.key}"]`);
  if (!row) {
    row = document.createElement("div");
    row.className = "install-row";
    row.dataset.step = d.key;
    rows.appendChild(row);
  }
  const mark = d.status === "ok" ? "✓" : d.status === "fail" ? "✕" : d.status === "skip" ? "–" : "•";
  row.innerHTML = `<span class="install-mark ${d.status}">${mark}</span>` +
    `<span>${esc(d.title)}</span><span class="install-detail">${esc(d.detail || "")}</span>`;
}

/* ── Wake Phrase Studio — train your phrase in-app ────────────────────── */

const wakeTrain = { phrase: "", count: 0, need: 6 };

function renderDots() {
  const dots = [];
  for (let i = 0; i < wakeTrain.need; i++) {
    dots.push(`<span class="dot ${i < wakeTrain.count ? "is-on" : ""}"></span>`);
  }
  $("wake-dots").innerHTML = dots.join("");
  $("wake-finish-btn").disabled = wakeTrain.count < 3;
  $("wake-record-btn").textContent = wakeTrain.count >= wakeTrain.need
    ? "Record again" : "Record a sample";
}

function showTrainResult(text, ok) {
  const el = $("wake-record-result");
  el.textContent = text;
  el.className = `setup-result ${ok ? "ok" : "bad"}`;
  el.hidden = false;
}

$("wake-train-begin").addEventListener("click", async () => {
  const phrase = $("wake-phrase-input").value.trim();
  if (!phrase) { toast("Type the phrase you'd like to use."); return; }
  try {
    const res = await api("/api/wake/train", { phrase });
    if (!res.ok) { toast(res.message); return; }
    wakeTrain.phrase = phrase; wakeTrain.count = 0; wakeTrain.need = res.need;
    $("wake-phrase-echo").textContent = phrase;
    $("wake-trainer").hidden = true;
    $("wake-recording").hidden = false;
    renderDots();
    showTrainResult("Ready when you are.", true);
  } catch { toast("Aura isn't responding."); }
});

$("wake-record-btn").addEventListener("click", async () => {
  try {
    const res = await api("/api/wake/train/capture");
    if (!res.ok) { toast(res.message); return; }
    $("wake-record-btn").disabled = true;
    $("wake-record-btn").textContent = "Listening…";
    showTrainResult("Say it now.", true);
  } catch { toast("Aura isn't responding."); }
});

$("wake-finish-btn").addEventListener("click", async () => {
  $("wake-finish-btn").disabled = true;
  $("wake-finish-btn").textContent = "Training…";
  try {
    const res = await api("/api/wake/train/finish");
    if (!res.ok) {
      showTrainResult(res.message, false);
      $("wake-finish-btn").disabled = wakeTrain.count < 3;
      $("wake-finish-btn").textContent = "Train phrase";
      return;
    }
    showTrainResult("Done. Say it anytime — Aura is listening for you.", true);
    toast("Your wake phrase is live.");
    setTimeout(() => {
      $("wake-recording").hidden = true;
      $("wake-trainer").hidden = false;
      $("wake-phrase-input").value = "";
      $("wake-phrase").value = res.phrase;
    }, 2200);
    document.querySelectorAll("#wake-mode .seg").forEach((s) =>
      s.classList.toggle("is-active", s.dataset.mode === "openwakeword"));
    $("wake-mode-help").textContent =
      "Always listening: Aura responds to your phrase — nothing else.";
  } catch { toast("Aura isn't responding."); }
});

$("wake-cancel-btn").addEventListener("click", async () => {
  try { await api("/api/wake/train/cancel", {}); } catch {}
  $("wake-recording").hidden = true;
  $("wake-trainer").hidden = false;
  $("wake-record-btn").disabled = false;
});

/* ── wake mode (live, persisted) ──────────────────────────────────────── */

async function setWake(mode, phrase) {
  try {
    const res = await api("/api/wake", { mode, phrase });
    if (res.ok === false) { toast(res.message || "Couldn't change that right now."); return false; }
    if (res.engine === "OpenWakeWordEngine" || res.engine === "TemplateWakeEngine") {
      toast(mode === "openwakeword" ? "Always listening is on." : "Manual wake.");
    } else {
      toast("Saved. The listening engine finishes setting up in Setup.");
    }
    return true;
  } catch { toast("Aura isn't responding."); return false; }
}

document.querySelectorAll("#wake-mode .seg").forEach((seg) =>
  seg.addEventListener("click", async () => {
    const ok = await setWake(seg.dataset.mode, $("wake-phrase").value.trim());
    if (ok) {
      document.querySelectorAll("#wake-mode .seg").forEach((s) =>
        s.classList.toggle("is-active", s === seg));
      $("wake-mode-help").textContent = seg.dataset.mode === "openwakeword"
        ? "Always listening: Aura responds to your wake phrase — nothing else."
        : "When I tap: Aura listens right after you tap the orb or press ⌥Space.";
    }
  }));

let phraseTimer = null;
$("wake-phrase") && $("wake-phrase").addEventListener("change", async () => {
  const mode = document.querySelector("#wake-mode .seg.is-active")?.dataset.mode;
  if (mode === "openwakeword") await setWake(mode, $("wake-phrase").value.trim());
});

/* ── SSE: the nervous system ──────────────────────────────────────────── */

function handleEvent(type, d) {
  switch (type) {
    case "state": {
      const wasArmed = S.state === "armed";
      setStatus(d.state);
      if (d.state !== "armed") $("live-actions").hidden = false;
      if (d.state === "armed" && wasArmed) {
        setTimeout(() => { $("live-actions").innerHTML = ""; $("live-actions").hidden = true; }, 1800);
      }
      if (d.state === "armed") { S.session = null; $("transcript-echo").hidden = true; }
      break;
    }
    case "hint": if (d.text) showCaption(d.text); break;
    case "transcript": showTranscriptEcho(d.text); showCaption("Got it."); break;
    case "plan": if (d.reply) showCaption(d.reply); break;
    case "proposal":
      S.session = d.session;
      showProposal({ token: d.token, actions: d.actions });
      showCaption(d.reply || "Your call.");
      break;
    case "action_started":
      if (d.session && d.session !== S.session) {
        S.session = d.session; S.liveRows = {};
        $("live-actions").innerHTML = "";
      }
      startLiveRow(d.index, d.skill);
      break;
    case "action_result": endLiveRow(d.index, d.ok); break;
    case "reply":
      showCaption(d.text);
      if (d.total_ms > 0) addLiveTimelineItem(d);
      break;
    case "feedback": break;
    case "train_sample": {
      wakeTrain.count = d.count;
      renderDots();
      showTrainResult(d.message, d.ok);
      $("wake-record-btn").disabled = false;
      $("wake-finish-btn").textContent = "Train phrase";
      break;
    }
    case "train_update": {
      if (d.phase === "listening" && !$("wake-recording").hidden) {
        $("wake-record-btn").textContent = "Listening… say it now";
      }
      if (d.phase === "capture" && !$("wake-recording").hidden && wakeTrain.count !== d.count) {
        wakeTrain.count = d.count;
        renderDots();
        $("wake-record-btn").disabled = false;
        $("wake-record-btn").textContent =
          wakeTrain.count >= wakeTrain.need ? "Record again" : "Record a sample";
      }
      break;
    }
    case "setup_progress":
      showSetupProgress(d);
      break;
    case "setup_done":
      toast(d.summary || "Setup finished.");
      setTimeout(() => loadPermissions(), 600);
      installing = false;
      break;
    case "log": console.info("[aura]", d.line); break;
  }
}

function addLiveTimelineItem(d) {
  const ev = {
    ts: Date.now() / 1000,
    transcript: S.lastTranscript || "(session)",
    reply: d.text,
    total_ms: d.total_ms,
    plan: S.lastPlan || { actions: [] },
  };
  timelineItem(ev, true);
  S.historyCount++;
  if (document.body.dataset.view !== "activity") S.activityUnread++;
  updateActivityBadge();
}

function connect() {
  const es = new EventSource("/api/events");
  es.onopen = () => { $("conn-badge").textContent = S.demo ? "developer build" : "live · on-device"; };
  es.onerror = () => {
    $("conn-badge").textContent = "reconnecting…";
    setTimeout(() => { es.close(); connect(); }, 2000);
  };
  const types = ["state", "hint", "transcript", "plan", "proposal", "action_started",
    "action_result", "reply", "feedback", "log", "train_update", "train_sample",
    "setup_progress", "setup_done"];
  types.forEach((t) => es.addEventListener(t, (e) => {
    try {
      const payload = JSON.parse(e.data);
      if (t === "transcript") S.lastTranscript = payload.text;
      if (t === "plan") S.lastPlan = { actions: payload.actions || [], reply: payload.reply };
      handleEvent(t, payload);
    } catch (err) { console.warn("bad event", err); }
  }));
}

/* ── boot ─────────────────────────────────────────────────────────────── */

(async function boot() {
  let platform = "mac";
  try {
    const perms = await api("/api/permissions");
    platform = perms.platform;
  } catch { /* ignore */ }
  S.demo = platform !== "mac";      // users never see this; dev machines do
  $("demo-banner").hidden = !S.demo;

  try {
    const st = await api("/api/state");
    $("version").textContent = `v${st.version}`;
    $("status-meta").textContent =
      `${st.bridge === "mac" ? "Ready on this Mac" : "Developer build"} · ` +
      `${st.wake_mode === "openwakeword" ? "always listening" : "tap to listen"}`;
    renderSettings({ ...st, data_dir: st.data_dir || "—" });
    setStatus(st.state === "starting" ? "armed" : st.state);
    $("wake-phrase").value = st.wake_phrase || "";
    document.querySelectorAll("#wake-mode .seg").forEach((seg) =>
      seg.classList.toggle("is-active", seg.dataset.mode === st.wake_mode));
    if (st.wake_mode === "openwakeword") {
      $("wake-mode-help").textContent =
        "Always listening: Aura responds to your wake phrase — nothing else.";
    }
  } catch { $("conn-badge").textContent = "offline"; }

  loadSkills();
  loadHistory();
  loadPermissions();
  connect();

  const initial = viewFromHash();
  if ($(`view-${initial}`)) showView(initial);
})();
