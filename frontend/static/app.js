// University Portal - vanilla JS client for the gateway API.
// All data is rendered with textContent (never innerHTML) so stored input cannot run as script.
"use strict";

const $ = (id) => document.getElementById(id);
const state = { token: null, role: null, sid: null, viewing: null, liveTimer: null, live: [] };

function el(tag, props = {}, children = []) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") n.className = v; else n.setAttribute(k, v);
  }
  for (const c of [].concat(children)) n.append(c instanceof Node ? c : document.createTextNode(c ?? ""));
  return n;
}

function uuid() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID().replace(/-/g, "");
  return Array.from({ length: 32 }, () => Math.floor(Math.random() * 16).toString(16)).join("");
}

// ------------------------------------------------------------------ API
async function api(path, { method = "GET", body, headers = {} } = {}) {
  const h = { ...headers };
  if (state.token) h.Authorization = `Bearer ${state.token}`;
  if (body !== undefined) h["Content-Type"] = "application/json";
  const t0 = performance.now();
  let res;
  try {
    res = await fetch(path, { method, headers: h, body: body !== undefined ? JSON.stringify(body) : undefined });
  } catch (e) {
    return { ok: false, status: 0, data: { error: "network error" }, ms: performance.now() - t0, servedBy: "-" };
  }
  let data = null;
  try { data = await res.json(); } catch { data = null; }
  const out = { ok: res.ok, status: res.status, data, ms: performance.now() - t0,
                servedBy: res.headers.get("X-Served-By") || "-", requestId: res.headers.get("X-Request-ID") || "" };
  if (res.status === 401 && state.token && !path.startsWith("/api/auth")) logout("Your session expired. Please sign in again.");
  return out;
}

function errText(r) {
  if (r.status === 0) return "The service cannot be reached.";
  if (r.status === 429) return "Too many requests - please slow down.";
  if (r.status === 403) return "You are not allowed to do this.";
  if (r.status === 503) return `Service temporarily unavailable (${(r.data && (r.data.dependency || r.data.error)) || "dependency"}). Please retry.`;
  if (r.status === 422) return "Invalid input: " + ((r.data && r.data.details || []).map((d) => d.msg).join("; ") || "check the fields");
  return `Request failed (HTTP ${r.status}).`;
}

// ---------------------------------------------------------------- login
async function login(ev) {
  ev.preventDefault();
  $("login-error").classList.add("hidden");
  const r = await api("/api/auth/login", { method: "POST", body: { username: $("username").value.trim(), password: $("password").value } });
  if (!r.ok) {
    $("login-error").textContent = r.status === 401 ? "Wrong username or password." : errText(r);
    $("login-error").classList.remove("hidden");
    return;
  }
  state.token = r.data.access_token;
  state.role = r.data.role;
  state.sid = r.data.student_id;
  sessionStorage.setItem("portal", JSON.stringify({ token: state.token, role: state.role, sid: state.sid, user: $("username").value.trim() }));
  $("password").value = "";
  enterApp($("username").value.trim());
}

function enterApp(user) {
  $("login-view").classList.add("hidden");
  $("app-view").classList.remove("hidden");
  $("userbox").classList.remove("hidden");
  $("who").textContent = `${user} (${state.role})`;
  const staff = state.role === "admin" || state.role === "registrar";
  $("staff-bar").classList.toggle("hidden", !staff);
  $("grade-card").classList.toggle("hidden", !staff);
  $("audit-card").classList.toggle("hidden", state.role !== "admin");
  state.viewing = state.sid || Number($("lookup-id").value);
  loadAll();
}

function logout(msg) {
  state.token = null;
  sessionStorage.removeItem("portal");
  stopLive();
  $("app-view").classList.add("hidden");
  $("userbox").classList.add("hidden");
  $("login-view").classList.remove("hidden");
  if (msg) { $("login-error").textContent = msg; $("login-error").classList.remove("hidden"); }
}

