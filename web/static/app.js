"use strict";

// --------------------------------------------------------------------------- //
// Helpers
// --------------------------------------------------------------------------- //
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

async function apiGet(path) {
  const r = await fetch(path, { headers: { "Accept": "application/json" } });
  if (r.status === 401) { location.href = "/login"; return null; }
  return r.json();
}
async function apiPost(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body === undefined ? {} : body),
  });
  if (r.status === 401) { location.href = "/login"; return null; }
  const data = await r.json().catch(() => ({}));
  return { ok: r.ok, data };
}

let toastTimer = null;
function toast(msg, kind = "ok") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast " + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 3000);
}

function showRestartBanner() {
  $("#restart-banner").classList.remove("hidden");
}

let restarting = false;
function waitForRestart() {
  if (restarting) return;
  restarting = true;
  $("#restart-banner").classList.add("hidden");
  $("#restarting-banner").classList.remove("hidden");
  // The service restarts ~1.5s after replying; give it a head start, then poll
  // until it answers again and reload.
  const poll = () => {
    fetch("/api/status", { cache: "no-store" })
      .then((r) => { if (r.ok || r.status === 401) location.reload(); else setTimeout(poll, 2000); })
      .catch(() => setTimeout(poll, 2000));
  };
  setTimeout(poll, 3500);
}

async function saveSection(path, body, okMsg) {
  const res = await apiPost(path, body);
  if (!res) return;
  if (res.ok) {
    toast(okMsg || "Збережено", "ok");
    const d = res.data || {};
    if (d.restarting) waitForRestart();
    else if (d.restart_required) showRestartBanner();
  } else {
    toast((res.data && res.data.error) || "Помилка збереження", "err");
  }
}

// --------------------------------------------------------------------------- //
// Tabs
// --------------------------------------------------------------------------- //
$$(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    $$(".tab").forEach((t) => t.classList.remove("active"));
    $$(".panel").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    $("#tab-" + tab.dataset.tab).classList.add("active");
  });
});

// --------------------------------------------------------------------------- //
// Config state (accounts needed to build target dropdowns)
// --------------------------------------------------------------------------- //
let currentAccounts = [];

// --------------------------------------------------------------------------- //
// Accounts
// --------------------------------------------------------------------------- //
function accountRow(acc = {}) {
  const div = document.createElement("div");
  div.className = "card account-row";
  div.innerHTML = `
    <div class="row-head">
      <strong>Акаунт</strong>
      <button type="button" class="remove-btn">Видалити</button>
    </div>
    <div class="row">
      <label>ID (унікальний)
        <input class="f-id" type="text" placeholder="acc1" value="${esc(acc.id)}">
      </label>
      <label>Ім'я
        <input class="f-name" type="text" placeholder="Intercom" value="${esc(acc.display_name)}">
      </label>
    </div>
    <div class="row">
      <label>Користувач
        <input class="f-user" type="text" placeholder="1001" value="${esc(acc.username)}">
      </label>
      <label>Auth-user (опц.)
        <input class="f-authuser" type="text" value="${esc(acc.auth_user)}">
      </label>
    </div>
    <div class="row">
      <label>Домен / SIP-сервер
        <input class="f-domain" type="text" placeholder="pbx.example.com" value="${esc(acc.domain)}">
      </label>
      <label>Пароль
        <input class="f-pass" type="password" placeholder="••••••" value="${acc.password ? "********" : ""}">
      </label>
    </div>
    <div class="row">
      <label>Registrar (опц.)
        <input class="f-registrar" type="text" value="${esc(acc.registrar)}">
      </label>
      <label>Proxy (опц.)
        <input class="f-proxy" type="text" value="${esc(acc.proxy)}">
      </label>
    </div>
    <label class="switch-row">
      <input class="f-enabled" type="checkbox" ${acc.enabled === false ? "" : "checked"}> Увімкнено
    </label>
  `;
  div.querySelector(".remove-btn").addEventListener("click", () => div.remove());
  return div;
}

function renderAccounts(list) {
  const wrap = $("#accounts-list");
  wrap.innerHTML = "";
  list.forEach((a) => wrap.appendChild(accountRow(a)));
}

