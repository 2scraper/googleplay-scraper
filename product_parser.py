"""
product_parser.py
-----------------
Google Play, and the only file in this repo that knows anything about it.

Everything here was derived from captures taken on 2026-09-22 from one
datacentre address (netcup, Nuremberg) with plain `curl` and no cookies. The
captures are in `captures/` and every number below can be re-measured from
them.

What this site publishes, and where
-----------------------------------
Play renders its grids server-side and then states them AGAIN, in full, as
JSON inside `AF_initDataCallback(...)` blocks. The JSON is the primary path
here — not the markup — because it carries columns the tile never paints
(the genre key, the IAP range, the description) and because Play's own CSS
class names are build hashes.

There is exactly ONE `application/ld+json` block on an app page and NONE on
any grid. It is read, and it is read second: the page's own payload and the
JSON-LD disagree about the same app's rating in the same fetch — 4.616471
against 4.616447925567627 — so the payload wins and the JSON-LD is a
cross-check, not a source. (Both are float32 noise around a store that
displays "4.6"; see `rating` in `output_writer.App`.)

The block key is NOT stable and must never be keyed on
--------------------------------------------------------
The payload that holds the grid is `ds:3` on a category page, `ds:4` on a
search page, `ds:3` on a developer page and `ds:8` on an app page — measured
across the captures, with the key stable per page KIND and different between
kinds. A sibling repo in this family records the same thing about another
Google property: the keys shift between URL variants.

So nothing here selects a block by name. `find_app_records()` walks every
block and recognises a record by its SHAPE — a list whose first element is
`[<package id>, 7]` — which is one rule for all four routes and survives
Google renumbering the blocks.

What a grid tile carries
------------------------
Measured on 330 distinct tiles from six captures: title, rating (display
string AND raw float), genre, price with currency, description, developer,
install bucket, content rating and the IAP range are on 100% of US tiles.
On a German capture, rating is on 92 of 128 and the install bucket on 108 of
128 — unrated and newly published apps genuinely lack them, which is why the
second locale is captured at all.

What only the APP page carries
------------------------------
The exact install count (12,322,703,000 beside a displayed
"10,000,000,000+"), the five-bucket rating histogram, the split between
ratings (244,140,056) and written reviews (1,952,258), the version, the
update date, the developer's email and site, the privacy policy, and Play's
own chart standing ("#1" in "top free communication"). Measured: 0 of those
330 listing tiles carry an exact install count.

Pagination is not arithmetic and there is no `?page=N`
------------------------------------------------------
A category page answers with ~64 apps and six addressable shelves
(`/store/apps/collection/cluster?gsr=<token>`), each expanding to 10-20 more.
The tokens are on page 1, so pages 2..N ARE independently addressable once
page 1 is in hand — which is the family's concurrency model exactly. The
terminating condition is DATA ("this fetch added no new package id"), never a
selector.

Note the parameter is `gsr=`. An earlier `clp=` reads as valid and answers
HTTP 302 to itself, forever.

Reviews come from one structure reached two ways
------------------------------------------------
The first page of an app's reviews is embedded in the app page's own payload.
Every page after it comes from `/_/PlayStoreUi/data/batchexecute`, RPC
`UsvDTd`, which answers a plain POST from a datacentre address with no
cookies and no key. Page 2 shares none of page 1's ids and every page carries
a continuation token, so the mode is bounded by `--pages` rather than by the
store. Both routes are the same 17-slot record, so `parse_review_record()`
serves both and they cannot drift.
"""

import html as _html
import json
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from output_writer import App, Review, SOURCE_DEFAULT


# --------------------------------------------------------------------------
# The site
# --------------------------------------------------------------------------

HOST = "play.google.com"
BASE = "https://play.google.com"

# Every mode this repo implements. `books` and `movies` are deliberately
# absent: both storefronts answer (1.8 MB and 1.3 MB of served markup on
# 2026-09-22) and neither has been captured or parsed, so a mode for them
# would ship against markup nobody has read.
MODES = ("listing", "search", "app", "developer", "reviews")

# Modes that read a grid of tiles. Kept as data so the engines cannot
# disagree with each other about which routes paginate.
GRID_MODES = ("listing", "search", "developer")

# Play's own locale-independent genre keys, taken from the `hreflang`-free
# route table the store itself links: `/store/apps/category/<KEY>`. Only the
# top-level ones are listed; a key this table does not know is still accepted
# and passed through, because Google adds them faster than anyone re-derives
# a list, and refusing an unknown one would be refusing a URL that works.
KNOWN_CATEGORIES = (
    "APPLICATION", "ANDROID_WEAR", "ART_AND_DESIGN", "AUTO_AND_VEHICLES",
    "BEAUTY", "BOOKS_AND_REFERENCE", "BUSINESS", "COMICS", "COMMUNICATION",
    "DATING", "EDUCATION", "ENTERTAINMENT", "EVENTS", "FINANCE",
    "FOOD_AND_DRINK", "HEALTH_AND_FITNESS", "HOUSE_AND_HOME",
    "LIBRARIES_AND_DEMO", "LIFESTYLE", "MAPS_AND_NAVIGATION", "MEDICAL",
    "MUSIC_AND_AUDIO", "NEWS_AND_MAGAZINES", "PARENTING", "PERSONALIZATION",
    "PHOTOGRAPHY", "PRODUCTIVITY", "SHOPPING", "SOCIAL", "SPORTS", "TOOLS",
    "TRAVEL_AND_LOCAL", "VIDEO_PLAYERS", "WATCH_FACE", "WEATHER", "GAME",
    "GAME_ACTION", "GAME_ADVENTURE", "GAME_ARCADE", "GAME_BOARD",
    "GAME_CARD", "GAME_CASINO", "GAME_CASUAL", "GAME_EDUCATIONAL",
    "GAME_MUSIC", "GAME_PUZZLE", "GAME_RACING", "GAME_ROLE_PLAYING",
    "GAME_SIMULATION", "GAME_SPORTS", "GAME_STRATEGY", "GAME_TRIVIA",
    "GAME_WORD", "FAMILY",
)

# An Android package id. Deliberately not `[\w.]+`: a package id has at least
# one dot, starts with a letter, and Play's URLs put nothing else in `id=` on
# the details route.
_PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+$")

# The id recovered from a store URL. Both spellings of the details path are
# real: Play links `/store/apps/details?id=` everywhere and also serves
# `/store/apps/details/<Slugged_Name>?id=`.
_ID_IN_URL_RE = re.compile(r"[?&]id=([A-Za-z0-9_.%]+)")

# The developer route has two spellings and they are NOT interchangeable:
# `/store/apps/dev?id=<numeric>` and `/store/apps/developer?id=<name>`.
# Measured on one page, both forms appear for different developers
# (`dev?id=4772240228547998649`, `developer?id=WhatsApp+LLC`), so the parser
# records whichever the page published rather than normalising to one.
_DEV_NUMERIC_RE = re.compile(r"^\d+$")


