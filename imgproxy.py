# SPDX-License-Identifier: MIT
"""Small caching image proxy for post and signature images.

Why: archived posts point at Bitcointalk's own image proxy (ip.bitcointalk.org) or straight at
third-party hosts. Loading those from the reader's browser tells every image host who is
reading, and many proxy links have expired. Here the original address is fetched once by the
server, checked, cached on disk and served from our own origin.

Safety rules, all enforced below:
  * only addresses signed by this server are fetched (it is not an open proxy);
  * http/https on the default ports only, and never to private, loopback or link-local hosts
    (the connection is pinned to the address that was checked, so DNS cannot be swapped);
  * at most 3 redirects, each checked the same way; 10 s timeouts; 8 MB size limit;
  * the body must really be PNG, JPEG, GIF or WebP (decided from its first bytes, not from
    what the remote server claims). SVG and everything else is refused;
  * responses carry nosniff, a sandboxing CSP and long cache headers;
  * nothing about the reader is sent upstream and nothing is logged.
"""
import base64
import hashlib
import hmac
import http.client
import ipaddress
import os
import socket
import ssl
import threading
import time
import urllib.parse

MAX_BYTES = 8 * 1024 * 1024
TIMEOUT = 10
NEGATIVE_TTL = 24 * 3600
UA = "Mozilla/5.0 (compatible; archive-image-cache)"
SECRET = (os.environ.get("IMG_SECRET") or base64.b16encode(os.urandom(16)).decode()).encode()
CACHE = os.environ.get("IMG_CACHE", "")
_slots = threading.BoundedSemaphore(8)
_ctx = ssl.create_default_context()
_TYPES = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}


def sign(url):
    return hmac.new(SECRET, url.encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()[:24]


def proxied(url):
    """Local address under which `url` is served."""
    b = base64.urlsafe_b64encode(url.encode("utf-8", "surrogatepass")).rstrip(b"=").decode()
    return "/img/%s/%s" % (sign(url), b)


def original(src):
    """Undo Bitcointalk's image proxy: ip.bitcointalk.org/?u=<address>&t=..&c=.. -> <address>."""
    try:
        p = urllib.parse.urlsplit(src)
    except ValueError:
        return src
    if (p.hostname or "").lower() == "ip.bitcointalk.org":
        u = urllib.parse.parse_qs(p.query).get("u", [""])[0]
        if u.startswith(("http://", "https://")):
            return u
    return src


def decode(sig, b64):
    try:
        url = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-8", "surrogatepass")
    except Exception:
        return None
    return url if hmac.compare_digest(sig, sign(url)) else None


def sniff(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _public_address(host):
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    addrs = {i[4][0] for i in infos}
    if not addrs:
        raise ValueError("no address")
    for a in addrs:
        if not ipaddress.ip_address(a.split("%")[0]).is_global:
            raise ValueError("not a public address")
    return sorted(addrs, key=lambda a: ":" in a)[0]  # prefer IPv4


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, 443, timeout=TIMEOUT, context=_ctx)
        self._address = address

    def connect(self):
        raw = socket.create_connection((self._address, 443), TIMEOUT)
        self.sock = _ctx.wrap_socket(raw, server_hostname=self.host)


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, address):
        super().__init__(host, 80, timeout=TIMEOUT)
        self._address = address

    def connect(self):
        self.sock = socket.create_connection((self._address, 80), TIMEOUT)


def fetch(url, hops=0):
    """(kind, bytes) for a real image, else raises."""
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        raise ValueError("unsupported address")
    if p.port not in (None, 80 if p.scheme == "http" else 443):
        raise ValueError("unsupported port")
    address = _public_address(p.hostname)
    conn = (_PinnedHTTPS if p.scheme == "https" else _PinnedHTTP)(p.hostname, address)
    deadline = time.time() + 3 * TIMEOUT
    try:
        path = urllib.parse.quote(p.path or "/", safe="/%:@!$&'()*+,;=~-._") + ("?" + p.query if p.query else "")
        conn.request("GET", path, headers={"User-Agent": UA, "Accept": "image/*", "Accept-Encoding": "identity"})
        r = conn.getresponse()
        if r.status in (301, 302, 303, 307, 308):
            loc = r.getheader("Location")
            if not loc or hops >= 3:
                raise ValueError("redirect")
            return fetch(urllib.parse.urljoin(url, loc), hops + 1)
        if r.status != 200:
            raise ValueError("status %d" % r.status)
        if int(r.getheader("Content-Length") or 0) > MAX_BYTES:
            raise ValueError("too large")
        chunks, size = [], 0
        while True:
            block = r.read(65536)
            if not block:
                break
            size += len(block)
            if size > MAX_BYTES or time.time() > deadline:
                raise ValueError("too large or too slow")
            chunks.append(block)
        data = b"".join(chunks)
    finally:
        conn.close()
    kind = sniff(data)
    if not kind:
        raise ValueError("not an image")
    return kind, data


def _paths(url):
    key = hashlib.sha256(url.encode("utf-8", "surrogatepass")).hexdigest()
    d = os.path.join(CACHE, key[:2])
    return d, os.path.join(d, key)


def get(url):
    """(content_type, bytes) from the cache or the network; None if it cannot be served."""
    if CACHE:
        d, base = _paths(url)
        for kind, ctype in _TYPES.items():
            try:
                with open("%s.%s" % (base, kind), "rb") as f:
                    return ctype, f.read()
            except OSError:
                pass
        try:
            if time.time() - os.path.getmtime(base + ".neg") < NEGATIVE_TTL:
                return None
        except OSError:
            pass
    if not _slots.acquire(timeout=15):
        return None
    try:
        kind, data = fetch(url)
    except Exception:
        if CACHE:
            try:
                os.makedirs(d, exist_ok=True)
                open(base + ".neg", "w").close()
            except OSError:
                pass
        return None
    finally:
        _slots.release()
    if CACHE:
        try:
            os.makedirs(d, exist_ok=True)
            tmp = "%s.%d.tmp" % (base, threading.get_ident())
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, "%s.%s" % (base, kind))
        except OSError:
            pass
    return _TYPES[kind], data
