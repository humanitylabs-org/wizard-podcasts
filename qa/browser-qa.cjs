// Standalone browser QA for Podcasts: screenshots phone + desktop, dark + light; real playback, seeking (Range) and
// server-side resume. Usage: node qa/browser-qa.cjs <base-url> <out-dir> [playwright-core path]
const path = require('path');
const fs = require('fs');
const base = process.argv[2], out = process.argv[3];
const { chromium } = require(process.argv[4] || 'playwright-core');
fs.mkdirSync(out, { recursive: true });
const log = (...a) => console.log(...a);

(async () => {
  const browser = await chromium.launch({ executablePath: '/usr/bin/chromium', args: ['--autoplay-policy=no-user-gesture-required', '--no-sandbox'] });
  const results = {};
  for (const [label, vp] of [['phone', { width: 390, height: 844 }], ['desktop', { width: 1280, height: 800 }]]) {
    for (const scheme of ['dark', 'light']) {
      const ctx = await browser.newContext({ viewport: vp, colorScheme: scheme, deviceScaleFactor: label === 'phone' ? 2 : 1, hasTouch: label === 'phone' });
      const page = await ctx.newPage();
      const ranges = [];
      page.on('response', r => { if (r.url().includes('/audio')) ranges.push(r.status()); });
      const errors = [];
      page.on('pageerror', e => errors.push(String(e)));
      await page.goto(base + '/', { waitUntil: 'networkidle' });
      await page.waitForSelector('#list-new li, #list-continue li, #inbox-empty:not([hidden])');
      const shot = n => page.screenshot({ path: path.join(out, `${label}-${scheme}-${n}.png`) });
      await shot('1-upnext');
      if (label === 'phone' && scheme === 'dark') {
        // play from the row, then open the full player
        await page.click('#list-new li .pc-icon-accent, #list-continue li .pc-icon-accent');
        await page.waitForFunction(() => { const a = document.getElementById('audio'); return a && !a.paused && a.currentTime > 0.5; }, null, { timeout: 30000 });
        await page.click('#mini-open');
        await page.click('.pc-speed:nth-child(6)'); // 2x
        await page.waitForTimeout(1500);
        // seek to 30:00 using the slider's change handler, as a tap would
        await page.evaluate(() => { const s = document.getElementById('pl-seek'); s.value = '1800'; s.dispatchEvent(new Event('change')); });
        await page.waitForFunction(() => { const a = document.getElementById('audio'); return a.currentTime >= 1800 && a.currentTime < 1830 && !a.seeking; }, null, { timeout: 30000 });
        await page.waitForTimeout(2500);
        const st = await page.evaluate(() => ({ t: document.getElementById('audio').currentTime, rate: document.getElementById('audio').playbackRate, paused: document.getElementById('audio').paused }));
        await shot('2-player');
        await page.click('#pl-toggle'); // pause -> saves position
        await page.waitForTimeout(1500);
        const id = await page.evaluate(() => document.querySelector('.pc-ep[data-id]')?.dataset.id || null);
        const saved = await page.evaluate(async () => (await (await fetch('api/inbox')).json()).now_playing);
        results.playback = { ...st, savedPosition: saved && saved.position, savedId: saved && saved.id, audioStatuses: [...new Set(ranges)] };
        // in-app Back closes the player view and stays in the app
        const backHandled = await page.evaluate(() => { const b = document.querySelector('.pc-app').dataset.view; history.length; return b; });
        results.viewBeforeBack = backHandled;
      }
      // Shows, a show, an episode, Add + search
      await page.click('.pc-tab[data-tab="shows"]');
      await page.waitForSelector('.pc-show');
      await page.waitForTimeout(400);
      await shot('3-shows');
      await page.click('.pc-show');
      await page.waitForSelector('#show-episodes li');
      await page.waitForTimeout(400);
      await shot('4-show');
      await page.click('#show-episodes li .pc-ep-open');
      await page.waitForSelector('#ep-notes p');
      await page.waitForTimeout(300);
      await shot('5-episode');
      const notesSafe = await page.evaluate(() => !document.querySelector('#ep-notes script, #ep-notes img, #ep-notes iframe, #ep-notes [style], #ep-notes [onclick]'));
      await page.click('.pc-tab[data-tab="add"]');
      await page.fill('#search-input', 'Lex Fridman');
      await page.press('#search-input', 'Enter');
      await page.waitForSelector('#search-results li', { timeout: 20000 });
      await page.waitForTimeout(1500);
      await shot('6-add');
      await page.click('.pc-tab[data-tab="shows"]');
      await page.click('#open-settings');
      await page.waitForSelector('#settings-sheet[open]');
      await page.waitForTimeout(400);
      await shot('7-settings');
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
      const small = await page.evaluate(() => [...document.querySelectorAll('.pc-dock button, .pc-ep button, .pc-tab')].filter(b => b.offsetParent && (b.getBoundingClientRect().height < 44 || b.getBoundingClientRect().width < 44)).map(b => b.id || b.className));
      results[`${label}-${scheme}`] = { errors, notesSafe, overflow, smallTargets: small };
      await ctx.close();
    }
  }
  await browser.close();
  fs.writeFileSync(path.join(out, 'results.json'), JSON.stringify(results, null, 2));
  log(JSON.stringify(results, null, 2));
})().catch(e => { console.error(e); process.exit(1); });
