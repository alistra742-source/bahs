/* Administrator panel: hand out access, and release the invoices that paid into
   the operator's own wallet.

   Everything here is a deliberate act by a signed-in administrator. The server
   re-checks the flag on every call -- this file only decides what to draw. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => window.App.esc(s);
  const fmt = (n) => window.App.fmt(n);
  const clock = (s) => window.App.clock(s);

  const PLANS = ["monthly", "forever", "trial", "revoke"];
  const LABEL = { monthly: "+ 1 month", forever: "forever", trial: "trial", revoke: "revoke" };

  let users = [];
  let orders = [];

  const post = (path, body) => window.App.api(path, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  });

  function say(message, kind) {
    const box = $("grantError");
    if (box) { box.textContent = message || ""; box.className = "form-error" + (kind === "ok" ? " ok" : ""); }
    window.App.toast(message, kind === "ok" ? null : "bad");
  }

  async function grant(username, plan, orderId) {
    if (!username) { say("Pick an account first."); return; }
    if (plan === "revoke" && !confirm("Revoke every route in for " + username + "?")) return;
    try {
      const d = await post("/admin/grant", { username, plan, order_id: orderId || "" });
      say(username + " → " + d.user.plan + (orderId ? " (invoice released)" : ""), "ok");
      await Promise.all([loadUsers($("userQuery") ? $("userQuery").value : ""), loadOrders()]);
      if (window.Auth && Auth.state.user && Auth.state.user.username === username) Auth.refresh();
    } catch (e) { say(e.message); }
  }

  function grantButtons(username, orderId) {
    return PLANS.map((p) =>
      `<button class="btn sm${p === "revoke" ? " danger" : ""}" data-user="${esc(username)}" data-plan="${p}"${orderId ? ` data-order="${esc(orderId)}"` : ""}>${LABEL[p]}</button>`
    ).join(" ");
  }

  function renderUsers() {
    const body = $("userRows");
    if (!body) return;
    if (!users.length) { body.innerHTML = "<tr><td colspan=\"5\" class=\"note\">No accounts match.</td></tr>"; }
    else {
      body.innerHTML = users.map((u) => {
        const ends = u.lifetime ? "forever"
          : u.trial_pending ? "trial, not started"
          : u.remaining_seconds > 0 ? clock(u.remaining_seconds) : "—";
        return `<tr><td><b>${esc(u.username)}</b>${window.Auth && Auth.state.user && Auth.state.user.username === u.username ? ' <span class="note">(you)</span>' : ""}</td>`
          + `<td>${esc(u.plan)}</td><td>${esc(ends)}</td><td>${u.admin ? "yes" : ""}</td>`
          + `<td class="actions">${grantButtons(u.username)}</td></tr>`;
      }).join("");
    }
    wire(body);
    if ($("userCount")) $("userCount").textContent = fmt(users.length) + " shown";
  }

  function renderOrders() {
    const box = $("orderList");
    if (!box) return;
    const pending = orders.filter((o) => o.status === "awaiting_transfer" && !o.granted);
    if ($("awaitingNote")) $("awaitingNote").textContent = pending.length ? fmt(pending.length) + " awaiting a transfer" : "nothing waiting";
    if (!orders.length) { box.innerHTML = '<p class="note">No invoices yet.</p>'; return; }
    box.innerHTML = orders.map((o) => {
      const open = o.status === "awaiting_transfer" && !o.granted;
      return `<div class="order-row"><span><b>${esc(o.username)}</b> · ${esc(o.plan)} · $${esc(o.price)} · ${esc(o.currency)}</span>`
        + `<span>${esc(o.status)}${o.self_custody ? " · to your wallet" : ""}</span>`
        + (open ? `<span>${grantButtons(o.username, o.id)}</span>` : `<span class="note">${o.granted ? "released" : "closed"}</span>`)
        + `</div>`;
    }).join("");
    wire(box);
  }

  function wire(root) {
    root.querySelectorAll("[data-plan]").forEach((b) => {
      b.onclick = () => grant(b.dataset.user, b.dataset.plan, b.dataset.order || "");
    });
  }

  async function loadUsers(query) {
    try {
      const d = await window.App.api("/admin/users?limit=50&q=" + encodeURIComponent(query || ""));
      users = d.users || [];
      renderUsers();
    } catch (e) {
      if ($("userRows")) $("userRows").innerHTML = `<tr><td colspan="5" class="note">${esc(e.message)}</td></tr>`;
    }
  }

  async function loadOrders() {
    try {
      const d = await window.App.api("/admin/orders?limit=25");
      orders = d.orders || [];
      renderOrders();
    } catch (e) {
      if ($("orderList")) $("orderList").innerHTML = `<p class="note">${esc(e.message)}</p>`;
    }
  }

  function init() {
    const me = window.Auth && window.Auth.state.user;
    if ($("adminWho")) $("adminWho").textContent = me ? "signed in as " + me.username : "";
    $("grantMe").onclick = () => { if (me) { $("grantName").value = me.username; } };
    document.querySelectorAll("[data-grant]").forEach((b) => {
      b.onclick = () => grant(($("grantName").value || "").trim().toLowerCase(), b.dataset.grant, "");
    });
    $("grantName").onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); grant(e.target.value.trim().toLowerCase(), "monthly", ""); } };
    $("userSearch").onclick = () => loadUsers($("userQuery").value);
    $("userQuery").onkeydown = (e) => { if (e.key === "Enter") loadUsers(e.target.value); };
    $("ordersReload").onclick = loadOrders;
    loadUsers("");
    loadOrders();
    setInterval(() => { if (!document.hidden) loadOrders(); }, 30000);
  }

  window.Admin = { init, grant };
})();
