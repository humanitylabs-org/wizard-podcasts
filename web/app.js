'use strict';
/* Podcasts view. Talks only to this app's documented relative API (api/...). Platform behaviour (Back root,
 * appearance, update holds, ducking Music) comes from the shared WizardApp client. */
const $ = id => document.getElementById(id);
const W = window.WizardApp && window.WizardApp.version === 1 ? window.WizardApp : null;
const audio = $('audio');
const SPEEDS = [0.8, 1, 1.2, 1.5, 1.75, 2, 2.5, 3];

const state = {
  tab: 'inbox',
  stack: [],              // nested views above the current tab: {view, id}
  settings: { speed: 1, skip_back: 15, skip_forward: 30, storage_cap_mb: 2048, refresh_minutes: 60, auto_download: true },
  show: null, showEpisodes: [], showOffset: 0,
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

// ------------------------------------------------------------------ formatting
function clock(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(s).padStart(2, '0');
}
function minutes(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  if (sec < 60) return sec + ' sec';
  const h = Math.floor(sec / 3600), m = Math.round(sec % 3600 / 60);
  return h ? h + ' h ' + (m ? m + ' min' : '') : m + ' min';
}
function day(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000), now = new Date();
  const opts = d.getFullYear() === now.getFullYear() ? { month: 'short', day: 'numeric' } : { month: 'short', day: 'numeric', year: 'numeric' };
  return d.toLocaleDateString(undefined, opts);
}
function episodeMeta(ep) {
  const bits = [];
  if (ep.show_title && state.stack.at(-1)?.view !== 'show') bits.push(ep.show_title);
  if (ep.published) bits.push(day(ep.published));
  if (ep.played) bits.push('Played');
  else if (ep.position > 0 && ep.duration) bits.push(minutes(ep.duration - ep.position) + ' left');
  else if (ep.duration) bits.push(minutes(ep.duration));
  const d = ep.download || {};
  if (d.state === 'downloading') bits.push('Downloading' + (d.size_bytes ? ' ' + Math.round(100 * d.progress_bytes / d.size_bytes) + '%' : '…'));
  else if (d.state === 'queued') bits.push('Waiting to download');
  else if (d.state === 'failed') bits.push('Download failed');
  return bits.join(' · ');
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
    check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    dl: '<path d="M12 4v11M7 10.5l5 5 5-5M5 20h14"/>',
  };
  const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('aria-hidden', 'true');
  s.innerHTML = paths[name];
  return s;
}
function art(img, url) {
  if (url) { img.src = url; img.hidden = false; } else { img.removeAttribute('src'); img.hidden = true; }
}

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
const VIEWS = ['inbox', 'shows', 'add', 'show', 'episode', 'player'];
function currentView() { return state.stack.length ? state.stack.at(-1).view : state.tab; }
function paint() {
  const v = currentView();
  for (const name of VIEWS) $('view-' + name).hidden = name !== v;
  for (const t of document.querySelectorAll('.pc-tab')) {
    if (t.dataset.tab === state.tab) t.setAttribute('aria-current', 'page'); else t.removeAttribute('aria-current');
  }
  document.querySelector('.pc-app').dataset.view = v;
  $('main').scrollTop = 0;
}
function setTab(tab) {
  state.tab = tab; state.stack = [];
  paint();
  load(tab);
}
function openView(view, id) {
  if (state.stack.at(-1)?.view === view && state.stack.at(-1)?.id === id) return;
  state.stack.push({ view, id });
  paint();
  load(view, id);
}
function popView() {
  if (!state.stack.length) return false;
  state.stack.pop();
  paint();
  const top = state.stack.at(-1);
  load(top ? top.view : state.tab, top?.id);
  return true;
}
// A finishing action (mark played, unfollow) returns to the screen the owner came from: the previous view in the
// app, or, when he arrived straight here (deep link from a chat), the screen before the app.
function finish(message, undo) {
  if (popView()) { if (message) toast(message, undo); return; }
  if (W && W.nav.back()) return;
  if (message) toast(message, undo);
}