// ----------------------------------------------------------------- views
function banner(kind, text) {
  const b = $("banner");
  if (!text) { b.classList.add("hidden"); return; }
  b.className = `banner ${kind}`;
  b.textContent = text;
}

function table(headers, rows) {
  if (!rows.length) return el("p", { class: "muted" }, "Nothing here yet.");
  return el("div", { class: "table-wrap" }, el("table", {}, [
    el("thead", {}, el("tr", {}, headers.map((h) => el("th", {}, h)))),
    el("tbody", {}, rows.map((r) => el("tr", {}, r.map((c) => el("td", {}, c))))),
  ]));
}

async function loadAll() {
  const id = state.viewing;
  const [dash, tr, pays] = await Promise.all([
    api(`/api/dashboard/${id}`), api(`/api/records/transcripts/${id}`), api(`/api/payments?student_id=${id}`),
  ]);
  const notes = [];

  // dashboard: profile (critical) + account and timetable (may be degraded)
  if (dash.ok) {
    const p = dash.data.profile;
    $("profile").replaceChildren(...[["Name", `${p.first_name} ${p.last_name}`], ["Student ID", p.id], ["Email", p.email],
      ["Program", p.program], ["Year", p.year], ["Status", p.status]].flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, String(v))]));
    const a = dash.data.account;
    $("account").replaceChildren(a
      ? el("div", {}, [el("div", { class: "muted small" }, "Outstanding balance"), el("div", { class: "big" }, `$${a.balance}`),
        el("div", { class: "muted small" }, `Tuition ${a.tuition_due}, paid ${a.total_paid}`)])
      : el("p", { class: "muted" }, "Balance is temporarily unavailable."));
    const t = dash.data.timetable;
    $("timetable").replaceChildren(t
      ? table(["Day", "Time", "Course", "Title", "Room"], t.classes.map((c) => [c.day, c.start_time, c.course_code, c.title, c.room]))
      : el("p", { class: "muted" }, "The timetable service is temporarily unavailable. Everything else keeps working."));
    if (dash.data.degraded && dash.data.degraded.length) notes.push(`Some information is temporarily unavailable: ${dash.data.degraded.join(", ")}.`);
  } else {
    $("profile").replaceChildren(el("dd", { class: "error" }, errText(dash)));
    $("account").replaceChildren();
    $("timetable").replaceChildren();
  }

  if (tr.ok) {
    $("gpa").textContent = tr.data.gpa !== null ? `GPA ${tr.data.gpa.toFixed(2)}` : "no grades";
    $("transcript").replaceChildren(table(["Term", "Course", "Title", "Credits", "Grade"],
      tr.data.courses.map((c) => [c.term, c.course_code, c.title, String(c.credits), c.grade])));
    if (tr.data.degraded && tr.data.degraded.length) notes.push(`Transcript served in degraded mode (${tr.data.degraded.join(", ")}).`);
  } else {
    $("gpa").textContent = "";
    $("transcript").replaceChildren(el("p", { class: "error" }, errText(tr)));
  }

  if (pays.ok) {
    $("payments").replaceChildren(table(["#", "Date", "Amount", "Method", "Term"],
      pays.data.items.map((x) => [String(x.payment_id), x.created_at.slice(0, 19).replace("T", " "), `$${x.amount}`, x.method, x.term])));
  } else {
    $("payments").replaceChildren(el("p", { class: "error" }, errText(pays)));
  }

  if (state.role === "admin") loadAudit();
  const failed = [dash, tr, pays].filter((r) => !r.ok);
  if (failed.length) banner("err", errText(failed[0]));
  else banner("warn", notes.join(" "));
}

async function loadAudit() {
  const r = await api("/api/admin/audit?limit=15");
  $("audit").replaceChildren(r.ok
    ? table(["Time", "Service", "Actor", "Action", "Outcome", "Resource"],
      r.data.items.map((e) => [e.ts.slice(11, 19), e.service, e.actor, e.action, e.outcome, e.resource || ""]))
    : el("p", { class: "error" }, errText(r)));
}

