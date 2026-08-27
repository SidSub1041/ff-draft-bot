"use strict";

/* ff-draft-bot companion panel.
   Vanilla JS, no build step, no network beyond this server.

   Two rules run through the whole file:
     1. every /api/* request carries X-FFBot-Token, and
     2. server data NEVER touches innerHTML - player names come from a PDF
        and the Sleeper API, and the chat echoes the user's own words back
        into the transcript, so all text lands via textContent.

   The shape of the UI: a board on the left, best-by-position over a chat on
   the right, and nothing else.  Roster, room, strategy and review are all
   questions you ask the bot, not tabs you hunt for. */

const POLL_FAST = 3000;
const POLL_SLOW = 10000;
const ERR_LIMIT = 3;

const BYPOS_N = 5;
const POSITIONS = ["QB", "RB", "WR", "TE"];
const ALL_POS = ["QB", "RB", "WR", "TE", "K", "DEF"];

/* The transcript is a DOM list that a long draft would otherwise grow
   without bound, and a 400-node flex column starts to cost real layout time
   on every repaint.  Oldest messages fall off the top. */
const MAX_MSGS = 120;

const HISTORY_KEY = "ffbot.chat.history";
const HISTORY_MAX = 60;

const DEFAULT_CHIPS = [
  "who should I take?",
  "my roster",
  "read the room",
  "how does the board look?",
];

const S = {
  connected: false,
  sid: "",
  state: null,
  errors: 0,
  stamp: 0,
  timer: null,
  busy: false,
  engine: "builtin",
  names: [],            // completion candidates, refreshed on every render
  open: new Set(),      // expanded by-position rows, keyed by player key
  cmpA: null,           // first half of a pending compare
  warned: {},           // endpoints we have already complained about once
  bypos: null,
  board: {
    shape: "",          // teams/rounds/my_slot - a change rebuilds the grid
    cells: new Map(),   // "round:slot" -> {td, btn, name, meta, sig}
    rows: new Map(),    // round -> tr
    follow: true,       // auto-scroll to the live round until the user drags
    nowRound: 0,
  },
  chat: {
    hist: [],
    idx: null,
    draft: "",
    comp: null,         // in-flight tab-completion cycle
    typing: null,
    busy: false,
  },
};

// ------------------------------------------------------------- dom helpers

function $(id) { return document.getElementById(id); }

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

/* Write text only when it actually changed: the panel repaints on a timer
   and we do not want to churn the layout. */
function setText(node, value) {
  const v = value === null || value === undefined ? "" : String(value);
  if (node.textContent !== v) node.textContent = v;
}

function setCls(node, value) {
  if (node.className !== value) node.className = value;
}

function fmt(x, dp) {
  return (x === null || x === undefined || isNaN(x)) ? "-" : Number(x).toFixed(dp);
}

function pct(x) {
  return (x === null || x === undefined) ? "-" : Math.round(x * 100) + "%";
}

function smooth() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches
    ? "auto" : "smooth";
}

let toastTimer = null;
function toast(msg, kind) {
  const t = $("toast");
  setCls(t, "toast" + (kind ? " " + kind : ""));
  setText(t, msg);
  t.hidden = false;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(function () { t.hidden = true; }, 4200);
}

/* "Christian McCaffrey" -> "C. McCaffrey".  A board cell is ~100px wide;
   the full name lives in the title and the aria-label. */
function shortName(name) {
  const parts = String(name || "").trim().split(/\s+/);
  if (parts.length < 2) return parts[0] || "";
  return parts[0].charAt(0) + ". " + parts.slice(1).join(" ");
}

function pickLabel(pickNo, teams) {
  if (!pickNo || !teams) return "";
  const rnd = Math.floor((pickNo - 1) / teams) + 1;
  const col = ((pickNo - 1) % teams) + 1;
  return rnd + "." + (col < 10 ? "0" + col : String(col));
}

// --------------------------------------------------------------- transport

function token() {
  const t = window.FFBOT_TOKEN;
  return (typeof t === "string" && t.slice(0, 7) !== "__FFBOT") ? t : "";
}

/* Hosted mode: the page gets no process token; /api/connect issues a session
   id instead, which is the credential for everything after.  Survives a
   reload via sessionStorage - per-tab, dropped when the tab closes. */
function sessionId() {
  if (S.sid) return S.sid;
  try { S.sid = sessionStorage.getItem("ffbot.sid") || ""; } catch (e) {}
  return S.sid || "";
}

function rememberSession(sid) {
  if (!sid) return;
  S.sid = sid;
  try { sessionStorage.setItem("ffbot.sid", sid); } catch (e) {}
}

function dropSession() {
  S.sid = "";
  try { sessionStorage.removeItem("ffbot.sid"); } catch (e) {}
}

async function api(path, body) {
  const opts = {cache: "no-store", headers: {}};
  const tok = token();
  if (tok) opts.headers["X-FFBot-Token"] = tok;
  const sid = sessionId();
  if (sid) opts.headers["X-FFBot-Session"] = sid;
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    const err = new Error("cannot reach the server");
    err.net = true;
    throw err;
  }
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (!res.ok) {
    const err = new Error((data && data.error) || ("HTTP " + res.status));
    err.status = res.status;
    // A dead session cannot recover by retrying - forget it so the next
    // connect starts clean instead of looping on 401s.
    if (res.status === 401 && sessionId() && !token()) dropSession();
    throw err;
  }
  return data || {};
}

