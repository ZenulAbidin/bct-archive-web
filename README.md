# bct-archive-web

The frontend of [forum.talksearch.io](https://forum.talksearch.io), a read-only copy of Bitcointalk that looks and works like a forum.

It is one Python file that turns the archive's JSON API into HTML pages. There is no framework, no build step and nothing to install.

## Running it

You need Python 3.9+ and an archive API to point it at.

    ARCHIVE_API=http://127.0.0.1:18183 PORT=18190 python3 archive_web.py

Open http://127.0.0.1:18190/. In production it sits behind nginx, which does TLS and gzip.

## Settings

All settings are environment variables.

| Variable | Default | What it does |
| --- | --- | --- |
| `ARCHIVE_API` | `http://127.0.0.1:18183` | Where the archive JSON API is |
| `BIND`, `PORT` | `127.0.0.1`, `18190` | Where to listen |
| `SITE_NAME` | `Bitcointalk Forum` | Name in the header |
| `SITE_TAGLINE` | `by TalkSearch` | Small text next to the name |
| `SITE_TAGLINE_URL` | `https://talksearch.io` | Link on the name after "by" |
| `SOURCE_URL` | none | Adds a "Source code" link to the footer |
| `TALKSEARCH_API` | none | TalkSearch search API; turns on advanced search |
| `BOARD_DESCRIPTIONS` | none | JSON file `{"board id": "text"}` shown under board names |
| `IMG_CACHE` | none | Folder for cached images. Set this |
| `IMG_SECRET` | random | Signs image links. Set this too, or the links change on every restart |
| `TALKIMG_KEY` | none | TalkImg API key. Turns on picture upload in the editor |

## URLs

| URL | Page |
| --- | --- |
| `/` | Board index |
| `/boards/<id>` | A board: child boards and topics |
| `/threads/<id>` | A topic, 25 posts a page. `?post=<id>` and `?last=1` jump to the right page |
| `/profile/<name or id>` | Member profile and posts |
| `/profile/<name>/stats` | Member statistics |
| `/stats` | Forum statistics |
| `/history/<post id>` | Earlier versions of a post |
| `/search/posts`, `/search/users` | Search |
| `/compose` | Reply, quote or new topic editor |
| `/recent` | Recent posts |
| `/index.php?topic=...`, `?board=...`, `?action=profile;u=...` | Bitcointalk links. Swap the domain and they redirect to the same place here |

## Things worth knowing

**Nobody logs in here.** There are no accounts and the site never asks for a Bitcointalk password. Reply, Quote and New topic open an editor with a preview; when you are done it copies your text and opens the real form on bitcointalk.org. PM, Merit, Report and Trust just open the matching Bitcointalk page.

**Picture upload is optional.** With `TALKIMG_KEY` set, the editor can upload a picture to TalkImg. The key must stay secret, so the picture passes through this server on the way; it is checked (PNG, JPEG, GIF or WebP, 8 MB), passed on and not kept. The text of a draft is never sent to the server.

**Images go through our own proxy** (`imgproxy.py`). Old posts point at Bitcointalk's image proxy, and most of those links have expired. We fetch the original picture once, check that it really is a PNG, JPEG, GIF or WebP under 8 MB, and serve it from `/img/`. The proxy only fetches links this server signed and refuses private addresses.

**It works without JavaScript.** The scripts only show dates in your time zone, run the editor and switch the theme.

**No visitor logs.**

## Credits

- Posts and profiles: bitcointalk.org
- Merit and trust data: LoyceV
- Post edit history: BitList Archive (TryNinja)
- Search: TalkSearch
- Favicon: the Bitcoin logo, which is in the public domain (CC0)

The URL layout and API shape follow [Epochtalk](https://github.com/epochtalk/epochtalk) (MIT), which this site used before. None of its code is in here.

## Licence

MIT, see `LICENSE`. Not affiliated with bitcointalk.org.