async function load(view, id) {
  try {
    if (view === 'inbox') return renderInbox(await api('api/inbox'));
    if (view === 'shows') return renderShows(await api('api/shows'));
    if (view === 'add') return setTimeout(() => $('search-input').focus({ preventScroll: true }), 50);
    if (view === 'show') return renderShow(await api('api/shows/' + id + '?limit=50'));
    if (view === 'episode') return renderEpisode(await api('api/episodes/' + id));
    if (view === 'player') return renderPlayer();
  } catch (e) { say(e.message, 'error'); }
}

// ------------------------------------------------------------------ rows
function episodeRow(ep, { onDone } = {}) {
  const li = el('li', 'pc-ep');
  li.dataset.id = ep.id;
  const open = el('button', 'wz-row pc-ep-open');
  open.type = 'button';
  const img = el('img', 'pc-art pc-art-row'); img.alt = ''; img.loading = 'lazy';
  art(img, ep.artwork);
  const main = el('span', 'wz-main');
  main.append(el('b', 'pc-clamp', ep.title), el('small', '', episodeMeta(ep)));
  if (ep.position > 0 && ep.duration && !ep.played) {
    const bar = el('span', 'pc-progress'); const fill = el('i');
    fill.style.width = Math.min(100, 100 * ep.position / ep.duration) + '%';
    bar.append(fill); main.append(bar);
  }
  open.append(img, main);
  open.onclick = () => openView('episode', ep.id);
  const play = el('button', 'pc-icon pc-icon-accent');
  play.type = 'button';
  const playing = state.current?.id === ep.id && !audio.paused;
  play.append(icon(playing ? 'pause' : 'play'));
  play.setAttribute('aria-label', (playing ? 'Pause ' : 'Play ') + ep.title);
  play.onclick = () => (state.current?.id === ep.id ? togglePlay() : playEpisode(ep));
  li.append(open, play);
  if (!ep.played) {
    const done = el('button', 'pc-icon');
    done.type = 'button';
    done.append(icon('check'));
    done.setAttribute('aria-label', 'Mark played: ' + ep.title);
    done.onclick = async () => {
      done.disabled = true;
      try {
        await api('api/episodes/' + ep.id + '/mark', { played: true });
        li.remove();
        if (onDone) onDone();
        toast('Marked played', async () => { await api('api/episodes/' + ep.id + '/mark', { played: false, in_inbox: ep.in_inbox }); load(currentView(), state.stack.at(-1)?.id); });
      } catch (e) { done.disabled = false; say(e.message, 'error'); }
    };
    li.append(done);
  }
  return li;
}

// ------------------------------------------------------------------ inbox
let pollTimer = 0;
function pollIfBusy(list) {
  clearTimeout(pollTimer);
  if (list.some(e => ['queued', 'downloading'].includes(e.download?.state))) {
    pollTimer = setTimeout(() => { if (!document.hidden) load(currentView(), state.stack.at(-1)?.id); }, 4000);
  }
}
function renderInbox(data) {
  const cont = data.continue || [], fresh = data.new || [];
  $('list-continue').replaceChildren(...cont.map(e => episodeRow(e)));
  const recount = () => { const n = $('list-new').children.length; $('new-count').textContent = n; $('inbox-new').hidden = !n; badge(n); emptyCheck(); };
  $('list-new').replaceChildren(...fresh.map(e => episodeRow(e, { onDone: recount })));
  $('inbox-continue').hidden = !cont.length;
  $('inbox-new').hidden = !fresh.length;
  $('new-count').textContent = fresh.length;
  badge(fresh.length);
  const emptyCheck = () => { $('inbox-empty').hidden = !!($('list-continue').children.length || $('list-new').children.length); };
  emptyCheck();
  if (data.now_playing && !state.current) loadIntoPlayer(data.now_playing, false);
  pollIfBusy(cont.concat(fresh));
}
function badge(n) { $('tab-badge').hidden = !n; $('tab-badge').textContent = n > 99 ? '99+' : String(n); }

