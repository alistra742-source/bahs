/* Footer — the small print, small. */
(function (UI) {
  "use strict";
  UI.siteFooter = () => `<footer class="site-foot">
      <div class="row">
        <b>VORTEX</b>
        <span class="dot-sep">·</span>
        <a href="/console">Console</a>
        <a href="/docs">Docs</a>
        <a href="/platforms">Platforms</a>
        <a href="/hits/">Hits</a>
      </div>
      <p class="fine">For checking availability of names you are entitled to check. You are
        responsible for the proxies you point it at and for each platform's terms.
        A read-only endpoint is not permission to hammer it.</p>
      <p class="fine dim2">© ${new Date().getFullYear()} VORTEX</p>
    </footer>`;
})(window.UI = window.UI || {});
