# bct-archive-web

A small, server-rendered frontend for a read-only Bitcointalk archive
(used by forum.talksearch.io). Plain HTML, one stylesheet, and about 1.6 KB of
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
| `SITE_NAME` | `Bitcointalk Archive` | Name in the header and titles |
| `SITE_TAGLINE` | `by TalkSearch` | Small text after the name |
| `SOURCE_URL` | empty | If set, a "Source code" link in the footer |

Put a reverse proxy (nginx) in front for TLS, gzip and rate limiting. Files under
`/static/` are served with a one-year cache lifetime and a version query string.

## Build

There is no build step. `static/style.css` and `static/app.js` are served as written.

## Design notes

- Familiar forum layout: categories and boards, topic lists, posts with the author
  column, SMF-style quote and code blocks, signatures under posts.
- Works without JavaScript. The script only localises dates, replaces dead images
  with a note, and repairs old post links that point at the wrong page.
- Light and dark themes follow the reader's system setting. System fonts only.
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
