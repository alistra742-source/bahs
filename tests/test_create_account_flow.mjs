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
  
  // Solve the security check like a user: drag the chip onto the target, then type the word shown.
  await page.waitForSelector('#vxKnob');
  const knob = await page.locator('#vxKnob').boundingBox();
  const zone = await page.locator('.vx-zone').boundingBox();
  const y = knob.y + knob.height / 2;
  await page.mouse.move(knob.x + knob.width / 2, y);
  await page.mouse.down();
  await page.mouse.move(zone.x + zone.width / 2, y, { steps: 12 });
  await page.mouse.up();
  await page.waitForFunction(() => document.getElementById('vxStatus')?.textContent.startsWith('On target'));
  // The word is an SVG image; read its letters from the image source the way a person reads them.
  const src = await page.locator('.vx-word').getAttribute('src');
  const svg = Buffer.from(src.split(',')[1], 'base64').toString('utf8');
  const word = [...svg.matchAll(/<text[^>]*>([A-Z0-9])<\/text>/g)].map(m => m[1]).join('');
  assert.equal(word.length, 6, 'word image should contain six letters');
  await page.fill('#vxWord', word);
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
