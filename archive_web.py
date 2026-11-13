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

API = os.environ.get("ARCHIVE_API", "http://127.0.0.1:18183").rstrip("/")
BIND = os.environ.get("BIND", "127.0.0.1")
PORT = int(os.environ.get("PORT", "18190"))
SITE = os.environ.get("SITE_NAME", "Bitcointalk Archive")
SITE_TAG = os.environ.get("SITE_TAGLINE", "by TalkSearch")
SOURCE_URL = os.environ.get("SOURCE_URL", "")
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
    b = b.replace("<img ", '<img loading="lazy" decoding="async" referrerpolicy="no-referrer" ')
    return b


def user_link(name, cls=""):
    if not name or name == "(unknown)":
        return '<span class="muted">%s</span>' % e(name or "unknown")
    return '<a class="user %s" href="/profile/%s">%s</a>' % (cls, urllib.parse.quote(name, safe=""), e(name))


def trust_html(stats):
    if not stats or not (stats.get("pos") or stats.get("neg")):
        return ""
    neg, pos = int(stats.get("neg") or 0), int(stats.get("pos") or 0)
    cls = "warn" if neg > pos else ""
    return ('<span class="trust %s" title="DefaultTrust network: trusted by %d, distrusted by %d">'
            'Trust: <b>+%d</b> / <b>−%d</b></span>' % (cls, pos, neg, pos, neg))


def avatar_html(url, name, size):
    if url:
        return ('<img class="avatar" src="%s" alt="" width="%d" height="%d" loading="lazy">'
                % (e(url), size, size))
    initial = e((name or "?")[:1].upper())
    return '<span class="avatar ph" aria-hidden="true" style="width:%dpx;height:%dpx">%s</span>' % (size, size, initial)


def pager(base, page, pages, label, anchor=""):
    """Numbered pagination plus a jump-to-page form. `base` already holds other query args."""
    if pages <= 1:
        return ""
    sep = "&amp;" if "?" in base else "?"

    def link(p, text=None, rel="", cls=""):
        href = base if p == 1 else "%s%spage=%d" % (base, sep, p)
        return '<a href="%s%s"%s%s>%s</a>' % (
            href, anchor, ' rel="%s"' % rel if rel else "", ' class="%s"' % cls if cls else "", text or num(p))
    shown = sorted({1, 2, pages - 1, pages, page - 2, page - 1, page, page + 1, page + 2} & set(range(1, pages + 1)))
    out = ['<nav class="pager" aria-label="%s">' % e(label)]
    if page > 1:
        out.append(link(page - 1, "‹ Prev", "prev", "step"))
    prev = 0
    for p in shown:
        if p - prev > 1:
            out.append('<span class="gap" aria-hidden="true">…</span>')
        if p == page:
            out.append('<span class="cur" aria-current="page"><span class="sr">Page </span>%s</span>' % num(p))
        else:
            out.append(link(p))
        prev = p
    if page < pages:
        out.append(link(page + 1, "Next ›", "next", "step"))
    if pages > 7:
        action, _, qs = base.partition("?")
        hidden = "".join('<input type="hidden" name="%s" value="%s">' % (e(k), e(v))
                         for k, v in urllib.parse.parse_qsl(html.unescape(qs)))
        out.append('<form class="jump" action="%s" method="get">%s<label>Go to page '
                   '<input name="page" type="number" min="1" max="%d" inputmode="numeric" required></label>'
                   '<button type="submit">Go</button></form>' % (action, hidden, pages))
    out.append("</nav>")
    return "".join(out)


ICON_EXT = ('<svg class="ext" viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">'
            '<path d="M6 3H3v10h10v-3M9 3h4v4M13 3 7 9" fill="none" stroke="currentColor" stroke-width="1.6" '
            'stroke-linecap="round" stroke-linejoin="round"/></svg>')
ICON_SEARCH = ('<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><circle cx="7" cy="7" r="4.5" '
               'fill="none" stroke="currentColor" stroke-width="1.8"/><path d="m10.5 10.5 3.5 3.5" stroke="currentColor" '
               'stroke-width="1.8" stroke-linecap="round"/></svg>')


def ext(href, text, cls=""):
    return '<a class="%s" href="%s" rel="noopener nofollow">%s %s</a>' % (cls, e(href), text, ICON_EXT)


