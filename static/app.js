/* Wrapped in an IIFE on purpose. A classic <script> shares one global lexical
   environment with every other classic script on the page, so this file's
   top-level `const $` collided with the same declaration in each page's inline
   script: `SyntaxError: Identifier '$' has already been declared`, which killed
   the whole inline script -- App.boot() never ran and the sidebar rendered zero
   links. Only the browser sees this; checking each file on its own does not. */
(function () {
/* VORTEX — shared client. One place decides what a number means, so no page can
   show a flattering version of a run.
 *
 * The rule this file exists to enforce: two rates are always shown, never one.
 * `checks/s` is requests the run completed, refusals included. `verdicts/s` is
 * names actually resolved. A run that is 80% refused looks fast on the first and
 * slow on the second, and only the second is what a sniper is for.
 */
"use strict";

const S = {
  info: null, menu: [], kinds: {}, plats: new Set(), spaces: new Set(),
  tune: { snd: false, notif: false, follow: true, keep: true },
  run: null, status: null, watching: false, lastRun: null,
  cursor: 0, feed: [], hits: [], filter: "all", proxies: null,
};
const SHOW = 400, HOLD = 6000;
const $ = (id) => document.getElementById(id);

const fmt = (n) => (n == null ? "—" : Number(n).toLocaleString("en-US"));
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const pct = (x) => (x == null ? "—" : Math.round(x * 100) + "%");
const clock = (s) => {
  s = Math.max(0, Math.round(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  return (h ? h + "h " : "") + (h || m ? m + "m " : "") + x + "s";
};

/* A verdict is only ever available or taken. Everything else is the run talking
   about itself or about the proxies, and belongs in health, not in results. */
const VERDICTS = new Set(["available", "taken"]);
const isVerdict = (st) => VERDICTS.has(st);

function toast(msg, kind) {
  const el = document.createElement("div");
  if (kind === "bad") el.className = "bad";
  el.textContent = msg;
  const box = $("toast");
  if (!box) return;
  box.appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = { raw: text }; }
  if (!r.ok) throw new Error((data && (data.detail || data.error)) || "HTTP " + r.status);
  return data;
}

const NAV = [
  // A trailing slash marks the pages that share a name with a JSON endpoint:
  // /hits, /proxies and /settings are APIs, /hits/ and friends are the pages.
  // The console lives at /console since the landing page took /.
  ["/console", "console", "Console"],
  ["/hits/", "hits", "Hits"],
  ["/platforms", "platforms", "Platforms"],
  ["/proxies/", "proxies", "Proxies"],
  ["/alerts", "alerts", "Alerts"],
  ["/settings/", "settings", "Settings"],
  ["/buy", "buy", "Buy access ↗"],
  ["/account", "account", "Account"],
  ["/docs", "docs", "Docs"],
];

function renderNav(active) {
  const el = $("nav");
  if (!el) return;
  el.innerHTML = NAV.map(([href, key, label]) => {
    const badge = key === "hits" ? '<em id="navHits">0</em>'
      : key === "proxies" ? '<em id="navProx">0</em>' : "";
    return `<a href="${href}" class="${key === active ? "on" : ""}">${label}${badge}</a>`;
  }).join("");
}

/* ---- selection ------------------------------------------------------- */
function restore() {
  try {
    Object.assign(S.tune, JSON.parse(localStorage.getItem("VORTEX.tune") || "{}"));
    const p = JSON.parse(localStorage.getItem("VORTEX.plats") || "[]");
    if (Array.isArray(p)) S.plats = new Set(p);
    const s = JSON.parse(localStorage.getItem("VORTEX.spaces") || '["3l"]');
    if (Array.isArray(s) && s.length) S.spaces = new Set(s);
    const f = localStorage.getItem("VORTEX.filter");
    if (f) S.filter = f;
  } catch (e) { /* a corrupt store must not stop the page */ }
}
function persist() {
  localStorage.setItem("VORTEX.tune", JSON.stringify(S.tune));
  localStorage.setItem("VORTEX.plats", JSON.stringify([...S.plats]));
  localStorage.setItem("VORTEX.spaces", JSON.stringify([...S.spaces]));
  localStorage.setItem("VORTEX.filter", S.filter);
}

/* The grid is the shared SniperCell component rather than markup written here,
   so a cell means the same thing on the console, on the Platforms page and in
   the preview on the landing page. The state is what carries the meaning: idle,
   healthy, paced, paused, blocked. */
/* `progress` is passed in rather than read off S.status: the tick that arrives
   on the stream is newer than the last /runs poll, and reading the polled copy
   left every cell at "idle" for eight seconds while names were plainly being
   resolved. */
function renderPlats(pace, paused, progress) {
  const el = $("platList");
  if (!el) return;
  const p = progress || (S.status && S.status.progress) || {};
  el.innerHTML = UI.cellMarkup({
    platforms: S.info.platforms || [],
    selected: S.plats,
    byPlatform: p.by_platform || {},
    paced: pace || p.platforms_paced || {},
    paused: paused || p.platforms_paused || {},
  });
  el.onclick = (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    const p = b.dataset.p;
    S.plats.has(p) ? S.plats.delete(p) : S.plats.add(p);
    persist(); renderPlats();
  };
  paintPlatSum();
}

/* The count in the header, refreshed wherever the selection changes -- it used
   to be written only by the console's own tick, so it read "0 of N selected"
   with a sniper plainly selected. */
function paintPlatSum() {
  const el = $("platSum");
  if (!el) return;
  const total = ((S.info && S.info.platforms) || []).length;
  el.textContent = `${S.plats.size} of ${total} selected`;
}

function renderSpaces() {
  const el = $("spaces");
  if (!el) return;
  const lens = [...new Set((S.menu || []).map((b) => b.length))].sort((a, b) => a - b);
  const kinds = Object.keys(S.kinds || {});
  let html = "";
  for (const L of lens) {
    const opts = (S.menu || []).filter((b) => b.length === L);
    html += `<div class="sizes" data-l="${L}">` + opts.map((b) => {
      const key = L + b.kind;
      return `<button data-k="${key}" class="${S.spaces.has(key) ? "on" : ""}">${key}
        <b>${fmt(b.size)}</b></button>`;
    }).join("") + `</div>`;
  }
  el.innerHTML = html;
  el.onclick = (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    const k = b.dataset.k;
    S.spaces.has(k) ? S.spaces.delete(k) : S.spaces.add(k);
    persist(); renderSpaces(); summarise();
  };
  summarise();
}

function spaceKeys() { return [...S.spaces]; }
function buckets() {
  // "3l" -- length then kind, the same order the picker labels and /menu uses.
  return spaceKeys().map((k) => {
    const m = k.match(/^(\d+)([a-z]+)$/);
    if (!m) return null;
    return { kind: m[2], length: Number(m[1]) };
  }).filter(Boolean);
}
function selectionSize() {
  return (S.menu || []).filter((b) => S.spaces.has(b.length + b.kind))
    .reduce((a, b) => a + b.size, 0);
}

/* A selection too big to build says so here, while you are still choosing, and
   offers the cap that turns it into a random sample. Finding out as a 400 after
   pressing start is the same information, delivered worse. */
function summarise() {
  const el = $("spaceSum");
  if (!el) return;
  const size = selectionSize();
  const n = spaceKeys().length;
  if (!n) { el.textContent = "nothing selected"; return; }
  const cap = (S.info && S.info.generation && S.info.generation.selection_build_max) || 2000000;
  const limitEl = $("limit");
  const capped = limitEl && Number(limitEl.value) > 0 ? Number(limitEl.value) : 0;
  if (capped) {
    el.innerHTML = `${n} selected · checking ${fmt(Math.min(capped, size))} of ${fmt(size)} names, ` +
      `drawn at random from the whole space`;
  } else if (size > cap) {
    el.innerHTML = `${n} selected · ${fmt(size)} names is over the ${fmt(cap)} a run will ` +
      `build — set a cap and it draws that many <b style="color:var(--accent)">at random</b> ` +
      `instead of walking them in order`;
    if (limitEl && !limitEl.value) limitEl.placeholder = String(Math.min(50000, size));
  } else {
    el.textContent = `${n} selected · ${fmt(size)} names, shuffled into a random order`;
  }
}

/* ---- the run bar ----------------------------------------------------- */
function paintRun(st) {
  S.status = st;
  const line = $("runLine"), span = line && line.querySelector("span");
  const stop = $("stopBtn");
  if (!line) return;
  const side = $("sideRun") && $("sideRun").querySelector("span");
  line.classList.toggle("live", !!(st && st.active));
  line.classList.toggle("dead", !!st && !st.active && !!st.stopped);
  let text, id;
  if (st && st.active) {
    text = `${st.kind} run · ${fmt(st.checked)} checks`;
    id = `run ${st.id}`;
    stop.disabled = false;
  } else if (st) {
    text = `last ${st.kind} run · ${fmt(st.checked)} checks`;
    id = `run ${st.id}`;
    stop.disabled = true;
  } else {
    text = "idle";
    id = "—";
    stop.disabled = true;
  }
  span.textContent = text;
  if (side) side.textContent = text;
  const note = $("runNoteGlobal");
  if (note) note.textContent = id;
}

async function pumpRun() {
  let data;
  try { data = await api("/runs"); } catch (e) { return; }
  const runs = data.runs || [];
  const active = runs.find((r) => r.active);
  if (active) {
    if (S.run !== active.id) {
      S.run = active.id; S.cursor = 0; S.watching = true;
      if (window.PAGE && PAGE.onReattach) PAGE.onReattach(active);
      else paintRun(active);
    } else {
      paintRun(active);
      if (window.PAGE && PAGE.onStatus) PAGE.onStatus(active);
    }
    return;
  }
  S.watching = false;
  S.lastRun = runs[0] || null;
  paintRun(runs[0] || null);
  const hits = $("navHits");
  if (hits && data.hits_count != null) hits.textContent = fmt(data.hits_count);
}

async function stopRun() {
  try {
    await api(S.run ? `/runs/stop?run_id=${encodeURIComponent(S.run)}` : "/runs/stop", { method: "POST" });
    toast("stop signalled — the run ends itself", "ok");
  } catch (e) { toast("stop failed: " + e.message, "bad"); }
}

/* ---- stream handling ------------------------------------------------- */
function applyProgress(p) {
  if (!p) return;
  /* The six numbers that answer "is the run working", from the same component
     the landing page's preview uses. Re-rendered per tick rather than patched by
     id: it is nine small elements, and one definition of each number is worth
     more than the diffing. */
  if ($("kpiRow")) $("kpiRow").innerHTML = UI.kpiRow(UI.runKpis(p, p), "primary");
  if ($("kpiRow2")) $("kpiRow2").innerHTML = UI.kpiRow([
    { label: "taken", value: fmt(p.taken || 0), sub: "held by a live account" },
    { label: "answered", value: pct(p.answer_rate),
      sub: `${fmt(p.answered || 0)} of ${fmt(p.checked || 0)} checks` },
    { label: "progress", value: pct(p.progress),
      sub: `${fmt(p.checked || 0)} of ${fmt(p.total_checks || 0)}` },
    { label: "elapsed", value: clock(p.elapsed_s), sub: "wall clock" },
    { label: "errors", value: fmt(p.error || 0), tone: "bad", sub: "never arrived" },
  ]);
  if ($("health")) $("health").innerHTML = UI.healthPanel(UI.healthRows(p, p));
  const bar = $("bar");
  if (bar) bar.style.width = Math.min(100, (p.progress || 0) * 100) + "%";
  renderPaced(p.platforms_paced, p.platforms_paused);
  if (S.info && S.info.platforms) renderPlats(p.platforms_paced, p.platforms_paused, p);
}

function renderPaced(by, paused) {
  const el = $("paced");
  if (!el) return;
  el.innerHTML = UI.pacedChips(by, paused);
}

function feedLine(ev) {
  if (!isVerdict(ev.status)) return;         // results are verdicts, nothing else
  const t = new Date().toLocaleTimeString("en-GB");
  S.feed.unshift({ t, u: ev.username, p: ev.platform, s: ev.status, c: ev.confirmed });
  if (S.feed.length > SHOW) S.feed.pop();
  if ($("feed")) renderFeed();
  if (ev.status === "available") {
    if (S.tune.snd) beep();
    if (S.tune.notif && window.Notification && Notification.permission === "granted") {
      new Notification("free on " + ev.platform, { body: ev.username });
    }
  }
}

function renderFeed() {
  const el = $("feed");
  if (!el) return;
  const rows = S.feed.filter((r) => S.filter === "all" || r.s === S.filter);
  // One shape for the feed and the hit store, so the shared VerdictFeed can
  // render either. `c` is the confirmation state: true, false, or unknown.
  el.innerHTML = UI.verdictFeed(rows.map((r) => ({
    t: r.t, username: r.u, platform: r.p, status: r.s, confirmed: r.c,
  })) || []);
}

function beep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.connect(g); g.connect(ctx.destination);
    o.frequency.value = 760; g.gain.value = 0.05;
    o.start(); o.stop(ctx.currentTime + 0.08);
  } catch (e) { /* sound is a nicety */ }
}

async function startSweep() {
  if (!S.plats.size) return toast("pick at least one sniper", "bad");
  if (!spaceKeys().length) return toast("pick at least one name space", "bad");
  if (!buckets().length) return toast("that name-space selection did not parse", "bad");
  if (!S.proxies || !S.proxies.count) return toast("no proxies stored — add some first", "bad");
  const body = { buckets: buckets(), platforms: [...S.plats], stream: true };
  if ($("limit") && Number($("limit").value) > 0) body.limit = Number($("limit").value);
  S.feed = []; renderFeed();
  await runStream("/scan", body);
}

async function startSingle(name) {
  if (!S.plats.size) return toast("pick at least one sniper", "bad");
  if (!name) return toast("type a name to check", "bad");
  await runStream("/snipe", { usernames: [name], platforms: [...S.plats], stream: true });
}

async function runStream(path, body) {
  const stop = $("stopBtn"); if (stop) stop.disabled = false;
  const ctrl = new AbortController();
  S.ac = ctrl;
  try {
    const r = await fetch(path, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify(body), signal: ctrl.signal,
    });
    if (!r.ok) {
      const txt = await r.text();
      let d = null; try { d = JSON.parse(txt); } catch (e) {}
      throw new Error((d && d.detail) || ("HTTP " + r.status));
    }
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const raw = buf.slice(0, nl); buf = buf.slice(nl + 1);
        if (!raw.trim()) continue;
        let ev; try { ev = JSON.parse(raw); } catch (e) { continue; }
        handleEvent(ev);
      }
    }
  } catch (e) {
    if (e.name !== "AbortError") toast("run failed: " + e.message, "bad");
  } finally {
    S.ac = null;
    pumpRun();
  }
}

