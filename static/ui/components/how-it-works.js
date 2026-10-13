/* HowItWorks — three steps, numbered. Anything longer belongs in the docs. */
(function (UI) {
  "use strict";
  UI.STEPS = [
    ["Create your account", "Pass the security check. Eligible accounts get a single 60-second trial across all sixteen platforms."],
    ["Pick snipers and name spaces", "Sixteen platforms, any combination of length × kind. Each cell shows the real size of that space before you spend a check on it."],
    ["Sweep, then take the name", "Verdicts stream in live. Free names are re-asked on a second exit; a confirmed hit carries its registration link."],
  ];
  UI.howItWorks = (steps) => `<section class="how">
      <h2 class="sec">How it runs</h2>
      <ol class="steps">${(steps || UI.STEPS).map((s, i) => `<li>
        <span class="n">${i + 1}</span><div><h3>${UI.esc(s[0])}</h3><p>${UI.esc(s[1])}</p></div>
      </li>`).join("")}</ol>
    </section>`;
})(window.UI = window.UI || {});