function qs(path, params) {
  const u = new URLSearchParams();
  Object.keys(params || {}).forEach(function (k) {
    const v = params[k];
    if (v !== null && v !== undefined && v !== "") u.set(k, String(v));
  });
  const q = u.toString();
  return q ? path + "?" + q : path;
}

// ------------------------------------------------------------------ header

function badge(text, hot) {
  return el("span", "badge" + (hot ? " hot" : ""), text);
}

function renderHeader() {
  const st = S.state;
  const badges = $("badges");
  clear(badges);

  if (!st) {
    setText($("draftName"), "not connected");
    setText($("clockMain"), "—");
    setText($("clockSlot"), "no slot");
    setCls($("turn"), "turn idle");
    setText($("turnText"), "connect a draft or start an offline mock");
    return;
  }

  setText($("draftName"), st.name || st.draft_id || "draft");
  badges.appendChild(badge(st.teams + " team"));
  badges.appendChild(badge((st.scoring || "ppr").replace("_", " ")));
  if (st.superflex) badges.appendChild(badge("superflex", true));
  if (st.dynasty) badges.appendChild(badge("dynasty", true));
  if (st.draft_id === "offline") badges.appendChild(badge("mock", true));
  if (st.strategy) badges.appendChild(badge(st.strategy));

  setText($("clockMain"), "R" + st.current_round + " · pick " + st.next_pick_no);
  const mine = st.my_next_picks || [];
  setText($("clockSlot"), "you: slot " + (st.my_slot || "?") +
    (mine.length ? " · next " + mine.slice(0, 3).join(", ") : ""));

  const turn = $("turn");
  const until = st.picks_until_my_turn;
  if (st.is_my_turn) {
    setCls(turn, "turn mine");
    setText($("turnText"), "you are on the clock");
  } else if (typeof until === "number" && until >= 0) {
    setCls(turn, "turn" + (until <= 2 ? " soon" : " idle"));
    setText($("turnText"), until === 1
      ? "1 pick until you're up"
      : until + " picks until you're up");
  } else {
    setCls(turn, "turn idle");
    setText($("turnText"), "slot " + (st.on_the_clock_slot || "?") +
      " is on the clock");
  }
}

function paintDot() {
  const dot = $("dot");
  let cls = "dot red";
  let title = "disconnected";
  if (S.errors >= ERR_LIMIT) {
    title = "server unreachable - retrying slowly";
  } else if (!S.connected || !S.state) {
    title = "no draft connected";
  } else if (S.state.draft_id === "offline") {
    cls = "dot amber";
    title = "offline mock - rival picks are simulated";
  } else if (S.state.status && /complete|paused/i.test(S.state.status)) {
    cls = "dot amber";
    title = "draft " + S.state.status;
  } else {
    cls = "dot green";
    title = "live draft - polling Sleeper";
  }
  setCls(dot, cls);
  if (dot.title !== title) dot.title = title;
}

function paintUpdated() {
  const node = $("updated");
  if (!S.stamp) { setText(node, "—"); return; }
  const secs = Math.max(0, Math.round((Date.now() - S.stamp) / 1000));
  const late = S.errors >= ERR_LIMIT;
  setText(node, (late ? "retrying · " : "") + secs + "s");
  node.style.color = late ? "var(--bad)" : "";
}

function applyState(state, connected) {
  const had = S.state && S.state.draft_id;
  S.connected = !!connected && !!state;
  S.state = state || null;
  if (S.state && had && had !== S.state.draft_id) resetForNewDraft();
  renderHeader();
  paintDot();
  syncWelcome();
}

/* First-run screen: the branded walkthrough owns the page until a draft is
   connected, then the workspace takes over. */
function syncWelcome() {
  const w = $("welcome");
  if (!w) return;
  w.hidden = !!S.connected;
  $("work").style.display = S.connected ? "" : "none";
  const tabs = $("compactTabs");
  if (tabs) tabs.style.display = S.connected ? "" : "none";
}

function wireWelcome() {
  const form = $("wForm");
  if (!form) return;
  form.addEventListener("submit", async function (e) {
    e.preventDefault();
    const body = {draft: $("wDraft").value.trim()};
    if (!body.draft) return;
    const user = $("wUser").value.trim();
    const slot = parseInt($("wSlot").value, 10);
    if (user) body.user = user;
    if (slot) body.slot = slot;
    await connectWith("/api/connect", body, "connecting to your draft…");
  });
  $("wMock").addEventListener("click", async function () {
    await connectWith("/api/mock", {teams: 12, slot: 5, upto: 18},
                      "building a practice draft…");
  });
}

function resetForNewDraft() {
  S.open.clear();
  S.cmpA = null;
  S.bypos = null;
  S.board.shape = "";
  S.board.cells.clear();
  S.board.rows.clear();
  S.board.follow = true;
  S.names = [];
}

// ------------------------------------------------------------- the board

function legend() {
  const box = $("legend");
  if (box.childElementCount) return;
  ALL_POS.forEach(function (p) {
    const s = el("span", "pos " + p, p);
    s.style.background = "var(--" + p.toLowerCase() + "-wash)";
    box.appendChild(s);
  });
}

async function loadBoard() {
  if (!S.connected) return;
  try {
    const d = await api("/api/boardgrid");
    renderBoard(d);
  } catch (e) {
    boardUnavailable(e);
  }
}

