"""Discover (0.5.0): a recommendation feed of shows, learned on this box from what the owner follows, likes and hears.

Nothing about the owner leaves the box except plain directory lookups (show names, ids) to free public sources:
  - Apple Podcasts top charts (rss.applemarketingtools.com, US + the owner's region) and per-genre popular shows
    (iTunes Search API with genreId), details + latest episode through the iTunes lookup API;
  - Apple Podcasts' own "You Might Also Like" shelf on a show's public page (similar shows);
  - iTunes search by the authors of followed shows (their other shows) and by guests that recur in the episode
    titles of the shows he follows (the guest's own show);
  - YouTube's related videos on a followed channel's newest uploads (channels YouTube itself pairs with it).
    The YouTube Music connector has no podcast/channel relation (its related-artists and radio are music-only).

Taste is local (SQLite `taste` table = explicit thumbs; follows, episodes heard through, skips and unfollows come from
the app's own data). Fetches are cached (discover_cache, 12 h) and the pool is rebuilt in the background at most twice
a day; iTunes calls are spaced out to stay inside Apple's free rate limit.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlencode

import feeds
import netfetch
import podcasts as P
import youtube
from netfetch import FetchError

MB = 1024 * 1024
TTL = int(os.environ.get('PODCASTS_DISCOVER_TTL_HOURS', '12')) * 3600
MATCH_TTL = 30 * 86400
# Popular charts: the US plus the owner's own country, learned from the time zone his screen reports (0.5.2; no
# country is baked in). PODCASTS_DISCOVER_COUNTRIES (e.g. 'us,gb') overrides both.
COUNTRIES_ENV = [c.strip().lower() for c in os.environ.get('PODCASTS_DISCOVER_COUNTRIES', '').split(',') if c.strip()]
COUNTRY_NAMES = {
    'us': 'the US', 'gb': 'the UK', 'ca': 'Canada', 'au': 'Australia', 'nz': 'New Zealand', 'ie': 'Ireland',
    'de': 'Germany', 'at': 'Austria', 'ch': 'Switzerland', 'fr': 'France', 'be': 'Belgium', 'nl': 'the Netherlands',
    'es': 'Spain', 'pt': 'Portugal', 'it': 'Italy', 'se': 'Sweden', 'no': 'Norway', 'dk': 'Denmark', 'fi': 'Finland',
    'pl': 'Poland', 'cz': 'Czechia', 'gr': 'Greece', 'tr': 'Turkey', 'il': 'Israel', 'ae': 'the UAE', 'sa': 'Saudi Arabia',
    'za': 'South Africa', 'ng': 'Nigeria', 'ke': 'Kenya', 'eg': 'Egypt', 'in': 'India', 'pk': 'Pakistan', 'th': 'Thailand',
    'vn': 'Vietnam', 'my': 'Malaysia', 'sg': 'Singapore', 'id': 'Indonesia', 'ph': 'the Philippines', 'jp': 'Japan',
    'kr': 'South Korea', 'tw': 'Taiwan', 'hk': 'Hong Kong', 'cn': 'China', 'mx': 'Mexico', 'br': 'Brazil',
    'ar': 'Argentina', 'cl': 'Chile', 'co': 'Colombia', 'pe': 'Peru', 'ro': 'Romania', 'hu': 'Hungary', 'ua': 'Ukraine'}
TZ_COUNTRY = {
    'Europe/London': 'gb', 'Europe/Dublin': 'ie', 'Europe/Berlin': 'de', 'Europe/Vienna': 'at', 'Europe/Zurich': 'ch',
    'Europe/Paris': 'fr', 'Europe/Brussels': 'be', 'Europe/Amsterdam': 'nl', 'Europe/Madrid': 'es', 'Atlantic/Canary': 'es',
    'Europe/Lisbon': 'pt', 'Europe/Rome': 'it', 'Europe/Stockholm': 'se', 'Europe/Oslo': 'no', 'Europe/Copenhagen': 'dk',
    'Europe/Helsinki': 'fi', 'Europe/Warsaw': 'pl', 'Europe/Prague': 'cz', 'Europe/Athens': 'gr', 'Europe/Istanbul': 'tr',
    'Europe/Bucharest': 'ro', 'Europe/Budapest': 'hu', 'Europe/Kyiv': 'ua', 'Europe/Kiev': 'ua', 'Asia/Jerusalem': 'il',
    'Asia/Tel_Aviv': 'il', 'Asia/Dubai': 'ae', 'Asia/Riyadh': 'sa', 'Africa/Johannesburg': 'za', 'Africa/Lagos': 'ng',
    'Africa/Nairobi': 'ke', 'Africa/Cairo': 'eg', 'Asia/Kolkata': 'in', 'Asia/Calcutta': 'in', 'Asia/Karachi': 'pk',
    'Asia/Bangkok': 'th', 'Asia/Ho_Chi_Minh': 'vn', 'Asia/Saigon': 'vn', 'Asia/Kuala_Lumpur': 'my', 'Asia/Singapore': 'sg',
    'Asia/Jakarta': 'id', 'Asia/Makassar': 'id', 'Asia/Manila': 'ph', 'Asia/Tokyo': 'jp', 'Asia/Seoul': 'kr',
    'Asia/Taipei': 'tw', 'Asia/Hong_Kong': 'hk', 'Asia/Shanghai': 'cn', 'Australia/Sydney': 'au', 'Australia/Melbourne': 'au',
    'Australia/Brisbane': 'au', 'Australia/Perth': 'au', 'Australia/Adelaide': 'au', 'Pacific/Auckland': 'nz',
    'America/Toronto': 'ca', 'America/Vancouver': 'ca', 'America/Edmonton': 'ca', 'America/Winnipeg': 'ca',
    'America/Halifax': 'ca', 'America/Mexico_City': 'mx', 'America/Sao_Paulo': 'br', 'America/Argentina/Buenos_Aires': 'ar',
    'America/Buenos_Aires': 'ar', 'America/Santiago': 'cl', 'America/Bogota': 'co', 'America/Lima': 'pe'}
REGION_KEY = 'region'
ITUNES_GAP = float(os.environ.get('PODCASTS_ITUNES_GAP', '2.5'))  # seconds between iTunes API calls (~20/min allowed)
STALE_DAYS = 180       # a show with nothing new in half a year is not recommended
FOR_YOU_SHARE = 7      # of every 10 cards: 7 'for you', 3 popular/new
KIND_W = {'like': 1.0, 'yt': 0.8, 'guest': 0.6, 'author': 0.5}  # Apple 'You Might Also Like', YouTube related, recurring guest, same author
FOLLOW_W, HEARD_W, HEARD_CAP, SKIP_W, SKIP_CAP = 1.0, 0.25, 1.5, 0.05, 0.75
LIKE_W, DISLIKE_W, UNFOLLOW_W = 2.5, -3.0, -1.0
AVOID_GENRE_AT = -1.0
POOL_V = 2  # pool layout; a pool from an older release is rebuilt (cached sources keep that quick)
APPLE_UA = {'User-Agent': youtube.BROWSER_UA, 'Accept-Language': 'en-US,en;q=0.9'}
# YouTube categories -> Apple Podcasts genres, so channel taste and podcast taste share one vocabulary.
YT_GENRES = {'Science & Technology': 'Technology', 'Education': 'Education', 'News & Politics': 'News', 'Comedy': 'Comedy',
             'Entertainment': 'Society & Culture', 'People & Blogs': 'Society & Culture', 'Howto & Style': 'Education',
             'Sports': 'Sports', 'Gaming': 'Leisure', 'Film & Animation': 'TV & Film', 'Music': 'Music',
             'Autos & Vehicles': 'Leisure', 'Travel & Events': 'Leisure', 'Pets & Animals': 'Leisure',
             'Nonprofits & Activism': 'Government'}
NOT_GENRES = {'podcasts'}

_build_lock = threading.Lock()
_itunes_lock = threading.Lock()
_itunes_last = [0.0]
_state = {'building': False, 'stage': '', 'error': '', 'pool': None}
wake = threading.Event()


def now():
    return int(time.time())


def key(text):
    text = unicodedata.normalize('NFKC', text or '').casefold()
    return ''.join(ch for ch in text if unicodedata.category(ch)[0] in 'LN')


_TAIL = re.compile(r'\b(?:the\s+)?(?:official\s+)?(?:podcast|show|pod|channel)\b\s*$', re.I)


def title_key(title):
    """Same show on Apple and YouTube -> same key: 'The Diary Of A CEO with Steven Bartlett' / 'The Diary Of A CEO'."""
    t = re.split(r'\s+[-–—|:]\s+|\s+\(|\s+with\s+', (title or '').strip(), maxsplit=1)[0]
    t = re.sub(r'^the\s+', '', t, flags=re.I)
    t = _TAIL.sub('', t).strip()
    return key(t) or key(title)


# ------------------------------------------------------------------ fetch + cache

def fetch_bytes(url, cap=4 * MB, headers=None, allow=None):
    _s, body, _r = netfetch.get_bytes(url, cap, 'directory', headers=headers, timeout=20, allow=allow)
    return body


def itunes_json(url):
    """One iTunes Search/lookup call, spaced from the last one (Apple allows roughly 20 calls a minute)."""
    with _itunes_lock:
        wait = _itunes_last[0] + ITUNES_GAP - time.time()
        if wait > 0:
            time.sleep(wait)
        try:
            return json.loads(fetch_bytes(url, 8 * MB))
        finally:
            _itunes_last[0] = time.time()


def plain_json(url):
    try:
        return json.loads(fetch_bytes(url, 8 * MB))
    except FetchError:  # Apple's chart feeds time out now and then: one more try
        time.sleep(2)
        return json.loads(fetch_bytes(url, 8 * MB))


def cache_get(k, ttl):
    row = P.q('SELECT value, at FROM discover_cache WHERE key=?', (k,), one=True)
    if not row:
        return None, False
    return json.loads(row['value']), now() - row['at'] < ttl


def cache_put(k, value):
    P.write(lambda c: c.execute('INSERT INTO discover_cache(key, value, at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET '
                                'value=excluded.value, at=excluded.at', (k, json.dumps(value), now())))


def cached(k, ttl, fn):
    """fn() through the cache; a failed refresh keeps the last good answer, a failed first fetch returns None."""
    value, fresh = cache_get(k, ttl)
    if fresh:
        return value
    try:
        got = fn()
    except (FetchError, ValueError, KeyError, TypeError, OSError):
        return value
    cache_put(k, got)
    return got


# ------------------------------------------------------------------ sources

def _https(url):
    return url if isinstance(url, str) and url.startswith('https://') else ''


def _art600(url):
    return re.sub(r'/\d+x\d+bb\.(jpg|png|webp)$', r'/600x600bb.\1', url or '')


def _genres(item):
    names = item.get('genres') or []
    if names and isinstance(names[0], dict):
        names = [g.get('name') for g in names]
    out = [str(g) for g in names if isinstance(g, str) and g.casefold() not in NOT_GENRES]
    if not out and item.get('primaryGenreName'):
        out = [str(item['primaryGenreName'])]
    return out[:4]


def _genre_ids(item):
    names, ids = item.get('genres') or [], item.get('genreIds') or []
    if names and isinstance(names[0], dict):
        return {g.get('name'): str(g.get('id')) for g in names if g.get('name') and g.get('id')}
    return {n: str(i) for n, i in zip(names, ids) if isinstance(n, str) and n.casefold() not in NOT_GENRES}


def show_from_itunes(item):
    feed = item.get('feedUrl')
    if not isinstance(feed, str) or not feed.startswith(('http://', 'https://')):
        return None
    return {'feed_url': feed, 'source': 'rss', 'itunes_id': int(item.get('collectionId') or item.get('trackId') or 0),
            'title': str(item.get('collectionName') or item.get('trackName') or '')[:300],
            'author': str(item.get('artistName') or '')[:300],
            'artwork': _https(item.get('artworkUrl600') or _art600(item.get('artworkUrl100') or '')),
            'genres': _genres(item), 'genre_ids': _genre_ids(item), 'episodes': item.get('trackCount') or 0,
            'release': item.get('releaseDate') or ''}


def _ts(iso):
    try:
        return int(time.mktime(time.strptime(iso[:19], '%Y-%m-%dT%H:%M:%S')) - time.timezone)
    except (TypeError, ValueError):
        return 0


def itunes_details(ids):
    """{itunes id: show dict with 'latest' episode} for many ids: cached per id, missing ones looked up 100 at a time."""
    out, missing = {}, []
    for i in dict.fromkeys(int(x) for x in ids if str(x).isdigit()):
        v, fresh = cache_get(f'itunes:{i}', TTL)
        if fresh:
            if v:
                out[i] = v
        else:
            missing.append(i)
    for n in range(0, len(missing), 100):
        batch = missing[n:n + 100]
        try:
            data = itunes_json('https://itunes.apple.com/lookup?' + urlencode(
                {'id': ','.join(map(str, batch)), 'entity': 'podcastEpisode', 'limit': 1}))
        except (FetchError, ValueError):
            continue
        shows, latest = {}, {}
        for item in data.get('results', []):
            cid = int(item.get('collectionId') or 0)
            if item.get('wrapperType') == 'podcastEpisode' or item.get('kind') == 'podcast-episode':
                if cid not in latest or item.get('releaseDate', '') > latest[cid].get('releaseDate', ''):
                    latest[cid] = item
            elif item.get('kind') == 'podcast':
                s = show_from_itunes(item)
                if s:
                    shows[cid] = s
        for i in batch:
            s = shows.get(i)
            if s:
                e = latest.get(i)
                s['latest'] = {'title': str(e.get('trackName') or '')[:300], 'published': _ts(e.get('releaseDate')),
                               'url': _https(e.get('episodeUrl')) or (e.get('episodeUrl') if str(e.get('episodeUrl') or '').startswith('http://') else '')} if e else None
                out[i] = s
            cache_put(f'itunes:{i}', s or {})
    return out


def chart_ids(country):
    def go():
        d = plain_json(f'https://rss.applemarketingtools.com/api/v2/{country}/podcasts/top/100/podcasts.json')
        return [int(r['id']) for r in d['feed']['results'] if str(r.get('id', '')).isdigit()]
    return cached(f'chart:{country}', TTL, go) or []


def genre_top(gid, country='us'):
    """Apple's top shows in one genre (the iTunes top-podcasts RSS, as ids in chart order)."""
    def go():
        d = plain_json(f'https://itunes.apple.com/{country}/rss/toppodcasts/limit=50/genre={int(gid)}/json')
        entries = d['feed'].get('entry') or []
        if isinstance(entries, dict):
            entries = [entries]
        return [int(e['id']['attributes']['im:id']) for e in entries if str(e.get('id', {}).get('attributes', {}).get('im:id', '')).isdigit()]
    return cached(f'genre:{country}:{int(gid)}', TTL, go) or []


