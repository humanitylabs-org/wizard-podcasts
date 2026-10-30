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
PAGES = {}


def fake_open(url, headers=None, timeout=20, method='GET', allow=None):
    netfetch.check_url(url)
    if allow is not None:
        assert allow(netfetch.urlsplit(url).hostname), url
    if url in PAGES:
        return FakeResp(200, PAGES[url], {'Content-Type': 'text/html'}, url)
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


def fake_get_bytes(url, cap, what='response', headers=None, timeout=25, allow=None):
    r = fake_open(url, headers, allow=allow)
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


def _chain_conn(monkeypatch, hops, final_status=200):
    """A fake pinned connection whose responses follow `hops`: {url: Location}."""
    calls = []

    class Conn:
        def __init__(self, host, port, address, timeout):
            self.host = host

        def request(self, method, path, headers=None):
            self.url = f'https://{self.host}{path}'
            calls.append(self.url)

        def getresponse(self):
            loc = hops.get(self.url)

            class R:
                status = 302 if loc else final_status
                headers = {}

                def getheader(self, n, d=None):
                    return loc if (n == 'Location' and loc) else d

                def read(self, n=-1):
                    return b''

                def close(self):
                    pass
            return R()

        def close(self):
            pass

    monkeypatch.setattr(netfetch, '_PinnedHTTPS', Conn)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda h, *a, **k: [(socket.AF_INET, 1, 6, '', ('10.0.0.5' if h.endswith('.internal') else '93.184.216.34', 443))])
    return calls


# The real Shawn Ryan Show enclosure chain (Oct 2026): 8 redirects before the audio.
SRS = ['https://www.podtrac.com/pts/redirect.mp3/pdst.fm/e/pdrl.fm/55dc8e/x.mp3', 'https://pdst.fm/e/pdrl.fm/55dc8e/x.mp3',
       'https://pdrl.fm/55dc8e/x.mp3', 'https://pscrb.fm/rss/p/x.mp3', 'https://mgln.ai/e/433/x.mp3',
       'https://www.claritaspod.com/measure/x.mp3', 'https://arttrk.com/p/QCDM1/x.mp3', 'https://traffic.megaphone.fm/x.mp3',
       'https://dcs-spotify.megaphone.fm/x.mp3?key=abc&request_event_id=1']


def test_long_tracking_redirect_chain_is_followed(monkeypatch):
    calls = _chain_conn(monkeypatch, dict(zip(SRS, SRS[1:])))
    r = ORIG_OPEN(SRS[0])
    assert r.status == 200 and r.url == SRS[-1] and calls == SRS


def test_relative_location_is_resolved_against_the_hop(monkeypatch):
    calls = _chain_conn(monkeypatch, {'https://a.example/e/1.mp3': '/cdn/1.mp3?sig=x',
                                      'https://a.example/cdn/1.mp3?sig=x': '//b.example/1.mp3'})
    r = ORIG_OPEN('https://a.example/e/1.mp3')
    assert r.url == 'https://b.example/1.mp3' and len(calls) == 3


def test_redirect_limit_and_loop(monkeypatch):
    many = [f'https://h{i}.example/a.mp3' for i in range(netfetch.MAX_REDIRECTS + 2)]
    _chain_conn(monkeypatch, dict(zip(many, many[1:])))
    with pytest.raises(netfetch.FetchError, match='too many times'):
        ORIG_OPEN(many[0])
    _chain_conn(monkeypatch, {'https://l.example/a': 'https://m.example/b', 'https://m.example/b': 'https://l.example/a'})
    with pytest.raises(netfetch.FetchError, match='loop'):
        ORIG_OPEN('https://l.example/a')


def test_private_hop_deep_in_a_chain_is_refused(monkeypatch):
    hops = dict(zip(SRS[:5], SRS[1:5] + ['https://metadata.internal/latest']))
    calls = _chain_conn(monkeypatch, hops)
    with pytest.raises(netfetch.FetchError, match='private or local'):
        ORIG_OPEN(SRS[0])
    assert 'https://metadata.internal/latest' not in calls