function collectAccounts() {
  return $$("#accounts-list .account-row").map((row) => ({
    id: row.querySelector(".f-id").value.trim(),
    display_name: row.querySelector(".f-name").value.trim(),
    username: row.querySelector(".f-user").value.trim(),
    auth_user: row.querySelector(".f-authuser").value.trim(),
    domain: row.querySelector(".f-domain").value.trim(),
    password: row.querySelector(".f-pass").value,
    registrar: row.querySelector(".f-registrar").value.trim(),
    proxy: row.querySelector(".f-proxy").value.trim(),
    enabled: row.querySelector(".f-enabled").checked,
  }));
}

$("#add-account").addEventListener("click", () => {
  if ($$("#accounts-list .account-row").length >= 3) {
    toast("Максимум 3 акаунти", "err");
    return;
  }
  $("#accounts-list").appendChild(accountRow({ id: "acc" + ($$("#accounts-list .account-row").length + 1) }));
});

$("#save-accounts").addEventListener("click", async () => {
  const accounts = collectAccounts();
  const ids = accounts.map((a) => a.id);
  if (ids.some((id) => !id)) { toast("У кожного акаунта має бути ID", "err"); return; }
  if (new Set(ids).size !== ids.length) { toast("ID акаунтів мають бути унікальні", "err"); return; }
  await saveSection("/api/accounts", accounts, "Акаунти збережено");
  currentAccounts = accounts;
});

// --------------------------------------------------------------------------- //
// Targets
// --------------------------------------------------------------------------- //
function accountOptions(selected) {
  return currentAccounts
    .map((a) => `<option value="${esc(a.id)}" ${a.id === selected ? "selected" : ""}>${esc(a.id)} (${esc(a.username)})</option>`)
    .join("");
}

function targetRow(t = {}) {
  const div = document.createElement("div");
  div.className = "card target-row";
  div.innerHTML = `
    <div class="row-head">
      <strong>Номер</strong>
      <button type="button" class="remove-btn">Видалити</button>
    </div>
    <div class="row">
      <label>Пріоритет
        <input class="f-prio" type="number" min="1" max="99" value="${t.priority || 1}">
      </label>
      <label>Мітка
        <input class="f-label" type="text" placeholder="Ресепшн" value="${esc(t.label)}">
      </label>
    </div>
    <div class="row">
      <label>Номер / SIP-адреса
        <input class="f-number" type="text" placeholder="2001" value="${esc(t.number)}">
      </label>
      <label>Дзвонити з акаунта
        <select class="f-account">${accountOptions(t.account_id)}</select>
      </label>
    </div>
    <label class="switch-row">
      <input class="f-enabled" type="checkbox" ${t.enabled === false ? "" : "checked"}> Увімкнено
    </label>
  `;
  div.querySelector(".remove-btn").addEventListener("click", () => div.remove());
  return div;
}

function renderTargets(list) {
  const wrap = $("#targets-list");
  wrap.innerHTML = "";
  [...list].sort((a, b) => (a.priority || 99) - (b.priority || 99)).forEach((t) => wrap.appendChild(targetRow(t)));
}

function collectTargets() {
  return $$("#targets-list .target-row").map((row) => ({
    priority: parseInt(row.querySelector(".f-prio").value, 10) || 1,
    label: row.querySelector(".f-label").value.trim(),
    number: row.querySelector(".f-number").value.trim(),
    account_id: row.querySelector(".f-account").value,
    enabled: row.querySelector(".f-enabled").checked,
  }));
}

$("#add-target").addEventListener("click", () => {
  if (currentAccounts.length === 0) { toast("Спочатку додайте акаунт", "err"); return; }
  const n = $$("#targets-list .target-row").length + 1;
  $("#targets-list").appendChild(targetRow({ priority: n }));
});

$("#save-targets").addEventListener("click", async () => {
  const targets = collectTargets();
  if (targets.some((t) => !t.number)) { toast("Заповніть номер у кожному рядку", "err"); return; }
  await saveSection("/api/targets", targets, "Номери збережено");
  await saveSection("/api/dial", { ring_timeout: parseInt($("#ring-timeout").value, 10) });
});