def crumbs_html(items):
    parts = ['<li><a href="/">Boards</a></li>']
    for i, (label, href) in enumerate(items):
        last = i == len(items) - 1
        if href and not last:
            parts.append('<li><a href="%s">%s</a></li>' % (href, e(label)))
        elif href:
            parts.append('<li><a href="%s" aria-current="page">%s</a></li>' % (href, e(label)))
        else:
            parts.append("<li><span>%s</span></li>" % e(label))
    return '<nav class="crumbs" aria-label="Breadcrumb"><ol>%s</ol></nav>' % "".join(parts)


def layout(title, body, q="", canonical="", noindex=False, active=""):
    full = "%s – %s" % (title, SITE) if title else "%s %s" % (SITE, SITE_TAG)
    def nav(href, text, key):
        return '<a href="%s"%s>%s</a>' % (href, ' aria-current="page"' if active == key else "", text)
    src = ' · <a href="%s" rel="noopener">Source code (MIT)</a>' % e(SOURCE_URL) if SOURCE_URL else ""
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%s</title>
%s%s<meta name="color-scheme" content="light dark">
<link rel="stylesheet" href="/static/style.css?v=%s">
<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
<script src="/static/app.js?v=%s" defer></script>
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<header class="site">
<div class="wrap bar">
<a class="brand" href="/"><b>%s</b> <span>%s</span></a>
<nav class="mainnav" aria-label="Main">%s%s%s</nav>
<form class="q" action="/search/posts" method="get" role="search">
<label class="sr" for="q">Search posts</label>
<input id="q" name="search" type="search" placeholder="Search posts" value="%s" autocomplete="off">
<button type="submit" aria-label="Search">%s</button>
</form>
</div>
</header>
<main id="main" class="wrap">
%s
</main>
<footer class="site"><div class="wrap">
<p>%s %s is an unofficial, read-only archive. Posts belong to their authors. Nothing you do here is sent to bitcointalk.org, and there are no accounts, trackers or ads.</p>
<p><a href="/">Boards</a> · <a href="/search/posts">Search</a> · <a href="/search/users">Members</a> · <a href="https://talksearch.io" rel="noopener">TalkSearch</a> · <a href="https://bitcointalk.org" rel="noopener">bitcointalk.org</a>%s</p>
</div></footer>
</body>
</html>
""" % (e(full),
       '<link rel="canonical" href="%s">\n' % e(canonical) if canonical else "",
       '<meta name="robots" content="noindex">\n' if noindex else "",
       ASSET_V, ASSET_V, e(SITE), e(SITE_TAG),
       nav("/", "Boards", "boards"), nav("/search/posts", "Search", "search"), nav("/search/users", "Members", "members"),
       e(q), ICON_SEARCH, body, e(SITE), e(SITE_TAG), src)


def message_page(title, text, extra=""):
    return layout(title, '<section class="panel msg"><h1>%s</h1><p>%s</p>%s<p><a class="btn" href="/">Back to the boards</a></p></section>'
                  % (e(title), text, extra), noindex=True)


# --------------------------------------------------------------------------- pages
def last_post_cell(b):
    if not b.get("last_thread_id"):
        return '<span class="muted">No posts yet</span>'
    return ('<a class="lp-title" href="/threads/%s?last=1">%s</a><span class="lp-meta">by %s · %s</span>'
            % (b["last_thread_id"], e(clean_title(b.get("last_thread_title"))),
               user_link(b.get("last_post_username")), when(b.get("last_post_created_at"), short=True)))


def page_home(q):
    data = board_index()
    out = ['<h1 class="sr">Boards</h1>']
    for cat in data.get("boards", []):
        rows = []
        for b in cat.get("boards", []):
            kids = ""
            if b.get("children"):
                kids = '<p class="kids"><span>Child boards:</span> %s</p>' % ", ".join(
                    '<a href="/boards/%s">%s</a>' % (c["id"], e(c["name"])) for c in b["children"])
            rows.append(
                '<tr><th scope="row" class="b-name"><a href="/boards/%s">%s</a>%s</th>'
                '<td class="n" data-l="Topics">%s</td><td class="n" data-l="Posts">%s</td>'
                '<td class="lp">%s</td></tr>'
                % (b["id"], e(b["name"]), kids, num(b.get("thread_count")), num(b.get("post_count")), last_post_cell(b)))
        out.append(
            '<section class="panel cat"><h2>%s</h2><table class="list boards"><thead><tr>'
            '<th scope="col">Board</th><th scope="col" class="n">Topics</th><th scope="col" class="n">Posts</th>'
            '<th scope="col">Last post</th></tr></thead><tbody>%s</tbody></table></section>'
            % (e(cat.get("name") or ""), "".join(rows)))
    recent = data.get("threads") or []
    if recent:
        items = "".join(
            '<li><a href="/threads/%s?last=1">%s</a><span class="lp-meta">%s · by %s · %s</span></li>'
            % (t["id"], e(clean_title(t.get("title"))),
               '<a href="/boards/%s">%s</a>' % (t["board"]["id"], e(t["board"]["name"])) if t.get("board") else "",
               user_link((t.get("user") or {}).get("username")), when((t.get("post") or {}).get("created_at")))
            for t in recent)
        out.append('<section class="panel cat"><h2>Latest posts</h2><ul class="recent">%s</ul></section>' % items)
    return layout("", "".join(out), active="boards", canonical="/")


def page_board(bid, q):
    page = max(1, int(q.get("page", ["1"])[0] or 1))
    if (page - 1) * PER_PAGE >= MAX_WINDOW:
        page = MAX_WINDOW // PER_PAGE
    data = api("/api/threads", {"board_id": bid, "limit": PER_PAGE, "page": page}, ttl=20)
    board = data["board"]
    if not board.get("description") and not data.get("normal"):
        raise NotFound()
    total = int(board.get("thread_count") or 0)
    pages = max(1, min(math.ceil(total / PER_PAGE), MAX_WINDOW // PER_PAGE))
    base = "/boards/%s" % bid
    rows = []
    for t in (data.get("sticky") or []) + (data.get("normal") or []):
        pc = int(t.get("post_count") or 1)
        tp = math.ceil(pc / PER_PAGE)
        mini = ""
        if tp > 1:
            nums = [1, 2, 3] if tp <= 4 else [1, 2]
            links = ['<a href="/threads/%s%s">%d</a>' % (t["id"], "" if p == 1 else "?page=%d" % p, p) for p in nums if p <= tp]
            if tp > 4:
                links.append("…")
            if tp > 3:
                links.append('<a href="/threads/%s?page=%d">%s</a>' % (t["id"], tp, num(tp)))
            elif tp == 4:
                links.append('<a href="/threads/%s?page=4">4</a>' % t["id"])
            mini = '<span class="mini" aria-label="Pages">%s</span>' % " ".join(links)
        rows.append(
            '<tr><th scope="row" class="t-name"><a href="/threads/%s">%s</a>%s</th>'
            '<td class="n" data-l="Replies">%s</td><td class="n" data-l="Views">%s</td>'
            '<td class="lp"><a href="/threads/%s?last=1">%s</a><span class="lp-meta">by %s</span></td></tr>'
            % (t["id"], e(clean_title(t.get("title"))), mini, num(max(0, pc - 1)), num(t.get("view_count")),
               t["id"], when(t.get("last_post_created_at")), user_link(t.get("last_post_username"))))
    kids = ""
    if board.get("children"):
        kids = '<section class="panel cat"><h2>Child boards</h2><table class="list boards"><tbody>%s</tbody></table></section>' % "".join(
            '<tr><th scope="row" class="b-name"><a href="/boards/%s">%s</a></th><td class="n" data-l="Topics">%s</td>'
            '<td class="n" data-l="Posts">%s</td><td class="lp">%s</td></tr>'
            % (c["id"], e(c["name"]), num(c.get("thread_count")), num(c.get("post_count")), last_post_cell(c))
            for c in board["children"])
    pg = pager(base, page, pages, "Topic list pages")
    capped = ""
    if math.ceil(total / PER_PAGE) > pages and page == pages:
        capped = '<p class="muted small">Older topics in this board are reachable through search.</p>'
    body = "%s<div class=\"head\"><h1>%s</h1><p class=\"sub\">%s topics · %s posts</p>%s</div>%s%s" \
           "<section class=\"panel\"><table class=\"list topics\"><thead><tr><th scope=\"col\">Subject</th>" \
           "<th scope=\"col\" class=\"n\">Replies</th><th scope=\"col\" class=\"n\">Views</th><th scope=\"col\">Last post</th>" \
           "</tr></thead><tbody>%s</tbody></table></section>%s%s" % (
               crumbs_html(board_crumbs(board)), e(board.get("name") or "Board"), num(total), num(board.get("post_count")),
               ext("%s?action=post;board=%s.0" % (BCT, bid), "New topic", "btn ghost"),
               kids, pg, "".join(rows) or '<tr><td colspan="4" class="muted">No topics.</td></tr>', pg, capped)
    return layout(board.get("name") or "Board", body, active="boards",
                  canonical=base if page == 1 else "%s?page=%d" % (base, page))


def poll_html(poll):
    if not poll or not poll.get("answers"):
        return ""
    total = sum(int(a.get("votes") or 0) for a in poll["answers"])
    rows = []
    for a in poll["answers"]:
        v = int(a.get("votes") or 0)
        if poll.get("display_mode") == "always" and total:
            pct = 100.0 * v / total
            rows.append('<li><span class="pl">%s</span><span class="pb"><span style="width:%.1f%%"></span></span>'
                        '<span class="pv">%s (%.1f%%)</span></li>' % (e(a.get("answer") or ""), pct, num(v), pct))
        else:
            rows.append('<li><span class="pl">%s</span></li>' % e(a.get("answer") or ""))
    foot = "Total votes: %s." % num(total) if total else "Results are hidden on bitcointalk.org until you vote there."
    return ('<section class="panel poll" aria-label="Poll"><h2>Poll: %s</h2><ul>%s</ul><p class="muted small">%s '
            'Voting happens on bitcointalk.org.</p></section>' % (e(poll.get("question") or ""), "".join(rows), foot))


def page_thread(tid, q):
    params = {"thread_id": tid, "limit": PER_PAGE}
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
    if start.isdigit():
        page = int(start) // PER_PAGE + 1
    else:
        page = max(1, int(q.get("page", ["1"])[0] or 1))
    params["page"] = page
    data = api("/api/posts", params, ttl=15)
    posts = data.get("posts") or []
    thread = data["thread"]
    count = int(thread.get("post_count") or 0)
    pages = max(1, math.ceil(count / PER_PAGE))
    if not posts:
        if count and page > pages:
            return ("redirect", "/threads/%d?page=%d" % (tid, pages))
        raise NotFound()
    title = clean_title(thread.get("title"))
    base = "/threads/%d" % tid
    bid = thread.get("board_id")
    crumbs = []
    if bid is not None:
        try:
            crumbs = board_crumbs(api("/api/boards/%s" % bid, ttl=600))
        except (NotFound, Upstream):
            crumbs = [(thread.get("board_name") or "Board", "/boards/%s" % bid)]
    crumbs.append((title, base))
    on_page = {str(p["id"]) for p in posts}
    arts = []
    for i, p in enumerate(posts):
        u = p.get("user") or {}
        name = u.get("username") or "(unknown)"
        rank = u.get("rank") or ""
        stats = []
        if isinstance(u.get("activity"), int) and u["activity"] >= 0:
            stats.append("<li>Activity: <b>%s</b></li>" % num(u["activity"]))
        if u.get("merit"):
            stats.append("<li>Merit: <b>%s</b></li>" % num(u["merit"]))
        tr = trust_html(u.get("stats"))
        if tr:
            stats.append("<li>%s</li>" % tr)
        sig = ""
        if u.get("signature"):
            sig = '<div class="sig" aria-label="Signature">%s</div>' % balanced(u["signature"], tight=True)
        last = ' <span id="last"></span>' if i == len(posts) - 1 else ""
        arts.append(
            '<article class="entry" id="%s" aria-label="Post %s by %s">%s'
            '<aside class="author">%s<div class="who">%s%s<ul class="stats">%s</ul></div></aside>'
            '<div class="pbody"><header><span class="pdate">%s</span><span class="ptools">'
            '<a href="%s?action=post;quote=%s;topic=%d" rel="noopener nofollow" title="Quote this post">Quote</a>'
            '<a href="%s?topic=%d.msg%s#msg%s" rel="noopener nofollow" title="Open the original post">Original</a>'
            '<a class="pnum" href="%s#%s" title="Link to this post">#%s</a></span></header>'
            '<div class="content">%s</div>%s</div></article>'
            % (p["id"], num(p.get("position")), e(name), last,
               avatar_html(p.get("avatar") or u.get("avatar"), name, 72),
               user_link(name, "name"), '<span class="rank">%s</span>' % e(rank) if rank else "", "".join(stats),
               when(p.get("created_at")), BCT, p["id"], tid, BCT, tid, p["id"], p["id"],
               base if page == 1 else "%s?page=%d" % (base, page), p["id"], num(p.get("position")),
               post_body(p.get("body_html"), tid, on_page), sig))
    pg = pager(base, page, pages, "Topic pages")
    reply = ext("%s?action=post;topic=%d.0" % (BCT, tid), "Reply", "btn")
    body = ('%s<div class="head"><h1>%s</h1><p class="sub">%s posts · page %s of %s</p>%s</div>%s'
            '<div class="tools">%s</div><div class="posts">%s</div><div class="tools">%s%s</div>'
            % (crumbs_html(crumbs), e(title), num(count), num(page), num(pages), reply,
               poll_html(thread.get("poll")) if page == 1 else "", pg, "".join(arts), pg, reply))
    return layout(title, body, active="boards", canonical=base if page == 1 else "%s?page=%d" % (base, page))


def post_cards(posts, titles=None, show_author=True):
    out = []
    for p in posts:
        tid = p.get("thread_id")
        title = p.get("thread_title") or (titles or {}).get(str(tid)) or "Topic %s" % tid
        board = ""
        if p.get("board_name"):
            board = ' · <a href="/boards/%s">%s</a>' % (p.get("board_id"), e(p["board_name"]))
        author = " · by %s" % user_link((p.get("user") or {}).get("username")) if show_author else ""
        out.append('<li><a class="r-title" href="/threads/%s?post=%s#%s">%s</a><p class="lp-meta">%s%s%s</p><p class="r-text">%s</p></li>'
                   % (tid, p["id"], p["id"], e(clean_title(title)), when(p.get("created_at")), author, board,
                      excerpt(p.get("body_html"))))
    return '<ul class="results">%s</ul>' % "".join(out)


def page_profile(name, q, sub=""):
    u = api("/api/users/%s" % urllib.parse.quote(name, safe=""), ttl=60)
    uname = u.get("username") or name
    page = max(1, int(q.get("page", ["1"])[0] or 1))
    if (page - 1) * PER_PAGE >= MAX_WINDOW:
        page = MAX_WINDOW // PER_PAGE
    pd = api("/api/posts/user/%s" % urllib.parse.quote(name, safe=""), {"limit": PER_PAGE, "page": page}, ttl=30)
    count = int(pd.get("count") or 0)
    pages = max(1, min(math.ceil(count / PER_PAGE), MAX_WINDOW // PER_PAGE))
    base = "/profile/%s" % urllib.parse.quote(uname, safe="")
    facts = []
    if u.get("rank"):
        facts.append(("Rank", e(u["rank"])))
    if isinstance(u.get("activity"), int) and u["activity"] >= 0:
        facts.append(("Activity", num(u["activity"])))
    facts.append(("Merit", num(u.get("merit") or 0)))
    facts.append(("Archived posts", num(u.get("post_count"))))
    if u.get("created_at"):
        facts.append(("First archived post", when(u["created_at"], short=True)))
    tr = trust_html(u.get("stats"))
    if tr:
        facts.append(("DefaultTrust", tr.replace("Trust: ", "")))
    dl = "".join("<div><dt>%s</dt><dd>%s</dd></div>" % f for f in facts)
    links = " ".join([
        ext(u.get("profile_on_bitcointalk") or "%s?action=profile;u=%s" % (BCT, u.get("id")), "Original profile", "btn ghost"),
        ext(u.get("merit_history_url") or "#", "Merit history", "btn ghost"),
        ext("https://bpip.org/Profile?p=%s" % urllib.parse.quote(uname), "Trust feedback (BPIP)", "btn ghost")])
    sig = '<div class="sig full" aria-label="Signature">%s</div>' % balanced(u["signature"], tight=True) if u.get("signature") else ""
    pg = pager(base, page, pages, "Post history pages")
    body = ('%s<section class="panel profile"><div class="p-top">%s<div><h1>%s</h1><dl class="facts">%s</dl></div></div>'
            '<p class="p-links">%s</p>%s</section><div class="head"><h2>Posts by %s</h2></div>%s<section class="panel">%s</section>%s'
            % (crumbs_html([("Members", "/search/users"), (uname, base)]), avatar_html(u.get("avatar"), uname, 96), e(uname), dl,
               links, sig, e(uname), pg,
               post_cards(pd.get("posts") or [], show_author=False) if pd.get("posts") else '<p class="pad muted">No archived posts.</p>', pg))
    return layout(uname, body, active="members", canonical=base if page == 1 else "%s?page=%d" % (base, page))


def page_search_posts(q):
    term = (q.get("search", q.get("q", [""]))[0] or "").strip()[:200]
    page = max(1, int(q.get("page", ["1"])[0] or 1))
    form = ('<form class="bigq" action="/search/posts" method="get" role="search"><label for="bq">Search all archived posts</label>'
            '<div><input id="bq" name="search" type="search" value="%s" required><button class="btn" type="submit">Search</button></div>'
            '<p class="muted small">Matches words in posts and topic titles. Use "quotes" for exact phrases, -word to exclude. '
            'For ranked and filtered search, use %s.</p></form>' % (e(term), ext("https://talksearch.io", "TalkSearch")))
    if not term:
        return layout("Search", '<div class="head"><h1>Search</h1></div><section class="panel pad">%s</section>' % form,
                      active="search", canonical="/search/posts")
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
    base = "/search/posts?search=%s" % e(urllib.parse.quote_plus(term))
    pg = pager(base, page, pages, "Search result pages")
    res = post_cards(posts, titles) if posts else '<p class="pad muted">No posts match. Try fewer or different words.</p>'
    body = ('<div class="head"><h1>Search</h1></div><section class="panel pad">%s</section>'
            '<p class="sub count" role="status">%s result%s for <b>%s</b></p>%s<section class="panel">%s</section>%s'
            % (form, num(count), "" if count == 1 else "s", e(term), pg, res, pg))
    return layout("Search: %s" % term, body, q=term, noindex=True, active="search")


def page_search_users(q):
    term = (q.get("search", q.get("username", [""]))[0] or "").strip()[:60]
    page = max(1, int(q.get("page", ["1"])[0] or 1))
    form = ('<form class="bigq" action="/search/users" method="get" role="search"><label for="uq">Find a member by name</label>'
            '<div><input id="uq" name="search" type="search" value="%s" minlength="2" required><button class="btn" type="submit">Find</button></div></form>' % e(term))
    res = ""
    if len(term) >= 2:
        data = api("/api/search/users", {"search": term, "limit": 50, "page": page}, ttl=60)
        users = data.get("users") or []
        count = int(data.get("count") or 0)
        pages = int(data.get("page_count") or 1)
        pg = pager("/search/users?search=%s" % e(urllib.parse.quote_plus(term)), page, pages, "Member result pages")
        items = "".join('<li>%s%s</li>' % (avatar_html(u.get("avatar"), u.get("username"), 32), user_link(u.get("username"))) for u in users)
        more = " (showing the first 500)" if count >= 500 else ""
        res = ('<p class="sub count" role="status">%s member%s matching <b>%s</b>%s</p>%s<section class="panel">%s</section>%s'
               % (num(count), "" if count == 1 else "s", e(term), more, pg,
                  '<ul class="members">%s</ul>' % items if users else '<p class="pad muted">No member names contain that text.</p>', pg))
    elif term:
        res = '<p class="sub count" role="status">Type at least 2 characters.</p>'
    body = '<div class="head"><h1>Members</h1></div><section class="panel pad">%s</section>%s' % (form, res)
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
    return None


ACCOUNT_ONLY = re.compile(r"^/(messages|settings|patrol|mentions|watchlist|login|register|join|reset|confirm|admin|threads/posted)(/|$)")
ROUTES = [
    (re.compile(r"^/boards/(\d+)/?$"), lambda m, q: page_board(int(m.group(1)), q)),
    (re.compile(r"^/threads/(\d+)(?:/[^/]*)?/?$"), lambda m, q: page_thread(int(m.group(1)), q)),
    (re.compile(r"^/profile/([^/]+)(?:/(posts|trust))?/?$"),
     lambda m, q: page_profile(urllib.parse.unquote(m.group(1)), q, m.group(2) or "")),
    (re.compile(r"^/search/posts/?$"), lambda m, q: page_search_posts(q)),
    (re.compile(r"^/search(?:/users)?/?$"), lambda m, q: page_search_users(q)),
]
STATIC_TYPES = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".svg": "image/svg+xml", ".txt": "text/plain; charset=utf-8", ".ico": "image/x-icon"}
CSP = ("default-src 'none'; img-src * data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
       "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")


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
            if path in ("/favicon.ico", "/robots.txt"):
                return self._static("favicon.svg" if path == "/favicon.ico" else "robots.txt")
            if path.startswith("/api/"):
                return self._proxy_api()
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
                    "<p>%s</p>" % ext("https://bitcointalk.org", "Go to bitcointalk.org", "btn ghost")), cache="no-store")
            for rx, fn in ROUTES:
                m = rx.match(path)
                if m:
                    out = fn(m, q)
                    if isinstance(out, tuple):
                        return self._redirect(out[1])
                    return self._send(200, out)
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
        self._send(405, message_page("Read-only", "This archive does not accept posts or other changes."), cache="no-store")


def main():
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    print("archive-web listening on http://%s:%d (API %s)" % (BIND, PORT, API), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
