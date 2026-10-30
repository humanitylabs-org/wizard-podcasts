const assert = require('assert');
const fs = require('fs');
const http = require('http');
const path = require('path');

const repo = path.resolve(__dirname, '..');
const web = path.join(repo, 'web');
const out = process.argv[2] || path.join(repo, '.qa-navigation-playback');
const playwrightPath = process.argv[3] || '/root/hermes-workspace/hacala/chatgpt-homepage-clarity-plugin/node_modules/playwright-core';
const { chromium } = require(playwrightPath);
fs.mkdirSync(out, { recursive: true });

const show = {
  id: 1,
  title: 'The Tim Ferriss Show',
  short_title: 'Tim Ferriss',
  author: 'Tim Ferriss',
  episodes: 1,
  artwork: '',
  feed_url: 'https://example.com/feed.xml',
  auto_download: true,
  pending: false,
  last_error: '',
};
const episode = {
  id: 101,
  show_id: 1,
  show_title: show.title,
  title: 'A real episode to play',
  published: Math.floor(Date.now() / 1000) - 120,
  duration: 2,
  position: 0,
  position_at: 0,
  played: false,
  in_inbox: true,
  artwork: '',
  audio: 'api/episodes/101/audio',
  download: { state: 'none', progress_bytes: 0, size_bytes: 0, error: '' },
  notes_html: '<p>Episode notes.</p>',
};
const settings = {
  speed: 1,
  skip_back: 15,
  skip_forward: 30,
  storage_cap_mb: 2048,
  refresh_minutes: 60,
  auto_download: true,
};

function wav(seconds = 2, rate = 8000) {
  const samples = seconds * rate;
  const dataSize = samples * 2;
  const b = Buffer.alloc(44 + dataSize);
  b.write('RIFF', 0);
  b.writeUInt32LE(36 + dataSize, 4);
  b.write('WAVEfmt ', 8);
  b.writeUInt32LE(16, 16);
  b.writeUInt16LE(1, 20);
  b.writeUInt16LE(1, 22);
  b.writeUInt32LE(rate, 24);
  b.writeUInt32LE(rate * 2, 28);
  b.writeUInt16LE(2, 32);
  b.writeUInt16LE(16, 34);
  b.write('data', 36);
  b.writeUInt32LE(dataSize, 40);
  for (let i = 0; i < samples; i++) b.writeInt16LE(Math.round(Math.sin(i / rate * Math.PI * 440) * 1200), 44 + i * 2);
  return b;
}
const audio = wav();

function json(res, body, status = 200) {
  const data = Buffer.from(JSON.stringify(body));
  res.writeHead(status, { 'Content-Type': 'application/json', 'Content-Length': data.length, 'Cache-Control': 'no-store' });
  res.end(data);
}

function serveAudio(req, res) {
  const range = req.headers.range;
  if (!range) {
    res.writeHead(200, { 'Content-Type': 'audio/wav', 'Content-Length': audio.length, 'Accept-Ranges': 'bytes' });
    res.end(audio);
    return;
  }
  const m = /bytes=(\d+)-(\d*)/.exec(range);
  const start = m ? Number(m[1]) : 0;
  const end = m && m[2] ? Math.min(Number(m[2]), audio.length - 1) : audio.length - 1;
  const part = audio.subarray(start, end + 1);
  res.writeHead(206, {
    'Content-Type': 'audio/wav',
    'Content-Length': part.length,
    'Content-Range': `bytes ${start}-${end}/${audio.length}`,
    'Accept-Ranges': 'bytes',
  });
  res.end(part);
}

const server = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://127.0.0.1');
  if (u.pathname === '/wizard-os-home/core/app-client.js') {
    const js = `window.WizardApp={version:1,hosted:true,ready(){},back:{register(){return()=>{}}},nav:{back(){return false}},update:{hold(){return{release(){}}}},audio:{duck(){}}};`;
    res.writeHead(200, { 'Content-Type': 'application/javascript', 'Content-Length': Buffer.byteLength(js) });
    res.end(js);
    return;
  }
  if (u.pathname === '/api/settings') return json(res, settings);
  if (u.pathname === '/api/feed') return json(res, { shows: [show], episodes: [episode], total: 1, now_playing: null });
  if (u.pathname === '/api/search') return json(res, {
    results: [{ title: show.title, author: show.author, artwork: '', feed_url: show.feed_url, show_id: 1, subscribed: true, source: 'rss' }],
    youtube: [],
  });
  if (u.pathname === '/api/shows/1') return json(res, { show, episodes: [episode] });
  if (u.pathname === '/api/episodes/101/audio') return serveAudio(req, res);
  if (u.pathname === '/api/episodes/101') return json(res, episode);
  if (u.pathname === '/api/episodes/101/progress') return json(res, { ...episode, position_at: 1 });
  const relative = u.pathname === '/' ? 'index.html' : u.pathname.replace(/^\//, '');
  const file = path.resolve(web, relative);
  if (!file.startsWith(web + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
    res.writeHead(404); res.end('not found'); return;
  }
  const ext = path.extname(file);
  const type = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' }[ext] || 'application/octet-stream';
  const data = fs.readFileSync(file);
  res.writeHead(200, { 'Content-Type': type, 'Content-Length': data.length, 'Cache-Control': 'no-store' });
  res.end(data);
});