function boardUnavailable(e) {
  $("boardTable").hidden = true;
  const p = $("boardEmpty");
  p.hidden = false;
  setText(p, "board unavailable: " + (e.message || e));
  if (!S.warned.board) {
    S.warned.board = true;
    if (!e.net) toast("board: " + e.message, "err");
  }
}

function renderBoard(d) {
  const cols = d.columns || [];
  const rows = d.rows || [];
  if (!cols.length || !rows.length) {
    boardUnavailable(new Error("nothing on the board yet"));
    return;
  }
  $("boardEmpty").hidden = true;
  $("boardTable").hidden = false;

  const shape = [d.teams, d.rounds, d.my_slot,
                 cols.map(function (c) { return c.label; }).join("|")].join("/");
  if (shape !== S.board.shape) {
    buildGrid(d);
    S.board.shape = shape;
  }
  const now = currentRound(d);
  patchGrid(d, now);
  renderRecent(d);

  const reading = S.board.touchedAt && Date.now() - S.board.touchedAt < 8000;
  if (now && now !== S.board.nowRound && !reading) {
    S.board.nowRound = now;
    if (S.board.follow) scrollToNow(true);
  }
}

function currentRound(d) {
  const rows = d.rows || [];
  for (let i = 0; i < rows.length; i++) {
    const cells = rows[i].cells || [];
    for (let j = 0; j < cells.length; j++) {
      if (cells[j].is_current) return rows[i].round;
    }
  }
  return d.current_pick && d.teams
    ? Math.floor((d.current_pick - 1) / d.teams) + 1 : 0;
}

/* Build the whole grid once per draft shape.  Every later poll only patches
   the cells whose contents actually moved, which is what keeps a pick
   landing from shifting anything on screen. */
function buildGrid(d) {
  const cols = d.columns || [];
  const table = $("boardTable");
  const head = $("boardHead");
  const body = $("boardBody");
  S.board.cells.clear();
  S.board.rows.clear();
  clear(head);
  clear(body);
  table.style.setProperty("--cols", String(cols.length));

  const hr = el("tr");
  const corner = el("th", "rail", "R");
  corner.scope = "col";
  hr.appendChild(corner);
  cols.forEach(function (c) {
    const th = el("th", c.is_me ? "me" : "", c.is_me ? "YOU" : c.label);
    th.scope = "col";
    th.title = c.label + (c.is_me ? " (you)" : "");
    hr.appendChild(th);
  });
  head.appendChild(hr);

  (d.rows || []).forEach(function (row) {
    const tr = el("tr");
    const rail = el("th", "rail", String(row.round));
    rail.scope = "row";
    tr.appendChild(rail);
    (row.cells || []).forEach(function (cell) {
      const td = el("td");
      const btn = el("button", "cell");
      btn.type = "button";
      const name = el("span", "cname");
      const meta = el("span", "cmeta");
      btn.appendChild(name);
      btn.appendChild(meta);
      td.appendChild(btn);
      tr.appendChild(td);
      const ref = {td: td, btn: btn, name: name, meta: meta, sig: null,
                   player: ""};
      btn.addEventListener("click", function () {
        prefill(ref.player ? "tell me about " + ref.player
                           : "who should I take?", true);
      });
      S.board.cells.set(row.round + ":" + cell.slot, ref);
    });
    S.board.rows.set(row.round, tr);
    body.appendChild(tr);
  });
}

function patchGrid(d, now) {
  const teams = d.teams;
  const names = [];
  (d.rows || []).forEach(function (row) {
    const tr = S.board.rows.get(row.round);
    let live = false;
    /* An empty cell only earns a pick number when it is one of yours or in
       the live round; 200 grey numbers is exactly the clutter we removed. */
    const label = row.round === now;
    (row.cells || []).forEach(function (cell) {
      const ref = S.board.cells.get(row.round + ":" + cell.slot);
      if (!ref) return;
      if (cell.is_current) live = true;
      if (cell.name) names.push(cell.name);
      const sig = [cell.pick_no, cell.name, cell.pos, cell.team, cell.is_me,
                   cell.is_current, cell.empty, label].join("|");
      if (sig === ref.sig) return;
      ref.sig = sig;
      paintCell(ref, cell, teams, label || cell.is_me);
    });
    if (tr) tr.classList.toggle("round-now", live);
  });
  S.names = uniqueNames(names.concat(S.names.slice(0, 200)));
}

function paintCell(ref, cell, teams, showLabel) {
  const label = pickLabel(cell.pick_no, teams);
  ref.player = cell.name || "";

  let tdCls = "";
  if (cell.is_me) tdCls += " me";
  if (cell.is_me && cell.name) tdCls += " mine";
  if (cell.is_current) tdCls += " now";
  if (!cell.name) tdCls += " empty-cell";
  setCls(ref.td, tdCls.trim());

  if (cell.name) {
    setCls(ref.btn, "cell " + (cell.pos || ""));
    setText(ref.name, shortName(cell.name));
    setText(ref.meta, [cell.pos, cell.team, label].filter(Boolean).join(" · "));
    ref.btn.disabled = false;
    ref.btn.title = cell.name + (cell.pos ? " · " + cell.pos : "") +
      (label ? " · pick " + label : "");
    ref.btn.setAttribute("aria-label", "ask about " + cell.name);
  } else if (cell.is_current) {
    setCls(ref.btn, "cell");
    setText(ref.name, "on the clock");
    setText(ref.meta, label);
    ref.btn.disabled = false;
    ref.btn.title = "pick " + label + " is on the clock";
    ref.btn.setAttribute("aria-label", "pick " + label + " is on the clock");
  } else {
    // Empty future cells would only clutter the tab order.
    setCls(ref.btn, "cell");
    setText(ref.name, "");
    setText(ref.meta, showLabel ? label : "");
    ref.btn.disabled = true;
    ref.btn.title = "";
    ref.btn.removeAttribute("aria-label");
  }
}