def _big_feed(n, notes_kb):
    head = (b'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
            b'<channel><title>Big Show</title><description>long running</description>')
    body = b'<p>' + b'notes ' * (notes_kb * 1024 // 6) + b'</p>'
    items = b''.join(b'<item><title>Ep %d</title><guid>big-%d</guid><pubDate>Mon, 19 Oct 2026 10:00:00 GMT</pubDate>'
                     b'<enclosure url="https://cdn.example.com/big%d.mp3" type="audio/mpeg" length="1"/>'
                     b'<content:encoded><![CDATA[' % (i, i, i) + body + b']]></content:encoded></item>' for i in range(n))
    return head + items + b'</channel></rss>'


def test_streaming_parser_drops_items_and_matches_whole_parse():
    data = _big_feed(50, 4)
    p = feeds.FeedParser('https://x.example/feed')
    for i in range(0, len(data), 1000):  # tiny chunks: tags split across chunk boundaries
        p.feed(data[i:i + 1000])
    show = p.close()
    assert len(show['episodes']) == 50 and show['title'] == 'Big Show'
    assert p.root.find('channel').findall('item') == []  # items are not retained in the tree
    assert show == feeds.parse_feed(data, 'https://x.example/feed')
    q = feeds.FeedParser()
    with pytest.raises(feeds.FeedError):  # entity declaration split across chunks is still refused
        q.feed(b'<?xml version="1.0"?><rss><channel><title>t</title><!EN')
        q.feed(b'TITY x "y"></channel></rss>')
    tail = feeds.FeedParser()
    with pytest.raises(feeds.FeedError, match='DTD'):
        tail.feed(b'<?xml version="1.0"?><rss><channel><title>' + b'x' * 5000 + b'</title><description><![CDATA[a <!ENT')
        tail.feed(b'ITY a "b"> ]]></description>')


def test_feed_larger_than_15_mb_subscribes_and_cap_still_applies(monkeypatch):
    data = _big_feed(400, 45)  # ~18 MB, like a long-running interview show
    assert len(data) > 15 * 1024 * 1024
    FEEDS['https://feeds.example.com/big.xml'] = data
    r, body = req('POST', '/api/subscribe', {'url': 'https://feeds.example.com/big.xml', 'auto_download': False},
                  {'Origin': f'http://127.0.0.1:{PORT}'})
    assert r.status == 200, body
    assert json.loads(body)['show']['title'] == 'Big Show'
    assert P.MAX_FEED_BYTES >= 64 * 1024 * 1024
    monkeypatch.setattr(P, 'MAX_FEED_BYTES', 1024 * 1024)
    FEEDS['https://feeds.example.com/big2.xml'] = data
    r, body = req('POST', '/api/subscribe', {'url': 'https://feeds.example.com/big2.xml'}, {'Origin': f'http://127.0.0.1:{PORT}'})
    assert r.status == 422 and b'larger than 1 MB' in body
    sid = json.loads(req('GET', '/api/shows')[1])
    sid = [x for x in (sid['shows'] if isinstance(sid, dict) else sid) if x['title'] == 'Big Show'][0]['id']
    assert req('POST', f'/api/shows/{sid}/unsubscribe', {}, {'Origin': f'http://127.0.0.1:{PORT}'})[0].status == 200


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


# ------------------------------------------------------------------ YouTube channels as shows
import struct  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

import youtube  # noqa: E402

CID = 'UC' + 'a' * 21 + 'Q'
YT_FEED = ("""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
 <id>yt:playlist:UULF%s</id><yt:playlistId>UULF%s</yt:playlistId><yt:channelId>%s</yt:channelId><title>Videos</title>
 <author><name>Test Channel</name><uri>https://www.youtube.com/channel/%s</uri></author>
 <entry><id>yt:video:vidAAAAAAA2</id><yt:videoId>vidAAAAAAA2</yt:videoId><title>Long talk 2</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v=vidAAAAAAA2"/><published>2026-10-28T13:31:29+00:00</published>
  <media:group><media:title>Long talk 2</media:title><media:thumbnail url="https://i3.ytimg.com/vi/vidAAAAAAA2/hqdefault.jpg"/>
  <media:description>Line one &lt;script&gt;
https://example.com/link</media:description></media:group></entry>
 <entry><id>yt:video:shortAAAAA1</id><yt:videoId>shortAAAAA1</yt:videoId><title>A short</title>
  <link rel="alternate" href="https://www.youtube.com/shorts/shortAAAAA1"/><published>2026-10-29T13:31:29+00:00</published></entry>
 <entry><id>yt:video:vidAAAAAAA1</id><yt:videoId>vidAAAAAAA1</yt:videoId><title>Long talk 1</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v=vidAAAAAAA1"/><published>2026-10-20T13:31:29+00:00</published></entry>
</feed>""" % (CID[2:], CID[2:], CID, CID)).encode()
YT_PAGE = ('<html><head><link rel="canonical" href="https://www.youtube.com/channel/%s">'
           '<meta property="og:title" content="Test Channel"><meta property="og:image" content="https://yt3.googleusercontent.com/abc=s900">'
           '<meta property="og:description" content="About the channel"></head></html>' % CID).encode()


def _m4a(seconds, pad=4000):
    mvhd = struct.pack('>B3xIIII', 0, 0, 0, 1000, seconds * 1000) + b'\0' * 80
    moov = struct.pack('>I4s', 8 + 8 + len(mvhd), b'moov') + struct.pack('>I4s', 8 + len(mvhd), b'mvhd') + mvhd
    ftyp = struct.pack('>I4s', 16, b'ftyp') + b'M4A \0\0\0\0'
    mdat = struct.pack('>I4s', 8 + pad, b'mdat') + bytes(range(256)) * (pad // 256) + b'\0' * (pad % 256)
    return ftyp + moov + mdat


YT_AUDIO = _m4a(754)


class Connector(BaseHTTPRequestHandler):
    mode = 'ok'
    hits = []

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='application/json', extra=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        Connector.hits.append((self.path, self.headers.get('Range')))
        if self.path == '/api/status':
            return self._send(200, json.dumps({'contract': 'wizard.music-source/1', 'ready': True,
                                               'playback': {'state': 'ok', 'message': 'Working.'}}).encode())
        if not self.path.startswith('/api/music/audio/'):
            return self._send(404, b'{}')
        if Connector.mode == 'blocked':
            return self._send(502, b'{"error": "blocked"}')
        data, rng = YT_AUDIO, self.headers.get('Range')
        if rng:
            a, b = rng[6:].split('-')
            a, b = int(a), int(b or len(data) - 1)
            return self._send(206, data[a:b + 1], 'audio/mp4', {'Content-Range': f'bytes {a}-{b}/{len(data)}'})
        return self._send(200, data, 'audio/mp4')


@pytest.fixture
def connector(monkeypatch):
    srv = ThreadingHTTPServer(('127.0.0.1', 0), Connector)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(youtube, 'SOURCES', [('wizard-youtube-music', f'http://127.0.0.1:{srv.server_address[1]}')])
    P._yt_status['at'] = 0
    Connector.mode, Connector.hits = 'ok', []
    yield srv
    try:
        srv.shutdown()
    except Exception:
        pass


def test_youtube_refs_and_hosts():
    assert youtube.parse_ref('@Fireship') == {'page': 'https://www.youtube.com/@Fireship'}
    assert youtube.parse_ref('https://www.youtube.com/@ras.mic/videos') == {'page': 'https://www.youtube.com/@ras.mic'}
    assert youtube.parse_ref('youtube.com/channel/' + CID) == {'channel_id': CID}
    assert youtube.parse_ref('https://m.youtube.com/c/Name') == {'page': 'https://www.youtube.com/c/Name'}
    assert youtube.parse_ref(youtube.feed_url(CID)) == {'channel_id': CID}
    assert youtube.parse_ref(youtube.channel_feed_url(CID)) == {'channel_id': CID}
    for no in ('https://feeds.example.com/x.xml', 'https://youtube.com.evil.example/@x', 'Fireship', 'https://www.youtube.com/watch?v=abc'):
        assert youtube.parse_ref(no) is None, no
    assert youtube.host_allowed('i3.ytimg.com') and youtube.host_allowed('yt3.ggpht.com') and youtube.host_allowed('yt3.googleusercontent.com')
    assert not youtube.host_allowed('evilyoutube.com') and not youtube.host_allowed('lh3.googleusercontent.com')
    assert youtube.video_id('https://www.youtube.com/watch?v=vidAAAAAAA2') == 'vidAAAAAAA2'
    assert youtube.video_id('https://cdn.example.com/watch?v=vidAAAAAAA2') is None


def test_youtube_feed_skips_shorts_and_uses_channel_name():
    show = feeds.parse_feed(YT_FEED)
    assert show['title'] == 'Test Channel'
    assert [e['guid'] for e in show['episodes']] == ['yt:vidAAAAAAA2', 'yt:vidAAAAAAA1']
    e = show['episodes'][0]
    assert e['url'] == 'https://www.youtube.com/watch?v=vidAAAAAAA2' and e['image'].startswith('https://i3.ytimg.com/')
    assert '<script' not in e['notes'] and '&lt;script&gt;' in e['notes']


def test_youtube_fetch_never_leaves_youtube_hosts(monkeypatch):
    _chain_conn(monkeypatch, {youtube.feed_url(CID): 'https://tracker.example/feed.xml'})
    with pytest.raises(netfetch.FetchError, match='not a YouTube address'):
        ORIG_OPEN(youtube.feed_url(CID), allow=youtube.host_allowed)


def test_mp4_duration(tmp_path):
    f = tmp_path / 'a.m4a'
    f.write_bytes(YT_AUDIO)
    assert P.mp4_duration(f) == 754
    f.write_bytes(b'not an mp4')
    assert P.mp4_duration(f) == 0


def _ep(eid):
    return json.loads(req('GET', f'/api/episodes/{eid}')[1])


def test_youtube_channel_subscribe_download_play_and_degrade(connector):
    origin = {'Origin': f'http://127.0.0.1:{PORT}'}
    PAGES['https://www.youtube.com/@testchan'] = YT_PAGE
    FEEDS[youtube.feed_url(CID)] = YT_FEED
    PAGES['https://www.youtube.com/results?search_query=test%20chan&sp=EgIQAg%253D%253D'] = (
        '<script>var ytInitialData = ' + json.dumps({'contents': [{'channelRenderer': {
            'channelId': CID, 'title': {'simpleText': 'Test Channel'}, 'subscriberCountText': {'simpleText': '@testchan'},
            'videoCountText': {'simpleText': '12K subscribers'}, 'navigationEndpoint': {'browseEndpoint': {'canonicalBaseUrl': '/@testchan'}},
            'thumbnail': {'thumbnails': [{'url': '//yt3.ggpht.com/x=s88'}]}}}]}) + ';</script>').encode()
    r, body = req('GET', '/api/search?q=test%20chan')
    found = json.loads(body)
    assert r.status == 200 and found['youtube'][0]['channel_id'] == CID and found['youtube'][0]['subscribers'] == '12K subscribers'
    assert found['youtube'][0]['artwork'] == 'https://yt3.ggpht.com/x=s88' and found['youtube'][0]['subscribed'] is False
    r, body = req('POST', '/api/subscribe', {'url': '@testchan'}, origin)
    assert r.status == 200, body
    show = json.loads(body)['show']
    sid = show['id']
    assert show['source'] == 'youtube' and show['title'] == 'Test Channel' and show['feed_url'] == youtube.feed_url(CID)
    assert show['episodes'] == 2 and show['link'] == youtube.channel_url(CID)
    # every way of naming the channel is the same show
    r, body = req('POST', '/api/subscribe', {'url': 'https://www.youtube.com/channel/' + CID}, origin)
    assert json.loads(body)['already_subscribed'] is True
    assert json.loads(req('GET', '/api/search?q=test%20chan')[1])['youtube'][0]['subscribed'] is True
    # the newest long-form video is in Up next and downloads through the connector, audio only
    new = [e for e in json.loads(req('GET', '/api/inbox')[1])['new'] if e['show_id'] == sid]
    assert [e['title'] for e in new] == ['Long talk 2']
    eid = new[0]['id']
    assert wait_for(lambda: _ep(eid)['download']['state'] == 'done')
    d = _ep(eid)
    assert d['file'] == f'episodes/{sid}/{eid}.m4a' and d['mime'] == 'audio/mp4' and d['duration'] == 754
    assert ('/api/music/audio/vidAAAAAAA2', None) in Connector.hits
    r, data = req('GET', f'/api/episodes/{eid}/audio', headers={'Range': 'bytes=0-99'})
    assert r.status == 206 and data == YT_AUDIO[:100] and r.getheader('Content-Type') == 'audio/mp4'
    # not downloaded: streaming with Range goes through the connector too
    other = [e for e in json.loads(req('GET', f'/api/shows/{sid}')[1])['episodes'] if e['id'] != eid][0]
    r, data = req('GET', f'/api/episodes/{other["id"]}/audio', headers={'Range': 'bytes=10-19'})
    assert r.status == 206 and data == YT_AUDIO[10:20]
    # progress and mark played work the same as RSS
    assert req('POST', f'/api/episodes/{eid}/progress', {'position': 30, 'duration': 754}, origin)[0].status == 200
    assert req('POST', f'/api/episodes/{eid}/mark', {'played': True}, origin)[0].status == 200
    assert _ep(eid)['played'] is True
    # status reports the connector
    st = json.loads(req('GET', '/api/status')[1])
    assert st['youtube']['installed'] is True and st['youtube']['shows'] == 1
    # YouTube refuses the network -> a clear message
    Connector.mode = 'blocked'
    req('POST', f'/api/episodes/{other["id"]}/download', {}, origin)
    assert wait_for(lambda: _ep(other['id'])['download']['state'] == 'failed')
    assert 'home relay' in _ep(other['id'])['download']['error']
    # connector not installed -> says which app to get
    connector.shutdown()
    connector.server_close()
    P._yt_status['at'] = 0
    req('POST', f'/api/episodes/{other["id"]}/download', {}, origin)
    assert wait_for(lambda: 'YouTube Music app' in (_ep(other['id'])['download']['error'] or ''))
    assert json.loads(req('GET', '/api/status')[1])['youtube']['installed'] is False
    assert req('POST', f'/api/shows/{sid}/unsubscribe', {}, origin)[0].status == 200



# ------------------------------------------------------------------ Up next filters (0.2.1)

def test_short_title():
    assert P.short_title('The Tucker Carlson Show') == 'Tucker Carlson'
    assert P.short_title('Joe Armstrong - AI & Automation') == 'Joe Armstrong'
    assert P.short_title('The Jordan B. Peterson Podcast') == 'Jordan B. Peterson'
    assert P.short_title('Diggnation (Rebooted)') == 'Diggnation'
    assert P.short_title('Naval') == 'Naval'
    assert P.short_title('Podcast') == 'Podcast' and P.short_title('') == ''


def test_inbox_filters_by_show_kind_and_state():
    import youtube
    now = int(time.time())
    t0 = now + 10 ** 7  # newer than anything else in the shared test database

    def seed(c):
        ids = []
        for url, title in (('https://feeds.example.com/f-a.xml', 'The Alpha Show'), ('https://feeds.example.com/f-b.xml', 'Beta Podcast'),
                           (youtube.feed_url('UCfilterfilterfilterfilt'), 'Gamma Channel')):
            ids.append(c.execute('INSERT INTO shows (feed_url, title, added_at, last_checked) VALUES (?,?,?,?)', (url, title, now, now)).lastrowid)
        a, b, y = ids
        rows = [(a, 'a1', t0 + 1, 1, 0), (a, 'a2', t0 + 2, 1, 0), (a, 'a3', t0 + 3, 0, 120),  # a3 started (Continue)
                (b, 'b1', t0 + 50, 1, 0), (y, 'y1', t0 + 20, 1, 0), (a, 'a0', t0 + 99, 0, 0)]   # a0 not in Up next
        for sid, guid, pub, ib, pos in rows:
            c.execute('INSERT INTO episodes (show_id, guid, title, published, enclosure_url, first_seen, inbox, position, position_at) '
                      'VALUES (?,?,?,?,?,?,?,?,?)', (sid, guid, guid, pub, 'https://cdn.example.com/' + guid + '.mp3', now, ib, pos, now if pos else 0))
        return ids
    a, b, y = P.write(seed)
    try:
        def get(qs=''):
            r, body = req('GET', '/api/inbox' + qs)
            return r.status, json.loads(body)

        def titles(d):
            return sorted(e['title'] for e in d['continue'] + d['new'])

        st, full = get()
        assert st == 200 and full['filter'] == {'show_id': None, 'kind': None, 'state': None, 'sort': 'newest', 'since': None}
        chips = full['filters']['shows']
        ours = [c for c in chips if c['id'] in (a, b, y)]
        # newest episode first: Beta (t0+50), Gamma (t0+20), Alpha (t0+3); a0 is not in Up next so does not count
        assert [c['id'] for c in ours] == [b, y, a] and chips[:3] == ours
        alpha = ours[2]
        assert alpha['unplayed'] == 3 and alpha['started'] == 1 and alpha['short_title'] == 'Alpha' and alpha['kind'] == 'podcast'
        assert ours[1]['kind'] == 'youtube' and ours[1]['unplayed'] == 1 and ours[0]['short_title'] == 'Beta'
        assert full['filters']['kinds']['youtube'] >= 1 and full['filters']['total'] == len(full['continue']) + len(full['new'])
        # one show: only its episodes, both sections; facets stay unfiltered
        st, d = get(f'?show_id={a}')
        assert st == 200 and titles(d) == ['a1', 'a2', 'a3'] and [e['title'] for e in d['continue']] == ['a3']
        assert {e['show_id'] for e in d['continue'] + d['new']} == {a} and d['filters'] == full['filters'] and d['filter']['show_id'] == a
        # through the gateway prefix too
        r, body = req('GET', f'/wizard-podcasts/api/inbox?show_id={b}')
        assert r.status == 200 and titles(json.loads(body)) == ['b1']
        # kind
        st, d = get('?kind=youtube')
        assert st == 200 and 'y1' in titles(d) and all(e['show_id'] != a and e['show_id'] != b for e in d['continue'] + d['new'])
        st, d = get('?kind=podcast')
        assert {'a1', 'a2', 'a3', 'b1'} <= set(titles(d)) and 'y1' not in titles(d)
        assert get('?kind=rss')[1]['filter']['kind'] == 'podcast'
        # state, and filters combine
        st, d = get(f'?show_id={a}&state=started')
        assert titles(d) == ['a3'] and d['new'] == []
        st, d = get(f'?show_id={a}&state=unplayed')
        assert titles(d) == ['a1', 'a2'] and d['continue'] == []
        st, d = get(f'?show_id={a}&kind=youtube')
        assert titles(d) == []
        # bad input is a 400 with a message, never a 500
        for qs in ('?show_id=x', '?kind=video', '?state=done', '?limit=x'):
            st, d = get(qs)
            assert st == 400 and d['error'], qs
        # the same filter through MCP podcasts_list
        def mcp(args):
            r, body = req('POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'podcasts_list', 'arguments': args}})
            return json.loads(body)['result']
        tools = json.loads(req('POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})[1])['result']['tools']
        props = [t for t in tools if t['name'] == 'podcasts_list'][0]['inputSchema']['properties']
        assert props['kind']['enum'] == ['podcast', 'youtube'] and props['state']['enum'] == ['started', 'unplayed']
        sc = mcp({'view': 'inbox', 'show_id': a})['structuredContent']
        assert titles(sc) == ['a1', 'a2', 'a3'] and sc['filters']['shows'][0]['id'] == b
        assert titles(mcp({'view': 'inbox', 'kind': 'youtube', 'show_id': y})['structuredContent']) == ['y1']
        assert titles(mcp({'view': 'inbox', 'show_id': a, 'state': 'started'})['structuredContent']) == ['a3']
        assert mcp({'view': 'inbox', 'kind': 'video'})['isError'] is True
        # without view, show_id still lists that show's episodes (unchanged)
        assert len(mcp({'show_id': a})['structuredContent']['episodes']) == 4
    finally:
        P.write(lambda c: [c.execute('DELETE FROM episodes WHERE show_id=?', (i,)) or c.execute('DELETE FROM shows WHERE id=?', (i,)) for i in (a, b, y)])


def test_inbox_sort_and_since():
    now = int(time.time())
    t0 = now + 2 * 10 ** 7  # newer than anything else in the shared test database

    def seed(c):
        a = c.execute('INSERT INTO shows (feed_url, title, added_at, last_checked) VALUES (?,?,?,?)',
                      ('https://feeds.example.com/sort-a.xml', 'Sort Show', now, now)).lastrowid
        b = c.execute('INSERT INTO shows (feed_url, title, added_at, last_checked) VALUES (?,?,?,?)',
                      ('https://feeds.example.com/sort-b.xml', 'Other Show', now, now)).lastrowid
        rows = [(a, 's1', t0 + 100, 0), (a, 's2', t0 + 300, 0), (a, 's3', t0 + 200, 0), (b, 'o1', t0 + 250, 0),
                (a, 's4', t0 + 150, 90), (b, 't1', t0 + 50, 0), (b, 't2', t0 + 50, 0)]  # s4 started (Continue); t1/t2 same time
        for sid, guid, pub, pos in rows:
            c.execute('INSERT INTO episodes (show_id, guid, title, published, enclosure_url, first_seen, inbox, position, position_at) '
                      'VALUES (?,?,?,?,?,?,1,?,?)', (sid, guid, guid, pub, 'https://cdn.example.com/' + guid + '.mp3', now, pos, now if pos else 0))
        return a, b
    a, b = P.write(seed)
    try:
        def get(qs):
            r, body = req('GET', '/api/inbox' + qs)
            return r.status, json.loads(body)

        def new(d):
            return [e['title'] for e in d['new']]

        # default newest first, by publish date; every episode carries published
        st, d = get(f'?show_id={a}')
        assert st == 200 and new(d) == ['s2', 's3', 's1'] and d['filter']['sort'] == 'newest'
        assert all(isinstance(e['published'], int) and e['published'] > 0 for e in d['new'] + d['continue'])
        assert [e['title'] for e in d['continue']] == ['s4']
        # oldest first reverses the new list; Continue is untouched
        st, d = get(f'?show_id={a}&sort=oldest')
        assert st == 200 and new(d) == ['s1', 's3', 's2'] and [e['title'] for e in d['continue']] == ['s4'] and d['filter']['sort'] == 'oldest'
        # same publish time: Oldest first is the exact reverse of Newest first
        st, d = get(f'?show_id={b}')
        assert new(d) == ['o1', 't2', 't1'] and new(get(f'?show_id={b}&sort=oldest')[1]) == ['t1', 't2', 'o1']
        # oldest + limit keeps the oldest ones
        st, d = get(f'?show_id={a}&sort=oldest&limit=2')
        assert new(d) == ['s1', 's3']
        # since: published at or after it, both sections; combines with show and sort
        st, d = get(f'?show_id={a}&since={t0 + 200}')
        assert st == 200 and new(d) == ['s2', 's3'] and d['continue'] == [] and d['filter']['since'] == t0 + 200
        st, d = get(f'?since={t0 + 200}&sort=oldest')
        assert new(d)[:3] == ['s3', 'o1', 's2'] and new(d)[0] == 's3'
        st, d = get(f'?show_id={a}&since={t0 + 150}&state=started')
        assert [e['title'] for e in d['continue']] == ['s4'] and d['new'] == []
        # facets stay unfiltered
        assert get(f'?since={t0 + 10 ** 6}')[1]['filters'] == get('')[1]['filters']
        # bad values are 400s with a message
        for qs in ('?sort=random', '?since=yesterday', '?since=-5'):
            st, d = get(qs)
            assert st == 400 and d['error'], qs
        # the same through MCP podcasts_list
        def mcp(args):
            r, body = req('POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'podcasts_list', 'arguments': args}})
            return json.loads(body)['result']
        tools = json.loads(req('POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})[1])['result']['tools']
        props = [t for t in tools if t['name'] == 'podcasts_list'][0]['inputSchema']['properties']
        assert props['sort']['enum'] == ['newest', 'oldest'] and props['since']['type'] == 'integer'
        assert new(mcp({'view': 'inbox', 'show_id': a, 'sort': 'oldest'})['structuredContent']) == ['s1', 's3', 's2']
        assert new(mcp({'view': 'inbox', 'show_id': a, 'since': t0 + 250})['structuredContent']) == ['s2']
        assert mcp({'view': 'inbox', 'sort': 'sideways'})['isError'] is True
    finally:
        P.write(lambda c: [c.execute('DELETE FROM episodes WHERE show_id=?', (i,)) or c.execute('DELETE FROM shows WHERE id=?', (i,)) for i in (a, b)])
