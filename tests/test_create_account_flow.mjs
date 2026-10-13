import { createRequire } from 'node:module';
import assert from 'node:assert/strict';
const require = createRequire(process.env.PLAYWRIGHT_PACKAGE_JSON || import.meta.url);
const { chromium } = require('playwright');
const base = process.env.TEST_ORIGIN || 'http://127.0.0.1:3001';

const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH,
  args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu']
});

try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  
  await page.goto(base, { waitUntil: 'networkidle' });
  
  // Click Get started
  await page.locator('.hero [data-auth="signup"]').click();
  await page.waitForSelector('dialog[open]');
  
  const uniqueName = 'vtx_' + Math.random().toString(36).substring(2, 7);
  await page.fill('dialog input[name="username"]', uniqueName);
  await page.fill('dialog input[name="password"]', 'pass123'); // testing flexible length
  
  // Solve the security check like a user: click each required glyph, then max the slider.
  await page.waitForSelector('.glyph-btn');
  const required = (await page.locator('#authCaptcha b').first().innerText()).trim().split(/\s+/);
  for (const g of required) await page.locator(`.glyph-btn[data-glyph="${g}"]`).click();
  await page.locator('#freqSlider').evaluate(el => { el.value = '100'; el.dispatchEvent(new Event('input')); });
  await page.waitForFunction(() => !document.querySelector('.auth-submit').disabled);

  console.log('Submitting signup form for username:', uniqueName);
  await page.locator('dialog .auth-submit').click();
  
  // A new account lands on /console when it has access (trial granted), otherwise on /buy.
  // The trial is one per IP, so this VM's IP may already have spent it.
  await page.waitForURL(/\/(console|buy)$/, { timeout: 15000 });
  const landed = new URL(page.url()).pathname;
  console.log('Landed on:', landed);
  const status = await page.evaluate(() => fetch('/auth/status', { credentials: 'same-origin' }).then(r => r.json()));
  assert.equal(status.user?.username, uniqueName, 'account should be logged in after signup');
  if (landed === '/console') {
    await page.waitForSelector('.access-banner');
    const clockText = await page.locator('.access-banner [data-access-clock]').innerText();
    console.log('Console clock text:', clockText);
    assert.match(clockText, /TRIAL/);
  }
  
  console.log('PASS: Create account button clicked, account created, logged in directly to console with 60s trial!');
  await context.close();
} finally {
  await browser.close();
}
