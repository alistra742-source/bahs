/* KPI — one number with a name and a subtitle. The unit is always in the label
   ("checks / s"), so a number can never be read as the one beside it. */
(function (UI) {
  "use strict";
  UI.kpi = (o) => {
    const id = o.id ? ` id="${o.id}"` : "";
    const sub = o.sub ? `<small>${o.sub}</small>` : "";
    return `<div class="kpi ${UI.esc(o.tone || "")}"><span>${UI.esc(o.label)}</span>`
      + `<b${id}>${o.value}</b>${sub}</div>`;
  };
  UI.kpiRow = (kpis, cls) =>
    `<div class="kpis ${UI.esc(cls || "")}">${kpis.map(UI.kpi).join("")}</div>`;
  /* The six numbers the console leads with, in one place because the landing
     page's preview and the live console have to agree: two definitions of
     "checks / s" is how a page ends up quietly flattering the tool. Units live
     in the labels, the sub-line says what the number counts, and a rate is never
     shown without the other rate beside it. */
  const PAIR = (p) => (p || {});

  UI.runKpis = (progress, pool) => {
    const p = PAIR(progress), q = PAIR(pool);
    const confirmed = p.confirmed_count || 0;
    return [
      { label: "checks / s", value: p.checks_per_second == null ? "—" : p.checks_per_second,
        sub: "requests done, refusals included" },
      { label: "verdicts / s", value: p.verdicts_per_second == null ? "—" : p.verdicts_per_second,
        tone: "pace", sub: "names actually resolved" },
      { label: "hits", value: UI.fmt(p.available_count || 0), tone: "free",
        sub: `${UI.fmt(confirmed)} confirmed on a second read` },
      { label: "refusals", value: UI.fmt(p.rate_limits || 0), tone: "warn",
        sub: "the platform said no" },
      { label: "unanswered", value: UI.fmt(p.unanswered || 0),
        sub: "an answer came back that said nothing" },
      q.pool_size == null
        ? { label: "proxy pool", value: "—", sub: "no run to report on" }
        : { label: "proxy pool", value: `${UI.fmt(q.pool_alive)}<span class="m">/${UI.fmt(q.pool_size)}</span>`,
            sub: `${UI.fmt(q.pool_resting)} resting · ${UI.fmt(q.pool_retired)} retired` },
    ];
  };

  UI.todayKpis = (stats, poolSize) => {
    const s = PAIR(stats);
    return [
      { label: "checks / s", value: s.checks_per_second == null ? "—" : s.checks_per_second,
        sub: `${UI.fmt(s.checks)} checks today` },
      { label: "verdicts / s", value: s.verdicts_per_second == null ? "—" : s.verdicts_per_second,
        tone: "pace", sub: `${UI.fmt(s.verdicts)} verdicts today` },
      { label: "hits", value: UI.fmt(s.hits || 0), tone: "free",
        sub: `${UI.fmt(s.runs || 0)} runs today` },
      { label: "refusals", value: UI.fmt(s.refused || 0), tone: "warn", sub: "the platform said no" },
      { label: "unanswered", value: UI.fmt(s.unanswered || 0),
        sub: `no verdict came back · ${UI.fmt(s.errors || 0)} never arrived` },
      poolSize == null
        ? { label: "proxy pool", value: "—", sub: "nothing stored" }
        : { label: "proxy pool", value: UI.fmt(poolSize), sub: "stored exits" },
    ];
  };
})(window.UI = window.UI || {});