# --------------------------------------------------------------------------
# Detecting whether we were served anything
# --------------------------------------------------------------------------

# Google's refusal vocabulary. Every one of these scored ZERO on all eleven
# captures taken 2026-09-22, served pages and the store's own 404 alike —
# which is the requirement (§ "count it on a page you know is good"), and is
# also all that can honestly be said about them: no refusal has been observed
# from this address, so these are candidates rather than confirmed markers.
#
# There is no reCAPTCHA marker in this set on purpose. `recaptcha/api.js`,
# `g-recaptcha`, `grecaptcha` and `data-sitekey` are each 0 on all eleven
# captures, and adding a marker that has never matched anything would be dead
# code that looks load-bearing. `captcha_solver.py` still runs its own live
# detection, so a challenge that does appear is found by looking at the page
# rather than by this list.
BOT_CHALLENGE_MARKERS = (
    # The interstitial Google serves to an address it has scored. It lives on
    # a path rather than in a vendor's markup, which is why the path is the
    # marker.
    "/sorry/index",
    "Our systems have detected unusual traffic",
)

# The structural signal, and the one that actually carries the weight here.
# Every page Play serves is built out of its own image host: 270 to 5,712
# references on each of the ten served captures, and 0 on the store's own
# 404. An interstitial, a proxy error page or Chromium's own network-error
# page is built out of none of it.
ASSET_HOST = "play-lh.googleusercontent.com"

# Two is the floor rather than one so a single incidental reference cannot
# pass. The lowest count on a real served page measured here is 252.
MIN_ASSET_REFERENCES = 2


def asset_reference_count(markup: str) -> int:
    """How many times the page references Play's own image host."""
    return markup.count(ASSET_HOST) if markup else 0


def challenge_marker(markup: str) -> Optional[str]:
    """The first refusal marker present, or None.

    Entities are unescaped over a bounded prefix first: a sibling repo in
    this family lost a marker for months because its edge entity-escaped the
    punctuation for an HTTP client and a browser served it back plain.
    """
    if not markup:
        return None
    head = _html.unescape(markup[:20000])
    for marker in BOT_CHALLENGE_MARKERS:
        if marker in head:
            return marker
    return None


# --------------------------------------------------------------------------
# Decoding and payload extraction
# --------------------------------------------------------------------------

def decode_page(raw: Any, charset_hint: Optional[str] = None) -> str:
    """Bytes (or str) -> str, honouring the declared charset.

    A browser engine hands this module a `str` and this is a no-op. The HTTP
    client hands it bytes; Play declares utf-8 on every route measured, but
    decoding blind is how a sibling repo turned every title on a page into
    replacement characters while the numbers still parsed.
    """
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, (bytes, bytearray)):
        return str(raw)
    encodings = []
    if charset_hint:
        encodings.append(charset_hint)
    head = bytes(raw[:4096]).decode("ascii", "replace")
    m = re.search(r'charset=["\']?([A-Za-z0-9_\-]+)', head)
    if m:
        encodings.append(m.group(1))
    encodings.append("utf-8")
    for enc in encodings:
        try:
            return bytes(raw).decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return bytes(raw).decode("utf-8", "replace")


# `AF_initDataCallback({key: 'ds:3', hash: '4', data:[...], sideChannel: {}})`
_AF_BLOCK_RE = re.compile(r"AF_initDataCallback\((\{.*?\})\);", re.S)
_AF_KEY_RE = re.compile(r"key:\s*'(ds:\d+)'")
_AF_DATA_RE = re.compile(r"data:\s*(\[.*\])\s*,\s*sideChannel", re.S)


def iter_data_blocks(markup: str) -> Iterable[Tuple[str, Any]]:
    """Yield `(key, parsed)` for every AF_initDataCallback block.

    A block whose data does not parse is skipped rather than raised on: Play
    ships blocks that are not JSON at all (an app page has thirteen blocks and
    several are bookkeeping), and one of them failing must not cost the run
    the twelve that are fine.
    """
    if not markup:
        return
    for m in _AF_BLOCK_RE.finditer(markup):
        blob = m.group(1)
        key_m = _AF_KEY_RE.search(blob)
        data_m = _AF_DATA_RE.search(blob)
        if not (key_m and data_m):
            continue
        try:
            yield key_m.group(1), json.loads(data_m.group(1))
        except (ValueError, RecursionError):
            continue


def _walk(node: Any) -> Iterable[list]:
    """Every list inside `node`, including `node`."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, list):
            yield cur
            # Reversed, because `pop()` takes from the end: without it the
            # walk is a pre-order DFS that visits each node's children
            # backwards, and every grid would come out in reverse document
            # order while still looking correct in a count.
            stack.extend(reversed(cur))


def is_app_record(node: Any) -> bool:
    """Is this the repeating record Play uses for one app in a grid?

    The shape is `[[<package id>, 7], <icon>, <screenshots>, <title>, ...]`.
    The 7 is Play's own type tag for an Android app; a book or a movie tile
    carries a different one, which is what keeps a `/store/books` payload from
    being read as apps.

    The length floor is what separates a full tile from the many short
    `[<package id>, 7]` references Play sprinkles through a page (tracking
    payloads, "you might also like" stubs). Measured: the floor of 16 gives
    64 tiles and 64 distinct ids on a category capture whose grid holds 64.
    """
    return (
        isinstance(node, list)
        and len(node) >= 16
        and isinstance(node[0], list)
        and len(node[0]) == 2
        and isinstance(node[0][0], str)
        and node[0][1] == 7
        and bool(_PACKAGE_RE.match(node[0][0]))
    )


def find_app_records(markup_or_blocks: Any) -> List[list]:
    """Every app record in a page, in document order, deduped by package id.

    Deduped because one page legitimately lists an app twice: a category
    capture holds 72 tile-shaped records for 64 distinct apps, an app being
    in more than one shelf. Document order is kept, so the first shelf an app
    appears in is the one whose rank it keeps.
    """
    if isinstance(markup_or_blocks, str):
        blocks = [data for _key, data in iter_data_blocks(markup_or_blocks)]
    else:
        blocks = list(markup_or_blocks)
    out: List[list] = []
    seen = set()
    for block in blocks:
        for node in _walk(block):
            if is_app_record(node):
                pkg = node[0][0]
                if pkg not in seen:
                    seen.add(pkg)
                    out.append(node)
    return out


# --------------------------------------------------------------------------
# Scalars
# --------------------------------------------------------------------------

def _text(node: Any) -> Optional[str]:
    """Play wraps almost every string in a one-element list."""
    if isinstance(node, str):
        return node or None
    if isinstance(node, list) and node and isinstance(node[0], str):
        return node[0] or None
    return None


def _strip_html(value: Optional[str]) -> Optional[str]:
    """Play's descriptions and release notes are HTML fragments."""
    if not value:
        return None
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = _html.unescape(text)
    text = re.sub(r"[ \t]+\n", "\n", text).strip()
    return text or None


