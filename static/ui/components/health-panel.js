/* HealthPanel — what is *not* a verdict.
 *
 * This panel exists because the alternative is burying refusals in the results
 * list, which reads as the platform having answered about the name. Everything
 * here is the run or the proxies talking about themselves: refused, unanswered,
 * never arrived, plus the pool's own state. It is also where the two platform
 * states that users ask about live -- a sniper asked slower on purpose, and one
 * sat out entirely. */
(function (UI) {
  "use strict";

  UI.poolRows = (pool) => {
    const q = pool || {};
    if (q.pool_size == null) return [];
    return [
      ["proxies alive", `${UI.fmt(q.pool_alive)} of ${UI.fmt(q.pool_size)}`],
      ["resting", UI.fmt(q.pool_resting || 0)],
      ["retired this run", UI.fmt(q.pool_retired || 0)],
      ["refused by a platform", UI.fmt(q.pool_blocked || 0)],
      ["requests carried", UI.fmt(q.proxy_attempts || 0)],
      ["failed at the transport", UI.fmt(q.proxy_failures || 0)],
    ];
  };

  UI.healthRows = (progress, pool) => {
    const p = progress || {};
    return [
      ["refused by the platform", UI.fmt(p.rate_limits || 0)],
      ["errors (never arrived)", UI.fmt(p.error || 0)],
      ["unreadable answer", UI.fmt(p.unanswered || 0)],
      ["banned accounts", UI.fmt(p.banned || 0)],
      ["free confirmed twice", UI.fmt(p.confirmed_count || 0)],
      ["first read corrected", UI.fmt(p.corrected_count || 0)],
      ["current target", p.current_target ? "asking " + p.current_target : "idle"],
      ...UI.poolRows(pool),
    ];
  };

  UI.healthPanel = (rows) => `<div class="kv">${rows
    .map(([k, v]) => `<span>${UI.esc(k)}</span><b>${UI.esc(v)}</b>`).join("")}</div>`;

  UI.pacedChips = (paced, paused, quiet) => {
    const chips = [];
    for (const [p, v] of Object.entries(paced || {})) {
      chips.push(`<b>${UI.esc(p)} · ${v.parallel} of ${v.ceiling} in flight</b>`);
    }
    for (const [p, v] of Object.entries(paused || {})) {
      chips.push(`<b class="paused">${UI.esc(p)} · sat out ${v.seconds_left}s${v.reason ? " — " + UI.esc(v.reason) : ""}</b>`);
    }
    return chips.length ? chips.join("")
      : `<span class="note">${UI.esc(quiet || "every sniper is at the run's full rate")}</span>`;
  };
})(window.UI = window.UI || {});