_SHELF = re.compile(r'aria-label="You Might Also Like"(.*?)(?:aria-label="(?!You Might)[^"]*"\s+aria-hidden|data-testid="section-container"|$)', re.S)


def parse_apple_related(page, own=0):
    m = _SHELF.search(page)
    if not m:
        return []
    ids = [int(x) for x in re.findall(r'/podcast/[^"/\s]*/id(\d+)', m.group(1))]
    return [i for i in dict.fromkeys(ids) if i != own][:15]


def apple_related(itunes_id):
    def go():
        page = fetch_bytes(f'https://podcasts.apple.com/us/podcast/id{int(itunes_id)}', 6 * MB, headers=APPLE_UA).decode('utf-8', 'replace')
        return parse_apple_related(page, int(itunes_id))
    return cached(f'apple-related:{int(itunes_id)}', TTL, go) or []


def _walk(o, name, out):
    if isinstance(o, dict):
        if name in o:
            out.append(o[name])
        for v in o.values():
            _walk(v, name, out)
    elif isinstance(o, list):
        for v in o:
            _walk(v, name, out)


def _secs(text):
    parts = [p for p in re.findall(r'\d+', text or '')][-3:]
    s = 0
    for p in parts:
        s = s * 60 + int(p)
    return s if re.fullmatch(r'\d+(:\d\d){1,2}', (text or '').strip()) else 0