def parse_rating(node: Any) -> Tuple[Optional[float], Optional[float]]:
    """`["4.6", 4.616471]` -> (4.6, 4.616471).

    The display string is the rating; the float beside it is provenance. They
    are not the same number and the difference is not rounding error in one
    direction — the same app's JSON-LD in the same fetch says 4.616447925567627.

    The display string is locale-formatted: a German page writes "4,6".
    """
    if not isinstance(node, list) or not node:
        return None, None
    shown, raw = None, None
    if isinstance(node[0], str):
        try:
            shown = float(node[0].replace(",", "."))
        except ValueError:
            shown = None
    if len(node) > 1 and isinstance(node[1], (int, float)):
        raw = float(node[1])
    return shown, raw


def parse_installs(node: Any) -> Tuple[Optional[str], Optional[int], Optional[int]]:
    """`["10,000,000,000+", 10000000000, 12322703000, "10B+"]`.

    Returns (display, floor, exact). The third slot is Play's real number and
    it is published on the app route only — 0 of 330 listing tiles carry it.
    On a listing tile the node is the bare display string.
    """
    if isinstance(node, str):
        return node or None, _installs_floor(node), None
    if not isinstance(node, list) or not node:
        return None, None, None
    display = node[0] if isinstance(node[0], str) else None
    floor = node[1] if len(node) > 1 and isinstance(node[1], int) else _installs_floor(display)
    exact = node[2] if len(node) > 2 and isinstance(node[2], int) else None
    # Guard the ordering rather than trusting it: the floor must not exceed
    # the exact count, and if it does the two slots are not what they were
    # taken for, so neither is reported.
    if exact is not None and floor is not None and exact < floor:
        return display, floor, None
    return display, floor, exact


def _installs_floor(display: Optional[str]) -> Optional[int]:
    """"1,000,000,000+" -> 1000000000.

    Grouping is locale-formatted, so the separators are stripped rather than
    interpreted: every character that is not a digit goes, which is safe here
    because an install bucket is always a whole number with a trailing "+".
    """
    if not display:
        return None
    digits = re.sub(r"[^\d]", "", display)
    return int(digits) if digits else None


def parse_offer(node: Any) -> Tuple[Optional[float], Optional[str], Optional[bool]]:
    """Play states a price in MICROS: `[[0, "USD", ""]]`, `[[6990000, "USD", "$6.99"]]`.

    Returns (price, currency, in_stock). Zero is a price the site published
    rather than a missing read, so a free app gets 0.0 and not None — and an
    app whose offer node is absent gets None, which is the distinction the
    family's "never present a guess as a fact" rule is about.

    The currency comes from the offer itself and is never inferred from the
    symbol in the third slot: that slot is display text and a bare symbol is
    a guess, while `"USD"` beside it is a fact.
    """
    for sub in _walk(node if isinstance(node, list) else []):
        if (len(sub) >= 2 and isinstance(sub[0], (int, float))
                and isinstance(sub[1], str) and len(sub[1]) == 3
                and sub[1].isalpha() and sub[1].isupper()):
            return round(sub[0] / 1_000_000.0, 6), sub[1], True
    return None, None, None


def epoch_to_iso(node: Any) -> Optional[str]:
    """`[1789778944, 484000000]` -> "2026-09-19T...+00:00".

    Play pairs epoch seconds with nanoseconds. Only the seconds are kept: the
    nanosecond field varies between fetches of one unchanged page, and writing
    it through would make two runs diff on a field that describes nothing.
    """
    seconds = None
    if isinstance(node, list):
        for v in node:
            if isinstance(v, int) and 1_000_000_000 < v < 4_000_000_000:
                seconds = v
                break
            if isinstance(v, list):
                inner = epoch_to_iso(v)
                if inner:
                    return inner
    elif isinstance(node, int) and 1_000_000_000 < node < 4_000_000_000:
        seconds = node
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


_CHART_RANK_RE = re.compile(r"#\s*(\d+)")


def parse_chart(node: Any) -> Tuple[Optional[str], Optional[int]]:
    """`["top free communication", [], "#1"]` -> ("top free communication", 1)."""
    if not isinstance(node, list) or not node or not isinstance(node[0], str):
        return None, None
    name = node[0] or None
    rank = None
    for v in node[1:]:
        if isinstance(v, str):
            m = _CHART_RANK_RE.search(v)
            if m:
                rank = int(m.group(1))
                break
    return name, rank


def _first_url(node: Any, must_contain: str = "") -> Optional[str]:
    """The first URL anywhere inside `node`.

    Play buries every link several levels down in a fixed-width envelope
    (`[null, null, null, null, [null, null, "<url>"]]`), and the envelope's
    width differs between slots. Searching by shape rather than by path is
    what keeps one helper working for icons, screenshots, websites and
    policies alike.
    """
    for sub in _walk(node if isinstance(node, list) else []):
        for v in sub:
            if isinstance(v, str) and (v.startswith("http") or v.startswith("/")):
                if not must_contain or must_contain in v:
                    return v
    return None


# --------------------------------------------------------------------------
# URLs
# --------------------------------------------------------------------------

def app_url(package_id: str, hl: str = "en", gl: str = "US") -> str:
    """The canonical store URL for one app in one market.

    Rebuilt from the package id rather than taken from the page: Play's own
    anchors are relative and carry a per-impression tracking tail, so two runs
    of one listing would otherwise never produce the same string. `hl`/`gl`
    stay on it because the page it addresses is market-specific.
    """
    q = urllib.parse.urlencode({"id": package_id, "hl": hl, "gl": gl})
    return f"{BASE}/store/apps/details?{q}"


def listing_url(category: str, hl: str = "en", gl: str = "US") -> str:
    q = urllib.parse.urlencode({"hl": hl, "gl": gl})
    return f"{BASE}/store/apps/category/{category}?{q}"


def search_url(query: str, hl: str = "en", gl: str = "US") -> str:
    q = urllib.parse.urlencode({"q": query, "c": "apps", "hl": hl, "gl": gl})
    return f"{BASE}/store/search?{q}"


def developer_url(developer: str, hl: str = "en", gl: str = "US") -> str:
    """Both spellings are real and they are not interchangeable.

    A numeric id answers on `/store/apps/dev`, a name on
    `/store/apps/developer`. Measured on one capture, which published both
    forms for different developers.
    """
    path = "dev" if _DEV_NUMERIC_RE.match(str(developer)) else "developer"
    q = urllib.parse.urlencode({"id": developer, "hl": hl, "gl": gl})
    return f"{BASE}/store/apps/{path}?{q}"


