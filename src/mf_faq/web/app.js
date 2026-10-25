/* Fund Facts chat UI (P8.1-P8.7, arch §9.2).
 *
 * - Every piece of text from the server is rendered with textContent / text nodes, never as
 *   HTML. The only anchor is the reply's source link, and only for the 5 Groww page URLs.
 * - Thread ids live in localStorage (wrapped in try/catch: the page works without it).
 * - The PII pre-check mirrors pipeline/pii.py; the server check is still the real one.
 */
"use strict";

const STORE_KEY = "mf-faq.threads";
const ACTIVE_KEY = "mf-faq.active";
const MAX_CHARS = 500;
const ALLOWED_LINK = /^https:\/\/groww\.in\/mutual-funds\/[a-z0-9-]+$/;
const SVG_NS = "http://www.w3.org/2000/svg";

const $ = (id) => document.getElementById(id);
const els = {
  sidebar: $("sidebar"), scrim: $("scrim"), openDrawer: $("open-drawer"),
  closeDrawer: $("close-drawer"), newChat: $("new-chat"), threadList: $("thread-list"),
  threadsEmpty: $("threads-empty"), retention: $("retention"), title: $("thread-title"),
  chat: $("chat"), welcome: $("welcome"), welcomeText: $("welcome-text"),
  schemeList: $("scheme-list"), schemeCount: $("scheme-count"), exampleList: $("example-list"),
  messages: $("messages"), form: $("composer"), input: $("input"), send: $("send"),
  counter: $("counter"), disclaimer: $("disclaimer"), hint: $("pii-hint-text"),
  dialog: $("pii-dialog"), piiTypes: $("pii-types"), piiPreview: $("pii-preview"),
  piiSend: $("pii-send"), piiEdit: $("pii-edit"), piiClose: $("pii-close"),
};

const state = {
  meta: null,
  threads: [], // [{id, title, updated_at, message_count}]
  activeId: null,
  messages: [], // as returned by the API (+ local pending/error entries)
  sending: false,
  pendingText: null, // text held while the PII dialog is open
};

/* -- storage (P8.4) ------------------------------------------------------------------------- */

const storage = {
  get(key, fallback) {
    try {
      const raw = window.localStorage.getItem(key);
      return raw === null ? fallback : JSON.parse(raw);
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      window.localStorage.setItem(key, JSON.stringify(value));
    } catch {
      /* private mode or blocked storage: the sidebar just won't survive a reload */
    }
  },
};

function saveThreads() {
  storage.set(STORE_KEY, state.threads.map((t) => t.id));
  storage.set(ACTIVE_KEY, state.activeId);
}

/* -- API ------------------------------------------------------------------------------------ */

class ApiError extends Error {
  constructor(status, detail) {
    super(detail);
    this.status = status;
  }
}

async function api(method, path, body) {
  let resp;
  try {
    resp = await fetch(path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, "Couldn't reach the server. Check your connection and try again.");
  }
  if (resp.status === 204) return null;
  let data = null;
  try {
    data = await resp.json();
  } catch {
    /* not JSON */
  }
  if (!resp.ok) {
    let detail = data && typeof data.detail === "string" ? data.detail : null;
    if (resp.status === 422) detail = `Please enter a question of 1–${MAX_CHARS} characters.`;
    throw new ApiError(resp.status, detail || "Something went wrong. Please try again.");
  }
  return data;
}

/* -- PII pre-check (P8.5): same patterns as pipeline/pii.py ---------------------------------- */