def parse_youtube_related(page, own=''):
    """Channels YouTube pairs with a video (its related column): [{channel_id, title, artwork}] in order, long videos only."""
    m = re.search(r'var ytInitialData = (\{.*?\});</script>', page, re.S)
    if not m:
        return {'channels': [], 'category': ''}
    cat = re.search(r'"category":"([^"]+)"', page)
    data = json.loads(m.group(1))
    locks = []
    _walk(data, 'lockupViewModel', locks)
    out, seen = [], set()
    for lk in locks:
        blob = json.dumps(lk.get('metadata') or {})
        cid = re.search(r'"browseId": "(UC[0-9A-Za-z_-]{22})"', blob)
        name = re.search(r'"a11yLabel": "Go to channel ([^"]+)"', blob)
        if not cid or not name or cid.group(1) == own or cid.group(1) in seen:
            continue
        badges = re.findall(r'"thumbnailBadgeViewModel": \{"text": "([^"]+)"', json.dumps(lk.get('contentImage') or {}))
        if badges and max(_secs(b) for b in badges) < 8 * 60:
            continue  # clips and shorts are not shows
        art = re.search(r'"avatarViewModel": \{"image": \{"sources": \[\{"url": "([^"]+)"', blob)
        art = art.group(1) if art else ''
        art = re.sub(r'=s\d+-', '=s240-', art)
        seen.add(cid.group(1))
        out.append({'channel_id': cid.group(1), 'title': json.loads('"' + name.group(1) + '"')[:300],
                    'artwork': art if art.startswith('https://') and youtube.host_allowed(art.split('/')[2]) else ''})
    return {'channels': out[:15], 'category': json.loads('"' + cat.group(1) + '"') if cat else ''}


def youtube_related(video_id):
    def go():
        page = fetch_bytes('https://www.youtube.com/watch?v=' + video_id, youtube.MAX_PAGE_BYTES, headers=youtube.PAGE_HEADERS,
                           allow=youtube.host_allowed).decode('utf-8', 'replace')
        return parse_youtube_related(page, '')
    return cached(f'yt-related:{video_id}', TTL, go) or {'channels': [], 'category': ''}


_AGO = re.compile(r'(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago', re.I)
_UNIT = {'second': 1, 'minute': 60, 'hour': 3600, 'day': 86400, 'week': 7 * 86400, 'month': 30 * 86400, 'year': 365 * 86400}


def ago_to_ts(text, now_ts=None):
    m = _AGO.search(text or '')
    if not m:
        return 0
    return (now_ts or now()) - int(m.group(1)) * _UNIT[m.group(2).lower()]


def parse_channel_videos(page):
    """A channel's Videos tab (newest first, long-form only): the first upload as {'title', 'published', 'url'}."""
    m = re.search(r'var ytInitialData = (\{.*?\});</script>', page, re.S)
    if not m:
        return {}
    locks = []
    _walk(json.loads(m.group(1)), 'lockupViewModel', locks)
    for lk in locks:
        vid = lk.get('contentId') or ''
        if not youtube.VIDEO_ID.match(vid):
            continue
        meta = (lk.get('metadata') or {}).get('lockupMetadataViewModel') or {}
        title = ((meta.get('title') or {}).get('content') or '')[:300]
        texts = re.findall(r'"content": "([^"]*ago)"', json.dumps(meta.get('metadata') or {}))
        return {'title': title, 'published': ago_to_ts(texts[0] if texts else ''), 'url': youtube.WATCH + vid, 'approx': True}
    return {}


