/* ConsolePreview — the console itself, framed, fed by this service's numbers.
 *
 * Everything inside the frame is built from the same components the real console
 * uses (KPI, SniperCell, VerdictFeed, HealthPanel), so it cannot drift into a
 * pretty picture of a tool that does not exist. If the service has never run a
 * sweep, the frame honestly shows zeroes and its empty feed -- which is what the
 * console shows too. When a run is in flight the preview follows that run.
 */
(function (UI) {
  "use strict";

  UI.consolePreview = (d) => {
    const data = d || {};
    const live = !!data.live;
    return `<section class="preview">
      <h2 class="sec">The console</h2>
      <p class="lead">Dashboard front and centre: ${live
        ? "this is the run in flight right now."
        : "today's numbers, straight from the service."}</p>
      <div class="frame">
        <div class="chrome">
          <i class="dot ${live ? "live" : ""}"></i><b>bahs</b>
          <span class="path">/console</span>
          <span class="tag ${live ? "ok" : ""}">${live ? "run in flight" : "today"}</span>
        </div>
        <div class="screen">
          ${UI.kpiRow(data.kpis || [], "primary")}
          <div class="grid two screen-cols">
            <div class="card">
              <header><h3>Snipers</h3><div class="sep"></div>
                <span class="note">${UI.esc(data.sniperNote || "")}</span></header>
              <div class="body"><div class="cells compact">${data.cells || ""}</div></div>
            </div>
            <div class="card">
              <header><h3>Verdicts</h3><div class="sep"></div>
                <span class="note">free and taken only — a refusal is not a verdict</span></header>
              <div class="body"><div class="feed">${UI.verdictFeed(data.feed)}</div></div>
            </div>
          </div>
          <div class="card screen-health">
            <header><h3>Run health</h3><div class="sep"></div>
              <span class="note">what is not a verdict</span></header>
            <div class="body">
              ${UI.healthPanel(data.health || [])}
              <h3 class="sub-h">Paced and paused snipers</h3>
              <div class="pacer">${UI.pacedChips(data.paced, data.paused)}</div>
            </div>
          </div>
        </div>
      </div>
      <p class="cap">
        <a class="btn primary" href="/console">Open the console</a>
        <span class="fine">Same page, no preview frame: start a sweep, watch the verdicts stream,
          stop it whenever. The sweep runs on the server, so closing this tab does not end it.</span>
      </p>
    </section>`;
  };

  /* A hit from /hits as a feed row. The hit store and the feed carry the same
     facts, so the preview shows what the console would have shown. */
  UI.hitRow = (h) => ({
    t: new Date((h.at || 0) * 1000).toLocaleTimeString("en-GB"),
    username: h.username,
    platform: h.platform,
    status: "available",
    confirmed: !!h.confirmed,
  });
})(window.UI = window.UI || {});
