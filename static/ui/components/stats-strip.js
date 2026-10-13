/* StatsStrip — today, counted from real runs.
 *
 * Four numbers the service actually keeps: names checked, platforms live, free
 * names found, and the average verdict rate of the runs that produced them. A
 * fresh install shows zeroes, which is the honest thing for it to show, and the
 * strip says where the numbers come from so nobody has to guess. */
(function (UI) {
  "use strict";
  UI.statItems = (s) => {
    const d = s || {};
    return [
      { label: "names checked today", value: d.checks || 0, tone: "" },
      { label: "platforms live", value: d.platforms || 0, tone: "" },
      { label: "hits found", value: d.hits || 0, tone: "free" },
      { label: "verdicts / s", value: d.verdicts_per_second || 0, tone: "pace" },
    ];
  };
  UI.statsStrip = (s) => {
    const items = UI.statItems(s);
    return `<section class="strip" aria-label="today">
      <div class="stats">
        ${items.map((i) => `<div class="stat ${i.tone}">
            <b data-count="${i.value}">0</b><span>${UI.esc(i.label)}</span></div>`).join("")}
      </div>
      <p class="stamp" id="stamp"></p>
    </section>`;
  };
})(window.UI = window.UI || {});
