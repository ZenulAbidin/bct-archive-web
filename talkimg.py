# SPDX-License-Identifier: MIT
"""Relay for composer image uploads to TalkImg (talkimg.com, the Bitcointalk community image host).

The API key has to stay on the server, so the reader's browser sends the picture here and this
module passes it on. Off unless TALKIMG_KEY is set. Only real PNG, JPEG, GIF or WebP data up to
MAX_BYTES is accepted, the hourly total is capped, and nothing about the upload is logged or kept.
"""
import json
import os
import threading
import time
import urllib.request
import uuid

import imgproxy

KEY = os.environ.get("TALKIMG_KEY", "")
API = os.environ.get("TALKIMG_API", "https://talkimg.com/api/1/upload")
MAX_BYTES = 8 * 1024 * 1024
PER_HOUR = int(os.environ.get("TALKIMG_PER_HOUR", "300"))
_lock = threading.Lock()
_times = []
_EXT = {"png": ("png", "image/png"), "jpg": ("jpg", "image/jpeg"), "gif": ("gif", "image/gif"), "webp": ("webp", "image/webp")}


def enabled():
    return bool(KEY)


def upload(data):
    """(status, dict) for the browser. dict has "url" on success, else "error"."""
    if not KEY:
        return 404, {"error": "Image upload is not available."}
    kind = imgproxy.sniff(data)
    if not kind:
        return 415, {"error": "Only PNG, JPEG, GIF and WebP pictures can be uploaded."}
    with _lock:
        now = time.time()
        _times[:] = [t for t in _times if now - t < 3600]
        if len(_times) >= PER_HOUR:
            return 429, {"error": "Too many uploads right now. Try again later."}
        _times.append(now)
    ext, ctype = _EXT[kind]
    boundary = uuid.uuid4().hex
    body = (("--%s\r\nContent-Disposition: form-data; name=\"format\"\r\n\r\njson\r\n"
             "--%s\r\nContent-Disposition: form-data; name=\"source\"; filename=\"image.%s\"\r\nContent-Type: %s\r\n\r\n"
             % (boundary, boundary, ext, ctype)).encode() + data + ("\r\n--%s--\r\n" % boundary).encode())
    req = urllib.request.Request(API, data=body, method="POST", headers={
        "X-API-Key": KEY, "Content-Type": "multipart/form-data; boundary=" + boundary,
        "Accept": "application/json", "User-Agent": "Mozilla/5.0 (compatible; archive-composer)"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            out = json.loads(r.read(1000000).decode("utf-8"))
        url = (out.get("image") or {}).get("url") or ""
    except Exception:
        return 502, {"error": "TalkImg did not accept the picture. Try again, or upload it on talkimg.com yourself."}
    if not url.startswith("https://"):
        return 502, {"error": "TalkImg did not accept the picture."}
    return 200, {"url": url, "preview": imgproxy.proxied(url)}