def package_from_url(url: str) -> Optional[str]:
    """The package id a store URL names, or None."""
    if not url:
        return None
    m = _ID_IN_URL_RE.search(url)
    if not m:
        return None
    candidate = urllib.parse.unquote(m.group(1))
    return candidate if _PACKAGE_RE.match(candidate) else None


def with_market(url: str, hl: Optional[str], gl: Optional[str]) -> str:
    """Set `hl`/`gl` on a URL, REPLACING rather than appending.

    A caller's own `--url` may already carry them, and a second copy of a
    query parameter is resolved by the server in a way nobody should have to
    know.
    """
    parts = urllib.parse.urlsplit(url)
    q = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    if hl:
        q["hl"] = hl
    if gl:
        q["gl"] = gl
    return urllib.parse.urlunsplit(
        (parts.scheme or "https", parts.netloc or HOST, parts.path,
         urllib.parse.urlencode(q), ""))


def mode_for_url(url: str) -> Optional[str]:
    """Which mode a URL belongs to, or None if this repo does not read it.

    Returning None is a refusal WITH a reason available to the caller, not a
    guess: `/store/books` and `/store/movies` are real Play routes that this
    repo has never captured, and reading them with the apps parser would
    return zero rows and call it an empty catalogue.
    """
    path = urllib.parse.urlsplit(url).path
    if "/store/apps/details" in path:
        return "app"
    if "/store/search" in path:
        return "search"
    if "/store/apps/dev" in path:       # covers /dev and /developer
        return "developer"
    if "/store/apps/category" in path or "/store/apps/collection" in path:
        return "listing"
    if path.rstrip("/") in ("/store/apps", "/store/games"):
        return "listing"
    return None


def refusal_reason(url: str) -> Optional[str]:
    """Why this repo will not read `url`, in words a reader can act on."""
    parts = urllib.parse.urlsplit(url)
    if parts.netloc and parts.netloc.split(":")[0] not in (HOST, f"www.{HOST}"):
        return (f"{parts.netloc} is not {HOST} — this repo reads the Google "
                f"Play store only")
    path = parts.path
    for store in ("books", "movies", "music", "tv"):
        if f"/store/{store}" in path:
            return (f"/store/{store} is a real Play storefront and this repo "
                    f"does not implement it: it has never been captured or "
                    f"parsed, so reading it with the apps parser would report "
                    f"an empty catalogue rather than an unread format")
    if mode_for_url(url) is None:
        return f"no mode in this repo reads {path or '/'}"
    return None


# The shelf links a grid page publishes. The parameter is `gsr=` and the
# token contains a colon, which is why this regex is not `[^&"]+`: an earlier
# pattern that stopped at the first colon produced a URL that answers 404.
_CLUSTER_LINK_RE = re.compile(
    r"/store/apps/collection/cluster\?gsr=[A-Za-z0-9%_\-]+(?::[A-Za-z0-9_\-]+)*")


def cluster_links(markup: str, hl: str = "en", gl: str = "US") -> List[str]:
    """Every addressable shelf on a grid page, in document order, deduped.

    These are what `--pages N` follows. They are all present on page 1, so
    pages 2..N are independently addressable once page 1 is in hand — which
    is what makes `--concurrency` safe here, and is the family's model
    exactly: page 1 alone, then the rest in parallel.

    The markup is unescaped first because inside a data block the links
    arrive with `\\u003d` for `=`.
    """
    if not markup:
        return []
    text = markup.replace("\\u003d", "=").replace("\\u0026", "&").replace("\\/", "/")
    out, seen = [], set()
    for path in _CLUSTER_LINK_RE.findall(text):
        if path in seen:
            continue
        seen.add(path)
        out.append(with_market(BASE + path, hl, gl))
    return out


# --------------------------------------------------------------------------
# Grid tiles -> App
# --------------------------------------------------------------------------

# Where each column sits inside a grid tile. Measured across 330 tiles from
# six captures in two locales; stable in all of them.
#
# This is a slot map and slot maps rot, so nothing below indexes it blindly:
# `_slot()` returns None for a short record, and `parse_tile()` is written so
# that a renumbered slot costs one column rather than the row.
TILE = {
    "package": 0, "icon": 1, "screenshots": 2, "title": 3, "rating": 4,
    "genre": 5, "offer": 8, "url": 10, "description": 13, "developer": 14,
    "installs": 15, "content_rating": 24, "iap": 29,
}


def _slot(record: Sequence[Any], index: int) -> Any:
    return record[index] if isinstance(record, (list, tuple)) and len(record) > index else None


def _records_under(node: Any) -> List[list]:
    """Every app record inside `node`, in document order."""
    return [n for n in _walk(node) if is_app_record(n)]


def find_shelves(markup_or_blocks: Any) -> List[Tuple[Optional[str], List[list]]]:
    """Every shelf on a grid page, as `(title, [records])`.

    A category page is not one grid — it is several, and they mean different
    things. Measured on one capture: seven shelves holding 20, 11, 10, 10, 9,
    8 and 4 tiles, with "Top free", "Top grossing", "Top paid" and "Popular"
    among their names, and an app legitimately sitting in more than one. A row
    that does not say which shelf it came from cannot say which ranking its
    `position` describes, which is why `shelf` is a column.

    A shelf is found as the nearest node at least two of whose DIRECT children
    each hold a tile. The tiles do not sit at a fixed depth — measured on one
    page, grouping by a fixed ancestor distance gives 16 groups for 7 shelves
    — so depth is what this must not assume.
    """
    if isinstance(markup_or_blocks, str):
        blocks = [data for _k, data in iter_data_blocks(markup_or_blocks)]
    else:
        blocks = list(markup_or_blocks)

    shelves: List[Tuple[Optional[str], List[list]]] = []

    def visit(node: Any, ancestors: List[list]) -> bool:
        """Emit the DEEPEST shelves under `node`; say whether any was found.

        Depth-first and children-first on purpose. A category page's outermost
        container also has two record-bearing children, so a shallowest-match
        rule returns one shelf holding the whole page — measured: 1 shelf of
        72 tiles where the page has 7 shelves.
        """
        if not isinstance(node, list) or is_app_record(node):
            return False
        found = False
        for child in node:
            if isinstance(child, list) and not is_app_record(child):
                if visit(child, ancestors + [node]):
                    found = True
        if found:
            return True
        bearing = [c for c in node if isinstance(c, list) and _records_under(c)]
        if len(bearing) >= 2:
            records = _records_under(node)
            if records:
                shelves.append((_shelf_title(ancestors + [node], records), records))
                return True
        return False

    for block in blocks:
        visit(block, [])
    return shelves


# Strings that sit near a shelf and are not its name: URLs, store paths, and
# the opaque tokens Play attaches to everything.
_NOT_A_SHELF_TITLE = re.compile(
    r"^(?:https?://|/store/|[A-Za-z0-9%_+=/\-]{24,}$)")


