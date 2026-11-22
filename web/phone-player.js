'use strict';
/* Podcasts on the phone's own media player (issue 30, always on).
 *
 * create(el, opts) returns a stand-in for the page's <audio> element with the same small surface app.js uses (paused,
 * currentTime, duration, readyState, playbackRate, error, src, play(), pause(), load(), get/removeAttribute('src'),
 * addEventListener). While an episode plays natively it drives WizardApp.device.backgroundAudio (the Wizard Android
 * app's player: lock screen and notification controls, headset buttons, keeps playing locked) and fires the same events
 * the element would (play, pause, timeupdate, seeked, ended, loadedmetadata) from polled state. Every other case
 * (browser, an APK that does not admit wizard-podcasts, a preview, any native failure) is the plain
 * element, so today's <audio> + navigator.mediaSession path is unchanged.
 *
 * opts: request(method, params) -> Promise (WizardApp.device.backgroundAudio.request), track(src) -> native queue item or null, startAt() -> seconds to start from, skip() -> {back, forward},
 * hidden() -> bool, now() -> ms, every(fn, ms) -> timer (setInterval). Contract: wizard-ux docs/apps-out-of-core.md. */
(function (root) {
  const APP = 'wizard-podcasts';
  const EVENTS = ['loadedmetadata', 'timeupdate', 'play', 'pause', 'seeked', 'ended', 'playing', 'waiting', 'seeking', 'emptied', 'error'];

  function create(el, opts) {
    const o = Object.assign({ hidden: () => false, now: () => Date.now(), every: (fn, ms) => setInterval(fn, ms) }, opts);
    const listeners = new Map();
    let capable = null;   // null: not asked yet; then true/false (APK admits wizard-podcasts)
    let nat = null;       // {src, track, sessionId, loaded, st, at, want, ended, busy}
    let rate = 1, polling = false, timer = null;

    const fire = (type, ev) => { for (const fn of [...(listeners.get(type) || [])]) { try { fn(ev || { type }); } catch (e) { setTimeout(() => { throw e; }); } } };
    for (const type of EVENTS) el.addEventListener(type, ev => { if (!nat) fire(type, ev); });

    function probe() {
      if (capable !== null || typeof o.request !== 'function') return Promise.resolve(!!capable);
      return o.request('capabilities', {}).then(c => { capable = !!(c && Array.isArray(c.apps) && c.apps.includes(APP)); return capable; },
        () => { capable = false; return false; });
    }
    const wanted = src => capable === true && !!src && !!o.track(src);
    const ms = sec => Math.max(0, Math.round((Number(sec) || 0) * 1000));
    const pos = () => {
      if (!nat || !nat.st) return 0;
      const s = nat.st, run = s.playing ? o.now() - nat.at : 0, end = s.durationMs > 0 ? s.durationMs : Infinity;
      return Math.min(end, Math.max(0, s.positionMs + run)) / 1000;
    };
    const send = (method, params = {}) => o.request(method, Object.assign({ sessionId: nat && nat.sessionId }, params));

    // A native command that fails (another app took the player, the APK went away): the next play loads again.
    function lost() { if (nat) { nat.loaded = false; nat.want = false; } }

    function take(st) {
      if (!nat || !st || typeof st !== 'object') return;
      const before = nat.st, wasOn = nat.want;
      const ours = st.appId === APP && st.index >= 0 && (!Array.isArray(st.queue) || (st.queue[st.index] || {}).id === nat.track.id);
      if (!ours && nat.loaded) { nat.st = Object.assign({}, before || {}, { playing: false, wantPlaying: false }); nat.want = false; nat.loaded = false; if (wasOn) fire('pause'); return; }
      if (!ours) return;
      nat.st = st; nat.at = o.now(); nat.sessionId = st.sessionId || nat.sessionId;
      const on = !!(st.playing || st.wantPlaying);
      if (on !== wasOn) { nat.want = on; fire(on ? 'play' : 'pause'); }
      if (!before || (before.durationMs || 0) <= 0 && st.durationMs > 0) fire('loadedmetadata');
      const atEnd = !on && st.durationMs > 0 && st.positionMs >= st.durationMs - 1500;
      if (atEnd && !nat.ended) { nat.ended = true; fire('ended'); }
      if (!atEnd) nat.ended = false;
      fire('timeupdate');
    }
    function poll() {
      if (!nat || !nat.loaded || polling || o.hidden()) return;
      polling = true;
      const mine = nat;
      o.request('state', {}).then(st => { if (nat === mine) take(st); }, () => {}).then(() => { polling = false; });
    }
    function ticking(on) {
      if (on && !timer) timer = o.every(poll, 1000);
      if (!on && timer) { clearInterval(timer); timer = null; }
    }

    function stopNative() {
      const old = nat;
      nat = null; ticking(false);
      if (old && old.loaded && old.sessionId) o.request('stop', { sessionId: old.sessionId }).catch(() => {});
    }
    function setSrc(src) {
      if (nat && nat.src === src) return;
      if (wanted(src)) {
        stopNative();
        if (el.getAttribute('src')) { el.pause(); el.removeAttribute('src'); el.load(); }
        nat = { src, track: o.track(src), sessionId: null, loaded: false, st: null, at: 0, want: false, ended: false };
        adopt(nat);
        return;
      }
      stopNative();
      el.src = src;
    }
    // After a reload the phone may still be playing this episode: pick it up instead of starting over.
    function adopt(mine) {
      o.request('state', {}).then(st => {
        if (nat !== mine || mine.loaded || !st || st.appId !== APP || st.index < 0 || !Array.isArray(st.queue)) return;
        if ((st.queue[st.index] || {}).id !== mine.track.id) return;
        mine.loaded = true; mine.sessionId = st.sessionId; ticking(true); take(st);
      }, () => {});
    }
    // Back to the element at the same place (the native player refused or is gone).
    function fallback(mine, why) {
      if (nat !== mine) return Promise.reject(why);
      const at = mine.st ? pos() : null;
      nat = null; ticking(false);
      el.src = mine.src;
      if (at) { const seek = () => { el.removeEventListener('loadedmetadata', seek); try { el.currentTime = at; } catch (_) {} }; el.addEventListener('loadedmetadata', seek); }
      el.playbackRate = rate;
      return el.play();
    }

    function play() {
      if (!nat) return el.play();
      const mine = nat;
      if (mine.loaded) {
        mine.want = true; fire('play');
        return send('play').then(st => { if (nat === mine) take(st); }, e => { if (nat !== mine) return; lost(); return play(); });
      }
      return o.request('attach', {}).then(a => {
        if (nat !== mine) return;
        mine.sessionId = a && a.sessionId;
        const at = o.startAt();
        return o.request('load', { sessionId: mine.sessionId, index: 0, positionMs: ms(at), skip: o.skip(), queue: [mine.track] }).then(st => {
          if (nat !== mine) return;
          mine.loaded = true; mine.want = true; mine.st = null;
          fire('play');
          if (st && st.sessionId) { mine.sessionId = st.sessionId; }
          mine.st = Object.assign({ positionMs: ms(at), durationMs: 0, playing: false, wantPlaying: true }, st && st.appId === APP ? st : {});
          mine.at = o.now();
          ticking(true); poll();
        });
      }).catch(e => fallback(mine, e));
    }
    function pause() {
      if (!nat) return el.pause();
      const mine = nat;
      if (!mine.loaded) return;
      if (mine.st) { mine.st = Object.assign({}, mine.st, { positionMs: ms(pos()), playing: false, wantPlaying: false }); mine.at = o.now(); }
      if (mine.want) { mine.want = false; fire('pause'); }
      send('pause').then(st => { if (nat === mine) take(st); }, () => { if (nat === mine) lost(); });
    }
    function seek(sec) {
      if (!nat) { el.currentTime = sec; return; }
      const mine = nat;
      if (!mine.loaded) return;  // not started on the phone yet: play() starts at startAt()
      mine.st = Object.assign({}, mine.st || {}, { positionMs: ms(sec) }); mine.at = o.now(); mine.ended = false;
      send('seek', { positionMs: ms(sec) }).then(st => { if (nat === mine) { take(st); fire('seeked'); } }, () => { if (nat === mine) lost(); });
    }

    const audio = {
      probe,
      get native() { return !!nat; },
      get paused() { return nat ? !nat.want : el.paused; },
      get currentTime() { return nat ? pos() : el.currentTime; },
      set currentTime(v) { seek(v); },
      get duration() { return nat ? (nat.st && nat.st.durationMs > 0 ? nat.st.durationMs / 1000 : NaN) : el.duration; },
      get readyState() { return nat ? (nat.loaded ? 4 : 0) : el.readyState; },
      get playbackRate() { return nat ? rate : el.playbackRate; },
      set playbackRate(v) { rate = v; if (!nat) el.playbackRate = v; },
      get error() { return nat ? null : el.error; },
      get src() { return nat ? nat.src : el.src; },
      set src(v) { setSrc(v); },
      getAttribute(name) { return name === 'src' && nat ? nat.src : el.getAttribute(name); },
      removeAttribute(name) { if (name === 'src' && nat) { stopNative(); return; } el.removeAttribute(name); },
      load() { if (!nat) el.load(); },
      play, pause,
      addEventListener(type, fn) { if (!listeners.has(type)) listeners.set(type, new Set()); listeners.get(type).add(fn); },
      removeEventListener(type, fn) { const s = listeners.get(type); if (s) s.delete(fn); },
      // Back in front: catch up with what the lock screen or headset did meanwhile.
      wake() { poll(); },
    };
    return audio;
  }

  const api = { create, APP };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.PodcastsPhonePlayer = api;
})(typeof window === 'object' ? window : globalThis);
