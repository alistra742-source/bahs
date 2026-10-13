/* The landing page's only script: mount the components, then fill them from the
   service's own endpoints. There is no copy on this page that is a static claim
   about a number -- the strip and the console frame are read from /stats,
   /info, /runs and /hits, which is also why the frame is worth looking at. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const api = async (path) => {
    const r = await fetch(path);
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  };

  let INFO = null, NOTES = {}, LIVE = null, LAST_RUN = null;

  function header(build) {
    $("head").innerHTML = `<div class="inner">
        <a class="mark" href="/">bahs</a>
        <span class="build" id="build">${UI.esc(build || "")}</span>
        <nav>
          <a href="/platforms">Platforms</a>
          <a href="/docs">Docs</a>
          <a href="/hits/">Hits</a>
          <a class="btn primary" href="/console">Launch console</a>
        </nav>
      </div>`;
  }

  function shell() {
    $("main").innerHTML =
      UI.hero() +
      UI.statsStrip() +
      `<div id="previewSlot"></div>` +
      UI.featureGrid() +
      UI.platformStrip((INFO && INFO.platforms) || []) +
      UI.howItWorks() +
      UI.pricingSection();
    $("foot").innerHTML = UI.siteFooter();
  }

  function fillStrip(stats) {
    const s = stats || {};
    /* The strip is rendered once, empty, before /stats answers -- so the values
       have to be written onto each number before it counts up. Counting up to
       the zero that was in the markup left the whole strip reading 0 next to a
       console frame that plainly said otherwise. */
    const items = UI.statItems(s);
    document.querySelectorAll(".strip .stat b").forEach((el, i) => {
      const value = items[i] ? items[i].value : 0;
      el.dataset.count = value;
      UI.countUp(el, value);
    });
    const stamp = $("stamp");
    if (!stamp) return;
    if (s.runs) {
      stamp.textContent = `counted from ${UI.fmt(s.runs)} run${s.runs === 1 ? "" : "s"} today · `
        + `${UI.fmt(Math.round(s.seconds))}s of sweeping · ${s.date}`
        + (s.active_runs ? ` · ${UI.fmt(s.active_runs)} still going` : "");
    } else if (s.active_runs) {
      stamp.textContent = "a sweep is in flight — the totals land when it ends";
    } else {
      stamp.textContent = "no sweeps yet today — this strip fills in from your own runs";
    }
  }

  function preview(stats, runs, hits) {
    const active = (runs && runs.runs || []).find((r) => r.active) || null;
    const platforms = (INFO && INFO.platforms) || [];
    const all = new Set(platforms);
    const feed = ((hits && hits.hits) || []).slice(0, 8).map(UI.hitRow);
    const data = {
      live: !!active,
      kpis: [],
      cells: [],
      feed,
      health: [],
      paced: {},
      paused: {},
      sniperNote: "",
    };

    if (active) {
      const p = active.progress || {};
      data.kpis = UI.runKpis(p, p);
      data.cells = UI.cellMarkup({
        platforms, selected: all, byPlatform: p.by_platform || {},
        paced: p.platforms_paced || {}, paused: p.platforms_paused || {},
      });
      data.paced = p.platforms_paced || {};
      data.paused = p.platforms_paused || {};
      data.health = UI.healthRows(p, p);
      data.sniperNote = `${UI.fmt(active.names)} names × ${active.platforms.length} snipers · run ${active.id}`;
    } else {
      const s = stats || {};
      data.kpis = UI.todayKpis(s, s.proxies);
      /* Nothing is selected with no run in flight, and that is what the console
         shows too. Marking every cell as picked here tinted the whole grid warm
         and spent the accent on sixteen dots that meant nothing. */
      data.cells = UI.cellMarkup({ platforms, selected: new Set(), byPlatform: {} });
      data.health = [
        ["refused by the platform", UI.fmt(s.refused || 0)],
        ["errors (never arrived)", UI.fmt(s.errors || 0)],
        ["unreadable answer", UI.fmt(s.unanswered || 0)],
        ["free names remembered", UI.fmt(s.hits_remembered || 0)],
        ["runs today", UI.fmt(s.runs || 0)],
        ["stored exits", UI.fmt(s.proxies || 0)],
      ];
      data.sniperNote = platforms.length ? `${platforms.length} snipers · nothing asked yet` : "";
    }
    $("previewSlot").innerHTML = UI.consolePreview(data);
    $("previewSlot").querySelector(".pacer").innerHTML =
      UI.pacedChips(data.paced, data.paused, "no run in flight — every sniper at its own rate");
  }

  function wireMarks() {
    const note = $("stripNote");
    const marks = $("marks");
    if (!note || !marks) return;
    const idle = note.textContent;
    const show = (p) => { note.innerHTML = `<b>${UI.esc(p)}</b> — ${UI.esc(NOTES[p] || "no rule recorded")}`; };
    marks.addEventListener("mouseover", (e) => {
      const b = e.target.closest(".mark-chip");
      if (b) show(b.dataset.p);
    });
    marks.addEventListener("focusin", (e) => {
      const b = e.target.closest(".mark-chip");
      if (b) show(b.dataset.p);
    });
    marks.addEventListener("mouseleave", () => { note.textContent = idle; });
    marks.addEventListener("focusout", () => { note.textContent = idle; });
  }

  async function load(first) {
    let stats = null, runs = null, hits = null;
    try { stats = await api("/stats"); } catch (e) { /* the strip says so */ }
    try { runs = await api("/runs"); } catch (e) { /* no run to show */ }
    try { hits = await api("/hits?limit=8"); } catch (e) { /* empty feed */ }
    if (!stats) {
      const stamp = $("stamp");
      if (stamp) stamp.textContent = "the API is not answering — nothing on this page is invented to cover for it";
      return;
    }
    const previous = LIVE ? LIVE.id : null;
    const active = (runs && runs.runs || []).find((r) => r.active) || null;
    LIVE = active;
    const changed = (active ? active.id : null) !== previous;
    if (first || changed || active) {
      preview(stats, runs, hits);
    }
    fillStrip(stats);
  }

  async function boot() {
    try { INFO = await api("/info"); } catch (e) { INFO = null; }
    for (const c of (INFO && INFO.checks) || []) NOTES[c.platform] = c.how;
    header((INFO && INFO.build) || "");
    shell();
    wireMarks();
    await load(true);
    setInterval(() => load(false), 5000);
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
