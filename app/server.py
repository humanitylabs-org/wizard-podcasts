"""Podcasts for Wizard OS: HTTP API, view and MCP over one service layer (podcasts.py).

Stdlib only. The Wizard gateway owns authentication; the direct port stays loopback.
Requests may arrive with or without the /wizard-podcasts prefix (same-origin gateway path).
"""
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import netfetch  # noqa: E402
import youtube  # noqa: E402
import podcasts as P  # noqa: E402
import discover as D  # noqa: E402
from podcasts import Problem  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
APP_ID = 'wizard-podcasts'
VERSION = '0.5.1'
PORT = int(os.environ.get('PORT', '9207'))
PREFIX = '/' + APP_ID
STATIC_TYPES = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
                '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json'}
BOX_ONLY_HEADERS = ('tailscale-user-login', 'x-forwarded-for', 'forwarded', 'x-real-ip')


def _csv(name):
    return {x.strip().lower() for x in os.environ.get(name, '').split(',') if x.strip()}


# Who may talk to this app. The Host must be one this app was configured for (stops DNS rebinding);
# loopback and the in-network app name stay trusted so the agent's MCP route always works.
_LOCAL = {f'127.0.0.1:{PORT}', f'localhost:{PORT}', f'{APP_ID}:{PORT}', APP_ID}
TRUSTED_HOSTS = _csv('WIZARD_TRUSTED_HOSTS') | _LOCAL
# A bare host name (no port) trusts that exact name on any port: the owner's Wizard serves the same app through
# several shell ports (e.g. 8444 phone shell, 8796 gateway) and the installer may list only one of them.
TRUSTED_NAMES = {h for h in TRUSTED_HOSTS if ':' not in h}
TRUSTED_ORIGINS = _csv('WIZARD_TRUSTED_ORIGINS') | {'http://' + h for h in _LOCAL}
FRAME_ANCESTORS = ' '.join(["'self'"] + sorted(o for o in TRUSTED_ORIGINS if o.startswith('https://')))
CSP = ("default-src 'self'; img-src 'self' data: https://*.mzstatic.com https://*.ggpht.com https://*.googleusercontent.com; media-src 'self'; connect-src 'self'; "
       f"style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors {FRAME_ANCESTORS}")


# ------------------------------------------------------------------ MCP

def _qint(v, default):
    try:
        return int(v) if v not in (None, '') else default
    except ValueError:
        raise Problem(400, 'limit must be a whole number.') from None


def _int(v, name):
    if isinstance(v, bool) or not isinstance(v, int):
        raise Problem(400, f'{name} must be an integer id.')
    return v


