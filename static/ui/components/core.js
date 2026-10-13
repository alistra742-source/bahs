/* The component layer, one file per component.
 *
 * These are plain functions that return markup, not a framework: the whole site
 * is a FastAPI process serving static files, so there is no build step to hang a
 * bundler off. Each file registers itself on `UI` and can be loaded alone, and
 * every component is used twice -- once by the landing page's preview and once
 * by the live console -- so the picture on the front page cannot drift from the
 * tool behind it.
 *
 * Classic scripts, so each one is wrapped: a top-level `const` in a classic
 * script shares the page's lexical scope with every other classic script, which
 * is how this app once killed every page with a duplicate `$`.
 */
(function (UI) {
  "use strict";

  UI.esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  UI.fmt = (n) => (n == null ? "—" : Number(n).toLocaleString("en-US"));
  UI.pct = (x) => (x == null ? "—" : Math.round(x * 100) + "%");

  /* Monochrome platform marks, one file per platform. The two the icon set does
     not carry (minecraft, guns.lol) fall back to a text mark rather than to a
     wrong logo. */
  const MARKS = {
    discord: 1, roblox: 1, tiktok: 1, x: 1, instagram: 1, github: 1, telegram: 1,
    youtube: 1, reddit: 1, facebook: 1, steam: 1, bluesky: 1, kick: 1, huggingface: 1,
  };
  UI.mark = (platform) => (MARKS[platform]
    ? `<img src="/ui/logos/${UI.esc(platform)}.svg" alt="" width="16" height="16" loading="lazy">`
    : `<span class="txt-mark">${UI.esc(String(platform).slice(0, 2))}</span>`);

  /* A number counting up to its real value. Used only where the value is a fact
     the page already has: an animation that outlives its number is a lie told in
     motion. */
  UI.countUp = (el, value, ms) => {
    if (!el) return;
    const target = Number(value) || 0;
    const decimals = String(value).includes(".") ? 1 : 0;
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      el.textContent = UI.fmt(target.toFixed(decimals));
      return;
    }
    const t0 = performance.now();
    const step = (now) => {
      const p = Math.min(1, (now - t0) / (ms || 700));
      const eased = 1 - Math.pow(1 - p, 3);
      el.textContent = UI.fmt((target * eased).toFixed(decimals));
      if (p < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  };
})(window.UI = window.UI || {});