def youtube_latest(channel_id):
    """Newest long-form upload of a channel: {'title', 'published', 'url'} or None (no long-form uploads). The public
    uploads feed gives the exact time; when YouTube answers it with an error, the channel's Videos tab is read instead."""
    def go():
        try:
            parsed, _r = P._fetch_feed(youtube.feed_url(channel_id))
            eps = sorted((parsed or {}).get('episodes') or [], key=lambda e: e['published'], reverse=True)
            if eps:
                e = eps[0]
                return {'title': e['title'][:300], 'published': e['published'], 'url': e['url']}
        except (FetchError, feeds.FeedError):
            pass
        page = fetch_bytes(youtube.channel_url(channel_id) + '/videos', youtube.MAX_PAGE_BYTES, headers=youtube.PAGE_HEADERS,
                           allow=youtube.host_allowed).decode('utf-8', 'replace')
        return parse_channel_videos(page)
    k = f'yt-latest:{channel_id}'
    v, fresh = cache_get(k, TTL)
    if fresh and (v or now() - (P.q('SELECT at FROM discover_cache WHERE key=?', (k,), one=True) or {'at': 0})['at'] < 2 * 3600):
        return v or None  # a channel with nothing found is asked again after 2 h, not 12
    try:
        v = go()
    except (FetchError, ValueError, KeyError, TypeError, OSError):
        return v or None
    cache_put(k, v)
    return v or None


def itunes_search(term, attribute=None, limit=8):
    k = f'search:{attribute or ""}:{key(term)}'

    def go():
        qs = {'term': term, 'media': 'podcast', 'entity': 'podcast', 'limit': limit}
        if attribute:
            qs['attribute'] = attribute
        d = itunes_json('https://itunes.apple.com/search?' + urlencode(qs))
        return [s for s in (show_from_itunes(i) for i in d.get('results', [])) if s]
    return cached(k, TTL, go) or []


# ------------------------------------------------------------------ guests in episode titles

_NAME = r"([A-Z][a-zA-Z'’.\-]+(?:\s+(?:[A-Z][a-zA-Z'’.\-]+|de|van|von|da|bin|al)){1,3})"
GUEST_PATTERNS = [
    re.compile(r'^#?\d+\s*[–—-]\s*' + _NAME + r'\s*(?::|$)'),         # 'No.501 – DHH: ...', 'No.2572 - Colum Tyrrell' (written with a hash)
    re.compile(r'^#\d+:?\s+' + _NAME + r'\s+[–—-]\s'),                 # 'No.352 Cain Velasquez - ...'
    re.compile(r'^' + _NAME + r'\s*[:|│]\s'),                            # 'Andrew Wilson: ...', 'Derek Paravicini | ...'
    re.compile(r'\s(?:with|ft\.?|feat\.?)\s+' + _NAME + r'\s*(?:[|│(]|$)'),
    re.compile(r'[|│]\s*' + _NAME + r'\s*$'),                            # '... | Henry Shukman'
    re.compile(r'\(' + _NAME + r'\)\s*$'),                               # '... (Heather Schmid)'
    re.compile(r'^' + _NAME + r'\s+on\s+[A-Z]'),                         # 'Jeffrey Sachs on Netanyahu ...'
    re.compile(r'[–—]\s+' + _NAME + r'\s+(?:on|of)\s'),                  # 'No.884: ... — Kevin Ryan of DoubleClick'
]
NOT_NAMES = {'the', 'how', 'why', 'what', 'when', 'who', 'is', 'are', 'can', 'my', 'your', 'our', 'this', 'that', 'live',
             'full', 'episode', 'part', 'show', 'global', 'emergency', 'random', 'q', 'a', 'ai', 'ex', 'cia', 'tim', 'new',
             'best', 'top', 'ep', 'bonus', 'special', 'mma', 'jre', 'ufc', 'nfl', 'nba', 'us', 'world', 'american', 'roman'}


def guests(title):
    out = []
    for pat in GUEST_PATTERNS:
        for m in pat.finditer(title or ''):
            name = m.group(1).strip(" .'’-")
            words = name.split()
            if len(words) < 2 or words[0].casefold() in NOT_NAMES or words[-1].casefold() in NOT_NAMES:
                continue
            if any(w.isupper() and len(w) > 3 for w in words):
                continue  # shouting headline words, not a name
            out.append(name)
    return list(dict.fromkeys(out))


def guest_show(name, results):
    """The guest's own show among search results: hosted by that person (the name is the show's author), or the top
    result named after them with a real back catalogue ('Huberman Lab' for Andrew Huberman). None when unsure."""
    k, last = key(name), key(name.split()[-1])
    for i, r in enumerate(results):
        if len(r['title']) > 70 or re.search(r'\b(?:fan|summary|summaries|clips|unofficial)\b', r['title'], re.I):
            continue
        if k in key(r['author']):
            return r
        if i == 0 and len(last) >= 4 and title_key(r['title']).startswith(last) and (r.get('episodes') or 0) >= 50:
            return r
    return None


def guest_counts(titles_by_seed):
    """{seed feed_url: [titles]} -> [(name, [seeds it appears in], count)] sorted by spread then count."""
    seen = {}
    for seed, titles in titles_by_seed.items():
        for t in titles:
            for name in guests(t):
                g = seen.setdefault(key(name), {'name': name, 'seeds': [], 'count': 0})
                g['count'] += 1
                if seed not in g['seeds']:
                    g['seeds'].append(seed)
    return sorted(seen.values(), key=lambda g: (-len(g['seeds']), -g['count'], g['name']))


# ------------------------------------------------------------------ taste (signals) and seeds

def _json_list(v):
    try:
        x = json.loads(v or '[]')
        return x if isinstance(x, list) else []
    except ValueError:
        return []


