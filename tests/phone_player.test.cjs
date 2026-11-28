// Issue 30: web/phone-player.js routes an episode through the phone's own media player (WizardApp.device.backgroundAudio)
// when the APK admits wizard-podcasts (always on since the switch was removed); otherwise it is the plain <audio> element.
// Run: node --test tests/phone_player.test.cjs (also run by tests/test_podcasts.py).
const test = require('node:test');
const assert = require('node:assert/strict');
const { create, APP } = require('../web/phone-player.js');

function fakeEl() {
  const on = {};
  return {
    paused: true, currentTime: 0, duration: NaN, readyState: 0, playbackRate: 1, error: null, _src: '', played: 0, loads: 0,
    get src() { return this._src; }, set src(v) { this._src = v; },
    getAttribute(n) { return n === 'src' ? this._src || null : null; },
    removeAttribute(n) { if (n === 'src') this._src = ''; },
    load() { this.loads++; },
    play() { this.played++; this.paused = false; return Promise.resolve(); },
    pause() { this.paused = true; },
    addEventListener(t, fn) { (on[t] = on[t] || []).push(fn); },
    removeEventListener(t, fn) { on[t] = (on[t] || []).filter(f => f !== fn); },
    emit(t) { for (const fn of on[t] || []) fn({ type: t }); },
  };
}
const TRACK = { id: 'ep:7', title: 'T', artist: 'S', album: 'S', stream: '/wizard-podcasts/api/episodes/7/audio', progress: '/wizard-podcasts/api/episodes/7/progress' };
function rig({ apps = [APP], failLoad = false, initial = null, rate = false } = {}) {
  const calls = [], el = fakeEl();
  let clock = 1000, snap = initial, tick = null;
  const request = (method, params) => {
    calls.push([method, params]);
    if (method === 'capabilities') return apps ? Promise.resolve({ v: 1, backgroundAudio: true, ...(apps.length ? { apps } : {}), ...(rate ? { methods: ['load', 'play', 'rate'] } : {}) }) : Promise.reject(new Error('Background audio unavailable'));
    if (method === 'attach') return Promise.resolve({ sessionId: 'S1', appId: APP, index: -1, queue: [] });
    if (method === 'load') {
      if (failLoad) return Promise.reject(new Error('Untrusted track'));
      snap = { sessionId: 'S1', appId: APP, index: 0, playing: true, wantPlaying: true, positionMs: params.positionMs, durationMs: 0, queue: params.queue };
      return Promise.resolve(snap);
    }
    if (method === 'state') return Promise.resolve(snap || { sessionId: 'S1', appId: APP, index: -1, playing: false, wantPlaying: false, positionMs: 0, durationMs: 0, queue: [] });
    if (method === 'pause') { snap = { ...snap, playing: false, wantPlaying: false }; return Promise.resolve(snap); }
    if (method === 'play') { snap = { ...snap, playing: true, wantPlaying: true }; return Promise.resolve(snap); }
    if (method === 'seek') { snap = { ...snap, positionMs: params.positionMs }; return Promise.resolve(snap); }
    if (method === 'stop') { snap = null; return Promise.resolve({}); }
    if (method === 'rate') { snap = { ...snap, rate: params.value }; return Promise.resolve(snap); }
    return Promise.reject(new Error('unknown ' + method));
  };
  const a = create(el, {
    request, track: src => src === 'api/episodes/7/audio' ? TRACK : null,
    startAt: () => 754.3, skip: () => ({ back: 15, forward: 30 }), now: () => clock,
    every: fn => { tick = fn; return 1; },
  });
  const events = [];
  for (const t of ['play', 'pause', 'timeupdate', 'ended', 'loadedmetadata', 'seeked']) a.addEventListener(t, () => events.push(t));
  return { a, el, calls, events, set: s => { snap = { ...snap, ...s }; }, advance: ms => { clock += ms; }, poll: () => tick && tick(), takeOver: () => { snap = { sessionId: 'S1', appId: APP, index: -1, playing: false, wantPlaying: false, positionMs: 0, durationMs: 0, queue: [] }; } };
}
const flush = () => new Promise(r => setTimeout(r, 5));
const names = calls => calls.map(c => c[0]);

test('browser (no phone player): the plain element plays, nothing native is asked', async () => {
  const r = rig({ apps: null });
  assert.equal(await r.a.probe(), false);
  r.a.src = 'api/episodes/7/audio';
  await r.a.play();
  assert.equal(r.el.src, 'api/episodes/7/audio');
  assert.equal(r.el.played, 1);
  assert.deepEqual(names(r.calls), ['capabilities']);
  assert.equal(r.a.native, false);
});