const VERHOEFF_D = [
  [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
  [3, 4, 0, 1, 2, 8, 9, 5, 6, 7], [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
  [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3], [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
  [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
];
const VERHOEFF_P = [
  [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
  [8, 9, 1, 6, 0, 4, 3, 5, 2, 7], [9, 4, 5, 3, 1, 2, 7, 6, 8, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
  [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
];
const digitsOf = (s) => s.replace(/\D/g, "");
function verhoeffValid(digits) {
  let check = 0;
  [...digits].reverse().forEach((ch, i) => {
    check = VERHOEFF_D[check][VERHOEFF_P[i % 8][Number(ch)]];
  });
  return check === 0;
}

const DOT = String.raw`(?:\s*(?:\(|\[)?\s*dot\s*(?:\)|\])?\s*|\.)`;
const PII_PATTERNS = [
  ["EMAIL", /[\w.%+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}/gi],
  ["EMAIL", new RegExp(
    String.raw`\b[\w.+-]+\s*(?:\(|\[)?\s*\bat\b\s*(?:\)|\])?\s*[\w-]+(?:${DOT}[\w-]+)*` +
    String.raw`\s*(?:\(|\[)?\s*\bdot\b\s*(?:\)|\])?\s*(?:com|in|org|net|co|edu|gov|io)\b`, "gi")],
  ["PAN", /\b[a-z]{5}\s?\d{4}\s?[a-z]\b/gi],
  ["AADHAAR", /(?<![\d-])[2-9]\d{3}([\s-]?)\d{4}\1\d{4}(?![\d-])/g, (m) => verhoeffValid(digitsOf(m))],
  ["AADHAAR", /(?<![\w*])[x*]{4}[\s-]?[x*]{4}[\s-]?\d{4}\b/gi],
  ["PHONE", /(?<![\d+])(?:\+\s?91[\s-]?|91[\s-]|0)?[6-9]\d{4}[\s-]?\d{5}(?!\d)/g],
  ["OTP", new RegExp(
    String.raw`\b(?:otp|one[\s-]time[\s-]password|verification\s+code|security\s+code|pin(?!\s*code)` +
    String.raw`|(?<!scheme\s)(?<!amfi\s)(?<!fund\s)(?<!isin\s)(?<!product\s)code)\b` +
    String.raw`(?:\W+\w+){0,3}?\W+\d{4,8}\b` +
    String.raw`|\b\d{4,8}\b(?:\W+\w+){0,3}?\W+(?:is\s+)?(?:my|the)\s+(?:otp|pin|code)\b`, "gi")],
  ["ACCOUNT", new RegExp(
    String.raw`\b(?:account|acct|a\/c|folio)(?:\s*(?:no|number|num|#))?\.?\s*(?:is\s*)?[:#-]?\s*` +
    String.raw`\d[\d\/-]{3,}\d`, "gi"), (m) => digitsOf(m).length >= 5],
  ["ACCOUNT", /(?<![\d.,/])\d{9,18}(?![\d.,/]|\s*%)/g],
];
const PII_LABELS = {
  EMAIL: "Email", PAN: "PAN", AADHAAR: "Aadhaar", PHONE: "Phone number", OTP: "OTP",
  ACCOUNT: "Account / folio number",
};

function normalize(text) {
  return text.normalize("NFKC").replace(/[­​-‏⁠﻿]/g, "");
}

/** {text, types, ranges}: the normalized text, PII types found and the [start, end) spans. */
function findPii(raw) {
  const text = normalize(raw);
  const types = [];
  const ranges = [];
  for (const [type, pattern, accept] of PII_PATTERNS) {
    for (const m of text.matchAll(pattern)) {
      if (accept && !accept(m[0])) continue;
      if (!types.includes(type)) types.push(type);
      ranges.push([m.index, m.index + m[0].length]);
    }
  }
  ranges.sort((a, b) => a[0] - b[0]);
  const merged = [];
  for (const r of ranges) {
    const last = merged[merged.length - 1];
    if (last && r[0] <= last[1]) last[1] = Math.max(last[1], r[1]);
    else merged.push([...r]);
  }
  return { text, types, ranges: merged };
}

/* -- DOM helpers ---------------------------------------------------------------------------- */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

const ICONS = {
  logo: ["M3 17l5-6 4 4 4-6", "circle:18.5,15.5,3", "m21 18 2 2"],
  info: ["circle:12,12,9", "M12 8v.01", "M12 11v5"],
  shield: ["M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6l-8-3Z", "M12 8v4", "M12 15v.01"],
  search: ["circle:11,11,7", "m20 20-3.5-3.5", "M8.5 8.5l5 5", "M13.5 8.5l-5 5"],
  alert: ["circle:12,12,9", "M12 8v5", "M12 16v.01"],
  ask: ["circle:12,12,9", "M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.7.3-1 .9-1 1.7", "M12 17v.01"],
  external: ["M10 14 21 3", "M15 3h6v6", "M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"],
  trash: ["M4 7h16", "M10 11v6", "M14 11v6", "M6 7l1 13h10l1-13", "M9 7V4h6v3"],
  arrow: ["M5 12h14", "m13 6 6 6-6 6"],
  pct: ["M19 5 5 19", "circle:7,7,2.5", "circle:17,17,2.5"],
};

function icon(name) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  for (const d of ICONS[name]) {
    let shape;
    if (d.startsWith("circle:")) {
      const [cx, cy, r] = d.slice(7).split(",");
      shape = document.createElementNS(SVG_NS, "circle");
      shape.setAttribute("cx", cx);
      shape.setAttribute("cy", cy);
      shape.setAttribute("r", r);
    } else {
      shape = document.createElementNS(SVG_NS, "path");
      shape.setAttribute("d", d);
    }
    svg.append(shape);
  }
  return svg;
}

function clockTime(iso) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function relativeTime(iso) {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const mins = Math.round((Date.now() - then) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  return days === 1 ? "Yesterday" : `${days}d ago`;
}

/** Text with [REDACTED_X] markers shown as small tags (still plain text nodes). */
function appendRedacted(parent, text) {
  const parts = text.split(/(\[REDACTED_[A-Z]+\])/);
  for (const part of parts) {
    if (/^\[REDACTED_[A-Z]+\]$/.test(part)) {
      const label = PII_LABELS[part.slice(10, -1)] || "personal detail";
      parent.append(el("span", "redacted", `${label} removed`));
    } else if (part) {
      parent.append(document.createTextNode(part));
    }
  }
}

/** Answer text with ₹ amounts and percentages emphasised (tabular figures come from CSS). */
function appendFigures(parent, text) {
  const re = /(₹\s?[\d,]+(?:\.\d+)?(?:\s?(?:Cr|crore|lakh))?|\d[\d,]*(?:\.\d+)?\s?%)/g;
  let last = 0;
  for (const m of text.matchAll(re)) {
    if (m.index > last) parent.append(document.createTextNode(text.slice(last, m.index)));
    parent.append(el("span", "fig", m[0]));
    last = m.index + m[0].length;
  }
  if (last < text.length) parent.append(document.createTextNode(text.slice(last)));
}

/* -- rendering: sidebar --------------------------------------------------------------------- */

function renderThreads() {
  els.threadList.replaceChildren();
  els.threadsEmpty.hidden = state.threads.length > 0;
  for (const t of state.threads) {
    const li = el("li", "thread-item");
    const active = t.id === state.activeId;
    if (active) li.classList.add("active");

    const open = el("button", "thread-open");
    open.type = "button";
    if (active) open.setAttribute("aria-current", "true");
    open.append(el("span", "thread-name", t.title || "New chat"));
    const meta = el("span", "thread-meta");
    meta.append(el("span", "when", relativeTime(t.updated_at)));
    const n = t.message_count || 0;
    meta.append(document.createTextNode(` · ${n} message${n === 1 ? "" : "s"}`));
    open.append(meta);
    open.addEventListener("click", () => {
      closeDrawer();
      if (t.id !== state.activeId) openThread(t.id);
    });

    const del = el("button", "icon-btn thread-delete");
    del.type = "button";
    del.setAttribute("aria-label", `Delete chat: ${t.title || "New chat"}`);
    del.append(icon("trash"));
    del.addEventListener("click", () => deleteThread(t.id));

    li.append(open, del);
    els.threadList.append(li);
  }
  const current = state.threads.find((t) => t.id === state.activeId);
  els.title.textContent = (current && current.title) || "New chat";
  document.title = current && current.title ? `${current.title} · Fund Facts` : "Fund Facts";
}

/* -- rendering: welcome (UI-1, UI-2) -------------------------------------------------------- */

function renderMeta() {
  const m = state.meta;
  els.disclaimer.textContent = m.disclaimer;
  els.welcomeText.textContent = m.welcome;
  els.hint.textContent = m.pii_hint;
  els.retention.textContent = `Chats are kept for ${m.thread_ttl_days} days.`;
  els.input.maxLength = m.max_question_chars;

  els.schemeCount.textContent = String(m.schemes.length);
  els.schemeList.replaceChildren();
  for (const s of m.schemes) {
    const li = el("li", `pill${s.scheme_id.includes("silver") ? " silver" : ""}`);
    const dot = el("span", "dot");
    dot.setAttribute("aria-hidden", "true");
    li.append(dot, el("span", null, s.short_name), el("span", "tag", s.category));
    els.schemeList.append(li);
  }

  els.exampleList.replaceChildren();
  for (const ex of m.examples) {
    const li = el("li");
    const btn = el("button", "example");
    btn.type = "button";
    const label = el("span", "example-label");
    label.append(icon(ex.fact === "expense_ratio" ? "pct" : "info"), document.createTextNode(ex.label));
    const go = el("span", "example-go");
    go.append(el("span", null, "Ask this"), icon("arrow"));
    btn.append(label, el("span", "example-q", ex.question), go);
    btn.addEventListener("click", () => submit(ex.question));
    li.append(btn);
    els.exampleList.append(li);
  }
}

/* -- rendering: messages -------------------------------------------------------------------- */

const KIND_HEAD = {
  answer: ["logo", "Fund Facts", "From the Groww scheme page", "chip-teal"],
  help: ["logo", "Fund Facts", "What I can answer", ""],
  refusal: ["info", "I can't advise on that", "Facts only", ""],
  performance: ["info", "Returns aren't shared", "Facts only", ""],
  not_found: ["search", "Not in the scheme pages I use", "", ""],
  pii: ["shield", "Personal details blocked", "Not stored", ""],
  clarify: ["ask", "Which scheme do you mean?", "", ""],
  multi_scheme: ["ask", "One scheme at a time", "", ""],
  error: ["alert", "Something went wrong", "", ""],
};
const NOTICE_KINDS = new Set(["refusal", "performance", "not_found", "help"]);

function renderMessages() {
  els.messages.replaceChildren(...state.messages.map(renderMessage));
  els.welcome.hidden = state.messages.length > 0;
  setBusy(state.sending);
}

function renderMessage(m, index) {
  const li = el("li", `msg msg-${m.role}`);
  if (m.role === "user") {
    const bubble = el("p", "bubble");
    appendRedacted(bubble, m.text);
    li.append(bubble);
    if (m.created_at) li.append(el("span", "time", clockTime(m.created_at)));
    return li;
  }
  if (m.pending) {
    const card = el("div", "reply");
    const dots = el("span", "typing");
    dots.setAttribute("role", "status");
    dots.setAttribute("aria-label", "Looking this up");
    dots.append(el("span"), el("span"), el("span"));
    card.append(dots);
    li.append(card);
    return li;
  }

  const kind = KIND_HEAD[m.kind] ? m.kind : "error";
  const [iconName, title, badge, badgeClass] = KIND_HEAD[kind];
  const card = el("article", `reply kind-${kind}${NOTICE_KINDS.has(kind) ? " notice" : ""}`);
  card.setAttribute("aria-label", title);

  const head = el("div", "reply-head");
  const ic = el("span", "reply-icon");
  ic.append(icon(iconName));
  head.append(ic, el("span", "reply-title", title));
  if (badge) head.append(el("span", `chip ${badgeClass}`, badge));
  card.append(head);

  const body = el("p", "reply-body");
  if (kind === "answer") appendFigures(body, m.text);
  else body.textContent = m.text;
  card.append(body);

  if (kind === "clarify" && state.meta) card.append(schemeChoices(index));

  if (m.source_url || m.last_updated) {
    const foot = el("div", "reply-foot");
    if (m.source_url && ALLOWED_LINK.test(m.source_url)) {
      const a = el("a", "source");
      a.href = m.source_url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.append(icon("external"), document.createTextNode(`Source: ${m.source_title || m.source_url}`));
      foot.append(a);
    }
    if (m.last_updated) foot.append(el("span", "updated", `Last updated from sources: ${m.last_updated}`));
    card.append(foot);
  }
  li.append(card);
  if (m.created_at) li.append(el("span", "time", clockTime(m.created_at)));
  return li;
}

/** Clarify replies: one button per scheme, re-asking the previous question for it. */
function schemeChoices(index) {
  const question = [...state.messages.slice(0, index)].reverse().find((x) => x.role === "user");
  const wrap = el("div");
  wrap.append(el("p", "reply-prompt", "Pick a scheme:"));
  const list = el("ul", "pill-list");
  for (const s of state.meta.schemes) {
    const li = el("li");
    const btn = el("button", "pill", s.short_name);
    btn.type = "button";
    btn.disabled = state.sending || !question || index !== state.messages.length - 1;
    btn.addEventListener("click", () => submit(`${question.text} (${s.short_name})`));
    li.append(btn);
    list.append(li);
  }
  wrap.append(list);
  return wrap;
}

function scrollToEnd() {
  requestAnimationFrame(() => {
    els.chat.scrollTop = els.chat.scrollHeight;
  });
}

/* -- composer (P8.6) ------------------------------------------------------------------------ */

function setBusy(busy) {
  state.sending = busy;
  const empty = els.input.value.trim().length === 0;
  els.send.disabled = busy || empty;
  els.send.setAttribute("aria-label", busy ? "Waiting for the reply" : "Send");
  for (const b of els.exampleList.querySelectorAll("button")) b.disabled = busy;
}

function updateCounter() {
  const n = els.input.value.length;
  const max = els.input.maxLength > 0 ? els.input.maxLength : MAX_CHARS;
  els.counter.textContent = `${n}/${max}`;
  els.counter.classList.toggle("near", n > max * 0.9);
  els.input.style.height = "auto";
  els.input.style.height = `${els.input.scrollHeight}px`;
  setBusy(state.sending);
}

function submit(text, { confirmed = false } = {}) {
  const question = text.trim();
  if (!question || state.sending) return;
  if (!confirmed) {
    const pii = findPii(question);
    if (pii.types.length) {
      openPiiDialog(question, pii);
      return;
    }
  }
  sendQuestion(question);
}

async function sendQuestion(question) {
  setBusy(true);
  const typed = els.input.value;
  els.input.value = "";
  updateCounter();

  const local = { role: "user", text: question, created_at: new Date().toISOString() };
  const pending = { role: "assistant", pending: true };
  state.messages.push(local, pending);
  renderMessages();
  scrollToEnd();

  try {
    if (!state.activeId) {
      const thread = await api("POST", "/api/threads");
      state.threads.unshift(thread);
      state.activeId = thread.id;
      saveThreads();
    }
    const reply = await api("POST", `/api/threads/${state.activeId}/messages`, { text: question });
    local.text = reply.user_text; // as stored: PII masked
    state.messages.splice(state.messages.indexOf(pending), 1, {
      role: "assistant",
      kind: reply.kind,
      text: reply.answer,
      source_url: reply.source_url,
      source_title: reply.source_title,
      last_updated: reply.last_updated,
      created_at: reply.created_at,
    });
    upsertThread(reply.thread);
  } catch (err) {
    state.messages.splice(state.messages.indexOf(pending), 1);
    state.messages.splice(state.messages.indexOf(local), 1);
    if (err.status === 404) {
      forgetThread(state.activeId);
    }
    state.messages.push({ role: "assistant", kind: "error", text: err.message, local: true });
    if (!els.input.value) els.input.value = typed || question; // nothing typed is lost
  } finally {
    setBusy(false);
    updateCounter();
    renderMessages();
    renderThreads();
    scrollToEnd();
    els.input.focus();
  }
}

function upsertThread(thread) {
  state.threads = [thread, ...state.threads.filter((t) => t.id !== thread.id)];
  saveThreads();
}

/* -- threads (P8.4) ------------------------------------------------------------------------- */

async function openThread(id) {
  state.activeId = id;
  state.messages = [];
  saveThreads();
  renderThreads();
  renderMessages();
  try {
    const data = await api("GET", `/api/threads/${id}/messages`);
    if (state.activeId !== id) return; // the user moved on meanwhile
    state.messages = data.messages;
  } catch (err) {
    if (err.status === 404) {
      forgetThread(id);
    } else {
      state.messages = [{ role: "assistant", kind: "error", text: err.message, local: true }];
    }
  }
  renderMessages();
  renderThreads();
  scrollToEnd();
}

function forgetThread(id) {
  state.threads = state.threads.filter((t) => t.id !== id);
  if (state.activeId === id) {
    state.activeId = null;
    state.messages = [];
  }
  saveThreads();
}

function newChat() {
  closeDrawer();
  state.activeId = null;
  state.messages = [];
  saveThreads();
  renderThreads();
  renderMessages();
  els.chat.scrollTop = 0;
  els.input.focus();
}

async function deleteThread(id) {
  const t = state.threads.find((x) => x.id === id);
  if (!window.confirm(`Delete "${(t && t.title) || "New chat"}"? This can't be undone.`)) return;
  try {
    await api("DELETE", `/api/threads/${id}`);
  } catch (err) {
    if (err.status !== 404) {
      window.alert(err.message);
      return;
    }
  }
  forgetThread(id);
  renderThreads();
  renderMessages();
}

async function loadThreads() {
  const ids = storage.get(STORE_KEY, []);
  const active = storage.get(ACTIVE_KEY, null);
  const valid = Array.isArray(ids) ? ids.filter((x) => typeof x === "string").slice(0, 100) : [];
  if (valid.length) {
    try {
      const data = await api("GET", `/api/threads?ids=${encodeURIComponent(valid.join(","))}`);
      state.threads = data.threads; // purged or deleted threads drop out here
    } catch {
      state.threads = [];
    }
  }
  saveThreads();
  renderThreads();
  if (active && state.threads.some((t) => t.id === active)) await openThread(active);
  else renderMessages();
}

/* -- PII dialog (P8.5) ---------------------------------------------------------------------- */

function openPiiDialog(question, pii) {
  state.pendingText = question;
  els.piiTypes.replaceChildren(
    icon("alert"),
    document.createTextNode(`Detected: ${pii.types.map((t) => PII_LABELS[t]).join(" / ")}`),
  );
  els.piiPreview.replaceChildren();
  let last = 0;
  for (const [start, end] of pii.ranges) {
    if (start > last) els.piiPreview.append(document.createTextNode(pii.text.slice(last, start)));
    els.piiPreview.append(el("mark", null, pii.text.slice(start, end)));
    last = end;
  }
  if (last < pii.text.length) els.piiPreview.append(document.createTextNode(pii.text.slice(last)));

  if (typeof els.dialog.showModal === "function") {
    els.dialog.showModal();
  } else if (window.confirm("This looks like personal information. Send anyway?")) {
    sendPending();
  }
}

function sendPending() {
  const text = state.pendingText;
  state.pendingText = null;
  if (text) submit(text, { confirmed: true });
}

function closePiiDialog() {
  if (els.dialog.open) els.dialog.close();
}

/* -- drawer (phone/tablet) ------------------------------------------------------------------ */

function openDrawer() {
  els.sidebar.classList.add("open");
  els.scrim.hidden = false;
  els.openDrawer.setAttribute("aria-expanded", "true");
  els.newChat.focus();
}

function closeDrawer() {
  if (!els.sidebar.classList.contains("open")) return;
  els.sidebar.classList.remove("open");
  els.scrim.hidden = true;
  els.openDrawer.setAttribute("aria-expanded", "false");
  els.openDrawer.focus();
}

/* -- wiring --------------------------------------------------------------------------------- */

function bind() {
  els.form.addEventListener("submit", (e) => {
    e.preventDefault();
    submit(els.input.value);
  });
  els.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      submit(els.input.value);
    }
  });
  els.input.addEventListener("input", updateCounter);
  els.newChat.addEventListener("click", newChat);
  els.openDrawer.addEventListener("click", openDrawer);
  els.closeDrawer.addEventListener("click", closeDrawer);
  els.scrim.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeDrawer();
  });

  els.piiEdit.addEventListener("click", () => {
    closePiiDialog();
    if (!els.input.value) els.input.value = state.pendingText || "";
    state.pendingText = null;
    updateCounter();
    els.input.focus();
  });
  els.piiClose.addEventListener("click", () => els.piiEdit.click());
  els.dialog.addEventListener("cancel", (e) => {
    e.preventDefault();
    els.piiEdit.click();
  });
  els.piiSend.addEventListener("click", () => {
    closePiiDialog();
    sendPending();
  });
  // Relative times in the sidebar stay fresh.
  setInterval(renderThreads, 60_000);
}

async function init() {
  bind();
  updateCounter();
  try {
    state.meta = await api("GET", "/api/meta");
    renderMeta();
  } catch (err) {
    state.messages = [{ role: "assistant", kind: "error", text: err.message, local: true }];
    renderMessages();
    return;
  }
  await loadThreads();
  els.input.focus();
}

init();