// ------------------------------------------------------------------ shows
function renderShows(data) {
  const grid = $('shows-grid');
  grid.replaceChildren();
  for (const s of data.shows) {
    const b = el('button', 'pc-show');
    b.type = 'button';
    const img = el('img', 'pc-art pc-art-tile'); img.alt = ''; img.loading = 'lazy';
    art(img, s.artwork);
    if (!s.artwork) { const ph = el('span', 'pc-art pc-art-tile pc-art-ph', (s.title || '?').slice(0, 1)); ph.setAttribute('aria-hidden', 'true'); b.append(ph); }
    else b.append(img);
    b.append(el('b', 'pc-clamp', s.title));
    b.append(el('small', '', s.pending ? 'Loading…' : s.last_error ? 'Feed problem' : s.new ? s.new + ' new' : s.episodes + ' episodes'));
    b.onclick = () => openView('show', s.id);
    grid.append(b);
  }
  $('shows-empty').hidden = data.shows.length > 0;
  if (data.shows.some(s => s.pending)) { clearTimeout(pollTimer); pollTimer = setTimeout(() => { if (currentView() === 'shows') load('shows'); }, 4000); }
  api('api/status').then(st => {
    $('storage-note').textContent = st.storage_used_mb + ' MB of ' + (st.storage_cap_mb / 1024).toFixed(1).replace('.0', '') + ' GB used · ' + st.downloaded + ' downloaded';
    $('refresh-note').textContent = st.last_refresh ? 'Last checked ' + new Date(st.last_refresh * 1000).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }) : 'Checks on its own';
  }).catch(() => {});
}

function renderShow(data, append = false) {
  const s = data.show;
  state.show = s;
  if (!append) { state.showEpisodes = []; }
  state.showEpisodes = state.showEpisodes.concat(data.episodes);
  state.showOffset = state.showEpisodes.length;
  art($('show-art'), s.artwork);
  $('show-title').textContent = s.title;
  $('show-meta').textContent = [s.author, s.episodes + ' episodes', s.new ? s.new + ' new' : ''].filter(Boolean).join(' · ');
  $('show-auto').setAttribute('aria-pressed', String(s.auto_download));
  $('show-auto').textContent = s.auto_download ? 'Auto-download on' : 'Auto-download off';
  $('show-error').hidden = !s.last_error;
  $('show-error').textContent = s.last_error ? 'Last check failed: ' + s.last_error : '';
  $('show-about').hidden = !s.description_html;
  renderNotes($('show-desc'), s.description_html);
  const list = $('show-episodes');
  if (!append) list.replaceChildren();
  for (const e of data.episodes) list.append(episodeRow(e));
  $('show-more').hidden = state.showOffset >= s.episodes;
  pollIfBusy(data.episodes);
}
$('show-more').onclick = async () => {
  try { renderShow(await api('api/shows/' + state.show.id + '?limit=50&offset=' + state.showOffset), true); } catch (e) { say(e.message, 'error'); }
};
$('show-auto').onclick = async () => {
  const on = $('show-auto').getAttribute('aria-pressed') !== 'true';
  try { const d = await api('api/shows/' + state.show.id, { auto_download: on }); state.show.auto_download = d.show.auto_download; $('show-auto').setAttribute('aria-pressed', String(d.show.auto_download)); $('show-auto').textContent = d.show.auto_download ? 'Auto-download on' : 'Auto-download off'; }
  catch (e) { say(e.message, 'error'); }
};
$('show-refresh').onclick = async () => {
  const b = $('show-refresh'); b.disabled = true; b.textContent = 'Checking…';
  try {
    const r = await api('api/shows/' + state.show.id + '/refresh', {});
    toast(r.ok ? (r.new_episodes ? r.new_episodes + ' new episode' + (r.new_episodes > 1 ? 's' : '') : 'No new episodes') : r.error);
    renderShow(await api('api/shows/' + state.show.id + '?limit=50'));
  } catch (e) { say(e.message, 'error'); }
  finally { b.disabled = false; b.textContent = 'Check now'; }
};
$('show-unsub').onclick = () => {
  $('unsub-text').textContent = '“' + state.show.title + '” and its downloaded episodes will be removed from this Wizard.';
  WzSheet.open($('unsub-sheet'));
};
$('unsub-no').onclick = () => WzSheet.close($('unsub-sheet'));
$('unsub-yes').onclick = async () => {
  const s = state.show;
  $('unsub-yes').disabled = true;
  try {
    await api('api/shows/' + s.id + '/unsubscribe', {});
    WzSheet.close($('unsub-sheet'));
    if (state.current?.show_id === s.id) unloadPlayer();
    finish('Unfollowed ' + s.title, async () => { await api('api/subscribe', { url: s.feed_url, auto_download: s.auto_download }); load(currentView(), state.stack.at(-1)?.id); });
  } catch (e) { say(e.message, 'error'); }
  finally { $('unsub-yes').disabled = false; }
};

