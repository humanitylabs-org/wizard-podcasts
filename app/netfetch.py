"""Outbound HTTP for Podcasts, with an SSRF guard.

Every feed, search, artwork and episode request goes through open_url():
- only http/https, no credentials in the URL;
- the host is resolved once and every address must be public (no loopback, private,
  link-local, CGNAT/Tailscale, multicast or reserved ranges);
- the socket connects to that validated address (no second DNS lookup, so DNS
  rebinding cannot swap in a private address), TLS still verifies the real hostname;
- redirects are followed by hand (max 5) and every hop is checked again.
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urljoin, urlsplit

USER_AGENT = 'WizardPodcasts/0.1 (+https://github.com/humanitylabs-org/wizard-podcasts)'
MAX_REDIRECTS = 5


class FetchError(Exception):
    """A request the app refused or could not complete. The message is shown to the owner."""


def _public_address(host: str, port: int):
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise FetchError(f'Could not find {host}.') from None
    chosen = None
    for family, _type, _proto, _canon, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0].split('%', 1)[0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise FetchError('That address points at a private or local network, which Podcasts never fetches.')
        if chosen is None:
            chosen = (family, str(ip))
    if chosen is None:
        raise FetchError(f'Could not find {host}.')
    return chosen


def check_url(url: str) -> str:
    """Static checks a URL must pass before it is stored or fetched."""
    if not isinstance(url, str) or not url.strip():
        raise FetchError('Paste a feed address that starts with http:// or https://.')
    url = url.strip()
    if len(url) > 2048:
        raise FetchError('That address is too long.')
    parts = urlsplit(url)
    if parts.scheme.lower() not in ('http', 'https'):
        raise FetchError('Only http:// and https:// addresses are supported.')
    if not parts.hostname:
        raise FetchError('That address has no host name.')
    if parts.username or parts.password:
        raise FetchError('Addresses with a user name or password are not supported.')
    try:
        parts.port
    except ValueError:
        raise FetchError('That address has an invalid port.') from None
    return url


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, address, timeout):
        super().__init__(host, port, timeout=timeout)
        self._address = address

    def connect(self):
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, address, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self):
        raw = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


class Response:
    """An open upstream response. Always close() it."""

    def __init__(self, conn, resp, url):
        self.conn, self.resp, self.url = conn, resp, url
        self.status = resp.status
        self.headers = resp.headers

    def header(self, name, default=None):
        return self.resp.getheader(name, default)

    def read(self, n=-1):
        return self.resp.read(n)

    def read_capped(self, cap: int, what='response') -> bytes:
        length = self.header('Content-Length')
        if length and length.isdigit() and int(length) > cap:
            raise FetchError(f'The {what} is larger than {cap // (1024 * 1024)} MB.')
        chunks, total = [], 0
        while True:
            chunk = self.resp.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > cap:
                raise FetchError(f'The {what} is larger than {cap // (1024 * 1024)} MB.')
            chunks.append(chunk)
        return b''.join(chunks)

    def close(self):
        try:
            self.resp.close()
        finally:
            self.conn.close()


def open_url(url: str, headers: dict | None = None, timeout: float = 20, method: str = 'GET') -> Response:
    for _ in range(MAX_REDIRECTS + 1):
        url = check_url(url)
        parts = urlsplit(url)
        https = parts.scheme.lower() == 'https'
        port = parts.port or (443 if https else 80)
        _family, address = _public_address(parts.hostname, port)
        cls = _PinnedHTTPS if https else _PinnedHTTP
        conn = cls(parts.hostname, port, address, timeout)
        path = (parts.path or '/') + (('?' + parts.query) if parts.query else '')
        send = {'User-Agent': USER_AGENT, 'Accept': '*/*', 'Accept-Encoding': 'identity'}
        send.update(headers or {})
        try:
            conn.request(method, path, headers=send)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            conn.close()
            raise FetchError(f'Could not reach {parts.hostname}: {exc.__class__.__name__}.') from None
        location = resp.getheader('Location')
        if resp.status in (301, 302, 303, 307, 308) and location:
            resp.close()
            conn.close()
            url = urljoin(url, location)
            if resp.status == 303:
                method = 'GET'
            continue
        return Response(conn, resp, url)
    raise FetchError('That address redirects too many times.')


def get_bytes(url: str, cap: int, what='response', headers=None, timeout=25):
    r = open_url(url, headers=headers, timeout=timeout)
    try:
        if r.status == 304:
            return r.status, b'', r
        if r.status >= 400:
            raise FetchError(f'The server answered {r.status} for that {what}.')
        return r.status, r.read_capped(cap, what), r
    finally:
        r.close()
