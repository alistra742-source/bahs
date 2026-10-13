(function(){
  'use strict';
  let coin='SOL', current=null, poll=null, timer=null, openedFrom=null;
  const $=id=>document.getElementById(id), esc=s=>Auth.esc(s);
  const coins=[['SOL','Solana'],['LTC','Litecoin'],['ETH','Ethereum'],['BTC','Bitcoin'],['USDT','Solana · SPL'],['USDC','Ethereum · ERC-20']];
  const statuses={waiting:'Awaiting payment',confirming:'Payment detected · awaiting confirmations',confirmed:'Confirmed · settlement in progress',sending:'Forwarding to merchant',finished:'Payment complete',partially_paid:'Partial payment · access not granted',expired:'Provider invoice expired',failed:'Payment failed',refunded:'Refunded',review:'Payment requires support review',creating:'Generating invoice'};
  function close(){clearInterval(poll);clearInterval(timer);poll=timer=null;$('checkoutHost').innerHTML='';document.body.style.overflow='';document.querySelector('main').inert=false;openedFrom?.focus();loadOrders();}
  function tick(){
    if(!current)return;
    const seconds=Math.max(0,Math.floor(current.expires-Date.now()/1000));
    const el=$('invoiceClock');if(el)el.textContent=String(Math.floor(seconds/60)).padStart(2,'0')+':'+String(seconds%60).padStart(2,'0');
    const expired=$('invoiceExpired');if(expired)expired.hidden=seconds>0||!!current.granted;
  }
  function render(order){
    current=order;
    const host=$('checkoutHost');
    host.innerHTML=`<section class="checkout-overlay" role="dialog" aria-modal="true" aria-labelledby="invoiceTitle" tabindex="-1"><div class="checkout-top"><a href="/">VORTEX ↗</a><span>SECURE CRYPTO CHECKOUT</span><button id="closeInvoice" aria-label="Close checkout and return">×</button></div><div class="checkout-card"><p class="eyebrow">${esc(order.plan.toUpperCase())} ACCESS / $${esc(order.price)} USD</p><h2 id="invoiceTitle">${order.granted?'You’re on the inside.':'Your next move.'}</h2><div class="checkout-status"><span><i class="signal-dot"></i>${esc(statuses[order.status]||order.status)}</span><b class="checkout-clock" id="invoiceClock">--:--</b></div>${order.granted?'<a class="btn primary big" href="/console">Launch console ↗</a>':order.address?`<div class="deposit-box"><label>SEND EXACTLY · ${esc(order.network)}</label><code>${esc(order.amount)} ${esc(order.currency)}</code><button data-copy="amount">Copy amount</button></div><div class="deposit-box"><label>ONE-TIME DEPOSIT ADDRESS · ${esc(order.network)}</label><code>${esc(order.address)}</code><button data-copy="address">Copy address</button></div><p class="checkout-warning">Only send ${esc(order.currency)} on ${esc(order.network)}. Include network fees separately so the full amount reaches this address.</p>`:''}<p class="checkout-warning" id="invoiceExpired" hidden>The checkout window ended. Do not send new funds. This invoice remains under silent reconciliation for two more hours. <button class="btn" id="newInvoice">Back to plans</button></p>${order.error?`<div class="checkout-error">${esc(order.error)}</div>`:''}<p class="checkout-note">Invoice ${esc(order.id)}<br>Access unlocks only after sufficient funds are confirmed and settlement completes. Closing this page does not stop server monitoring. Never send a wallet seed phrase.</p><p class="checkout-note">Monthly access renews by a new payment, not an automatic wallet debit. Processor/network minimums and fees apply.</p><p class="form-error" id="invoiceError" role="alert"></p></div></section>`;
    $('closeInvoice').onclick=close;
    if($('newInvoice'))$('newInvoice').onclick=close;
    host.querySelectorAll('[data-copy]').forEach(b=>b.onclick=async()=>{try{await navigator.clipboard.writeText(order[b.dataset.copy]);b.textContent='Copied ✓';}catch{$('invoiceError').textContent='Copy is unavailable here. Select the value above and copy it manually.';}});
    tick();
  }
  async function show(order){
    openedFrom=document.activeElement;render(order);document.body.style.overflow='hidden';document.querySelector('main').inert=true;$('closeInvoice').focus();
    clearInterval(timer);clearInterval(poll);timer=setInterval(tick,1000);
    poll=setInterval(async()=>{if(document.hidden)return;try{const data=await Auth.json('/billing/orders/'+order.id);if(data.status!==current.status||data.granted!==current.granted){render(data);if(data.granted)Auth.refresh();}else current=data;}catch(e){const el=$('invoiceError');if(el)el.textContent=e.message;}},10000);
  }
  async function loadOrders(){
    if(!Auth.state.user)return;
    try{const d=await Auth.json('/billing/orders');$('orderList').innerHTML=d.orders.length?d.orders.map(o=>`<div class="order-row"><span>${esc(o.plan)} · $${esc(o.price)} · ${esc(o.currency)}</span><span>${esc(statuses[o.status]||o.status)}</span><button data-order="${esc(o.id)}">View invoice ↗</button></div>`).join(''):'<p>No invoices yet.</p>';
      $('orderList').querySelectorAll('[data-order]').forEach(b=>b.onclick=()=>show(d.orders.find(o=>o.id===b.dataset.order)));
    }catch(e){$('orderList').textContent=e.message;}
  }
  document.addEventListener('keydown',e=>{if(!$('checkoutHost').firstChild)return;if(e.key==='Escape'){e.preventDefault();close();}if(e.key==='Tab'){const nodes=[...$('checkoutHost').querySelectorAll('a,button,[tabindex="0"]')].filter(el=>el.offsetParent!==null);if(!nodes.length)return;const first=nodes[0],last=nodes.at(-1);if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}}});
  async function boot(){
    await Auth.ready;
    $('coinPicker').innerHTML=coins.map(([symbol,network])=>`<button class="${symbol===coin?'on':''}" data-coin="${symbol}" title="${network}">${symbol}</button>`).join('');
    $('coinPicker').querySelectorAll('button').forEach(b=>b.onclick=()=>{coin=b.dataset.coin;$('coinPicker').querySelectorAll('button').forEach(x=>x.classList.toggle('on',x===b));});
    let config;try{config=await Auth.json('/billing/plans');}catch{config={ready:false};}
    if(!config.ready){$('buySetup').hidden=false;$('buySetup').textContent='Live payments are not enabled. The requested free/unlimited blockchain setup has not been configured; the alternative processor has fees. No deposit address is generated and no funds are accepted.';}
    document.querySelectorAll('[data-plan]').forEach(b=>b.onclick=async()=>{
      if(!Auth.state.user){Auth.open('signup');return;}
      if(!config.ready){$('buySetup').scrollIntoView({behavior:'smooth',block:'center'});return;}
      b.disabled=true;const original=b.textContent;b.textContent='Generating your invoice…';
      try{await show(await Auth.json('/billing/checkout',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({plan:b.dataset.plan,currency:coin})}));}
      catch(e){$('buySetup').hidden=false;$('buySetup').textContent=e.message;}
      finally{b.disabled=false;b.textContent=original;}
    });
    await loadOrders();
    if(!Auth.state.user)Auth.open('signup');
  }
  boot();
})();