// --------------------------------------------------------------------------- //
// Incoming / Audio / GPIO / System
// --------------------------------------------------------------------------- //
$("#save-incoming").addEventListener("click", () => saveSection("/api/incoming", {
  auto_answer: $("#in-auto").checked,
  answer_after_rings: parseInt($("#in-rings").value, 10) || 0,
  busy_when_in_call: $("#in-busy").checked,
}, "Налаштування вхідних збережено"));

$("#save-audio").addEventListener("click", () => saveSection("/api/audio", {
  capture_dev: $("#au-capture").value.trim(),
  playback_dev: $("#au-playback").value.trim(),
  tx_gain: parseFloat($("#au-tx").value),
  rx_gain: parseFloat($("#au-rx").value),
  ec_tail_ms: parseInt($("#au-ec").value, 10) || 0,
}, "Аудіо збережено"));

$("#save-gpio").addEventListener("click", () => saveSection("/api/gpio", {
  button_pin: parseInt($("#gp-button").value, 10),
  led_pin: parseInt($("#gp-led").value, 10),
  active_low: $("#gp-active-low").checked,
  debounce_ms: parseInt($("#gp-debounce").value, 10) || 0,
}, "GPIO збережено"));

$("#save-web").addEventListener("click", () => saveSection("/api/web", {
  username: $("#web-username").value.trim(),
  port: parseInt($("#web-port").value, 10),
}, "Налаштування веб збережено"));

$("#save-password").addEventListener("click", async () => {
  const res = await apiPost("/api/password", {
    current: $("#pw-current").value,
    new: $("#pw-new").value,
  });
  if (!res) return;
  if (res.ok) { toast("Пароль змінено", "ok"); $("#pw-current").value = ""; $("#pw-new").value = ""; }
  else toast((res.data && res.data.error) || "Помилка", "err");
});

// --------------------------------------------------------------------------- //
// Control buttons
// --------------------------------------------------------------------------- //
$("#btn-call").addEventListener("click", () => apiPost("/api/control/call"));
$("#btn-hangup").addEventListener("click", () => apiPost("/api/control/hangup"));
$("#btn-press").addEventListener("click", () => apiPost("/api/control/button"));

if (window.IS_MOCK) {
  let lastCallId = null;
  window.__setLastCall = (id) => { lastCallId = id; };
  const si = $("#sim-incoming");
  if (si) si.addEventListener("click", async () => {
    const r = await apiPost("/api/sim/incoming", {});
    if (r && r.data && r.data.call_id) lastCallId = r.data.call_id;
  });
  const sa = $("#sim-answer");
  if (sa) sa.addEventListener("click", () => apiPost("/api/sim/answer", { call_id: lastCallId }));
  const sh = $("#sim-remote-hangup");
  if (sh) sh.addEventListener("click", () => apiPost("/api/sim/remote-hangup", { call_id: lastCallId }));
}

// --------------------------------------------------------------------------- //
// Call log
// --------------------------------------------------------------------------- //
const RESULT_LABELS = {
  answered: "Відповіли",
  busy: "Зайнято",
  rejected: "Відхилено",
  "no answer": "Не відповіли",
  canceled: "Скасовано",
  failed: "Помилка",
};

function fmtTime(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleString("uk-UA");
}
function fmtDur(s) {
  if (!s) return "—";
  const m = Math.floor(s / 60), sec = s % 60;
  return (m ? m + " хв " : "") + sec + " с";
}
function shortNum(remote) {
  if (!remote) return "";
  const m = String(remote).match(/sip:([^@;>]+)@?/);
  return m ? m[1] : remote;
}

function renderCalls(list) {
  const body = $("#calls-body");
  body.innerHTML = "";
  $("#calls-empty").classList.toggle("hidden", list.length > 0);
  list.forEach((c) => {
    const tr = document.createElement("tr");
    const dir = c.direction === "in" ? "Вхідний" : "Вихідний";
    const dirCls = c.direction === "in" ? "dir-in" : "dir-out";
    const resKey = c.result || "failed";
    const resCls = resKey.replace(/ /g, "-");
    tr.innerHTML = `
      <td class="num">${esc(fmtTime(c.started_at))}</td>
      <td class="${dirCls}">${dir}</td>
      <td>${esc(shortNum(c.remote))}</td>
      <td>${esc(c.account_id || "")}</td>
      <td><span class="res ${resCls}">${esc(RESULT_LABELS[resKey] || resKey)}</span></td>
      <td class="num">${esc(fmtDur(c.duration))}</td>`;
    body.appendChild(tr);
  });
}

