'use strict';
/* Podcasts view (0.4): one nav bar with three buttons (My feed, Discover, Filters) at the top of a wide window and at
 * the bottom of a phone. My feed is the episodes from followed shows; Filters narrows it (one show, state, order);
 * Discover searches podcasts and YouTube channels and lists the followed shows. Feed rows work like notifications:
 * tap plays, the row's Skip dismisses it. Talks only to this app's documented relative API (api/...). Platform
 * behaviour (Back root, appearance, update holds, ducking Music) comes from the shared WizardApp client.
 * Narrow windows are thumb-first: the feed reads like a chat (newest at the bottom, list starts at the bottom), the
 * nav bar sits at the bottom with the mini player right above it; wide windows keep newest on top. */
const $ = id => document.getElementById(id);
const W = window.WizardApp && window.WizardApp.version === 1 ? window.WizardApp : null;
const audio = $('audio');
const SPEEDS = [0.8, 1, 1.2, 1.5, 1.75, 2, 2.5, 3];
const PAGE = 100;

const state = {
  tab: 'feed',            // the root the nav bar shows: 'feed' | 'discover'
  stack: [],              // pages opened above it: {view, id}
  settings: { speed: 1, skip_back: 15, skip_forward: 30, storage_cap_mb: 2048, refresh_minutes: 60, auto_download: true },
  shows: [],              // every followed show, newest episode first (from api/feed)
  feed: [], feedTotal: 0,
  show: null, showEpisodes: [],
  episode: null,
  current: null,          // episode loaded in the player
  lastSaved: 0, savedPos: -1,
  playHold: null, ducked: false,
  pendingSeek: null,
};

