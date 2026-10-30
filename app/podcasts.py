"""Podcasts service layer: one implementation shared by the HTTP API, the view and the MCP tools.

Data lives under DATA_DIR (app data, never the owner's media folder):
  podcasts.sqlite3          shows, episodes, positions, settings
  episodes/<show>/<ep>.ext  downloaded audio (paths are stored relative to DATA_DIR)
  art/<show>.img            cached show artwork
"""
from __future__ import annotations

import html
import json
import os
import struct
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

import feeds
import netfetch
import youtube
from netfetch import FetchError

DATA = Path(os.environ.get('DATA_DIR', Path(__file__).resolve().parent.parent / '.data'))
DB_PATH = DATA / 'podcasts.sqlite3'
EPISODES_DIR = DATA / 'episodes'
ART_DIR = DATA / 'art'
MB = 1024 * 1024
# Long-running shows (e.g. 800+ episodes with full show notes) publish 30+ MB feeds.
# Feeds are parsed as a stream (feeds.FeedParser), so this caps transfer, not memory.
MAX_FEED_BYTES = int(os.environ.get('PODCASTS_MAX_FEED_MB', '64')) * MB
MAX_ART_BYTES = 5 * MB
MAX_OPML_BYTES = 2 * MB
MAX_EPISODE_BYTES = int(os.environ.get('PODCASTS_MAX_EPISODE_MB', '1024')) * MB
DISK_RESERVE_BYTES = int(os.environ.get('PODCASTS_DISK_RESERVE_MB', '512')) * MB
SEARCH_URL = 'https://itunes.apple.com/search'
DEFAULTS = {
    'storage_cap_mb': 2048,     # all downloads together
    'refresh_minutes': 60,      # background feed refresh
    'auto_download': True,      # default for new subscriptions
    'speed': 1.0,               # 0.8 - 3.0
    'skip_back': 15,
    'skip_forward': 30,
}
LIMITS = {'storage_cap_mb': (256, 512000), 'refresh_minutes': (15, 1440), 'speed': (0.8, 3.0),
          'skip_back': (5, 60), 'skip_forward': (5, 120)}
EXT = {'audio/mpeg': 'mp3', 'audio/mp3': 'mp3', 'audio/mp4': 'm4a', 'audio/x-m4a': 'm4a', 'audio/m4a': 'm4a',
       'audio/aac': 'aac', 'audio/ogg': 'ogg', 'audio/opus': 'opus', 'audio/wav': 'wav', 'audio/x-wav': 'wav',
       'audio/flac': 'flac', 'audio/webm': 'webm', 'video/mp4': 'mp4', 'video/x-m4v': 'm4v', 'video/webm': 'webm'}
MIME = {v: k for k, v in reversed(list(EXT.items()))}
MIME.update({'mp3': 'audio/mpeg', 'm4a': 'audio/mp4', 'm4b': 'audio/mp4', 'oga': 'audio/ogg', 'mp4': 'video/mp4'})

wake_refresh = threading.Event()
wake_download = threading.Event()
_db_lock = threading.RLock()
_started = False
_state = {'last_refresh': 0, 'refreshing': False, 'downloading': None}


