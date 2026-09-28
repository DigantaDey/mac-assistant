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
  session: null,          // active session id
  proposal: null,         // {token, actions:[…]}
  liveRows: {},           // index → element (current session's action rows)
  historyCount: 0,
  activityUnread: 0,
  demo: false,
};

const STATE_TEXT = {
  starting:  ["Starting", ""],
  disabled:  ["Disabled", ""],
  armed:     ["Ready", "Say your wake word — or press the orb"],
  capturing: ["Listening…", "Just talk — I'll take it from here"],
  transcribing: ["Transcribing…", "Turning speech into text, on-device"],
  planning:  ["Thinking…", "Choosing the smallest safe plan"],
  proposing: ["Needs your OK", "Review the plan below"],
  executing: ["Working…", "Running your plan"],
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

  // rotating conic ring (outer)
  for (let ring = 0; ring < 3; ring++) {
    const r = 150 - ring * 22;
    const a0 = orb.t * (0.6 + ring * 0.35) + ring * 2.1;
    const alpha = (0.5 - ring * 0.13) * (0.5 + orb.level);
    const grad = ctx.createConicGradient
      ? ctx.createConicGradient(a0, cx, cy)
      : null;
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

  // waveform bars (inner)
  const bars = 48;
  for (let i = 0; i < bars; i++) {
    const ang = (i / bars) * Math.PI * 2;
    const wobble =
      Math.sin(orb.t * 3 + i * 0.55) * 0.5 +
      Math.sin(orb.t * 7.3 + i * 1.7) * 0.5;
    const len = 6 + Math.abs(wobble) * 26 * orb.level;
    const r1 = 62, r2 = 62 + len;
    const x1 = cx + Math.cos(ang) * r1, y1 = cy + Math.sin(ang) * r1;
    const x2 = cx + Math.cos(ang) * r2, y2 = cy + Math.sin(ang) * r2;
    ctx.strokeStyle = `rgba(${accent},${0.25 + 0.55 * orb.level})`;
    ctx.lineWidth = 2.4;
    ctx.lineCap = "round";
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
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
  $("orb-title").textContent = label === "Ready" ? "Say your wake word" : label;
  $("orb-sub").textContent = sub || "or press the orb — then just talk";

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
  // restart the pop animation
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
  } catch { toast("Aura isn't reachable"); }
});

document.querySelectorAll(".chip").forEach((chip) =>
  chip.addEventListener("click", () => {
    $("command-input").value = chip.dataset.cmd;
    $("composer").requestSubmit();
  }));

$("orb").addEventListener("click", async () => {
  try { await api("/api/trigger", {}); }
  catch { toast("Aura isn't reachable"); }
});

/* ── proposal (confirm before run) ────────────────────────────────────── */