TOOLS = [
    {'name': 'podcasts_status', 'description': 'Podcasts overview: number of shows, new episodes in Up next, download queue and failures, storage used vs cap, refresh interval, and what is playing (with its saved position). Call first.',
     'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'podcasts_search', 'description': "Search the public Apple Podcasts directory (free iTunes Search API) and YouTube channels by name. 'results' are podcasts with their public RSS feed_url; 'youtube' are YouTube channels (feed_url is the channel URL). Pass either feed_url to podcasts_subscribe. A YouTube channel URL or @handle as the query looks up that channel. 'subscribed' marks shows already followed.",
     'inputSchema': {'type': 'object', 'properties': {'query': {'type': 'string', 'description': 'Show name or topic, e.g. "Darknet Diaries".'},
                                                      'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50}}, 'required': ['query']}},
    {'name': 'podcasts_subscribe', 'description': 'Follow a podcast by its public RSS/Atom feed URL, or a YouTube channel by its URL (youtube.com/@handle, /channel/UC...) or bare @handle; YouTube episodes are long-form uploads (no Shorts), downloaded as audio only through the YouTube Music app. Idempotent. The newest episode goes to Up next and downloads automatically when auto-download is on; new episodes keep arriving in the background.',
     'inputSchema': {'type': 'object', 'properties': {'feed_url': {'type': 'string', 'description': 'http(s) feed address, YouTube channel URL, or @handle.'},
                                                      'auto_download': {'type': 'boolean'}}, 'required': ['feed_url']}},
    {'name': 'podcasts_list', 'description': "List shows (no show_id), or one show's episodes newest first (show_id). view='inbox' returns Up next: 'continue' (started, unfinished) then 'new' episodes; filter it with show_id (one show), kind ('podcast' or 'youtube'), state ('started' or 'unplayed') and/or since (unix seconds; only episodes published at or after it); sort 'newest' (default) or 'oldest' orders 'new' by publish date. Every episode has 'published' (unix seconds). Its 'filters' lists the shows in Up next (newest episode first, with unplayed counts) and kind counts, always unfiltered. Episode rows include id, played, position (seconds), duration and download state. q searches episodes: words matched case-insensitively in episode titles, show names and show notes; with view='feed' (the default when q is given) it searches every episode of the followed shows, narrowed by show_id, state ('unplayed' default, 'started', 'played', 'skipped' or 'all' for every episode) and sort, paged with limit/offset ({episodes, total}); with view='inbox' it narrows Up next.",
     'inputSchema': {'type': 'object', 'properties': {'view': {'type': 'string', 'enum': ['shows', 'inbox', 'episodes', 'feed']},
                                                      'q': {'type': 'string', 'maxLength': 200, 'description': 'search words (episode title, show name, notes); view=feed or inbox'},
                                                      'show_id': {'type': 'integer'}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 500},
                                                      'offset': {'type': 'integer', 'minimum': 0},
                                                      'kind': {'type': 'string', 'enum': ['podcast', 'youtube'], 'description': 'view=inbox only'},
                                                      'state': {'type': 'string', 'enum': ['started', 'unplayed', 'played', 'skipped', 'all'], 'description': "view=inbox: 'started' or 'unplayed'; view=feed: any (default 'unplayed')"},
                                                      'sort': {'type': 'string', 'enum': ['newest', 'oldest'], 'description': 'view=inbox or feed; orders by publish date (default newest)'},
                                                      'since': {'type': 'integer', 'minimum': 0, 'description': 'view=inbox only; unix seconds, only episodes published at or after it'}}}},
    {'name': 'podcasts_episode', 'description': 'One episode in full: show notes (sanitized html and plain text), publish date, duration, saved position, played flag, download state, file (path relative to the Podcasts app data folder when downloaded), enclosure_url, and a transcript field (not available yet; v2 will use the Transcriber app).',
     'inputSchema': {'type': 'object', 'properties': {'episode_id': {'type': 'integer'}}, 'required': ['episode_id']}},
    {'name': 'podcasts_mark', 'description': "Mark an episode played (leaves Up next, position cleared) or unplayed (played=false; position reset), and/or skipped (skipped=true: dismissed from the owner's feed and Up next without being played, its unstarted download is removed; skipped=false brings it back). Send played, skipped or both. Optional in_inbox puts it back in or removes it from Up next. Idempotent; returns the episode (with 'skipped') for readback.",
     'inputSchema': {'type': 'object', 'properties': {'episode_id': {'type': 'integer'}, 'played': {'type': 'boolean'},
                                                      'skipped': {'type': 'boolean'},
                                                      'in_inbox': {'type': 'boolean'}}, 'required': ['episode_id']}},
    {'name': 'podcasts_discover', 'description': "Shows the owner might like (the Discover feed), best first: about 70% similar to shows he follows or thumbed up (reason 'Like <show>', 'Guest on <show>', 'More from <author>') and 30% popular ('Popular in <genre or country>'). Never a show he follows or thumbed down. Each item: title, author, feed_url (pass to podcasts_subscribe to follow), reason, latest episode {title, published unix seconds}, taste (his thumb: 1, -1 or 0). Page with cursor = the previous answer's next_cursor. building=true while the first list is still being gathered (try again in a minute).",
     'inputSchema': {'type': 'object', 'properties': {'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50}, 'cursor': {'type': 'string'}}}},
    {'name': 'podcasts_taste', 'description': "The owner's show taste for Discover. Without value: a summary (liked shows, avoided shows, liked and avoided genres). With value: set his thumb on one show by feed_url (a Discover item or followed show) or show_id: 1 = thumbs up (more like it), -1 = thumbs down (never recommended again; its genre and similar shows count less), 0 = clear. Only on the owner's word. Returns the new summary.",
     'inputSchema': {'type': 'object', 'properties': {'feed_url': {'type': 'string'}, 'show_id': {'type': 'integer'},
                                                      'value': {'type': 'integer', 'enum': [1, -1, 0]}}}},
]


def call_tool(name, a):
    if name == 'podcasts_status':
        return P.status()
    if name == 'podcasts_search':
        return P.search_directory(a.get('query'), a.get('limit') or 20)
    if name == 'podcasts_subscribe':
        ad = a.get('auto_download')
        if ad is not None and not isinstance(ad, bool):
            raise Problem(400, 'auto_download must be true or false.')
        return P.subscribe(a.get('feed_url') or a.get('url'), ad)
    if name == 'podcasts_list':
        view = a.get('view') or ('feed' if a.get('q') else 'episodes' if a.get('show_id') is not None else 'shows')
        if view == 'inbox':
            return P.inbox(int(a.get('limit') or 100), a.get('show_id'), a.get('kind'), a.get('state'), a.get('sort'), a.get('since'), a.get('q'))
        if view == 'feed':
            return P.feed(a.get('show_id'), a.get('state'), a.get('sort'), a.get('limit') or 100, a.get('offset') or 0, a.get('q'))
        if view == 'episodes':
            if a.get('show_id') is None:
                raise Problem(400, 'Pass show_id to list episodes.')
            return P.show_detail(_int(a['show_id'], 'show_id'), a.get('limit') or 100, a.get('offset') or 0)
        return P.list_shows()
    if name == 'podcasts_episode':
        return P.episode(_int(a.get('episode_id'), 'episode_id'))
    if name == 'podcasts_mark':
        played, skipped = a.get('played'), a.get('skipped')
        if played is not None and not isinstance(played, bool):
            raise Problem(400, 'played must be true or false.')
        if skipped is not None and not isinstance(skipped, bool):
            raise Problem(400, 'skipped must be true or false.')
        if played is None and skipped is None:
            raise Problem(400, 'Send played and/or skipped (true or false).')
        ib = a.get('in_inbox')
        return P.mark(_int(a.get('episode_id'), 'episode_id'), played, ib if isinstance(ib, bool) else None, skipped)
    if name == 'podcasts_discover':
        return D.discover(a.get('limit') or 20, a.get('cursor'))
    if name == 'podcasts_taste':
        if a.get('value') is None:
            return D.summary()
        return D.set_taste(a.get('feed_url'), a.get('show_id'), a.get('value'))
    return None


def rpc(msg):
    if not isinstance(msg, dict):
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Expected a JSON-RPC object.'}}
    m, i, p = msg.get('method'), msg.get('id'), msg.get('params') or {}
    if 'id' not in msg:
        return None
    try:
        if m == 'initialize':
            r = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}, 'serverInfo': {'name': APP_ID, 'version': VERSION},
                 'instructions': 'Private podcast player. podcasts_status first; podcasts_search then podcasts_subscribe with feed_url; podcasts_list view=inbox for Up next.'}
        elif m == 'tools/list':
            r = {'tools': TOOLS}
        elif m == 'tools/call':
            value = call_tool(p.get('name'), p.get('arguments') or {})
            if value is None:
                return {'jsonrpc': '2.0', 'id': i, 'error': {'code': -32602, 'message': 'Unknown tool.'}}
            r = {'content': [{'type': 'text', 'text': json.dumps(value)}], 'structuredContent': value, 'isError': False}
        elif m == 'ping':
            r = {}
        else:
            return {'jsonrpc': '2.0', 'id': i, 'error': {'code': -32601, 'message': 'Unknown method.'}}
    except Problem as e:
        r = {'content': [{'type': 'text', 'text': e.message}], 'isError': True}
    except (ValueError, TypeError):
        r = {'content': [{'type': 'text', 'text': 'Invalid arguments.'}], 'isError': True}
    return {'jsonrpc': '2.0', 'id': i, 'result': r}


# ------------------------------------------------------------------ HTTP

RANGE = re.compile(r'^bytes=(\d*)-(\d*)$')
EP_ROUTE = re.compile(r'^/api/episodes/(\d+)(?:/(audio|progress|mark|skip|download|delete-download))?$')
SHOW_ROUTE = re.compile(r'^/api/shows/(\d+)(?:/(art|refresh|unsubscribe|skip-all))?$')


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'WizardPodcasts'
    sys_version = ''

    # -------- trust
    def _origin_ok(self):
        origin = (self.headers.get('Origin') or '').lower()
        host = (self.headers.get('Host') or '').lower()
        if origin:
            return origin in TRUSTED_ORIGINS or urlsplit(origin).netloc == host
        return (self.headers.get('Sec-Fetch-Site') or '') != 'cross-site'

    def refused(self, write=False):
        host = (self.headers.get('Host') or '').lower()
        if host not in TRUSTED_HOSTS and host.rsplit(':', 1)[0] not in TRUSTED_NAMES:
            self.send(403, {'error': 'Unknown host.'})
            return True
        if write and not self._origin_ok():
            self.send(403, {'error': 'Cross-origin writes refused.'})
            return True
        return False

    def box_only(self):
        names = {k.lower() for k in self.headers.keys()}
        return any(h in names for h in BOX_ONLY_HEADERS) or any(n.startswith('cf-') for n in names)

    # -------- responses
    def headers_common(self, content_type, length):
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(length))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', CSP)

    def send(self, code, body=None, content_type='application/json', cache='no-store'):
        b = body if isinstance(body, bytes) else b'' if body is None else json.dumps(body).encode()
        self.send_response(code)
        self.headers_common(content_type, len(b))
        self.send_header('Cache-Control', cache)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(b)

    def path_only(self):
        path = urlsplit(self.path).path
        if path == PREFIX:
            return '/'
        if path.startswith(PREFIX + '/'):
            path = path[len(PREFIX):]
        return path

    def query(self):
        return {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}

    # -------- GET
    def do_GET(self):
        if self.refused():
            return
        path = self.path_only()
        try:
            if path == '/healthz':
                return self.send(200, {'ok': True, 'app': APP_ID, 'version': VERSION})
            if path == '/api/status':
                return self.send(200, dict(P.status(), discover=D.status()))
            if path == '/api/search':
                qs = self.query()
                return self.send(200, P.search_directory(qs.get('q', ''), int(qs.get('limit', '20') or 20)))
            if path == '/api/shows':
                return self.send(200, P.list_shows())
            if path == '/api/discover':
                qs = self.query()
                return self.send(200, D.discover(_qint(qs.get('limit'), 20), qs.get('cursor')))
            if path == '/api/discover/audio':
                src = D.preview_source(self.query().get('feed_url', ''))
                return self.proxy({'enclosure_url': src['url'], 'mime': ''})
            if path == '/api/taste':
                return self.send(200, D.summary())
            if path == '/api/feed':
                qs = self.query()
                return self.send(200, P.feed(qs.get('show_id'), qs.get('state'), qs.get('sort'), _qint(qs.get('limit'), 100), _qint(qs.get('offset'), 0), qs.get('q')))
            if path == '/api/inbox':
                qs = self.query()
                return self.send(200, P.inbox(_qint(qs.get('limit'), 100), qs.get('show_id'), qs.get('kind'), qs.get('state'), qs.get('sort'), qs.get('since'), qs.get('q')))
            if path == '/api/settings':
                return self.send(200, P.get_settings())
            if path == '/api/opml':
                body = P.export_opml().encode()
                self.send_response(200)
                self.headers_common('text/x-opml; charset=utf-8', len(body))
                self.send_header('Content-Disposition', 'attachment; filename="podcasts.opml"')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(body)
                return
            m = SHOW_ROUTE.match(path)
            if m and m.group(2) in (None, 'art'):
                sid = int(m.group(1))
                if m.group(2) == 'art':
                    data, ctype = P.art(sid)
                    return self.send(200, data, ctype, cache='private, max-age=86400')
                qs = self.query()
                return self.send(200, P.show_detail(sid, int(qs.get('limit', '100')), int(qs.get('offset', '0'))))
            m = EP_ROUTE.match(path)
            if m and m.group(2) in (None, 'audio'):
                eid = int(m.group(1))
                if m.group(2) == 'audio':
                    return self.audio(eid)
                return self.send(200, P.episode(eid))
            if path == '/mcp':
                return self.send(405, {'error': 'Use POST /mcp.'})
            name = 'index.html' if path == '/' else path[1:]
            kind = STATIC_TYPES.get(Path(name).suffix)
            f = ROOT / 'web' / name
            if kind and '/' not in name and not name.startswith('.') and f.is_file():
                return self.send(200, f.read_bytes(), kind, cache='no-cache')
            if name == 'icon.png' and (ROOT / 'icon.png').is_file():
                return self.send(200, (ROOT / 'icon.png').read_bytes(), 'image/png', cache='max-age=86400')
            self.send(404, {'error': 'Not found.'})
        except Problem as e:
            self.send(e.code, {'error': e.message})
        except (ValueError, TypeError):
            self.send(400, {'error': 'Invalid request.'})

    def do_HEAD(self):
        self.do_GET()

    # -------- audio with HTTP Range (seeking)
    def audio(self, eid):
        row = P._ep_row(eid)
        if row['download'] == 'done' and row['file']:
            path = (P.DATA / row['file']).resolve()
            if P.EPISODES_DIR.resolve() in path.parents and path.is_file():
                return self.local_file(path, P.MIME.get(path.suffix[1:].lower(), 'application/octet-stream'))
        return self.proxy(row)

    def local_file(self, path, ctype):
        size = path.stat().st_size
        start, end, partial = 0, size - 1, False
        rng = self.headers.get('Range')
        if rng:
            m = RANGE.match(rng.strip())
            if not m or (not m.group(1) and not m.group(2)):
                return self.range_error(size)
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:  # suffix range: last N bytes
                start = max(0, size - int(m.group(2)))
            if start >= size or start > end:
                return self.range_error(size)
            partial = True
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.headers_common(ctype, length)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Cache-Control', 'private, max-age=3600')
        if partial:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.end_headers()
        if self.command == 'HEAD':
            return
        with open(path, 'rb') as f:
            f.seek(start)
            left = length
            try:
                while left > 0:
                    chunk = f.read(min(256 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def range_error(self, size):
        self.send_response(416)
        self.headers_common('application/json', 0)
        self.send_header('Content-Range', f'bytes */{size}')
        self.end_headers()

    def proxy(self, row):
        """Not downloaded yet: stream the publisher's public enclosure through the SSRF-guarded
        fetcher, forwarding Range so seeking works before the download finishes."""
        headers = {}
        rng = self.headers.get('Range')
        if rng and RANGE.match(rng.strip()):
            headers['Range'] = rng.strip()
        vid = youtube.video_id(row['enclosure_url'])
        try:
            if vid:  # YouTube episode: audio only, through the music-source connector (Range forwarded)
                up = youtube.open_audio(vid, headers)
            else:
                up = netfetch.open_url(row['enclosure_url'], headers=headers, timeout=30, method=self.command)
        except netfetch.FetchError as e:
            return self.send(502, {'error': str(e)})
        try:
            if up.status not in (200, 206):
                return self.send(502, {'error': f'The podcast host answered {up.status}.'})
            ctype = (up.header('Content-Type') or row['mime'] or 'audio/mpeg').split(';')[0]
            if not ctype.startswith(('audio/', 'video/', 'application/octet-stream')):
                return self.send(502, {'error': 'The podcast host did not send audio.'})
            self.send_response(up.status)
            length = up.header('Content-Length')
            self.send_header('Content-Type', ctype)
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', CSP)
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Cache-Control', 'no-store')
            if up.header('Content-Range'):
                self.send_header('Content-Range', up.header('Content-Range'))
            if length and length.isdigit():
                self.send_header('Content-Length', length)
            else:
                self.close_connection = True
            self.end_headers()
            if self.command == 'HEAD':
                return
            try:
                while True:
                    chunk = up.read(256 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, OSError):
                self.close_connection = True
        finally:
            up.close()

    # -------- POST
    def do_POST(self):
        if self.refused(write=True):
            return
        path = self.path_only()
        if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
            return self.send(415, {'error': 'Use application/json.'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            limit = P.MAX_OPML_BYTES * 2 if path == '/api/opml' else 65536
            if not 0 <= size <= limit:
                return self.send(413, {'error': 'Request too large.'})
            body = json.loads(self.rfile.read(size) or b'{}')
            if path == '/mcp':
                if self.box_only():
                    return self.send(403, {'error': 'Podcasts tools are box-only: call them through the Wizard agent, not over the network.'})
                if not isinstance(body, dict):
                    return self.send(400, {'error': 'Expected a JSON-RPC object.'})
                out = rpc(body)
                return self.send(202 if out is None else 200, out)
            if not isinstance(body, dict):
                return self.send(400, {'error': 'Expected a JSON object.'})
            if path == '/api/subscribe':
                ad = body.get('auto_download')
                return self.send(200, P.subscribe(body.get('url') or body.get('feed_url'), ad if isinstance(ad, bool) else None))
            if path == '/api/shows/taste':
                return self.send(200, D.set_taste(body.get('feed_url'), body.get('show_id'), body.get('value')))
            if path == '/api/refresh':
                return self.send(200, P.refresh_all(force=True))
            if path == '/api/settings':
                return self.send(200, P.set_settings(body))
            if path == '/api/opml':
                return self.send(200, P.import_opml(body.get('opml')))
            m = SHOW_ROUTE.match(path)
            if m:
                sid = int(m.group(1))
                action = m.group(2)
                if action == 'refresh':
                    return self.send(200, P.refresh_show(sid))
                if action == 'unsubscribe':
                    return self.send(200, P.unsubscribe(sid, body.get('undo') is True))
                if action == 'skip-all':
                    sk = body.get('skipped', True)
                    return self.send(200, P.skip_all(sid, sk, body.get('episode_ids')))
                if action is None:
                    return self.send(200, P.update_show(sid, body))
            m = EP_ROUTE.match(path)
            if m:
                eid, action = int(m.group(1)), m.group(2)
                if action == 'progress':
                    return self.send(200, P.set_progress(eid, body))
                if action == 'mark':
                    ib = body.get('in_inbox')
                    sk = body.get('skipped')
                    return self.send(200, P.mark(eid, body.get('played'), ib if isinstance(ib, bool) else None, sk))
                if action == 'skip':
                    return self.send(200, P.skip(eid, body.get('skipped', True)))
                if action == 'download':
                    return self.send(200, P.queue_download(eid))
                if action == 'delete-download':
                    return self.send(200, P.delete_download(eid))
            self.send(404, {'error': 'Not found.'})
        except Problem as e:
            self.send(e.code, {'error': e.message})
        except (ValueError, TypeError, AttributeError):
            self.send(400, {'error': 'Invalid JSON request.'})

    def log_message(self, format, *args):
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):  # skip getfqdn(): it stalls ~5 s in containers without a resolver
        import socketserver
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = 'localhost', self.server_address[1]


def wait_for_data(limit=180):
    """Runtipi creates the app-data bind folder as root and widens it just after start; wait for it
    instead of crash-looping."""
    import time
    for _ in range(limit):
        try:
            P.DATA.mkdir(parents=True, exist_ok=True)
            probe = P.DATA / '.write-test'
            probe.write_text('ok')
            probe.unlink()
            return
        except OSError:
            time.sleep(1)
    raise SystemExit(f'{P.DATA} is not writable')


def main():
    wait_for_data()
    P.init()
    P.start_workers()
    D.start()
    Server((os.environ.get('BIND', '0.0.0.0'), PORT), Handler).serve_forever()


if __name__ == '__main__':
    main()