/* The phone layout hides the grid, so the same payload also feeds a plain
   reverse-chronological list of what has come off the board. */
function renderRecent(d) {
  const list = $("recentList");
  const picks = [];
  (d.rows || []).forEach(function (row) {
    (row.cells || []).forEach(function (cell) {
      if (cell.name && cell.pick_no) picks.push(cell);
    });
  });
  picks.sort(function (a, b) { return b.pick_no - a.pick_no; });
  clear(list);
  picks.slice(0, 40).forEach(function (cell) {
    const li = el("li", cell.is_me ? "mine" : "");
    li.appendChild(el("span", "rp", pickLabel(cell.pick_no, d.teams)));
    const btn = el("button", "cell " + (cell.pos || ""));
    btn.type = "button";
    btn.appendChild(el("span", "cname", cell.name));
    btn.appendChild(el("span", "cmeta",
      [cell.pos, cell.team].filter(Boolean).join(" · ")));
    btn.addEventListener("click", function () {
      prefill("tell me about " + cell.name, true);
    });
    const wrap = el("span", "rn");
    wrap.appendChild(btn);
    li.appendChild(wrap);
    list.appendChild(li);
  });
}

/* Rect maths rather than offsetTop: a table row's offsetParent is not worth
   reasoning about, and this is exact whatever the scroller's position is. */
function scrollToNow(animate) {
  const tr = S.board.rows.get(S.board.nowRound);
  const box = $("boardScroll");
  if (!tr || !box || !box.clientHeight) return;
  const r = tr.getBoundingClientRect();
  const b = box.getBoundingClientRect();
  const top = box.scrollTop + (r.top - b.top) - (b.height / 2) + (r.height / 2);
  box.scrollTo({top: Math.max(0, top),
                behavior: animate ? smooth() : "auto"});
}

function setFollow(on) {
  S.board.follow = on;
  $("btnNow").hidden = on;
}

// -------------------------------------------------------- best by position

async function loadBypos() {
  if (!S.connected) return;
  try {
    const d = await api(qs("/api/bypos", {n: BYPOS_N}));
    S.bypos = d;
    renderBypos(d);
  } catch (e) {
    const box = $("byposCols");
    clear(box);
    box.appendChild(el("p", "empty", "by-position unavailable: " +
      (e.message || e)));
    if (!S.warned.bypos) {
      S.warned.bypos = true;
      if (!e.net) toast("by position: " + e.message, "err");
    }
  }
}

// ------------------------------------------------------- compact tabs

function setCview(view) {
  const work = $("work");
  if (!work) return;
  work.dataset.cview = view;
  document.querySelectorAll("#compactTabs button").forEach(function (b) {
    b.classList.toggle("on", b.dataset.cview === view);
  });
  try { localStorage.setItem("ffbot.cview", view); } catch (e) { /* fine */ }
}

function initCompactTabs() {
  const nav = $("compactTabs");
  if (!nav) return;
  nav.hidden = false;                 // CSS decides visibility by width
  nav.addEventListener("click", function (e) {
    const btn = e.target.closest("button[data-cview]");
    if (btn) setCview(btn.dataset.cview);
  });
  let saved = "options";
  try { saved = localStorage.getItem("ffbot.cview") || "options"; } catch (e) {}
  setCview(saved);
}

function renderBypos(d) {
  /* Rebuilding the list resets the reader's place, and the panel rebuilds
     every time a pick lands - which is exactly when the user is reading it.
     Put the scroll position back where it was. */
  const scroller = $("byposCols");
  const keep = scroller ? scroller.scrollTop : 0;
  setText($("byposPick"), "pick " + (d.pick_no || "?") + " · R" +
    (d.round || "?"));

  const script = $("scriptSays");
  clear(script);
  script.appendChild(document.createTextNode("the script wants "));
  script.appendChild(el("b", null, d.script_says || "best available"));
  script.appendChild(document.createTextNode(" here."));

  const box = $("byposCols");
  clear(box);
  const outlook = d.outlook || {};
  const positions = d.positions || {};
  const names = [];

  POSITIONS.forEach(function (pos) {
    const col = el("div", "poscol");
    const head = el("div", "poshead");
    head.appendChild(el("span", "pos " + pos, pos));
    head.appendChild(el("span", "outlook", outlookLine(outlook[pos])));
    col.appendChild(head);

    const list = el("ol", "poslist");
    const recs = positions[pos] || [];
    if (!recs.length) {
      const li = el("li");
      li.appendChild(el("div", "detail", "nothing left worth listing"));
      list.appendChild(li);
    }
    recs.forEach(function (rec) {
      names.push(rec.name);
      list.appendChild(playerRow(rec));
    });
    col.appendChild(list);
    box.appendChild(col);
  });
  S.names = uniqueNames(names.concat(S.names));
  if (scroller && keep) scroller.scrollTop = keep;
}