// ------------------------------------------------------------------ episode
function renderEpisode(ep) {
  state.episode = ep;
  art($('ep-art'), ep.artwork);
  $('ep-show').textContent = ep.show_title || '';
  $('ep-show').onclick = () => openView('show', ep.show_id);
  $('ep-title').textContent = ep.title;
  $('ep-meta').textContent = [day(ep.published), ep.duration ? minutes(ep.duration) : '', ep.played ? 'Played' : ''].filter(Boolean).join(' · ');
  const isCurrent = state.current?.id === ep.id;
  const playLabel = isCurrent && !audio.paused ? 'Pause' : ep.position > 0 && !ep.played ? 'Resume from ' + clock(ep.position) : 'Play';
  $('ep-play').replaceChildren(icon(isCurrent && !audio.paused ? 'pause' : 'play'), el('span', '', playLabel));
  $('ep-play').onclick = () => (isCurrent ? togglePlay() : playEpisode(ep));
  const d = ep.download;
  const dl = $('ep-download');
  dl.disabled = false;
  if (d.state === 'done') { dl.textContent = 'Remove download'; dl.onclick = () => episodeAction(ep, 'delete-download', 'Download removed'); }
  else if (d.state === 'queued' || d.state === 'downloading') { dl.textContent = 'Stop download'; dl.onclick = () => episodeAction(ep, 'delete-download', 'Download stopped'); }
  else { dl.textContent = d.state === 'failed' ? 'Try download again' : 'Download'; dl.onclick = () => episodeAction(ep, 'download', 'Downloading'); }
  const note = d.state === 'done' ? 'Saved on your Wizard · ' + Math.round(d.size_bytes / 1048576) + ' MB'
    : d.state === 'downloading' ? 'Downloading' + (d.size_bytes ? ' ' + Math.round(100 * d.progress_bytes / d.size_bytes) + '%' : '…')
    : d.state === 'queued' ? 'Waiting to download' : d.state === 'failed' ? (d.error || 'Download failed') : 'Plays straight from the publisher until downloaded';
  $('ep-dl-note').hidden = false; $('ep-dl-note').textContent = note;
  $('ep-mark').textContent = ep.played ? 'Mark unplayed' : 'Mark played';
  $('ep-mark').onclick = async () => {
    try {
      const was = ep.played, inbox = ep.in_inbox;
      await api('api/episodes/' + ep.id + '/mark', { played: !was });
      if (!was) { if (state.current?.id === ep.id && !audio.paused) audio.pause(); finish('Marked played', async () => { await api('api/episodes/' + ep.id + '/mark', { played: false, in_inbox: inbox }); load(currentView(), state.stack.at(-1)?.id); }); }
      else renderEpisode(await api('api/episodes/' + ep.id));
    } catch (e) { say(e.message, 'error'); }
  };
  renderNotes($('ep-notes'), ep.notes_html);
  pollIfBusy([ep]);
}
async function episodeAction(ep, action, message) {
  $('ep-download').disabled = true;
  try { renderEpisode(await api('api/episodes/' + ep.id + '/' + action, {})); toast(message); }
  catch (e) { say(e.message, 'error'); $('ep-download').disabled = false; }
}