def _shelf_title(ancestors: List[list], records: List[list]) -> Optional[str]:
    """The heading Play prints above a shelf, or None.

    Taken as the last string in the shelf's own envelope that is not text
    belonging to one of its tiles. Searching outward from the item list rather
    than indexing a slot, because the header envelope's width differs between
    shelf kinds on one page.
    """
    tile_text = set()
    for rec in records[:6]:
        for key in ("title", "developer", "genre"):
            value = _text(_slot(rec, TILE[key]))
            if value:
                tile_text.add(value)

    for parent in reversed(ancestors[-3:]):
        best = None
        for branch in parent:
            if isinstance(branch, list) and _records_under(branch):
                continue          # the item list itself, not its heading
            for sub in _walk(branch if isinstance(branch, list) else []):
                for value in sub:
                    if (isinstance(value, str) and 2 <= len(value) <= 60
                            and value not in tile_text
                            and not _NOT_A_SHELF_TITLE.match(value)
                            and not _PACKAGE_RE.match(value)):
                        best = best or value
        if best:
            return best
    return None


def parse_tile(record: Sequence[Any], hl: str = "en", gl: str = "US",
               page: Optional[int] = None, position: Optional[int] = None,
               shelf: Optional[str] = None,
               shelf_rank: Optional[int] = None) -> Optional[App]:
    """One grid tile -> one row, or None if it carries no package id.

    Returning None rather than a row with a null `sku` is deliberate: an id is
    what makes a row joinable, and a row nobody can join is one a consumer
    cannot use and a diff will report as churn forever.
    """
    package = _text(_slot(record, TILE["package"]))
    if not package or not _PACKAGE_RE.match(package):
        return None

    rating, rating_raw = parse_rating(_slot(record, TILE["rating"]))
    installs, installs_min, installs_exact = parse_installs(_slot(record, TILE["installs"]))
    price, currency, in_stock = parse_offer(_slot(record, TILE["offer"]))
    iap_node = _slot(record, TILE["iap"])
    iap_range = _text(iap_node)

    content = _slot(record, TILE["content_rating"])
    description = _slot(record, TILE["description"])
    screenshots = _slot(record, TILE["screenshots"])

    return App(
        source=SOURCE_DEFAULT,
        url=app_url(package, hl, gl),
        sku=package,
        title=_text(_slot(record, TILE["title"])),
        brand=_text(_slot(record, TILE["developer"])),
        price=price,
        currency=currency,
        rating=rating,
        in_stock=in_stock,
        image_url=_first_url(_slot(record, TILE["icon"])),
        category=_text(_slot(record, TILE["genre"])),
        price_source="listing",
        page=page,
        position=position,
        gl=gl,
        hl=hl,
        installs=installs,
        installs_min=installs_min,
        installs_exact=installs_exact,
        content_rating=_text(content) if content else None,
        iap_price_range=iap_range,
        offers_iap=bool(iap_range) if iap_range is not None else None,
        rating_raw=rating_raw,
        description=_strip_html(description[1] if isinstance(description, list)
                                and len(description) > 1
                                and isinstance(description[1], str) else _text(description)),
        screenshot_count=len(screenshots) if isinstance(screenshots, list) else None,
        shelf=shelf,
        shelf_rank=shelf_rank,
    )


def parse_grid(markup: str, hl: str = "en", gl: str = "US",
               page: Optional[int] = None) -> List[App]:
    """A category, search, developer or shelf page -> rows.

    Rows come out in document order and deduped by package id, with the shelf
    each one was first seen in recorded. `position` counts the rows this
    parser EMITS, not the slots the payload holds — a sibling repo in this
    family learned that the hard way when a payload's sponsored entries
    shifted every position between two engines fetching seconds apart.
    """
    rows: List[App] = []
    seen = set()
    for shelf_title, records in find_shelves(markup):
        for rank, record in enumerate(records, start=1):
            package = _text(_slot(record, TILE["package"]))
            if not package or package in seen:
                continue
            row = parse_tile(record, hl=hl, gl=gl, page=page,
                             position=len(rows) + 1, shelf=shelf_title,
                             shelf_rank=rank)
            if row is None:
                continue
            seen.add(package)
            rows.append(row)

    # A page whose shelves were not recognised still has tiles on it, and a
    # shelf-shaped reading failing must not cost the run the grid. This is the
    # fallback path, and it is the one that runs on a search page, whose
    # results are one flat list rather than a set of named shelves.
    if not rows:
        for record in find_app_records(markup):
            row = parse_tile(record, hl=hl, gl=gl, page=page,
                             position=len(rows) + 1)
            if row is not None:
                rows.append(row)
    return rows


# --------------------------------------------------------------------------
# The app page -> App
# --------------------------------------------------------------------------

# Where each column sits inside an app page's own record. Measured on two
# captures (a free app and one with in-app purchases) and identical in both.
#
# A slot map, with the same caveat as `TILE`: `_slot()` tolerates a short
# record and every read below is independent, so a renumbered slot costs one
# column rather than the page. `find_detail_body()` additionally refuses a
# body whose slot 77 does not hold the package id the URL asked for, so a
# wholesale renumbering is detected rather than silently mis-read.
DETAIL = {
    "title": 0, "content_rating": 9, "released": 10, "installs": 13,
    "iap": 19, "developer": 37, "rating": 51, "offer": 57, "chart": 58,
    "developer_link": 68, "developer_contact": 69, "description": 72,
    "package": 77, "screenshots": 78, "genre": 79, "icon": 95,
    "privacy": 99, "version": 140, "whats_new": 144, "updated": 145,
}

_DETAIL_MIN_SLOTS = 80


def find_detail_body(markup: str, package_id: Optional[str] = None) -> Optional[list]:
    """The app page's own record for the app the page is about.

    Found by shape and then CONFIRMED by content: a list of at least eighty
    slots whose title slot holds a string and whose package slot holds the id
    the URL named. The confirmation is the point — an app page also carries
    six tiles for similar apps, and a reader that took the first app-shaped
    thing on the page would describe a neighbour.
    """
    for _key, data in iter_data_blocks(markup):
        for node in _walk(data):
            if len(node) < _DETAIL_MIN_SLOTS:
                continue
            found = _text(_slot(node, DETAIL["package"]))
            if not found or not _PACKAGE_RE.match(found):
                continue
            if package_id and found != package_id:
                continue
            if _text(_slot(node, DETAIL["title"])):
                return node
    return None


def parse_jsonld(markup: str) -> Optional[dict]:
    """The app page's single `SoftwareApplication` block, or None.

    There is exactly one on an app page and none on any grid. It is a
    cross-check rather than a source: it disagrees with the page's own
    payload about the same app's rating in the same fetch.
    """
    for m in re.finditer(
            r'<script type="application/ld\+json"[^>]*>(.*?)</script>',
            markup or "", re.S):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "SoftwareApplication":
            return data
    return None