function outlookLine(o) {
  if (!o) return "no read";
  const bits = [];
  if (o.current_tier && o.current_tier.length === 2) {
    bits.push("T" + o.current_tier[0] + " (" + o.current_tier[1] + ")");
  }
  bits.push(fmt(o.expected_gone_of_top12, 1) + "/12 gone");
  bits.push(o.available + " left");
  return bits.join(" · ");
}

function injuryTag(rec) {
  if (!rec.injury) return null;
  const bad = rec.injury !== "Questionable";
  return el("span", "tag " + (bad ? "inj" : "inj mild"), rec.injury);
}

function adpTag(rec) {
  if (rec.adp >= 900) return el("span", "tag even", "no adp");
  const dv = rec.adp_delta;
  if (dv >= 4) return el("span", "tag value", "value +" + fmt(dv, 0));
  if (dv <= -4) return el("span", "tag reach", "reach " + fmt(Math.abs(dv), 0));
  return el("span", "tag even", "on adp");
}

function playerRow(rec) {
  const li = el("li");
  const open = S.open.has(rec.key);

  const btn = el("button", "prow");
  btn.type = "button";
  btn.setAttribute("aria-expanded", open ? "true" : "false");
  btn.appendChild(el("span", "pname", rec.name));

  const line = el("span", "pline");
  const rank = (rec.off_guide ? "mkt " : "") + rec.pos +
    (rec.guide_pos_rank || "?");
  line.appendChild(el("span", null, rank));
  if (rec.team) line.appendChild(el("span", null, rec.team));
  line.appendChild(el("span", null, fmt(rec.ppg, 1) + "ppg"));
  line.appendChild(adpTag(rec));
  const inj = injuryTag(rec);
  if (inj) line.appendChild(inj);
  const surv = el("span", "surv", pct(rec.survival_to_next_pick) + " left");
  surv.title = "chance he is still there at your next pick";
  line.appendChild(surv);
  btn.appendChild(line);

  const detail = detailBox(rec);
  detail.hidden = !open;

  btn.addEventListener("click", function () {
    const nowOpen = detail.hidden;
    detail.hidden = !nowOpen;
    btn.setAttribute("aria-expanded", nowOpen ? "true" : "false");
    if (nowOpen) S.open.add(rec.key); else S.open.delete(rec.key);
    if (nowOpen) prefill("why " + rec.name, false);
  });

  li.appendChild(btn);
  li.appendChild(detail);
  return li;
}

function detailBox(rec) {
  const box = el("div", "detail");

  const stats = el("div", "stats");
  [["score", fmt(rec.score, 2)],
   ["now", fmt(rec.value_now, 2)],
   ["ahead", fmt(rec.lookahead, 2)],
   ["vor", fmt(rec.vor, 1)],
   ["floor", fmt(rec.floor, 1)],
   ["ceil", fmt(rec.upside, 1)],
   ["adp", rec.adp >= 900 ? "-" : fmt(rec.adp, 1)],
   ["tier", rec.tier ? String(rec.tier[0]) : "-"]].forEach(function (pair) {
    const s = el("span");
    s.appendChild(el("b", null, pair[1]));
    s.appendChild(document.createTextNode(" " + pair[0]));
    stats.appendChild(s);
  });
  box.appendChild(stats);

  const reasons = rec.reasons || [];
  if (reasons.length) {
    const ul = el("ul");
    reasons.slice(0, 6).forEach(function (r) {
      ul.appendChild(el("li", null, r.text));
    });
    box.appendChild(ul);
  }

  const acts = el("div", "acts");
  acts.appendChild(act("why", function () { send("why " + rec.name); }));
  acts.appendChild(act("compare", function () { queueCompare(rec.name); }));
  acts.appendChild(act("+0.5", function () { bump(rec.name, 0.5); }));
  acts.appendChild(act("-0.5", function () { bump(rec.name, -0.5); }));
  acts.appendChild(act("ban", function () { ban(rec.name); }));
  box.appendChild(acts);
  return box;
}

function act(label, fn) {
  const b = el("button", "small", label);
  b.type = "button";
  b.addEventListener("click", fn);
  return b;
}

function uniqueNames(list) {
  const seen = Object.create(null);
  const out = [];
  list.forEach(function (n) {
    if (!n || seen[n]) return;
    seen[n] = true;
    out.push(n);
  });
  return out.slice(0, 400);
}

// ------------------------------------------------------------- player acts

function queueCompare(name) {
  if (S.cmpA && S.cmpA !== name) {
    const a = S.cmpA;
    S.cmpA = null;
    send("compare " + a + " vs " + name);
    return;
  }
  S.cmpA = name;
  toast("comparing " + name + " - now pick the other one");
}

async function bump(name, delta) {
  try {
    const d = await api("/api/bump", {name: name, delta: delta,
                                      reason: "panel"});
    addSys((d.player || name) + " bumped " + (delta > 0 ? "+" : "") + delta);
    await tick(true);
  } catch (e) {
    addSys(e.message, true);
  }
}

async function ban(name) {
  try {
    await api("/api/ban", {name: name});
    addSys(name + " banned for this draft");
    await tick(true);
  } catch (e) {
    addSys(e.message, true);
  }
}