def seeds():
    """Every show that says something about the owner's taste, with its weight:
    follow +1, each episode heard through +0.25 (max +1.5), each skipped -0.05 (max -0.75), thumbs up +2.5,
    thumbs down -3 (and blocked), unfollowed -1."""
    meta = {r['feed_url']: r for r in P.q('SELECT * FROM show_meta')}
    stats = {r['show_id']: r for r in P.q('SELECT show_id, SUM(played=1) heard, SUM(skipped=1 AND played=0) skipped FROM episodes GROUP BY show_id')}
    out = {}
    for s in P.q('SELECT id, feed_url, title, author, art_file FROM shows'):
        st = stats.get(s['id'])
        heard, skipped = (st['heard'] or 0, st['skipped'] or 0) if st else (0, 0)
        m = meta.get(s['feed_url'])
        out[s['feed_url']] = {'feed_url': s['feed_url'], 'title': s['title'], 'author': s['author'], 'followed': True,
                              'show_id': s['id'], 'source': 'youtube' if youtube.is_feed(s['feed_url']) else 'rss',
                              'itunes_id': m['itunes_id'] if m else 0, 'genres': _json_list(m['genres']) if m else [],
                              'channel_id': _channel(s['feed_url']), 'thumb': 0, 'heard': heard, 'skipped': skipped,
                              'w': FOLLOW_W + min(HEARD_CAP, HEARD_W * heard) - min(SKIP_CAP, SKIP_W * skipped)}
    for r in P.q('SELECT * FROM unfollows'):
        if r['feed_url'] not in out:
            out[r['feed_url']] = {'feed_url': r['feed_url'], 'title': r['title'], 'author': '', 'followed': False,
                                  'source': 'youtube' if youtube.is_feed(r['feed_url']) else 'rss', 'itunes_id': r['itunes_id'] or 0,
                                  'genres': _json_list(r['genres']), 'channel_id': _channel(r['feed_url']), 'thumb': 0,
                                  'unfollowed': True, 'w': UNFOLLOW_W}
    for r in P.q('SELECT * FROM taste'):
        s = out.get(r['feed_url'])
        if s is None:
            s = out[r['feed_url']] = {'feed_url': r['feed_url'], 'title': r['title'], 'author': r['author'], 'followed': False,
                                      'source': 'youtube' if youtube.is_feed(r['feed_url']) else 'rss', 'itunes_id': r['itunes_id'] or 0,
                                      'genres': _json_list(r['genres']), 'channel_id': r['channel_id'] or _channel(r['feed_url']), 'w': 0.0}
        s['thumb'] = r['value']
        if r['value'] > 0:
            s['w'] = max(s['w'], 0.0) + LIKE_W
        elif r['value'] < 0:
            s['w'] = DISLIKE_W
        if not s.get('genres'):
            s['genres'] = _json_list(r['genres'])
        if not s.get('itunes_id'):
            s['itunes_id'] = r['itunes_id'] or 0
    return out


def _channel(feed_url):
    if not youtube.is_feed(feed_url):
        return ''
    ref = youtube.parse_ref(feed_url)
    return (ref or {}).get('channel_id', '')


def genre_affinity(seed_map):
    g = {}
    for s in seed_map.values():
        gs = s.get('genres') or []
        for name in gs:
            g[name] = g.get(name, 0.0) + s['w'] / len(gs)
    return g


# ------------------------------------------------------------------ matching followed shows to the directories

def match_show(s):
    """Find a followed show's Apple Podcasts id + genres (RSS: same feed URL or same title+author; YouTube: same title).
    YouTube shows also get a genre from their newest upload's YouTube category. Cached in show_meta for 30 days."""
    row = P.q('SELECT * FROM show_meta WHERE feed_url=?', (s['feed_url'],), one=True)
    if row and now() - row['at'] < MATCH_TTL and (row['itunes_id'] or row['genres'] != '[]'):
        return
    itunes_id, genres, gids = 0, [], {}
    try:
        results = itunes_search(s['title'], limit=10)
    except Exception:
        results = []
    tk, ak = title_key(s['title']), key(s.get('author'))
    for r in results:
        same_feed = r['feed_url'].rstrip('/') == s['feed_url'].rstrip('/')
        same_name = title_key(r['title']) == tk and (s['source'] == 'youtube' or not ak or key(r['author']) == ak or ak in key(r['author']))
        if same_feed or same_name:
            itunes_id, genres, gids = r['itunes_id'], r['genres'], r.get('genre_ids', {})
            break
    if s['source'] == 'youtube' and not genres:
        vid = latest_videos(s.get('show_id'), 1)
        if vid:
            cat = youtube_related(vid[0]).get('category') or ''
            if YT_GENRES.get(cat):
                genres = [YT_GENRES[cat]]
    P.write(lambda c: c.execute('INSERT INTO show_meta(feed_url, itunes_id, genres, genre_ids, at) VALUES(?,?,?,?,?) ON CONFLICT(feed_url) DO UPDATE SET '
                                'itunes_id=excluded.itunes_id, genres=excluded.genres, genre_ids=excluded.genre_ids, at=excluded.at',
                                (s['feed_url'], itunes_id, json.dumps(genres), json.dumps(gids), now())))


def latest_videos(show_id, n=2):
    if not show_id:
        return []
    out = []
    for r in P.q('SELECT enclosure_url FROM episodes WHERE show_id=? ORDER BY published DESC LIMIT 6', (show_id,)):
        v = youtube.video_id(r['enclosure_url'])
        if v:
            out.append(v)
        if len(out) >= n:
            break
    return out


# ------------------------------------------------------------------ building the pool

def _empty_pool():
    return {'built_at': 0, 'shows': {}, 'genre_ids': {}}


def load_pool():
    if _state['pool'] is None:
        v, _fresh = cache_get('pool', 10 ** 9)
        _state['pool'] = v or _empty_pool()
    return _state['pool']


def save_pool(pool):
    _state['pool'] = pool
    cache_put('pool', pool)


def _add(pool, show):
    cur = pool['shows'].get(show['feed_url'])
    if cur is None:
        cur = pool['shows'][show['feed_url']] = dict(show, edges=[], pop=[])
        cur.pop('genre_ids', None)
    else:
        for k in ('title', 'author', 'artwork', 'genres', 'latest', 'itunes_id', 'channel_id'):
            if show.get(k) and not cur.get(k):
                cur[k] = show[k]
        if 'latest' in show and show['latest'] is not None:
            cur['latest'] = show['latest']
    for name, gid in (show.get('genre_ids') or {}).items():
        pool['genre_ids'].setdefault(name, gid)
    return cur


def _edge(cand, seed, kind):
    e = [seed, kind]
    if e not in cand['edges']:
        cand['edges'].append(e)


def _pop(cand, kind, label, rank):
    p = [kind, label, rank]
    if not any(x[0] == kind and x[1] == label for x in cand['pop']):
        cand['pop'].append(p)


def _youtube_cand(ch):
    cid = ch['channel_id']
    return {'feed_url': youtube.feed_url(cid), 'source': 'youtube', 'channel_id': cid, 'itunes_id': 0, 'title': ch['title'],
            'author': ch['title'], 'artwork': ch.get('artwork') or '', 'genres': [], 'latest': None}