def parse_detail(markup: str, package_id: Optional[str] = None,
                 hl: str = "en", gl: str = "US",
                 page: Optional[int] = None,
                 position: Optional[int] = None) -> Optional[App]:
    """One `/store/apps/details` page -> one row with the detail columns filled."""
    package = package_id
    body = find_detail_body(markup, package)
    if body is None:
        return None
    package = _text(_slot(body, DETAIL["package"])) or package
    if not package:
        return None

    rating, rating_raw = parse_rating(_slot(_slot(body, DETAIL["rating"]), 0))
    hist = _histogram(_slot(body, DETAIL["rating"]))
    ratings_total, reviews_total = _rating_counts(_slot(body, DETAIL["rating"]))
    installs, installs_min, installs_exact = parse_installs(_slot(body, DETAIL["installs"]))
    price, currency, in_stock = parse_offer(_slot(body, DETAIL["offer"]))
    chart_name, chart_rank = parse_chart(_slot(body, DETAIL["chart"]))
    genre_display, genre_id = _genre(_slot(body, DETAIL["genre"]))
    iap_range = _text(_slot(body, DETAIL["iap"]))
    website, email = _developer_contact(_slot(body, DETAIL["developer_contact"]))
    screenshots = _slot(body, DETAIL["screenshots"])

    row = App(
        source=SOURCE_DEFAULT,
        url=app_url(package, hl, gl),
        sku=package,
        title=_text(_slot(body, DETAIL["title"])),
        brand=_developer_name(body),
        price=price,
        currency=currency,
        rating=rating,
        review_count=ratings_total,
        in_stock=in_stock,
        image_url=_first_url(_slot(body, DETAIL["icon"])),
        category=genre_display,
        price_source="detail",
        page=page,
        position=position,
        gl=gl,
        hl=hl,
        developer_id=_developer_id(_slot(body, DETAIL["developer_link"])),
        genre_id=genre_id,
        installs=installs,
        installs_min=installs_min,
        installs_exact=installs_exact,
        content_rating=_text(_slot(body, DETAIL["content_rating"])),
        iap_price_range=iap_range,
        offers_iap=bool(iap_range) if iap_range is not None else None,
        rating_raw=rating_raw,
        review_text_count=reviews_total,
        histogram_1=hist[0], histogram_2=hist[1], histogram_3=hist[2],
        histogram_4=hist[3], histogram_5=hist[4],
        version=_version(_slot(body, DETAIL["version"])),
        updated_at=epoch_to_iso(_slot(body, DETAIL["updated"])),
        description=_strip_html(_description(_slot(body, DETAIL["description"]))),
        developer_email=email,
        developer_website=website,
        privacy_policy_url=_first_url(_slot(body, DETAIL["privacy"]), "http"),
        screenshot_count=len(screenshots) if isinstance(screenshots, list) else None,
        chart_name=chart_name,
        chart_rank=chart_rank,
    )

    # The JSON-LD is read last and only to CONFIRM. Where it disagrees the
    # row is left alone and the disagreement is reported by the caller: this
    # family's rule is that overwriting a correct row is worse than leaving
    # one uncorrected, and here the two sources are known to disagree in the
    # last decimal places of a float nobody should be reading anyway.
    ld = parse_jsonld(markup)
    if ld:
        row.price_source = "detail+jsonld"
    return row


def jsonld_disagreements(row: App, markup: str) -> List[str]:
    """Where the app page's two sources describe different things.

    Only the fields worth arguing about: the identity and the headline
    numbers. A float difference in `rating_raw` is expected and is not
    reported — see `parse_rating`.
    """
    ld = parse_jsonld(markup)
    if not ld or row is None:
        return []
    out = []
    ld_id = package_from_url(ld.get("url") or "")
    if ld_id and row.sku and ld_id != row.sku:
        out.append(f"sku: payload {row.sku}, JSON-LD {ld_id}")
    if ld.get("name") and row.title and ld["name"] != row.title:
        out.append(f"title: payload {row.title!r}, JSON-LD {ld['name']!r}")
    agg = ld.get("aggregateRating") or {}
    try:
        ld_count = int(agg.get("ratingCount"))
    except (TypeError, ValueError):
        ld_count = None
    if ld_count is not None and row.review_count is not None and ld_count != row.review_count:
        out.append(f"review_count: payload {row.review_count}, JSON-LD {ld_count}")
    return out


def _histogram(node: Any) -> List[Optional[int]]:
    """`[null, ["10,894,983", 10894983], ...]` -> five ints, one star each."""
    buckets = _slot(node, 1)
    out: List[Optional[int]] = [None] * 5
    if not isinstance(buckets, list):
        return out
    pairs = [b for b in buckets if isinstance(b, list) and len(b) >= 2
             and isinstance(b[1], int)]
    for i, pair in enumerate(pairs[:5]):
        out[i] = pair[1]
    return out


def _rating_counts(node: Any) -> Tuple[Optional[int], Optional[int]]:
    """Ratings and written reviews, which Play states separately.

    244,140,056 against 1,952,258 on one app measured 2026-09-22 — two orders
    of magnitude apart, and a reader that conflates them reports a review
    count a hundred times too large.
    """
    def count(slot: Any) -> Optional[int]:
        if isinstance(slot, list) and len(slot) >= 2 and isinstance(slot[1], int):
            return slot[1]
        return None
    return count(_slot(node, 2)), count(_slot(node, 3))


def _genre(node: Any) -> Tuple[Optional[str], Optional[str]]:
    """`[[["Arcade", {...}, "GAME_ARCADE"]]]` -> ("Arcade", "GAME_ARCADE").

    The display name is locale text — a German page says "Kommunikation" —
    and the key is not, so a cross-market join uses the key.
    """
    for sub in _walk(node if isinstance(node, list) else []):
        if (len(sub) >= 3 and isinstance(sub[0], str) and isinstance(sub[2], str)
                and sub[2].isupper()):
            return sub[0] or None, sub[2] or None
    return None, None


def _developer_name(body: Sequence[Any]) -> Optional[str]:
    """The developer, read from the link block rather than from slot 37.

    Slot 37 is the SELLER OF RECORD and it is locale-dependent: on the German
    page for an app whose developer is "WhatsApp LLC" it holds
    "Google Commerce Ltd", because that is who sells it in the EU. Measured
    2026-09-22 on three captures — slot 37 differs between the US and German
    pages of one app, and the developer link block agrees with itself in all
    three. Reading slot 37 would have put Google's name in the `brand` column
    of every European row, which is the kind of defect only a second locale
    finds.

    Slot 37 is still the fallback, for a page whose link block is missing.
    """
    linked = _text(_slot(_slot(body, DETAIL["developer_link"]), 0))
    return linked or _text(_slot(body, DETAIL["developer"]))