// -------------------------------------------------------------------- chat

function trimLog() {
  const log = $("chatLog");
  while (log.childElementCount > MAX_MSGS) log.removeChild(log.firstChild);
}

function atBottom(log) {
  return log.scrollHeight - log.scrollTop - log.clientHeight < 80;
}

function addMsg(kind, who) {
  const log = $("chatLog");
  const stick = atBottom(log);
  const msg = el("div", "msg " + kind);
  if (who) msg.appendChild(el("span", "who", who));
  const bubble = el("div", "bubble");
  msg.appendChild(bubble);
  log.appendChild(msg);
  trimLog();
  if (stick) log.scrollTop = log.scrollHeight;
  return {msg: msg, bubble: bubble, log: log};
}

function addUser(text) {
  const m = addMsg("user", "you");
  m.bubble.textContent = text;
  m.log.scrollTop = m.log.scrollHeight;
}

function addBot(text, meta, prose) {
  const m = addMsg("bot", meta);
  if (prose) {
    /* Deep answers are sentences, not tables - render them as text with a
       reading line-height instead of the terminal-style block. */
    const div = el("div", "prose", text);
    m.bubble.appendChild(div);
  } else {
    const pre = el("pre", null, text);
    m.bubble.appendChild(pre);
  }
  m.log.scrollTop = m.log.scrollHeight;
}

function addSys(text, isErr) {
  const m = addMsg("sys" + (isErr ? " err" : ""), null);
  m.bubble.textContent = text;
  m.log.scrollTop = m.log.scrollHeight;
}

function showTyping() {
  const m = addMsg("bot", "bot");
  const dots = el("div", "typing");
  dots.appendChild(el("i"));
  dots.appendChild(el("i"));
  dots.appendChild(el("i"));
  m.bubble.appendChild(dots);
  m.log.scrollTop = m.log.scrollHeight;
  S.chat.typing = m.msg;
}

function hideTyping() {
  if (S.chat.typing && S.chat.typing.parentNode) {
    S.chat.typing.parentNode.removeChild(S.chat.typing);
  }
  S.chat.typing = null;
}

function renderChips(list) {
  const box = $("chatChips");
  clear(box);
  (list && list.length ? list : DEFAULT_CHIPS).slice(0, 4).forEach(function (t) {
    const b = el("button", "chip", t);
    b.type = "button";
    b.addEventListener("click", function () { send(t); });
    box.appendChild(b);
  });
}

/* Clicking a player anywhere loads the obvious question, ready to send but
   never sent for you.  `focus` is false where the click already did
   something visible (expanding a row), so it cannot yank the caret away. */
function prefill(text, focus) {
  const inp = $("chatInput");
  inp.value = text;
  S.chat.idx = null;
  S.chat.comp = null;
  autoGrow(inp);
  if (focus) inp.focus();
  inp.setSelectionRange(inp.value.length, inp.value.length);
}

/* Intents that retune the engine: after one of these the board and the
   by-position lists on screen are stale, so pull them again. */
const MUTATING = {strategy_set: 1, bump: 1, ban: 1, preset: 1, note: 1};

async function send(text) {
  const message = String(text || "").trim();
  if (!message) return;
  const inp = $("chatInput");
  if (inp.value.trim() === message) { inp.value = ""; autoGrow(inp); }
  pushHistory(message);
  S.chat.idx = null;
  S.chat.comp = null;
  addUser(message);
  showTyping();
  S.chat.busy = true;
  $("chatForm").querySelector("button").disabled = true;
  try {
    const d = await api("/api/chat", {message: message});
    hideTyping();
    S.engine = d.engine || S.engine || "builtin";
    setText($("engineTag"), "chat: " + S.engine);
    const bits = ["bot", S.engine];
    if (typeof d.confidence === "number") {
      bits.push(Math.round(d.confidence * 100) + "%");
    }
    if (d.understood === false) bits.push("guessing");
    const prose = d.intent === "deep" || d.intent === "deep_detail";
    addBot(d.output || "(no answer)", bits.join(" · "), prose);
    renderChips(d.suggestions);
    if (d.intent && MUTATING[d.intent]) await tick(true);
  } catch (e) {
    hideTyping();
    addSys(e.message, true);
  } finally {
    S.chat.busy = false;
    $("chatForm").querySelector("button").disabled = false;
  }
}

// ----------------------------------------------------------- chat history