def build(force=False):
    """(Re)build the candidate pool. Runs in the background; saves after each stage so Discover fills in early."""
    if not _build_lock.acquire(blocking=False):
        return load_pool()
    _state['building'] = True
    _state['error'] = ''
    try:
        old = load_pool()
        if not force and not due(old):
            return old
        pool = _empty_pool()
        pool['built_at'] = 0
        pool['genre_ids'] = dict(old.get('genre_ids') or {})
        # 1. charts (two calls + one batched lookup): something to show within seconds
        _state['stage'] = 'charts'
        chart = {}
        for c in countries():
            for rank, i in enumerate(chart_ids(c)):
                chart.setdefault(i, []).append((c, rank))
        for i, s in itunes_details(list(chart)).items():
            cand = _add(pool, s)
            for c, rank in chart[i]:
                _pop(cand, 'chart', c, rank)
        missing = [] if chart else ['charts']
        if not old['shows']:
            save_pool(dict(pool, built_at=0))
        # 2. what the owner's shows are (Apple id, genres)
        _state['stage'] = 'match'
        sm = seeds()
        for s in sm.values():
            if s.get('followed'):
                match_show(s)
        sm = seeds()
        for s in sm.values():
            for name, gid in (_json_dict(P.q('SELECT genre_ids FROM show_meta WHERE feed_url=?', (s['feed_url'],), one=True))).items():
                pool['genre_ids'].setdefault(name, gid)
        # 3. similar shows: Apple's "You Might Also Like" and YouTube's related channels, for every seed (+ and -)
        _state['stage'] = 'related'
        related_ids = {}
        with ThreadPoolExecutor(3) as ex:
            apple = {s['feed_url']: ex.submit(apple_related, s['itunes_id']) for s in sm.values() if s.get('itunes_id')}
            yt = {}
            for s in sm.values():
                if s['source'] == 'youtube':
                    vids = latest_videos(s.get('show_id'), 2) if s.get('followed') else _cand_video(old, s['feed_url'])
                    yt[s['feed_url']] = [ex.submit(youtube_related, v) for v in vids]
            for seed, f in apple.items():
                for i in f.result() or []:
                    related_ids.setdefault(i, []).append(seed)
            for seed, fs in yt.items():
                own = sm[seed].get('channel_id')
                for f in fs:
                    for ch in (f.result() or {}).get('channels', []):
                        if ch['channel_id'] == own:
                            continue
                        _edge(_add(pool, _youtube_cand(ch)), seed, 'yt')
        for i, s in itunes_details(list(related_ids)).items():
            cand = _add(pool, s)
            for seed in related_ids[i]:
                _edge(cand, seed, 'like')
        if not old['shows']:
            save_pool(dict(pool, built_at=0))
        # 4. other shows by the same authors; 5. guests who recur across his shows (their own shows)
        _state['stage'] = 'authors'
        pos = [s for s in sm.values() if s['w'] > 0]
        for s in pos:
            a = (s.get('author') or '').strip()
            if s['source'] != 'rss' or not a or key(a) == key(s['title']) or len(key(a)) < 4:
                continue
            for r in itunes_search(a, 'artistTerm', 10):
                if key(r['author']) == key(a) and title_key(r['title']) != title_key(s['title']):
                    _edge(_add(pool, r), s['feed_url'], 'author')
        _state['stage'] = 'guests'
        titles = {}
        for s in pos:
            if s.get('show_id'):
                titles[s['feed_url']] = [r['title'] for r in P.q('SELECT title FROM episodes WHERE show_id=? ORDER BY published DESC LIMIT 80', (s['show_id'],))]
        followed_keys = {title_key(s['title']) for s in sm.values() if s.get('followed')}
        hosts = {key(s.get('author')) for s in sm.values() if s.get('followed')} | {key(s['title']) for s in sm.values() if s.get('followed')}
        picked = [g for g in guest_counts(titles) if not any(key(g['name']) in h for h in hosts if h)]
        for g in picked[:14]:
            r = guest_show(g['name'], itunes_search(g['name'], None, 5))
            if r and title_key(r['title']) not in followed_keys:
                seed = max(g['seeds'], key=lambda f: sm[f]['w'])
                _edge(_add(pool, r), seed, 'guest')
        # 6. popular in the genres he likes most
        _state['stage'] = 'genres'
        aff = genre_affinity(sm)
        for name, score in sorted(aff.items(), key=lambda kv: -kv[1])[:6]:
            gid = pool['genre_ids'].get(name)
            if score <= 0 or not gid:
                continue
            ids = genre_top(gid)
            details = itunes_details(ids)
            for rank, i in enumerate(ids):
                if i in details:
                    _pop(_add(pool, details[i]), 'genre', name, rank)
        # latest episodes for shows found through searches (lookups are cached per id)
        _state['stage'] = 'latest'
        need = [c['itunes_id'] for c in pool['shows'].values() if c['source'] == 'rss' and c.get('itunes_id') and 'latest' not in c]
        for i, s in itunes_details(need).items():
            cand = pool['shows'].get(s['feed_url'])
            if cand is not None:
                cand['latest'] = s.get('latest')
        pool['shows'] = dedupe(pool['shows'])
        pool['built_at'] = now()
        pool['retry_at'] = now() + 3600 if missing else 0  # a source that failed is tried again within the hour
        pool['v'] = POOL_V
        save_pool(pool)
        return pool
    except Exception as e:  # never let the worker die; keep the last pool
        _state['error'] = f'{e.__class__.__name__}: {e}'[:300]
        return load_pool()
    finally:
        _state['building'] = False
        _state['stage'] = ''
        _build_lock.release()


def countries():
    """Chart storefronts for 'Popular in …': the override, else the US plus the owner's own country when known."""
    if COUNTRIES_ENV:
        return COUNTRIES_ENV
    region = cache_get(REGION_KEY, 10 ** 12)[0]
    return ['us'] + ([region] if region in COUNTRY_NAMES and region != 'us' else [])


def note_timezone(tz):
    """The screen reports its time zone (Intl, e.g. 'Asia/Bangkok'); a new country rebuilds the charts soon.
    Zones that name no country (UTC, unknown) change nothing."""
    cc = TZ_COUNTRY.get(str(tz or '').strip()[:64])
    if not cc or cache_get(REGION_KEY, 10 ** 12)[0] == cc:
        return False
    cache_put(REGION_KEY, cc)
    if not COUNTRIES_ENV:
        pool = load_pool()
        if pool.get('built_at'):
            save_pool(dict(pool, retry_at=now()))
        wake.set()
    return True


def due(pool):
    """The pool needs a rebuild: never built, older than the cache time, or a source failed last time and an hour passed."""
    if not pool.get('built_at') or now() - pool['built_at'] >= TTL or pool.get('v') != POOL_V:
        return True
    return bool(pool.get('retry_at')) and now() >= pool['retry_at']


def _json_dict(row):
    try:
        v = json.loads(row['genre_ids'] or '{}') if row else {}
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def _cand_video(pool, feed_url):
    c = (pool.get('shows') or {}).get(feed_url) or {}
    v = youtube.video_id((c.get('latest') or {}).get('url') or '')
    return [v] if v else []


def dedupe(shows):
    """One card per show: a YouTube channel with the same name as a podcast folds into the podcast (RSS kept)."""
    by_key = {}
    for c in sorted(shows.values(), key=lambda c: (c['source'] != 'rss', c['feed_url'])):
        k = title_key(c['title'])
        cur = by_key.get(k)
        if cur is None:
            by_key[k] = c
            continue
        for e in c['edges']:
            _edge(cur, *e)
        for p in c['pop']:
            _pop(cur, *p)
    return {c['feed_url']: c for c in by_key.values()}