// --------------------------------------------------------------- actions
async function pay(ev) {
  ev.preventDefault();
  const btn = ev.submitter || ev.target.querySelector("button");
  btn.disabled = true;
  const key = uuid();  // one key per logical payment: retries reuse it, so the charge happens once
  const body = { student_id: state.viewing, amount: Number($("pay-amount").value).toFixed(2), term: "Fall-2026", method: $("pay-method").value };
  let r;
  for (let attempt = 1; attempt <= 3; attempt++) {
    r = await api("/api/payments", { method: "POST", body, headers: { "Idempotency-Key": key } });
    if (r.ok || (r.status >= 400 && r.status < 500 && r.status !== 429)) break;
    $("pay-msg").textContent = `Attempt ${attempt} failed (${r.status || "network"}), retrying with the same Idempotency-Key...`;
    await new Promise((res) => setTimeout(res, 400 * attempt));
  }
  btn.disabled = false;
  $("pay-msg").textContent = r.ok
    ? `Payment #${r.data.payment_id} confirmed${r.status === 200 ? " (duplicate request recognised - charged once)" : ""}. Key ${key.slice(0, 8)}...`
    : errText(r);
  loadAll();
}

async function saveGrade(ev) {
  ev.preventDefault();
  const r = await api("/api/records/grades", { method: "POST", body: {
    student_id: state.viewing, course_code: $("g-course").value.trim(), term: $("g-term").value.trim(), grade: $("g-grade").value } });
  $("grade-msg").textContent = r.ok ? "Grade saved and recorded in the audit log." : errText(r);
  if (r.ok) loadAll();
}

// ----------------------------------------------------------- live monitor
async function liveTick() {
  const r = await api(`/api/students/${state.viewing}`);
  state.live.push({ ok: r.ok, ms: r.ms, by: r.servedBy, status: r.status });
  if (state.live.length > 60) state.live.shift();
  $("live-bars").replaceChildren(...state.live.map((x) => {
    const h = Math.max(8, Math.min(48, 8 + Math.log10(Math.max(1, x.ms)) * 14));
    const s = el("span", { class: x.ok ? "" : "err", title: `${x.status} - ${x.ms.toFixed(0)} ms - ${x.by}` });
    s.style.height = `${h}px`;
    return s;
  }));
  const ok = state.live.filter((x) => x.ok).length;
  const by = {};
  state.live.forEach((x) => { by[x.by] = (by[x.by] || 0) + 1; });
  $("live-stats").textContent = `Last ${state.live.length} requests: ${ok} OK, ${state.live.length - ok} failed. ` +
    `Latest: HTTP ${r.status}, ${r.ms.toFixed(0)} ms, served by ${r.servedBy}. ` +
    `Instances: ${Object.entries(by).map(([k, v]) => `${k} x${v}`).join(", ")}.`;
}

function stopLive() {
  if (state.liveTimer) clearInterval(state.liveTimer);
  state.liveTimer = null;
  $("live").checked = false;
}

// ------------------------------------------------------------------ init
document.addEventListener("DOMContentLoaded", () => {
  $("login-form").addEventListener("submit", login);
  $("logout").addEventListener("click", () => logout());
  $("pay-form").addEventListener("submit", pay);
  $("grade-form").addEventListener("submit", saveGrade);
  $("lookup-btn").addEventListener("click", () => { state.viewing = Number($("lookup-id").value); state.live = []; loadAll(); });
  $("live").addEventListener("change", (e) => {
    if (e.target.checked) { state.live = []; state.liveTimer = setInterval(liveTick, 500); } else stopLive();
  });
  try {
    const saved = JSON.parse(sessionStorage.getItem("portal") || "null");
    if (saved && saved.token) {
      Object.assign(state, { token: saved.token, role: saved.role, sid: saved.sid });
      enterApp(saved.user);
    }
  } catch { /* storage unavailable: start at the login form */ }
});