// ------------------------------------------------------------------ search / add
$('search-form').onsubmit = async event => {
  event.preventDefault();
  const text = $('search-input').value.trim();
  if (!text) return;
  const list = $('search-results');
  $('search-go').disabled = true;
  try {
    if (/^https?:\/\//i.test(text)) {
      say('Loading feed…', 'note');
      const r = await api('api/subscribe', { url: text });
      say('');
      $('search-input').value = '';
      toast(r.already_subscribed ? 'Already following ' + r.show.title : 'Following ' + r.show.title);
      openView('show', r.show.id);
      return;
    }
    $('search-note').textContent = 'Searching…';
    const r = await api('api/search?q=' + encodeURIComponent(text));
    list.replaceChildren();
    for (const item of r.results) list.append(resultRow(item));
    $('search-note').textContent = r.results.length ? r.results.length + ' shows from the Apple Podcasts directory' : 'No shows found. Try another name, or paste the feed address.';
  } catch (e) { $('search-note').textContent = ''; say(e.message, 'error'); }
  finally { $('search-go').disabled = false; }
};
function resultRow(item) {
  const li = el('li', 'pc-ep');
  const main = el('div', 'wz-row pc-result');
  const img = el('img', 'pc-art pc-art-row'); img.alt = ''; img.loading = 'lazy';
  art(img, item.artwork);
  const text = el('span', 'wz-main');
  text.append(el('b', 'pc-clamp', item.title), el('small', '', [item.author, item.genre].filter(Boolean).join(' · ')));
  main.append(img, text);
  const btn = el('button', item.subscribed ? 'wz-button secondary pc-follow' : 'wz-button pc-follow', item.subscribed ? 'Following' : 'Follow');
  btn.type = 'button';
  btn.disabled = item.subscribed;
  btn.onclick = async () => {
    btn.disabled = true; btn.textContent = 'Adding…';
    try {
      const r = await api('api/subscribe', { url: item.feed_url });
      btn.textContent = 'Following'; btn.className = 'wz-button secondary pc-follow';
      toast('Following ' + r.show.title + (r.show.new ? ' · newest episode is in Up next' : ''));
    } catch (e) { btn.disabled = false; btn.textContent = 'Follow'; say(e.message, 'error'); }
  };
  li.append(main, btn);
  return li;
}
$('opml-file').onchange = async () => {
  const f = $('opml-file').files[0];
  if (!f) return;
  if (f.size > 2 * 1048576) { say('That file is larger than 2 MB.', 'error'); return; }
  try {
    const r = await api('api/opml', { opml: await f.text() });
    toast('Found ' + r.found + ' shows · ' + r.added + ' added');
    setTab('shows');
  } catch (e) { say(e.message, 'error'); }
  finally { $('opml-file').value = ''; }
};

// ------------------------------------------------------------------ settings
$('open-settings').onclick = async () => {
  try {
    const [s, st] = await Promise.all([api('api/settings'), api('api/status')]);
    state.settings = s;
    pickOption($('set-cap'), s.storage_cap_mb);
    pickOption($('set-refresh'), s.refresh_minutes);
    $('set-auto').checked = s.auto_download;
    $('set-used').textContent = st.storage_used_mb + ' MB used by ' + st.downloaded + ' downloaded episodes.';
    WzSheet.open($('settings-sheet'));
  } catch (e) { say(e.message, 'error'); }
};
function pickOption(select, value) {
  if (![...select.options].some(o => +o.value === +value)) select.add(new Option(value + (select.id === 'set-cap' ? ' MB' : ' min'), value));
  select.value = String(value);
}
$('settings-form').onsubmit = async event => {
  event.preventDefault();
  try {
    state.settings = await api('api/settings', { storage_cap_mb: +$('set-cap').value, refresh_minutes: +$('set-refresh').value, auto_download: $('set-auto').checked });
    WzSheet.close($('settings-sheet'));
    toast('Settings saved');
    if (currentView() === 'shows') load('shows');
  } catch (e) { say(e.message, 'error'); }
};
$('refresh-all').onclick = async () => {
  $('refresh-note').textContent = 'Checking…';
  try { const r = await api('api/refresh', {}); toast(r.new_episodes ? r.new_episodes + ' new episodes' : 'No new episodes'); load('shows'); }
  catch (e) { say(e.message, 'error'); }
};

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
      toast('Picked up where you left off on another device · ' + clock(e.position));
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
  $('mini-sub').textContent = (ep.show_title || '') + (dur ? ' · ' + minutes(dur - pos) + ' left' : '');
  art($('mini-art'), ep.artwork);
  $('mini-progress').style.width = dur ? Math.min(100, 100 * pos / dur) + '%' : '0';
  $('mini-toggle').replaceChildren(icon(playing ? 'pause' : 'play'));
  $('mini-toggle').setAttribute('aria-label', playing ? 'Pause' : 'Play');
  if (currentView() === 'player') {
    art($('pl-art'), ep.artwork);
    $('pl-title').textContent = ep.title;
    $('pl-show').textContent = ep.show_title || '';
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
  paintPlayer(); refreshPlayButtons();
});
audio.addEventListener('pause', async () => {
  paintPlayer(); refreshPlayButtons();
  if (W && state.ducked) { state.ducked = false; W.audio.duck(false); }
  await saveProgress(true);
  if (state.playHold && audio.paused) { state.playHold.release(); state.playHold = null; }
});
// Seeks the owner makes (slider, skip, lock screen) save explicitly; restoring a saved place must not write it back.
audio.addEventListener('seeked', () => paintPlayer());
audio.addEventListener('ended', async () => {
  await saveProgress(true, true);
  toast('Finished · marked played');
  if (currentView() === 'inbox') load('inbox');
});
audio.addEventListener('error', () => {
  if (state.current && audio.getAttribute('src')) say('This episode could not be played. Try downloading it first.', 'error');
});
function refreshPlayButtons() {
  for (const li of document.querySelectorAll('.pc-ep[data-id]')) {
    const b = li.querySelector('.pc-icon-accent');
    if (!b) continue;
    const on = state.current?.id === +li.dataset.id && !audio.paused;
    b.replaceChildren(icon(on ? 'pause' : 'play'));
  }
  if (currentView() === 'episode' && state.episode) {
    const on = state.current?.id === state.episode.id && !audio.paused;
    const label = on ? 'Pause' : (state.current?.id === state.episode.id ? 'Resume from ' + clock(audio.currentTime) : $('ep-play').textContent);
    $('ep-play').replaceChildren(icon(on ? 'pause' : 'play'), el('span', '', label));
  }
}