def expand(feed_url):
    """A new thumbs up/down: learn that show's own similar shows now (one or two fetches), so the next page reflects it."""
    pool = load_pool()
    sm = seeds()
    s = sm.get(feed_url)
    if not s:
        return
    c = pool['shows'].get(feed_url) or {}
    changed = False
    if s.get('itunes_id') or c.get('itunes_id'):
        iid = s.get('itunes_id') or c.get('itunes_id')
        ids = apple_related(iid)
        details = itunes_details(ids)
        for i in ids:
            if i in details:
                _edge(_add(pool, details[i]), feed_url, 'like')
                changed = True
    elif s['source'] == 'youtube':
        vids = latest_videos(s.get('show_id'), 2) if s.get('followed') else _cand_video(pool, feed_url)
        for v in vids:
            for ch in youtube_related(v).get('channels', []):
                if ch['channel_id'] != s.get('channel_id'):
                    _edge(_add(pool, _youtube_cand(ch)), feed_url, 'yt')
                    changed = True
    if changed:
        pool['shows'] = dedupe(pool['shows'])
        save_pool(pool)


# ------------------------------------------------------------------ ranking

def _seed_label(s):
    return s.get('title') or ''


def rank(pool=None, seed_map=None, now_ts=None):
    """The whole ranked list (cheap, local): ~70% 'for you' (similar to what he likes) and ~30% popular, never a followed
    or thumbed-down show, never a show from a genre he avoids unless something he likes points to it."""
    pool = pool if pool is not None else load_pool()
    sm = seed_map if seed_map is not None else seeds()
    now_ts = now_ts or now()
    aff = genre_affinity(sm)
    top_aff = max([v for v in aff.values() if v > 0], default=1.0)
    followed = {f for f, s in sm.items() if s.get('followed')}
    blocked = {f for f, s in sm.items() if s.get('thumb', 0) < 0 or (s.get('unfollowed') and s.get('thumb', 0) <= 0)}  # he let these go
    no_keys = {title_key(s['title']) for f, s in sm.items() if s.get('followed') or s.get('thumb', 0) < 0}
    no_ids = {s['itunes_id'] for s in sm.values() if (s.get('followed') or s.get('thumb', 0) < 0) and s.get('itunes_id')}
    no_ch = {s['channel_id'] for s in sm.values() if (s.get('followed') or s.get('thumb', 0) < 0) and s.get('channel_id')}
    for_you, popular = [], []
    for c in pool.get('shows', {}).values():
        f = c['feed_url']
        if f in followed or f in blocked or title_key(c['title']) in no_keys:
            continue
        if (c.get('itunes_id') and c['itunes_id'] in no_ids) or (c.get('channel_id') and c['channel_id'] in no_ch):
            continue
        if re.search(r'\bclips?\b|\bhighlights\b|\bshorts\b', c['title'] or '', re.I):
            continue
        lt = c.get('latest')
        if c['source'] == 'rss':
            if not lt or not lt.get('published') or now_ts - lt['published'] > STALE_DAYS * 86400:
                continue
        contrib = []
        for seed, kind in c['edges']:
            s = sm.get(seed)
            if s:
                contrib.append((s['w'] * KIND_W.get(kind, 0.5), kind, s))
        gs = c.get('genres') or []
        if not gs:  # a channel's genre: what the shows that point to it are
            gs = list(dict.fromkeys(g for w, _k, s in contrib if w > 0 for g in (s.get('genres') or [])))[:3]
        ga = sum(aff.get(g, 0.0) for g in gs) / len(gs) / top_aff if gs else 0.0
        fy = sum(x[0] for x in contrib)
        best = max(contrib, key=lambda x: x[0], default=None)
        avoided = bool(c.get('genres')) and any(aff.get(g, 0.0) <= AVOID_GENRE_AT for g in c['genres'])
        if fy > 0 and best and best[0] > 0:
            if avoided and fy < 1.5:
                continue
            kind, s = best[1], best[2]
            reason = ('Guest on ' if kind == 'guest' else 'More from ' if kind == 'author' else 'Like ') + (
                (s.get('author') or s['title']) if kind == 'author' else _seed_label(s))
            for_you.append((fy + 0.5 * ga, c, reason, 'for_you'))
            continue
        if fy < 0 or avoided or not c['pop']:
            continue
        best_pop, reason = 0.0, ''
        for kind, label, r in c['pop']:
            v = (1 - r / 100) if kind == 'chart' else (1 - r / 50) * 1.1
            if v > best_pop:
                best_pop = v
                reason = 'Popular in ' + (COUNTRY_NAMES.get(label, label.upper()) if kind == 'chart' else label)
        popular.append((best_pop * (1 + 0.4 * max(ga, 0.0)), c, reason, 'popular'))
    for_you = diversify(sorted(for_you, key=lambda x: (-x[0], x[1]['title'])))
    popular = diversify(sorted(popular, key=lambda x: (-x[0], x[1]['title'])))
    out, i, a, b = [], 0, 0, 0
    while a < len(for_you) or b < len(popular):
        want_pop = (i % 10) in (3, 6, 9)
        if (want_pop and b < len(popular)) or a >= len(for_you):
            out.append(popular[b])
            b += 1
        else:
            out.append(for_you[a])
            a += 1
        i += 1
    return out


def diversify(entries, fade=0.75):
    """Greedy re-order so one reason ('Like Lex Fridman Podcast') never fills the screen: each further card with a
    reason already shown counts fade^n of its score."""
    used, out, left = {}, [], list(entries)
    while left:
        best = max(range(len(left)), key=lambda i: (left[i][0] * fade ** used.get(left[i][2], 0), -i))
        e = left.pop(best)
        used[e[2]] = used.get(e[2], 0) + 1
        out.append(e)
    return out


def card(entry, taste_value=0):
    score, c, reason, kind = entry
    lt = c.get('latest') or None
    return {'feed_url': c['feed_url'], 'title': c['title'], 'author': c.get('author') or '', 'artwork': c.get('artwork') or '',
            'source': c['source'], 'genres': c.get('genres') or [], 'reason': reason, 'kind': kind, 'score': round(score, 3),
            'taste': taste_value,
            'latest': {'title': lt.get('title') or '', 'published': lt.get('published') or 0, 'approx': bool(lt.get('approx')),
                       'playable': bool(lt.get('url'))} if lt else None}


