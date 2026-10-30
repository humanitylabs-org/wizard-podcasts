'use strict';
/* Podcasts view (0.3): one feed of episodes from followed shows, show chips + one filter sheet on top, a find button,
 * and a mini player. Talks only to this app's documented relative API (api/...). Platform behaviour (Back root,
 * appearance, update holds, ducking Music) comes from the shared WizardApp client. */
const $ = id => document.getElementById(id);
const W = window.WizardApp && window.WizardApp.version === 1 ? window.WizardApp : null;
const audio = $('audio');
const SPEEDS = [0.8, 1, 1.2, 1.5, 1.75, 2, 2.5, 3];
const PAGE = 100;

const state = {
  stack: [],              // views above the feed: {view, id}
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
function toast(text, undo) {
  $('toast-text').textContent = text;
  const b = $('toast-undo');
  b.hidden = !undo;
  b.onclick = undo ? async () => { $('toast').hidden = true; try { await undo(); } catch (e) { say(e.message, 'error'); } } : null;
  $('toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, 5000);
}

// ------------------------------------------------------------------ formatting (always the viewer's local time)
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
const VIEWS = ['feed', 'add', 'show', 'episode', 'player'];
function currentView() { return state.stack.length ? state.stack.at(-1).view : 'feed'; }
function paint() {
  const v = currentView();
  for (const name of VIEWS) $('view-' + name).hidden = name !== v;
  $('top').hidden = v !== 'feed';
  $('nav-back').hidden = v === 'feed';
  document.querySelector('.pc-app').dataset.view = v;
  if (v !== 'feed') $('main').scrollTop = 0;
}
function openView(view, id) {
  if (state.stack.at(-1)?.view === view && state.stack.at(-1)?.id === id) return;
  if (!state.stack.length) feedScroll = $('main').scrollTop;
  state.stack.push({ view, id });
  paint();
  load(view, id);
}
let feedScroll = 0;
function popView() {
  if (!state.stack.length) return false;
  state.stack.pop();
  paint();
  const top = state.stack.at(-1);
  if (!top) { $('main').scrollTop = feedScroll; loadFeed(); } else load(top.view, top.id);
  return true;
}
$('nav-back').onclick = popView;
// A finishing action (mark played, unfollow) returns to the screen the owner came from: the previous view in the
// app, or, when he arrived straight here (deep link from a chat), the screen before the app.
function finish(message, undo) {
  if (popView()) { if (message) toast(message, undo); return; }
  if (W && W.nav.back()) return;
  if (message) toast(message, undo);
}
function reload() { const top = state.stack.at(-1); return load(top ? top.view : 'feed', top?.id); }

async function load(view, id) {
  try {
    if (view === 'feed') return await loadFeed();
    if (view === 'add') { $('search-input').focus({ preventScroll: true }); return; }
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
const FILTER_KEY = 'wizard-podcasts.feed-filter';
const filter = (() => {
  try {
    const f = JSON.parse(localStorage.getItem(FILTER_KEY) || '{}');
    return { show: Number.isInteger(f.show) ? f.show : null, state: ['started', 'played'].includes(f.state) ? f.state : 'unplayed', sort: f.sort === 'oldest' ? 'oldest' : 'newest' };
  } catch (_) { return { show: null, state: 'unplayed', sort: 'newest' }; }
})();
try { localStorage.removeItem('wizard-podcasts.upnext-filter'); } catch (_) {}  // the 0.2 filter, retired
function saveFilter() { try { localStorage.setItem(FILTER_KEY, JSON.stringify(filter)); } catch (_) {} }
function setFilter(change) {
  Object.assign(filter, change);
  saveFilter();
  $('main').scrollTop = 0;
  loadFeed();
}
function feedPath(offset = 0) {
  const qs = new URLSearchParams({ limit: String(PAGE) });
  if (filter.show != null) qs.set('show_id', filter.show);
  if (filter.state !== 'unplayed') qs.set('state', filter.state);
  if (filter.sort === 'oldest') qs.set('sort', 'oldest');
  if (offset) qs.set('offset', String(offset));
  return 'api/feed?' + qs;
}
$('open-filter').onclick = () => sheet('Filter',
  seg([['unplayed', 'Unplayed'], ['started', 'Started'], ['played', 'Played']], filter.state, v => setFilter({ state: v })),
  seg([['newest', 'Newest first'], ['oldest', 'Oldest first']], filter.sort, v => setFilter({ sort: v })));

// ------------------------------------------------------------------ show chips
let chipSig = '';
function chip(label, show) {
  const b = el('button', 'pc-chip' + (show ? '' : ' pc-chip-all'));
  b.type = 'button';
  if (show) {
    if (show.artwork) { const img = el('img', 'pc-chip-art'); img.alt = ''; img.loading = 'lazy'; img.src = show.artwork; b.append(img); }
    else { const ph = el('span', 'pc-chip-art pc-chip-ph', (label || '?').slice(0, 1)); ph.setAttribute('aria-hidden', 'true'); b.append(ph); }
    b.dataset.show = show.id;
    b.title = show.title;
    holdable(b, () => showSheet(show));
    b.onclick = () => { if (b.__held) { b.__held = false; return; } if (filter.show === show.id) showSheet(show); else setFilter({ show: show.id }); };
  } else {
    b.dataset.all = '1';
    b.onclick = () => setFilter({ show: null });
  }
  b.append(el('span', 'pc-chip-name', label));
  return b;
}
// Long-press (touch) or right-click (mouse) opens a chip's show actions.
function holdable(node, fn) {
  let timer = 0, x = 0, y = 0;
  const clear = () => { clearTimeout(timer); timer = 0; };
  node.addEventListener('pointerdown', e => {
    if (e.pointerType === 'mouse') return;
    x = e.clientX; y = e.clientY;
    timer = setTimeout(() => { timer = 0; node.__held = true; fn(); }, 500);
  });
  node.addEventListener('pointermove', e => { if (timer && Math.hypot(e.clientX - x, e.clientY - y) > 10) clear(); });
  for (const t of ['pointerup', 'pointercancel', 'pointerleave']) node.addEventListener(t, clear);
  node.addEventListener('contextmenu', e => { e.preventDefault(); if (!node.__held) fn(); });
}
function renderChips() {
  const row = $('chips');
  const sig = JSON.stringify(state.shows.map(s => [s.id, s.short_title, s.artwork]));
  if (sig !== chipSig) {
    const first = !chipSig;
    chipSig = sig;
    const left = row.scrollLeft;
    row.replaceChildren(chip('All', null), ...state.shows.map(s => chip(s.short_title || s.title, s)));
    row.scrollLeft = left;
    if (first) requestAnimationFrame(() => row.querySelector('[data-show="' + filter.show + '"]')?.scrollIntoView({ block: 'nearest', inline: 'center' }));
  }
  for (const b of row.children) b.setAttribute('aria-pressed', String(b.dataset.all ? filter.show == null : +b.dataset.show === filter.show));
  $('filter-dot').hidden = filter.state === 'unplayed' && filter.sort === 'newest';
}
// A mouse wheel scrolls the chip row sideways on desktop.
$('chips').addEventListener('wheel', e => {
  const row = $('chips');
  if (Math.abs(e.deltaY) > Math.abs(e.deltaX) && row.scrollWidth > row.clientWidth) { row.scrollLeft += e.deltaY; e.preventDefault(); }
}, { passive: false });

// One show's actions: its full episode list, auto-download, unfollow (second tap confirms).
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
  if (currentView() !== 'show') rows.push(sheetRow('All episodes', () => { closeSheet(); openView('show', show.id); }));
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

// ------------------------------------------------------------------ feed
let feedSeq = 0;
async function loadFeed(append = false) {
  const seq = ++feedSeq;
  const data = await api(feedPath(append ? state.feed.length : 0));
  if (seq !== feedSeq) return;
  state.shows = data.shows || [];
  // A remembered show that is no longer followed falls back to All.
  if (filter.show != null && !state.shows.some(s => s.id === filter.show)) { filter.show = null; saveFilter(); return loadFeed(); }
  state.feed = append ? state.feed.concat(data.episodes) : data.episodes;
  state.feedTotal = data.total;
  renderChips();
  renderFeed();
  if (data.now_playing && !state.current) loadIntoPlayer(data.now_playing, false);
}
$('feed-more').onclick = () => loadFeed(true).catch(e => say(e.message, 'error'));

function episodeRow(ep, { showShow = true } = {}) {
  const li = el('li', 'pc-ep' + (ep.played ? ' pc-played' : ''));
  li.dataset.id = ep.id;
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
  open.onclick = () => openView('episode', ep.id);
  li.append(open);
  return li;
}
// Episodes in quiet date sections (Today, Yesterday, weekdays, Last week, months) in the server's order, so
// Oldest first simply runs the sections the other way.
function renderFeed() {
  const now = new Date(), groups = [];
  for (const e of state.feed) {
    const g = dateGroup(e.published, now);
    if (groups.at(-1)?.key !== g.key) groups.push({ ...g, eps: [] });
    groups.at(-1).eps.push(e);
  }
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
  if (!empty) return;
  const go = $('feed-empty-go');
  go.hidden = true;
  if (!state.shows.length) {
    $('feed-empty-text').textContent = 'Follow a show to see its episodes here.';
    go.hidden = false; go.textContent = 'Find shows'; go.onclick = () => openView('add');
  } else if (filter.show != null) {
    $('feed-empty-text').textContent = filter.state === 'played' ? 'Nothing played yet.' : filter.state === 'started' ? 'Nothing started.' : 'Nothing new.';
    go.hidden = false; go.textContent = 'All episodes'; go.onclick = () => openView('show', filter.show);
  } else {
    $('feed-empty-text').textContent = filter.state === 'played' ? 'Nothing played yet.' : filter.state === 'started' ? 'Nothing started.' : 'All caught up.';
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

// ------------------------------------------------------------------ find shows (podcasts and YouTube channels, one list)
let searchSeq = 0, searchTimer = 0;
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
    $('search-results').replaceChildren(...merged.map(resultRow));
    note.hidden = merged.length > 0;
    note.textContent = merged.length ? '' : 'No shows found.';
  } catch (e) { if (seq === searchSeq) { note.hidden = true; say(e.message, 'error'); } }
}
$('search-input').addEventListener('input', () => {
  clearTimeout(searchTimer);
  const text = $('search-input').value.trim();
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
    $('search-input').value = '';
    toast(r.already_subscribed ? 'Already following ' + r.show.title : 'Following ' + r.show.title);
    openView('show', r.show.id);
  } catch (e) { say(e.message, 'error'); }
};
function resultRow(item) {
  const li = el('li', 'pc-ep pc-result');
  const main = el('button', 'wz-row pc-result-open');
  main.type = 'button';
  const img = el('img', 'pc-art pc-art-row'); img.alt = ''; img.loading = 'lazy';
  art(img, item.artwork);
  const text = el('span', 'wz-main');
  const sub = item.source === 'youtube' ? [item.author, item.subscribers] : [item.author];
  text.append(el('b', 'pc-clamp', item.title), el('small', '', sub.filter(Boolean).join(' · ')));
  main.append(img, text);
  const btn = el('button', 'pc-follow');
  btn.type = 'button';
  let showId = item.show_id || null;
  const paintBtn = on => {
    btn.textContent = on ? 'Episodes' : 'Follow';
    btn.dataset.followed = String(on);
    main.disabled = !on;
    main.setAttribute('aria-label', on ? 'Open ' + item.title : item.title);
  };
  const openShow = () => { if (showId) openView('show', showId); };
  main.onclick = openShow;
  paintBtn(!!showId);
  btn.onclick = async () => {
    if (showId) { openShow(); return; }
    btn.disabled = true;
    try {
      const r = await api('api/subscribe', { url: item.feed_url });
      showId = r.show.id;
      paintBtn(true);
      openShow();
    } catch (e) { say(e.message, 'error'); }
    finally { btn.disabled = false; }
  };
  li.append(main, btn);
  return li;
}
$('open-add').onclick = () => openView('add');

// ------------------------------------------------------------------ settings (from the find screen)
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
    if (p) p.catch(e => { if (e.name !== 'AbortError') say('Could not start playback: ' + e.message, 'error'); });
  }
}
async function playEpisode(ep) {
  if (state.current && state.current.id !== ep.id) await saveProgress(true);
  // Fresh position from the server (another device may have moved it).
  try { ep = await api('api/episodes/' + ep.id); } catch (_) {}
  loadIntoPlayer(ep, true);
}
function togglePlay() {
  if (!state.current) return;
  if (audio.paused) {
    // Start inside the tap (mobile browsers need it), then catch up with a newer place saved on another device.
    const p = audio.play(); if (p) p.catch(e => say('Could not start playback: ' + e.message, 'error'));
    syncFromServer();
  } else audio.pause();
}
async function syncFromServer() {
  const ep = state.current;
  if (!ep) return;
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
function unloadPlayer() {
  audio.pause();
  audio.removeAttribute('src'); audio.load();
  state.current = null;
  $('mini').hidden = true;
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
  $('mini-title').textContent = ep.title;
  $('mini-sub').textContent = showName(ep.show_id, ep.show_title) + (dur ? ' · ' + minutes(dur - pos) + ' left' : '');
  art($('mini-art'), ep.artwork);
  $('mini-progress').style.width = dur ? Math.min(100, 100 * pos / dur) + '%' : '0';
  $('mini-toggle').replaceChildren(icon(playing ? 'pause' : 'play'));
  $('mini-toggle').setAttribute('aria-label', playing ? 'Pause' : 'Play');
  if (currentView() === 'player') {
    art($('pl-art'), ep.artwork);
    $('pl-title').textContent = ep.title;
    $('pl-show').textContent = ep.show_title || '';
    $('pl-date').textContent = [postedFull(ep.published), ep.duration ? minutes(ep.duration) : ''].filter(Boolean).join(' · ');
    $('pl-toggle').replaceChildren(icon(playing ? 'pause' : 'play'));
    $('pl-toggle').setAttribute('aria-label', playing ? 'Pause' : 'Play');
    const seek = $('pl-seek');
    if (!seek.matches(':active')) { seek.max = String(Math.max(1, Math.floor(dur))); seek.value = String(Math.floor(pos)); }
    $('pl-pos').textContent = clock(pos);
    $('pl-left').textContent = '−' + clock(Math.max(0, dur - pos));
    $('pl-back').querySelector('span').textContent = '−' + state.settings.skip_back;
    $('pl-fwd').querySelector('span').textContent = '+' + state.settings.skip_forward;
  }
}
function renderPlayer() {
  if (!state.current) { popView(); return; }
  renderSpeeds();
  paintPlayer();
}
async function saveProgress(force, completed = false) {
  const ep = state.current;
  if (!ep || !state.touched || (!audio.readyState && !completed)) return;
  const pos = Math.floor(audio.currentTime * 10) / 10;
  if (!force && !completed && Math.abs(pos - state.savedPos) < 3) return;
  state.savedPos = pos; state.lastSaved = Date.now();
  const body = { position: pos, duration: duration() || undefined, completed };
  try { const e = await api('api/episodes/' + ep.id + '/progress', body); state.savedAt = Math.max(state.savedAt || 0, e.position_at); if (state.current?.id === e.id) Object.assign(state.current, { position: e.position, played: e.played }); }
  catch (_) { /* next tick retries */ }
}
function beaconProgress() {
  const ep = state.current;
  if (!ep || !state.touched || !audio.readyState) return;
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
  await saveProgress(true, true);
  toast('Finished · marked played');
  if (currentView() === 'feed') loadFeed();
});
audio.addEventListener('error', () => {
  if (state.current && audio.getAttribute('src')) say('This episode could not be played. Try downloading it first.', 'error');
});

$('mini-open').onclick = () => openView('player');
$('mini-toggle').onclick = togglePlay;
$('pl-toggle').onclick = togglePlay;
$('pl-back').onclick = () => skip(-state.settings.skip_back);
$('pl-fwd').onclick = () => skip(state.settings.skip_forward);
$('pl-seek').addEventListener('input', () => { $('pl-pos').textContent = clock(+$('pl-seek').value); });
$('pl-seek').addEventListener('change', () => { audio.currentTime = +$('pl-seek').value; saveProgress(true); });
$('pl-show').onclick = () => state.current && openView('show', state.current.show_id);
$('pl-title').onclick = () => state.current && openView('episode', state.current.id);
$('pl-mark').onclick = async () => {
  const ep = state.current;
  if (!ep) return;
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
  W.ready({ name: 'wizard-podcasts', version: '0.3.1' });
  // Domain Back: close the nested view (find, show, episode, player); at the feed, the core decides.
  W.back.register({ canGoBack: () => state.stack.length > 0, back: () => popView() });
}
document.addEventListener('visibilitychange', async () => {
  if (document.hidden) { beaconProgress(); return; }
  // Back in front: pick up position changes made on another device while paused here.
  if (state.current && audio.paused) { await syncFromServer(); paintPlayer(); }
  if (currentView() !== 'add') reload();
});
window.addEventListener('pagehide', beaconProgress);

(async function start() {
  try { state.settings = await api('api/settings'); } catch (_) {}
  const qs = new URLSearchParams(location.search);
  paint();
  try { await loadFeed(); } catch (e) { say(e.message, 'error'); }
  if (qs.get('episode')) openView('episode', +qs.get('episode'));
  else if (qs.get('show')) openView('show', +qs.get('show'));
  else if (qs.get('tab') === 'add') openView('add');
})();
