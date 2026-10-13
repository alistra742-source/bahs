/* SniperCell — one platform, its live counter, and its state.
 *
 * The state is the whole point of the cell. A platform the pacer has slowed is
 * still answering, a platform sitting out is not, and a platform the run refuses
 * to keep hammering looks different again; showing all three as "a platform" is
 * how a throttled run reads as a broken one.
 *
 *   idle     nothing asked yet this run
 *   healthy  answering at its full rate
 *   paced    asked slower on purpose, still producing
 *   paused   sat out until the platform's cooldown expires
 *   blocked  most of its checks came back refused
 */
(function (UI) {
  "use strict";

  UI.cellState = (platform, opts) => {
    const o = opts || {};
    const row = (o.byPlatform || {})[platform];
    const paced = (o.paced || {})[platform];
    const paused = (o.paused || {})[platform];
    if (paused) return "paused";
    if (row && row.checked >= 5 && (row.blocked / row.checked) >= 0.5) return "blocked";
    if (paced) return "paced";
    if (row && row.checked > 0) return "healthy";
    return "idle";
  };

  UI.sniperCell = (o) => {
    const state = o.state || "idle";
    const count = o.count ? `<b>${UI.fmt(o.count)}</b>` : "";
    const title = o.title || o.platform;
    return `<button type="button" class="cell ${o.selected ? "on" : ""} ${UI.esc(state)}"`
      + ` data-p="${UI.esc(o.platform)}" title="${UI.esc(title)}">`
      + `<i></i>${UI.esc(o.platform)}${count}</button>`;
  };

  UI.sniperGrid = (cells, cls) => `<div class="cells ${UI.esc(cls || "")}">${cells.join("")}</div>`;

  /* Just the cells, without the wrapper: a page that already has its own grid
     container (the console's #platList) needs the items, because nesting one
     grid inside another collapsed the whole grid into a single 179px column. */

  /* The whole grid from what the console already knows: the selected set, the
     run's per-platform counters, and the pacer's snapshot. */
  UI.cellMarkup = (opts) => UI.sniperGridFrom(opts)
    .replace(/^<div class="cells[^"]*">/, "").replace(/<\/div>$/, "");

  UI.sniperGridFrom = (opts) => {
    const o = opts || {};
    const rows = (o.byPlatform || {});
    return UI.sniperGrid((o.platforms || []).map((p) => {
      const row = rows[p];
      return UI.sniperCell({
        platform: p,
        selected: o.selected ? o.selected.has(p) : false,
        state: UI.cellState(p, o),
        count: row ? row.checked : 0,
        title: row
          ? `${row.checked} checks · ${row.available} free · ${row.taken} held · ${row.blocked} refused · ${row.error} errored`
          : "nothing asked on this platform yet",
      });
    }));
  };
})(window.UI = window.UI || {});