function showProposal(p) {
  S.proposal = p;
  const box = $("proposal");
  const n = p.actions.length;
  $("proposal-count").textContent = `${n} action${n > 1 ? "s" : ""}`;
  $("proposal-reason").textContent =
    "One or more actions need your OK — Aura checks before it touches anything.";
  $("proposal-actions").innerHTML = p.actions.map((a) => `
    <li class="proposal-action">
      <span class="risk-pill ${a.verdict === "blocked" ? "risk-confirm" : "risk-confirm"}">${esc(a.verdict)}</span>
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
  catch { toast("Couldn't reach Aura"); }
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
  el.dataset.transcript = ev.transcript || "";
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
      <button class="fb-btn" data-fb="good" title="Aura got it right — reinforce this">👍 worked</button>
      <button class="fb-btn" data-fb="bad" title="Aura got it wrong — teach it">👎 correct</button>
      <input class="tl-note" placeholder="teach: “when I say X, do Y”" style="display:none">
      <button class="fb-btn" data-fb="send-note" style="display:none">Send</button>
    </div>`;

  let verdict = null;
  el.querySelectorAll("[data-fb]").forEach((btn) => btn.addEventListener("click", async () => {
    const kind = btn.dataset.fb;
    if (kind === "send-note") {
      const note = el.querySelector(".tl-note").value.trim();
      if (!note) return;
      await api("/api/correct", {
        transcript: ev.transcript, skill: actions[0]?.skill || "",
        verdict: "bad", note,
      });
      toast("Taught — it's in the learning set");
      return;
    }
    verdict = kind === "good" ? "good" : "bad";
    el.querySelectorAll("[data-fb]").forEach((b) => b.classList.remove("is-on"));
    btn.classList.add("is-on");
    if (verdict === "bad") {
      el.querySelector(".tl-note").style.display = "";
      el.querySelector('[data-fb="send-note"]').style.display = "";
      el.querySelector(".tl-note").focus();
    } else {
      el.querySelector(".tl-note").style.display = "none";
      el.querySelector('[data-fb="send-note"]').style.display = "none";
      await api("/api/correct", { transcript: ev.transcript, skill: actions[0]?.skill || "", verdict: "good" });
      toast("Reinforced — weighted into the next fine-tune");
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
    $("activity-count") && updateActivityBadge();
  } catch { /* server warming up */ }
}

/* ── skills ───────────────────────────────────────────────────────────── */

async function loadSkills() {
  try {
    const { skills } = await api("/api/skills");
    $("skills-sub").textContent = `${skills.length} skills — each one declared, inspectable, permission-gated.`;
    $("skill-grid").innerHTML = skills.map((s) => `
      <div class="skill-card">
        <span class="risk-pill risk-${esc(s.risk)}">${esc(s.risk)}</span>
        <div class="skill-name">${esc(s.name)}</div>
        <div class="skill-desc">${esc(s.description)}</div>
        ${s.examples[0] ? `<div class="skill-ex">“${esc(s.examples[0])}”</div>` : ""}
      </div>`).join("");
  } catch { /* ignore */ }
}

/* ── settings (live, read-only in v0.1) ───────────────────────────────── */

function renderSettings(st) {
  const rows = (items) => items.map(([k, help, value, control]) => `
    <div class="setting-row">
      <div><div class="setting-label">${k}</div><div class="setting-help">${help}</div></div>
      ${control || `<div class="setting-value">${esc(value)}</div>`}
    </div>`).join("");

  const t = (on) => `<button class="toggle ${on ? "is-on" : ""}" disabled aria-label="toggle"></button>`;
  $("settings-body").innerHTML = `
    <div class="settings-grid">
      <div class="card"><h2>Runtime</h2>${rows([
        ["Profile", "mac brings real executors; demo simulates everywhere", st.profile],
        ["Execution bridge", st.bridge === "mac" ? "macOS (real)" : "dry-run (simulated)", st.bridge],
        ["Wake mode", "manual or always-listening", st.wake_mode],
        ["Data directory", "history, preferences, learned examples", st.data_dir],
      ])}</div>
      <div class="card"><h2>Voice</h2>${rows([
        ["Speak replies", "on-device TTS (say / Kokoro)", "", t(st.tts_enabled)],
        ["Planner model", "any local OpenAI-compatible server", `${st.planner.model} @ ${st.planner.base_url}`],
      ])}</div>
      <div class="card"><h2>Laya decision gate</h2>${rows([
        ["Backend", st.laya.backend === "RealLayaBackend" ? "Laya model loaded (Apache-2.0)" : "heuristic fallback (deterministic rules)", st.laya.backend],
        ["Match threshold", "below this, Aura asks instead of acting", st.laya.confidence ?? "—"],
        ["Learned examples", "auto ✓ + confirmed ✓✓ + corrections ✗ — the fine-tune set",
          `${st.laya.examples.total} total · ${st.laya.examples.confirmed} confirmed · ${st.laya.examples.corrected} corrected · ${st.laya.examples.cancelled} cancelled`],
      ])}</div>
    </div>`;
}

/* ── SSE: the nervous system ──────────────────────────────────────────── */

function handleEvent(type, d) {
  switch (type) {
    case "state": {
      const wasArmed = S.state === "armed";
      setStatus(d.state);
      if (d.state !== "armed") $("live-actions").hidden = false;
      if (d.state === "armed" && wasArmed) {
        // session fully ended
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
      showCaption(d.reply || "Needs your OK.");
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
    case "feedback": toast(`Feedback recorded: ${d.skill}`); break;
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
  es.onopen = () => { $("conn-badge").textContent = S.demo ? "demo mode" : "live · on-device"; };
  es.onerror = () => {
    $("conn-badge").textContent = "reconnecting…";
    setTimeout(() => { es.close(); connect(); }, 2000);
  };
  // SSE dispatch: we set every event's `type` server-side
  const types = ["state", "hint", "transcript", "plan", "proposal", "action_started",
    "action_result", "reply", "feedback", "log"];
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
  try {
    const st = await api("/api/state");
    S.demo = st.bridge !== "mac";
    $("demo-banner").hidden = !S.demo;
    $("version").textContent = `v${st.version}`;
    $("status-meta").textContent =
      `${st.bridge === "mac" ? "macOS bridge" : "dry-run bridge"} · wake: ${st.wake_mode}`;
    renderSettings({ ...st, data_dir: st.data_dir || "—" });
    setStatus(st.state === "starting" ? "armed" : st.state);
  } catch { $("conn-badge").textContent = "offline"; }

  loadSkills();
  loadHistory();
  connect();

  // apply saved wake settings (display-only in v0.1)
  try {
    const st = await api("/api/state");
    $("wake-phrase").value = st.wake_phrase || "";
    document.querySelectorAll("#wake-mode .seg").forEach((seg) =>
      seg.classList.toggle("is-active", seg.dataset.mode === st.wake_mode));
  } catch { /* ignore */ }
})();