function loadHistory() {
  try {
    const raw = window.localStorage.getItem(HISTORY_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return Array.isArray(arr)
      ? arr.filter(function (x) { return typeof x === "string"; })
           .slice(-HISTORY_MAX)
      : [];
  } catch (e) {
    return [];               // private mode, or somebody hand-edited it
  }
}

function pushHistory(text) {
  const h = S.chat.hist;
  if (h[h.length - 1] === text) return;
  h.push(text);
  while (h.length > HISTORY_MAX) h.shift();
  try {
    window.localStorage.setItem(HISTORY_KEY, JSON.stringify(h));
  } catch (e) {
    // A full or disabled localStorage must not cost you the message.
  }
}

function walkHistory(dir) {
  const inp = $("chatInput");
  const h = S.chat.hist;
  if (!h.length) return false;
  if (dir < 0) {
    if (S.chat.idx === null) {
      S.chat.draft = inp.value;
      S.chat.idx = h.length;
    }
    if (S.chat.idx === 0) return true;
    S.chat.idx -= 1;
  } else {
    if (S.chat.idx === null) return false;
    S.chat.idx += 1;
    if (S.chat.idx >= h.length) {
      S.chat.idx = null;
      inp.value = S.chat.draft;
      autoGrow(inp);
      inp.setSelectionRange(inp.value.length, inp.value.length);
      return true;
    }
  }
  inp.value = h[S.chat.idx];
  autoGrow(inp);
  inp.setSelectionRange(inp.value.length, inp.value.length);
  return true;
}

// -------------------------------------------------------- tab completion

/* Complete against whatever is on screen: the by-position lists and every
   name already on the board.  Names contain spaces, so the tail to match is
   the last one, two or three words - longest wins.  Repeated Tab cycles. */
function tabComplete(inp) {
  const caret = inp.selectionStart;
  const head = inp.value.slice(0, caret);
  const rest = inp.value.slice(caret);
  if (!head || /\s$/.test(head)) return false;

  const comp = S.chat.comp;
  if (comp && comp.applied === head && comp.matches.length > 1) {
    comp.idx = (comp.idx + 1) % comp.matches.length;
    return applyComp(comp, rest, inp);
  }

  const words = head.split(/\s+/);
  for (let take = Math.min(3, words.length); take >= 1; take--) {
    const tail = words.slice(words.length - take).join(" ");
    if (tail.length < 2) continue;
    const low = tail.toLowerCase();
    const matches = S.names.filter(function (n) {
      const l = n.toLowerCase();
      return l.indexOf(low) === 0 && l !== low;
    });
    if (!matches.length) continue;
    S.chat.comp = {before: head.slice(0, head.length - tail.length),
                   matches: matches, idx: 0, applied: ""};
    return applyComp(S.chat.comp, rest, inp);
  }
  return false;
}

function applyComp(state, rest, inp) {
  const next = state.before + state.matches[state.idx];
  inp.value = next + rest;
  state.applied = next;
  autoGrow(inp);
  inp.setSelectionRange(next.length, next.length);
  return true;
}

function autoGrow(ta) {
  ta.style.height = "auto";
  ta.style.height = Math.min(ta.scrollHeight, 118) + "px";
}

// ----------------------------------------------------------------- polling

function schedule() {
  if (S.timer) clearTimeout(S.timer);
  const delay = S.errors >= ERR_LIMIT ? POLL_SLOW : POLL_FAST;
  S.timer = setTimeout(function () { tick(false); }, delay);
}

function onPollError(e) {
  const msg = String(e.message || e);
  if (!e.net && (/no draft|not connected/i.test(msg)
                 || (e.status === 401 && !S.connected))) {
    applyState(null, false);      // healthy server, nothing to watch yet
    S.errors = 0;
    return;
  }
  S.errors += 1;
  if (S.errors === 1 || S.errors === ERR_LIMIT) toast(msg, "err");
}

async function refreshPanes() {
  await loadBoard();
  await loadBypos();
}

async function tick(force) {
  if (S.busy) { schedule(); return; }
  S.busy = true;
  try {
    if (!S.connected) {
      const d = await api("/api/state");
      S.errors = 0;
      S.stamp = Date.now();
      const was = S.connected;
      applyState(d.state, d.connected);
      if (d.connected && !was) {
        await loadPresets();
        await refreshPanes();
      }
    } else {
      const d = await api("/api/refresh");
      S.errors = 0;
      S.stamp = Date.now();
      applyState(d.state, true);
      if (d.changed || force) await refreshPanes();
    }
  } catch (e) {
    onPollError(e);
  } finally {
    S.busy = false;
    paintDot();
    paintUpdated();
    schedule();
  }
}

// ------------------------------------------------------------------- setup

function setMenuOpen(open) {
  $("menu").hidden = !open;
  $("btnMenu").setAttribute("aria-expanded", open ? "true" : "false");
  if (open) $("cDraft").focus();
}

async function loadPresets() {
  try {
    const d = await api("/api/strategy");
    const sel = $("presetSel");
    const want = (S.state && S.state.strategy) || "";
    clear(sel);
    (d.presets || []).forEach(function (name) {
      const o = el("option", null, name);
      o.value = name;
      if (name === want) o.selected = true;
      sel.appendChild(o);
    });
  } catch (e) {
    // Presets are a nicety; a missing engine is reported elsewhere.
  }
}

async function connectWith(path, body, msg) {
  toast(msg);
  const btns = $("menu").querySelectorAll("button");
  Array.prototype.forEach.call(btns, function (b) { b.disabled = true; });
  try {
    const d = await api(path, body);
    rememberSession(d.session);
    resetForNewDraft();
    S.warned = {};
    applyState(d.state || d, true);
    S.stamp = Date.now();
    S.errors = 0;
    setMenuOpen(false);
    if (d.warning) toast(d.warning, "err");
    else toast("connected", "ok");
    addSys("connected to " + ((d.state && d.state.name) || "the draft"));
    await loadPresets();
    await refreshPanes();
    paintUpdated();
  } catch (e) {
    toast(e.message, "err");
  } finally {
    Array.prototype.forEach.call(btns, function (b) { b.disabled = false; });
  }
}

async function doConnect(ev) {
  ev.preventDefault();
  const draft = $("cDraft").value.trim();
  if (!draft) { toast("paste a Sleeper draft URL or id", "err"); return; }
  const body = {draft: draft};
  const user = $("cUser").value.trim();
  const slot = parseInt($("cSlot").value, 10);
  if (user) body.user = user;
  if (!isNaN(slot) && slot > 0) body.slot = slot;
  await connectWith("/api/connect", body, "connecting to Sleeper…");
}

async function doMock(ev) {
  ev.preventDefault();
  const upto = parseInt($("mUpto").value, 10);
  const body = {
    teams: parseInt($("mTeams").value, 10) || 12,
    rounds: parseInt($("mRounds").value, 10) || 15,
    slot: parseInt($("mSlot").value, 10) || 1,
    scoring: $("mScoring").value,
    superflex: $("mSuperflex").checked,
    dynasty: $("mDynasty").checked,
  };
  if (!isNaN(upto) && upto > 0) body.upto = upto;
  await connectWith("/api/mock", body, "building the mock…");
}

// ----------------------------------------------------------------- wiring

function wire() {
  $("btnMenu").addEventListener("click", function () {
    setMenuOpen($("menu").hidden);
  });
  $("connectForm").addEventListener("submit", doConnect);
  $("mockForm").addEventListener("submit", doMock);

  $("presetApply").addEventListener("click", async function () {
    const preset = $("presetSel").value;
    if (!preset) return;
    try {
      const d = await api("/api/strategy", {preset: preset});
      addSys("strategy: " + ((d.changes || []).join("; ") || preset));
      setMenuOpen(false);
      await tick(true);
    } catch (e) {
      toast(e.message, "err");
    }
  });

  $("noteForm").addEventListener("submit", async function (ev) {
    ev.preventDefault();
    const text = $("noteText").value.trim();
    if (!text) return;
    try {
      await api("/api/note", {text: text});
      $("noteText").value = "";
      toast("noted", "ok");
    } catch (e) {
      toast(e.message, "err");
    }
  });

  // Following stops the moment the user drives the board themselves.
  const board = $("boardScroll");
  ["wheel", "pointerdown", "touchstart"].forEach(function (evt) {
    board.addEventListener(evt, function () { setFollow(false); },
                           {passive: true});
  });
  board.addEventListener("keydown", function (e) {
    if (/^(Arrow|Page|Home|End)/.test(e.key)) setFollow(false);
  });
  $("btnNow").addEventListener("click", function () {
    setFollow(true);
    scrollToNow(true);
    board.focus();
  });

  $("chatForm").addEventListener("submit", function (e) {
    e.preventDefault();
    send($("chatInput").value);
  });

  const inp = $("chatInput");
  inp.addEventListener("input", function () {
    S.chat.idx = null;
    S.chat.comp = null;
    autoGrow(inp);
  });
  inp.addEventListener("keydown", onChatKey);

  document.addEventListener("keydown", onGlobalKey);
}

function onChatKey(e) {
  const inp = e.target;
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    send(inp.value);
    return;
  }
  if (e.key === "Tab" && !e.shiftKey) {
    // Only steal Tab when it has something to complete, so focus still
    // escapes the box when the user means it to.
    if (tabComplete(inp)) e.preventDefault();
    return;
  }
  if (e.key === "ArrowUp" || e.key === "ArrowDown") {
    const caret = inp.selectionStart;
    const before = inp.value.slice(0, caret);
    const after = inp.value.slice(caret);
    const ok = e.key === "ArrowUp"
      ? before.indexOf("\n") < 0        // on the first line
      : after.indexOf("\n") < 0;        // on the last one
    if (ok && walkHistory(e.key === "ArrowUp" ? -1 : 1)) e.preventDefault();
  }
}

