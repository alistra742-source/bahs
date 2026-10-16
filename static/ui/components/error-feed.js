/* ErrorFeed — the checks that never reached a verdict.
 *
 * The counterpart to VerdictFeed, and the honesty half of the console. A row
 * here is a request that never came back with an answer about the name: a
 * transport failure, an exit that opened nothing, a read that never landed. It
 * is therefore *not* a check of the name, it is never counted as one in the
 * verdicts list, and it says nothing about whether the handle is free or held.
 *
 * The two lists are separate on purpose. Mixing them into one stream is how a
 * run that resolved nothing reads as a run that resolved thousands of names
 * and found no hits -- the same table, the same row count, opposite meanings.
 * The failure class is shown per row because "never arrived" is the same
 * number whether the tunnel died at the front door (a proxy problem) or the
 * far side stayed quiet (a target problem), and only the class tells which.
 */
(function (UI) {
  "use strict";

  UI.errorRow = (r) => {
    const kind = r.e || r.detail || "error";
    const ms = r.ms == null ? "" : `${Math.round(r.ms)} ms`;
    return `<div class="error"${ms ? ` title="${UI.esc(ms)}"` : ""}>`
      + `<span class="t">${UI.esc(r.t || "")}</span>`
      + `<span class="u">${UI.esc(r.username)}</span>`
      + `<span class="e">${UI.esc(kind)}</span>`
      + `<span class="p">${UI.esc(r.platform)}</span></div>`;
  };

  UI.errorFeed = (rows, empty) => (rows && rows.length
    ? rows.map(UI.errorRow).join("")
    : `<div class="empty">${UI.esc(empty
      || "nothing yet — a request that never came back with an answer lands here, and only here")}</div>`);
})(window.UI = window.UI || {});