def _developer_id(node: Any) -> Optional[str]:
    """The id `--mode developer` takes, from the app page's developer link."""
    if isinstance(node, list):
        for v in node:
            if isinstance(v, str) and _DEV_NUMERIC_RE.match(v):
                return v
    link = _first_url(node, "/store/apps/dev")
    if link:
        m = _ID_IN_URL_RE.search(link)
        if m:
            return urllib.parse.unquote(m.group(1)).replace("+", " ")
    return None


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def _developer_contact(node: Any) -> Tuple[Optional[str], Optional[str]]:
    """(website, email) from the app page's developer block."""
    email = None
    for sub in _walk(node if isinstance(node, list) else []):
        for v in sub:
            if isinstance(v, str) and _EMAIL_RE.match(v):
                email = v
                break
        if email:
            break
    return _first_url(node, "http"), email


def _description(node: Any) -> Optional[str]:
    """The long description, which Play wraps as `[[null, "<html>"]]`."""
    for sub in _walk(node if isinstance(node, list) else []):
        for v in sub:
            if isinstance(v, str) and len(v) > 40:
                return v
    return None


_VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z._\-]{0,30}$")


def _version(node: Any) -> Optional[str]:
    """The app version, where Play states one.

    Absent on plenty of apps — Play prints "Varies with device" instead, and
    that phrase is not a version, so the column stays null rather than
    carrying it.
    """
    for sub in _walk(node if isinstance(node, list) else []):
        for v in sub:
            if isinstance(v, str) and _VERSION_RE.match(v) and any(c.isdigit() for c in v):
                return v
    return None


# --------------------------------------------------------------------------
# Reviews: one structure, two routes
# --------------------------------------------------------------------------

# Play's own RPC for "more reviews". The id is opaque and is the one the
# store's front end calls; it answers a plain POST from a datacentre address
# with no cookies and no key.
REVIEWS_RPC = "UsvDTd"
BATCHEXECUTE_PATH = "/_/PlayStoreUi/data/batchexecute"

# How many reviews to ask for in one call.
#
# This is a DEFAULT, not a cap, and the difference was worth measuring.
# Play's front end asks for 40 and that is where this number started; asked
# for more, the endpoint simply returns more. Measured 2026-09-22 on
# com.whatsapp: 100 -> 100, 200 -> 200, 500 -> 500, 1000 -> 1000,
# 2000 -> 2000 (2.07 MB), with a continuation token on every one of them. No
# ceiling was found. 200 is the default because it is twenty times fewer
# round trips than the front end makes while keeping a response small enough
# to fail cheaply; `--reviews-per-call` raises it.
REVIEWS_PER_CALL = 200

# Play's sort key. There is exactly ONE, and that is a measurement rather
# than a design decision: the store's front end distinguishes "most
# relevant" (1), "newest" (2) and "rating" (3), and in THIS request shape
# only 2 returns anything at all. Measured 2026-09-22 on two apps, 40
# requested each: sort 1 -> 0 reviews and no token, sort 2 -> 40 and a token,
# sort 3 -> 0 and no token.
#
# So this repo does not implement the other two orderings. That is a TODO
# about this request shape, not a limitation of the store — the front end
# plainly gets them — and the flag offers only what was measured to work,
# because a `--sort` with three values of which two silently return an empty
# run is worse than a flag with one.
REVIEW_SORTS = {"newest": 2}
DEFAULT_REVIEW_SORT = "newest"

# Where each column sits in a review record. Identical in both routes,
# because they are the same structure.
REVIEW = {
    "id": 0, "author": 1, "score": 2, "text": 4, "posted": 5,
    "thumbs_up": 6, "reply": 7, "author_profile": 9, "app_version": 10,
}


def batchexecute_url(hl: str = "en", gl: str = "US") -> str:
    q = urllib.parse.urlencode({"hl": hl, "gl": gl})
    return f"{BASE}{BATCHEXECUTE_PATH}?{q}"


def build_reviews_request(package_id: str, count: int = REVIEWS_PER_CALL,
                          token: Optional[str] = None,
                          sort: str = DEFAULT_REVIEW_SORT) -> Dict[str, str]:
    """The form body for one page of reviews.

    Returned as a dict for the caller to urlencode, so no engine has to know
    the wire format and every engine sends the identical thing.

    There is deliberately no score filter. The slot for one exists in the
    payload and setting it does NOTHING: measured 2026-09-22, asking for
    score 1 and score 5 each returned forty reviews spanning all five scores.
    Shipping a `--min-score`-style filter here would be shipping a flag that
    reports success and changes no row.
    """
    sort_key = REVIEW_SORTS.get(sort, REVIEW_SORTS[DEFAULT_REVIEW_SORT])
    pagination = [count, None, token]
    inner = [None, None,
             [sort_key, None, pagination, None, []],
             [package_id, 7]]
    payload = [[[REVIEWS_RPC, json.dumps(inner, separators=(",", ":")),
                 None, "generic"]]]
    return {"f.req": json.dumps(payload, separators=(",", ":"))}


def parse_batchexecute(text: str) -> Optional[Any]:
    """The inner payload of a `batchexecute` response, or None.

    The wire format is `)]}'` then length-prefixed chunks of JSON, and the
    envelope is `[["wrb.fr", "<rpc>", "<json string>", ...]]`. Parsed
    tolerantly rather than by the length prefixes: Play ships trailing
    bookkeeping chunks that are not reviews, and a strict reader that trips
    over one loses the forty that came first.
    """
    if not text:
        return None
    body = text.split("\n", 1)[-1]
    start = body.find('[["wrb.fr"')
    if start < 0:
        return None
    try:
        envelope, _ = json.JSONDecoder().raw_decode(body[start:])
    except ValueError:
        return None
    for entry in envelope:
        if (isinstance(entry, list) and len(entry) >= 3
                and entry[0] == "wrb.fr" and isinstance(entry[2], str)):
            try:
                return json.loads(entry[2])
            except ValueError:
                return None
    return None


def reviews_from_payload(payload: Any) -> Tuple[List[list], Optional[str]]:
    """`(records, next_token)` out of a reviews payload.

    The token is what makes this mode unbounded: measured 2026-09-22, page 2
    shared none of page 1's forty ids and carried a token of its own.
    """
    if not isinstance(payload, list) or not payload:
        return [], None
    records = [r for r in payload[0] if isinstance(r, list) and len(r) >= 11] \
        if isinstance(payload[0], list) else []
    token = None
    if len(payload) > 1 and isinstance(payload[1], list):
        for v in payload[1]:
            if isinstance(v, str) and len(v) > 20:
                token = v
                break
    return records, token


