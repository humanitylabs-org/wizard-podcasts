"""Unit + HTTP tests. Stdlib server started in-process on a free loopback port with a temp DATA_DIR.
Network is never touched: feed/audio fetches are served by a fake opener."""
import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'app'))
TMP = tempfile.mkdtemp(prefix='podcasts-test-')
os.environ['DATA_DIR'] = TMP
os.environ['WIZARD_TRUSTED_HOSTS'] = 'wizard.example:8796,wizard.example'

import feeds  # noqa: E402
import netfetch  # noqa: E402
import podcasts as P  # noqa: E402

ORIG_OPEN = netfetch.open_url

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:podcast="https://podcastindex.org/namespace/1.0">
<channel><title>Test Show</title><itunes:author>Ann</itunes:author><link>https://example.com/</link>
<description>About &lt;b&gt;it&lt;/b&gt;</description><itunes:image href="https://example.com/art.jpg"/>
<item><title>Ep 2</title><guid>g2</guid><pubDate>Tue, 20 Oct 2026 10:00:00 GMT</pubDate><itunes:duration>1:02:03</itunes:duration>
<enclosure url="https://cdn.example.com/ep2.mp3" type="audio/mpeg" length="3000"/>
<content:encoded><![CDATA[<p>Hi <script>alert(1)</script><a href="javascript:x()">bad</a> <a href="https://ok.example/" onclick="x()">good</a><img src="https://t.example/p.gif"><iframe src="https://e.example"></iframe></p>]]></content:encoded>
<podcast:transcript url="https://cdn.example.com/ep2.vtt" type="text/vtt"/></item>
<item><title>Ep 1</title><guid>g1</guid><pubDate>Mon, 19 Oct 2026 10:00:00 GMT</pubDate><itunes:duration>125</itunes:duration>
<enclosure url="https://cdn.example.com/ep1.mp3" type="audio/mpeg" length="3000"/><description>Plain text
second line</description></item>
<item><title>Blog post</title><guid>g0</guid><enclosure url="https://cdn.example.com/a.pdf" type="application/pdf"/></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Atom Show</title>
<entry><title>A1</title><id>a1</id><published>2026-10-01T10:00:00Z</published>
<link rel="enclosure" href="https://cdn.example.com/a1.m4a" type="audio/mp4" length="10"/><summary>s</summary></entry></feed>"""

AUDIO = bytes(range(256)) * 12  # 3072 bytes


class FakeResp:
    def __init__(self, status, body, headers, url):
        self.status, self._body, self._h, self.url = status, body, headers, url
        self.headers = headers
        self._pos = 0

    def header(self, name, default=None):
        for k, v in self._h.items():
            if k.lower() == name.lower():
                return v
        return default

    def read(self, n=-1):
        if n is None or n < 0:
            n = len(self._body) - self._pos
        out = self._body[self._pos:self._pos + n]
        self._pos += len(out)
        return out

    read_capped = netfetch.Response.read_capped

    @property
    def resp(self):
        return self

    def close(self):
        pass


FEEDS = {}


def fake_open(url, headers=None, timeout=20, method='GET'):
    netfetch.check_url(url)
    if url in FEEDS:
        return FakeResp(200, FEEDS[url], {'Content-Type': 'application/rss+xml', 'ETag': '"e1"'}, url)
    if url.endswith('.mp3') or url.endswith('.m4a'):
        rng = (headers or {}).get('Range')
        if rng:
            a, b = rng[6:].split('-')
            a, b = int(a), int(b or len(AUDIO) - 1)
            return FakeResp(206, AUDIO[a:b + 1], {'Content-Type': 'audio/mpeg', 'Content-Length': str(b - a + 1),
                                                   'Content-Range': f'bytes {a}-{b}/{len(AUDIO)}'}, url)
        return FakeResp(200, AUDIO, {'Content-Type': 'audio/mpeg', 'Content-Length': str(len(AUDIO))}, url)
    if url.endswith('art.jpg'):
        return FakeResp(200, b'\xff\xd8jpeg', {'Content-Type': 'image/jpeg'}, url)
    return FakeResp(404, b'', {}, url)


def fake_get_bytes(url, cap, what='response', headers=None, timeout=25):
    r = fake_open(url, headers)
    if r.status >= 400:
        raise netfetch.FetchError(f'The server answered {r.status} for that {what}.')
    return r.status, r.read_capped(cap, what), r


@pytest.fixture(autouse=True, scope='session')
def server():
    netfetch.open_url = fake_open
    netfetch.get_bytes = fake_get_bytes
    P.netfetch.open_url = fake_open
    P.netfetch.get_bytes = fake_get_bytes
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    os.environ['PORT'] = str(port)
    import server as S
    S.netfetch.open_url = fake_open
    P.init()
    P.start_workers()
    srv = S.Server(('127.0.0.1', port), S.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    globals()['PORT'] = port
    globals()['S'] = S
    yield
    srv.shutdown()


def req(method, path, body=None, headers=None):
    c = http.client.HTTPConnection('127.0.0.1', PORT, timeout=10)
    h = {'Host': f'127.0.0.1:{PORT}'}
    if body is not None:
        h['Content-Type'] = 'application/json'
    h.update(headers or {})
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r, data


def wait_for(pred, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.1)
    return False


# ------------------------------------------------------------------ pure units

def test_ssrf_guard_blocks_private(monkeypatch):
    for host, ip in [('a.test', '127.0.0.1'), ('b.test', '10.1.2.3'), ('c.test', '169.254.169.254'),
                     ('d.test', '100.101.102.103'), ('e.test', '::1'), ('f.test', '::ffff:192.168.1.1'), ('g.test', '0.0.0.0')]:
        fam = socket.AF_INET6 if ':' in ip else socket.AF_INET
        monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, ip=ip, fam=fam, **k: [(fam, 1, 6, '', (ip, 80))])
        with pytest.raises(netfetch.FetchError, match='private or local'):
            netfetch._public_address(host, 80)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(socket.AF_INET, 1, 6, '', ('93.184.216.34', 80))])
    assert netfetch._public_address('ok.test', 80)[1] == '93.184.216.34'
    # one private address among public ones is still refused (round-robin rebinding)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(socket.AF_INET, 1, 6, '', ('93.184.216.34', 80)), (socket.AF_INET, 1, 6, '', ('127.0.0.1', 80))])
    with pytest.raises(netfetch.FetchError):
        netfetch._public_address('mixed.test', 80)


def test_check_url_rejects_schemes():
    for bad in ['file:///etc/passwd', 'ftp://x.example/f', 'gopher://x', 'http://user:pw@x.example/', '', 'javascript:alert(1)', 'http://x.example:99999/']:
        with pytest.raises(netfetch.FetchError):
            netfetch.check_url(bad)
    assert netfetch.check_url(' https://x.example/feed ') == 'https://x.example/feed'


def test_redirects_are_rechecked(monkeypatch):
    calls = []

    class Conn:
        def __init__(self, host, port, address, timeout):
            calls.append(host)

        def request(self, *a, **k):
            pass

        def getresponse(self):
            class R:
                status = 302
                headers = {}
                def getheader(self, n, d=None):
                    return 'http://127.0.0.1/secret' if n == 'Location' else d
                def close(self):
                    pass
            return R()

        def close(self):
            pass

    monkeypatch.setattr(netfetch, '_PinnedHTTPS', Conn)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda h, *a, **k: [(socket.AF_INET, 1, 6, '', ('127.0.0.1' if h == '127.0.0.1' else '93.184.216.34', 443))])
    with pytest.raises(netfetch.FetchError, match='private or local'):
        ORIG_OPEN('https://public.example/feed')
    assert calls == ['public.example']


def test_parse_rss_and_sanitize():
    show = feeds.parse_feed(RSS)
    assert show['title'] == 'Test Show' and show['author'] == 'Ann' and show['image'] == 'https://example.com/art.jpg'
    assert [e['guid'] for e in show['episodes']] == ['g2', 'g1']  # the pdf item is not an episode
    e2 = show['episodes'][0]
    assert e2['duration'] == 3723 and e2['transcript_url'] == 'https://cdn.example.com/ep2.vtt'
    n = e2['notes']
    assert 'script' not in n and 'alert' not in n and 'javascript' not in n and 'iframe' not in n and '<img' not in n and 'onclick' not in n
    assert '<a href="https://ok.example/" target="_blank" rel="noopener noreferrer nofollow">good</a>' in n
    assert show['episodes'][1]['notes'] == '<p>Plain text<br>second line</p>'
    assert show['episodes'][1]['duration'] == 125


def test_parse_atom_and_opml():
    show = feeds.parse_feed(ATOM)
    assert show['title'] == 'Atom Show' and show['episodes'][0]['url'] == 'https://cdn.example.com/a1.m4a'
    opml = feeds.build_opml([{'title': 'A & B', 'feed_url': 'https://x.example/f?a=1&b=2'}])
    assert feeds.parse_opml(opml.encode()) == [{'url': 'https://x.example/f?a=1&b=2', 'title': 'A & B'}]


def test_xml_bombs_refused():
    bomb = b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]><rss><channel><title>&lol2;</title></channel></rss>'
    with pytest.raises(feeds.FeedError):
        feeds.parse_feed(bomb)
    with pytest.raises(feeds.FeedError):
        feeds.parse_feed(b'<html><body>not a feed</body></html>')


def test_sanitizer_edge_cases():
    s = feeds.sanitize_html('<div><style>p{}</style><p>a<b>b</p><a href="data:text/html,x">d</a><svg><script>x</script></svg>&lt;script&gt;</div>')
    assert '<style' not in s and 'data:' not in s and '<svg' not in s and '<script>' not in s and '&lt;script&gt;' in s
    big = feeds.sanitize_html('<p>' + 'x' * 300000 + '</p>')
    assert len(big) <= feeds.MAX_NOTES + 20


# ------------------------------------------------------------------ service through HTTP

def test_subscribe_download_range_progress_mark_mcp():
    FEEDS['https://feeds.example.com/test.xml'] = RSS
    r, body = req('POST', '/api/subscribe', {'url': 'https://feeds.example.com/test.xml'})
    assert r.status == 200, body
    d = json.loads(body)
    sid = d['show']['id']
    assert d['show']['episodes'] == 2 and d['show']['new'] == 1
    # idempotent
    r, body = req('POST', '/wizard-podcasts/api/subscribe', {'url': 'https://feeds.example.com/test.xml'})
    assert json.loads(body)['already_subscribed'] is True
    inbox = json.loads(req('GET', '/api/inbox')[1])
    ep = inbox['new'][0]
    assert ep['title'] == 'Ep 2'
    eid = ep['id']
    assert wait_for(lambda: json.loads(req('GET', f'/api/episodes/{eid}')[1])['download']['state'] == 'done')
    detail = json.loads(req('GET', f'/api/episodes/{eid}')[1])
    assert detail['file'] == f'episodes/{sid}/{eid}.mp3' and (Path(TMP) / detail['file']).read_bytes() == AUDIO
    assert detail['transcript']['status'] == 'not_available'
    # Range: seeking works on the local file
    r, data = req('GET', f'/api/episodes/{eid}/audio', headers={'Range': 'bytes=100-199'})
    assert r.status == 206 and data == AUDIO[100:200] and r.getheader('Content-Range') == f'bytes 100-199/{len(AUDIO)}'
    r, data = req('GET', f'/api/episodes/{eid}/audio', headers={'Range': 'bytes=-10'})
    assert r.status == 206 and data == AUDIO[-10:]
    r, data = req('GET', f'/api/episodes/{eid}/audio', headers={'Range': 'bytes=99999-'})
    assert r.status == 416
    r, data = req('GET', f'/api/episodes/{eid}/audio')
    assert r.status == 200 and data == AUDIO and r.getheader('Accept-Ranges') == 'bytes'
    # Range through the proxy for a not-downloaded episode
    other = [e for e in json.loads(req('GET', f'/api/shows/{sid}')[1])['episodes'] if e['id'] != eid][0]
    assert other['download']['state'] == 'none'
    r, data = req('GET', f'/api/episodes/{other["id"]}/audio', headers={'Range': 'bytes=10-19'})
    assert r.status == 206 and data == AUDIO[10:20]
    # progress is saved server-side and shows in Continue + now_playing
    r, body = req('POST', f'/api/episodes/{eid}/progress', {'position': 61.5, 'duration': 3723})
    assert r.status == 200 and json.loads(body)['position'] == 61.5
    inbox = json.loads(req('GET', '/api/inbox')[1])
    assert inbox['continue'][0]['id'] == eid and inbox['now_playing']['id'] == eid
    # MCP: list, episode, mark played, readback, mark unplayed
    def mcp(name, args):
        r, b = req('POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}})
        return json.loads(b)['result']
    tools = json.loads(req('POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})[1])['result']['tools']
    assert [t['name'] for t in tools] == ['podcasts_status', 'podcasts_search', 'podcasts_subscribe', 'podcasts_list', 'podcasts_episode', 'podcasts_mark']
    st = mcp('podcasts_status', {})['structuredContent']
    assert st['shows'] == 1 and st['downloaded'] == 1 and st['now_playing']['position'] == 61.5
    assert mcp('podcasts_list', {'view': 'inbox'})['structuredContent']['continue'][0]['id'] == eid
    assert len(mcp('podcasts_list', {'show_id': sid})['structuredContent']['episodes']) == 2
    e = mcp('podcasts_episode', {'episode_id': eid})['structuredContent']
    assert e['file'] == detail['file'] and 'good' in e['notes_text']
    m = mcp('podcasts_mark', {'episode_id': eid, 'played': True})['structuredContent']
    assert m['played'] is True and m['in_inbox'] is False and m['position'] == 0
    assert json.loads(req('GET', '/api/inbox')[1])['new'] == []
    m = mcp('podcasts_mark', {'episode_id': eid, 'played': False, 'in_inbox': True})['structuredContent']
    assert m['played'] is False and m['in_inbox'] is True
    assert mcp('podcasts_episode', {'episode_id': 99999})['isError'] is True
    # box-only MCP and origin/host guards
    r, _ = req('POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, {'Tailscale-User-Login': 'x@y'})
    assert r.status == 403
    r, _ = req('POST', '/api/refresh', {}, {'Origin': 'https://evil.example'})
    assert r.status == 403
    r, _ = req('GET', '/api/status', headers={'Host': 'evil.example'})
    assert r.status == 403
    # a bare trusted name covers every shell port of the owner's Wizard; other names stay refused
    r, _ = req('GET', '/api/status', headers={'Host': 'wizard.example:8444'})
    assert r.status == 200
    r, _ = req('POST', '/api/refresh', {}, {'Host': 'wizard.example:8444', 'Origin': 'https://wizard.example:8444'})
    assert r.status == 200
    r, _ = req('GET', '/api/status', headers={'Host': 'wizard.example.evil.example:8444'})
    assert r.status == 403
    # completed playback marks played
    r, body = req('POST', f'/api/episodes/{eid}/progress', {'position': 3700, 'completed': True})
    assert json.loads(body)['played'] is True


def test_settings_validation_and_storage_cap():
    r, body = req('POST', '/api/settings', {'speed': 3.5})
    assert r.status == 400
    r, body = req('POST', '/api/settings', {'speed': 1.75, 'refresh_minutes': 30})
    assert r.status == 200 and json.loads(body)['speed'] == 1.75
    # eviction: played downloads go first
    eps = P.q("SELECT * FROM episodes WHERE download='done'")
    assert eps
    P.write(lambda c: c.execute("UPDATE episodes SET file_size=? WHERE download='done'", (300 * P.MB,)))
    P.write(lambda c: c.execute('UPDATE episodes SET played=1, played_at=1'))
    r, body = req('POST', '/api/settings', {'storage_cap_mb': 256})
    assert r.status == 200
    assert P.storage_used() == 0
    assert not list((Path(TMP) / 'episodes').rglob('*.mp3'))


def test_opml_import_and_unsubscribe():
    FEEDS['https://feeds.example.com/atom.xml'] = ATOM
    opml = feeds.build_opml([{'title': 'Atom', 'feed_url': 'https://feeds.example.com/atom.xml'},
                             {'title': 'Local', 'feed_url': 'http://127.0.0.1/feed'}])
    r, body = req('POST', '/api/opml', {'opml': opml})
    d = json.loads(body)
    assert r.status == 200 and d['found'] == 2 and d['added'] == 2  # local one is stored, refused at fetch
    assert wait_for(lambda: any(s['title'] == 'Atom Show' for s in json.loads(req('GET', '/api/shows')[1])['shows']), 15)
    shows = {s['title']: s for s in json.loads(req('GET', '/api/shows')[1])['shows']}
    bad = [s for s in shows.values() if s['feed_url'] == 'http://127.0.0.1/feed'][0]
    assert wait_for(lambda: json.loads(req('GET', f'/api/shows/{bad["id"]}')[1])['show']['last_error'])
    sid = shows['Atom Show']['id']
    r, body = req('POST', f'/api/shows/{sid}/unsubscribe', {})
    assert r.status == 200
    assert req('GET', f'/api/shows/{sid}')[0].status == 404
    r, body = req('GET', '/api/opml')
    assert r.status == 200 and b'atom.xml' not in body