function onGlobalKey(e) {
  const t = e.target;
  const tag = t && t.tagName;
  const inField = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";

  if (e.key === "Escape") {
    if (!$("menu").hidden) { setMenuOpen(false); return; }
    if (inField) t.blur();
    return;
  }
  if ((e.ctrlKey || e.metaKey) && (e.key === "l" || e.key === "L")) {
    e.preventDefault();
    clear($("chatLog"));
    addSys("transcript cleared");
    return;
  }
  if (e.key === "/" && !inField && !e.ctrlKey && !e.metaKey && !e.altKey) {
    e.preventDefault();
    $("chatInput").focus();
  }
}

// -------------------------------------------------------------------- boot

async function boot() {
  wire();
  legend();
  renderChips(null);
  S.chat.hist = loadHistory();
  addSys("Ask in plain English. Try “who should I take”, “compare Bijan vs " +
    "Jefferson”, “my roster”, “read the room”, “set qb_min_round 8”.");
  setInterval(paintUpdated, 1000);
  initCompactTabs();
  wireWelcome();
  const bs = $("boardScroll");
  if (bs) {
    ["wheel", "touchstart", "pointerdown"].forEach(function (evt) {
      bs.addEventListener(evt, function () {
        S.board.touchedAt = Date.now();
      }, {passive: true});
    });
  }
  try {
    const d = await api("/api/state");
    S.stamp = Date.now();
    applyState(d.state, d.connected);
    if (d.connected) {
      await loadPresets();
      await refreshPanes();
    } else {
      syncWelcome();
    }
  } catch (e) {
    onPollError(e);
    applyState(null, false);
  }
  paintDot();
  paintUpdated();
  schedule();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