test('old APK (capabilities without wizard-podcasts): the element', async () => {
  for (const opts of [{ apps: [] }, { apps: ['wizard-music'] }]) {
    const r = rig(opts);
    await r.a.probe();
    r.a.src = 'api/episodes/7/audio';
    await r.a.play();
    assert.equal(r.el.played, 1, JSON.stringify(opts));
    assert.ok(!names(r.calls).includes('load'));
  }
});

test('a preview (no native track) stays in the page', async () => {
  const r = rig();
  await r.a.probe();
  r.a.src = 'api/discover/audio?feed_url=x';
  await r.a.play();
  assert.equal(r.el.played, 1);
  assert.ok(!names(r.calls).includes('attach'));
});

test('phone player on: attach, then load with the saved place, -15/+30 and the root-relative track', async () => {
  const r = rig();
  await r.a.probe();
  r.a.src = 'api/episodes/7/audio';
  assert.equal(r.a.native, true);
  assert.equal(r.el.src, '', 'the element never gets the source');
  await r.a.play();
  await flush();
  const load = r.calls.find(c => c[0] === 'load')[1];
  assert.deepEqual(load, { sessionId: 'S1', index: 0, positionMs: 754300, skip: { back: 15, forward: 30 }, queue: [TRACK] });
  assert.ok(names(r.calls).indexOf('attach') < names(r.calls).indexOf('load'));
  assert.equal(r.el.played, 0);
  assert.equal(r.a.paused, false);
  assert.equal(r.events.filter(e => e === 'play').length, 1);
  assert.equal(r.a.readyState, 4);
});

test('position, duration and lock-screen pause come from polled state; the end fires ended once', async () => {
  const r = rig();
  await r.a.probe();
  r.a.src = 'api/episodes/7/audio';
  await r.a.play(); await flush();
  r.set({ positionMs: 800000, durationMs: 3600000 }); r.poll(); await flush();
  assert.equal(r.a.duration, 3600);
  assert.ok(r.events.includes('loadedmetadata'));
  r.advance(2000);
  assert.equal(r.a.currentTime, 802, 'playing: the time runs on between polls');
  r.set({ playing: false, wantPlaying: false, positionMs: 810000 }); r.poll(); await flush();
  assert.equal(r.a.paused, true);
  assert.equal(r.events.filter(e => e === 'pause').length, 1);
  r.advance(5000);
  assert.equal(r.a.currentTime, 810, 'paused: the time stands still');
  r.set({ positionMs: 3600000 }); r.poll(); await flush(); r.poll(); await flush();
  assert.equal(r.events.filter(e => e === 'ended').length, 1);
});

test('pause, play and seek go to the phone with the session id', async () => {
  const r = rig();
  await r.a.probe();
  r.a.src = 'api/episodes/7/audio';
  await r.a.play(); await flush();
  r.a.pause(); await flush();
  assert.equal(r.a.paused, true);
  r.a.currentTime = 100; await flush();
  assert.equal(r.a.currentTime, 100);
  await r.a.play(); await flush();
  const sent = r.calls.filter(c => ['pause', 'seek', 'play'].includes(c[0]));
  assert.deepEqual(sent, [['pause', { sessionId: 'S1' }], ['seek', { sessionId: 'S1', positionMs: 100000 }], ['play', { sessionId: 'S1' }]]);
  assert.ok(r.events.includes('seeked'));
});

test('closing the mini player stops the phone player', async () => {
  const r = rig();
  await r.a.probe();
  r.a.src = 'api/episodes/7/audio';
  await r.a.play(); await flush();
  r.a.pause(); r.a.removeAttribute('src'); r.a.load(); await flush();
  assert.deepEqual(r.calls.find(c => c[0] === 'stop'), ['stop', { sessionId: 'S1' }]);
  assert.equal(r.a.native, false);
});

test('a refused load falls back to the element', async () => {
  const r = rig({ failLoad: true });
  await r.a.probe();
  r.a.src = 'api/episodes/7/audio';
  await r.a.play(); await flush();
  assert.equal(r.a.native, false);
  assert.equal(r.el.src, 'api/episodes/7/audio');
  assert.equal(r.el.played, 1);
  r.el.emit('play');
  assert.ok(r.events.includes('play'), 'element events reach the app again');
});

test('after a reload the episode still playing on the phone is picked up, not restarted', async () => {
  const r = rig({ initial: { sessionId: 'S9', appId: APP, index: 0, playing: true, wantPlaying: true, positionMs: 42000, durationMs: 60000, queue: [{ ...TRACK, stream: 'https://box/wizard-podcasts/api/episodes/7/audio' }] } });
  await r.a.probe();
  r.a.src = 'api/episodes/7/audio';
  await flush();
  assert.equal(r.a.paused, false);
  assert.equal(r.a.currentTime, 42);
  assert.ok(!names(r.calls).includes('load'));
  r.a.pause(); await flush();
  assert.deepEqual(r.calls.find(c => c[0] === 'pause'), ['pause', { sessionId: 'S9' }]);
});