function handleEvent(ev) {
  if (ev.type === "start") {
    S.run = ev.run_id; S.watching = true;
    if ($("runNote")) $("runNote").textContent = `${fmt(ev.names)} names × ${ev.platforms.length} snipers`;
    applyProgress({ progress: 0, checked: 0, total_checks: ev.names * ev.platforms.length,
                    available_count: 0, checks_per_second: 0, verdicts_per_second: 0 });
    return;
  }
  if (ev.type === "result" || ev.status) {
    feedLine(ev);
    if (ev.type === "result" && ev.status === "available" && S.tune.notif) { /* handled in feedLine */ }
    if (window.PAGE && PAGE.onResult) PAGE.onResult(ev);
    return;
  }
  if (ev.type === "progress" || ev.type === "waiting" || ev.type === "heartbeat") {
    applyProgress(ev);
    return;
  }
  if (ev.type === "done") {
    applyProgress(ev);
    S.watching = false;
    if ($("runNote")) {
      $("runNote").textContent = ev.stopped ? "stopped on request" : "finished";
    }
    const name = $("runLine") && $("runLine").querySelector("span");
    if (name) name.textContent = `last run · ${fmt(ev.checked)} checks · ${ev.verdicts_per_second}/s verdicts`;
    pumpRun();
    return;
  }
  if (ev.type === "note") {
    if ($("poolNote")) $("poolNote").textContent = ev.pool_note || "";
    return;
  }
}

