# SPDX-License-Identifier: MIT
"""Turn an archived post's HTML back into Bitcointalk BBCode, for the Quote button.

The archive stores posts as the HTML Bitcointalk rendered. To quote one on the real forum the
composer needs BBCode again. This covers the tags Bitcointalk's own renderer produces; anything
unknown is reduced to its text, so the result is always safe to paste.
"""
import re
import urllib.parse
from html.parser import HTMLParser

_SIMPLE = {"b": "b", "strong": "b", "i": "i", "em": "i", "u": "u", "s": "s", "strike": "s", "del": "s",
           "tt": "tt", "sub": "sub", "sup": "sup", "pre": "pre", "table": "table", "tr": "tr", "td": "td",
           "li": "li", "center": "center"}
_QUOTE_HEAD = re.compile(r"^Quote from:\s*(.+?)\s+on\s+(.+)$", re.S)
_LINK = re.compile(r"/threads/(\d+)(?:\?post=\d+)?#(\d+)$")


_SMILEY = re.compile(r"bitcointalk\.org/Smileys/[^/]+/([A-Za-z0-9_]+)\.gif$")
SMILEYS = {"smiley": ":)", "wink": ";)", "cheesy": ":D", "grin": ";D", "angry": ">:(", "sad": ":(", "shocked": ":o",
           "cool": "8)", "huh": "???", "rolleyes": "::)", "tongue": ":P", "embarrassed": ":-[", "lipsrsealed": ":-X",
           "undecided": ":-\\", "kiss": ":-*", "cry": ":'("}


def _image_source(src):
    try:
        p = urllib.parse.urlsplit(src)
        if (p.hostname or "").lower() == "ip.bitcointalk.org":
            return urllib.parse.parse_qs(p.query).get("u", [src])[0]
    except ValueError:
        pass
    return src


class _ToBB(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.stack = []        # closing text for each open element
        self.head = None       # collecting a quote header: {"text": [], "href": ""}
        self.pending = None    # quote header waiting for its quote body

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        close = ""
        if tag == "br":
            self.emit("\n")
            return
        if tag == "hr":
            self.emit("[hr]")
            return
        if tag == "img":
            if a.get("src"):
                m = _SMILEY.search(a["src"])
                if m and m.group(1).lower() in SMILEYS:
                    self.emit(SMILEYS[m.group(1).lower()])   # forum smileys go back to their text codes
                else:
                    self.emit("[img]%s[/img]" % _image_source(a["src"]))
            return
        if tag == "div" and cls == "quoteheader":
            self.head = {"text": [], "href": ""}
            self.stack.append("\0head")
            return
        if self.head is not None:
            if tag == "a" and a.get("href"):
                self.head["href"] = a["href"]
            self.stack.append("")
            return
        if tag == "div" and cls == "quote":
            open_tag = "[quote]"
            if self.pending:
                m = _QUOTE_HEAD.match(self.pending["text"])
                link = _LINK.search(self.pending["href"] or "")
                if m:
                    open_tag = "[quote author=%s%s]" % (
                        m.group(1), " link=topic=%s.msg%s#msg%s" % (link.group(1), link.group(2), link.group(2)) if link else "")
            self.pending = None
            self.emit(open_tag)
            close = "[/quote]\n"
        elif tag == "div" and cls == "codeheader":
            self.stack.append("\0skip")
            return
        elif tag == "div" and cls == "code":
            self.emit("[code]")
            close = "[/code]\n"
        elif tag in _SIMPLE:
            self.emit("[%s]" % _SIMPLE[tag])
            close = "[/%s]" % _SIMPLE[tag]
        elif tag in ("ul", "ol"):
            self.emit("[list]")
            close = "[/list]"
        elif tag == "a" and a.get("href"):
            href = a["href"]
            if href.startswith("/"):
                href = "https://bitcointalk.org/index.php?topic=" + href.rsplit("/", 1)[-1].replace("#", ".msg") \
                    if href.startswith("/threads/") else href
            self.emit("[url=%s]" % href)
            close = "[/url]"
        elif tag in ("span", "font", "div", "p"):
            style = a.get("style") or ""
            opens, closes = [], []
            m = re.search(r"(?<![-\w])color:\s*([#\w(),.% ]+?)\s*(?:;|$)", style)
            if m or a.get("color"):
                opens.append("[color=%s]" % (m.group(1) if m else a["color"]))
                closes.append("[/color]")
            m = re.search(r"font-size:\s*([\d.]+(?:pt|px|em|%))", style)
            if m:
                opens.append("[size=%s]" % m.group(1))
                closes.append("[/size]")
            m = re.search(r"font-family:\s*([^;]+)", style)
            if m:
                opens.append("[font=%s]" % m.group(1).strip())
                closes.append("[/font]")
            if "text-decoration: underline" in style:
                opens.append("[u]")
                closes.append("[/u]")
            align = (a.get("align") or "").lower() or (re.search(r"text-align:\s*(\w+)", style) or [None, ""])[1]
            if align in ("center", "right", "left"):
                opens.append("[%s]" % align)
                closes.append("[/%s]" % align)
            self.emit("".join(opens))
            close = "".join(reversed(closes))
        self.stack.append(close)

    def handle_endtag(self, tag):
        if tag in ("br", "hr", "img") or not self.stack:
            return
        close = self.stack.pop()
        if close == "\0head":
            self.pending = {"text": "".join(self.head["text"]).strip(), "href": self.head["href"]}
            self.head = None
        elif close == "\0skip":
            pass
        elif self.head is None:
            self.emit(close)

    def handle_data(self, d):
        if self.head is not None:
            self.head["text"].append(d)
        elif "\0skip" not in self.stack:
            self.emit(d)

    def emit(self, text):
        if self.head is None and text:
            self.out.append(text)


def to_bbcode(body_html):
    p = _ToBB()
    try:
        p.feed(body_html or "")
        p.close()
    except Exception:
        return re.sub(r"<[^>]+>", "", body_html or "")
    while p.stack:
        close = p.stack.pop()
        if not close.startswith("\0"):
            p.out.append(close)
    text = "".join(p.out).replace("\r", "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def quote_block(author, topic_id, post_id, unix_time, body_html):
    date = " date=%d" % unix_time if unix_time else ""
    return "[quote author=%s link=topic=%s.msg%s#msg%s%s]\n%s\n[/quote]\n" % (
        author, topic_id, post_id, post_id, date, to_bbcode(body_html))