test('another app (Music) takes the player: Podcasts shows paused and loads again on play', async () => {
  const r = rig();
  await r.a.probe();
  r.a.src = 'api/episodes/7/audio';
  await r.a.play(); await flush();
  r.set({ positionMs: 30000 }); r.poll(); await flush();
  r.takeOver(); r.poll(); await flush();
  assert.equal(r.a.paused, true);
  assert.ok(r.events.includes('pause'));
  await r.a.play(); await flush();
  assert.equal(r.calls.filter(c => c[0] === 'load').length, 2);
});

// Issue 35: speed buttons did nothing on the phone (0.5.14): the phone player had no speed command.
test('a speed picked while playing on a phone player without rate carries on in the element at that speed and place', async () => {
  const r = rig();
  await r.a.probe();
  r.a.src = 'api/episodes/7/audio';
  await r.a.play(); await flush();
  r.set({ positionMs: 800000, durationMs: 3600000 }); r.poll(); await flush();
  r.a.playbackRate = 2; await flush();
  assert.equal(r.a.native, false, 'off the phone player');
  assert.deepEqual(r.calls.find(c => c[0] === 'stop'), ['stop', { sessionId: 'S1' }], 'the phone copy stops: never twice');
  assert.equal(r.el.src, 'api/episodes/7/audio');
  assert.equal(r.el.playbackRate, 2);
  assert.equal(r.a.playbackRate, 2);
  assert.equal(r.el.played, 1, 'keeps playing');
  r.el.emit('loadedmetadata');
  assert.equal(r.el.currentTime, 800, 'at the same second');
});

test('a paused episode switched to another speed stays paused in the element', async () => {
  const r = rig();
  await r.a.probe();
  r.a.src = 'api/episodes/7/audio';
  await r.a.play(); await flush();
  r.a.pause(); await flush();
  r.a.playbackRate = 1.5; await flush();
  assert.equal(r.a.native, false);
  assert.equal(r.el.played, 0);
  assert.equal(r.el.playbackRate, 1.5);
});

test('the next episode at a speed other than 1x plays in the element when the phone player has no rate', async () => {
  const r = rig();
  await r.a.probe();
  r.a.playbackRate = 2;
  r.a.src = 'api/episodes/7/audio';
  await r.a.play(); await flush();
  assert.equal(r.a.native, false);
  assert.equal(r.el.playbackRate, 2);
  assert.equal(r.el.played, 1);
  assert.ok(!names(r.calls).includes('load'));
});

test('a startup at 2x (src first, then the speed) never leaves the episode on the phone player', async () => {
  const r = rig();
  await r.a.probe();
  r.a.src = 'api/episodes/7/audio';
  r.a.playbackRate = 2;
  await flush();
  assert.equal(r.a.native, false);
  assert.equal(r.el.src, 'api/episodes/7/audio');
  await r.a.play();
  assert.equal(r.el.played, 1);
  assert.equal(r.el.playbackRate, 2);
});

test('repeating the same speed (the app sets it on every play) changes nothing', async () => {
  const r = rig();
  await r.a.probe();
  r.a.src = 'api/episodes/7/audio';
  await r.a.play(); await flush();
  r.a.playbackRate = 1; await flush();
  assert.equal(r.a.native, true);
  assert.ok(!names(r.calls).includes('stop'));
});

test('a phone player that advertises rate keeps the episode and gets the speed on load and on change', async () => {
  const r = rig({ rate: true });
  await r.a.probe();
  r.a.playbackRate = 1.5;
  r.a.src = 'api/episodes/7/audio';
  assert.equal(r.a.native, true);
  await r.a.play(); await flush();
  assert.equal(r.calls.find(c => c[0] === 'load')[1].rate, 1.5);
  r.a.playbackRate = 2; await flush();
  assert.equal(r.a.native, true);
  assert.deepEqual(r.calls.find(c => c[0] === 'rate'), ['rate', { sessionId: 'S1', value: 2 }]);
  assert.equal(r.a.playbackRate, 2);
});

test('in a browser the speed goes straight to the element', async () => {
  const r = rig({ apps: null });
  await r.a.probe();
  r.a.src = 'api/episodes/7/audio';
  r.a.playbackRate = 1.75;
  assert.equal(r.el.playbackRate, 1.75);
});