class Problem(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def now():
    return int(time.time())


# ------------------------------------------------------------------ database

SCHEMA = """
CREATE TABLE IF NOT EXISTS shows (
  id INTEGER PRIMARY KEY, feed_url TEXT NOT NULL UNIQUE, title TEXT NOT NULL DEFAULT '', author TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '', image_url TEXT NOT NULL DEFAULT '', link TEXT NOT NULL DEFAULT '',
  art_file TEXT NOT NULL DEFAULT '', auto_download INTEGER NOT NULL DEFAULT 1, added_at INTEGER NOT NULL,
  last_checked INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '', etag TEXT NOT NULL DEFAULT '',
  last_modified TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS episodes (
  id INTEGER PRIMARY KEY, show_id INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE, guid TEXT NOT NULL,
  title TEXT NOT NULL, published INTEGER NOT NULL DEFAULT 0, duration INTEGER NOT NULL DEFAULT 0,
  enclosure_url TEXT NOT NULL, mime TEXT NOT NULL DEFAULT '', length INTEGER NOT NULL DEFAULT 0,
  notes TEXT NOT NULL DEFAULT '', link TEXT NOT NULL DEFAULT '', image_url TEXT NOT NULL DEFAULT '',
  transcript_url TEXT NOT NULL DEFAULT '', first_seen INTEGER NOT NULL,
  inbox INTEGER NOT NULL DEFAULT 0, played INTEGER NOT NULL DEFAULT 0, played_at INTEGER NOT NULL DEFAULT 0,
  position REAL NOT NULL DEFAULT 0, position_at INTEGER NOT NULL DEFAULT 0,
  download TEXT NOT NULL DEFAULT '', download_error TEXT NOT NULL DEFAULT '', queued_at INTEGER NOT NULL DEFAULT 0,
  progress INTEGER NOT NULL DEFAULT 0, file TEXT NOT NULL DEFAULT '', file_size INTEGER NOT NULL DEFAULT 0,
  downloaded_at INTEGER NOT NULL DEFAULT 0,
  UNIQUE(show_id, guid));
CREATE INDEX IF NOT EXISTS episodes_show ON episodes(show_id, published DESC);
CREATE INDEX IF NOT EXISTS episodes_download ON episodes(download, queued_at);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.execute('PRAGMA journal_mode=WAL')
    return c


def init():
    DATA.mkdir(parents=True, exist_ok=True)
    EPISODES_DIR.mkdir(exist_ok=True)
    ART_DIR.mkdir(exist_ok=True)
    c = connect()
    try:
        c.executescript(SCHEMA)
        # A download that was running when the app stopped resumes from the queue.
        c.execute("UPDATE episodes SET download='queued', progress=0 WHERE download='downloading'")
        c.commit()
    finally:
        c.close()
    for part in EPISODES_DIR.rglob('*.part'):
        part.unlink(missing_ok=True)


def q(sql, args=(), one=False):
    c = connect()
    try:
        rows = c.execute(sql, args).fetchall()
        return (rows[0] if rows else None) if one else rows
    finally:
        c.close()


def write(fn):
    with _db_lock:
        c = connect()
        try:
            with c:
                return fn(c)
        finally:
            c.close()


# ------------------------------------------------------------------ settings

def get_settings():
    out = dict(DEFAULTS)
    for r in q('SELECT key, value FROM settings'):
        if r['key'] in DEFAULTS:
            try:
                out[r['key']] = json.loads(r['value'])
            except ValueError:
                pass
    return out


def set_settings(patch):
    if not isinstance(patch, dict):
        raise Problem(400, 'Send settings as a JSON object.')
    clean = {}
    for k, v in patch.items():
        if k not in DEFAULTS:
            raise Problem(400, f'Unknown setting {k}.')
        if isinstance(DEFAULTS[k], bool):
            if not isinstance(v, bool):
                raise Problem(400, f'{k} must be true or false.')
        else:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise Problem(400, f'{k} must be a number.')
            lo, hi = LIMITS[k]
            if not lo <= v <= hi:
                raise Problem(400, f'{k} must be between {lo} and {hi}.')
            v = round(float(v), 2) if k == 'speed' else int(v)
        clean[k] = v
    write(lambda c: [c.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                               (k, json.dumps(v))) for k, v in clean.items()])
    if 'storage_cap_mb' in clean:
        make_room(0)
    if 'refresh_minutes' in clean:
        wake_refresh.set()
    return get_settings()


# ------------------------------------------------------------------ serialisation

def _rel(path: Path) -> str:
    return path.resolve().relative_to(DATA.resolve()).as_posix()


def show_dict(r, counts=None):
    d = {'id': r['id'], 'title': r['title'] or urlsplit(r['feed_url']).hostname, 'author': r['author'],
         'feed_url': r['feed_url'], 'link': r['link'], 'auto_download': bool(r['auto_download']),
         'artwork': f'api/shows/{r["id"]}/art' if r['art_file'] else '', 'added_at': r['added_at'],
         'last_checked': r['last_checked'], 'last_error': r['last_error'] or None,
         'pending': r['last_checked'] == 0 and not r['last_error'],
         'source': 'youtube' if youtube.is_feed(r['feed_url']) else 'rss'}
    if counts is not None:
        d.update(counts)
    return d


def episode_dict(r, detail=False):
    d = {'id': r['id'], 'show_id': r['show_id'], 'show_title': r['show_title'] if 'show_title' in r.keys() else None,
         'title': r['title'], 'published': r['published'], 'duration': r['duration'],
         'position': round(r['position'], 1), 'position_at': r['position_at'], 'played': bool(r['played']),
         'in_inbox': bool(r['inbox']),
         'download': {'state': r['download'] or 'none', 'progress_bytes': r['progress'], 'size_bytes': r['file_size'] or r['length'],
                      'error': r['download_error'] or None},
         'file': r['file'] or None, 'audio': f'api/episodes/{r["id"]}/audio',
         'artwork': f'api/shows/{r["show_id"]}/art' if ('art_file' in r.keys() and r['art_file']) else ''}
    if detail:
        d.update({
            'enclosure_url': r['enclosure_url'], 'mime': r['mime'], 'link': r['link'],
            'notes_html': r['notes'], 'notes_text': feeds.notes_text(r['notes']),
            'file_note': 'file is relative to the Podcasts app data folder (DATA_DIR, /data in the container).' if r['file'] else None,
            # v2 hook: transcripts will come from the installed Transcriber app (see README "Transcripts (v2)").
            'transcript': {'status': 'not_available', 'text': None, 'source': None,
                           'feed_transcript_url': r['transcript_url'] or None,
                           'planned': 'v2: transcribe the downloaded file with the Transcriber app'},
        })
    return d


EP_SELECT = ('SELECT e.*, s.title AS show_title, s.art_file AS art_file FROM episodes e JOIN shows s ON s.id=e.show_id ')


# ------------------------------------------------------------------ directory search

def _youtube_result(c, subscribed):
    cid = c['channel_id']
    return {'title': c['title'], 'author': c.get('handle') or 'YouTube', 'feed_url': youtube.channel_url(cid),
            'artwork': c.get('artwork') or '', 'genre': 'YouTube channel', 'subscribers': c.get('subscribers') or None,
            'source': 'youtube', 'channel_id': cid,
            'subscribed': youtube.feed_url(cid) in subscribed or youtube.channel_feed_url(cid) in subscribed}


def _search_youtube(term, out):
    try:
        out.extend(youtube.search_channels(term, 6))
    except Exception:  # YouTube search is a bonus; Apple results never depend on it
        pass


def search_directory(term, limit=20):
    term = (term or '').strip() if isinstance(term, str) else ''
    if not term:
        raise Problem(400, 'Type a podcast name to search.')
    if len(term) > 200:
        raise Problem(400, 'That search is too long.')
    limit = max(1, min(int(limit or 20), 50))
    subscribed = {r['feed_url'] for r in q('SELECT feed_url FROM shows')}
    if youtube.parse_ref(term) is not None:  # a channel URL or @handle: look that one channel up
        try:
            info = youtube.resolve(term)
        except FetchError as e:
            raise Problem(422, str(e)) from None
        c = {'channel_id': info['channel_id'], 'title': info['title'], 'artwork': info['image'],
             'handle': term if term.startswith('@') else ''}
        return {'query': term, 'results': [], 'youtube': [_youtube_result(c, subscribed)], 'source': 'YouTube'}
    if urlsplit(term).scheme in ('http', 'https'):
        return {'query': term, 'results': [], 'youtube': [], 'is_url': True}
    yt = []
    worker = threading.Thread(target=_search_youtube, args=(term, yt), daemon=True)
    worker.start()
    url = f'{SEARCH_URL}?media=podcast&entity=podcast&limit={limit}&term={quote(term)}'
    problem = None
    data = {}
    try:
        _status, body, _r = netfetch.get_bytes(url, 2 * MB, 'search')
        data = json.loads(body)
    except FetchError as e:
        problem = Problem(502, f'Podcast search is unavailable right now ({e}).')
    except ValueError:
        problem = Problem(502, 'Podcast search returned an unreadable answer.')
    worker.join(20)
    youtube_results = [_youtube_result(c, subscribed) for c in list(yt)]
    if problem and not youtube_results:
        raise problem
    results = []
    for item in data.get('results', []):
        feed = item.get('feedUrl')
        if not isinstance(feed, str) or urlsplit(feed).scheme not in ('http', 'https'):
            continue
        art = item.get('artworkUrl600') or item.get('artworkUrl100') or ''
        results.append({'title': str(item.get('collectionName') or item.get('trackName') or '')[:300],
                        'author': str(item.get('artistName') or '')[:300], 'feed_url': feed,
                        'artwork': art if isinstance(art, str) and art.startswith('https://') else '',
                        'genre': str(item.get('primaryGenreName') or '')[:80], 'episodes': item.get('trackCount'),
                        'itunes_id': item.get('collectionId'), 'subscribed': feed in subscribed, 'source': 'rss'})
    return {'query': term, 'results': results, 'youtube': youtube_results,
            'source': 'Apple Podcasts directory (iTunes Search API); YouTube channel search'}


# ------------------------------------------------------------------ subscribe / refresh

def _fetch_feed(url, etag='', last_modified=''):
    headers = {'Accept': 'application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5'}
    if etag:
        headers['If-None-Match'] = etag
    if last_modified:
        headers['If-Modified-Since'] = last_modified
    if youtube.is_feed(url):
        r = netfetch.open_url(url, headers=headers, timeout=25, allow=youtube.host_allowed)
    else:
        r = netfetch.open_url(url, headers=headers, timeout=25)
    try:
        if r.status == 304:
            return None, r
        if r.status >= 400:
            raise FetchError(f'The server answered {r.status} for that feed.')
        too_big = f'The feed is larger than {MAX_FEED_BYTES // MB} MB.'
        length = r.header('Content-Length')
        if length and length.isdigit() and int(length) > MAX_FEED_BYTES:
            raise FetchError(too_big)
        parser = feeds.FeedParser(r.url)
        total = 0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_FEED_BYTES:
                raise FetchError(too_big)
            parser.feed(chunk)
        return parser.close(), r
    finally:
        r.close()


def _existing(*urls):
    for u in urls:
        row = q('SELECT * FROM shows WHERE feed_url=?', (u,), one=True)
        if row:
            return {'already_subscribed': True, 'show': show_dict(row, _counts(row['id']))}
    return None


def _subscribe_youtube(text):
    """A YouTube channel reference -> (feed url, parsed feed, response), or an 'already' answer."""
    try:
        info = youtube.resolve(text)
    except FetchError as e:
        raise Problem(422, str(e)) from None
    cid = info['channel_id']
    long_form, every = youtube.feed_url(cid), youtube.channel_feed_url(cid)
    already = _existing(long_form, every)
    if already:
        return already, None, None, None
    url = long_form
    try:
        parsed, r = _fetch_feed(url)
    except FetchError as e:
        if '404' not in str(e):
            raise Problem(422, str(e)) from None
        url = every  # no long-form uploads list: the channel feed, with Shorts skipped by the parser
        try:
            parsed, r = _fetch_feed(url)
        except (FetchError, feeds.FeedError) as e2:
            raise Problem(422, str(e2)) from None
    except feeds.FeedError as e:
        raise Problem(422, str(e)) from None
    if parsed is not None:
        parsed['title'] = info['title'] or parsed['title']
        parsed['author'] = parsed['author'] or info['title']
        parsed['image'] = info['image'] or parsed['image']
        parsed['link'] = info['url']
        parsed['description'] = parsed['description'] or feeds.sanitize_html(html.escape(info['description']))[:20000]
    return None, url, parsed, r


def subscribe(url, auto_download=None):
    if isinstance(url, str) and youtube.parse_ref(url) is not None:
        already, url, parsed, r = _subscribe_youtube(url)
        if already:
            return already
    else:
        try:
            url = netfetch.check_url(url)
        except FetchError as e:
            raise Problem(400, str(e)) from None
        already = _existing(url)
        if already:
            return already
        try:
            parsed, r = _fetch_feed(url)
        except FetchError as e:
            raise Problem(422, str(e)) from None
        except feeds.FeedError as e:
            raise Problem(422, str(e)) from None
    if parsed is None:
        raise Problem(422, 'The feed answered "not modified" to a first request.')
    if auto_download is None:
        auto_download = get_settings()['auto_download']

    def add(c):
        cur = c.execute('INSERT OR IGNORE INTO shows(feed_url, added_at, auto_download) VALUES(?,?,?)', (url, now(), int(bool(auto_download))))
        sid = cur.lastrowid if cur.rowcount else c.execute('SELECT id FROM shows WHERE feed_url=?', (url,)).fetchone()['id']
        _apply_feed(c, sid, parsed, r, first=True)
        return sid
    sid = write(add)
    threading.Thread(target=_cache_art, args=(sid,), daemon=True).start()
    wake_download.set()
    row = q('SELECT * FROM shows WHERE id=?', (sid,), one=True)
    return {'already_subscribed': False, 'show': show_dict(row, _counts(sid))}


def _apply_feed(c, sid, parsed, r, first):
    # A feed that omits artwork/description/link (YouTube uploads feeds do) keeps what the show already has.
    c.execute("UPDATE shows SET title=?, author=?, description=CASE WHEN ?<>'' THEN ? ELSE description END, "
              "image_url=CASE WHEN ?<>'' THEN ? ELSE image_url END, link=CASE WHEN ?<>'' THEN ? ELSE link END, "
              'last_checked=?, last_error=?, etag=?, last_modified=? WHERE id=?',
              (parsed['title'], parsed['author'], parsed['description'], parsed['description'], parsed['image'], parsed['image'],
               parsed['link'], parsed['link'], now(), '',
               (r.header('ETag') or '')[:300], (r.header('Last-Modified') or '')[:100], sid))
    auto = c.execute('SELECT auto_download FROM shows WHERE id=?', (sid,)).fetchone()['auto_download']
    eps = sorted(parsed['episodes'], key=lambda e: e['published'], reverse=True)
    t = now()
    new_ids = []
    for e in eps:
        cur = c.execute('INSERT OR IGNORE INTO episodes(show_id, guid, title, published, duration, enclosure_url, mime, length, notes, link, image_url, transcript_url, first_seen) '
                        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (sid, e['guid'], e['title'], e['published'], e['duration'], e['url'], e['mime'], e['length'], e['notes'], e['link'], e['image'], e['transcript_url'], t))
        if cur.rowcount:
            new_ids.append(cur.lastrowid)
        else:
            c.execute('UPDATE episodes SET title=?, published=?, duration=CASE WHEN ?>0 THEN ? ELSE duration END, enclosure_url=?, mime=?, length=?, notes=?, link=?, transcript_url=? '
                      'WHERE show_id=? AND guid=?',
                      (e['title'], e['published'], e['duration'], e['duration'], e['url'], e['mime'], e['length'], e['notes'], e['link'], e['transcript_url'], sid, e['guid']))
    # First fetch: only the newest episode goes to Up next (and downloads); older ones are the back catalogue.
    inbox_ids = new_ids[:1] if first else new_ids
    for eid in inbox_ids:
        c.execute('UPDATE episodes SET inbox=1 WHERE id=?', (eid,))
        if auto:
            c.execute("UPDATE episodes SET download='queued', queued_at=? WHERE id=? AND download IN ('', 'failed')", (t, eid))
    return new_ids


def refresh_show(sid):
    row = q('SELECT * FROM shows WHERE id=?', (sid,), one=True)
    if not row:
        raise Problem(404, 'Show not found.')
    first = row['last_checked'] == 0 and not q('SELECT 1 FROM episodes WHERE show_id=? LIMIT 1', (sid,), one=True)
    try:
        parsed, r = _fetch_feed(row['feed_url'], row['etag'], row['last_modified'])
    except (FetchError, feeds.FeedError) as e:
        write(lambda c: c.execute('UPDATE shows SET last_checked=?, last_error=? WHERE id=?', (now(), str(e)[:300], sid)))
        return {'show_id': sid, 'ok': False, 'error': str(e), 'new_episodes': 0}
    if parsed is None:
        write(lambda c: c.execute("UPDATE shows SET last_checked=?, last_error='' WHERE id=?", (now(), sid)))
        return {'show_id': sid, 'ok': True, 'new_episodes': 0, 'not_modified': True}
    new = write(lambda c: _apply_feed(c, sid, parsed, r, first))
    if not row['art_file'] or (parsed['image'] and row['image_url'] != parsed['image']):
        threading.Thread(target=_cache_art, args=(sid,), daemon=True).start()
    if new:
        wake_download.set()
    return {'show_id': sid, 'ok': True, 'new_episodes': len(new)}


def refresh_all(force=False):
    every = get_settings()['refresh_minutes'] * 60
    out = []
    _state['refreshing'] = True
    try:
        for row in q('SELECT id, last_checked FROM shows ORDER BY last_checked'):
            if force or now() - row['last_checked'] >= every:
                out.append(refresh_show(row['id']))
    finally:
        _state['refreshing'] = False
        _state['last_refresh'] = now()
    return {'refreshed': len(out), 'new_episodes': sum(r.get('new_episodes', 0) for r in out),
            'errors': [r for r in out if not r.get('ok')]}


def _cache_art(sid):
    row = q('SELECT image_url, feed_url FROM shows WHERE id=?', (sid,), one=True)
    if not row or not row['image_url']:
        return
    try:
        if youtube.is_feed(row['feed_url']):
            r = netfetch.open_url(row['image_url'], timeout=20, allow=youtube.host_allowed)
        else:
            r = netfetch.open_url(row['image_url'], timeout=20)
        try:
            ctype = (r.header('Content-Type') or '').split(';')[0].strip().lower()
            if r.status != 200 or ctype not in ('image/jpeg', 'image/png', 'image/webp', 'image/gif'):
                return
            body = r.read_capped(MAX_ART_BYTES, 'artwork')
        finally:
            r.close()
    except FetchError:
        return
    path = ART_DIR / f'{sid}.img'
    tmp = ART_DIR / f'.{sid}.tmp'
    tmp.write_bytes(body)
    os.replace(tmp, path)
    (ART_DIR / f'{sid}.type').write_text(ctype)
    write(lambda c: c.execute('UPDATE shows SET art_file=? WHERE id=?', (_rel(path), sid)))


def art(sid):
    row = q('SELECT art_file FROM shows WHERE id=?', (sid,), one=True)
    if not row or not row['art_file']:
        raise Problem(404, 'No artwork.')
    path = (DATA / row['art_file']).resolve()
    if ART_DIR.resolve() not in path.parents or not path.is_file():
        raise Problem(404, 'No artwork.')
    t = ART_DIR / f'{sid}.type'
    return path.read_bytes(), (t.read_text() if t.is_file() else 'image/jpeg')


def unsubscribe(sid):
    row = q('SELECT * FROM shows WHERE id=?', (sid,), one=True)
    if not row:
        raise Problem(404, 'Show not found.')
    write(lambda c: c.execute('DELETE FROM shows WHERE id=?', (sid,)))
    shutil.rmtree(EPISODES_DIR / str(sid), ignore_errors=True)
    for name in (f'{sid}.img', f'{sid}.type'):
        (ART_DIR / name).unlink(missing_ok=True)
    return {'unsubscribed': sid, 'title': row['title']}


def update_show(sid, args):
    if not q('SELECT 1 FROM shows WHERE id=?', (sid,), one=True):
        raise Problem(404, 'Show not found.')
    if 'auto_download' in args:
        if not isinstance(args['auto_download'], bool):
            raise Problem(400, 'auto_download must be true or false.')
        write(lambda c: c.execute('UPDATE shows SET auto_download=? WHERE id=?', (int(args['auto_download']), sid)))
    return show_detail(sid, limit=0)


def import_opml(text):
    if not isinstance(text, str) or not text.strip():
        raise Problem(400, 'Choose an OPML file exported from another podcast app.')
    data = text.encode('utf-8', 'replace')
    if len(data) > MAX_OPML_BYTES:
        raise Problem(413, 'That OPML file is larger than 2 MB.')
    try:
        items = feeds.parse_opml(data)
    except feeds.FeedError as e:
        raise Problem(422, str(e)) from None
    auto = int(bool(get_settings()['auto_download']))
    added, skipped = 0, 0
    for it in items:
        try:
            url = netfetch.check_url(it['url'])
        except FetchError:
            skipped += 1
            continue
        def ins(c, url=url, title=it['title']):
            return c.execute('INSERT OR IGNORE INTO shows(feed_url, title, added_at, auto_download) VALUES(?,?,?,?)', (url, title, now(), auto)).rowcount
        if write(ins):
            added += 1
        else:
            skipped += 1
    wake_refresh.set()
    return {'found': len(items), 'added': added, 'skipped': skipped,
            'note': 'New shows load in the background over the next minute or two.'}


def export_opml():
    return feeds.build_opml([dict(r) for r in q('SELECT title, feed_url FROM shows ORDER BY title COLLATE NOCASE')])


# ------------------------------------------------------------------ reading

def _counts(sid):
    r = q("SELECT COUNT(*) n, SUM(played=0) unplayed, SUM(inbox=1 AND played=0) new, SUM(download='done') downloaded, MAX(published) latest "
          'FROM episodes WHERE show_id=?', (sid,), one=True)
    return {'episodes': r['n'] or 0, 'unplayed': r['unplayed'] or 0, 'new': r['new'] or 0,
            'downloaded': r['downloaded'] or 0, 'latest': r['latest'] or 0}


def list_shows():
    return {'shows': [show_dict(r, _counts(r['id'])) for r in q('SELECT * FROM shows ORDER BY title COLLATE NOCASE')]}


def show_detail(sid, limit=100, offset=0):
    row = q('SELECT * FROM shows WHERE id=?', (sid,), one=True)
    if not row:
        raise Problem(404, 'Show not found.')
    limit = max(0, min(int(limit), 500))
    offset = max(0, int(offset))
    eps = q(EP_SELECT + 'WHERE e.show_id=? ORDER BY e.published DESC, e.id DESC LIMIT ? OFFSET ?', (sid, limit, offset)) if limit else []
    d = show_dict(row, _counts(sid))
    d['description_html'] = row['description']
    return {'show': d, 'episodes': [episode_dict(e) for e in eps], 'offset': offset}


def inbox(limit=100):
    cont = q(EP_SELECT + 'WHERE e.position>0 AND e.played=0 ORDER BY e.position_at DESC LIMIT 20')
    seen = {r['id'] for r in cont}
    new = [r for r in q(EP_SELECT + 'WHERE e.inbox=1 AND e.played=0 ORDER BY e.published DESC LIMIT ?', (limit,)) if r['id'] not in seen]
    np = now_playing()
    return {'continue': [episode_dict(r) for r in cont], 'new': [episode_dict(r) for r in new], 'now_playing': np}


def now_playing():
    r = q(EP_SELECT + 'WHERE e.position_at>0 ORDER BY e.position_at DESC LIMIT 1', one=True)
    return episode_dict(r) if r else None


def episode(eid):
    r = q(EP_SELECT + 'WHERE e.id=?', (eid,), one=True)
    if not r:
        raise Problem(404, 'Episode not found.')
    return episode_dict(r, detail=True)


def _ep_row(eid):
    r = q('SELECT * FROM episodes WHERE id=?', (eid,), one=True)
    if not r:
        raise Problem(404, 'Episode not found.')
    return r


# ------------------------------------------------------------------ playback state

def set_progress(eid, args):
    _ep_row(eid)
    pos = args.get('position')
    if isinstance(pos, bool) or not isinstance(pos, (int, float)) or pos < 0 or pos > 200000:
        raise Problem(400, 'position must be seconds (a number).')
    dur = args.get('duration')
    completed = args.get('completed') is True
    t = now()

    def go(c):
        if isinstance(dur, (int, float)) and not isinstance(dur, bool) and 0 < dur < 200000:
            c.execute('UPDATE episodes SET duration=? WHERE id=? AND (duration=0 OR ABS(duration-?)>5)', (int(dur), eid, dur))
        if completed:
            c.execute('UPDATE episodes SET played=1, played_at=?, inbox=0, position=0, position_at=? WHERE id=?', (t, t, eid))
        else:
            c.execute('UPDATE episodes SET position=?, position_at=? WHERE id=?', (float(pos), t, eid))
    write(go)
    return episode(eid)


def mark(eid, played, inbox_flag=None):
    if not isinstance(played, bool) and played is not None:
        raise Problem(400, 'played must be true or false.')
    _ep_row(eid)
    t = now()

    def go(c):
        if played is True:
            c.execute('UPDATE episodes SET played=1, played_at=?, inbox=0, position=0 WHERE id=?', (t, eid))
        elif played is False:
            c.execute('UPDATE episodes SET played=0, played_at=0, position=0 WHERE id=?', (eid,))
        if isinstance(inbox_flag, bool):
            c.execute('UPDATE episodes SET inbox=? WHERE id=?', (int(inbox_flag), eid))
    write(go)
    return episode(eid)


# ------------------------------------------------------------------ downloads

def storage_used():
    r = q("SELECT COALESCE(SUM(file_size),0) s FROM episodes WHERE download='done'", one=True)
    return r['s']


def queue_download(eid):
    r = _ep_row(eid)
    if r['download'] != 'done':
        write(lambda c: c.execute("UPDATE episodes SET download='queued', download_error='', queued_at=? WHERE id=?", (now(), eid)))
        wake_download.set()
    return episode(eid)


def _remove_file(c, r):
    if r['file']:
        path = (DATA / r['file']).resolve()
        if EPISODES_DIR.resolve() in path.parents:
            path.unlink(missing_ok=True)
    c.execute("UPDATE episodes SET download='', file='', file_size=0, progress=0, download_error='' WHERE id=?", (r['id'],))


def delete_download(eid):
    r = _ep_row(eid)
    write(lambda c: _remove_file(c, r))
    return episode(eid)


def make_room(need, for_episode=None):
    """Free space for `need` bytes under the storage cap. Played downloads go first (oldest
    played first), then unplayed, not-started episodes older than the one being fetched."""
    cap = get_settings()['storage_cap_mb'] * MB
    used = storage_used()
    if used + need <= cap:
        return True
    exclude = for_episode['id'] if for_episode else -1
    newer_than = for_episode['published'] if for_episode else 1 << 62
    victims = list(q("SELECT * FROM episodes WHERE download='done' AND played=1 AND id<>? ORDER BY played_at", (exclude,)))
    victims += list(q("SELECT * FROM episodes WHERE download='done' AND played=0 AND position=0 AND id<>? AND published<? ORDER BY published",
                      (exclude, newer_than)))
    for v in victims:
        if used + need <= cap:
            break
        write(lambda c, v=v: _remove_file(c, v))
        used -= v['file_size']
    return used + need <= cap


def _pick_ext(ctype, url, mime):
    for m in (ctype, mime):
        m = (m or '').split(';')[0].strip().lower()
        if m in EXT:
            return EXT[m]
    m = feeds.MEDIA_EXT.search(urlsplit(url).path + '?')
    return m.group(1).lower() if m else 'mp3'


def mp4_duration(path) -> int:
    """Seconds from an MP4/M4A file's mvhd box (YouTube feeds carry no duration). 0 if unknown."""
    try:
        with open(path, 'rb') as f:
            size_total = os.fstat(f.fileno()).st_size

            def boxes(start, end):
                pos = start
                while pos + 8 <= end:
                    f.seek(pos)
                    hdr = f.read(8)
                    if len(hdr) < 8:
                        return
                    size, kind = struct.unpack('>I4s', hdr)
                    head = 8
                    if size == 1:
                        size = struct.unpack('>Q', f.read(8))[0]
                        head = 16
                    elif size == 0:
                        size = end - pos
                    if size < head:
                        return
                    yield kind, pos + head, pos + size
                    pos += size
            for kind, a, b in boxes(0, size_total):
                if kind != b'moov':
                    continue
                for k2, a2, _b2 in boxes(a, b):
                    if k2 == b'mvhd':
                        f.seek(a2)
                        ver = f.read(1)[0]
                        f.read(3)
                        if ver == 1:
                            _c, _m, scale, dur = struct.unpack('>QQIQ', f.read(28))
                        else:
                            _c, _m, scale, dur = struct.unpack('>IIII', f.read(16))
                        return int(dur / scale) if scale else 0
    except (OSError, struct.error, IndexError):
        return 0
    return 0


def _fail(eid, msg):
    write(lambda c: c.execute("UPDATE episodes SET download='failed', download_error=?, progress=0 WHERE id=?", (msg[:300], eid)))


def _download(r):
    eid = r['id']
    write(lambda c: c.execute("UPDATE episodes SET download='downloading', progress=0, download_error='' WHERE id=?", (eid,)))
    tmp = None
    vid = youtube.video_id(r['enclosure_url'])
    try:
        resp = youtube.open_audio(vid) if vid else netfetch.open_url(r['enclosure_url'], timeout=30)
    except FetchError as e:
        return _fail(eid, str(e))
    _state['downloading'] = eid
    try:
        if resp.status != 200:
            return _fail(eid, f'The podcast host answered {resp.status}.')
        length = resp.header('Content-Length')
        length = int(length) if length and length.isdigit() else 0
        if length > MAX_EPISODE_BYTES:
            return _fail(eid, f'This episode is larger than the {MAX_EPISODE_BYTES // MB} MB per-episode limit.')
        need = length or r['length'] or 0
        if not make_room(need, r):
            return _fail(eid, 'Storage cap reached. Raise it in Settings or delete played downloads.')
        if shutil.disk_usage(DATA).free - need < DISK_RESERVE_BYTES:
            return _fail(eid, 'The Wizard is low on disk space.')
        ext = _pick_ext(resp.header('Content-Type'), resp.url, r['mime'])
        folder = EPISODES_DIR / str(r['show_id'])
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / f'{eid}.part'
        final = folder / f'{eid}.{ext}'
        cap = get_settings()['storage_cap_mb'] * MB
        total, last_mark = 0, 0
        with open(tmp, 'wb') as out:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_EPISODE_BYTES:
                    return _fail(eid, f'This episode is larger than the {MAX_EPISODE_BYTES // MB} MB per-episode limit.')
                out.write(chunk)
                if total - last_mark >= 2 * MB:
                    last_mark = total
                    cur = q('SELECT download FROM episodes WHERE id=?', (eid,), one=True)
                    if not cur or cur['download'] != 'downloading':
                        return None  # cancelled or unsubscribed
                    if storage_used() + total > cap and not make_room(total, r):
                        return _fail(eid, 'Storage cap reached. Raise it in Settings or delete played downloads.')
                    write(lambda c, t=total: c.execute('UPDATE episodes SET progress=? WHERE id=?', (t, eid)))
        if total == 0:
            return _fail(eid, 'The podcast host sent an empty file.')
        if length and total < length:
            return _fail(eid, 'The download stopped early. It will retry on the next tap.')
        os.replace(tmp, final)
        tmp = None
        rel = _rel(final)

        secs = r['duration'] or (mp4_duration(final) if ext in ('m4a', 'mp4', 'm4b', 'm4v') else 0)

        def done(c):
            cur = c.execute("UPDATE episodes SET download='done', file=?, file_size=?, progress=?, downloaded_at=?, mime=CASE WHEN mime='' THEN ? ELSE mime END, "
                            "duration=CASE WHEN duration=0 THEN ? ELSE duration END "
                            "WHERE id=? AND download='downloading'", (rel, total, total, now(), MIME.get(ext, ''), secs, eid))
            return cur.rowcount
        if not write(done):
            final.unlink(missing_ok=True)
    except (OSError, FetchError) as e:
        _fail(eid, f'Download failed: {e}')
    finally:
        resp.close()
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        _state['downloading'] = None


def _download_loop():
    while True:
        r = q("SELECT * FROM episodes WHERE download='queued' ORDER BY queued_at DESC LIMIT 1", one=True)
        if r is None:
            wake_download.wait(60)
            wake_download.clear()
            continue
        try:
            _download(r)
        except Exception as e:  # never let the worker die
            _fail(r['id'], f'Download failed: {e.__class__.__name__}')


def _refresh_loop():
    time.sleep(3)
    while True:
        try:
            refresh_all()
        except Exception:
            pass
        wake_refresh.wait(60)
        wake_refresh.clear()


def start_workers():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_download_loop, name='downloads', daemon=True).start()
    threading.Thread(target=_refresh_loop, name='refresh', daemon=True).start()


