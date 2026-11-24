#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Server-rendered, read-only frontend for the TalkSearch Bitcointalk archive.

Plain HTML + one stylesheet + a tiny optional script. It talks to the archive JSON API
(the same /api/* contract the Epochtalk Vue client used) and keeps every public URL of
that client working:

    /                         board index
    /boards/<id>[?page=N]     topic list
    /threads/<id>[?page=N|?start=N|?post=<post id>][#<post id>]
    /profile/<name>[/posts|/trust][?page=N]
    /search/posts?search=...  /search/users?search=...
    /index.php?topic=..., ?board=..., ?action=profile;u=...   (Bitcointalk-style links)

Python 3.9+ standard library only. Configuration is by environment variables (see README).
"""
import html
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bbcode
import imgproxy
import talkimg

API = os.environ.get("ARCHIVE_API", "http://127.0.0.1:18183").rstrip("/")
BIND = os.environ.get("BIND", "127.0.0.1")
PORT = int(os.environ.get("PORT", "18190"))
SITE = os.environ.get("SITE_NAME", "Bitcointalk Forum")
SITE_TAG = os.environ.get("SITE_TAGLINE", "by TalkSearch")
SITE_TAG_URL = os.environ.get("SITE_TAGLINE_URL", "https://talksearch.io")  # the name after "by " links here
SOURCE_URL = os.environ.get("SOURCE_URL", "")
SITE_URL = os.environ.get("SITE_URL", "https://forum.talksearch.io").rstrip("/")   # absolute links in feeds
TALKSEARCH_API = os.environ.get("TALKSEARCH_API", "").rstrip("/")   # optional: ranked advanced search
BOARD_DESCRIPTIONS = os.environ.get("BOARD_DESCRIPTIONS", "")        # optional JSON file {board id: text}
BOARD_SEQUENCE = os.environ.get("BOARD_SEQUENCE", "")                # optional JSON list of board ids in display order
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
PER_PAGE = 25           # same page size as the previous client, so old ?page=N links still match
MAX_WINDOW = 10000      # the API cannot page topic lists / search results past this many rows
BCT = "https://bitcointalk.org/index.php"

e = html.escape
_pool = ThreadPoolExecutor(max_workers=8)


# --------------------------------------------------------------------------- API access
class NotFound(Exception):
    pass


class Upstream(Exception):
    pass


_cache = {}
_cache_lock = threading.Lock()


def api(path, params=None, ttl=0):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    now = time.time()
    if ttl:
        hit = _cache.get(url)
        if hit and hit[0] > now:
            return hit[1]
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as ex:
        if ex.code in (400, 404):
            raise NotFound()
        raise Upstream(str(ex.code))
    except Exception as ex:  # refused, timeout, bad JSON
        raise Upstream(type(ex).__name__)
    if ttl:
        with _cache_lock:
            if len(_cache) > 5000:
                for k in [k for k, v in _cache.items() if v[0] <= now]:
                    _cache.pop(k, None)
                if len(_cache) > 5000:
                    _cache.clear()
            _cache[url] = (now + ttl, data)
    return data


def board_index():
    return api("/api/boards", ttl=45)


def board_paths():
    """{'Bitcoin > Mining': board_id} built from the board tree, for linked breadcrumbs."""
    hit = _cache.get("#paths")
    if hit and hit[0] > time.time():
        return hit[1]
    out = {}

    def walk(b):
        segs = [s.strip() for s in (b.get("description") or "").split(">")][1:]
        if segs:
            out[" > ".join(segs)] = b["id"]
        for c in b.get("children") or []:
            walk(c)
    try:
        for cat in board_index().get("boards", []):
            for b in cat.get("boards", []):
                walk(b)
    except (NotFound, Upstream):
        return out
    _cache["#paths"] = (time.time() + 600, out)
    return out


def board_crumbs(board):
    """Breadcrumb list [(label, href|None)] for a board dict that has a 'description' path."""
    segs = [s.strip() for s in (board.get("description") or "").split(">")][1:]
    if not segs:
        return [(board.get("name") or "Board", "/boards/%s" % board.get("id"))]
    paths = board_paths()
    crumbs = []
    for i, seg in enumerate(segs):
        bid = paths.get(" > ".join(segs[:i + 1]))
        if i == len(segs) - 1:
            bid = board.get("id")
        crumbs.append((seg, "/boards/%s" % bid if bid is not None else None))
    return crumbs


def find_post_offset(tid, pid):
    """0-based position of post `pid` inside topic `tid` (posts are ordered by id)."""
    key = "#off:%s:%s" % (tid, pid)
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    try:
        pos = api("/api/posts/%d/position" % pid)
        if str(pos.get("thread_id")) == str(tid):
            _cache[key] = (time.time() + 3600, int(pos["offset"]))
            return int(pos["offset"])
    except (NotFound, Upstream, KeyError, ValueError):
        pass  # API without the position endpoint: walk the topic instead
    first = api("/api/posts", {"thread_id": tid, "limit": 1, "start": 0})
    count = int(first["thread"].get("post_count") or 0)
    lo, hi = 0, max(0, count - 1)
    for _ in range(32):
        if lo >= hi:
            break
        mid = (lo + hi) // 2
        got = api("/api/posts", {"thread_id": tid, "limit": 1, "start": mid}).get("posts") or []
        if not got:
            hi = mid
            continue
        if int(got[0]["id"]) < pid:
            lo = mid + 1
        else:
            hi = mid
    _cache[key] = (time.time() + 3600, lo)
    return lo


# --------------------------------------------------------------------------- formatting
def parse_dt(s):
    if not s:
        return None
    s = str(s).replace("Z", "+00:00")
    s = re.sub(r"\.\d+", "", s)
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def when(s, short=False):
    d = parse_dt(s)
    if not d:
        return ""
    d = d.astimezone(timezone.utc)
    txt = d.strftime("%b %d, %Y") if short else d.strftime("%B %d, %Y, %H:%M UTC")
    return '<time datetime="%s">%s</time>' % (d.strftime("%Y-%m-%dT%H:%M:%SZ"), txt)


def num(n):
    try:
        return "{:,}".format(int(n))
    except (TypeError, ValueError):
        return ""


def clean_title(t):
    t = (t or "").strip()
    while t[:4].lower() == "re: ":
        t = t[4:].strip()
    return t or "(untitled)"


_QUOTE_RE = re.compile(r'<div class="quoteheader">.*?</div>\s*<div class="quote">.*?</div>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def excerpt(body, n=320):
    b = body or ""
    for _ in range(6):  # nested quotes: peel from the inside out
        b2 = _QUOTE_RE.sub(" ", b)
        if b2 == b:
            break
        b = b2
    b = re.sub(r"<br\s*/?>", " ", b)
    t = re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", b))).strip()
    if len(t) > n:
        t = t[:n].rsplit(" ", 1)[0] + "…"
    return e(t)


def _restyle(style):
    out = _FSIZE_RE.sub(_scale_size, style)
    if "--lh: 1.17em" in out:  # art-sized text: its own line box must be tight as well
        out = re.sub(r"line-height:\s*[\d.]+em", "line-height: 1.17em", out)
    return out


_VOID = {"br", "img", "hr", "wbr"}
_NO_TEXT = {"table", "thead", "tbody", "tr", "ul", "ol"}
_FSIZE_RE = re.compile(r"font-size:\s*(\d{1,3}(?:\.\d{1,2})?)(pt|px)", re.I)


def _scale_size(m):
    # Bitcointalk's body text is 13px (about 10pt). Express absolute sizes as multiples of
    # --pt, which the stylesheet sets to one tenth of the local base size, so [size=10pt]
    # text matches normal text here and banner art keeps its proportions.
    n = float(m.group(1)) * (0.75 if m.group(2).lower() == "px" else 1.0)
    # Block-drawing characters at art sizes must touch the next row; 1.3em leaves hairline gaps.
    return "font-size: calc(var(--pt) * %g)%s" % (n, "; --lh: 1.17em" if n <= 4 else "; --lh: initial")


class _Balancer(HTMLParser):
    """Re-emits already-sanitised HTML with every element properly closed, so one post or
    signature with a missing end tag can never swallow the rest of the page."""

    def __init__(self, tight=False):
        super().__init__(convert_charrefs=False)
        self.out, self.stack, self.tight = [], [], tight

    def handle_starttag(self, tag, attrs):
        a = "".join(' %s="%s"' % (k, html.escape(_restyle(html.unescape(v or ""))
                                                 if k == "style" else html.unescape(v or ""), quote=True))
                    for k, v in attrs)
        if tag == "img":
            src = html.unescape(dict(attrs).get("src") or "")
            if src.startswith(("http://", "https://")):
                attrs = [(k, imgproxy.proxied(imgproxy.original(src), src) if k == "src" else v) for k, v in attrs]
            elif not src.startswith("/"):
                return
            a = "".join(' %s="%s"' % (k, html.escape(html.unescape(v or ""), quote=True)) for k, v in attrs)
        if self.tight and tag == "br":
            self.out.append('<span class="t"><br></span>')
            return
        self.out.append("<%s%s>" % (tag, a))
        if tag not in _VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in _VOID or tag not in self.stack:
            return
        while self.stack:
            t = self.stack.pop()
            self.out.append("</%s>" % t)
            if t == tag:
                break

    def handle_data(self, d):
        d = d.replace("<", "&lt;").replace(">", "&gt;")
        if self.tight and not (d.isspace() and self.stack and self.stack[-1] in _NO_TEXT):
            d = '<span class="t">%s</span>' % d
        self.out.append(d)

    def handle_entityref(self, n):
        self.out.append(('<span class="t">&%s;</span>' if self.tight else "&%s;") % n)

    def handle_charref(self, n):
        self.out.append(('<span class="t">&#%s;</span>' if self.tight else "&#%s;") % n)


def balanced(fragment, tight=False):
    """`tight` reproduces how Bitcointalk's pages lay out lines: a line is only as tall as the
    text actually on it, which is what lets banner art drawn in 2pt characters stack closely.
    Each run of text gets its own inline box; the stylesheet zeroes the block line height."""
    if not fragment:
        return ""
    b = _Balancer(tight)
    try:
        b.feed(fragment)
        b.close()
    except Exception:
        return html.escape(_TAG_RE.sub(" ", fragment))
    while b.stack:
        b.out.append("</%s>" % b.stack.pop())
    return "".join(b.out)


_LINK_RE = re.compile(r'href="/threads/(\d+)#(\d+)"')


def post_body(body, tid, on_page):
    """Sanitised post HTML from the API, with images lazy-loaded and quote links that
    resolve to the right page of the quoted topic."""
    def fix(m):
        t, p = m.group(1), m.group(2)
        if t == str(tid) and p in on_page:
            return 'href="#%s"' % p
        return 'href="/threads/%s?post=%s#%s"' % (t, p, p)
    sized = "font-size" in (body or "")
    b = _LINK_RE.sub(fix, balanced(body, tight=sized))
    if sized:
        b = '<div class="tight">%s</div>' % b
    b = b.replace("<img ", '<img loading="lazy" decoding="async" ')
    return b




# --------------------------------------------------------------------------- board tree
try:
    DESCRIPTIONS = json.load(open(BOARD_DESCRIPTIONS, encoding="utf-8")) if BOARD_DESCRIPTIONS else {}
except (OSError, ValueError):
    DESCRIPTIONS = {}


def _load_sequence():
    try:
        with open(BOARD_SEQUENCE) as f:
            return {int(b): i for i, b in enumerate(json.load(f))}
    except (OSError, ValueError, TypeError):
        return {}


SEQUENCE = _load_sequence()


def forum():
    """Every board as a tree (all levels), built from the API's flat list and its path strings."""
    hit = _cache.get("#forum")
    if hit and hit[0] > time.time():
        return hit[1]
    flat = api("/api/boards", {"stripped": "true"})["boards"]
    index = api("/api/boards")
    boards, by_path = {}, {}
    for b in flat:
        n = dict(b, kids=[], parent=None)
        n["segs"] = [s.strip() for s in (b.get("description") or "").split(">")][1:]
        boards[b["id"]] = n
        by_path[" > ".join(n["segs"])] = b["id"]
    for bid, n in boards.items():
        if len(n["segs"]) > 2:
            pid = by_path.get(" > ".join(n["segs"][:-1]))
            if pid is not None and pid != bid:
                n["parent"] = pid
                boards[pid]["kids"].append(bid)
    pos = lambda i: (SEQUENCE.get(i, 10 ** 6), i)   # the forum's own order; unknown boards go last, by id
    for n in boards.values():
        n["kids"].sort(key=pos)
    boards = {i: boards[i] for i in sorted(boards, key=pos)}
    cats = [(c.get("name") or "", sorted((b["id"] for b in c.get("boards", []) if b["id"] in boards), key=pos))
            for c in index.get("boards", [])]
    f = {"boards": boards, "cats": cats, "recent": index.get("threads") or []}
    _cache["#forum"] = (time.time() + 45, f)
    return f


def board_trail(bid):
    """[(label, href|None)] from the category down to this board, parents included."""
    boards = forum()["boards"]
    n = boards.get(bid)
    if not n:
        return []
    chain = []
    cur = n
    while cur:
        chain.append((cur["name"], "/boards/%s" % cur["id"]))
        cur = boards.get(cur["parent"]) if cur["parent"] is not None else None
    cat = n["segs"][0] if n["segs"] else None
    return ([(cat, "/#c-%s" % re.sub(r"[^a-z0-9]+", "-", cat.lower()))] if cat else []) + list(reversed(chain))


# --------------------------------------------------------------------------- small pieces
ICON_EXT = ('<svg class="ext" viewBox="0 0 16 16" width="10" height="10" aria-hidden="true">'
            '<path d="M6 3H3v10h10v-3M9 3h4v4M13 3 7 9" fill="none" stroke="currentColor" stroke-width="1.8" '
            'stroke-linecap="round" stroke-linejoin="round"/></svg>')
ICON_BOARD = ('<svg class="bi %s" viewBox="0 0 32 32" width="30" height="30" role="img" aria-label="%s">'
              '<path d="M4 7h17a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-8l-5 4v-4H4a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2z"/>'
              '<path class="b2" d="M26 12h2a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-1v3l-4-3h-7a2 2 0 0 1-2-2v-.5h7a4 4 0 0 0 4-4z"/></svg>')
ICON_TOPIC = ('<svg class="ti %s" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">'
              '<path d="M3 1.5h7l3 3v10H3z"/><path class="b2" d="M5.5 7h5M5.5 9.5h5M5.5 12h3" fill="none"/></svg>')
ICON_PM = ('<svg class="pmi" viewBox="0 0 18 13" width="18" height="13" aria-hidden="true">'
           '<rect x=".75" y=".75" width="16.500" height="11.500" rx="1.200" fill="var(--win2)" stroke="currentColor" stroke-width="1.500"/>'
           '<path d="M1.200 1.400 9 7.600l7.800-6.200" fill="none" stroke="currentColor" stroke-width="1.500" stroke-linejoin="round"/></svg>')
ICON_LAST = ('<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><path d="M3 1.500h6l3.500 3.500v9.500H3z" fill="var(--win2)" stroke="currentColor" stroke-width="1.200" stroke-linejoin="round"/>'
             '<path d="M9 1.500V5h3.500" fill="none" stroke="currentColor" stroke-width="1.200" stroke-linejoin="round"/>'
             '<path d="M5.200 10h5.300M8.300 7.700l2.300 2.300-2.300 2.300" fill="none" stroke="var(--icon-on)" stroke-width="1.700" stroke-linecap="round" stroke-linejoin="round"/></svg>')


def go_last(tid):
    """Small 'go to the last post' button for Last post cells."""
    return ('<a class="golast" href="/threads/%s?last=1" title="Go to the last post" aria-label="Go to the last post">%s</a>' % (tid, ICON_LAST))


ICON_PIN = ('<svg class="pin" viewBox="0 0 16 16" width="13" height="13" role="img" aria-label="Sticky topic"><title>Sticky topic</title>'
            '<path d="M9.800 1.200 14.800 6.200 13.400 7.600 12.300 7.200 9.700 9.800 10 13 8.600 14.400 5.300 11.100 1.700 14.700 1.300 14.300 4.900 10.700 1.600 7.400 3 6l3.200.300L8.800 3.700 8.400 2.600z"/></svg> ')
ICON_FOLD = ('<svg class="fold" viewBox="0 0 12 12" width="11" height="11" aria-hidden="true">'
             '<path d="M2 4l4 4 4-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>')
ICON_POST = ('<svg class="pi" viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">'
             '<path d="M2 2.5h12v8H8l-3.5 3v-3H2z"/></svg>')


def bct(query):
    return "%s?%s" % (BCT, query)


def act(href, label, title="", cls=""):
    """An action that happens on bitcointalk.org: opens there in a popup window (new tab without JS)."""
    return ('<a class="pop %s" href="%s" target="_blank" rel="noopener nofollow"%s>%s%s</a>'
            % (cls, e(href), ' title="%s"' % e(title) if title else "", label, ICON_EXT))


def user_link(name, cls=""):
    if not name or name == "(unknown)":
        return '<span class="muted">%s</span>' % e(name or "unknown")
    return '<a class="%s" href="/profile/%s">%s</a>' % (cls, urllib.parse.quote(name, safe=""), e(name))


def trust_html(stats, label="Trust: "):
    """Trust score the way Bitcointalk shows it: positive / neutral / negative feedback counted by
    the DefaultTrust network (weekly snapshot)."""
    f = (stats or {}).get("feedback")
    if not f:
        return ""
    pos, neg = int(f.get("positive") or 0), int(f.get("negative") or 0)
    if not pos and not neg:
        # Bitcointalk shows plain "neutral" when no trusted member left positive or negative feedback.
        return ('<span class="trust" title="No positive or negative feedback counted by the DefaultTrust network">%sneutral</span>' % label)
    neu = int(f.get("neutral") or 0)
    mark = f.get("mark") or ("!!!" if f.get("warning") else "")
    return ('<span class="trust" title="Trust feedback counted by the DefaultTrust network: %d positive, %d neutral, %d negative">'
            '%s%s<b class="%s">+%d</b> / =%d / <b class="%s">-%d</b></span>'
            % (pos, neu, neg, label,
               '<b class="neg" title="Warning: trade with extreme caution">!!!</b>: ' if mark == "!!!" else "# " if mark == "#" else "",
               "pos" if pos else "", pos, neu, "neg" if neg else "", neg))


def avatar_html(url, name, size):
    if url:
        return '<img class="avatar" src="%s" alt="" width="%d" height="%d" loading="lazy">' % (e(url), size, size)
    return ""


def is_recent(s, hours=24):
    d = parse_dt(s)
    return bool(d) and (datetime.now(timezone.utc) - d).total_seconds() < hours * 3600


def pager(base, page, pages, label, anchor=""):
    """SMF-style page list: Pages: [1] 2 3 ... 265, plus a jump box on long lists."""
    if pages <= 1:
        return '<nav class="pages" aria-label="%s">Pages: [<b>1</b>]</nav>' % e(label)
    sep = "&amp;" if "?" in base else "?"

    def link(p, text=None, rel=""):
        href = base if p == 1 else "%s%spage=%d" % (base, sep, p)
        return '<a href="%s%s"%s>%s</a>' % (href, anchor, ' rel="%s"' % rel if rel else "", text or num(p))
    shown = sorted({1, 2, pages - 1, pages, page - 2, page - 1, page, page + 1, page + 2} & set(range(1, pages + 1)))
    out = ['<nav class="pages" aria-label="%s">Pages: ' % e(label)]
    if page > 1:
        out.append(link(page - 1, "«", "prev") + " ")
    prev = 0
    for p in shown:
        if p - prev > 1:
            out.append('<span class="gap">…</span> ')
        out.append('[<b aria-current="page">%s</b>] ' % num(p) if p == page else link(p) + " ")
        prev = p
    if page < pages:
        out.append(link(page + 1, "»", "next"))
    if pages > 7:
        action, _, qs = base.partition("?")
        hidden = "".join('<input type="hidden" name="%s" value="%s">' % (e(k), e(v))
                         for k, v in urllib.parse.parse_qsl(html.unescape(qs)))
        out.append('<form class="jump" action="%s" method="get">%s<label>Go to page '
                   '<input name="page" type="number" min="1" max="%d" inputmode="numeric" required></label>'
                   '<button type="submit">Go</button></form>' % (action, hidden, pages))
    out.append("</nav>")
    return "".join(out)


def crumbs_html(items):
    parts = ['<a href="/"><b>%s</b></a>' % e(SITE)]
    for i, (label, href) in enumerate(items):
        last = i == len(items) - 1
        text = "<b>%s</b>" % e(label) if last else e(label)
        parts.append('<a href="%s"%s>%s</a>' % (href, ' aria-current="page"' if last else "", text) if href else text)
    return '<nav class="crumbs" aria-label="Breadcrumb">%s</nav>' % ' <span aria-hidden="true">&gt;</span> '.join(parts)


def _tag_html():
    if SITE_TAG_URL and SITE_TAG.startswith("by ") and len(SITE_TAG) > 3:
        return 'by <a href="%s" rel="noopener">%s</a>' % (e(SITE_TAG_URL), e(SITE_TAG[3:]))
    return e(SITE_TAG)


TAG_HTML = _tag_html()


def layout(title, body, q="", canonical="", noindex=False, active="", scripts=(), feed=""):
    if feed:   # a small visible link; feed readers also find it through the link element in the head
        body = '<a class="feedlink" href="%s" title="Follow this page in a feed reader">Atom feed</a>' % e(feed) + body
    site_full = ("%s %s" % (SITE, SITE_TAG)).strip()
    full = "%s - %s" % (title, site_full) if title else site_full

    def tab(href, text, key):
        return '<a href="%s"%s>%s</a>' % (href, ' class="on" aria-current="page"' if active == key else "", text)
    src = ' | <a href="%s" rel="noopener">Source code (MIT)</a>' % e(SOURCE_URL) if SOURCE_URL else ""
    extra = "".join('<script src="/static/%s?v=%s" defer></script>\n' % (s, ASSET_V) for s in scripts)
    return """<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%s</title>
<meta property="og:title" content="%s">
<meta property="og:site_name" content="%s">
<meta property="og:type" content="website">
%s%s<meta name="color-scheme" content="light dark">
<script src="/static/theme.js?v=%s"></script>
<link rel="stylesheet" href="/static/style.css?v=%s">
<link rel="icon" href="/favicon.ico" sizes="48x48">
<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="manifest" href="/static/site.webmanifest">
<meta name="theme-color" content="#f7931a">
<script src="/static/app.js?v=%s" defer></script>
%s</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<div class="page">
<header class="hd">
<div class="brand"><a href="/">%s</a> <span>%s</span></div>
<form class="q" action="/search/posts" method="get" role="search">
<label class="sr" for="q">Search posts</label>
<input id="q" name="search" type="search" placeholder="Search posts" value="%s" autocomplete="off">
<button type="submit">Search</button>
</form>
</header>
<nav class="tabs" aria-label="Main">%s%s%s%s%s%s<button id="theme" type="button" hidden aria-label="Dark theme" title="Dark theme"><svg class="ico-moon" viewBox="0 0 24 24" width="17" height="17" aria-hidden="true" focusable="false"><path d="M20.5 14.2A8.5 8.5 0 0 1 9.8 3.5a8.5 8.5 0 1 0 10.7 10.7z" fill="currentColor"/></svg><svg class="ico-sun" viewBox="0 0 24 24" width="17" height="17" aria-hidden="true" focusable="false"><circle cx="12" cy="12" r="4.5" fill="currentColor"/><g stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 1.5v2.5M12 20v2.5M1.5 12H4M20 12h2.5M4.6 4.6l1.8 1.8M17.6 17.6l1.8 1.8M4.6 19.4l1.8-1.8M17.6 6.4l1.8-1.8"/></g></svg></button></nav>
<main id="main">
%s
</main>
<footer class="ft">
<p>%s %s is an unofficial, read-only archive of public Bitcointalk posts and is not affiliated with bitcointalk.org. Posts belong to their authors.</p>
<p>Credits: public posts and profiles from <a href="https://bitcointalk.org" rel="noopener">bitcointalk.org</a>; trust lists and merit totals from <a href="https://loyce.club" rel="noopener">LoyceV</a>; post edit history from the <a href="https://bitlist.co" rel="noopener">BitList Archive</a> and LoyceV; search by <a href="https://talksearch.io" rel="noopener">TalkSearch</a>.</p>
<p>Made by <a href="https://bitcointalk.org/index.php?action=profile;u=2739424" rel="noopener">NotATether</a></p>
<p><a href="/">Home</a> | <a href="/search/posts">Search</a> | <a href="/search/users">Members</a> | <a href="/modlog">Mod log</a>%s</p>
</footer>
</div>
</body>
</html>
""" % (e(full), e(full), e(site_full),
       ('<link rel="canonical" href="%s">\n' % e(canonical) if canonical else "")
       + ('<link rel="alternate" type="application/atom+xml" title="Atom feed" href="%s">\n' % e(feed) if feed else ""),
       '<meta name="robots" content="noindex">\n' if noindex else "",
       ASSET_V, ASSET_V, ASSET_V, extra, e(SITE), TAG_HTML, e(q),
       tab("/", "Home", "boards"), tab("/search/posts", "Search", "search"), tab("/search/users", "Members", "members"),
       tab("/recent", "Recent posts", "recent") + tab("/stats", "Stats", "stats"),
       act(bct("action=login"), "Login"), act(bct("action=register"), "Register"),
       body, e(SITE), e(SITE_TAG), src)


def message_page(title, text, extra=""):
    return layout(title, '<div class="tb"><h1 class="catbg">%s</h1><div class="pad"><p>%s</p>%s<p><a href="/">Back to the board index</a></p></div></div>'
                  % (e(title), text, extra), noindex=True)


# --------------------------------------------------------------------------- board index and boards
def moderators_html(bid):
    try:
        names = (api("/api/board-moderators", ttl=600) or {}).get(str(bid)) or []
    except (NotFound, Upstream):
        names = []
    if not names:
        return ""
    return '<div class="mods"><i>Moderator%s: %s</i></div>' % ("" if len(names) == 1 else "s", ", ".join(user_link(x) for x in names))


def board_row(n, boards):
    last, m_last = '<span class="muted">No posts</span>', ""
    if n.get("last_thread_id"):
        tid = n["last_thread_id"]
        # the last post's own subject ("Re: ..." for a reply), linked to that post
        href = "/threads/%s?post=%s" % (tid, n["last_post_id"]) if n.get("last_post_id") else "/threads/%s?last=1" % tid
        title = e((n.get("last_thread_title") or "").strip() or "(untitled)")
        last = ('%s<b>Last post</b> by %s<br>in <a href="%s">%s</a><br>on %s'
                % (go_last(tid), user_link(n.get("last_post_username")), href, title, when(n.get("last_post_created_at"))))
        m_last = ('<div class="m-last"><div class="lp"><span>Last post by %s in</span> <a class="lt" href="%s" title="%s">%s</a></div>%s %s</div>'
                  % (user_link(n.get("last_post_username")), href, title, title, when(n.get("last_post_created_at")), go_last(tid)))
    kids = ""
    if n["kids"]:
        kids = '<div class="kids"><b>Child Boards</b>: %s</div>' % ", ".join(
            '<a href="/boards/%s">%s</a>' % (k, e(boards[k]["name"])) for k in n["kids"])
    desc = DESCRIPTIONS.get(str(n["id"]), "")
    mods = moderators_html(n["id"])
    new = is_recent(n.get("last_post_created_at"))
    return ('<tr><td class="ic">%s</td><td class="info"><a class="bn" href="/boards/%s">%s</a>%s%s'
            '<div class="m-stats">%s Posts, %s Topics</div>%s</td>'
            '<td class="stats">%s Posts<br>%s Topics</td><td class="last">%s</td></tr>'
            % (ICON_BOARD % ("new" if new else "", "New posts in the last day" if new else "No new posts in the last day"),
               n["id"], e(n["name"]), ('<div class="desc">%s</div>' % e(desc) if desc else "") + mods, kids,
               num(n.get("post_count")), num(n.get("thread_count")),
               m_last,
               num(n.get("post_count")), num(n.get("thread_count")), last))


def page_home(q):
    f = forum()
    boards = f["boards"]
    out = ['<h1 class="sr">Board index</h1>']
    for name, ids in f["cats"]:
        out.append('<section class="tb" id="c-%s"><h2 class="catbg">%s</h2><table class="grid boards"><tbody>%s</tbody></table></section>'
                   % (re.sub(r"[^a-z0-9]+", "-", name.lower()), e(name), "".join(board_row(boards[i], boards) for i in ids)))
    total_posts = sum(int(b.get("post_count") or 0) for b in boards.values())
    total_topics = sum(int(b.get("thread_count") or 0) for b in boards.values())
    recent = "".join(
        "<li>%s by %s in %s <span class=\"muted\">%s</span></li>"
        % ('<a href="/threads/%s?last=1">%s</a>' % (t["id"], e(clean_title(t.get("title")))),
           user_link((t.get("user") or {}).get("username")),
           '<a href="/boards/%s">%s</a>' % (t["board"]["id"], e(t["board"]["name"])) if t.get("board") else "",
           when((t.get("post") or {}).get("created_at")))
        for t in f["recent"])
    out.append('<section class="tb"><h2 class="catbg">Info Center</h2><table class="grid"><tbody>'
               '<tr><td class="titlebg" colspan="2">Recent Posts</td></tr><tr><td class="ic">%s</td><td class="w2"><ul class="plain">%s</ul>'
               '<p class="small"><a href="/recent">View the most recent posts</a></p></td></tr>'
               '<tr><td class="titlebg" colspan="2">Forum Stats</td></tr><tr><td class="ic">%s</td><td class="w2 small">'
               '%s Posts in %s Topics across %s boards</td></tr></tbody></table></section>'
               % (ICON_POST, recent, ICON_POST, num(total_posts), num(total_topics), num(len(boards))))
    return layout("", "".join(out), active="boards", canonical="/")


def page_recent(q):
    f = forum()
    rows = "".join(
        '<tr><td class="info"><a class="tn" href="/threads/%s?last=1">%s</a></td><td class="w1">%s</td>'
        '<td class="w2">%s</td><td class="w1 small">%s</td></tr>'
        % (t["id"], e(clean_title(t.get("title"))),
           '<a href="/boards/%s">%s</a>' % (t["board"]["id"], e(t["board"]["name"])) if t.get("board") else "",
           user_link((t.get("user") or {}).get("username")), when((t.get("post") or {}).get("created_at")))
        for t in f["recent"])
    body = ('%s<div class="tb"><h1 class="catbg">Recent Posts</h1><table class="grid topics"><thead><tr class="titlebg">'
            '<th scope="col">Topic</th><th scope="col">Board</th><th scope="col">Last post by</th><th scope="col">Date</th>'
            '</tr></thead><tbody>%s</tbody></table></div><p class="small muted">Each board\'s topic list is sorted by latest reply as well.</p>'
            % (crumbs_html([("Recent Posts", "/recent")]), rows))
    return layout("Recent Posts", body, active="recent", canonical="/recent")


def page_board(bid, q):
    f = forum()
    boards = f["boards"]
    node = boards.get(bid)
    page = max(1, int(q.get("page", ["1"])[0] or 1))
    if (page - 1) * PER_PAGE >= MAX_WINDOW:
        page = MAX_WINDOW // PER_PAGE
    sort = (q.get("sort", [""])[0] or "").lower()
    sort = sort if sort in SORT_COLUMNS else ""
    direction = "asc" if (q.get("dir", [""])[0] or "").lower() == "asc" else "desc"
    params = {"board_id": bid, "limit": PER_PAGE, "page": page}
    if sort:
        params.update({"sort": sort, "dir": direction})
    data = api("/api/threads", params, ttl=20)
    board = data["board"]
    bump_board = bool(data.get("bump_board"))
    default_sort = not sort or (sort == "last_post" and direction == "desc" and not bump_board)
    if not node and not data.get("normal"):
        raise NotFound()
    name = (node or board).get("name") or "Board"
    total = int(board.get("thread_count") or 0)
    pages = max(1, min(math.ceil(total / PER_PAGE), MAX_WINDOW // PER_PAGE))
    plain = "/boards/%s" % bid
    base = plain if default_sort else "%s?sort=%s&amp;dir=%s" % (plain, sort, direction)

    def th(label, key, cls=""):
        """Column header with an up and a down arrow; the active one is marked."""
        def arrow(d, sym):
            on = (sort == key and direction == d) or (not sort and not bump_board and key == "last_post" and d == "desc")
            return '<a href="%s?sort=%s&amp;dir=%s"%s title="Sort by %s, %s" aria-label="Sort by %s, %s">%s</a>' % (
                plain, key, d, ' class="on" aria-current="true"' if on else "", label.lower(),
                "ascending" if d == "asc" else "descending", label.lower(), "ascending" if d == "asc" else "descending", sym)
        return '<th scope="col"%s>%s <span class="sort">%s%s</span></th>' % (
            ' class="%s"' % cls if cls else "", label, arrow("asc", "&#9650;"), arrow("desc", "&#9660;"))
    bump_th = ""
    if bump_board:
        bump_th = ('<th scope="col" class="n bump">Bump <span class="sort"><a href="%s"%s title="Bitcointalk\'s own order for this board" '
                   'aria-label="Sort by bump order">&#9660;</a></span></th>' % (plain, ' class="on" aria-current="true"' if not sort else ""))
    m_sort = '<p class="m-sort small">Sort: %s%s</p>' % (
        '<a href="%s"%s>Bump order</a> · ' % (plain, ' class="on"' if not sort else "") if bump_board else "",
        " · ".join('%s <a href="%s?sort=%s&amp;dir=asc"%s>&#9650;</a><a href="%s?sort=%s&amp;dir=desc"%s>&#9660;</a>'
                   % (lab, plain, k, ' class="on"' if sort == k and direction == "asc" else "", plain, k,
                      ' class="on"' if (sort == k and direction == "desc") or (not sort and not bump_board and k == "last_post") else "")
                   for k, lab in (("last_post", "Last post"), ("replies", "Replies"), ("views", "Views"), ("subject", "Subject"), ("started", "Started"))))
    rows = []
    rank = (page - 1) * PER_PAGE
    for t in (data.get("sticky") or []) + (data.get("normal") or []):
        pc = int(t.get("post_count") or 1)
        bump_td = ""
        if bump_board:
            if not t.get("sticky"):
                rank += 1
            bump_td = '<td class="w1 n bump">%s</td>' % (num(rank) if not sort and not t.get("sticky") else "")
        tp = math.ceil(pc / PER_PAGE)
        mini = ""
        if tp > 1:
            nums = [1, 2, 3] if tp <= 4 else [1, 2]
            links = ['<a href="/threads/%s%s">%d</a>' % (t["id"], "" if p == 1 else "?page=%d" % p, p) for p in nums if p <= tp]
            if tp > 4:
                links.append("...")
            if tp >= 4:
                links.append('<a href="/threads/%s?page=%d">%s</a>' % (t["id"], tp, num(tp)))
            mini = ' <span class="mini">« %s »</span>' % " ".join(links)
        hot = "hot" if pc > 100 else ("new" if is_recent(t.get("last_post_created_at")) else "")
        starter = user_link(t.get("started_by")) if t.get("started_by") else '<span class="muted">-</span>'
        rows.append(
            '<tr><td class="ic">%s</td><td class="info">%s<a class="tn" href="/threads/%s">%s</a>%s'
            '<div class="m-stats">Started by %s · %s replies · %s views</div>'
            '<div class="m-last">Last post by %s, <a href="/threads/%s?last=1">%s</a> %s</div></td>'
            '%s<td class="w2 starter">%s</td><td class="w1 n">%s</td><td class="w1 n">%s</td>'
            '<td class="last">%s<a href="/threads/%s?last=1">%s</a><br>by %s</td></tr>'
            % (ICON_TOPIC % hot, ICON_PIN if t.get("sticky") else "", t["id"], e(clean_title(t.get("title"))), mini, starter, num(max(0, pc - 1)),
               num(t.get("view_count")), user_link(t.get("last_post_username")), t["id"], when(t.get("last_post_created_at")), go_last(t["id"]),
               bump_td, starter, num(max(0, pc - 1)), num(t.get("view_count")),
               go_last(t["id"]), t["id"], when(t.get("last_post_created_at")), user_link(t.get("last_post_username"))))
    kids = ""
    if node and node["kids"]:
        kids = ('<section class="tb"><h2 class="catbg">Child Boards</h2><table class="grid boards"><tbody>%s</tbody></table></section>'
                % "".join(board_row(boards[k], boards) for k in node["kids"]))
    pg = pager(base, page, pages, "Topic list pages")
    buttons = ('<span class="btns"><a class="btn" href="/compose?board=%s">New topic</a>%s</span>'
               % (bid, act(bct("action=notifyboard;board=%s.0" % bid), "Notify", "Get notified about new topics", "btn")))
    capped = ""
    if math.ceil(total / PER_PAGE) > pages and page == pages:
        capped = '<p class="small muted">Older topics in this board are reachable through search.</p>'
    desc = DESCRIPTIONS.get(str(bid), "")
    heads = (th("Subject", "subject") + bump_th + th("Started by", "started", "starter") + th("Replies", "replies", "n")
             + th("Views", "views", "n") + th("Last post", "last_post"))
    body = ('%s%s<div class="bar">%s%s</div><div class="tb"><h1 class="catbg">%s%s</h1>%s%s<table class="grid topics"><thead><tr class="titlebg">'
            '<th scope="col" class="ic"><span class="sr">Status</span></th>%s</tr></thead>'
            '<tbody>%s</tbody></table></div><div class="bar">%s%s</div>%s'
            % (crumbs_html(board_trail(bid) or [(name, base)]), kids, pg, buttons, e(name),
               ' <span class="sub">%s</span>' % e(desc) if desc else "",
               '<div class="pad small">%s</div>' % moderators_html(bid) if moderators_html(bid) else "", m_sort, heads,
               "".join(rows) or '<tr><td colspan="7" class="muted">No topics.</td></tr>', pg, buttons, capped))
    return layout(name, body, active="boards", canonical=plain if page == 1 else "%s?page=%d" % (plain, page), noindex=not default_sort, feed="/feed/boards/%d" % bid)


# --------------------------------------------------------------------------- topics
def poll_html(poll, tid=0):
    """The topic's poll, read-only: question, options with votes and bars, total voters and its state.
    Voting happens on bitcointalk.org."""
    if not poll or not poll.get("answers"):
        return ""
    answers = poll["answers"]
    shown = poll.get("display_mode") == "always" and any(a.get("votes") is not None for a in answers)
    total = sum(int(a.get("votes") or 0) for a in answers)
    rows = []
    for a in answers:
        v = int(a.get("votes") or 0)
        if shown:
            pct = 100.0 * v / total if total else 0.0
            rows.append('<tr><td class="pl">%s</td><td class="pb"><span style="width:%.1f%%"></span></td><td class="n">%s (%.1f%%)</td></tr>'
                        % (e(a.get("answer") or ""), pct, num(v), pct))
        else:
            rows.append('<tr><td class="pl" colspan="3">%s</td></tr>' % e(a.get("answer") or ""))
    voters = poll.get("total_voters")
    notes = []
    if shown:
        notes.append("Total Voters: <b>%s</b>" % num(voters if voters is not None else total))
    else:
        notes.append("Results are hidden for now; Bitcointalk shows them after you vote or when the poll closes.")
    expiry = parse_dt(poll.get("expiration")) if poll.get("expiration") else None
    if poll.get("locked"):
        notes.append('<b class="neg">Voting closed</b>%s' % (" on %s" % when(poll["expiration"], short=True) if expiry else ""))
    elif expiry:
        notes.append("Voting closes on %s" % when(poll["expiration"]))
    if int(poll.get("max_answers") or 1) > 1:
        notes.append("Up to %s options per voter" % num(poll["max_answers"]))
    vote = ""
    if tid:
        vote = '<p class="btns">%s</p>' % act(bct("topic=%d.0" % tid), "Voting closed: view on Bitcointalk" if poll.get("locked") else "Vote on Bitcointalk",
                                             "Opens the topic on bitcointalk.org, where members vote", "btn")
    return ('<section class="tb poll"><h2 class="catbg">Poll</h2><div class="pad"><p><b>Question:</b> %s</p>'
            '<table class="pollt"><tbody>%s</tbody></table><p class="small muted pnote">%s</p>%s</div></section>'
            % (e(poll.get("question") or ""), "".join(rows), " · ".join(notes), vote))


def page_thread(tid, q):
    if q.get("post", [""])[0].isdigit():
        pid = int(q["post"][0])
        try:
            p = api("/api/posts/%d" % pid, ttl=600)
            tid = int(p.get("thread_id") or tid)
        except NotFound:
            return ("redirect", "/threads/%d" % tid)
        page = find_post_offset(tid, pid) // PER_PAGE + 1
        return ("redirect", "/threads/%d%s#%d" % (tid, "" if page == 1 else "?page=%d" % page, pid))
    if q.get("last"):
        first = api("/api/posts", {"thread_id": tid, "limit": 1, "start": 0}, ttl=20)
        cnt = int(first["thread"].get("post_count") or 0)
        page = max(1, math.ceil(cnt / PER_PAGE))
        return ("redirect", "/threads/%d%s#last" % (tid, "" if page == 1 else "?page=%d" % page))
    start = q.get("start", [""])[0]
    page = int(start) // PER_PAGE + 1 if start.isdigit() else max(1, int(q.get("page", ["1"])[0] or 1))
    data = api("/api/posts", {"thread_id": tid, "limit": PER_PAGE, "page": page}, ttl=15)
    # never show the scraper's page-change markers ("sitemap:<topic>:<offset>") as posts
    posts = [p for p in data.get("posts") or [] if not str(p.get("id") or "").startswith("sitemap:")]
    thread = data["thread"]
    count = int(thread.get("post_count") or 0)
    pages = max(1, math.ceil(count / PER_PAGE))
    if not posts:
        if count and page > pages:
            return ("redirect", "/threads/%d?page=%d" % (tid, pages))
        raise NotFound()
    title = clean_title(thread.get("title"))
    base = "/threads/%d" % tid
    here = base if page == 1 else "%s?page=%d" % (base, page)
    bid = thread.get("board_id")
    crumbs = (board_trail(bid) if bid is not None else []) or [(thread.get("board_name") or "Board", "/boards/%s" % bid)]
    crumbs.append((title, base))
    on_page = {str(p["id"]) for p in posts}
    rows = []
    for i, p in enumerate(posts):
        u = p.get("user") or {}
        name = u.get("username") or "(unknown)"
        uid = u.get("id") or ""
        pid = p["id"]
        info = []
        if u.get("rank"):
            info.append('<div class="rank">%s</div>' % e(u["rank"]))
        if isinstance(u.get("activity"), int) and u["activity"] >= 0:
            info.append("<div>Activity: %s</div>" % num(u["activity"]))
        info.append("<div>Merit: %s</div>" % num(u.get("merit") or 0))
        av = avatar_html(p.get("avatar") or u.get("avatar"), name, 64)
        tr = ""
        ulinks = ""
        if uid:
            # Trust on its own line under the avatar, as on the forum: Trust: +7 / =2 / -0 (links to the trust page).
            score = trust_html(u.get("stats"), "")
            tr = ('<a class="utrust" href="/profile/%s/trust" rel="nofollow" title="Trust feedback and trust lists">Trust: %s</a>'
                  % (urllib.parse.quote(name if name != "(unknown)" else str(uid), safe=""), score or '<span class="muted">not rated</span>'))
            ulinks = '<div class="ulinks">%s %s</div>' % (
                '<a class="pop pm" href="%s" target="_blank" rel="noopener nofollow" title="Send this member a personal message" aria-label="Send a personal message">%s</a>'
                % (e(bct("action=pm;sa=send;u=%s" % uid)), ICON_PM),
                act(bct("topic=%d.msg%s#msg%s" % (tid, pid, pid)), "Ignore", "Opens the post on bitcointalk.org, where Ignore sits under the author"))
        merit = ""
        if p.get("merit"):
            mt = p["merit"]
            head = 'Merited: <b>+%s</b> by %s member%s' % (num(mt.get("total")), num(mt.get("senders")), "" if mt.get("senders") == 1 else "s")
            if mt.get("by"):
                merit = ('<details class="merited" open><summary title="Show or hide who sent merit">%s%s</summary><div class="mby">%s</div></details>'
                         % (ICON_FOLD, head, ", ".join("%s (%s)" % (user_link(x["username"]), num(x["merit"])) for x in mt["by"])))
            else:
                merit = '<div class="merited">%s</div>' % head
        sig = '<div class="sig" aria-label="Signature"><div class="sigin">%s</div></div>' % balanced(u["signature"], tight=True) if u.get("signature") else ""
        tools = '<a href="/compose?topic=%d&amp;quote=%s">Quote</a> %s' % (
            tid, pid, act(bct("action=merit;msg=%s" % pid), "Merit", "Send merit for this post"))
        acts = "".join([
            act(bct("action=reporttm;topic=%d.0;msg=%s" % (tid, pid)), "Report", "Report this post to the moderators"),
            '<a href="/history/%s" rel="nofollow" title="Earlier versions of this post, kept by the BitList Archive">Edit history</a>' % pid,
            act(bct("topic=%d.msg%s#msg%s" % (tid, pid, pid)), "Original", "This post on bitcointalk.org")])
        rows.append(
            '<tr class="post %s" id="%s"><td class="poster">%s<b class="pname">%s</b>%s%s%s%s</td>'
            '<td class="msg"><div class="phead">%s<div class="pt"><a class="subject" href="%s#%s">%s%s</a>'
            '<div class="small">%s</div>%s</div><span class="ptools">%s</span><a class="pnum" href="%s#%s" title="Link to this post">#%s</a></div>'
            '<div class="content">%s</div>%s<div class="acts">%s</div></td></tr>'
            % ("alt" if i % 2 else "", pid, '<span id="last"></span>' if i == len(posts) - 1 else "",
               user_link(name), "".join(info), av, '<div>%s</div>' % tr if tr else "", ulinks,
               ICON_POST, here, pid, "" if p.get("position") == 1 else "Re: ", e(title), when(p.get("created_at")), merit, tools,
               here, pid, num(p.get("position")), post_body(p.get("body_html"), tid, on_page), sig, acts))
    pg = pager(base, page, pages, "Topic pages")
    buttons = ('<span class="btns"><button class="btn sigs" type="button" hidden title="Remembered in this browser only">'
               'Hide signatures</button><a class="btn" href="/compose?topic=%d">Reply</a>%s%s</span>' % (
                   tid, act(bct("action=notify;topic=%d.0" % tid), "Notify", "Get notified about replies", "btn"),
                   act(bct("action=printpage;topic=%d.0" % tid), "Print", "", "btn")))
    body = ('%s%s<div class="bar">%s%s</div><div class="tb"><table class="grid posts"><thead><tr class="catbg">'
            '<th scope="col" class="poster">Author</th><th scope="col">Topic: <h1>%s</h1> <span class="sub">(%s posts)</span></th></tr></thead>'
            '<tbody>%s</tbody></table></div><div class="bar">%s%s</div>'
            % (crumbs_html(crumbs), poll_html(thread.get("poll"), tid) if page == 1 else "", pg, buttons, e(title), num(count),
               "".join(rows), pg, buttons))
    return layout(title, body, active="boards", canonical=here, feed="/feed/threads/%d" % tid)


# --------------------------------------------------------------------------- post edit history
def _plain(body_html):
    """Post HTML as plain text for comparing versions: line breaks kept, tags dropped."""
    t = re.sub(r"(?i)<br\s*/?>|</div>|</p>|</li>|</tr>", "\n", body_html or "")
    return html.unescape(re.sub(r"<[^>]+>", "", t)).strip()


def diff_html(old, new):
    """Word-level difference between two versions: (html, words added, words removed), or None if too long to compare."""
    import difflib
    a, b = re.findall(r"\s+|\S+", _plain(old)), re.findall(r"\s+|\S+", _plain(new))
    if len(a) > 20000 or len(b) > 20000:
        return None
    out, added, removed = [], 0, 0
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            seg = "".join(a[i1:i2])
            if len(seg) > 400:      # long unchanged stretches are shortened so the changes stand out
                seg = seg[:160] + " … " + seg[-160:]
            out.append(e(seg))
            continue
        if op in ("delete", "replace"):
            removed += sum(1 for w in a[i1:i2] if not w.isspace()); out.append("<del>%s</del>" % e("".join(a[i1:i2])))
        if op in ("insert", "replace"):
            added += sum(1 for w in b[j1:j2] if not w.isspace()); out.append("<ins>%s</ins>" % e("".join(b[j1:j2])))
    return "".join(out), added, removed


def changes_block(old, new, label):
    d = diff_html(old, new)
    if d is None:
        return '<p class="small muted">These versions are too long to compare here.</p>'
    body, added, removed = d
    if not added and not removed:
        return '<p class="small muted">Same wording as %s (only formatting, links or pictures differ, if anything).</p>' % e(label)
    return ('<details class="chg" open><summary>What changed from %s: <span class="pos">+%s word%s</span>, <span class="neg">−%s word%s</span></summary>'
            '<div class="diff">%s</div></details>' % (e(label), num(added), "" if added == 1 else "s", num(removed), "" if removed == 1 else "s", body))


def page_history(pid):
    post = api("/api/posts/%d" % pid, ttl=60)
    tid = int(post.get("thread_id") or 0)
    name = (post.get("user") or {}).get("username") or "(unknown)"
    back = "/threads/%d?post=%d#%d" % (tid, pid, pid)
    bitlist = act("https://bitlist.co/post/%d" % pid, "Open this post on BitList")
    try:
        hist = api("/api/posts/%d/history" % pid, ttl=300)
    except (NotFound, Upstream):
        hist = {"available": False, "busy": True}
    versions = hist.get("versions") or []
    rows = []
    for i, v in enumerate(versions):
        rows.append('<tr class="post %s"><td class="poster"><b>Version %d</b><div>%s</div>%s%s</td>'
                    '<td class="msg"><div class="content">%s</div></td></tr>'
                    % ("alt" if i % 2 else "", i + 1, when(v.get("date")),
                       '<div class="neg">Post was deleted</div>' if v.get("deleted") else "",
                       '<div class="small muted">%s</div>' % {"talksearch": "Recorded by TalkSearch", "loyce": "As first posted (LoyceV's archive)"}.get(v.get("source"), "Recorded by BitList"),
                       post_body(v.get("body_html"), tid, ()) + (changes_block(versions[i - 1].get("body_html"), v.get("body_html"), "version %d" % i) if i else "")))
    if not hist.get("available"):
        note = "<p>The edit history could not be loaded just now. Try again in a minute, or use the link below.</p>"
    elif not versions:
        note = ("<p>This post reads the same as when it was first posted (checked against LoyceV's archive).</p>" if hist.get("unchanged_since_posting")
                else "<p>No earlier version of this post is on record: it was not edited, or the edit happened when nobody was recording.</p>")
    else:
        note = "<p>%d recorded version%s, oldest first. The version now in the archive is shown last.</p>" % (
            len(versions), "" if len(versions) == 1 else "s")
    rows.append('<tr class="post"><td class="poster"><b>In the archive now</b><div>%s</div></td>'
                '<td class="msg"><div class="content">%s</div></td></tr>'
                % (when(post.get("created_at")), post_body(post.get("body_html"), tid, ())
                   + (changes_block(versions[-1].get("body_html"), post.get("body_html"), "version %d" % len(versions)) if versions else "")))
    body = ('%s<div class="tb"><h1 class="catbg">Edit history of a post by %s</h1><div class="pad">%s'
            '<p class="small">Edits made since 13 November 2026 are recorded by TalkSearch itself. Older versions (up to April 2025) come from the '
            '<a href="https://bitlist.co" rel="noopener">BitList Archive</a>, and the post as first made comes from '
            '<a href="https://loyce.club" rel="noopener">LoyceV\'s archive</a>; both are shown with permission. %s</p></div>'
            '<table class="grid posts"><tbody>%s</tbody></table></div>'
            % (crumbs_html([("Back to the post", back), ("Edit history", "")]), e(name), note, bitlist, "".join(rows)))
    return layout("Edit history", body, noindex=True, active="boards")


# --------------------------------------------------------------------------- composer
# Toolbar icons: original 16x16 line drawings (MIT, like the rest of this file).
_TOOL_ICON = ('<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6" '
              'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">%s</svg>')
TOOL_ICONS = {
    "b": '<path d="M4.5 2.5h4a2.7 2.7 0 0 1 0 5.4h-4zM4.5 7.9h4.6a2.8 2.8 0 0 1 0 5.6H4.5z" stroke-width="2.2"/>',
    "i": '<path d="M7 2.5h5M4 13.5h5M9.8 2.5 6.2 13.5"/>',
    "u": '<path d="M4.5 2.5v5a3.5 3.5 0 0 0 7 0v-5M3.5 14h9"/>',
    "s": '<path d="M11.5 4.6C11 3.3 9.700 2.5 8 2.5c-2 0-3.300 1-3.300 2.500 0 1.300.9 2 3.300 2.600M4.300 11c.4 1.500 1.800 2.500 3.700 2.500 2.100 0 3.500-1 3.500-2.600 0-.7-.2-1.200-.7-1.700M2.500 8h11"/>',
    "quote": '<path d="M2.500 9.500c0-3 1.200-5 3.500-6v2c-1 .6-1.500 1.400-1.500 2.500H6.500v4h-4zM9.500 9.500c0-3 1.200-5 3.500-6v2c-1 .6-1.500 1.400-1.500 2.500h2v4h-4z" fill="currentColor" stroke="none"/>',
    "code": '<path d="M5.500 4 1.500 8l4 4M10.500 4l4 4-4 4"/>',
    "url": '<path d="M6.700 9.300a3 3 0 0 0 4.200 0l2.300-2.300a3 3 0 0 0-4.200-4.200L8 3.800M9.300 6.700a3 3 0 0 0-4.200 0L2.800 9a3 3 0 0 0 4.200 4.200L8 12.200"/>',
    "img": '<rect x="1.500" y="2.500" width="13" height="11" rx="1"/><circle cx="5" cy="6" r="1.200"/><path d="m2 12.500 3.500-3.500 2.500 2.500 2.500-3 3.500 4"/>',
    "size": '<path d="M1.500 5V3.500h8V5M5.500 3.500v9M4 12.500h3M9.500 9V8h5v1M12 8v4.500M11 12.500h2"/>',
    "color": '<path d="M4 11.500 8 2.500l4 9M5.400 8.500h5.200"/><path d="M2.500 14.500h11" stroke="#e07d08" stroke-width="2.4"/>',
    "list": '<path d="M6 4h8M6 8h8M6 12h8"/><circle cx="2.700" cy="4" r=".9" fill="currentColor"/><circle cx="2.700" cy="8" r=".9" fill="currentColor"/><circle cx="2.700" cy="12" r=".9" fill="currentColor"/>',
    "center": '<path d="M2 3.500h12M4.500 6.500h7M2 9.500h12M4.500 12.500h7"/>',
    "tt": '<path d="m2.500 4.500 3.500 3.500-3.500 3.500M8.500 12h5.500"/>',
    "hr": '<path d="M1.500 8h13" stroke-width="2"/><path d="M4 4h8M4 12h8" stroke-width="1" opacity=".45"/>',
}



def page_compose(q):
    """Write a reply, quote or new topic here; it is then pasted into the real form on
    bitcointalk.org. Nothing typed on this page is sent to this server."""
    tid = q.get("topic", [""])[0]
    bid = q.get("board", [""])[0]
    quote = q.get("quote", [""])[0]
    prefill, heading, crumbs = "", "", []
    if tid.isdigit():
        first = api("/api/posts", {"thread_id": tid, "limit": 1, "start": 0}, ttl=60)
        if not first.get("posts"):
            raise NotFound()
        title = clean_title(first["thread"].get("title"))
        target = bct("action=post;topic=%s.0" % tid)
        key, heading = "reply-%s" % tid, "Reply to: %s" % title
        crumbs = (board_trail(first["thread"].get("board_id")) or []) + [(title, "/threads/%s" % tid), ("Reply", None)]
        if quote.isdigit():
            p = api("/api/posts/%s" % quote, ttl=300)
            d = parse_dt(p.get("created_at"))
            prefill = bbcode.quote_block((p.get("user") or {}).get("username") or "", p.get("thread_id") or tid, p["id"],
                                         int(d.timestamp()) if d else 0, p.get("body_html"))
        subject_value = ("Re: " + title)[:80]
    elif bid.isdigit():
        node = forum()["boards"].get(int(bid))
        if not node:
            raise NotFound()
        target = bct("action=post;board=%s.0" % bid)
        key, heading = "topic-%s" % bid, "New topic in: %s" % node["name"]
        crumbs = board_trail(int(bid)) + [("New topic", None)]
        subject_value = ""
    else:
        raise NotFound()
    subject = ('<p><label for="subject"><b>Subject</b></label><br><span class="row"><input id="subject" type="text" maxlength="80" value="%s">'
               '<button type="button" class="btn" data-copy="subject">Copy subject</button></span></p>' % e(subject_value))
    tools = "".join('<button type="button" data-tag="%s" title="%s" aria-label="%s">%s</button>' % (t, e(tip), e(tip), _TOOL_ICON % TOOL_ICONS[t])
                    for t, tip in (
        ("b", "Bold"), ("i", "Italic"), ("u", "Underline"), ("s", "Strikethrough"), ("quote", "Quote"), ("code", "Code"),
        ("url", "Link"), ("img", "Image"), ("size", "Text size"), ("color", "Text colour"), ("list", "List"),
        ("center", "Centre"), ("tt", "Monospace"), ("hr", "Horizontal line")))
    uploader = ('<p class="row upl"><button type="button" class="btn" id="upload">Upload image</button>'
                '<input type="file" id="file" accept="image/png,image/jpeg,image/gif,image/webp" hidden>'
                '<span class="small muted">or paste or drop a picture into the message. It is uploaded to '
                '<a href="https://talkimg.com" rel="noopener">TalkImg</a> through this site (PNG, JPEG, GIF, WebP, up to 8 MB).</span></p>'
                if talkimg.enabled() else "")
    body = ('%s<div class="tb"><h1 class="catbg">%s</h1><div class="pad composer" id="composer" data-key="%s" data-target="%s">'
            '<noscript><p class="warn">The composer needs JavaScript. You can also %s.</p></noscript>'
            '<h2 class="titlebg">Preview</h2><div id="preview" class="content preview" aria-live="polite"></div>%s'
            '<div class="toolbar" role="toolbar" aria-label="Formatting">%s</div>'
            '<label class="sr" for="msg">Message (BBCode)</label><textarea id="msg" rows="12" spellcheck="true"></textarea>'
            '%s'
            '<textarea id="prefill" hidden>%s</textarea>'
            '<p class="row"><button type="button" class="btn primary" id="go">Copy and open Bitcointalk</button>'
            '<button type="button" class="btn" data-copy="msg">Copy only</button>'
            '<button type="button" class="btn" id="clear">Clear draft</button><span id="status" role="status" class="small"></span></p>'
            '<p class="small muted">Your draft is kept only in this browser. The text you type here is not sent to this site, and this site never asks for '
            'your Bitcointalk login. Pressing the first button copies the message and opens the real form on bitcointalk.org in a new window: '
            '<b>paste your message there and press Post.</b> If you changed the subject, check the Subject box there before posting.</p>'
            '</div></div>'
            % (crumbs_html(crumbs), e(heading), e(key), e(target), act(target, "write directly on bitcointalk.org"),
               subject, tools, uploader, e(prefill)))
    return layout(heading, body, noindex=True, active="boards", scripts=("composer.js",))


# --------------------------------------------------------------------------- members
def post_list(posts, titles=None, show_author=True):
    out = []
    for p in posts:
        tid = p.get("thread_id") or p.get("topic_id")
        pid = p.get("id") or p.get("post_id")
        title = p.get("thread_title") or p.get("topic_title") or (titles or {}).get(str(tid)) or "Topic %s" % tid
        board = ' in <a href="/boards/%s">%s</a>' % (p.get("board_id"), e(p["board_name"])) if p.get("board_name") else ""
        who = (p.get("user") or {}).get("username") or p.get("author")
        author = " by %s" % user_link(who) if show_author else ""
        merit = ' · <span class="merited">Merit +%s</span>' % num(p["post_merit_total"]) if p.get("post_merit_total") else ""
        out.append('<tr><td class="info"><a class="tn" href="/threads/%s?post=%s#%s">%s</a><div class="small">%s%s%s%s</div>'
                   '<div class="excerpt">%s</div></td></tr>'
                   % (tid, pid, pid, e(clean_title(title)), when(p.get("created_at") or p.get("date")), author, board, merit,
                      excerpt(p.get("body_html") or p.get("content"))))
    return '<table class="grid results"><tbody>%s</tbody></table>' % "".join(out)


def optional(path, ttl):
    """Data the API may not offer yet (older API): None instead of an error."""
    try:
        return api(path, ttl=ttl)
    except (NotFound, Upstream):
        return None


def page_profile(name, q, sub=""):
    quoted = urllib.parse.quote(name, safe="")
    u = api("/api/users/%s" % quoted, {"fresh": 1}, ttl=60)     # fresh: stale profile figures are re-read first
    uname = u.get("username") or name
    uid = u.get("id")
    page = max(1, int(q.get("page", ["1"])[0] or 1))
    if (page - 1) * PER_PAGE >= MAX_WINDOW:
        page = MAX_WINDOW // PER_PAGE
    pd = api("/api/posts/user/%s" % quoted, {"limit": PER_PAGE, "page": page}, ttl=30)
    lists = optional("/api/trust/%s/lists" % quoted, 600) if page == 1 else None
    stats = optional("/api/users/%s/stats" % quoted, 600) if page == 1 else None
    count = int(pd.get("count") or 0)
    pages = max(1, min(math.ceil(count / PER_PAGE), MAX_WINDOW // PER_PAGE))
    base = "/profile/%s" % urllib.parse.quote(uname, safe="")
    facts = [("Name", e(uname))]
    if u.get("rank"):
        facts.append(("Position", e(u["rank"])))
    facts.append(("Posts in archive", num(u.get("post_count"))))
    if isinstance(u.get("activity"), int) and u["activity"] >= 0:
        facts.append(("Activity", num(u["activity"])))
    facts.append(("Merit", "%s · %s" % (
        act(bct("action=merit;u=%s" % uid), "%s received%s" % (num(u.get("merit") or 0), ", %s sent" % num(u["merit_sent"]) if u.get("merit_sent") is not None else ""),
            "Merit sent and received, on bitcointalk.org"),
        act(u.get("merit_history_url") or "https://loyce.club/Merit/history/%s.html" % uid, "history", "LoyceV's merit history"))))
    if stats:
        facts.append(("Topics posted in", num(stats.get("topics"))))
        facts.append(("First archived post", when(stats.get("first"), short=True)))
        facts.append(("Last archived post", when(stats.get("last"), short=True)))
    elif u.get("created_at"):
        facts.append(("First archived post", when(u["created_at"], short=True)))
    tr = trust_html(u.get("stats"), "")
    facts.append(("Trust", '<a href="%s/trust" rel="nofollow" title="Trust feedback and trust lists">%s</a>'
                  % (base, tr or '<span class="muted">no feedback counted by DefaultTrust</span>')))
    st_ = u.get("stats") or {}
    facts.append(("Trust lists", '%s<a href="%s/trust#lists" rel="nofollow">included by %s member%s, excluded by %s</a>' % (
        "DT%d member; " % st_["dt"] if st_.get("dt") else "", base, num(st_.get("pos") or 0), "" if st_.get("pos") == 1 else "s", num(st_.get("neg") or 0))))
    dl = "".join('<tr><th scope="row">%s:</th><td>%s</td></tr>' % f for f in facts)
    actions = " ".join([
        '<a class="btn" href="%s/stats">Statistics</a>' % base,
        act(bct("action=pm;sa=send;u=%s" % uid), "Send PM", "", "btn"),
        act(bct("action=profile;u=%s;sa=showPosts" % uid), "All posts", "", "btn"),
        act(u.get("profile_on_bitcointalk") or bct("action=profile;u=%s" % uid), "Original profile", "", "btn")])
    extra = []
    if lists and any(lists.values()):
        def names(key):
            return ", ".join('<b title="DT%d member">%s</b>' % (x["dt"], user_link(x["username"])) if x.get("dt") else user_link(x["username"])
                             for x in lists[key]) or '<span class="muted">nobody</span>'
        extra.append('<details class="tb"><summary class="titlebg">Trust lists (DefaultTrust network, LoyceV snapshot)</summary><table class="grid kv"><tbody>'
                     '<tr><th scope="row">Included by (%d)</th><td>%s</td></tr><tr><th scope="row">Excluded by (%d)</th><td>%s</td></tr>'
                     '<tr><th scope="row">Includes (%d)</th><td>%s</td></tr><tr><th scope="row">Excludes (%d)</th><td>%s</td></tr>'
                     '</tbody></table><p class="pad small muted">%sNames in <b>bold</b> are DefaultTrust members (DT1 or DT2). '
                     'Feedback with comments is on the <a href="%s/trust" rel="nofollow">Trust</a> page.</p>'
                     '<p class="pad small">%s</p></details>'
                     % (len(lists["trusted_by"]), names("trusted_by"), len(lists["distrusted_by"]), names("distrusted_by"),
                        len(lists["trusts"]), names("trusts"), len(lists["distrusts"]), names("distrusts"),
                        "%s is a DT%d member. " % (e(uname), lists["dt"]) if lists.get("dt") else "", base,
                        act(bct("action=trust;settings"), "Trust settings", "Edit your own trust list on bitcointalk.org")))
    if stats and stats.get("boards"):
        top = max(b["posts"] for b in stats["boards"]) or 1
        brow = "".join('<tr><td><a href="/boards/%s">%s</a></td><td class="pb"><span style="width:%.0f%%"></span></td><td class="n">%s</td></tr>'
                       % (b["id"], e(b["name"]), 100.0 * b["posts"] / top, num(b["posts"])) for b in stats["boards"])
        ytop = max(y["posts"] for y in stats["years"]) or 1
        yrow = "".join('<tr><td>%s</td><td class="pb"><span style="width:%.0f%%"></span></td><td class="n">%s</td></tr>'
                       % (y["year"], 100.0 * y["posts"] / ytop, num(y["posts"])) for y in stats["years"])
        extra.append('<details class="tb"><summary class="titlebg">Posting statistics (from the archive)</summary><div class="two">'
                     '<table class="pollt"><caption>Most active boards</caption><tbody>%s</tbody></table>'
                     '<table class="pollt"><caption>Posts per year</caption><tbody>%s</tbody></table></div></details>' % (brow, yrow))
    sig = ('<div class="sigbox"><b>Signature:</b><div class="sig prof"><div class="sigin">%s</div></div></div>' % balanced(u["signature"], tight=True)
           if u.get("signature") else "")
    pg = pager(base, page, pages, "Post history pages")
    body = ('%s<div class="tb"><h1 class="catbg">Summary - %s</h1><div class="summary"><table class="kv"><tbody>%s</tbody></table>'
            '<div class="av">%s</div></div>%s<div class="pad btns">%s</div></div>%s'
            '<div class="bar">%s</div><div class="tb"><h2 class="catbg">Posts by %s</h2>%s</div><div class="bar">%s</div>'
            % (crumbs_html([("Members", "/search/users"), (uname, base)]), e(uname), dl, avatar_html(u.get("avatar"), uname, 96), sig,
               actions, "".join(extra), pg, e(uname),
               post_list(pd.get("posts") or [], show_author=False) if pd.get("posts") else '<p class="pad muted">No archived posts.</p>', pg))
    return layout(uname, body, active="members", canonical=base if page == 1 else "%s?page=%d" % (base, page), feed="/feed" + base)


# --------------------------------------------------------------------------- statistics
def bars(caption, rows, unit=""):
    """rows: (label html, number). A small table with proportional bars; no script needed."""
    rows = [r for r in rows if r[1] is not None]
    if not rows:
        return ""
    top = max(r[1] for r in rows) or 1
    return ('<table class="pollt"><caption>%s</caption><tbody>%s</tbody></table>'
            % (e(caption), "".join('<tr><td>%s</td><td class="pb"><span style="width:%.0f%%"></span></td><td class="n">%s%s</td></tr>'
                                   % (label, 100.0 * v / top, num(v), unit) for label, v in rows)))


def merit_post_rows(posts):
    return [('<a href="/threads/%s?post=%s#%s">%s</a>%s' % (p.get("topic_id") or 0, p["post_id"], p["post_id"],
                                                             e(clean_title(p.get("title")) or "Post %s" % p["post_id"]),
                                                             " by %s" % user_link(p["author"]) if p.get("author") else ""), p["merit"])
            for p in posts if p.get("topic_id")]


_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def page_user_stats(name):
    quoted = urllib.parse.quote(name, safe="")
    u = api("/api/users/%s" % quoted, ttl=60)
    uname = u.get("username") or name
    base = "/profile/%s" % urllib.parse.quote(uname, safe="")
    st = api("/api/users/%s/stats" % quoted, ttl=600)
    m = st.get("merit") or {}
    facts = [("Posts in archive", num(u.get("post_count"))), ("Topics posted in", num(st.get("topics"))),
             ("First archived post", when(st.get("first"), short=True)), ("Last archived post", when(st.get("last"), short=True))]
    if m:
        facts += [("Merit received", "%s in %s transactions from %s members" % (num(m["received"]), num(m["received_tx"]), num(m["distinct_senders"]))),
                  ("Merit sent", "%s in %s transactions to %s members" % (num(m["sent"]), num(m["sent_tx"]), num(m["distinct_recipients"])))]
    dl = "".join('<tr><th scope="row">%s:</th><td>%s</td></tr>' % f for f in facts)
    blocks = [
        bars("Posts per year", [(y["year"], y["posts"]) for y in st.get("years") or []]),
        bars("Posts per month, last 24 months", [(x["month"], x["posts"]) for x in st.get("months") or []]),
        bars("Most active boards", [('<a href="/boards/%s">%s</a>' % (b["id"], e(b["name"])), b["posts"]) for b in st.get("boards") or []]),
        bars("Topics with the most posts by %s" % uname, [('<a href="/threads/%s">%s</a>' % (t["id"], e(clean_title(t.get("title")) or "Topic %s" % t["id"])), t["posts"])
                                                          for t in st.get("top_topics") or []]),
        bars("Posts by hour of day (UTC)", [("%02d:00" % h, v) for h, v in enumerate(st.get("hours") or [])]),
        bars("Posts by day of week (UTC)", list(zip(_DAYS, st.get("weekdays") or []))),
    ]
    if m:
        blocks += [
            bars("Merit received per year", [(y["year"], y["received"]) for y in m.get("years") or []]),
            bars("Merit sent per year", [(y["year"], y["sent"]) for y in m.get("years") or []]),
            bars("Merit received from", [(user_link(x["username"]), x["merit"]) for x in m.get("top_senders") or []]),
            bars("Merit sent to", [(user_link(x["username"]), x["merit"]) for x in m.get("top_recipients") or []]),
            bars("Most merited posts", merit_post_rows(m.get("top_posts") or [])),
        ]
    body = ('%s<div class="tb"><h1 class="catbg">Statistics - %s</h1><div class="summary"><table class="kv"><tbody>%s</tbody></table></div>'
            '<div class="two stats-grid">%s</div>'
            '<p class="pad small muted">Posting figures are counted from the posts in this archive. Merit figures come from the public merit data published by LoyceV.</p></div>'
            % (crumbs_html([("Members", "/search/users"), (uname, base), ("Statistics", "")]), e(uname), dl, "".join(b for b in blocks if b)))
    return layout("Statistics - %s" % uname, body, active="members", canonical=base + "/stats")


def page_user_trust(name):
    """A member's trust, as on Bitcointalk's trust page: score, received and sent feedback, DefaultTrust lists."""
    quoted = urllib.parse.quote(name, safe="")
    u = api("/api/users/%s" % quoted, ttl=60)
    uname = u.get("username") or name
    uid = u.get("id")
    base = "/profile/%s" % urllib.parse.quote(uname, safe="")
    try:
        fb = api("/api/trust/%s/feedback" % quoted, ttl=20)
    except (NotFound, Upstream):
        fb = {"available": False, "pending": True}
    lists = optional("/api/trust/%s/lists" % quoted, 600) or {}

    def stamp(ts, short=True):
        try:
            return when(datetime.fromtimestamp(int(ts), timezone.utc).isoformat(), short=short) if ts else ""
        except (TypeError, ValueError, OverflowError):
            return ""

    def table(key, heading, col, anchor):
        rows = fb.get(key) or []
        if not rows:
            return '<div class="tb" id="%s"><h2 class="titlebg">%s (0)</h2><p class="pad muted">None.</p></div>' % (anchor, heading)
        body = "".join(
            '<tr><td>%s</td><td class="nw">%s</td><td class="nw"><b class="%s">%s</b></td><td>%s</td><td class="cm">%s</td></tr>'
            % ('<b title="DT%d member">%s</b>' % (r["dt"], user_link(r["username"])) if r.get("dt") else user_link(r["username"]),
               when(r.get("date"), short=True) or e(r.get("date") or ""),
               {"positive": "pos", "negative": "neg"}.get(r.get("rating"), ""), e((r.get("rating") or "").capitalize()),
               '<a href="%s" rel="noopener nofollow ugc">Reference</a>' % e(r["reference"]) if str(r.get("reference") or "").startswith(("http://", "https://")) else "",
               e(r.get("comment") or ""))
            for r in rows)
        return ('<div class="tb" id="%s"><h2 class="titlebg">%s (%d)</h2><div class="scroll"><table class="grid fb"><thead><tr class="titlebg">'
                '<th scope="col">%s</th><th scope="col">Date</th><th scope="col">Rating</th>'
                '<th scope="col">Reference</th><th scope="col">Comments</th></tr></thead><tbody>%s</tbody></table></div></div>'
                % (anchor, heading, len(rows), col, body))

    def tally(key):
        rows = fb.get(key) or []
        c = {k: sum(1 for r in rows if r.get("rating") == k) for k in ("positive", "neutral", "negative")}
        return ('%s: <b class="pos">%s</b> positive, %s neutral, <b class="neg">%s</b> negative'
                % ('<a href="#%s">%s</a>' % (key, num(len(rows))), num(c["positive"]), num(c["neutral"]), num(c["negative"])))

    tr = trust_html(u.get("stats"), "")
    st_ = u.get("stats") or {}
    dt = lists.get("dt") or st_.get("dt")
    facts = [("Trust", tr or '<span class="muted">no feedback counted by DefaultTrust</span>'),
             ("DefaultTrust", "DT%d member" % dt if dt else '<span class="muted">not a DefaultTrust member</span>'),
             ("On trust lists", '<a href="#lists">included by %s, excluded by %s</a>'
              % (num(len(lists.get("trusted_by") or [])) if lists else num(st_.get("pos") or 0),
                 num(len(lists.get("distrusted_by") or [])) if lists else num(st_.get("neg") or 0)))]
    if fb.get("available"):
        facts += [("Trusted feedback", tally("trusted")), ("Untrusted feedback", tally("untrusted")),
                  ("Sent feedback", '<a href="#sent">%s</a>' % num(len(fb.get("sent") or [])))]
    asof = []
    if fb.get("available"):
        asof.append("Feedback copied from bitcointalk.org on %s%s."
                    % (when(fb.get("fetched_at")) or "an earlier date", " (a newer copy is on its way; reload in a minute)" if fb.get("pending") else ""))
    src = ((st_.get("feedback") or {}).get("source"))
    if src == "loyce" and lists.get("scores_as_of"):
        asof.append("Trust score: LoyceV's weekly list of %s, the score Bitcointalk shows." % stamp(lists["scores_as_of"]))
    elif src == "trust page":
        asof.append("Trust score: counted from the copied feedback (this member is not on LoyceV's weekly list).")
    if lists.get("lists_as_of"):
        asof.append("Trust lists: LoyceV's weekly snapshot of %s." % stamp(lists["lists_as_of"]))
    dl = "".join('<tr><th scope="row">%s:</th><td>%s</td></tr>' % f for f in facts)
    head = ('<div class="tb"><h1 class="catbg">Trust - %s</h1><div class="pad"><table class="kv tsum"><tbody>%s</tbody></table>'
            '<p class="small muted">%s "Trusted" and "untrusted" follow DefaultTrust, as Bitcointalk shows them to guests; a member\'s own trust list only changes what they see when logged in there. Bold names are DefaultTrust members.</p>'
            '<p class="btns">%s %s <a class="small vob" href="%s" target="_blank" rel="noopener nofollow">View on Bitcointalk%s</a></p></div></div>'
            % (e(uname), dl, " ".join(asof),
               act(bct("action=trust;u=%s" % uid), "Leave feedback", "Opens the trust page on bitcointalk.org", "btn"),
               act(bct("action=trust;settings"), "Trust settings", "Edit your own trust list on bitcointalk.org", "btn"),
               e(bct("action=trust;u=%s" % uid)), ICON_EXT))
    if fb.get("available"):
        tables = (table("trusted", "Trusted feedback", "From", "trusted") + table("untrusted", "Untrusted feedback", "From", "untrusted")
                  + table("sent", "Sent feedback", "To", "sent"))
    else:
        tables = ('<div class="tb"><p class="pad">The feedback for this member is being copied from bitcointalk.org. '
                  'Reload this page in a minute.</p></div>')

    def names(key):
        return ", ".join('<b title="DT%d member">%s</b>' % (x["dt"], user_link(x["username"])) if x.get("dt") else user_link(x["username"])
                         for x in lists.get(key) or []) or '<span class="muted">nobody</span>'
    trust_lists = ('<div class="tb" id="lists"><h2 class="titlebg">Trust lists (DefaultTrust network)</h2><table class="grid kv"><tbody>'
                   '<tr><th scope="row">Included by (%d)</th><td>%s</td></tr><tr><th scope="row">Excluded by (%d)</th><td>%s</td></tr>'
                   '<tr><th scope="row">Includes (%d)</th><td>%s</td></tr><tr><th scope="row">Excludes (%d)</th><td>%s</td></tr>'
                   '</tbody></table><p class="pad small muted">From LoyceV\'s weekly snapshot%s. Names in <b>bold</b> are DefaultTrust members (DT1 or DT2).</p></div>'
                   % (len(lists.get("trusted_by") or []), names("trusted_by"), len(lists.get("distrusted_by") or []), names("distrusted_by"),
                      len(lists.get("trusts") or []), names("trusts"), len(lists.get("distrusts") or []), names("distrusts"),
                      " of %s" % stamp(lists.get("lists_as_of")) if lists.get("lists_as_of") else "")) if lists else ""
    body = '%s%s%s%s' % (crumbs_html([("Members", "/search/users"), (uname, base), ("Trust", "")]), head, tables, trust_lists)
    return layout("Trust - %s" % uname, body, active="members", noindex=True)


PERIODS = (("24h", "24 hours"), ("7d", "7 days"), ("30d", "30 days"))


def chart(caption, points, cls=""):
    """points: (label, number). A bar chart as inline SVG: no script, colours from the theme."""
    if not points:
        return ""
    top = max(v for _, v in points) or 1
    n = len(points)
    rects = "".join('<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f"><title>%s: %s</title></rect>'
                    % (i + 0.12, 100 - 100.0 * v / top, 0.76, 100.0 * v / top, e(label), num(v)) for i, (label, v) in enumerate(points) if v)
    return ('<figure class="chart %s"><figcaption>%s <span class="muted">(highest: %s)</span></figcaption>'
            '<svg viewBox="0 0 %d 100" preserveAspectRatio="none" role="img" aria-label="%s">%s</svg>'
            '<div class="axis"><span>%s</span><span>%s</span></div></figure>'
            % (cls, e(caption), num(top), n, e(caption), rects, e(points[0][0]), e(points[-1][0])))


def activity_block(q):
    """Posts and merit for a chosen period, per hour or per day (from /api/stats/activity)."""
    period = (q.get("period", ["24h"])[0] or "24h")
    if period not in dict(PERIODS):
        period = "24h"
    step = (q.get("step", [""])[0] or "")
    if step not in ("hour", "day"):
        step = "day" if period == "30d" else "hour"
    act = optional("/api/stats/activity?period=%s" % period, 300)
    if not act:
        return ""
    m = act.get("merit") or {}
    label = dict(PERIODS)[period]
    def pick(items, cur, key, other):
        return "".join('<a class="btn%s" href="/stats?%s=%s&amp;%s#activity"%s>%s</a>'
                       % (" primary" if k == cur else "", key, k, other, ' aria-current="true"' if k == cur else "", e(v)) for k, v in items)
    picker = ('<div class="pick"><span>Period: %s</span><span>Chart: %s</span></div>'
              % (pick(PERIODS, period, "period", "step=" + step), pick((("hour", "Per hour"), ("day", "Per day")), step, "step", "period=" + period)))
    hours = act.get("hours") or []
    if step == "day":
        days = {}
        for h in hours:
            d = days.setdefault(time.strftime("%b %d", time.gmtime(h["t"])), [0, 0]); d[0] += h["posts"]; d[1] += h["merit"]
        posts = [(k, v[0]) for k, v in days.items()]; merits = [(k, v[1]) for k, v in days.items()]
    else:
        fmt = lambda t: time.strftime("%b %d, %H:00 UTC", time.gmtime(t))
        posts = [(fmt(h["t"]), h["posts"]) for h in hours]; merits = [(fmt(h["t"]), h["merit"]) for h in hours]
    unit = "day" if step == "day" else "hour"
    def board(b):
        return ' <span class="muted">in <a href="/boards/%s">%s</a></span>' % (b["id"], e(b["name"])) if b else ""
    def users(rows, field):
        return [(user_link(x["username"]), x[field]) for x in rows or []]
    lists = [
        bars("Most merit received", users(m.get("receivers"), "merit")) if m else "",
        bars("Most merit sent", users(m.get("senders"), "merit")) if m else "",
        bars("Most active members", users(act.get("posters"), "posts")),
        bars("Most active topics", [('<a href="/threads/%s">%s</a>%s' % (t["id"], e(clean_title(t.get("title")) or "Topic %s" % t["id"]), board(t.get("board"))), t["posts"])
                                    for t in act.get("topics") or []]),
        bars("Most merited posts", [('<a href="/threads/%s?post=%s#%s">%s</a>%s%s' % (
            p.get("topic_id") or 0, p["post_id"], p["post_id"], e(clean_title(p.get("title")) or "Post %s" % p["post_id"]),
            " by %s" % user_link(p["author"]) if p.get("author") else "", board(p.get("board"))), p["merit"]) for p in m.get("posts") or []]) if m else "",
        bars("Most active boards", [('<a href="/boards/%s">%s</a>' % (b["id"], e(b["name"])), b["posts"]) for b in act.get("boards") or []]),
    ]
    facts = "%s posts" % num(act.get("posts")) + (" and %s merit in %s transactions" % (num(m["total"]), num(m["transactions"])) if m else "")
    return ('<h2 class="titlebg" id="activity">Activity, last %s</h2>%s<p class="pad">%s in the last %s.</p>'
            '<div class="two charts">%s%s</div><div class="two stats-grid">%s</div>'
            % (e(label), picker, facts, e(label), chart("Posts per %s" % unit, posts), chart("Merit per %s" % unit, merits, "merit") if m else "",
               "".join(b for b in lists if b)))


def page_forum_stats(q=None):
    st = api("/api/stats", ttl=600)
    activity = activity_block(q or {})
    m = st.get("merit") or {}
    facts = [("Posts in the last 30 days", num(st.get("posts_30d")))]
    if m:
        facts += [("Merit sent, all time", "%s in %s transactions" % (num(m["total"]), num(m["transactions"]))),
                  ("Merit sent, last 30 days", "%s in %s transactions" % (num(m["total_30d"]), num(m["transactions_30d"]))),
                  ("Members who received merit", num(m["recipients"])), ("Members who sent merit", num(m["senders"]))]
    dl = "".join('<tr><th scope="row">%s:</th><td>%s</td></tr>' % f for f in facts)
    def users(key, field):
        return [(user_link(x["username"]), x[field]) for x in (m.get(key) if field == "merit" else st.get(key)) or []]
    blocks = [
        bars("Most merit received, all time", users("receivers_all", "merit")) if m else "",
        bars("Most merit sent, all time", users("senders_all", "merit")) if m else "",
        bars("Most merited posts, all time", merit_post_rows(m.get("posts_all") or [])) if m else "",
        bars("Merit sent per year", [(y["year"], y["merit"]) for y in m.get("years") or []]) if m else "",
        bars("Posts per month, last 24 months", [(x["month"], x["posts"]) for x in st.get("months") or []]),
    ]
    body = ('%s<div class="tb"><h1 class="catbg">Forum statistics</h1><div class="summary"><table class="kv"><tbody>%s</tbody></table></div>'
            '%s<h2 class="titlebg">All time</h2><div class="two stats-grid">%s</div>'
            '<p class="pad small muted">Posting figures are counted from the posts in this archive. Merit figures come from the public merit lists of Bitcointalk and the merit data published by LoyceV. Times are UTC. '
            'Open a member\'s profile and choose Statistics for their own figures.</p></div>'
            % (crumbs_html([("Statistics", "/stats")]), dl, activity, "".join(b for b in blocks if b)))
    return layout("Forum statistics", body, active="stats", canonical="/stats")


# --------------------------------------------------------------------------- moderation log
def page_modlog(q):
    def val(k):
        return (q.get(k, [""])[0] or "").strip()[:80]
    keep = {k: val(k) for k in ("from", "to", "action", "board_id", "member") if val(k)}
    page = int(val("page")) if val("page").isdigit() else 1
    d = optional("/api/modlog?" + urllib.parse.urlencode(dict(keep, page=page)), 120) or {}
    if not d.get("available"):
        return layout("Moderation log", '%s<div class="tb"><h1 class="catbg">Moderation log</h1><p class="pad">The moderation log is not available right now.</p></div>'
                      % crumbs_html([("Moderation log", "/modlog")]), active="modlog", canonical="/modlog")
    def opts(items, cur, first):
        return '<option value="">%s</option>' % first + "".join(
            '<option value="%s"%s>%s (%s)</option>' % (e(str(v)), " selected" if str(v) == cur else "", e(label), num(n)) for v, label, n in items)
    def date_field(name, label):
        return ('<div class="fld"><label for="ml-%s">%s</label><span class="dt"><input id="ml-%s" name="%s" type="date" min="2026-09-16" max="%s" value="%s">'
                '<button type="button" class="btn dclr" data-clear="ml-%s" hidden>Clear</button></span></div>'
                % (name, label, name, name, time.strftime("%Y-%m-%d", time.gmtime(time.time() + 86400)), e(val(name)), name))
    form = ('<form class="sform mlf" action="/modlog" method="get"><div class="grid2">%s%s'
            '<div class="fld"><label for="ml-action">Action</label><select id="ml-action" name="action">%s</select></div>'
            '<div class="fld"><label for="ml-board">Board</label><select id="ml-board" name="board_id">%s</select></div>'
            '<div class="fld"><label for="ml-member">Member (name or number)</label><input id="ml-member" name="member" type="text" value="%s" autocomplete="off"></div></div>'
            '<p class="row"><button class="btn primary" type="submit">Show</button> <a class="btn" href="/modlog">All entries</a></p></form>'
            % (date_field("from", "First seen from"), date_field("to", "First seen to"),
               opts([(a["action"], a["action"], a["count"]) for a in d.get("actions") or []], val("action"), "Any action"),
               opts([(b["id"], b["name"], b["count"]) for b in d.get("boards") or []], val("board_id"), "Any board"), e(val("member"))))
    rows = []
    for ev in d.get("events") or []:
        t = when(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ev["first_seen"])))
        if ev.get("approx"):   # already in the log when we first read it: only the day we first saw it is known
            t = 'first seen %s <span class="muted">(actual date unknown)</span>' % time.strftime("%d %b %Y", time.gmtime(ev["first_seen"])).lstrip("0")
        title = e(clean_title(ev.get("title"))) if ev.get("title") else ""
        tid, pid, action = ev.get("topic_id"), ev.get("post_id"), ev["action"]
        m = ev.get("member")
        who = (user_link(m["username"]) if not m["username"].startswith("uid ") else '<a href="/profile/%s">member %s</a>' % (m["uid"], m["uid"])) if m else ""
        ours = ' <span class="muted">(from our archive)</span>' if m and m.get("source") != "log" else ""
        if action == "Remove topic":
            subject = (title or "Topic %s" % tid) + ' <span class="muted">(topic %s, removed)</span>' % tid if tid else title
            member = "started by %s%s" % (who, ours) if who else ""
        elif action == "Delete reply":
            subject = ('<a href="/threads/%s">%s</a>' % (tid, title or "Topic %s" % tid) if tid else (title or "")) + (' <span class="muted">(post %s, deleted)</span>' % pid if pid else "")
            member = "posted by %s%s" % (who, ours) if who else ""
        else:   # actions on a member: nuke, signature ban, automatic ban
            subject = "Member %s" % who if who else '<span class="muted">-</span>'
            member = ""
        b = ev.get("board")
        rows.append('<tr><td class="mt">%s</td><td class="ma">%s</td><td class="ms">%s</td><td class="mb">%s</td><td class="mm">%s</td></tr>'
                    % (t, e(action), subject, '<a href="/boards/%s">%s</a>' % (b["id"], e(b["name"])) if b else "", member))
    pages = max(1, -(-int(d.get("total") or 0) // int(d.get("limit") or 50)))
    base = "/modlog" + ("?" + e(urllib.parse.urlencode(keep)) if keep else "")
    nav = pager(base, page, pages, "Moderation log pages")
    table = ('<table class="grid modlog"><thead><tr class="titlebg"><th scope="col">First seen</th><th scope="col">Action</th><th scope="col">Subject</th>'
             '<th scope="col">Board</th><th scope="col">Author</th></tr></thead><tbody>%s</tbody></table>' % "".join(rows)
             if rows else '<p class="pad">No entries match.</p>')
    last = d.get("last_collect")
    note = ('<p class="pad small muted">This is the public deletion log of bitcointalk.org as this archive has seen it, checked every 15 minutes. '
            'Times are when an entry was first seen here, not the exact time of the action, so they can be up to 15 minutes late. '
            'The record starts on 16 September 2026; where an entry was already in the log when it was first read here, only that first-seen day is shown. The log names the author of the deleted post or removed topic, or the member a ban applies to; it does not say which moderator acted. “From our archive” means the name was looked up in this archive because our early records kept only the post or topic number. '
            'Deleted posts and removed topics are not kept in this archive, so only the topic of a deleted post is linked. '
            '%s entries%s.</p>' % (num(d.get("count")), ", last checked %s" % when(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(last))) if last else ""))
    body = ('%s<div class="tb"><h1 class="catbg">Moderation log</h1>%s<div class="bar">%s<span class="muted">%s entr%s</span></div>%s<div class="bar">%s</div>%s</div>'
            % (crumbs_html([("Moderation log", "/modlog")]), form, nav, num(d.get("total")), "y" if d.get("total") == 1 else "ies", table, nav, note))
    return layout("Moderation log", body, active="modlog", canonical="/modlog", noindex=bool(keep) or page > 1)


# --------------------------------------------------------------------------- search
def search_form(term, adv, q):
    def val(k):
        return e((q.get(k, [""])[0] or "")[:200])
    simple = ('<form class="sform" action="/search/posts" method="get" role="search"><p><label for="bq"><b>Search for</b></label><br>'
              '<span class="row"><input id="bq" name="search" type="search" value="%s" required><button class="btn primary" type="submit">Search</button></span></p>'
              '<p class="small muted">Matches words in posts and topic titles. "quotes" for exact phrases, -word to exclude.</p></form>' % e(term))
    boards = forum()["boards"]
    opts = "".join('<option value="%s"%s>%s%s</option>' % (b["id"], " selected" if val("board_id") == str(b["id"]) else "",
                                                           "- " * max(0, len(b["segs"]) - 2), e(b["name"])) for b in boards.values())
    def date_field(name, label):
        # The label is next to the field, not around it: some phone browsers close the date
        # picker again when the field sits inside its own label. The Clear button needs the script.
        return ('<div class="fld"><label for="f-%s">%s</label><span class="dt">'
                '<input id="f-%s" name="%s" type="date" min="2009-11-22" max="%s" value="%s">'
                '<button type="button" class="btn dclr" data-clear="f-%s" hidden>Clear</button></span></div>'
                % (name, label, name, name, time.strftime("%%Y-%%m-%%d", time.gmtime(time.time() + 86400)), val(name), name))
    advanced = ('<details class="adv"%s><summary><b>Advanced search</b> (filters, merit, order)</summary>'
                '<form class="sform grid2" action="/search/posts" method="get"><input type="hidden" name="adv" value="1">'
                '<label>All of these words<input name="search" type="search" value="%s"></label>'
                '<label>This exact phrase<input name="exact_phrase" value="%s"></label>'
                '<label>Any of these words<input name="any_words" value="%s"></label>'
                '<label>None of these words<input name="without_words" value="%s"></label>'
                '<label>By member<input name="author" value="%s" autocomplete="off"></label>'
                '<label>In board<select name="board_id"><option value="">All boards</option>%s</select></label>'
                '%s%s'
                '<label>Merit of at least<input name="min_merit" type="number" min="0" max="10000" inputmode="numeric" value="%s"></label>'
                '<label>Order<select name="sort">%s</select></label>'
                '<label class="chk"><input type="checkbox" name="starters" value="1"%s> Only the first post of each topic</label>'
                '<p><button class="btn primary" type="submit">Search</button></p></form></details>'
                % (" open" if adv else "", e(term), val("exact_phrase"), val("any_words"), val("without_words"), val("author"),
                   opts, date_field("date_from", "From date"), date_field("date_to", "To date"), val("min_merit"),
                   "".join('<option value="%s"%s>%s</option>' % (k, " selected" if val("sort") == k else "", v)
                           for k, v in (("", "Best match"), ("merit", "Most merit first"), ("newest", "Newest first"), ("oldest", "Oldest first"))),
                   " checked" if val("starters") else ""))
    return simple + advanced


def adv_words(term, filters):
    words = term
    if filters.get("exact_phrase"):
        words += ' "%s"' % filters["exact_phrase"].replace('"', " ")
    if (filters.get("any_words") or "").split():
        words += " (%s)" % " | ".join(w.strip('"|()') for w in filters["any_words"].split())
    if (filters.get("without_words") or "").split():
        words += " " + " ".join("-" + w.lstrip("-") for w in filters["without_words"].split())
    return words.strip()


FEED_ROUTE = re.compile(r"^/feed/(search|threads/(\d+)|boards/(\d+)|profile/([^/]+))/?$")


def feed(path, q):
    """Atom feed of the newest posts for a search, a topic, a board or a member (25 entries, cached 10 minutes)."""
    m = FEED_ROUTE.match(path)
    if not m:
        raise NotFound()
    def val(k):
        return (q.get(k, [""])[0] or "").strip()[:200]
    params = {"limit": 25, "page": 1, "sort": "newest"}
    if m.group(2):
        params["topic_id"] = m.group(2); link = "/threads/%s" % m.group(2); title = "Topic %s" % m.group(2)
    elif m.group(3):
        fb = forum()["boards"]
        b = fb.get(int(m.group(3))) or fb.get(m.group(3))
        if not b:
            raise NotFound()
        params["board_id"] = m.group(3); link = "/boards/%s" % m.group(3); title = "Board: %s" % b["name"]
    elif m.group(4):
        name = urllib.parse.unquote(m.group(4))
        api("/api/users/%s" % urllib.parse.quote(name, safe=""), ttl=600)      # 404 for an unknown member
        params["author"] = name; link = "/profile/%s" % urllib.parse.quote(name, safe=""); title = "Posts by %s" % name
    else:
        words = adv_words(val("search"), {k: val(k) for k in ("exact_phrase", "any_words", "without_words")})
        for k in ("board_id", "author", "date_from", "date_to", "min_merit", "starters"):
            if val(k):
                params[k] = val(k)
        if not words and len(params) == 3:
            raise NotFound()
        params["search"] = words
        link = "/search/posts?" + urllib.parse.urlencode(dict({k: v for k, v in params.items() if k not in ("limit", "page", "sort")}, adv="1"))
        title = "Search: %s" % (words or "filtered posts")
    posts = api("/api/search/posts", params, ttl=600).get("posts") or []
    if m.group(2) and posts:
        title = clean_title(posts[0].get("thread_title")) or title
    x = html.escape
    def iso(p):
        d = parse_dt(p.get("created_at"))
        return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if d else "1970-01-01T00:00:00Z"
    entries = []
    for p in posts:
        url = "%s/threads/%s?post=%s#%s" % (SITE_URL, p.get("thread_id"), p.get("id"), p.get("id"))
        who = (p.get("user") or {}).get("username") or "unknown"
        entries.append("<entry><id>%s</id><title>%s</title><link href=\"%s\"/><updated>%s</updated><author><name>%s</name></author>"
                       "<summary>%s</summary></entry>" % (x(url), x(clean_title(p.get("thread_title")) or "Post %s" % p.get("id")), x(url), iso(p), x(who),
                                                       x(excerpt(p.get("body_html"), 600))))
    return ('<?xml version="1.0" encoding="utf-8"?>\n<feed xmlns="http://www.w3.org/2005/Atom"><id>%s</id><title>%s - %s</title>'
            '<link href="%s"/><link rel="self" href="%s"/><updated>%s</updated>%s</feed>'
            % (x(SITE_URL + path + ("?" + urllib.parse.urlencode({k: val(k) for k in q if val(k)}) if q else "")), x(title), x(SITE), x(SITE_URL + link),
               x(SITE_URL + path + ("?" + urllib.parse.urlencode({k: val(k) for k in q if val(k)}) if q else "")),
               iso(posts[0]) if posts else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "".join(entries)))


def page_search_posts(q):
    term = (q.get("search", q.get("q", [""]))[0] or "").strip()[:200]
    adv = bool(q.get("adv"))
    page = max(1, int(q.get("page", ["1"])[0] or 1))
    head = '%s<div class="tb"><h1 class="catbg">Search</h1><div class="pad">%s</div></div>' % (
        crumbs_html([("Search", "/search/posts")]), search_form(term, adv, q))
    filters = {k: (q.get(k, [""])[0] or "").strip()[:200] for k in ("exact_phrase", "any_words", "without_words")}
    extra = {k: (q.get(k, [""])[0] or "").strip()[:200] for k in ("author", "board_id", "date_from", "date_to", "min_merit", "sort", "starters")}
    if not term and not (adv and (any(filters.values()) or any(extra.values()))):
        return layout("Search", head, active="search", canonical="/search/posts")
    if adv:
        words = term
        if filters["exact_phrase"]:
            words += ' "%s"' % filters["exact_phrase"].replace('"', " ")
        if filters["any_words"].split():
            words += " (%s)" % " | ".join(w.strip('"|()') for w in filters["any_words"].split())
        if filters["without_words"].split():
            words += " " + " ".join("-" + w.lstrip("-") for w in filters["without_words"].split())
        params = {"search": words.strip(), "limit": PER_PAGE, "page": page}
        for k in ("board_id", "author", "date_from", "date_to", "min_merit", "sort", "starters"):
            if extra[k]:
                params[k] = extra[k]
        note = ""
        if extra["author"]:
            try:
                api("/api/users/%s" % urllib.parse.quote(extra["author"], safe=""), ttl=600)
            except NotFound:
                note = '<p class="warn">No member is called “%s”.</p>' % e(extra["author"])
        if (page - 1) * PER_PAGE >= MAX_WINDOW:
            page = params["page"] = MAX_WINDOW // PER_PAGE
        data = api("/api/search/posts", params, ttl=60)
        posts = data.get("posts") or []
        count = int(data.get("count") or 0)
        pages = max(1, min(math.ceil(count / PER_PAGE), MAX_WINDOW // PER_PAGE))
        keep = dict({k: v for k, v in filters.items() if v}, **{k: v for k, v in extra.items() if v})
        keep.update(adv="1", search=term)
        qs = urllib.parse.urlencode(keep)
        pg = pager("/search/posts?" + e(qs), page, pages, "Search result pages")
        res = post_list(posts) if posts else '<p class="pad muted">No posts match. Try fewer words or fewer filters.</p>'
        body = ('%s%s<div class="bar">%s</div><div class="tb"><h2 class="catbg">%s result%s%s</h2>%s</div><div class="bar">%s</div>'
                % (head, note, pg, num(count), "" if count == 1 else "s", " (%s)" % e(data["note"]) if data.get("note") else "", res, pg))
        return layout("Search: %s" % (term or "filtered"), body, q=term, noindex=True, active="search",
                      feed="/feed/search?" + urllib.parse.urlencode({k: v for k, v in keep.items() if k not in ("adv", "sort") and v}))
    if (page - 1) * PER_PAGE >= MAX_WINDOW:
        page = MAX_WINDOW // PER_PAGE
    data = api("/api/search/posts", {"search": term, "limit": PER_PAGE, "page": page}, ttl=30)
    posts = data.get("posts") or []
    count = int(data.get("count") or 0)
    pages = max(1, min(math.ceil(count / PER_PAGE), MAX_WINDOW // PER_PAGE))
    tids = sorted({str(p.get("thread_id")) for p in posts})

    def title_of(t):
        try:
            c = api("/api/breadcrumbs", {"id": t, "type": "thread"}, ttl=3600)
            return t, (c[-1]["label"] if c else None)
        except (NotFound, Upstream):
            return t, None
    titles = dict(_pool.map(title_of, tids))
    pg = pager("/search/posts?search=%s" % e(urllib.parse.quote_plus(term)), page, pages, "Search result pages")
    res = post_list(posts, titles) if posts else '<p class="pad muted">No posts match. Try fewer or different words.</p>'
    body = ('%s<div class="bar">%s</div><div class="tb"><h2 class="catbg">%s result%s for “%s”</h2>%s</div><div class="bar">%s</div>'
            % (head, pg, num(count), "" if count == 1 else "s", e(term), res, pg))
    return layout("Search: %s" % term, body, q=term, noindex=True, active="search", feed="/feed/search?" + urllib.parse.urlencode({"search": term}))


def sanitize_light(content):
    """TalkSearch hits are used for excerpts only, which strip all markup anyway."""
    return content or ""


def page_search_users(q):
    term = (q.get("search", q.get("username", [""]))[0] or "").strip()[:60]
    page = max(1, int(q.get("page", ["1"])[0] or 1))
    form = ('<form class="sform" action="/search/users" method="get" role="search"><p><label for="uq"><b>Find a member by name</b></label><br>'
            '<span class="row"><input id="uq" name="search" type="search" value="%s" minlength="2" required><button class="btn primary" type="submit">Find</button></span></p></form>' % e(term))
    res = ""
    if len(term) >= 2:
        data = api("/api/search/users", {"search": term, "limit": 50, "page": page}, ttl=60)
        users = data.get("users") or []
        count = int(data.get("count") or 0)
        pg = pager("/search/users?search=%s" % e(urllib.parse.quote_plus(term)), page, int(data.get("page_count") or 1), "Member result pages")
        rows = "".join('<tr><td class="ic">%s</td><td class="info">%s</td></tr>' % (avatar_html(u.get("avatar"), u.get("username"), 28), user_link(u.get("username"), "bn"))
                       for u in users)
        more = " (showing the first 500)" if count >= 500 else ""
        res = ('<div class="bar">%s</div><div class="tb"><h2 class="catbg">%s member%s matching “%s”%s</h2>%s</div>'
               % (pg, num(count), "" if count == 1 else "s", e(term), more,
                  '<table class="grid members"><tbody>%s</tbody></table>' % rows if users else '<p class="pad muted">No member names contain that text.</p>'))
    elif term:
        res = '<p class="warn">Type at least 2 characters.</p>'
    body = '%s<div class="tb"><h1 class="catbg">Members</h1><div class="pad">%s</div></div>%s' % (crumbs_html([("Members", "/search/users")]), form, res)
    return layout("Members", body, noindex=bool(term), active="members", canonical="/search/users")


def smf_redirect(raw_query):
    """Bitcointalk/SMF style links: ?topic=178336.msg67296272, ?topic=5.40, ?board=1.0, ?action=profile;u=3"""
    args = dict(urllib.parse.parse_qsl(raw_query.replace(";", "&"), keep_blank_values=True))
    t = args.get("topic")
    if t:
        m = re.match(r"^(\d+)(?:\.(msg)?(\d+))?", t)
        if m:
            tid = m.group(1)
            if m.group(2):
                return "/threads/%s?post=%s#%s" % (tid, m.group(3), m.group(3))
            if m.group(3) and int(m.group(3)) > 0:
                return "/threads/%s?start=%s" % (tid, m.group(3))
            return "/threads/%s" % tid
    b = args.get("board")
    if b:
        m = re.match(r"^(\d+)(?:\.(\d+))?", b)
        if m:
            off = int(m.group(2) or 0)
            return "/boards/%s%s" % (m.group(1), "?page=%d" % (off // 40 + 1) if off else "")
    if args.get("action") == "profile" and args.get("u", "").isdigit():
        return "/profile/%s" % args["u"]
    if args.get("action") == "search":
        return "/search/posts"
    if args.get("action") == "recent":
        return "/recent"
    return None


ACCOUNT_ONLY = re.compile(r"^/(messages|settings|patrol|mentions|watchlist|login|register|join|reset|confirm|admin|threads/posted)(/|$)")
SORT_COLUMNS = ("subject", "started", "replies", "views", "last_post")
ROUTES = [
    (re.compile(r"^/boards/(\d+)/?$"), lambda m, q: page_board(int(m.group(1)), q)),
    (re.compile(r"^/threads/(\d+)(?:/[^/]*)?/?$"), lambda m, q: page_thread(int(m.group(1)), q)),
    (re.compile(r"^/profile/([^/]+)/trust/?$"), lambda m, q: page_user_trust(urllib.parse.unquote(m.group(1)))),
    (re.compile(r"^/profile/([^/]+)/stats/?$"), lambda m, q: page_user_stats(urllib.parse.unquote(m.group(1)))),
    (re.compile(r"^/stats/?$"), lambda m, q: page_forum_stats(q)),
    (re.compile(r"^/modlog/?$"), lambda m, q: page_modlog(q)),
    (re.compile(r"^/profile/([^/]+)(?:/(posts))?/?$"),
     lambda m, q: page_profile(urllib.parse.unquote(m.group(1)), q, m.group(2) or "")),
    (re.compile(r"^/search/posts/?$"), lambda m, q: page_search_posts(q)),
    (re.compile(r"^/search(?:/users)?/?$"), lambda m, q: page_search_users(q)),
    (re.compile(r"^/recent/?$"), lambda m, q: page_recent(q)),
    (re.compile(r"^/history/(\d+)/?$"), lambda m, q: page_history(int(m.group(1)))),
    (re.compile(r"^/compose/?$"), lambda m, q: page_compose(q)),
]
STATIC_TYPES = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".svg": "image/svg+xml", ".txt": "text/plain; charset=utf-8", ".ico": "image/x-icon", ".png": "image/png",
                ".webmanifest": "application/manifest+json"}
CSP = ("default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
       "form-action 'self'; base-uri 'none'; frame-ancestors 'none'; manifest-src 'self'; connect-src 'self'")
SEEN_ROUTE = re.compile(r"^/seen/(\d{1,10})$")
IMG_ROUTE = re.compile(r"^/img/([0-9a-f]{24})/([A-Za-z0-9_-]{8,4000})$")


def asset_version():
    try:
        return str(int(max(os.path.getmtime(os.path.join(STATIC_DIR, f)) for f in os.listdir(STATIC_DIR))))
    except (OSError, ValueError):
        return "0"


ASSET_V = asset_version()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "archive-web"

    def log_message(self, *a):  # no visitor logs, by design
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", cache="public, max-age=30", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", CSP)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _redirect(self, to, code=302):
        self._send(code, "", extra={"Location": to}, cache="no-store")

    def _static(self, name):
        name = os.path.basename(name)
        ext_ = os.path.splitext(name)[1]
        path = os.path.join(STATIC_DIR, name)
        if ext_ not in STATIC_TYPES or not os.path.isfile(path):
            return self._send(404, message_page("Page not found", "There is nothing at this address."), cache="no-store")
        with open(path, "rb") as f:
            self._send(200, f.read(), STATIC_TYPES[ext_], "public, max-age=31536000, immutable")

    def _image(self, m):
        url = imgproxy.decode(m.group(1), m.group(2))
        got = imgproxy.get(url, avif="image/avif" in (self.headers.get("Accept") or "")) if url else None
        if not got:
            return self._send(404, b"", "text/plain; charset=utf-8", "public, max-age=3600", {"Vary": "Accept"})
        self._send(200, got[1], got[0], "public, max-age=2592000, immutable",
                   {"Content-Security-Policy": "default-src 'none'; sandbox", "Content-Disposition": "inline", "Vary": "Accept"})

    def _proxy_api(self):
        try:
            with urllib.request.urlopen(API + self.path, timeout=30) as r:
                return self._send(r.status, r.read(), r.headers.get("Content-Type", "application/json"),
                                  r.headers.get("Cache-Control", "public, max-age=60"))
        except urllib.error.HTTPError as ex:
            return self._send(ex.code, ex.read(), "application/json", "no-store")
        except Exception:
            return self._send(502, '{"error":"upstream"}', "application/json", "no-store")

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        path = p.path
        try:
            q = urllib.parse.parse_qs(p.query, keep_blank_values=True)
            if path.startswith("/static/"):
                return self._static(path)
            im = IMG_ROUTE.match(path)
            if im:
                return self._image(im)
            if path in ("/favicon.ico", "/robots.txt", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
                return self._static("apple-touch-icon.png" if path.startswith("/apple") else path[1:])
            if path.startswith("/feed/"):
                return self._send(200, feed(path, q), "application/atom+xml; charset=utf-8", "public, max-age=600")
            if path.startswith("/api/"):
                return self._proxy_api()
            sm = SEEN_ROUTE.match(path)
            if sm:   # a browser showing a topic page: ask for that page to be re-checked for edits first
                try:
                    api("/internal/note-view", {"topic": int(sm.group(1)), "page": max(1, int((q.get("p", ["1"])[0] or "1")[:6]))})
                except Exception:
                    pass
                return self._send(204, b"", "text/plain; charset=utf-8", "no-store")
            if path in ("/", "/index.php") and p.query:
                to = smf_redirect(p.query)
                if to:
                    return self._redirect(to, 301)
            if path in ("/", "/index.php", "/boards", "/boards/"):
                return self._send(200, page_home(q))
            if ACCOUNT_ONLY.match(path):
                return self._send(403, message_page(
                    "Not available in the archive",
                    "This is a read-only archive with no accounts, messages or settings. Use bitcointalk.org for anything that needs you to sign in.",
                    "<p>%s</p>" % act("https://bitcointalk.org", "Go to bitcointalk.org")), cache="no-store")
            for rx, fn in ROUTES:
                m = rx.match(path)
                if m:
                    out = fn(m, q)
                    if isinstance(out, tuple):
                        return self._redirect(out[1])
                    return self._send(200, out, cache="no-store" if path.startswith("/compose") else "public, max-age=30")
            raise NotFound()
        except NotFound:
            self._send(404, message_page("Page not found", "That board, topic, member or page is not in the archive."
                                         " It may never have been public, or the address may be mistyped."), cache="no-store")
        except (ValueError, OverflowError):
            self._send(400, message_page("Bad request", "That address has a value this site cannot read."), cache="no-store")
        except Upstream:
            self._send(503, message_page("Temporarily unavailable", "The archive data service did not answer. Please try again in a moment."),
                       cache="no-store", extra={"Retry-After": "15"})

    do_HEAD = do_GET

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path == "/upload-image":
            return self._upload()
        self._send(405, message_page("Read-only", "This archive does not accept posts or other changes."), cache="no-store")

    def _upload(self):
        """Composer picture -> TalkImg. The picture is passed on and forgotten; nothing is logged."""
        def reply(code, obj):
            self._send(code, json.dumps(obj), "application/json", "no-store")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        origin = self.headers.get("Origin") or ""
        host = self.headers.get("Host") or ""
        if not talkimg.enabled():
            return reply(404, {"error": "Image upload is not available."})
        if not origin or urllib.parse.urlsplit(origin).netloc != host or self.headers.get("X-Composer") != "1":
            return reply(403, {"error": "Uploads are only accepted from the composer on this site."})
        if n <= 0 or n > talkimg.MAX_BYTES:
            self.close_connection = True
            return reply(413, {"error": "The picture is too large (limit 8 MB)."})
        data = self.rfile.read(n)
        code, obj = talkimg.upload(data)
        reply(code, obj)


def main():
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    print("archive-web listening on http://%s:%d (API %s)" % (BIND, PORT, API), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
