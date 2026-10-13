/* PricingCard — two tiers, big numbers, one button each.
 *
 * The self-hosted tier is the real product: this service, on your box, over your
 * proxies. The managed tier is a placeholder price -- set your own in PRICING
 * below and the whole section follows. */
(function (UI) {
  "use strict";

  UI.PRICING = [
    {
      name: "Self-hosted",
      price: "$0",
      per: "forever",
      blurb: "This service, on your box or your own hosted project.",
      lines: [
        "16 platforms, the full sniper grid",
        "Your proxy list, rotated with rate-based retirement",
        "Platform pacer, webhook alerts, the whole API",
        "No accounts, no per-check metering",
      ],
      cta: { label: "Launch console", href: "/console", primary: true },
    },
    {
      name: "Managed",
      price: "$29",
      per: "/ month",
      blurb: "We run it: exits included, alerts configured, nothing to babysit.",
      lines: [
        "Rotating exits, sized to the run",
        "Runs kept alive and resumed for you",
        "Discord alerts wired to your server",
        "Placeholder price — change it in pricing-card.js",
      ],
      cta: { label: "Read the docs", href: "/docs", primary: false },
      note: "placeholder pricing",
    },
  ];

  UI.pricingCard = (c) => `<article class="price ${c.cta.primary ? "pick" : ""}">
      <header><span class="name">${UI.esc(c.name)}</span>
        ${c.note ? `<span class="tag">${UI.esc(c.note)}</span>` : ""}</header>
      <p class="num"><b>${UI.esc(c.price)}</b><span>${UI.esc(c.per)}</span></p>
      <p class="blurb">${UI.esc(c.blurb)}</p>
      <ul>${c.lines.map((l) => `<li>${UI.esc(l)}</li>`).join("")}</ul>
      <a class="btn ${c.cta.primary ? "primary" : ""}" href="${UI.esc(c.cta.href)}">${UI.esc(c.cta.label)}</a>
    </article>`;

  UI.pricingSection = (cards) => `<section class="pricing">
      <h2 class="sec">Pricing</h2>
      <p class="lead">Self-host it for nothing. Pay only if you would rather not run the exits yourself.</p>
      <div class="price-grid">${(cards || UI.PRICING).map(UI.pricingCard).join("")}</div>
    </section>`;
})(window.UI = window.UI || {});