def embedded_reviews(markup: str) -> List[list]:
    """The first page of reviews, which the app page carries in its own payload.

    So `--mode app` gets reviews without a second request, and `--mode
    reviews` starts from the same structure it will then page through. One
    parser for both is what keeps the two routes from drifting.
    """
    out: List[list] = []
    for _key, data in iter_data_blocks(markup):
        for node in _walk(data):
            if all(is_review_record(c) for c in node) and len(node) >= 2:
                out.extend(node)
                if out:
                    return out
    return out


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def is_review_record(node: Any) -> bool:
    """The repeating record Play uses for one review.

    Anchored on the uuid in slot 0 and an integer star score in slot 2. The
    uuid is what keeps this from matching the many other 17-slot lists on a
    page.
    """
    return (isinstance(node, list) and len(node) >= 11
            and isinstance(node[0], str) and bool(_UUID_RE.match(node[0]))
            and isinstance(node[2], int) and 1 <= node[2] <= 5)


def parse_review_record(record: Sequence[Any], package_id: str,
                        hl: str = "en", gl: str = "US",
                        page: Optional[int] = None,
                        position: Optional[int] = None,
                        include_author: bool = True) -> Optional[Review]:
    """One review record -> one row.

    `include_author` is what `--no-authors` turns off. Play publishes a
    reviewer's display name, avatar URL and a stable numeric profile id, and
    republishing those is a separate act from the store showing them on its
    own page. The rest of the row — the score, the text, the date, the
    version — is what the mode is for and is unaffected.
    """
    review_id = _slot(record, REVIEW["id"])
    if not isinstance(review_id, str) or not _UUID_RE.match(review_id):
        return None

    author_name = author_id = author_image = None
    if include_author:
        author = _slot(record, REVIEW["author"])
        author_name = _text(author)
        author_image = _first_url(author, "http")
        profile = _slot(record, REVIEW["author_profile"])
        if isinstance(profile, list) and profile and isinstance(profile[0], str):
            author_id = profile[0]

    reply_text, reply_at = _developer_reply(_slot(record, REVIEW["reply"]))

    return Review(
        source=SOURCE_DEFAULT,
        url=app_url(package_id, hl, gl),
        sku=package_id,
        review_id=review_id,
        score=_slot(record, REVIEW["score"]),
        text=_strip_html(_slot(record, REVIEW["text"])),
        posted_at=epoch_to_iso(_slot(record, REVIEW["posted"])),
        thumbs_up=_slot(record, REVIEW["thumbs_up"]) if isinstance(
            _slot(record, REVIEW["thumbs_up"]), int) else None,
        app_version=_slot(record, REVIEW["app_version"]) if isinstance(
            _slot(record, REVIEW["app_version"]), str) else None,
        developer_reply=reply_text,
        developer_replied_at=reply_at,
        author_name=author_name,
        author_id=author_id,
        author_image=author_image,
        gl=gl,
        hl=hl,
        page=page,
        position=position,
    )


def _developer_reply(node: Any) -> Tuple[Optional[str], Optional[str]]:
    """The developer's answer and when it was posted, where there is one.

    Read by shape rather than by index, and NOT yet seen taking a non-null
    value: 0 of the 40 reviews in the capture carry a reply. So this column
    is less proven than the rest of the row — a null here may be "no reply"
    or may be "this reader is looking in the wrong slot", and the smoke suite
    pins the current behaviour rather than asserting a value nobody has
    observed.
    """
    if not isinstance(node, list) or not node:
        return None, None
    text = None
    for sub in _walk(node):
        for v in sub:
            if isinstance(v, str) and len(v) > 1 and not _UUID_RE.match(v):
                text = v
                break
        if text:
            break
    return _strip_html(text), epoch_to_iso(node)


# --------------------------------------------------------------------------
# What did we actually get?
# --------------------------------------------------------------------------

PAGE_STATES = ("content", "not_found", "blocked", "empty", "unknown")


def detect_page_state(markup: str, status: Optional[int] = None,
                      url: str = "", expect: str = "grid") -> Tuple[str, str]:
    """`(state, reason)` for one fetched page.

    The signals are ordered by how much they PROVE, not by how cheap they
    are. A sibling repo in this family shipped the opposite order and
    reported exit 3 — blocked — for a minimal but perfectly real page, which
    sent its reader hunting for a proxy problem that did not exist.

    1. **We parsed the thing we came for.** Nothing an interstitial can do
       produces an app record or a detail body, so this outranks every
       heuristic and is checked first even though it is the most expensive.
    2. **HTTP 404.** Play answers a package id that does not exist with a
       clean 404 and 1.6 KB — measured — so this needs no guessing at all.
    3. **A refusal marker.**
    4. **The page is not built out of Play's own images.** The structural
       signal, and a threshold rather than a fact, so it goes last among the
       negative ones. Every served capture references the image host 252 to
       5,712 times; the store's own 404 references it zero times.
    5. Otherwise the page was served and holds nothing we recognise, which is
       `empty` — a real answer, not a failure — unless nothing about it looks
       like Play at all, which is `unknown` and worth one more attempt.
    """
    markup = markup or ""

    if expect == "detail":
        if find_detail_body(markup, package_from_url(url)) is not None:
            return "content", "detail_payload"
    elif expect == "reviews":
        if embedded_reviews(markup):
            return "content", "reviews_payload"
    if find_app_records(markup):
        return "content", "app_records"

    if status == 404:
        return "not_found", "http_404"

    marker = challenge_marker(markup)
    if marker:
        return "blocked", f"challenge_marker:{marker}"

    if status in (403, 429):
        return "blocked", f"http_{status}"
    if status == 503:
        return "unknown", "http_503"

    assets = asset_reference_count(markup)
    if assets < MIN_ASSET_REFERENCES:
        # Covers the cases no marker list can: a proxy error page, Chromium's
        # own network-error page (which carries the SITE's hostname in its
        # title and would pass any title check), and an interstitial nobody
        # has seen yet.
        return "blocked", f"asset_references={assets}"

    if not markup.strip():
        return "unknown", "empty_response"

    return "empty", f"served_no_records assets={assets}"


# --------------------------------------------------------------------------
# The names the family's shared modules call
# --------------------------------------------------------------------------
#
# `scraper_api_client.py` is family core and imports these by name. They are
# thin wrappers rather than a second implementation, so the Scraper API path
# and the browser engines cannot come to different conclusions about one page
# — which is the whole reason the shared client exists.

def detect_bot_challenge(markup: str) -> Optional[str]:
    """The refusal marker on this page, or None. Family-named alias."""
    return challenge_marker(markup)


def parse_products(markup: str, url: str = "", category: Optional[str] = None,
                   hl: str = "en", gl: str = "US",
                   page: Optional[int] = None) -> List[App]:
    """Rows out of one fetched page, whatever route it came from.

    `category` is accepted because the family's signature has it and is used
    as a fallback for a grid whose own genre text is missing — never to
    OVERWRITE what the page said about itself.
    """
    mode = mode_for_url(url) if url else None
    if mode == "app":
        row = parse_detail(markup, package_from_url(url), hl=hl, gl=gl, page=page)
        rows = [row] if row is not None else []
    else:
        rows = parse_grid(markup, hl=hl, gl=gl, page=page)
    if category:
        for row in rows:
            if not row.category:
                row.category = category
    return rows
