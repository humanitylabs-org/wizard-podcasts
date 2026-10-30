"""YouTube channels as shows.

- A channel is named by its URL (/@handle, /channel/UC..., /c/name, /user/name), a bare @handle,
  or its public feed URL. It is resolved to a channel id from YouTube's own channel page.
- Episodes come from YouTube's public uploads feed for long-form videos only
  (https://www.youtube.com/feeds/videos.xml?playlist_id=UULF<id>), so Shorts and live streams
  are left out; the channel_id feed is the fallback, with /shorts/ links skipped.
- Audio (never video) comes from the installed music-source connector (wizard-youtube-music,
  contract wizard.music-source/1, GET /api/music/audio/<videoId>), which owns the YouTube
  playback path and its home relay. Podcasts itself never downloads from googlevideo.
- Every YouTube fetch is limited to YouTube's own hosts (youtube.com, ytimg.com, ggpht.com,
  yt3.googleusercontent.com) on every redirect hop, on top of the public-address SSRF guard.
"""
from __future__ import annotations

import html
import json
import os
import re
import urllib.error
import urllib.request
from urllib.parse import parse_qs, quote, urlsplit

import netfetch
from netfetch import FetchError

CHANNEL_ID = re.compile(r'^UC[0-9A-Za-z_-]{22}$')
VIDEO_ID = re.compile(r'^[0-9A-Za-z_-]{11}$')
HANDLE = re.compile(r'^@[0-9A-Za-z._-]{1,100}$')
YT_DOMAINS = ('youtube.com', 'ytimg.com', 'ggpht.com')
YT_EXACT = ('yt3.googleusercontent.com',)
BROWSER_UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36'
PAGE_HEADERS = {'User-Agent': BROWSER_UA, 'Accept-Language': 'en-US,en;q=0.9', 'Accept': 'text/html,*/*;q=0.5',
                'Cookie': 'SOCS=CAI'}  # SOCS=CAI: decline-all consent, so EU boxes get the page instead of a consent wall
MAX_PAGE_BYTES = 6 * 1024 * 1024
WATCH = 'https://www.youtube.com/watch?v='
# "<app-id>=<base URL>" pairs, like Music's WIZARD_MUSIC_SOURCES. The connector answers on its
# private app link (Docker network wizard-link-wizard-podcasts--wizard-youtube-music).
SOURCES = [tuple(x.split('=', 1)) for x in os.environ.get(
    'PODCASTS_YOUTUBE_SOURCES', 'wizard-youtube-music=http://wizard-youtube-music:9206').split(',') if '=' in x]
CONNECTOR_TIMEOUT = float(os.environ.get('PODCASTS_YOUTUBE_TIMEOUT', '900'))
MISSING = ('YouTube episodes need the YouTube Music app. Get it from the App Store (free), then tap Download again.')
BLOCKED = ("YouTube refused this Wizard's network. Open YouTube Music and set up its home relay, then tap Download again.")


def host_allowed(host: str) -> bool:
    host = (host or '').lower().rstrip('.')
    return host in YT_EXACT or any(host == d or host.endswith('.' + d) for d in YT_DOMAINS)


def feed_url(channel_id: str) -> str:
    return 'https://www.youtube.com/feeds/videos.xml?playlist_id=UULF' + channel_id[2:]


def channel_feed_url(channel_id: str) -> str:
    return 'https://www.youtube.com/feeds/videos.xml?channel_id=' + channel_id


def channel_url(channel_id: str) -> str:
    return 'https://www.youtube.com/channel/' + channel_id


def is_youtube_url(url: str) -> bool:
    try:
        return urlsplit(url).scheme in ('http', 'https') and host_allowed(urlsplit(url).hostname or '')
    except ValueError:
        return False


def is_feed(url: str) -> bool:
    p = urlsplit(url or '')
    return host_allowed(p.hostname or '') and p.path == '/feeds/videos.xml'


def video_id(enclosure_url: str):
    p = urlsplit(enclosure_url or '')
    if not host_allowed(p.hostname or '') or p.path != '/watch':
        return None
    v = parse_qs(p.query).get('v', [''])[0]
    return v if VIDEO_ID.match(v) else None


def parse_ref(text: str):
    """What the owner typed -> {'channel_id': ...} or {'page': url}, or None when it is not YouTube."""
    text = (text or '').strip()
    if HANDLE.match(text):
        return {'page': 'https://www.youtube.com/' + quote(text, safe='@._-')}
    if CHANNEL_ID.match(text):
        return {'channel_id': text}
    if not re.match(r'^https?://', text, re.I) and re.match(r'^(www\.|m\.)?youtube\.com/', text, re.I):
        text = 'https://' + text
    if not is_youtube_url(text):
        return None
    p = urlsplit(text)
    if (p.hostname or '').lower() not in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'):
        return None
    parts = [x for x in p.path.split('/') if x]
    qs = parse_qs(p.query)
    if p.path == '/feeds/videos.xml':
        cid = qs.get('channel_id', [''])[0]
        pl = qs.get('playlist_id', [''])[0]
        if CHANNEL_ID.match(cid):
            return {'channel_id': cid}
        if re.match(r'^UU(LF)?[0-9A-Za-z_-]{22}$', pl):
            return {'channel_id': 'UC' + pl[-22:]}
        return None
    if parts and parts[0] == 'channel' and len(parts) > 1 and CHANNEL_ID.match(parts[1]):
        return {'channel_id': parts[1]}
    if parts and HANDLE.match(parts[0]):
        return {'page': 'https://www.youtube.com/' + quote(parts[0], safe='@._-')}
    if len(parts) > 1 and parts[0] in ('c', 'user'):
        return {'page': f'https://www.youtube.com/{parts[0]}/{quote(parts[1])}'}
    return None


