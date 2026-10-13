// Browser-only fixtures validate UI wiring; NOT a live CAPTCHA/payment test.
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
const require=createRequire(process.env.PLAYWRIGHT_PACKAGE_JSON||import.meta.url);
const {chromium}=require('playwright');
const base=process.env.TEST_ORIGIN||'http://127.0.0.1:3001';
const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH,args:['--no-sandbox','--disable-dev-shm-usage','--disable-gpu']});
try {
 for(const width of [1440,390]){
  const context=await browser.newContext({viewport:{width,height:950}});
  const now=Date.now()/1000;
  const order={id:'browser-fixture-only',plan:'monthly',currency:'SOL',price:'8.99',address:'TEST-ONLY-NOT-A-REAL-DEPOSIT-ADDRESS',amount:'0.1',created:now,expires:now+3600,status:'waiting',granted:0,error:'',network:'Solana',seconds_left:3600,expired:false};
  let paid=false;
  await context.route('**/auth/status',r=>r.fulfill({json:{user:{id:'fixture',username:'ui_fixture',access:true,lifetime:paid,trial_until:now+60,paid_until:0},csrf:'fixture-csrf'}}));
  await context.route('**/billing/plans',r=>r.fulfill({json:{ready:true}}));
  await context.route('**/billing/orders',r=>r.fulfill({json:{orders:[]}}));
  await context.route('**/billing/checkout',async r=>{
    assert.equal(r.request().headers()['x-csrf-token'],'fixture-csrf');
    assert.deepEqual(r.request().postDataJSON(),{plan:'monthly',currency:'SOL'});
    await r.fulfill({json:order});
  });
  await context.route('**/billing/orders/browser-fixture-only',r=>r.fulfill({json:paid?{...order,status:'finished',granted:1}:order}));
  await context.route('**/proxies?*',r=>r.fulfill({json:{count:1,proxies:[],managed:true}}));
  await context.route('**/runs',r=>r.fulfill({json:{runs:[],hits_count:0}}));
  const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));page.on('console',m=>{if(m.type()==='error')errors.push(m.text());});
  await page.goto(base+'/console',{waitUntil:'networkidle'});
  assert.equal(await page.locator('#platList .cell').count(),16);
  assert.equal(await page.locator('#nav a[href="/buy"]').count(),1);
  assert.equal(await page.locator('#nav a[href="/account"]').count(),1);
  assert.match(await page.locator('.access-banner [data-access-clock]').innerText(),/TRIAL/);
  await page.locator('#platList .cell[data-p="discord"]').click();
  assert.match(await page.locator('#platSum').innerText(),/1 of 16/);
  await page.screenshot({path:`data/console-fixture-${width}.png`,fullPage:true});
  await page.goto(base+'/buy',{waitUntil:'networkidle'});
  await page.locator('[data-plan="monthly"]').click();
  await page.waitForSelector('.checkout-overlay');
  assert.equal(await page.locator('main').evaluate(e=>e.inert),true);
  assert.match(await page.locator('.checkout-card').innerText(),/TEST-ONLY-NOT-A-REAL/);
  assert.equal(await page.locator('.checkout-overlay').evaluate(e=>e.scrollWidth>e.clientWidth),false);
  await page.screenshot({path:`data/checkout-fixture-${width}.png`,fullPage:true});
  paid=true;
  await page.waitForSelector('.checkout-card a[href="/console"]',{timeout:16000});
  assert.match(await page.locator('.checkout-card').innerText(),/Payment complete/);
  await page.locator('#closeInvoice').click();
  assert.equal(await page.locator('.checkout-overlay').count(),0);
  assert.equal(await page.locator('main').evaluate(e=>e.inert),false);
  assert.match(page.url(),/\/buy$/);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth-innerWidth),0);
  assert.deepEqual(errors,[]);
  console.log(`PASS ${width}px fixture UI: private console grid/navigation/countdown, CSRF checkout payload, full-page invoice, confirmed update, X returns to original page`);
  await context.close();
 }
}finally{await browser.close();}
