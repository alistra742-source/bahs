/* PlatformStrip — the sixteen snipers, monochrome marks, and the rule each one
   decides a name by. The rules are the service's own CHECK_NOTES, so hovering a
   mark tells you what "taken" means on that platform rather than making you
   trust the verdict. */
(function (UI) {
  "use strict";
  UI.platformStrip = (platforms) => `<section class="platforms">
      <h2 class="sec">Sixteen snipers</h2>
      <div class="marks" id="marks">${(platforms || []).map((p) =>
        `<button type="button" class="mark-chip" data-p="${UI.esc(p)}">
           ${UI.mark(p)}<span>${UI.esc(p)}</span></button>`).join("")}</div>
      <p class="strip-note" id="stripNote">Hover a sniper to read the rule it decides a name by.</p>
    </section>`;
})(window.UI = window.UI || {});