# ------------------------------------------------------------------ status

_yt_status = {'at': 0, 'value': None}


def youtube_status():
    """Whether YouTube shows can download: the music-source connector's own status (cached 30 s)."""
    n = sum(1 for r in q('SELECT feed_url FROM shows') if youtube.is_feed(r['feed_url']))
    if time.time() - _yt_status['at'] > 30 or _yt_status['value'] is None:
        _yt_status['value'] = youtube.source_status()
        _yt_status['at'] = time.time()
    return {'shows': n, **_yt_status['value']}


def status():
    s = get_settings()
    r = q("SELECT COUNT(*) n, SUM(download='done') d, SUM(download IN ('queued','downloading')) qd, SUM(download='failed') f, "
          'SUM(inbox=1 AND played=0 AND position=0) new, SUM(position>0 AND played=0) started FROM episodes', one=True)
    shows = q('SELECT COUNT(*) n, SUM(last_error<>\'\') bad FROM shows', one=True)
    used = storage_used()
    return {'app': 'wizard-podcasts', 'ok': True, 'shows': shows['n'] or 0, 'shows_with_errors': shows['bad'] or 0,
            'episodes': r['n'] or 0, 'new_episodes': r['new'] or 0, 'in_progress': r['started'] or 0, 'downloaded': r['d'] or 0,
            'download_queue': r['qd'] or 0, 'download_failures': r['f'] or 0, 'downloading_episode': _state['downloading'],
            'storage_used_mb': round(used / MB, 1), 'storage_cap_mb': s['storage_cap_mb'],
            'refresh_minutes': s['refresh_minutes'], 'last_refresh': _state['last_refresh'], 'refreshing': _state['refreshing'],
            'now_playing': now_playing(), 'settings': s, 'youtube': youtube_status(),
            'data_dir_note': 'Episode files are under the app data folder; podcasts_episode returns each file path relative to it.'}