async function loadCalls() {
  const list = await apiGet("/api/calls");
  if (list) renderCalls(list);
}

$("#clear-calls").addEventListener("click", async () => {
  await apiPost("/api/calls/clear");
  loadCalls();
});

// --------------------------------------------------------------------------- //
// Live status
// --------------------------------------------------------------------------- //
const STATE_LABELS = {
  idle: "Очікування",
  dialing: "Набір…",
  ringing_in: "Вхідний виклик…",
  in_call: "Розмова",
};

function renderState(state, call) {
  const dot = $("#state-dot");
  dot.className = "dot " + (state || "idle");
  $("#state-text").textContent = STATE_LABELS[state] || state || "—";
  const info = $("#call-info");
  if (call && call.remote) {
    const dir = call.direction === "in" ? "вхідний" : "вихідний";
    info.textContent = `${dir}: ${call.remote} (${call.account_id || "?"})`;
    if (window.IS_MOCK && call.call_id && window.__setLastCall) window.__setLastCall(call.call_id);
  } else {
    info.textContent = "";
  }
}

function renderRegistrations(regs) {
  const body = $("#reg-body");
  body.innerHTML = "";
  const ids = Object.keys(regs || {});
  if (ids.length === 0) {
    body.innerHTML = `<tr><td colspan="3" class="muted">Немає акаунтів</td></tr>`;
    return;
  }
  ids.forEach((id) => {
    const r = regs[id];
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${esc(id)}</td>
      <td class="${r.registered ? "reg-ok" : "reg-bad"}">${r.registered ? "● Зареєстровано" : "○ Не зареєстровано"}</td>
      <td>${r.code || ""} ${esc(r.reason || "")}</td>`;
    body.appendChild(tr);
  });
}

function connectEvents() {
  const es = new EventSource("/api/events");
  es.onmessage = (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch { return; }
    if (data.type === "status") {
      renderState(data.state, data.call);
    } else if (data.type === "reg") {
      // Pull the full registration snapshot to keep the table consistent.
      apiGet("/api/status").then((s) => s && renderRegistrations(s.registrations));
    } else if (data.type === "incoming_busy") {
      toast("Вхідний відхилено (зайнято): " + (data.remote || ""), "err");
    } else if (data.type === "calllog") {
      loadCalls();
    }
  };
  es.onerror = () => { /* EventSource auto-reconnects */ };
}

// --------------------------------------------------------------------------- //
// Init
// --------------------------------------------------------------------------- //
function esc(v) {
  if (v === undefined || v === null) return "";
  return String(v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function init() {
  const cfg = await apiGet("/api/config");
  if (!cfg) return;
  currentAccounts = cfg.sip.accounts || [];
  renderAccounts(currentAccounts);
  renderTargets(cfg.dial.targets || []);
  $("#ring-timeout").value = cfg.dial.ring_timeout;

  $("#in-auto").checked = cfg.incoming.auto_answer;
  $("#in-rings").value = cfg.incoming.answer_after_rings;
  $("#in-busy").checked = cfg.incoming.busy_when_in_call;

  $("#au-capture").value = cfg.audio.capture_dev;
  $("#au-playback").value = cfg.audio.playback_dev;
  $("#au-tx").value = cfg.audio.tx_gain;
  $("#au-rx").value = cfg.audio.rx_gain;
  $("#au-ec").value = cfg.audio.ec_tail_ms;

  $("#gp-button").value = cfg.gpio.button_pin;
  $("#gp-led").value = cfg.gpio.led_pin;
  $("#gp-active-low").checked = cfg.gpio.active_low;
  $("#gp-debounce").value = cfg.gpio.debounce_ms;

  $("#web-username").value = cfg.web.username;
  $("#web-port").value = cfg.web.port;

  const status = await apiGet("/api/status");
  if (status) {
    renderState(status.state, status.call);
    renderRegistrations(status.registrations);
  }
  await loadCalls();
  connectEvents();
}

init();