// ------------------------------------------------------------------ api
async function api(path, data) {
  const init = data === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) };
  const res = await fetch(path, init);
  let body = {};
  try { body = await res.json(); } catch (_) {}
  if (!res.ok) throw new Error(body.error || 'Podcasts could not do that. Try again.');
  return body;
}
function say(text, kind = '') {
  const s = $('status');
  s.classList.toggle('wz-sr', !kind);
  s.classList.toggle('note', kind === 'note');
  s.textContent = text;
  if (kind === 'error') setTimeout(() => { if (s.textContent === text) s.textContent = ''; }, 6000);
}
let toastTimer = 0;
// A toast says what happened, with Undo and at most one more action (e.g. thumbs down in the player offers Unfollow).
function toast(text, undo, extra) {
  $('toast-text').textContent = text;
  const b = $('toast-undo');
  b.hidden = !undo;
  b.onclick = undo ? async () => { $('toast').hidden = true; try { await undo(); } catch (e) { say(e.message, 'error'); } } : null;
  const a = $('toast-act');
  a.hidden = !extra;
  a.textContent = extra ? extra.label : '';
  a.onclick = extra ? async () => { $('toast').hidden = true; try { await extra.run(); } catch (e) { say(e.message, 'error'); } } : null;
  $('toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, 5000);
}

// ------------------------------------------------------------------ formatting (always the viewer's local time)
/* pure:start (no DOM; tests/test_podcasts.py runs this block in node) */
function clock(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(s).padStart(2, '0');
}
function minutes(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  if (sec < 60) return sec + ' sec';
  const h = Math.floor(sec / 3600), m = Math.round(sec % 3600 / 60);
  return h ? h + ' h' + (m ? ' ' + m + ' min' : '') : m + ' min';
}
function day(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const opts = d.getFullYear() === now.getFullYear() ? { month: 'short', day: 'numeric' } : { month: 'short', day: 'numeric', year: 'numeric' };
  return d.toLocaleDateString(undefined, opts);
}
function startOfDay(d) { const x = new Date(d); x.setHours(0, 0, 0, 0); return x; }
function daysBetween(later, earlier) { return Math.round((startOfDay(later) - startOfDay(earlier)) / 86400000); }
// the screen's time zone: Discover's 'Popular in <your country>' charts follow it (no country is baked in)
const VIEW_TZ = (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch (_) { return ''; } })();
const WEEK_START = (() => {  // 0 = Sunday … 6 = Saturday, from the viewer's locale; Monday when unknown
  try { const l = new Intl.Locale(navigator.language); const w = l.getWeekInfo ? l.getWeekInfo() : l.weekInfo; if (w && w.firstDay) return w.firstDay % 7; } catch (_) {}
  return 1;
})();
function startOfWeek(now) { const s = startOfDay(now); s.setDate(s.getDate() - (s.getDay() - WEEK_START + 7) % 7); return s; }
function timeOfDay(d) { return d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }); }
// Feed section for a publish time: Today, Yesterday, this week's weekdays, Last week, then months.
function dateGroup(ts, now = new Date()) {
  if (!ts) return { key: 'undated', label: 'Date unknown' };
  const d = new Date(ts * 1000), diff = daysBetween(now, d);
  if (diff <= 0) return { key: 'today', label: 'Today' };
  if (diff === 1) return { key: 'yesterday', label: 'Yesterday' };
  const week = startOfWeek(now);
  if (d >= week) return { key: 'day-' + d.toDateString(), label: d.toLocaleDateString(undefined, { weekday: 'long' }) };
  const lastWeek = new Date(week); lastWeek.setDate(lastWeek.getDate() - 7);
  if (d >= lastWeek) return { key: 'last-week', label: 'Last week' };
  const opts = d.getFullYear() === now.getFullYear() ? { month: 'long' } : { month: 'long', year: 'numeric' };
  return { key: 'month-' + d.getFullYear() + '-' + d.getMonth(), label: d.toLocaleDateString(undefined, opts) };
}
// When an episode was posted, for a row: '12 min ago', '3h ago', 'Yesterday 9:14 PM', 'Wed 9:14 PM', 'Oct 21'.
function posted(ts, now = new Date()) {
  if (!ts) return '';
  const d = new Date(ts * 1000), diff = daysBetween(now, d), secs = (now - d) / 1000;
  if (diff <= 0) {
    if (secs < 60) return 'Just now';
    if (secs < 3600) return Math.floor(secs / 60) + ' min ago';
    return Math.floor(secs / 3600) + 'h ago';
  }
  if (diff === 1) return 'Yesterday ' + timeOfDay(d);
  if (diff < 7) return d.toLocaleDateString(undefined, { weekday: 'short' }) + ' ' + timeOfDay(d);
  return day(ts);
}
// Full date and time for the episode page and the player: 'Wednesday, October 28, 2026 at 9:14 PM'.
function postedFull(ts) {
  if (!ts) return '';
  return new Date(ts * 1000).toLocaleString(undefined, { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit' });
}
// Feed episodes (server order) as date sections. Wide: as served (newest on top). Thumb-first: the same list run
// backwards, so sections go older -> Today and, inside one, the newest episode is the lowest row.
function feedGroups(episodes, now, thumb) {
  const groups = [];
  for (const e of thumb ? episodes.slice().reverse() : episodes) {
    const g = dateGroup(e.published, now);
    if (groups.at(-1)?.key !== g.key) groups.push({ ...g, eps: [] });
    groups.at(-1).eps.push(e);
  }
  return groups;
}
// Scroll policy for the bottom-anchored phone lists (0.4.1). The list is only ever moved by script when the owner is
// already at the bottom (gap 0) and is not touching or flinging it; scrolled up, a resize (mini player, dock, rows
// collapsing) leaves his scroll position alone.
function shouldRepin(gap, busy) { return gap === 0 && !busy; }
// A touch on a feed row: 'swipe' (skip) only for a clearly horizontal leftward move, 'scroll' as soon as it looks
// vertical (or rightward, the shell's Back), '' while undecided.
function swipeIntent(mx, my) {
  const ax = Math.abs(mx), ay = Math.abs(my);
  if (mx <= -18 && ax > ay * 2.2) return 'swipe';
  if (mx > 12 || (ay > 10 && ax <= ay * 2.2) || ax > 40) return 'scroll';
  return '';
}
// Swipe down on the mini player closes it: a mostly vertical downward drag past the threshold.
function miniDragIntent(dx, dy) {
  if (dy > 10 && dy > Math.abs(dx) * 1.2) return 'drag';
  if (Math.abs(dx) > 12 || dy < -10) return 'none';
  return '';
}
/* pure:end */
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}
function icon(name) {
  const paths = {
    play: '<path d="M8 5.5v13l11-6.5z" class="pc-fill"/>',
    pause: '<path d="M7 5h3.5v14H7zM13.5 5H17v14h-3.5z" class="pc-fill"/>',
    warn: '<path d="M12 4 3 20h18z"/><path d="M12 10v4.5M12 17.2v.3"/>',
    skip: '<path d="M7 7l10 10M17 7 7 17"/>',
    unskip: '<path d="M9 7 5 11l4 4"/><path d="M5 11h9a5 5 0 0 1 0 10h-2"/>',
    // thumbs: Lucide icons (ISC licence, see NOTICE.md)
    up: '<path d="M7 10v12"/><path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z"/>',
    down: '<path d="M17 14V2"/><path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z"/>',
  };
  const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('aria-hidden', 'true');
  s.innerHTML = paths[name];
  return s;
}
function art(img, url) {
  if (url) { img.src = url; img.hidden = false; } else { img.removeAttribute('src'); img.hidden = true; }
}
function showName(id, fallback) { const s = state.shows.find(x => x.id === id); return s ? s.short_title || s.title : fallback || ''; }

// Show notes arrive sanitized by the server; this second allow-list pass rebuilds them node by node so nothing
// but text, a few tags and http(s)/mailto links ever reaches the page.
const NOTE_TAGS = new Set(['P', 'BR', 'A', 'B', 'STRONG', 'I', 'EM', 'U', 'UL', 'OL', 'LI', 'BLOCKQUOTE', 'CODE', 'PRE', 'HR', 'H4', 'H5', 'SPAN', 'SMALL', 'SUP', 'SUB']);
function renderNotes(target, html) {
  target.replaceChildren();
  const doc = new DOMParser().parseFromString('<body>' + (html || '') + '</body>', 'text/html');
  const copy = (src, dst) => {
    for (const node of src.childNodes) {
      if (node.nodeType === 3) { dst.append(node.textContent); continue; }
      if (node.nodeType !== 1) continue;
      if (!NOTE_TAGS.has(node.tagName)) { copy(node, dst); continue; }
      const n = document.createElement(node.tagName.toLowerCase());
      if (node.tagName === 'A') {
        const href = node.getAttribute('href') || '';
        if (!/^(https?:|mailto:)/i.test(href)) { copy(node, dst); continue; }
        n.href = href; n.target = '_blank'; n.rel = 'noopener noreferrer nofollow';
      }
      copy(node, n);
      dst.append(n);
    }
  };
  copy(doc.body, target);
}

// ------------------------------------------------------------------ navigation
// Two roots (My feed, Discover) switched by the nav bar, and a stack of pages above them (show, episode, player).
const VIEWS = ['feed', 'discover', 'show', 'episode', 'player'];
function currentView() { return state.stack.length ? state.stack.at(-1).view : state.tab; }

// Thumb-first (narrow window): styles.css sets --pc-thumb inside the app's narrow container query, so the script
// and the layout always agree on one breakpoint (window width, never the device).
function readThumb() { return getComputedStyle($('main')).getPropertyValue('--pc-thumb').trim() === '1'; }
let thumb = readThumb();
// The feed is anchored to its bottom: `gap` is how far the viewport's bottom sits above the newest row (0 = pinned).
// 0.4.1: script only moves the list while it is pinned (or on open / filter change / jump to newest); once the owner
// has scrolled up, nothing re-positions it, and nothing ever moves it while a finger is down or a fling is running.
let gap = 0;
function feedShown() { return thumb && currentView() === 'feed' && !$('view-feed').hidden; }
function gapNow() { const m = $('main'); return Math.max(0, m.scrollHeight - m.scrollTop - m.clientHeight); }
// Only the owner's own scrolling moves the gap: a scroll event that merely reports where the script put the list
// (it arrives a frame later, after the dock may have grown) must not be read as a new position.
let placed = -1;
function setScroll(v) { const m = $('main'); m.scrollTop = v; placed = m.scrollTop; }
function keepGap() { const m = $('main'); setScroll(m.scrollHeight - m.clientHeight - gap); }
function toBottom() { setScroll($('main').scrollHeight); }
// Is the owner handling the list right now (finger down, or a scroll he started still running)?
let touching = false, userScrollAt = 0;
const busy = () => touching || performance.now() - userScrollAt < 220;
$('main').addEventListener('touchstart', () => { touching = true; }, { passive: true });
for (const t of ['touchend', 'touchcancel']) $('main').addEventListener(t, () => { touching = false; }, { passive: true });
$('main').addEventListener('scroll', () => {
  const m = $('main');
  if (Math.abs(m.scrollTop - placed) < 1) return;
  placed = -1;
  userScrollAt = performance.now();
  if (!feedShown()) return;
  const g = gapNow();
  gap = g < 2 ? 0 : g;
}, { passive: true });
// Work that would move the list (re-rendering the feed) waits until the owner's scroll has come to rest.
let idleWaiters = [], idleTimer = 0;
function whenIdle(fn) {
  if (!busy()) return fn();
  if (!idleWaiters.includes(fn)) idleWaiters.push(fn);
  if (idleTimer) return;
  const check = () => {
    if (busy()) { idleTimer = setTimeout(check, 120); return; }
    idleTimer = 0;
    const fns = idleWaiters; idleWaiters = [];
    for (const f of fns) f();
  };
  idleTimer = setTimeout(check, 120);
}
// Pinned: the dock or list changing height keeps the newest row on the bottom edge. Scrolled up: leave it alone.
const repin = () => { if (feedShown() && shouldRepin(gap, busy())) keepGap(); };
const bottomKeeper = new ResizeObserver(() => { if (feedShown() && gap === 0) whenIdle(repin); });
bottomKeeper.observe($('main'));
bottomKeeper.observe($('feed-groups'));
// Switching between narrow and wide re-lays the lists in that width's order.
new ResizeObserver(() => {
  const t = readThumb();
  if (t === thumb) return;
  thumb = t;
  gap = 0;
  const v = currentView();
  if (v === 'feed') { renderFeed(); if (!thumb) $('main').scrollTop = 0; }
  else if (v === 'discover') paintDiscover();
}).observe(document.querySelector('.pc-app'));

function paint() {
  const v = currentView();
  for (const name of VIEWS) $('view-' + name).hidden = name !== v;
  const app = document.querySelector('.pc-app');
  app.dataset.view = v;
  $('backrow').hidden = !state.stack.length;
  for (const [tab, b] of [['feed', $('nav-feed')], ['discover', $('nav-discover')]]) {
    if (state.tab === tab) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
  }
  // Filters narrows My feed only: it rests (disabled, same place) everywhere else so the bar never shifts.
  $('nav-filter').disabled = v !== 'feed';
  paintFilterState();
  if (v !== 'feed' && v !== 'discover') $('main').scrollTop = 0;
}
// Where each root was left, so coming back to it (tab or Back) lands on the same rows.
let feedScroll = 0, feedGap = 0, discoverScroll = 0;
function saveRoot() {
  if (state.stack.length) return;
  if (state.tab === 'feed') { feedScroll = $('main').scrollTop; feedGap = gap; }
  else discoverScroll = $('main').scrollTop;
}
async function showRoot() {
  if (state.tab === 'feed') {
    if (thumb) { gap = feedGap; keepGap(); } else $('main').scrollTop = feedScroll;
    return loadFeed().catch(e => say(e.message, 'error'));
  }
  await loadDiscover();
  if (currentView() === 'discover') $('main').scrollTop = discoverScroll;
}
// The nav bar: one tap switches roots from anywhere (pages above are dropped); tapping the root already shown
// jumps to its newest rows.
function setTab(tab) {
  if (tab === state.tab && !state.stack.length) {
    if (tab === 'feed') { if (thumb) { gap = 0; keepGap(); } else $('main').scrollTop = 0; }
    if (tab === 'discover' && !searching()) { if (!thumb) $('main').scrollTop = 0; discFetch(true); }  // re-tap: fresh picks
    return;
  }
  saveRoot();
  const from = state.tab;
  state.stack = [];
  state.tab = tab;
  paint();
  if (tab === 'discover' && from !== 'discover') { $('main').scrollTop = 0; loadDiscover().then(() => { if (!thumb && currentView() === 'discover') $('main').scrollTop = discoverScroll; }); }
  else showRoot();
}
$('nav-feed').onclick = () => setTab('feed');
$('nav-discover').onclick = () => setTab('discover');
function openView(view, id) {
  if (state.stack.at(-1)?.view === view && state.stack.at(-1)?.id === id) return;
  saveRoot();
  state.stack.push({ view, id });
  paint();
  load(view, id);
}
// Back: close the page on top; from Discover go to My feed; at My feed the shell decides (never trapped).
function popView() {
  if (state.stack.length) {
    state.stack.pop();
    paint();
    const top = state.stack.at(-1);
    if (top) load(top.view, top.id); else showRoot();
    return true;
  }
  if (state.tab !== 'feed') { setTab('feed'); return true; }
  return false;
}
$('back-btn').onclick = () => popView();
// A finishing action (mark played, unfollow) returns to the screen the owner came from: the previous view in the
// app, or, when he arrived straight here (deep link from a chat), the screen before the app.
function finish(message, undo) {
  if (popView()) { if (message) toast(message, undo); return; }
  if (W && W.nav.back()) return;
  if (message) toast(message, undo);
}
function reload() { return load(currentView(), state.stack.at(-1)?.id); }

async function load(view, id) {
  try {
    if (view === 'feed') return await loadFeed();
    if (view === 'discover') return await loadDiscover();
    if (view === 'show') return renderShow(await api('api/shows/' + id + '?limit=' + PAGE));
    if (view === 'episode') return renderEpisode(await api('api/episodes/' + id));
    if (view === 'player') return renderPlayer();
  } catch (e) { say(e.message, 'error'); }
}

// ------------------------------------------------------------------ one pop-up
function sheet(title, ...body) {
  $('sheet-h').textContent = title;
  $('sheet-body').replaceChildren(...body);
  WzSheet.open($('sheet'));
}
function closeSheet() { WzSheet.close($('sheet')); }
function seg(options, value, onPick) {
  const box = el('div', 'pc-seg');
  box.setAttribute('role', 'group');
  const paintSeg = v => { for (const b of box.children) b.setAttribute('aria-pressed', String(b.dataset.v === v)); };
  for (const [v, label] of options) {
    const b = el('button', 'pc-seg-b', label);
    b.type = 'button'; b.dataset.v = v;
    b.onclick = () => { paintSeg(v); onPick(v); };
    box.append(b);
  }
  paintSeg(value);
  return box;
}
function sheetRow(label, onclick, cls = '') {
  const b = el('button', 'pc-sheet-row' + (cls ? ' ' + cls : ''), label);
  b.type = 'button'; b.onclick = onclick;
  return b;
}

// ------------------------------------------------------------------ feed filter (remembered on this device only)
// The search words (0.4.1) live only while the app is open; show, state and order are remembered.
const FILTER_KEY = 'wizard-podcasts.feed-filter';
const STATES = ['all', 'unplayed', 'started', 'played', 'skipped'];
const filter = (() => {
  try {
    const f = JSON.parse(localStorage.getItem(FILTER_KEY) || '{}');
    return { show: Number.isInteger(f.show) ? f.show : null, state: STATES.includes(f.state) ? f.state : 'unplayed', sort: f.sort === 'oldest' ? 'oldest' : 'newest', q: '' };
  } catch (_) { return { show: null, state: 'unplayed', sort: 'newest', q: '' }; }
})();
// Searching looks through every episode: Unplayed turns into All while words are in the box and comes back when the
// search is cleared (the sheet shows the switch).
let stateBeforeSearch = null;
try { localStorage.removeItem('wizard-podcasts.upnext-filter'); } catch (_) {}  // the 0.2 filter, retired
function saveFilter() {
  const keep = { show: filter.show, state: stateBeforeSearch || filter.state, sort: filter.sort };
  try { localStorage.setItem(FILTER_KEY, JSON.stringify(keep)); } catch (_) {}
}
function setFilter(change) {
  if ('q' in change) {
    const q = change.q.replace(/\s+/g, ' ').trim();
    change.q = q;
    if (q && !filter.q && filter.state === 'unplayed' && !('state' in change)) { stateBeforeSearch = 'unplayed'; change.state = 'all'; }
    if (!q && filter.q && stateBeforeSearch && !('state' in change)) change.state = stateBeforeSearch;
    if (!q) stateBeforeSearch = null;
  }
  if ('state' in change && !('q' in change)) stateBeforeSearch = null;  // his own pick wins
  Object.assign(filter, change);
  saveFilter();
  if (thumb) gap = 0; else $('main').scrollTop = 0;
  loadFeed();
}
function feedPath(offset = 0) {
  const qs = new URLSearchParams({ limit: String(PAGE) });
  if (filter.show != null) qs.set('show_id', filter.show);
  if (filter.state !== 'unplayed') qs.set('state', filter.state);
  if (filter.sort === 'oldest') qs.set('sort', 'oldest');
  if (filter.q) qs.set('q', filter.q);
  if (offset) qs.set('offset', String(offset));
  return 'api/feed?' + qs;
}
// Filters: search, one show (or all), state and order. Picks apply at once and the sheet stays open to combine them.
function filterChip(show) {
  const b = el('button', 'pc-chip' + (show ? '' : ' pc-chip-all'));
  b.type = 'button';
  const label = show ? show.short_title || show.title : 'All shows';
  if (show) {
    if (show.artwork) { const img = el('img', 'pc-chip-art'); img.alt = ''; img.loading = 'lazy'; img.src = show.artwork; b.append(img); }
    else { const ph = el('span', 'pc-chip-art pc-chip-ph', (label || '?').slice(0, 1)); ph.setAttribute('aria-hidden', 'true'); b.append(ph); }
    b.dataset.show = show.id;
    b.title = show.title;
  } else b.dataset.all = '1';
  b.setAttribute('aria-pressed', String(show ? filter.show === show.id : filter.show == null));
  b.append(el('span', 'pc-chip-name', label));
  b.onclick = () => { setFilter({ show: show ? show.id : null }); openFilters(); };
  if (!show) return b;
  // 0.5.0: managing followed shows lives here (and on the show page): each row picks the filter, and a small Unfollow
  // sits at its end; the row stays (dimmed, 'Follow') until the sheet is opened again, so a slip is one tap to undo.
  const row = el('div', 'pc-srow');
  const x = el('button', 'pc-srow-x', 'Unfollow');
  x.type = 'button';
  x.setAttribute('aria-label', 'Unfollow ' + show.title);
  x.onclick = async () => {
    x.disabled = true;
    try {
      if (row.classList.contains('pc-off')) {
        const r = await api('api/subscribe', { url: show.feed_url, auto_download: show.auto_download });
        Object.assign(show, { id: r.show.id });
        row.classList.remove('pc-off'); x.textContent = 'Unfollow'; x.setAttribute('aria-label', 'Unfollow ' + show.title); b.disabled = false;
      } else {
        await api('api/shows/' + show.id + '/unsubscribe', {});
        if (state.current?.show_id === show.id) unloadPlayer();
        if (filter.show === show.id) { filter.show = null; saveFilter(); }
        row.classList.add('pc-off'); x.textContent = 'Follow'; x.setAttribute('aria-label', 'Follow ' + show.title); b.disabled = true;
      }
      loadFeed().catch(() => {});
    } catch (e) { say(e.message, 'error'); }
    finally { x.disabled = false; }
  };
  row.append(b, x);
  return row;
}
let stateSeg = null;
function searchField() {
  const form = el('form', 'pc-qform');
  form.setAttribute('role', 'search');
  form.autocomplete = 'off';
  const input = el('input', 'wz-field pc-qinput');
  Object.assign(input, { type: 'search', id: 'feed-q', placeholder: 'Search episodes', value: filter.q, maxLength: 200, enterKeyHint: 'search' });
  input.setAttribute('aria-label', 'Search episodes');
  input.inputMode = 'search';
  let t = 0;
  input.addEventListener('input', () => {
    clearTimeout(t);
    t = setTimeout(() => { if (input.value.trim() !== filter.q) { setFilter({ q: input.value }); paintStateSeg(); } }, 250);
  });
  // Enter: search now and show the results (the keyboard and the sheet go away).
  form.onsubmit = e => { e.preventDefault(); clearTimeout(t); if (input.value.trim() !== filter.q) setFilter({ q: input.value }); input.blur(); closeSheet(); };
  form.append(input);
  return form;
}
function paintStateSeg() { if (stateSeg) for (const b of stateSeg.children) b.setAttribute('aria-pressed', String(b.dataset.v === filter.state)); }
function openFilters() {
  if (currentView() !== 'feed') return;
  const keep = $('sheet-body').querySelector('.pc-showgrid')?.scrollTop || 0;
  const grid = el('div', 'pc-showgrid pc-showlist');
  grid.setAttribute('role', 'group'); grid.setAttribute('aria-label', 'Shows');
  grid.append(filterChip(null), ...state.shows.map(filterChip));
  // 'Newest' means newest where the eye starts: on top in a wide window, at the bottom (by the thumb) on a phone.
  const order = thumb ? [['newest', 'Newest at bottom'], ['oldest', 'Oldest at bottom']] : [['newest', 'Newest first'], ['oldest', 'Oldest first']];
  stateSeg = seg([['all', 'All'], ['unplayed', 'Unplayed'], ['started', 'Started'], ['played', 'Played'], ['skipped', 'Skipped']], filter.state, v => setFilter({ state: v }));
  stateSeg.classList.add('pc-seg-state');
  const rows = [searchField(), grid, stateSeg, seg(order, filter.sort, v => setFilter({ sort: v }))];
  // With one show chosen: clear everything it has waiting in one go (Undo in the toast).
  const show = filter.show != null && state.shows.find(s => s.id === filter.show);
  if (show && show.unplayed > 0) rows.push(sheetRow('Skip all from ' + (show.short_title || show.title), () => { closeSheet(); skipAll(show); }));
  const typing = document.activeElement?.id === 'feed-q';
  sheet('Filters', ...rows);
  if (typing) $('sheet-body').querySelector('.pc-qinput')?.focus();
  grid.scrollTop = keep;
  if (!keep) grid.querySelector('[aria-pressed="true"]')?.scrollIntoView({ block: 'nearest' });
}
$('nav-filter').onclick = openFilters;
// The filter in force, visible without opening the sheet: a dot on Filters, and the chosen show and the search words
// as pills (one tap clears each).
function paintFilterState() {
  $('filter-dot').hidden = filter.show == null && filter.state === 'unplayed' && filter.sort === 'newest' && !filter.q;
  const show = filter.show != null && state.shows.find(s => s.id === filter.show);
  const onFeed = currentView() === 'feed';
  $('filter-pill').hidden = !show;
  $('q-pill').hidden = !filter.q;
  $('pillbar').hidden = !onFeed || (!show && !filter.q);
  if (filter.q) { $('q-pill-text').textContent = filter.q; $('q-pill').setAttribute('aria-label', 'Search: ' + filter.q + '. Clear search'); }
  if (!show) return;
  $('pill-name').textContent = show.short_title || show.title;
  art($('pill-art'), show.artwork);
  $('filter-pill').setAttribute('aria-label', 'Showing only ' + show.title + '. Show all');
}
$('filter-pill').onclick = () => setFilter({ show: null });
$('q-pill').onclick = () => setFilter({ q: '' });

// One show's actions (the show page's ⋯): skip all, auto-download, unfollow (second tap confirms).
function showSheet(show) {
  const auto = el('label', 'pc-sheet-row pc-switch');
  const box = el('input'); box.type = 'checkbox'; box.checked = !!show.auto_download;
  auto.append(el('span', '', 'Auto-download'), box);
  box.onchange = async () => {
    try { const d = await api('api/shows/' + show.id, { auto_download: box.checked }); show.auto_download = d.show.auto_download; box.checked = d.show.auto_download; }
    catch (e) { box.checked = !box.checked; say(e.message, 'error'); }
  };
  const unsub = sheetRow('Unfollow', () => {
    if (!unsub.dataset.armed) { unsub.dataset.armed = '1'; unsub.textContent = 'Tap again to unfollow'; return; }
    unfollow(show);
  }, 'pc-danger');
  const rows = [];
  const waiting = state.shows.find(s => s.id === show.id)?.unplayed ?? show.unplayed ?? 0;
  if (waiting > 0) rows.push(sheetRow('Skip all from ' + (show.short_title || show.title), () => { closeSheet(); skipAll(show); }));
  rows.push(auto, unsub);
  sheet(show.title, ...rows);
}
async function unfollow(show) {
  try {
    await api('api/shows/' + show.id + '/unsubscribe', {});
    closeSheet();
    if (state.current?.show_id === show.id) unloadPlayer();
    if (filter.show === show.id) { filter.show = null; saveFilter(); }
    const undo = async () => { await api('api/subscribe', { url: show.feed_url, auto_download: show.auto_download }); reload(); };
    if (currentView() === 'show') finish('Unfollowed ' + show.title, undo);
    else { toast('Unfollowed ' + show.title, undo); reload(); }
  } catch (e) { say(e.message, 'error'); }
}

// ------------------------------------------------------------------ skip: rows are dismissed like notifications
async function skipAll(show) {
  try {
    const r = await api('api/shows/' + show.id + '/skip-all', {});
    if (state.current && r.episode_ids.includes(state.current.id) && audio.paused) unloadPlayer();
    toast(r.count ? 'Skipped ' + r.count : 'Nothing to skip', r.count ? async () => {
      await api('api/shows/' + show.id + '/skip-all', { skipped: false, episode_ids: r.episode_ids }); reload();
    } : null);
    reload();
  } catch (e) { say(e.message, 'error'); }
}
// One tap: the row leaves in place (feed) or turns quiet (show page); Undo lives in the toast.
async function skipEpisode(li, ep, on) {
  li.classList.add('pc-busy');
  let fresh;
  try { fresh = await api('api/episodes/' + ep.id + '/skip', { skipped: on }); }
  catch (e) { li.classList.remove('pc-busy'); resetSwipe(li); say(e.message, 'error'); return; }
  if (on && state.current?.id === ep.id && audio.paused) unloadPlayer();
  const undo = async () => { await api('api/episodes/' + ep.id + '/skip', { skipped: !on }); reload(); };
  toast(on ? 'Skipped' : 'Back in your feed', undo);
  if (currentView() === 'feed') removeRow(li, ep.id);
  else { const row = episodeRow(fresh, { showShow: li.dataset.showShow === '1' }); li.replaceWith(row); }
}
function removeRow(li, id) {
  state.feed = state.feed.filter(e => e.id !== id);
  state.feedTotal = Math.max(0, state.feedTotal - 1);
  const drop = () => {
    const sec = li.closest('.pc-group');
    li.remove();
    if (sec && !sec.querySelector('.pc-ep')) sec.remove();
    if (!state.feed.length) renderFeed();
  };
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return drop();
  li.style.height = li.offsetHeight + 'px';
  requestAnimationFrame(() => { li.classList.add('pc-gone'); li.style.height = '0px'; });
  setTimeout(drop, 220);
}
// Phone: swipe a feed row left to skip it. Edges stay with the shell's own gestures; vertical moves scroll. The row
// only takes the gesture once it is clearly horizontal (swipeIntent), never while the list is still flinging, and
// gives it up for good as soon as it looks vertical, so a slightly diagonal scroll never drags a row sideways.
const EDGE = 28;
function resetSwipe(li) { li.classList.remove('pc-swiping', 'pc-armed'); li.style.removeProperty('--dx'); }
function swipeable(li, onSkip) {
  let id = null, x0 = 0, y0 = 0, dx = 0, mode = '', top0 = 0;
  li.addEventListener('pointerdown', e => {
    if (e.pointerType !== 'touch' || id !== null || busy()) return;
    if (e.clientX < EDGE || e.clientX > document.documentElement.clientWidth - EDGE) return;
    id = e.pointerId; x0 = e.clientX; y0 = e.clientY; dx = 0; mode = ''; top0 = $('main').scrollTop;
  });
  li.addEventListener('pointermove', e => {
    if (e.pointerId !== id) return;
    const mx = e.clientX - x0, my = e.clientY - y0;
    if (!mode) {
      const intent = Math.abs($('main').scrollTop - top0) > 1 ? 'scroll' : swipeIntent(mx, my);
      if (intent === 'scroll') { id = null; return; }
      if (!intent) return;
      mode = 'swipe'; li.classList.add('pc-swiping'); try { li.setPointerCapture(id); } catch (_) {}
    }
    dx = Math.min(0, mx);
    li.style.setProperty('--dx', dx + 'px');
    li.classList.toggle('pc-armed', -dx > li.clientWidth * 0.3);
  });
  const end = e => {
    if (e.pointerId !== id) return;
    id = null;
    if (mode !== 'swipe') return;
    li.__swiped = true; setTimeout(() => { li.__swiped = false; }, 400);
    if (e.type === 'pointerup' && -dx > li.clientWidth * 0.3) { li.style.setProperty('--dx', -li.clientWidth + 'px'); onSkip(); }
    else resetSwipe(li);
  };
  li.addEventListener('pointerup', end);
  li.addEventListener('pointercancel', end);
}

// ------------------------------------------------------------------ discover: recommendations (empty box) or search results
// 0.5.0: Discover is a feed of shows he might like, ranked on the box from what he follows, thumbs, hears through and
// skips (api/discover). Each card: artwork, name, one reason line, the newest episode; thumbs up/down, Follow, and a tap
// previews the newest episode in the player without following. Thumb-first: the best card is the lowest one, right
// above the search box; reaching the far end (top on a phone, bottom on a wide window) loads more.
const searching = () => !!$('search-input').value.trim();
const disc = { items: [], cursor: '0', done: false, loading: false, at: 0, seq: 0, poll: 0 };
const DISC_PAGE = 20;
async function loadDiscover() {
  paintDiscover();
  if (searching()) return;
  if (!disc.items.length || Date.now() - disc.at > 10 * 60000) await discFetch(true);
}
function paintDiscover() {
  const on = searching();
  $('disc-list').hidden = on;
  $('search-results').hidden = !on;
  if (!on) $('search-note').hidden = true;
  if (on) { $('disc-more').hidden = true; $('disc-note').hidden = true; paintResults(); }
  else renderDiscover();
}
// Fresh = start over (open, re-tap Discover, still gathering); otherwise the next page goes on the far end.
async function discFetch(fresh) {
  if (disc.loading && !fresh) return;
  if (!fresh && (disc.done || disc.cursor == null)) return;
  const seq = ++disc.seq;
  disc.loading = true;
  clearTimeout(disc.poll);
  try {
    const d = await api('api/discover?limit=' + DISC_PAGE + '&cursor=' + encodeURIComponent(fresh ? '0' : disc.cursor) + '&tz=' + encodeURIComponent(VIEW_TZ));
    if (seq !== disc.seq) return;
    const have = new Set(fresh ? [] : disc.items.map(i => i.feed_url));
    const add = (d.items || []).filter(i => !have.has(i.feed_url));
    disc.items = fresh ? add : disc.items.concat(add);
    disc.cursor = d.next_cursor; disc.done = d.next_cursor == null; disc.at = Date.now();
    for (const i of disc.items) tasteOf.set(i.feed_url, i.taste || 0);
    // the first list is still being gathered on the box: show what there is and look again shortly
    if (d.building && disc.items.length < DISC_PAGE) disc.poll = setTimeout(() => { if (currentView() === 'discover' && !searching()) discFetch(true); }, 5000);
    disc.building = !!d.building;
    if (fresh) renderDiscover(); else appendDiscover(add);
  } catch (e) { if (seq === disc.seq) say(e.message, 'error'); }
  finally { if (seq === disc.seq) disc.loading = false; }
}
function renderDiscover() {
  if (currentView() !== 'discover' || searching()) return;
  const list = disc.items.map(cardRow);
  $('disc-list').replaceChildren(...(thumb ? list.reverse() : list));
  paintDiscoverEnd();
  if (thumb) toBottom();
}
// Another page: wide windows add it below; a phone adds it above (older picks further from the thumb) and keeps the
// cards in view where they were.
function appendDiscover(items) {
  if (currentView() !== 'discover' || searching()) return;
  const rows = items.map(cardRow);
  const m = $('main');
  if (thumb) {
    const before = m.scrollHeight;
    $('disc-list').prepend(...rows.reverse());
    setScroll(m.scrollTop + (m.scrollHeight - before));
  } else $('disc-list').append(...rows);
  paintDiscoverEnd();
}
function paintDiscoverEnd() {
  const empty = !disc.items.length;
  $('disc-more').hidden = empty || disc.done;
  $('disc-note').hidden = !empty;
  $('disc-note').textContent = disc.loading || disc.building ? 'Finding shows for you…' : 'Nothing to recommend yet. Follow a show or two first.';
}
$('disc-more').onclick = () => discFetch(false);
// Reaching the far end of the list loads the next page on its own.
new IntersectionObserver(es => { if (es.some(e => e.isIntersecting) && currentView() === 'discover' && !searching() && disc.items.length) discFetch(false); },
  { root: $('main'), rootMargin: '400px 0px' }).observe($('disc-more'));

// His thumb on each show (Discover cards, followed shows, the player): 1, -1 or 0.
const tasteOf = new Map();
async function setTaste(feedUrl, value) {
  const r = await api('api/shows/taste', { feed_url: feedUrl, value });
  tasteOf.set(feedUrl, r.value);
  const s = state.shows.find(x => x.feed_url === feedUrl); if (s) s.taste = r.value;
  const c = disc.items.find(x => x.feed_url === feedUrl); if (c) c.taste = r.value;
  return r;
}
function thumbButton(dir, item, onPick) {
  const b = el('button', 'pc-thumb');
  b.type = 'button'; b.dataset.v = String(dir);
  b.append(icon(dir > 0 ? 'up' : 'down'));
  b.setAttribute('aria-label', (dir > 0 ? 'More like ' : 'Less like ') + item.title);
  b.title = dir > 0 ? 'More like this' : 'Not for me';
  b.setAttribute('aria-pressed', String((tasteOf.get(item.feed_url) ?? item.taste ?? 0) === dir));
  b.onclick = onPick;
  return b;
}
function cardRow(item) {
  const li = el('li', 'pc-card');
  li.dataset.feed = item.feed_url;
  if (state.current?.preview && state.current.feed_url === item.feed_url) li.classList.add('pc-current');
  const open = el('button', 'pc-card-open');
  open.type = 'button';
  let img;
  if (item.artwork) { img = el('img', 'pc-art pc-art-card'); img.alt = ''; img.loading = 'lazy'; img.referrerPolicy = 'no-referrer'; img.src = item.artwork; }
  else { img = el('span', 'pc-art pc-art-card pc-chip-ph', (item.title || '?').slice(0, 1)); img.setAttribute('aria-hidden', 'true'); }
  const text = el('span', 'pc-card-text');
  text.append(el('b', 'pc-card-name', item.title), el('small', 'pc-reason', item.reason));
  const lt = item.latest;
  if (lt && lt.title) {
    const ep = el('span', 'pc-card-ep', lt.title);
    // a channel page only says '3 days ago': show the day, not a made-up time
    const when = el('small', 'pc-card-when', lt.approx ? (lt.published ? dateGroup(lt.published).label : '') : posted(lt.published));
    if (lt.published) when.title = postedFull(lt.published);
    text.append(ep, when);
  }
  open.append(img, text);
  open.setAttribute('aria-label', 'Preview ' + item.title + (lt && lt.title ? ': ' + lt.title : ''));
  open.disabled = !(lt && lt.playable);
  open.onclick = () => previewShow(item);
  const acts = el('div', 'pc-card-acts');
  const up = thumbButton(1, item, async () => {
    const v = up.getAttribute('aria-pressed') === 'true' ? 0 : 1;
    up.disabled = true;
    try { await setTaste(item.feed_url, v); up.setAttribute('aria-pressed', String(v === 1)); down.setAttribute('aria-pressed', 'false'); }
    catch (e) { say(e.message, 'error'); }
    finally { up.disabled = false; }
  });
  const down = thumbButton(-1, item, () => dislikeCard(li, item));
  const follow = el('button', 'pc-follow');
  follow.type = 'button';
  let showId = null;
  const paintFollow = on => { follow.textContent = on ? 'Following' : 'Follow'; follow.setAttribute('aria-pressed', String(on)); follow.setAttribute('aria-label', (on ? 'Unfollow ' : 'Follow ') + item.title); };
  paintFollow(false);
  follow.onclick = async () => {
    follow.disabled = true;
    try {
      if (showId) {  // a second tap takes the follow back (not a 'less like this')
        await api('api/shows/' + showId + '/unsubscribe', { undo: true });
        if (state.current?.show_id === showId) unloadPlayer();
        showId = null; paintFollow(false);
      } else {
        const r = await api('api/subscribe', { url: item.feed_url });
        showId = r.show.id; paintFollow(true);
        toast('Following ' + r.show.title);
        youtubeHint(r.show);
      }
    } catch (e) { say(e.message, 'error'); }
    finally { follow.disabled = false; }
  };
  acts.append(up, down, follow);
  li.append(open, acts);
  return li;
}
// Thumbs down: the card leaves in place; Undo puts it back where it was.
async function dislikeCard(li, item) {
  li.classList.add('pc-busy');
  try { await setTaste(item.feed_url, -1); }
  catch (e) { li.classList.remove('pc-busy'); say(e.message, 'error'); return; }
  const at = disc.items.indexOf(item);
  disc.items = disc.items.filter(x => x !== item);
  if (state.current?.preview && state.current.feed_url === item.feed_url && audio.paused) unloadPlayer();
  toast('Not for you', async () => {
    await setTaste(item.feed_url, 0);
    item.taste = 0;
    disc.items.splice(Math.max(0, Math.min(at, disc.items.length)), 0, item);
    renderDiscoverKeep();
  });
  const drop = () => { li.remove(); paintDiscoverEnd(); };
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return drop();
  li.style.height = li.offsetHeight + 'px';
  requestAnimationFrame(() => { li.classList.add('pc-gone'); li.style.height = '0px'; });
  setTimeout(drop, 220);
}
// Re-render without moving what is under the thumb (phone: keep the distance to the bottom).
function renderDiscoverKeep() {
  if (currentView() !== 'discover' || searching()) return;
  const m = $('main'), fromEnd = m.scrollHeight - m.scrollTop, top = m.scrollTop;
  const list = disc.items.map(cardRow);
  $('disc-list').replaceChildren(...(thumb ? list.reverse() : list));
  paintDiscoverEnd();
  setScroll(thumb ? m.scrollHeight - fromEnd : top);
}
// Preview: the show's newest episode plays in the usual player without following it; nothing is saved.
function previewShow(item) {
  if (state.current?.preview && state.current.feed_url === item.feed_url) { togglePlay(); return; }
  const lt = item.latest || {};
  playEpisode({ id: 'preview:' + item.feed_url, preview: true, feed_url: item.feed_url, show_id: null, title: lt.title || item.title,
    show_title: item.title, artwork: item.artwork, published: lt.published || 0, duration: 0, position: 0, position_at: 0, played: false,
    audio: 'api/discover/audio?feed_url=' + encodeURIComponent(item.feed_url) });
}

// ------------------------------------------------------------------ feed
let feedSeq = 0;
async function loadFeed(append = false) {
  const seq = ++feedSeq;
  const data = await api(feedPath(append ? state.feed.length : 0));
  if (seq !== feedSeq) return;
  state.shows = data.shows || [];
  // A remembered show that is no longer followed falls back to All.
  if (filter.show != null && !state.shows.some(s => s.id === filter.show)) { filter.show = null; saveFilter(); return loadFeed(); }
  // New rows never land under a moving finger: the re-render waits for the owner's scroll to come to rest.
  whenIdle(() => {
    if (seq !== feedSeq) return;
    state.feed = append ? state.feed.concat(data.episodes) : data.episodes;
    state.feedTotal = data.total;
    paintFilterState();
    renderFeed();
    if (data.now_playing && !state.current && !miniClosed(data.now_playing)) loadIntoPlayer(data.now_playing, false);
  });
}
$('feed-more').onclick = () => loadFeed(true).catch(e => say(e.message, 'error'));

function episodeRow(ep, { showShow = true } = {}) {
  const li = el('li', 'pc-ep' + (ep.played || ep.skipped ? ' pc-played' : ''));
  li.dataset.id = ep.id;
  li.dataset.showShow = showShow ? '1' : '';
  if (state.current?.id === ep.id) li.classList.add('pc-current');
  const open = el('button', 'wz-row pc-ep-open');
  open.type = 'button';
  const img = el('img', 'pc-art pc-art-row'); img.alt = ''; img.loading = 'lazy';
  art(img, ep.artwork);
  const main = el('span', 'wz-main');
  const meta = el('small', 'pc-meta');
  const bits = [];
  const when = posted(ep.published);
  if (when) bits.push(when);
  if (ep.duration) bits.push(minutes(ep.duration));
  // the show name gives way first on a narrow row; when and how long always stay whole
  if (showShow) meta.append(el('span', 'pc-meta-show', showName(ep.show_id, ep.show_title)), el('span', 'pc-meta-sep', bits.length ? ' · ' : ''));
  meta.append(el('span', 'pc-meta-when', bits.join(' · ')));
  if (ep.published) meta.title = postedFull(ep.published);
  main.append(el('b', 'pc-clamp', ep.title), meta);
  if (ep.position > 0 && ep.duration && !ep.played) {
    const bar = el('span', 'pc-progress'); const fill = el('i');
    fill.style.width = Math.min(100, 100 * ep.position / ep.duration) + '%';
    bar.append(fill); main.append(bar);
  }
  open.append(img, main);
  if (ep.download?.state === 'failed') {
    const w = el('span', 'pc-failed'); w.append(icon('warn')); w.title = 'Download failed';
    w.setAttribute('aria-label', 'Download failed'); open.append(w);
  }
  // Tapping the row plays it (the playing row pauses); notes live behind the player's title.
  open.onclick = () => { if (li.__swiped) return; if (state.current?.id === ep.id) togglePlay(); else playEpisode(ep); };
  open.setAttribute('aria-label', 'Play ' + ep.title);
  li.append(open);
  if (!ep.played) {
    const on = !ep.skipped;
    const x = el('button', 'pc-skip');
    x.type = 'button';
    x.append(icon(on ? 'skip' : 'unskip'));
    x.title = on ? 'Skip' : 'Unskip';
    x.setAttribute('aria-label', (on ? 'Skip ' : 'Unskip ') + ep.title);
    x.onclick = () => skipEpisode(li, ep, on);
    li.append(x);
    if (on && currentView() === 'feed') swipeable(li, () => skipEpisode(li, ep, true));
  }
  return li;
}
// Thumb-first keeps the row the owner is looking at in place across a re-render (new episodes, Undo, More), or
// stays pinned to the newest row when he was already at the bottom.
// The anchor is the lowest row in view (the one nearest the thumb), so rows coming back or arriving above it push
// the older rows up instead of moving what is under his finger.
function feedAnchor() {
  if (!feedShown() || gap === 0) return null;
  const box = $('main').getBoundingClientRect();
  for (const li of [...$('feed-groups').querySelectorAll('.pc-ep')].reverse()) {
    const r = li.getBoundingClientRect();
    if (r.top < box.bottom - 1) return { id: li.dataset.id, off: r.top - box.top };
  }
  return null;
}
function restoreAnchor(a) {
  if (!feedShown()) return;
  const m = $('main');
  const li = a && $('feed-groups').querySelector('.pc-ep[data-id="' + a.id + '"]');
  if (li) { setScroll(m.scrollTop + li.getBoundingClientRect().top - m.getBoundingClientRect().top - a.off); gap = gapNow(); }
  else keepGap();
}
// Episodes in quiet date sections (Today, Yesterday, weekdays, Last week, months) in the server's order, so
// Oldest first simply runs the sections the other way (and thumb-first runs them bottom-up).
function renderFeed() {
  const anchor = feedAnchor();
  const groups = feedGroups(state.feed, new Date(), thumb);
  const showShow = filter.show == null;
  $('feed-groups').replaceChildren(...groups.map(g => {
    const sec = el('section', 'pc-group');
    sec.dataset.group = g.key;
    const ul = el('ul', 'wz-list');
    ul.append(...g.eps.map(e => episodeRow(e, { showShow })));
    sec.append(el('h2', 'pc-day-h', g.label), ul);
    return sec;
  }));
  $('feed-more').hidden = state.feed.length >= state.feedTotal;
  const empty = !state.feed.length;
  $('feed-empty').hidden = !empty;
  if (thumb) restoreAnchor(anchor);
  if (!empty) return;
  const go = $('feed-empty-go');
  go.hidden = true;
  if (!state.shows.length) {
    $('feed-empty-text').textContent = 'Follow a show to see its episodes here.';
    go.hidden = false; go.textContent = 'Discover shows'; go.onclick = () => setTab('discover');
  } else if (filter.q) {
    $('feed-empty-text').textContent = 'Nothing found.';
  } else if (filter.show != null) {
    $('feed-empty-text').textContent = filter.state === 'played' ? 'Nothing played yet.' : filter.state === 'started' ? 'Nothing started.' : filter.state === 'skipped' ? 'Nothing skipped.' : 'Nothing new.';
    go.hidden = false; go.textContent = 'All episodes'; go.onclick = () => openView('show', filter.show);
  } else {
    $('feed-empty-text').textContent = filter.state === 'played' ? 'Nothing played yet.' : filter.state === 'started' ? 'Nothing started.' : filter.state === 'skipped' ? 'Nothing skipped.' : 'All caught up.';
  }
}

// ------------------------------------------------------------------ show page (full back catalogue)
function renderShow(data, append = false) {
  const s = data.show;
  state.show = s;
  state.showEpisodes = append ? state.showEpisodes.concat(data.episodes) : data.episodes;
  art($('show-art'), s.artwork);
  $('show-title').textContent = s.title;
  $('show-meta').textContent = [s.author !== s.title ? s.author : '', s.episodes + ' episodes'].filter(Boolean).join(' · ');
  $('show-error').hidden = !s.last_error;
  $('show-error').textContent = s.last_error ? 'Last check failed: ' + s.last_error : '';
  const list = $('show-episodes');
  if (!append) list.replaceChildren();
  for (const e of data.episodes) list.append(episodeRow(e, { showShow: false }));
  $('show-more').hidden = state.showEpisodes.length >= s.episodes;
  if (s.pending) setTimeout(() => { if (currentView() === 'show' && state.show?.id === s.id) load('show', s.id); }, 4000);
}
$('show-more').onclick = async () => {
  try { renderShow(await api('api/shows/' + state.show.id + '?limit=' + PAGE + '&offset=' + state.showEpisodes.length), true); } catch (e) { say(e.message, 'error'); }
};
$('show-actions').onclick = () => state.show && showSheet(state.show);

// ------------------------------------------------------------------ episode page
let epPoll = 0;
function renderEpisode(ep) {
  state.episode = ep;
  art($('ep-art'), ep.artwork);
  $('ep-show').textContent = ep.show_title || '';
  $('ep-show').onclick = () => openView('show', ep.show_id);
  $('ep-title').textContent = ep.title;
  $('ep-meta').textContent = [postedFull(ep.published), ep.duration ? minutes(ep.duration) : ''].filter(Boolean).join(' · ');
  paintEpisodePlay();
  const d = ep.download;
  const dl = $('ep-download');
  dl.disabled = false;
  if (d.state === 'done') { dl.textContent = 'Remove download'; dl.onclick = () => episodeAction(ep, 'delete-download', 'Download removed'); }
  else if (d.state === 'queued' || d.state === 'downloading') { dl.textContent = 'Stop download'; dl.onclick = () => episodeAction(ep, 'delete-download', 'Download stopped'); }
  else { dl.textContent = d.state === 'failed' ? 'Retry download' : 'Download'; dl.onclick = () => episodeAction(ep, 'download', 'Downloading'); }
  const note = d.state === 'downloading' ? 'Downloading' + (d.size_bytes ? ' ' + Math.round(100 * d.progress_bytes / d.size_bytes) + '%' : '…')
    : d.state === 'failed' ? (d.error || 'Download failed') : '';
  $('ep-dl-note').hidden = !note; $('ep-dl-note').textContent = note;
  $('ep-mark').textContent = ep.played ? 'Mark unplayed' : 'Mark played';
  $('ep-mark').onclick = async () => {
    try {
      const was = ep.played, inbox = ep.in_inbox;
      await api('api/episodes/' + ep.id + '/mark', { played: !was });
      if (!was) {
        if (state.current?.id === ep.id) unloadPlayer();
        finish('Marked played', async () => { await api('api/episodes/' + ep.id + '/mark', { played: false, in_inbox: inbox }); reload(); });
      } else renderEpisode(await api('api/episodes/' + ep.id));
    } catch (e) { say(e.message, 'error'); }
  };
  renderNotes($('ep-notes'), ep.notes_html);
  clearTimeout(epPoll);
  if (['queued', 'downloading'].includes(d.state)) epPoll = setTimeout(() => { if (currentView() === 'episode' && state.episode?.id === ep.id && !document.hidden) load('episode', ep.id); }, 4000);
}
function paintEpisodePlay() {
  const ep = state.episode;
  if (!ep) return;
  const isCurrent = state.current?.id === ep.id, playing = isCurrent && !audio.paused;
  const pos = isCurrent ? audio.currentTime : ep.position;
  const label = playing ? 'Pause' : pos > 0 && !ep.played ? 'Resume from ' + clock(pos) : 'Play';
  $('ep-play').replaceChildren(icon(playing ? 'pause' : 'play'), el('span', '', label));
  $('ep-play').onclick = () => (isCurrent ? togglePlay() : playEpisode(ep));
}
async function episodeAction(ep, action, message) {
  $('ep-download').disabled = true;
  try { renderEpisode(await api('api/episodes/' + ep.id + '/' + action, {})); toast(message); }
  catch (e) { say(e.message, 'error'); $('ep-download').disabled = false; }
}

// search: podcasts and YouTube channels in one list
let searchSeq = 0, searchTimer = 0;
let results = [];
const isFeedUrl = t => /^https?:\/\//i.test(t) && !/youtube\.com|youtu\.be/i.test(t);
async function runSearch(text) {
  const seq = ++searchSeq;
  const note = $('search-note');
  note.hidden = false; note.textContent = 'Searching…';
  try {
    const r = await api('api/search?q=' + encodeURIComponent(text));
    if (seq !== searchSeq) return;
    const a = r.results || [], y = r.youtube || [], merged = [];
    for (let i = 0; i < Math.max(a.length, y.length); i++) { if (a[i]) merged.push(a[i]); if (y[i]) merged.push(y[i]); }
    const nameKey = t => String(t || '').toLowerCase().replace(/^@/, '').replace(/[^\p{L}\p{N}]+/gu, ' ').trim();
    const want = nameKey(text);
    merged.sort((x, z) => (nameKey(z.title) === want) - (nameKey(x.title) === want));  // stable: exact name first
    results = merged;
    paintResults();
    note.hidden = merged.length > 0;
    note.textContent = merged.length ? '' : 'No shows found.';
    if (thumb) toBottom();
  } catch (e) { if (seq === searchSeq) { note.hidden = true; say(e.message, 'error'); } }
}
// Thumb-first: the search box sits at the bottom like a composer and the best match is the row right above it.
function paintResults() {
  $('search-results').replaceChildren(...(thumb ? results.slice().reverse() : results).map(resultRow));
  if (thumb) toBottom();
}
let wasSearching = false;
$('search-input').addEventListener('input', () => {
  clearTimeout(searchTimer);
  const text = $('search-input').value.trim();
  // Emptying the box brings the followed shows back (fresh, so a show just followed from results is in it).
  if (!text) { ++searchSeq; results = []; wasSearching = false; loadDiscover(); return; }
  if (!wasSearching) { wasSearching = true; results = []; paintDiscover(); }
  if (text.length < 3 || /^https?:\/\//i.test(text)) return;
  searchTimer = setTimeout(() => runSearch(text), 600);
});
$('search-form').onsubmit = async event => {
  event.preventDefault();
  clearTimeout(searchTimer);
  const text = $('search-input').value.trim();
  if (!text) return;
  if (!isFeedUrl(text)) return runSearch(text);
  try {
    say('Loading feed…', 'note');
    const r = await api('api/subscribe', { url: text });
    say('');
    $('search-input').value = ''; wasSearching = false; results = [];
    toast(r.already_subscribed ? 'Already following ' + r.show.title : 'Following ' + r.show.title);
    openView('show', r.show.id);
  } catch (e) { say(e.message, 'error'); }
};
// Following a YouTube channel on a Wizard without YouTube Music: say so at once, not at the first failed play.
function youtubeHint(show) {
  if (show && show.source === 'youtube') api('api/status').then(st => { if (st.youtube && !st.youtube.installed && st.youtube.message) say(st.youtube.message, 'error'); }).catch(() => {});
}
function resultRow(item) {
  const li = el('li', 'pc-ep pc-result');
  const main = el('div', 'wz-row');
  const img = el('img', 'pc-art pc-art-row'); img.alt = ''; img.loading = 'lazy';
  art(img, item.artwork);
  const text = el('span', 'wz-main');
  const sub = item.source === 'youtube' ? [item.author, item.subscribers] : [item.author];
  text.append(el('b', 'pc-clamp', item.title), el('small', '', sub.filter(Boolean).join(' · ')));
  main.append(img, text);
  const btn = el('button', 'pc-follow');
  btn.type = 'button';
  let showId = item.show_id || null;
  const paintBtn = on => { btn.textContent = on ? 'Following' : 'Follow'; btn.setAttribute('aria-pressed', String(on)); };
  paintBtn(!!item.subscribed);
  btn.onclick = async () => {
    btn.disabled = true;
    try {
      if (btn.getAttribute('aria-pressed') === 'true' && showId) {
        await api('api/shows/' + showId + '/unsubscribe', {});
        if (state.current?.show_id === showId) unloadPlayer();
        showId = null; paintBtn(false);
      } else {
        const r = await api('api/subscribe', { url: item.feed_url });
        showId = r.show.id; paintBtn(true); youtubeHint(r.show);
      }
    } catch (e) { say(e.message, 'error'); }
    finally { btn.disabled = false; }
  };
  li.append(main, btn);
  return li;
}

// ------------------------------------------------------------------ settings (the gear in Discover)
$('open-settings').onclick = async () => {
  try {
    const [s, st] = await Promise.all([api('api/settings'), api('api/status')]);
    state.settings = s;
    const opt = (value, label) => new Option(label, value);
    const cap = el('select', 'wz-field');
    cap.append(...[[512, '0.5 GB'], [1024, '1 GB'], [2048, '2 GB'], [5120, '5 GB'], [10240, '10 GB'], [20480, '20 GB'], [51200, '50 GB']].map(([v, l]) => opt(v, l)));
    pick(cap, s.storage_cap_mb, ' MB');
    const every = el('select', 'wz-field');
    every.append(...[[30, 'Every 30 minutes'], [60, 'Every hour'], [180, 'Every 3 hours'], [720, 'Twice a day'], [1440, 'Once a day']].map(([v, l]) => opt(v, l)));
    pick(every, s.refresh_minutes, ' min');
    const auto = el('input'); auto.type = 'checkbox'; auto.checked = s.auto_download;
    const save = patch => api('api/settings', patch).then(v => { state.settings = v; }).catch(e => say(e.message, 'error'));
    cap.onchange = () => save({ storage_cap_mb: +cap.value });
    every.onchange = () => save({ refresh_minutes: +every.value });
    auto.onchange = () => save({ auto_download: auto.checked });
    const used = (st.storage_used_mb / 1024).toFixed(1) + ' GB used';
    const field = (label, control, note) => { const l = el('label', 'pc-field'); l.append(el('span', '', label), control); if (note) l.append(el('small', '', note)); return l; };
    const autoRow = el('label', 'pc-sheet-row pc-switch'); autoRow.append(el('span', '', 'Auto-download new shows'), auto);
    const file = el('input'); file.type = 'file'; file.accept = '.opml,.xml,text/xml,text/x-opml,application/xml'; file.className = 'wz-sr';
    const imp = el('label', 'pc-sheet-row', 'Import OPML'); imp.append(file);
    file.onchange = () => importOpml(file);
    const exp = el('a', 'pc-sheet-row', 'Export OPML'); exp.href = 'api/opml'; exp.download = 'podcasts.opml';
    const check = sheetRow('Check for new episodes', async () => {
      check.disabled = true; check.textContent = 'Checking…';
      try { const r = await api('api/refresh', {}); check.textContent = r.new_episodes ? r.new_episodes + ' new' : 'No new episodes'; }
      catch (e) { check.textContent = 'Check for new episodes'; say(e.message, 'error'); }
      finally { check.disabled = false; }
    });
    sheet('Settings', field('Storage', cap, used), field('Check feeds', every), autoRow, check, imp, exp);
  } catch (e) { say(e.message, 'error'); }
};
function pick(select, value, unit) {
  if (![...select.options].some(o => +o.value === +value)) select.add(new Option(value + unit, value));
  select.value = String(value);
}
async function importOpml(input) {
  const f = input.files[0];
  if (!f) return;
  if (f.size > 2 * 1048576) { say('That file is larger than 2 MB.', 'error'); return; }
  try {
    const r = await api('api/opml', { opml: await f.text() });
    closeSheet();
    toast(r.added + ' shows added');
  } catch (e) { say(e.message, 'error'); }
  finally { input.value = ''; }
}

// ------------------------------------------------------------------ player
function renderSpeeds() {
  const box = $('pl-speeds');
  box.replaceChildren(...SPEEDS.map(v => {
    const b = el('button', 'pc-speed', v + '×');
    b.type = 'button';
    b.setAttribute('aria-pressed', String(Math.abs(v - state.settings.speed) < 0.01));
    b.onclick = () => setSpeed(v);
    return b;
  }));
}
async function setSpeed(v) {
  state.settings.speed = v;
  audio.playbackRate = v;
  renderSpeeds();
  try { await api('api/settings', { speed: v }); } catch (e) { say(e.message, 'error'); }
}
function loadIntoPlayer(ep, autoplay) {
  if (state.current?.id !== ep.id) {
    state.current = ep;
    state.pendingSeek = ep.played ? 0 : ep.position || 0;
    state.savedAt = ep.position_at || 0;
    state.touched = false;  // a page that only shows this episode never overwrites a place saved elsewhere
    audio.src = ep.audio;
    audio.playbackRate = state.settings.speed;
    state.savedPos = state.pendingSeek;
  }
  $('mini').hidden = false;
  paintPlayer();
  mediaSession(ep);
  if (autoplay) {
    const p = audio.play();
    if (p) p.catch(playFailed);
  }
}
function playEpisode(ep) {
  try { localStorage.removeItem(MINI_KEY); } catch (_) {}
  if (state.current && state.current.id !== ep.id) saveProgress(true);  // reads the old place synchronously
  // Start inside the tap (mobile browsers need it), then catch up with a newer place saved on another device.
  loadIntoPlayer(ep, true);
  syncFromServer();
  markCurrentRow();
}
function markCurrentRow() {
  for (const n of document.querySelectorAll('.pc-ep.pc-current, .pc-card.pc-current')) n.classList.remove('pc-current');
  if (!state.current) return;
  const sel = state.current.preview ? '.pc-card[data-feed="' + CSS.escape(state.current.feed_url) + '"]' : '.pc-ep[data-id="' + state.current.id + '"]';
  for (const n of document.querySelectorAll(sel)) n.classList.add('pc-current');
}
function togglePlay() {
  if (!state.current) return;
  if (audio.paused) {
    // Start inside the tap (mobile browsers need it), then catch up with a newer place saved on another device.
    const p = audio.play(); if (p) p.catch(playFailed);
    syncFromServer();
  } else audio.pause();
}
async function syncFromServer() {
  const ep = state.current;
  if (!ep || ep.preview) return;
  try {
    const e = await api('api/episodes/' + ep.id);
    if (state.current?.id !== e.id || e.played || e.position_at <= (state.savedAt || 0)) return;
    if (Math.abs(e.position - audio.currentTime) > 3) {
      if (audio.readyState) audio.currentTime = e.position; else state.pendingSeek = e.position;
      state.savedPos = e.position; state.savedAt = e.position_at;
      toast('Picked up where you left off · ' + clock(e.position));
    }
  } catch (_) {}
}
// Close (X or swipe down): pause, save the place, hide the bar. The episode stays in the feed as started; the bar stays
// closed on this device until something is played (or another device moves that episode's place on).
const MINI_KEY = 'wizard-podcasts.mini-closed';
function miniClosed(np) {
  try {
    const c = JSON.parse(localStorage.getItem(MINI_KEY) || 'null');
    return !!c && c.id === np.id && (np.position_at || 0) <= c.at;
  } catch (_) { return false; }
}
async function closePlayer() {
  const ep = state.current;
  if (!ep) return;
  const save = ep.preview ? null : saveProgress(true);  // reads the place synchronously, before the audio is unloaded
  audio.pause();
  unloadPlayer();
  if (ep.preview) return;  // a preview leaves nothing behind
  await save;
  const at = Number.isFinite(state.savedAt) && state.savedAt > 0 ? state.savedAt : ep.position_at || 0;
  try { localStorage.setItem(MINI_KEY, JSON.stringify({ id: ep.id, at: Math.max(at, ep.position_at || 0) })); } catch (_) {}
  if (currentView() === 'feed') loadFeed().catch(() => {});
}
function unloadPlayer() {
  audio.pause();
  audio.removeAttribute('src'); audio.load();
  state.current = null;
  $('mini').hidden = true;
  markCurrentRow();
}
function skip(sec) {
  if (!state.current) return;
  const dur = isFinite(audio.duration) ? audio.duration : state.current.duration || Infinity;
  audio.currentTime = Math.max(0, Math.min(dur - 1, audio.currentTime + sec));
  saveProgress(true);
}
function duration() { return isFinite(audio.duration) && audio.duration > 0 ? audio.duration : state.current?.duration || 0; }
function paintPlayer() {
  const ep = state.current;
  if (!ep) return;
  const pos = audio.readyState ? audio.currentTime : state.pendingSeek || ep.position || 0;
  const dur = duration();
  const playing = !audio.paused;
  setText($('mini-title'), ep.title);
  setText($('mini-show'), showName(ep.show_id, ep.show_title));
  const left = dur ? minutes(dur - pos) + ' left' : '';
  setText($('mini-left'), left ? ' · ' + left : '');
  setText($('mini-time'), left);
  art($('mini-art'), ep.artwork);
  $('mini-progress').style.width = dur ? Math.min(100, 100 * pos / dur) + '%' : '0';
  // The play/pause icon is swapped only when the state changes: replacing it on every timeupdate (4x a second) pulled
  // the node out from under a finger mid-tap, so the tap was lost (0.4.0).
  setIcon($('mini-toggle'), playing ? 'pause' : 'play');
  $('mini-toggle').setAttribute('aria-label', playing ? 'Pause' : 'Play');
  if (currentView() === 'player') {
    art($('pl-art'), ep.artwork);
    $('pl-title').textContent = ep.title;
    $('pl-show').textContent = ep.show_title || '';
    $('pl-date').textContent = [postedFull(ep.published), ep.duration ? minutes(ep.duration) : ''].filter(Boolean).join(' · ');
    setIcon($('pl-toggle'), playing ? 'pause' : 'play');
    $('pl-toggle').setAttribute('aria-label', playing ? 'Pause' : 'Play');
    const seek = $('pl-seek');
    if (!seek.matches(':active')) { seek.max = String(Math.max(1, Math.floor(dur))); seek.value = String(Math.floor(pos)); }
    $('pl-pos').textContent = clock(pos);
    $('pl-left').textContent = '−' + clock(Math.max(0, dur - pos));
    $('pl-back').querySelector('span').textContent = '−' + state.settings.skip_back;
    $('pl-fwd').querySelector('span').textContent = '+' + state.settings.skip_forward;
    paintPlayerTaste();
  }
}
// The show's thumbs in the full player (a preview's show too); Mark played becomes Follow for a preview.
function playerShow() {
  const ep = state.current;
  if (!ep) return null;
  if (ep.preview) return { feed_url: ep.feed_url, title: ep.show_title, preview: true };
  return state.shows.find(s => s.id === ep.show_id) || null;
}
function paintPlayerTaste() {
  const s = playerShow();
  const v = s ? (tasteOf.get(s.feed_url) ?? s.taste ?? 0) : 0;
  $('pl-up').setAttribute('aria-pressed', String(v === 1));
  $('pl-down').setAttribute('aria-pressed', String(v === -1));
  $('pl-up').disabled = $('pl-down').disabled = !s;
  const pre = !!state.current?.preview;
  $('pl-mark').textContent = pre ? 'Follow' : 'Mark played';
  $('pl-show').disabled = $('pl-title').disabled = pre;
}
async function playerThumb(dir) {
  const s = playerShow();
  if (!s) return;
  const was = tasteOf.get(s.feed_url) ?? s.taste ?? 0;
  const v = was === dir ? 0 : dir;
  try { await setTaste(s.feed_url, v); } catch (e) { say(e.message, 'error'); return; }
  paintPlayerTaste();
  if (v === -1) {
    const undo = async () => { await setTaste(s.feed_url, was); paintPlayerTaste(); };
    toast('Less like this', undo, s.preview ? null : { label: 'Unfollow', run: () => unfollow(s) });
  }
}
$('pl-up').onclick = () => playerThumb(1);
$('pl-down').onclick = () => playerThumb(-1);
function setText(n, t) { if (n.textContent !== t) n.textContent = t; }
function setIcon(btn, name) { if (btn.dataset.icon === name) return; btn.dataset.icon = name; btn.replaceChildren(icon(name)); }
function renderPlayer() {
  if (!state.current) { popView(); return; }
  $('pl-mark').disabled = false;
  renderSpeeds();
  paintPlayer();
}
async function saveProgress(force, completed = false) {
  const ep = state.current;
  if (!ep || ep.preview || !state.touched || (!audio.readyState && !completed)) return;
  const pos = Math.floor(audio.currentTime * 10) / 10;
  if (!force && !completed && Math.abs(pos - state.savedPos) < 3) return;
  state.savedPos = pos; state.lastSaved = Date.now();
  const body = { position: pos, duration: duration() || undefined, completed };
  try { const e = await api('api/episodes/' + ep.id + '/progress', body); state.savedAt = Math.max(state.savedAt || 0, e.position_at); if (state.current?.id === e.id) Object.assign(state.current, { position: e.position, played: e.played }); }
  catch (_) { /* next tick retries */ }
}
function beaconProgress() {
  const ep = state.current;
  if (!ep || ep.preview || !state.touched || !audio.readyState) return;
  // keepalive lets the save finish while the page is being hidden or closed.
  const body = JSON.stringify({ position: Math.floor(audio.currentTime), duration: duration() || undefined });
  state.savedPos = audio.currentTime;
  try { fetch('api/episodes/' + ep.id + '/progress', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body, keepalive: true }).catch(() => {}); }
  catch (_) { saveProgress(true); }
}
function mediaSession(ep) {
  if (!('mediaSession' in navigator)) return;
  try {
    navigator.mediaSession.metadata = new MediaMetadata({ title: ep.title, artist: ep.show_title || '', album: 'Podcasts',
      artwork: ep.artwork ? [{ src: new URL(ep.artwork, location.href).href, sizes: '600x600' }] : [] });
    navigator.mediaSession.setActionHandler('play', () => audio.play());
    navigator.mediaSession.setActionHandler('pause', () => audio.pause());
    navigator.mediaSession.setActionHandler('seekbackward', () => skip(-state.settings.skip_back));
    navigator.mediaSession.setActionHandler('seekforward', () => skip(state.settings.skip_forward));
    navigator.mediaSession.setActionHandler('seekto', d => { if (d.seekTime != null) { audio.currentTime = d.seekTime; saveProgress(true); } });
  } catch (_) {}
}

audio.addEventListener('loadedmetadata', () => {
  if (state.pendingSeek != null) {
    const dur = duration();
    const target = dur && state.pendingSeek > dur - 5 ? 0 : state.pendingSeek;
    if (target > 0) audio.currentTime = target;
    state.pendingSeek = null;
  }
  audio.playbackRate = state.settings.speed;
  paintPlayer();
});
audio.addEventListener('timeupdate', () => {
  paintPlayer();
  if (Date.now() - state.lastSaved > 10000) saveProgress(false);
});
audio.addEventListener('play', () => {
  state.touched = true;
  audio.playbackRate = state.settings.speed;
  if (W && !state.playHold) state.playHold = W.update.hold('Playing a podcast');
  if (W && !state.ducked) { state.ducked = true; W.audio.duck(true); }
  paintPlayer(); paintEpisodePlay();
});
audio.addEventListener('pause', async () => {
  paintPlayer(); paintEpisodePlay();
  if (W && state.ducked) { state.ducked = false; W.audio.duck(false); }
  await saveProgress(true);
  if (state.playHold && audio.paused) { state.playHold.release(); state.playHold = null; }
});
// Seeks the owner makes (slider, skip, lock screen) save explicitly; restoring a saved place must not write it back.
audio.addEventListener('seeked', () => paintPlayer());
audio.addEventListener('ended', async () => {
  if (state.current?.preview) return;  // a preview never touches his feed
  await saveProgress(true, true);
  toast('Finished · marked played');
  if (currentView() === 'feed') loadFeed();
});
// A source that fails says why in plain words: the app's own answer (e.g. 'YouTube episodes need the YouTube Music
// app') when it has one, never the browser's 'no supported source'. The audio error event owns that message.
function playFailed(e) { if (e.name !== 'AbortError' && e.name !== 'NotSupportedError') say('Could not start playback: ' + e.message, 'error'); }
audio.addEventListener('error', async () => {
  const src = audio.getAttribute('src');
  if (!state.current || !src) return;
  let why = state.current.preview ? 'This preview could not be played.' : 'This episode could not be played. Try downloading it first.';
  try {
    const r = await fetch(src, { headers: { Range: 'bytes=0-0' } });
    if (!r.ok) { const j = await r.json().catch(() => null); if (j && typeof j.error === 'string' && j.error) why = j.error; }
  } catch (_) {}
  if (audio.getAttribute('src') === src) say(why, 'error');
});

// Mini player: the bar opens the full player; play/pause and close are their own 44px+ targets and never open it.
// Swipe down (touch) closes it like the X.
const mini = $('mini');
mini.addEventListener('click', e => {
  if (mini.__dragged) return;
  if (e.target.closest('#mini-toggle, #mini-close')) return;
  openView('player');
});
$('mini-toggle').addEventListener('click', e => { e.stopPropagation(); if (!mini.__dragged) togglePlay(); });
$('mini-close').addEventListener('click', e => { e.stopPropagation(); if (!mini.__dragged) closePlayer(); });
(() => {
  let id = null, x0 = 0, y0 = 0, dy = 0, mode = '';
  const reset = () => { mini.classList.remove('pc-mini-drag'); mini.style.removeProperty('--mdy'); };
  mini.addEventListener('pointerdown', e => {
    if (e.pointerType !== 'touch' || id !== null) return;
    id = e.pointerId; x0 = e.clientX; y0 = e.clientY; dy = 0; mode = '';
  });
  mini.addEventListener('pointermove', e => {
    if (e.pointerId !== id) return;
    const mx = e.clientX - x0, my = e.clientY - y0;
    if (!mode) {
      const intent = miniDragIntent(mx, my);
      if (intent === 'none') { id = null; return; }
      if (!intent) return;
      mode = 'drag'; mini.classList.add('pc-mini-drag'); try { mini.setPointerCapture(id); } catch (_) {}
    }
    dy = Math.max(0, my);
    mini.style.setProperty('--mdy', dy + 'px');
  });
  const end = e => {
    if (e.pointerId !== id) return;
    id = null;
    if (mode !== 'drag') return;
    mini.__dragged = true; setTimeout(() => { mini.__dragged = false; }, 350);
    if (e.type === 'pointerup' && dy > Math.min(48, mini.offsetHeight * 0.6)) { closePlayer(); setTimeout(reset, 50); }
    else reset();
  };
  mini.addEventListener('pointerup', end);
  mini.addEventListener('pointercancel', end);
})();
$('pl-toggle').onclick = togglePlay;
$('pl-back').onclick = () => skip(-state.settings.skip_back);
$('pl-fwd').onclick = () => skip(state.settings.skip_forward);
$('pl-seek').addEventListener('input', () => { $('pl-pos').textContent = clock(+$('pl-seek').value); });
$('pl-seek').addEventListener('change', () => { audio.currentTime = +$('pl-seek').value; saveProgress(true); });
$('pl-show').onclick = () => state.current && !state.current.preview && openView('show', state.current.show_id);
$('pl-title').onclick = () => state.current && !state.current.preview && openView('episode', state.current.id);
$('pl-mark').onclick = async () => {
  const ep = state.current;
  if (!ep) return;
  if (ep.preview) {  // Follow the show being previewed
    try { const r = await api('api/subscribe', { url: ep.feed_url }); toast('Following ' + r.show.title); $('pl-mark').disabled = true; }
    catch (e) { say(e.message, 'error'); }
    return;
  }
  try {
    audio.pause();
    await api('api/episodes/' + ep.id + '/mark', { played: true });
    const inbox = ep.in_inbox, pos = audio.currentTime;
    unloadPlayer();
    finish('Marked played', async () => { await api('api/episodes/' + ep.id + '/mark', { played: false, in_inbox: inbox }); await api('api/episodes/' + ep.id + '/progress', { position: pos }); reload(); });
  } catch (e) { say(e.message, 'error'); }
};

// ------------------------------------------------------------------ platform
if (!W) {
  say('Wizard system functions are unavailable. Open Podcasts through Wizard.', 'error');
} else {
  W.ready({ name: 'wizard-podcasts', version: '0.5.1' });
  // Domain Back: close the page on top (show, episode, player), then Discover -> My feed; at My feed the core decides.
  W.back.register({ canGoBack: () => state.stack.length > 0 || state.tab !== 'feed', back: () => popView() });
}
document.addEventListener('visibilitychange', async () => {
  if (document.hidden) { beaconProgress(); return; }
  // Back in front: pick up position changes made on another device while paused here.
  if (state.current && audio.paused) { await syncFromServer(); paintPlayer(); }
  if (currentView() !== 'discover') reload();
});
window.addEventListener('pagehide', beaconProgress);

(async function start() {
  try { state.settings = await api('api/settings'); } catch (_) {}
  const qs = new URLSearchParams(location.search);
  paint();
  try { await loadFeed(); } catch (e) { say(e.message, 'error'); }
  if (qs.get('episode')) openView('episode', +qs.get('episode'));
  else if (qs.get('show')) openView('show', +qs.get('show'));
  else if (['discover', 'add'].includes(qs.get('tab'))) setTab('discover');
})();
