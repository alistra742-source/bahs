// The console's two result sections, and the rule that keeps them apart: a
// request that never reached a verdict is never counted as a check of a name.
//
// The reported failure was a run whose every row was a transport timeout, shown
// as "128 checks" with an empty verdicts list and a single number for the
// errors. One list and one number is what let a sweep that resolved nothing
// read like a sweep that resolved everything and found no name free. So this
// test drives the console's own event handler and the component that renders
// the failures, and asserts the two lists never share a row.
//
// Run with: node tests/ui_errors.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const page = readFileSync(join(root, 'static/console.html'), 'utf8');

// The static page contract: two result cards, each with its own list, and the
// error list rendered by its own component.
assert.match(page, /<h3>Verdicts<\/h3>/, 'the verdicts section must exist');
assert.match(page, /<h3>Errors<\/h3>/, 'the errors section must exist');
assert.ok(page.includes('id="feed"'), 'the verdicts list needs its own container');
assert.ok(page.includes('id="errFeed"'), 'the error list needs its own container');
assert.ok(page.indexOf('id="feed"') < page.indexOf('id="errFeed"'),
  'verdicts and errors are two sections, verdicts first');
assert.match(page, /<script src="\/ui\/components\/error-feed\.js"><\/script>/,
  'the error feed must be loaded, or the section renders nothing');
assert.ok(page.includes('not</b> a check of the name'),
  'the section has to say a failure is not a check, in the page itself');

// The live client, loaded the way the page loads it. `document` is stubbed to
// nothing: the point under test is which list an event lands in, not the DOM.
global.window = global;
global.document = { getElementById: () => null, querySelector: () => null };
global.localStorage = { getItem: () => null, setItem: () => {} };
for (const file of ['static/ui/components/core.js', 'static/ui/components/error-feed.js',
                    'static/app.js']) {
  new Function(readFileSync(join(root, file), 'utf8'))();
}
const { App, UI } = global;

// One stream, every kind of result the run can emit.
App.feedLine({ status: 'error', username: 'brew', platform: 'guns.lol',
               detail: 'ConnectTimeout', latency_ms: 2006 });
App.feedLine({ status: 'available', username: 'zephyr', platform: 'x', confirmed: true });
App.feedLine({ status: 'taken', username: 'admin', platform: 'x' });
App.feedLine({ status: 'unanswered', username: 'ghost', platform: 'x', detail: 'clearance' });
App.feedLine({ status: 'blocked', username: 'spam', platform: 'github' });

assert.equal(App.S.errors.length, 1, 'only a transport error is an error, not a refusal');
const timeout = App.S.errors[0];
assert.deepEqual(timeout, { t: timeout.t, username: 'brew', platform: 'guns.lol',
  e: 'ConnectTimeout', ms: 2006 }, 'the row carries the failure class and the latency');
assert.equal(App.S.feed.length, 2, 'verdicts only: available and taken');
assert.deepEqual(App.S.feed.map((r) => r.s), ['taken', 'available'], 'newest first');
assert.equal(App.S.feed.some((r) => r.u === 'brew'), false,
  'an errored check must never appear among the verdicts');
assert.equal(App.S.errors.some((r) => r.username === 'zephyr'), false,
  'a verdict must never appear among the errors');

// Below the fold: the cap keeps a million-name sweep from growing a list in the
// tab, and the two caps are independent.
for (let i = 0; i < 450; i += 1) {
  App.feedLine({ status: 'error', username: 'n' + i, platform: 'x', detail: 'ReadTimeout' });
}
assert.ok(App.S.errors.length <= 400, `errors capped, got ${App.S.errors.length}`);
assert.equal(App.S.feed.length, 2, 'error traffic does not evict a verdict');

// The component itself: the failure class is shown, and the row is never
// coloured like a verdict.
const html = UI.errorFeed([timeout]);
assert.match(html, /class="error"/, html);
assert.match(html, /ConnectTimeout/, html);
assert.match(html, /guns\.lol/, html);
assert.equal(/class="available"|class="taken"/.test(html), false,
  'an error is never rendered as a verdict');
assert.deepEqual(UI.errorRow({ username: '<img src=x>', platform: 'x', e: '<b>' }),
  '<div class="error"><span class="t"></span><span class="u">&lt;img src=x&gt;</span>'
  + '<span class="e">&lt;b&gt;</span><span class="p">x</span></div>', 'every field is escaped');
assert.match(UI.errorFeed([]), /nothing yet/, 'an empty list says so rather than rendering blank');
console.log('PASS console: verdicts and errors are two sections, and a failure is never a check');
