/* Hero — one headline, one subhead, one button. The tool is the pitch, so the
   hero's job is to get out of the way of the console below it. */
(function (UI) {
  "use strict";
  UI.hero = () => `
<section class="hero">
  <p class="eyebrow">proxy-rotated username checks</p>
  <h1>Sixteen platforms.<br>One sweep.<br>Every name.</h1>
  <p class="sub">Proxy-rotated username checks across the platforms that matter.
     Confirmed hits. Live console. No sampling.</p>
  <div class="cta">
    <a class="btn primary big" href="/console">Launch console</a>
    <a class="link" href="/docs">How a verdict is reached</a>
  </div>
  <p class="fine">Runs on your own box, over your own proxies. The console below is
     reading this service's own numbers, not a screenshot.</p>
</section>`;
})(window.UI = window.UI || {});
