/* FeatureCard — title and one line. Six of them, no icons, no illustrations:
   the product is a tool and the copy should read like its changelog. */
(function (UI) {
  "use strict";
  UI.FEATURES = [
    ["16 platforms, one sweep",
     "Discord, Roblox, TikTok, X, Instagram, GitHub and ten more, checked in parallel through your own exits."],
    ["Proxy pool with rate-based retirement",
     "A host that starts failing is rested, then written off. The run does not stop for it."],
    ["Free verdicts confirmed on a second exit",
     "Every available name is re-asked through a different exit. What a second read cannot prove is labelled unconfirmed."],
    ["Runs that survive tab closure",
     "The sweep is a task on the server. Close the tab, lose signal, come back and reattach to the tail."],
    ["Platform pacer — backs off, never drops",
     "Refused too often, a platform is asked slower and keeps producing verdicts instead of being dropped or reported as a wall."],
    ["Discord webhook alerts",
     "Hits posted to your webhook as they land, batched and rate-limited."],
  ];
  UI.featureCard = (f) => `<article class="feat"><h3>${UI.esc(f[0])}</h3><p>${UI.esc(f[1])}</p></article>`;
  UI.featureGrid = (features) => `<section class="feats">
      <h2 class="sec">What it does</h2>
      <div class="feat-grid">${(features || UI.FEATURES).map(UI.featureCard).join("")}</div>
    </section>`;
})(window.UI = window.UI || {});
