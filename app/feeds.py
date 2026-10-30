"""Feed parsing (RSS 2.0 / Atom, iTunes + Podcasting 2.0 tags), OPML, and show-notes sanitizing.

Only public enclosures the publisher lists in its feed are used. Nothing is scraped.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import html
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

ITUNES = '{http://www.itunes.com/dtds/podcast-1.0.dtd}'
CONTENT = '{http://purl.org/rss/1.0/modules/content/}'
ATOM = '{http://www.w3.org/2005/Atom}'
PODCAST = '{https://podcastindex.org/namespace/1.0}'
MEDIA = '{http://search.yahoo.com/mrss/}'

MAX_NOTES = 100_000
MAX_EPISODES = 3000
MEDIA_TYPES = ('audio/', 'video/')
MEDIA_EXT = re.compile(r'\.(mp3|m4a|m4b|aac|mp4|m4v|ogg|oga|opus|wav|flac|webm)(?:$|\?)', re.I)


class FeedError(Exception):
    pass


# ------------------------------------------------------------------ XML safety

def parse_xml(data: bytes):
    """Parse untrusted XML. DTDs (and with them entity expansion bombs and external
    entities) are refused outright; podcast feeds never need them."""
    head = data[:4096].lower()
    if b'<!doctype' in head or b'<!entity' in data.lower():
        raise FeedError('That feed uses an XML feature Podcasts does not accept (DTD/entities).')
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        raise FeedError('That address did not return a valid RSS or Atom feed.') from None


def _text(el, path):
    if el is None:
        return ''
    found = el.find(path)
    return (found.text or '').strip() if found is not None and found.text else ''


def _clean_line(value, limit=500):
    value = re.sub(r'\s+', ' ', html.unescape(value or '')).strip()
    return value[:limit]


def parse_duration(value) -> int:
    value = (value or '').strip()
    if not value:
        return 0
    try:
        if ':' in value:
            total = 0
            for part in value.split(':'):
                total = total * 60 + float(part or 0)
            return max(0, int(total))
        return max(0, int(float(value)))
    except ValueError:
        return 0


def parse_date(value) -> int:
    value = (value or '').strip()
    if not value:
        return 0
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return 0
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp())


def _http_url(value, base=''):
    value = (value or '').strip()
    if not value:
        return ''
    value = urljoin(base, value) if base else value
    return value if urlsplit(value).scheme.lower() in ('http', 'https') and len(value) <= 2048 else ''


def _is_media(url, mime):
    mime = (mime or '').lower()
    return mime.startswith(MEDIA_TYPES) or bool(MEDIA_EXT.search(urlsplit(url).path + '?'))


class FeedParser:
    """Incremental, memory-bounded RSS/Atom parser.

    Feed it the response body chunk by chunk; each <item>/<entry> is turned into an
    episode dict as soon as it closes and is then dropped from the tree, so memory
    stays proportional to one episode (plus the parsed results), not to the whole
    feed. Long-running shows publish 30+ MB feeds; building a full tree for those
    would need several hundred MB.
    DTDs/entity declarations are refused before the bytes reach expat."""

    HEAD = 4096

    def __init__(self, base_url: str = ''):
        self.base = base_url
        self._p = ET.XMLPullParser(events=('start', 'end'))
        self._head = b''
        self._tail = b''
        self._stack = []
        self.root = None
        self._rss, self._atom = [], []
        self._seen_rss = self._seen_atom = 0

    def feed(self, chunk: bytes):
        if not chunk:
            return
        if len(self._head) < self.HEAD:
            self._head += chunk[:self.HEAD - len(self._head)]
            if b'<!doctype' in self._head.lower():
                raise FeedError('That feed uses an XML feature Podcasts does not accept (DTD/entities).')
        if b'<!entity' in (self._tail + chunk).lower():
            raise FeedError('That feed uses an XML feature Podcasts does not accept (DTD/entities).')
        self._tail = chunk[-16:]
        try:
            self._p.feed(chunk)
            self._drain()  # XMLPullParser queues parse errors and raises them from read_events()
        except ET.ParseError:
            raise FeedError('That address did not return a valid RSS or Atom feed.') from None

    def _drain(self):
        for event, el in self._p.read_events():
            if event == 'start':
                if self.root is None:
                    self.root = el
                self._stack.append(el)
                continue
            self._stack.pop()
            parent = self._stack[-1] if self._stack else None
            if parent is None:
                continue
            if el.tag == 'item' and parent.tag == 'channel':
                if self._seen_rss < MAX_EPISODES:
                    ep = _rss_item(el, self.base)
                    if ep:
                        self._rss.append(ep)
                self._seen_rss += 1
                parent.remove(el)
            elif el.tag == ATOM + 'entry' and parent is self.root and parent.tag == ATOM + 'feed':
                if self._seen_atom < MAX_EPISODES:
                    ep = _atom_entry(el, self.base)
                    if ep:
                        self._atom.append(ep)
                self._seen_atom += 1
                parent.remove(el)

    def close(self) -> dict:
        try:
            self._p.close()
            self._drain()
        except ET.ParseError:
            raise FeedError('That address did not return a valid RSS or Atom feed.') from None
        root = self.root
        if root is None:
            raise FeedError('That address did not return a valid RSS or Atom feed.')
        if root.tag == 'rss' or root.find('channel') is not None:
            return _parse_rss(root, self.base, self._rss)
        if root.tag == ATOM + 'feed':
            return _parse_atom(root, self.base, self._atom)
        raise FeedError('That address did not return an RSS or Atom podcast feed.')


def parse_feed(data: bytes, base_url: str = '') -> dict:
    p = FeedParser(base_url)
    step = 1 << 20
    for i in range(0, len(data), step):
        p.feed(data[i:i + step])
    return p.close()


def _parse_rss(root, base, episodes):
    ch = root.find('channel')
    if ch is None:
        raise FeedError('That RSS feed has no channel.')
    image = ''
    it_img = ch.find(ITUNES + 'image')
    if it_img is not None:
        image = _http_url(it_img.get('href'), base)
    if not image:
        image = _http_url(_text(ch, 'image/url'), base)
    return {
        'title': _clean_line(_text(ch, 'title'), 300) or 'Untitled podcast',
        'author': _clean_line(_text(ch, ITUNES + 'author') or _text(ch, 'managingEditor'), 300),
        'description': sanitize_html(_text(ch, ITUNES + 'summary') or _text(ch, 'description'))[:20000],
        'image': image,
        'link': _http_url(_text(ch, 'link'), base),
        'episodes': episodes,
    }


def _rss_item(item, base):
    enc = item.find('enclosure')
    url, mime, length = '', '', 0
    if enc is not None:
        url, mime = _http_url(enc.get('url'), base), (enc.get('type') or '').strip()[:100]
        length = int(enc.get('length')) if (enc.get('length') or '').isdigit() else 0
    if not url:
        mc = item.find(MEDIA + 'content')
        if mc is not None:
            url, mime = _http_url(mc.get('url'), base), (mc.get('type') or '').strip()[:100]
    if not url or not _is_media(url, mime):
        return None
    it_ep_img = item.find(ITUNES + 'image')
    transcript = ''
    for t in item.findall(PODCAST + 'transcript'):
        transcript = _http_url(t.get('url'), base)
        if transcript:
            break
    notes = item.find(CONTENT + 'encoded')
    notes = (notes.text if notes is not None and notes.text else '') or _text(item, 'description') or _text(item, ITUNES + 'summary')
    guid = _text(item, 'guid') or url
    return {
        'guid': guid[:1000],
        'title': _clean_line(_text(item, 'title'), 500) or 'Untitled episode',
        'published': parse_date(_text(item, 'pubDate')),
        'duration': parse_duration(_text(item, ITUNES + 'duration')),
        'url': url,
        'mime': mime,
        'length': length,
        'notes': sanitize_html(notes),
        'link': _http_url(_text(item, 'link'), base),
        'image': _http_url(it_ep_img.get('href'), base) if it_ep_img is not None else '',
        'transcript_url': transcript,
    }


def _atom_link(el, rel):
    for l in el.findall(ATOM + 'link'):
        if (l.get('rel') or 'alternate') == rel:
            return l
    return None


def _parse_atom(root, base, episodes):
    alt = _atom_link(root, 'alternate')
    return {
        'title': _clean_line(_text(root, ATOM + 'title'), 300) or 'Untitled podcast',
        'author': _clean_line(_text(root, ATOM + 'author/' + ATOM + 'name'), 300),
        'description': sanitize_html(_text(root, ATOM + 'subtitle'))[:20000],
        'image': _http_url(_text(root, ATOM + 'logo') or _text(root, ATOM + 'icon'), base),
        'link': _http_url(alt.get('href') if alt is not None else '', base),
        'episodes': episodes,
    }


def _atom_entry(entry, base):
    enc = _atom_link(entry, 'enclosure')
    if enc is None:
        return None
    url, mime = _http_url(enc.get('href'), base), (enc.get('type') or '')[:100]
    if not url or not _is_media(url, mime):
        return None
    ealt = _atom_link(entry, 'alternate')
    return {
        'guid': (_text(entry, ATOM + 'id') or url)[:1000],
        'title': _clean_line(_text(entry, ATOM + 'title'), 500) or 'Untitled episode',
        'published': parse_date(_text(entry, ATOM + 'published') or _text(entry, ATOM + 'updated')),
        'duration': parse_duration(_text(entry, ITUNES + 'duration')),
        'url': url,
        'mime': mime,
        'length': int(enc.get('length')) if (enc.get('length') or '').isdigit() else 0,
        'notes': sanitize_html(_text(entry, ATOM + 'content') or _text(entry, ATOM + 'summary')),
        'link': _http_url(ealt.get('href') if ealt is not None else '', base),
        'image': '',
        'transcript_url': '',
    }


# ------------------------------------------------------------------ OPML

def parse_opml(data: bytes) -> list[dict]:
    root = parse_xml(data)
    if root.tag.lower() != 'opml':
        raise FeedError('That file is not OPML.')
    out, seen = [], set()
    for node in root.iter('outline'):
        url = _http_url(node.get('xmlUrl') or node.get('xmlurl') or '')
        if url and url not in seen:
            seen.add(url)
            out.append({'url': url, 'title': _clean_line(node.get('title') or node.get('text') or '', 300)})
    return out[:500]


def build_opml(shows) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<opml version="2.0">', '<head><title>Podcasts</title></head>', '<body>']
    for s in shows:
        t = html.escape(s['title'], quote=True)
        lines.append(f'<outline type="rss" text="{t}" title="{t}" xmlUrl="{html.escape(s["feed_url"], quote=True)}"/>')
    lines += ['</body>', '</opml>', '']
    return '\n'.join(lines)


# ------------------------------------------------------------------ show-notes sanitizer

ALLOWED_TAGS = {'p', 'br', 'a', 'b', 'strong', 'i', 'em', 'u', 'ul', 'ol', 'li', 'blockquote', 'code', 'pre', 'hr',
                'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'span', 'div', 'small', 'sup', 'sub'}
VOID = {'br', 'hr'}
HEADING = {'h1': 'h4', 'h2': 'h4', 'h3': 'h4', 'h4': 'h4', 'h5': 'h5', 'h6': 'h5'}
DROP_WITH_CONTENT = {'script', 'style', 'iframe', 'object', 'embed', 'noscript', 'template', 'svg', 'math', 'head',
                     'title', 'form', 'textarea', 'select', 'button', 'audio', 'video', 'canvas', 'frame', 'frameset'}


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.stack, self.drop = [], [], 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in DROP_WITH_CONTENT:
            if tag not in VOID:
                self.drop += 1
            return
        if self.drop or tag not in ALLOWED_TAGS:
            return
        if tag in ('span', 'div'):
            tag = 'p' if tag == 'div' else 'span'
        tag = HEADING.get(tag, tag)
        if tag == 'a':
            href = ''
            for k, v in attrs:
                if k.lower() == 'href' and v:
                    v = v.strip()
                    if urlsplit(v).scheme.lower() in ('http', 'https', 'mailto'):
                        href = v
            if not href:
                self.stack.append(None)
                return
            self.out.append(f'<a href="{html.escape(href, quote=True)}" target="_blank" rel="noopener noreferrer nofollow">')
            self.stack.append('a')
            return
        if tag in VOID:
            self.out.append(f'<{tag}>')
            return
        self.out.append(f'<{tag}>')
        self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if not self.drop and tag in VOID:
            self.out.append(f'<{tag}>')

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in DROP_WITH_CONTENT:
            self.drop = max(0, self.drop - 1)
            return
        if self.drop or tag not in ALLOWED_TAGS or tag in VOID:
            return
        tag = {'div': 'p'}.get(tag, HEADING.get(tag, tag))
        if tag == 'a':
            tag = 'a'
        # close up to the matching open tag
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i] == tag or (tag == 'a' and self.stack[i] is None):
                for t in reversed(self.stack[i:]):
                    if t:
                        self.out.append(f'</{t}>')
                del self.stack[i:]
                return

    def handle_data(self, data):
        if not self.drop:
            self.out.append(html.escape(data, quote=False))

    def result(self):
        for t in reversed(self.stack):
            if t:
                self.out.append(f'</{t}>')
        return ''.join(self.out)


def sanitize_html(value: str) -> str:
    """Allow-list sanitizer: a few text tags, http(s)/mailto links (new tab, no referrer). No attributes
    besides href, no images (tracking pixels), no scripts, styles, frames or forms."""
    value = (value or '')[:MAX_NOTES * 2]
    if not value.strip():
        return ''
    if '<' not in value:
        paras = [p.strip() for p in re.split(r'\n\s*\n', html.unescape(value)) if p.strip()]
        return ''.join('<p>' + html.escape(p, quote=False).replace('\n', '<br>') + '</p>' for p in paras)[:MAX_NOTES]
    s = _Sanitizer()
    try:
        s.feed(value)
        s.close()
    except Exception:  # malformed markup: fall back to plain text
        return '<p>' + html.escape(re.sub(r'<[^>]*>', ' ', value), quote=False)[:MAX_NOTES] + '</p>'
    out = s.result()
    if len(out) > MAX_NOTES:  # re-balance tags after truncation (one extra pass, never recursive)
        again = _Sanitizer()
        again.feed(out[:MAX_NOTES])
        again.close()
        out = again.result()
    return out


def notes_text(value: str) -> str:
    text = re.sub(r'<(br|/p|/li|/h\d|hr)[^>]*>', '\n', value or '')
    text = html.unescape(re.sub(r'<[^>]+>', '', text))
    return re.sub(r'\n{3,}', '\n\n', re.sub(r'[ \t]+', ' ', text)).strip()
