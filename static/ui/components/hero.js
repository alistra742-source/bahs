(function (UI) {
  'use strict';
  UI.hero = () => `
<section class="hero hero-v2">
  <div class="hero-copy">
    <p class="eyebrow"><i class="signal-dot"></i> THE HANDLE HUNT. REENGINEERED.</p>
    <h1>Find your<br>next <span class="outline-word">identity.</span></h1>
    <p class="sub">Sixteen platforms. A single sweep.<br>Turn the noise into your next great handle.</p>
    <div class="cta"><button class="btn primary big" data-auth="signup">Start your 60-second trial <span>↗</span></button><a class="link" href="#previewSlot">Inside the console ↓</a></div>
    <div class="hero-foot"><span>01 / ALL PLATFORMS</span><span>02 / VERIFIED LEADS</span><span>03 / YOUR EDGE</span></div>
  </div>
  <div class="scanner" aria-label="Animated illustration of a username scan">
    <div class="scanner-head"><span><i class="signal-dot"></i> SIGNAL ENGINE</span><span>VISUAL DEMO / 001</span></div>
    <div class="orbit-scene" aria-hidden="true">
      <div class="orbit orbit-a"></div><div class="orbit orbit-b"></div><div class="orbit orbit-c"></div>
      <div class="crosshair horizontal"></div><div class="crosshair vertical"></div>
      <div class="scanner-core"><span class="core-icon">b↗</span><small>VORTEX // SEARCH</small></div>
      <div class="orbit-point point-a">@<b>discord</b></div><div class="orbit-point point-b">#<b>github</b></div><div class="orbit-point point-c">↗<b>roblox</b></div>
      <span class="axis-label axis-a">36°  //  SEARCH SPACE</span><span class="axis-label axis-b">16 NODES CONNECTED</span>
    </div>
    <div class="scan-readout"><span>SEARCHING THE POSSIBILITY SPACE</span><b id="demoTarget">@your_next_handle</b><div class="scan-track"><i></i></div></div>
    <div class="scanner-footer"><span>ILLUSTRATION, NOT LIVE VERDICTS</span><span class="scan-bars">▂ ▅ ▃ ▇ ▄ ▆</span></div>
  </div>
</section>`;
})(window.UI = window.UI || {});
