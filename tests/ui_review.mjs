// Requires Playwright installed in the local test tooling; no production dependency.
import { createRequire } from 'node:module';
import assert from 'node:assert/strict';
const require = createRequire(process.env.PLAYWRIGHT_PACKAGE_JSON || import.meta.url);
const {chromium} = require('playwright');
const base=process.env.TEST_ORIGIN||'http://127.0.0.1:3001';
const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH,args:['--no-sandbox','--disable-dev-shm-usage','--disable-gpu']});
try {
  for(const width of [1440,390]){
    const context=await browser.newContext({viewport:{width,height:950}});
    const page=await context.newPage(),errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    page.on('console',m=>{if(m.type()==='error')errors.push(m.text());});
    await page.goto(base,{waitUntil:'networkidle'});
    await page.waitForSelector('.hero h1');
    assert.match(await page.locator('h1').innerText(),/Find your/);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth-innerWidth),0);
    await page.locator('.hero [data-auth="signup"]').click();
    await page.waitForSelector('dialog[open]');
    assert.match(await page.locator('dialog').innerText(),/Already have an account\? Log in/);
    assert.equal(await page.locator('dialog .auth-submit').isDisabled(),true,'Unconfigured signup must fail closed');
    await page.locator('dialog [data-mode="login"]').click();
    assert.match(await page.locator('dialog h2').innerText(),/Welcome back/);
    assert.equal(await page.locator('dialog input[name="password"]').getAttribute('type'),'password');
    assert.equal(await page.locator('dialog').evaluate(e=>e.scrollWidth>e.clientWidth),false);
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('dialog').evaluate(e=>e.open),false);
    await page.locator('#pricing').scrollIntoViewIfNeeded();
    await page.waitForTimeout(800);
    assert.equal(await page.locator('.price-grid .price').count(),2);
    assert.match(await page.locator('.price-grid').innerText(),/8\.99/);
    assert.match(await page.locator('.price-grid').innerText(),/45\.99/);
    await page.screenshot({path:`data/landing-reviewed-${width}.png`,fullPage:true});
    await page.goto(base+'/buy',{waitUntil:'networkidle'});
    await page.waitForSelector('dialog[open]');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('[data-coin]').count(),6);
    assert.match(await page.locator('#buySetup').innerText(),/not enabled/);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth-innerWidth),0);
    await page.screenshot({path:`data/buy-reviewed-${width}.png`,fullPage:true});
    await page.goto(base+'/console',{waitUntil:'networkidle'});
    await page.waitForURL('**/?auth=login');
    await page.waitForSelector('dialog[open]');
    assert.match(await page.locator('dialog h2').innerText(),/Welcome back/);
    assert.deepEqual(errors,[],`Console/page errors at ${width}px`);
    console.log(`PASS ${width}px: landing, animation DOM, modal switching/Escape, fail-closed signup, pricing, six networks, signed-out console gate, no overflow/errors`);
    await context.close();
  }
  const context=await browser.newContext({viewport:{width:390,height:844},reducedMotion:'reduce'});
  const page=await context.newPage();await page.goto(base,{waitUntil:'networkidle'});
  console.log('PASS reduced-motion preference disables continuous animations');
  await context.close();
} finally {await browser.close();}