(async () => {
  const remote = Boolean(process.env.CDP_ENDPOINT);
  const externalBase = process.env.TEST_BASE_URL;
  if (!externalBase) await new Promise(resolve => server.listen(0, remote ? '0.0.0.0' : '127.0.0.1', resolve));
  const baseHost = remote ? (process.env.TEST_BASE_HOST || '10.128.16.1') : '127.0.0.1';
  const base = externalBase ? externalBase.replace(/\/$/, '') : `http://${baseHost}:${server.address().port}`;
  const browser = remote
    ? await chromium.connectOverCDP(process.env.CDP_ENDPOINT)
    : await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || '/snap/bin/chromium', args: ['--no-sandbox', '--autoplay-policy=no-user-gesture-required'] });
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true, colorScheme: 'dark' });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(String(error)));

  if (externalBase) {
    await page.route('**/wizard-os-home/core/app-client.js', route => route.fulfill({
      contentType: 'application/javascript',
      body: `window.WizardApp={version:1,hosted:true,ready(){},back:{register(){return()=>{}}},nav:{back(){return false}},update:{hold(){return{release(){}}}},audio:{duck(){}}};`,
    }));
    await page.route('**/api/search*', route => route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        results: [
          { title: show.title, author: show.author, artwork: '', feed_url: show.feed_url, show_id: 1, subscribed: true, source: 'rss' },
          { title: 'Another useful show', author: 'Another host', artwork: '', feed_url: 'https://example.com/another.xml', show_id: null, subscribed: false, source: 'rss' },
        ],
        youtube: [],
      }),
    }));
    await page.route('**/api/subscribe', route => route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ already_subscribed: false, show }),
    }));
  }

  await page.goto(base + '/', { waitUntil: 'networkidle' });
  await page.waitForSelector('#feed-groups .pc-ep');
  await page.click('#open-add');
  await page.waitForSelector('#view-add:not([hidden])');
  assert.strictEqual(await page.locator('#nav-back').isVisible(), true, 'Find shows needs a visible Back control');

  await page.fill('#search-input', 'Tim Ferriss');
  await page.press('#search-input', 'Enter');
  await page.waitForSelector('#search-results .pc-result');
  assert.deepStrictEqual(await page.locator('#search-results .pc-follow').allTextContents(), ['Episodes', 'Follow'], 'Search actions must distinguish followed and new shows');
  await page.locator('#search-results .pc-follow').nth(1).click();
  await page.waitForSelector('#view-show:not([hidden]) #show-episodes .pc-ep');
  await page.click('#nav-back');
  await page.waitForSelector('#view-add:not([hidden])');
  await page.locator('#search-results .pc-follow').first().click();
  await page.waitForSelector('#view-show:not([hidden]) #show-episodes .pc-ep');

  await page.click('#show-episodes .pc-ep-open');
  await page.waitForSelector('#view-episode:not([hidden]) #ep-play');
  assert.match((await page.locator('#ep-play').textContent()).trim(), /Play/, 'Episode needs a visible Play action');
  await page.click('#ep-play');
  await page.waitForFunction(() => {
    const player = document.getElementById('audio');
    return player && !player.paused && player.currentTime > 0.05;
  });

  await page.screenshot({ path: path.join(out, 'episode-playing-phone-dark.png'), fullPage: true });
  await page.click('#nav-back');
  await page.waitForSelector('#view-show:not([hidden])');
  await page.click('#nav-back');
  await page.waitForSelector('#view-add:not([hidden])');
  await page.click('#nav-back');
  await page.waitForSelector('#view-feed:not([hidden])');
  assert.strictEqual(await page.locator('#top').isVisible(), true, 'Back from Find shows must return to the main feed');
  assert.deepStrictEqual(errors, [], 'page errors');

  await context.close();
  if (!remote) await browser.close();
  if (!externalBase) server.close();
  fs.writeFileSync(path.join(out, 'results.json'), JSON.stringify({ passed: true, errors }, null, 2));
  console.log('NAVIGATION_PLAYBACK_QA_OK');
  process.exit(0);
})().catch(async error => {
  console.error(error.stack || error);
  if (server.listening) server.close();
  process.exit(1);
});