def _page(url: str) -> str:
    _status, body, _r = netfetch.get_bytes(url, MAX_PAGE_BYTES, 'YouTube page', headers=PAGE_HEADERS, timeout=25, allow=host_allowed)
    return body.decode('utf-8', 'replace')


def _meta(page: str, prop: str) -> str:
    m = re.search(r'<meta property="' + re.escape(prop) + r'" content="([^"]*)"', page)
    return html.unescape(m.group(1)) if m else ''


def resolve(text: str) -> dict:
    """-> {'channel_id', 'title', 'image', 'url', 'description'} for a YouTube channel reference."""
    ref = parse_ref(text)
    if ref is None:
        raise FetchError('That is not a YouTube channel address.')
    page_url = channel_url(ref['channel_id']) if 'channel_id' in ref else ref['page']
    try:
        page = _page(page_url)
    except FetchError as e:
        if 'channel_id' in ref:  # the id alone is enough to follow; names come from the feed
            return {'channel_id': ref['channel_id'], 'title': '', 'image': '', 'url': page_url, 'description': ''}
        if '404' in str(e):
            raise FetchError('YouTube has no channel at that address.') from None
        raise
    cid = ''
    m = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[0-9A-Za-z_-]{22})"', page)
    if m:
        cid = m.group(1)
    if not cid:
        m = re.search(r'"(?:externalId|channelId)":"(UC[0-9A-Za-z_-]{22})"', page)
        cid = m.group(1) if m else ''
    if not CHANNEL_ID.match(cid):
        raise FetchError('Could not find a YouTube channel at that address.')
    image = _meta(page, 'og:image')
    return {'channel_id': cid, 'title': _meta(page, 'og:title')[:300], 'url': channel_url(cid),
            'image': image if image.startswith('https://') and host_allowed(urlsplit(image).hostname or '') else '',
            'description': _meta(page, 'og:description')[:5000]}


def _walk(o, key, out, limit):
    if len(out) >= limit:
        return
    if isinstance(o, dict):
        if key in o:
            out.append(o[key])
        for v in o.values():
            _walk(v, key, out, limit)
    elif isinstance(o, list):
        for v in o:
            _walk(v, key, out, limit)


def _txt(v):
    if isinstance(v, dict):
        if 'simpleText' in v:
            return str(v['simpleText'])
        if 'runs' in v:
            return ''.join(str(r.get('text', '')) for r in v['runs'] if isinstance(r, dict))
    return ''


def search_channels(term: str, limit: int = 6) -> list[dict]:
    """YouTube's own channel search (results page, channel filter). Returns [] on any failure."""
    url = 'https://www.youtube.com/results?search_query=' + quote(term) + '&sp=EgIQAg%253D%253D'
    page = _page(url)
    m = re.search(r'var ytInitialData = (\{.*?\});</script>', page, re.S)
    if not m:
        return []
    found = []
    _walk(json.loads(m.group(1)), 'channelRenderer', found, limit)
    out = []
    for c in found:
        cid = c.get('channelId', '')
        if not CHANNEL_ID.match(cid):
            continue
        handle = (c.get('navigationEndpoint', {}).get('browseEndpoint', {}).get('canonicalBaseUrl') or '').lstrip('/')
        counts = [_txt(c.get('subscriberCountText')), _txt(c.get('videoCountText'))]
        subs = next((x for x in counts if 'subscriber' in x), '')
        thumbs = (c.get('thumbnail') or {}).get('thumbnails') or [{}]
        art = thumbs[-1].get('url', '')
        art = ('https:' + art) if art.startswith('//') else art
        out.append({'channel_id': cid, 'title': _txt(c.get('title'))[:300], 'handle': handle if handle.startswith('@') else '',
                    'subscribers': subs[:60], 'artwork': art if art.startswith('https://') and host_allowed(urlsplit(art).hostname or '') else ''})
    return out


# ------------------------------------------------------------------ audio through the connector

class ConnectorResponse:
    """The connector's answer, shaped like netfetch.Response for the download loop."""

    def __init__(self, resp, url):
        self.resp, self.url, self.status = resp, url, resp.status

    def header(self, name, default=None):
        return self.resp.headers.get(name, default)

    def read(self, n=-1):
        return self.resp.read(n)

    def close(self):
        self.resp.close()


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def source_status(timeout=4) -> dict:
    for app_id, base in SOURCES:
        try:
            with _opener.open(base.rstrip('/') + '/api/status', timeout=timeout) as r:
                st = json.loads(r.read(1_000_000))
            pb = st.get('playback') or {}
            return {'installed': True, 'app': app_id, 'ready': bool(st.get('ready')), 'playback': pb.get('state'),
                    'message': pb.get('message') or ''}
        except Exception:
            continue
    return {'installed': False, 'app': SOURCES[0][0] if SOURCES else 'wizard-youtube-music', 'ready': False,
            'message': MISSING}


def open_audio(vid: str, headers=None) -> ConnectorResponse:
    if not VIDEO_ID.match(vid or ''):
        raise FetchError('That YouTube episode has an invalid video id.')
    last = MISSING
    for _app_id, base in SOURCES:
        url = base.rstrip('/') + '/api/music/audio/' + vid
        req = urllib.request.Request(url, headers=dict(headers or {}))
        try:
            return ConnectorResponse(_opener.open(req, timeout=CONNECTOR_TIMEOUT), url)
        except urllib.error.HTTPError as e:
            e.close()
            raise FetchError(BLOCKED if e.code == 502 else f'The YouTube Music app answered {e.code} for this episode.') from None
        except (urllib.error.URLError, OSError):
            last = MISSING
            continue
    raise FetchError(last)