async function follow(runId) {
  const ctrl = new AbortController();
  S.ac = ctrl;
  try {
    const r = await fetch(`/runs/${runId}/stream`, { signal: ctrl.signal });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const reader = r.body.getReader(); const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const raw = buf.slice(0, nl); buf = buf.slice(nl + 1);
        if (!raw.trim()) continue;
        let ev; try { ev = JSON.parse(raw); } catch (e) { continue; }
        handleEvent(ev);
      }
    }
  } catch (e) {
    if (e.name !== "AbortError") toast("reattach failed: " + e.message, "bad");
  }
}

async function loadProxies() {
  try {
    S.proxies = await api("/proxies?limit=1");
    const p = $("navProx"); if (p) p.textContent = fmt(S.proxies.count);
    if (window.PAGE && PAGE.onProxies) PAGE.onProxies(S.proxies);
  } catch (e) { /* the bar simply shows nothing */ }
}

async function boot() {
  if (window.Auth) {
    await Auth.ready;
    if (!Auth.state.user) { location.replace('/?auth=login'); return; }
    const foot = document.querySelector('.side-foot');
    if (foot) foot.insertAdjacentHTML('afterbegin', '<a href="/account" data-account-label></a><span data-access-clock></span><button class="btn sm ghost" data-logout>Log out</button>');
    const main = document.querySelector('main');
    if (main) main.insertAdjacentHTML('afterbegin', '<div class="access-banner"><span data-access-clock></span><a href="/buy">Buy access ↗</a></div>');
    Auth.refresh();
  }
  const page = window.PAGE || { name: "console", title: "Console" };
  try { S.info = await api("/info"); } catch (e) {
    toast("API unreachable: " + e.message, "bad"); return;
  }
  try {
    const menu = await api("/menu"); S.menu = menu.buckets; S.kinds = menu.kinds;
  } catch (e) { S.menu = []; }
  restore();
  if ($("build")) $("build").textContent = S.info.build;
  const brand = document.querySelector(".brand");
  if (brand) {
    brand.style.cursor = "pointer";
    brand.title = "back to the landing page";
    brand.onclick = () => { location.href = "/"; };
  }
  document.title = "VORTEX · " + page.title;
  if ($("vTitle")) $("vTitle").textContent = page.title;
  renderNav(page.name);
  renderPlats(); if ($("spaces")) { renderSpaces(); }
  const stop = $("stopBtn"); if (stop) stop.onclick = stopRun;
  await loadProxies();
  await pumpRun();
  if (page.init) await page.init();
  setInterval(pumpRun, 8000);
  setInterval(() => { if (S.run && S.watching && $("feed") == null) pumpRun(); }, 15000);
}

window.App = {
  S, $, api, toast, fmt, esc, pct, clock, boot, renderPlats, renderSpaces, summarise,
  applyProgress, renderPaced, renderFeed, feedLine, startSweep, startSingle, follow,
  selectionSize, summarise, paintPlatSum,
  pumpRun, stopRun, renderNav, persist, isVerdict, runStream,
};

})();
