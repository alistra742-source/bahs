/* VerdictFeed — the streaming result list.
 *
 * Only two things belong in here: a name you can take and a name somebody holds.
 * A refusal is not a verdict about the name, so it never appears as a row -- it
 * moves the health panel instead. A free name a second exit could not confirm
 * carries its own tag, because "free" and "free, confirmed" are not the same
 * promise.
 */
(function (UI) {
  "use strict";

  UI.verdictRow = (r) => {
    const unconfirmed = r.status === "available" && r.confirmed === false;
    return `<div class="${UI.esc(r.status)}">`
      + `<span class="t">${UI.esc(r.t || "")}</span>`
      + `<span class="u">${UI.esc(r.username)}`
      + (unconfirmed ? '<span class="tag">unconfirmed</span>' : "")
      + (r.status === "available" && r.confirmed === true ? '<span class="tag ok">confirmed</span>' : "")
      + `</span><span class="p">${UI.esc(r.platform)}</span></div>`;
  };

  UI.verdictFeed = (rows, empty) => (rows && rows.length
    ? rows.map(UI.verdictRow).join("")
    : `<div class="empty">${UI.esc(empty || "nothing yet — a run puts its verdicts here, free ones first")}</div>`);
})(window.UI = window.UI || {});