$('mini-open').onclick = () => openView('player');
$('mini-toggle').onclick = togglePlay;
$('mini-fwd').onclick = () => skip(state.settings.skip_forward);
$('pl-toggle').onclick = togglePlay;
$('pl-back').onclick = () => skip(-state.settings.skip_back);
$('pl-fwd').onclick = () => skip(state.settings.skip_forward);
$('pl-seek').addEventListener('input', () => { $('pl-pos').textContent = clock(+$('pl-seek').value); });
$('pl-seek').addEventListener('change', () => { audio.currentTime = +$('pl-seek').value; saveProgress(true); });
$('pl-show').onclick = () => state.current && openView('show', state.current.show_id);
$('pl-notes').onclick = () => state.current && openView('episode', state.current.id);
$('pl-mark').onclick = async () => {
  const ep = state.current;
  if (!ep) return;
  try {
    audio.pause();
    await api('api/episodes/' + ep.id + '/mark', { played: true });
    const inbox = ep.in_inbox, pos = audio.currentTime;
    unloadPlayer();
    finish('Marked played', async () => { await api('api/episodes/' + ep.id + '/mark', { played: false, in_inbox: inbox }); await api('api/episodes/' + ep.id + '/progress', { position: pos }); load(currentView(), state.stack.at(-1)?.id); });
  } catch (e) { say(e.message, 'error'); }
};

for (const t of document.querySelectorAll('.pc-tab')) t.onclick = () => setTab(t.dataset.tab);
for (const b of document.querySelectorAll('[data-go]')) b.onclick = () => setTab(b.dataset.go);

// ------------------------------------------------------------------ platform
if (!W) {
  say('Wizard system functions are unavailable. Open Podcasts through Wizard.', 'error');
} else {
  W.ready({ name: 'wizard-podcasts', version: '0.1.2' });
  // Domain Back: close the nested view (player, episode, show); at a tab root, the core decides.
  W.back.register({ canGoBack: () => state.stack.length > 0, back: () => popView() });
}
document.addEventListener('visibilitychange', async () => {
  if (document.hidden) { beaconProgress(); return; }
  // Back in front: pick up position changes made on another device while paused here.
  if (state.current && audio.paused) { await syncFromServer(); paintPlayer(); }
  load(currentView(), state.stack.at(-1)?.id);
});
window.addEventListener('pagehide', beaconProgress);

(async function start() {
  try { state.settings = await api('api/settings'); } catch (_) {}
  const qs = new URLSearchParams(location.search);
  paint();
  await load('inbox');
  if (qs.get('episode')) openView('episode', +qs.get('episode'));
  else if (qs.get('show')) openView('show', +qs.get('show'));
  else if (qs.get('tab') && ['inbox', 'shows', 'add'].includes(qs.get('tab'))) setTab(qs.get('tab'));
})();