def discover(limit=20, cursor=None):
    try:
        limit = max(1, min(int(limit or 20), 50))
        start = max(0, int(cursor or 0))
    except (TypeError, ValueError):
        raise P.Problem(400, 'limit and cursor must be whole numbers.') from None
    pool = load_pool()
    if due(pool):
        wake.set()
    ranked = rank(pool)
    tastes = {r['feed_url']: r['value'] for r in P.q('SELECT feed_url, value FROM taste')}
    out, i, changed = [], start, False
    while i < len(ranked) and len(out) < limit:
        batch = ranked[i:i + (limit - len(out))]
        i += len(batch)
        need = [e[1] for e in batch if e[1]['source'] == 'youtube' and not e[1].get('latest')]
        if need:  # newest upload of each YouTube channel on this page (cached; a channel with none is dropped)
            with ThreadPoolExecutor(4) as ex:
                got = list(ex.map(lambda c: youtube_latest(c['channel_id']), need))
            for c, lt in zip(need, got):
                c['latest'] = lt or {}
            changed = True
        for e in batch:
            lt = e[1].get('latest')
            if e[1]['source'] == 'youtube' and (not lt or not lt.get('url') or (lt.get('published') and now() - lt['published'] > STALE_DAYS * 86400)):
                continue
            out.append(card(e, tastes.get(e[1]['feed_url'], 0)))
    if changed:
        save_pool(pool)
    return {'items': out, 'next_cursor': str(i) if i < len(ranked) else None, 'total': len(ranked),
            'building': _state['building'] or not pool['built_at'], 'built_at': pool['built_at'] or None}


# ------------------------------------------------------------------ taste writes and summary

def set_taste(feed_url=None, show_id=None, value=None):
    if isinstance(value, bool) or value not in (1, -1, 0):
        raise P.Problem(400, 'value must be 1 (thumbs up), -1 (thumbs down) or 0 (clear).')
    if show_id not in (None, ''):
        if isinstance(show_id, bool) or not isinstance(show_id, int):
            try:
                show_id = int(show_id)
            except (TypeError, ValueError):
                raise P.Problem(400, 'show_id must be a whole number.') from None
        row = P.q('SELECT feed_url FROM shows WHERE id=?', (show_id,), one=True)
        if not row:
            raise P.Problem(404, 'Show not found.')
        feed_url = row['feed_url']
    if not isinstance(feed_url, str) or not feed_url.startswith(('http://', 'https://')) or len(feed_url) > 2000:
        raise P.Problem(400, 'Send feed_url (a show from Discover or one you follow) or show_id.')
    if value == 0:
        P.write(lambda c: c.execute('DELETE FROM taste WHERE feed_url=?', (feed_url,)))
    else:
        info = _show_info(feed_url)
        if info is None:
            raise P.Problem(404, 'That show is not in Discover or your shows.')
        P.write(lambda c: c.execute(
            'INSERT INTO taste(feed_url, value, title, author, artwork, genres, itunes_id, channel_id, at) VALUES(?,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(feed_url) DO UPDATE SET value=excluded.value, at=excluded.at',
            (feed_url, value, info['title'], info.get('author') or '', info.get('artwork') or '', json.dumps(info.get('genres') or []),
             info.get('itunes_id') or 0, info.get('channel_id') or '', now())))
        threading.Thread(target=_quiet_expand, args=(feed_url,), daemon=True).start()
    return {'feed_url': feed_url, 'value': value, 'taste': summary()}


def _quiet_expand(feed_url):
    try:
        expand(feed_url)
    except Exception:
        pass


def _show_info(feed_url):
    row = P.q('SELECT s.title, s.author, m.itunes_id, m.genres FROM shows s LEFT JOIN show_meta m ON m.feed_url=s.feed_url WHERE s.feed_url=?',
              (feed_url,), one=True)
    if row:
        return {'title': row['title'], 'author': row['author'], 'itunes_id': row['itunes_id'] or 0,
                'genres': _json_list(row['genres']), 'channel_id': _channel(feed_url)}
    c = load_pool()['shows'].get(feed_url)
    if c:
        return c
    t = P.q('SELECT * FROM taste WHERE feed_url=?', (feed_url,), one=True)
    if t:
        return {'title': t['title'], 'author': t['author'], 'artwork': t['artwork'], 'genres': _json_list(t['genres']),
                'itunes_id': t['itunes_id'], 'channel_id': t['channel_id']}
    return None


def summary(limit=8):
    sm = seeds()
    aff = genre_affinity(sm)
    liked = sorted((s for s in sm.values() if s['w'] > 0), key=lambda s: -s['w'])
    return {
        'liked': [{'title': s['title'], 'feed_url': s['feed_url'], 'weight': round(s['w'], 2),
                   'why': [w for w in ('thumbs up' if s.get('thumb', 0) > 0 else '', 'following' if s.get('followed') else '',
                                       f"{s['heard']} heard" if s.get('heard') else '') if w]} for s in liked[:limit]],
        'avoided_shows': [{'title': s['title'], 'feed_url': s['feed_url'], 'why': 'thumbs down' if s.get('thumb', 0) < 0 else 'unfollowed'}
                          for s in sm.values() if s['w'] < 0],
        'liked_genres': [g for g, v in sorted(aff.items(), key=lambda kv: -kv[1]) if v > 0][:limit],
        'avoided_genres': [g for g, v in sorted(aff.items(), key=lambda kv: kv[1]) if v <= AVOID_GENRE_AT][:limit],
        'thumbs': {'up': sum(1 for s in sm.values() if s.get('thumb', 0) > 0), 'down': sum(1 for s in sm.values() if s.get('thumb', 0) < 0)},
    }


# ------------------------------------------------------------------ preview (latest episode of a show he does not follow)

def preview_source(feed_url):
    """-> {'url', 'title', 'show', 'artwork', 'published'} for a Discover card's newest episode."""
    c = load_pool()['shows'].get(feed_url)
    if not c:
        raise P.Problem(404, 'That show is not in Discover right now.')
    lt = c.get('latest') or {}
    if c['source'] == 'youtube' and not lt.get('url'):
        lt = youtube_latest(c['channel_id']) or {}
    if not lt.get('url'):
        raise P.Problem(404, 'This show has no episode to preview.')
    return {'url': lt['url'], 'title': lt.get('title') or '', 'show': c['title'], 'artwork': c.get('artwork') or '',
            'published': lt.get('published') or 0}


# ------------------------------------------------------------------ background

def _loop():
    time.sleep(int(os.environ.get('PODCASTS_DISCOVER_DELAY', '20')))
    while True:
        try:
            if due(load_pool()):
                build()
        except Exception:
            pass
        wake.wait(3600)
        wake.clear()


_started = False


def start():
    global _started
    if _started or os.environ.get('PODCASTS_DISCOVER', '1') == '0':
        return
    _started = True
    threading.Thread(target=_loop, name='discover', daemon=True).start()


def status():
    pool = load_pool()
    return {'building': _state['building'], 'stage': _state['stage'], 'error': _state['error'] or None,
            'built_at': pool['built_at'] or None, 'candidates': len(pool['shows'])}
