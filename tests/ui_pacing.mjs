// What the console says about a pool that has been paced, rendered through the
// page's own components rather than re-described here.
//
// The pacing is what stops a saturated endpoint from being retired, and it is
// invisible unless the dashboard says so: a run that is quietly asking for fewer
// tunnels looks exactly like a run that has been *cut* -- which is what the
// reported failure read as, with `1 retired` where the truth was "asked for 128
// at once". So the numbers have to reach the two places the console already
// reads the pool from, and they must never carry an endpoint.
//
// Run with: node tests/ui_pacing.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
global.window = global;
for (const file of ['static/ui/components/core.js', 'static/ui/components/kpi.js',
                    'static/ui/components/health-panel.js']) {
  new Function(readFileSync(join(root, file), 'utf8'))();
}
const UI = global.UI;

// The tail of a run through one endpoint that accepts four tunnels at once: the
// width paced down from 128, and 516 attempts where 20 never opened.
const pool = {
  pool_size: 1, pool_alive: 1, pool_resting: 0, pool_retired: 0,
  proxy_attempts: 516, proxy_failures: 20, proxy_timeouts: 20,
  proxies_paced: 1, proxy_capacity: 4, per_proxy_connections: 128,
  proxy_pace_adjustments: [{ was: 128, now: 64, seconds_ago: 31.2 }],
};
const healthy = { ...pool, proxies_paced: 0, proxy_timeouts: 0, proxy_capacity: 128 };

const sub = UI.runKpis(pool, pool).at(-1).sub;
assert.match(sub, /1 paced/, sub);
assert.match(UI.runKpis(healthy, healthy).at(-1).sub, /0 resting · 0 retired$/,
  'an endpoint nobody paced gets no line about it');

const rows = UI.poolRows(pool);
const paced = rows.find(([k]) => k === 'endpoints paced');
assert.ok(paced, rows);
assert.equal(paced[1], '1 of 1 · 4 tunnel(s) at once of 128', paced[1]);
assert.deepEqual(rows.find(([k]) => k === 'tunnels timed out'), ['tunnels timed out', '20']);

// An endpoint that needed a longer connect clock than the run configured. Shown
// because it changes what the rest of the run means, and absent when nobody
// needed one -- a healthy pool must not carry a line about a ceiling it kept.
const raised = UI.poolRows({ ...pool, proxies_ceiling_raised: 1 })
assert.deepEqual(raised.find(([k]) => k === 'endpoints given longer to connect'),
  ['endpoints given longer to connect', '1 of 1'], raised);
assert.equal(rows.some(([k]) => k === 'endpoints given longer to connect'), false,
  'a pool that never raised a ceiling says nothing about raising one');
assert.equal(UI.poolRows(healthy).some(([k]) => k === 'endpoints paced'), false,
  'a healthy pool says nothing about pacing');

const html = UI.healthPanel(UI.healthRows({}, pool));
assert.match(html, /endpoints paced/);
assert.match(html, /tunnels timed out/);
assert.equal(html.includes('http://'), false, 'no endpoint, no credentials');
console.log('PASS console: pacing reaches the proxy KPI and the health panel, and names no endpoint');
