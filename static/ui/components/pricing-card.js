(function(UI) {
  'use strict';
  UI.PRICING = [
    {name:'Monthly',price:'$8.99',per:'/ month',blurb:'Every platform. Every tool. One month of access.',lines:['All 16 platform snipers','Proxy-rotated checks + live verdicts','Persistent hits + Discord alerts','Crypto renewal is manual; no automatic debit'],cta:{label:'Get monthly access ↗',href:'/buy?plan=monthly',primary:false}},
    {name:'Forever',price:'$45.99',per:'one payment',blurb:'Your permanent edge. No renewal date.',lines:['Everything in Monthly','Lifetime access to this service','Priority support eligibility','Pay once. Keep the full workspace.'],cta:{label:'Get forever access ↗',href:'/buy?plan=forever',primary:true},note:'ONE AND DONE'}
  ];
  UI.pricingCard = c => `<article class="price ${c.cta.primary?'pick':''}"><header><span class="name">${UI.esc(c.name)}</span>${c.note?`<span class="tag">${UI.esc(c.note)}</span>`:''}</header><p class="num"><b>${UI.esc(c.price)}</b><span>${UI.esc(c.per)}</span></p><p class="blurb">${UI.esc(c.blurb)}</p><ul>${c.lines.map(l=>`<li>${UI.esc(l)}</li>`).join('')}</ul><a class="btn ${c.cta.primary?'primary':''}" href="${c.cta.href}">${UI.esc(c.cta.label)}</a></article>`;
  UI.pricingSection = cards => `<section class="pricing" id="pricing"><p class="eyebrow">YOUR NEXT MOVE</p><h2 class="section-title">An unfair edge.<br>A fair price.</h2><p class="lead">Try all sixteen platforms for 60 seconds. Then pick your pace.</p><div class="price-grid">${(cards||UI.PRICING).map(UI.pricingCard).join('')}</div><p class="fine">Crypto payments: SOL · LTC · ETH · BTC · USDT on Solana · USDC on Ethereum. Network fees and minimums apply.</p></section>`;
})(window.UI = window.UI || {});
