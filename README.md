# bct-archive-web

A small, server-rendered frontend for a read-only Bitcointalk archive
(used by forum.talksearch.io). Plain HTML, one stylesheet, and a few KB of
optional JavaScript. No framework, no build step, no web fonts, no trackers.

It renders pages from the archive's JSON API (the `/api/*` contract the
Epochtalk Vue client used) and keeps that client's public URLs working.

## Pages and URLs

| URL | Page |
| --- | --- |
| `/` | Board index and latest posts |
| `/boards/<id>?page=N` | Topic list |
| `/threads/<id>?page=N` | Topic (25 posts per page); `?start=N`, `?post=<post id>` and `?last=1` redirect to the right page |
| `/threads/<id>#<post id>` | Link to one post |
| `/profile/<name or user id>` | Member profile and post history (`/posts`, `/trust` also answer) |
| `/history/<post id>` | Earlier versions of a post, from the BitList Archive (needs `/api/posts/<id>/history` on the API) |
| `/search/posts?search=...` | Post search |
| `/search/users?search=...` | Member search |
| `/index.php?topic=T.msgP`, `?topic=T.N`, `?board=B.N`, `?action=profile;u=ID` | Bitcointalk-style links, redirected |
| `/api/*` | Passed through to the archive API |

Account-only addresses of the old client (`/messages`, `/settings`, `/login`, ...)
answer 403 with a short explanation. The archive has no accounts.

## Run

Requires Python 3.9 or newer. There are no dependencies to install.

```sh
ARCHIVE_API=http://127.0.0.1:18183 PORT=18190 python3 archive_web.py
```

Then open http://127.0.0.1:18190/.

| Variable | Default | Meaning |
| --- | --- | --- |
| `ARCHIVE_API` | `http://127.0.0.1:18183` | Base URL of the archive JSON API |
| `BIND` | `127.0.0.1` | Listen address |
| `PORT` | `18190` | Listen port |
| `SITE_NAME` | `Bitcointalk Forum` | Name in the header and titles |
| `SITE_TAGLINE` | `by TalkSearch` | Small text after the name |
| `SOURCE_URL` | empty | If set, a "Source code" link in the footer |

Put a reverse proxy (nginx) in front for TLS, gzip and rate limiting. Files under
`/static/` are served with a one-year cache lifetime and a version query string.

## Optional settings

| Variable | Meaning |
| --- | --- |
| `TALKSEARCH_API` | Base URL of a TalkSearch search API. Enables the ranked "Advanced search" form |
| `BOARD_DESCRIPTIONS` | JSON file `{"<board id>": "description"}` shown under board names |
| `IMG_CACHE` | Directory for the image proxy's disk cache (strongly recommended) |
| `IMG_SECRET` | Secret that signs image addresses. Set it, or links change at every restart |

## Image proxy

Post and signature images are never loaded from third-party hosts by the reader's browser.
`imgproxy.py` undoes Bitcointalk's own image proxy links, fetches the original once, checks
it and serves it from `/img/...`. It only fetches addresses this server signed, refuses
private and loopback hosts (the connection is pinned to the checked address), follows at
most 3 redirects, stops at 8 MB or 10 s, and accepts only real PNG, JPEG, GIF or WebP data.
Nothing is logged.

## Actions and the composer

Reply, Quote and New topic open a BBCode composer (`/compose`) with a toolbar and a live
preview. The draft stays in the browser's local storage and nothing typed there is sent to
this server. "Copy and open Bitcointalk" copies the text and opens the real form on
bitcointalk.org in a popup window. All other actions (PM, merit, report, trust, notify,
print, login) are plain links that open the matching bitcointalk.org page in that popup.
This site has no accounts and never asks for Bitcointalk credentials.

## Build

There is no build step. `static/style.css` and `static/app.js` are served as written.

## Design notes

- Familiar forum layout: categories and boards, topic lists, posts with the author
  column, SMF-style quote and code blocks, signatures under posts.
- Works without JavaScript. The script only localises dates, replaces dead images
  with a note, and repairs old post links that point at the wrong page.
- Dense table layout in the style of Simple Machines Forum, in Bitcoin orange. Light theme by
  default, dark on request (button, or `?theme=dark`). System fonts only.
- Accessibility: skip link, landmarks, table headers, visible focus, 4.5:1 text
  contrast, 44 px touch targets on phones, no motion.
- Post and signature HTML is sanitised by the API. This frontend additionally
  re-balances it so a missing end tag cannot break the page, and sends a strict
  Content-Security-Policy (no inline or third-party scripts).
- No request logging.

## Licence and attribution

MIT, see `LICENSE`. All code and assets in this repository were written for it;
the two icons and the favicon are original SVG drawings. No third-party code,
fonts or images are included.

The URL scheme and JSON API shape follow [Epochtalk](https://github.com/epochtalk/epochtalk)
(MIT, Copyright (c) 2016 Epochtalk) so that existing links keep working. No
Epochtalk source code is included. Quote and code block class names
(`quoteheader`, `quote`, `codeheader`, `code`) come from the archived post markup
produced by Simple Machines Forum.

This project is not affiliated with bitcointalk.org, Epochtalk or Simple Machines.
