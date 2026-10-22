#!/usr/bin/env python3
"""
smoke_test.py
-------------
One file of plain functions with inline fixtures. No pytest, no conftest, no
fixtures directory. `tests/test_smoke.py` wraps it as a single pytest test so
`pytest` works as an entry point without a second copy of the checks.

It must pass with NO engine library installed at all: every
`import playwright_scraper` / `puppeteer_scraper` / `selenium_scraper` is
guarded and the skip is recorded. CI's `engine-smoke` job installs each engine
in its own virtualenv and fails if the matching group reports a skip —
"skipped, engine absent" reads identically to a real import error otherwise.

The fixtures are cut from real captures in `captures/`, which is also what
makes the assertions worth anything: a column can be 100% populated and
entirely wrong, so the checks pin VALUES.

Reviewer names, profile ids and avatar URLs in the review fixtures are
REPLACED with obvious placeholders. Play publishes real people's names beside
their words, and republishing those in a public repo is a separate act from
the store showing them on its own page. The checks need the STRUCTURE of a
review, not the person, and `check_fixtures_carry_no_real_reviewers` guards
the next capture by PATTERN rather than by these literals.
"""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout, redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import output_writer
import page_flow
import product_parser as parser
from output_writer import App, Product, Review

FAILURES = []
SKIPS = []


def check(label, condition, detail=""):
    if condition:
        return True
    FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"FAILED: {label}{': ' + detail if detail else ''}")
    return False


def skip(group, why):
    SKIPS.append(f"{group}: {why}")
    print(f"SKIPPED: {group} — {why}")


def capture(name):
    path = os.path.join(HERE, "captures", name)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


# ==========================================================================
# The row contract
# ==========================================================================

# The family's first eighteen columns, byte-identical and in order in every
# repo in this family. A consumer written against a sibling reads this prefix
# unchanged, so reordering or renaming one of these is a breaking change to
# every repo at once rather than a local decision.
FAMILY_PREFIX = (
    "source", "scraped_at", "url", "sku", "title", "brand", "price",
    "currency", "original_price", "discount_pct", "rating", "review_count",
    "in_stock", "image_url", "category", "price_source", "page", "position",
)


def test_row_schema():
    ok = True
    from dataclasses import fields
    names = [f.name for f in fields(App)]
    ok &= check("[schema] family prefix intact and in order",
                tuple(names[:18]) == FAMILY_PREFIX,
                f"got {names[:18]}")
    ok &= check("[schema] Product is an alias for App, not a second class",
                Product is App)
    ok &= check("[schema] Review dedupes on review_id, not sku",
                "review_id" in [f.name for f in fields(Review)])
    ok &= check("[schema] reviews is NOT in UNIQUE_BY_SKU_MODES",
                "reviews" not in output_writer.UNIQUE_BY_SKU_MODES,
                "many review rows share a sku; deduping on it would keep one "
                "review per app and throw the rest away")
    ok &= check("[schema] every mode maps to a row class",
                set(output_writer.ROW_CLASS_BY_MODE) == set(parser.MODES),
                f"{sorted(output_writer.ROW_CLASS_BY_MODE)} vs {sorted(parser.MODES)}")
    ok &= check("[schema] reviews maps to Review",
                output_writer.ROW_CLASS_BY_MODE["reviews"] is Review)
    # A column that is null on every row of every run should not exist. These
    # are the ones this repo measured OUT rather than in, and the comment in
    # output_writer records the measurement; the check stops one being added
    # back without one.
    ok &= check("[schema] no contains_ads column",
                "contains_ads" not in names,
                "measured: 'Contains ads' appears 0 times and 'In-app "
                "purchases' exactly once in BOTH app captures, including one "
                "with no IAP — the rendered labels are template text")
    return ok


# ==========================================================================
# Values, on real fixtures
# ==========================================================================

def test_detail_values():
    """Assert VALUES, not coverage. A column can be 100% populated and wrong."""
    ok = True
    html = capture("app_free_us.html")
    if html is None:
        skip("detail-values", "captures/app_free_us.html absent")
        return True

    row = parser.parse_detail(html, "com.whatsapp", hl="en", gl="US")
    ok &= check("[detail] a row came back", row is not None)
    if row is None:
        return False

    ok &= check("[detail] sku is the package id", row.sku == "com.whatsapp", row.sku)
    ok &= check("[detail] title", row.title == "WhatsApp Messenger", row.title)
    # The developer, NOT the seller of record. Slot 37 holds "Google Commerce
    # Ltd" on the German page for this same app.
    ok &= check("[detail] brand is the developer", row.brand == "WhatsApp LLC", row.brand)
    ok &= check("[detail] a free app's price is 0.0, not None", row.price == 0.0, str(row.price))
    ok &= check("[detail] currency", row.currency == "USD", row.currency)
    # The DISPLAYED rating, not the float32 beside it.
    ok &= check("[detail] rating is the displayed figure", row.rating == 4.6, str(row.rating))
    ok &= check("[detail] rating_raw keeps the payload's own number",
                row.rating_raw is not None and abs(row.rating_raw - 4.616471) < 1e-5,
                str(row.rating_raw))
    # Ratings and written reviews are different numbers, two orders of
    # magnitude apart. Conflating them is the defect this pins.
    ok &= check("[detail] review_count is the RATING count",
                row.review_count == 244140056, str(row.review_count))
    ok &= check("[detail] review_text_count is the WRITTEN count",
                row.review_text_count == 1952258, str(row.review_text_count))
    ok &= check("[detail] the two counts are not the same field",
                row.review_count != row.review_text_count)
    ok &= check("[detail] installs display", row.installs == "10,000,000,000+", row.installs)
    ok &= check("[detail] installs floor", row.installs_min == 10000000000, str(row.installs_min))
    # Play's exact number, published on the app route and nowhere else.
    ok &= check("[detail] installs_exact", row.installs_exact == 12322703000,
                str(row.installs_exact))
    ok &= check("[detail] the exact count exceeds the bucket floor",
                row.installs_exact > row.installs_min)
    ok &= check("[detail] genre_id is the locale-independent key",
                row.genre_id == "COMMUNICATION", row.genre_id)
    ok &= check("[detail] histogram, five buckets",
                [row.histogram_1, row.histogram_2, row.histogram_3,
                 row.histogram_4, row.histogram_5] ==
                [10894983, 3453600, 8922598, 21848798, 199020024])
    ok &= check("[detail] chart standing is Play's own claim",
                (row.chart_name, row.chart_rank) == ("top free communication", 1),
                f"{row.chart_name!r} {row.chart_rank}")
    ok &= check("[detail] developer_email", row.developer_email == "android@support.whatsapp.com",
                row.developer_email)
    ok &= check("[detail] developer_id", row.developer_id == "5191370068110375942",
                row.developer_id)
    ok &= check("[detail] price_source says both sources were read",
                row.price_source == "detail+jsonld", row.price_source)
    ok &= check("[detail] market is a column", (row.gl, row.hl) == ("US", "en"))
    return ok


def test_paid_app_values():
    ok = True
    html = capture("app_iap_us.html")
    if html is None:
        skip("paid-values", "captures/app_iap_us.html absent")
        return True
    row = parser.parse_detail(html, "com.mojang.minecraftpe", hl="en", gl="US")
    ok &= check("[paid] a row came back", row is not None)
    if row is None:
        return False
    # Micros, not the display string: 6990000 -> 6.99.
    ok &= check("[paid] price from micros", row.price == 6.99, str(row.price))
    ok &= check("[paid] version", row.version == "1.26.51.1", row.version)
    ok &= check("[paid] iap range", row.iap_price_range == "$0.99 - $49.99 per item",
                row.iap_price_range)
    ok &= check("[paid] offers_iap follows the range", row.offers_iap is True)
    ok &= check("[paid] genre_id", row.genre_id == "GAME_ARCADE", row.genre_id)
    # The app page wraps its screenshots one level deeper than a grid tile;
    # counting the wrapper reported 1 on every app row up to 0.2.0.
    ok &= check("[paid] screenshot_count", row.screenshot_count == 8,
                str(row.screenshot_count))
    return ok


def test_the_market_is_a_property_of_the_row():
    """The finding this whole repo is positioned on, pinned as a check.

    Play computes a rating PER COUNTRY. Two captures of one app, taken
    minutes apart from one address with only `gl`/`hl` changed, must disagree
    about the rating and agree about the identity — and if they ever stop
    disagreeing, the `gl` column has quietly stopped meaning anything.
    """
    ok = True
    us, de = capture("app_free_us.html"), capture("app_free_de.html")
    if us is None or de is None:
        skip("market", "both locale captures needed")
        return True
    row_us = parser.parse_detail(us, "com.whatsapp", hl="en", gl="US")
    row_de = parser.parse_detail(de, "com.whatsapp", hl="de", gl="DE")
    ok &= check("[market] same app", row_us.sku == row_de.sku == "com.whatsapp")
    ok &= check("[market] the rating differs between markets",
                row_us.rating != row_de.rating,
                f"US {row_us.rating} DE {row_de.rating}")
    ok &= check("[market] the currency follows the market",
                (row_us.currency, row_de.currency) == ("USD", "EUR"),
                f"{row_us.currency} {row_de.currency}")
    ok &= check("[market] the RATING COUNT is global, not per-market",
                row_us.review_count == row_de.review_count,
                f"{row_us.review_count} vs {row_de.review_count}")
    # The bug the second locale found: slot 37 is the seller of record and
    # says "Google Commerce Ltd" on the German page.
    ok &= check("[market] brand is the developer in BOTH markets",
                row_us.brand == row_de.brand == "WhatsApp LLC",
                f"{row_us.brand!r} vs {row_de.brand!r}")
    ok &= check("[market] the locale-independent genre key agrees",
                row_us.genre_id == row_de.genre_id == "COMMUNICATION")
    return ok


def test_grid_values():
    ok = True
    html = capture("search_vpn_us.html")
    if html is None:
        skip("grid-values", "captures/search_vpn_us.html absent")
        return True
    rows = parser.parse_grid(html, hl="en", gl="US", page=1)
    ok &= check("[grid] the whole grid parsed", len(rows) == 30, str(len(rows)))
    ok &= check("[grid] every row has a package id",
                all(r.sku and "." in r.sku for r in rows))
    ok &= check("[grid] ids are unique", len({r.sku for r in rows}) == len(rows))
    # position counts the rows the parser EMITS, not the slots the payload
    # holds. A sibling repo had two engines number the same 45 products 1-45
    # and 8-52 because it counted payload slots.
    ok &= check("[grid] position is 1..n over emitted rows",
                [r.position for r in rows] == list(range(1, len(rows) + 1)))
    first = rows[0]
    ok &= check("[grid] a listing row says which route it came from",
                first.price_source == "listing", first.price_source)
    ok &= check("[grid] a listing row carries no exact install count",
                all(r.installs_exact is None for r in rows),
                "measured: 0 of 330 listing tiles publish one")
    ok &= check("[grid] a listing row carries no histogram",
                all(r.histogram_5 is None for r in rows))
    # Pinned because the README said the opposite until a fresh clone was run
    # the way a stranger runs it, and the first row it printed had a null
    # here. A grid tile publishes the genre only as display text.
    ok &= check("[grid] a listing row carries no genre key",
                all(r.genre_id is None for r in rows),
                "the tile has no locale-independent key anywhere in it")
    ok &= check("[grid] but it does carry the displayed genre",
                all(r.category for r in rows))
    ok &= check("[grid] rating is populated on a US grid",
                sum(1 for r in rows if r.rating is not None) == len(rows))
    return ok


def test_shelves_are_named():
    """A category page is several grids, and a row must say which one it is in."""
    ok = True
    html = capture("cat_game_us.html")
    if html is None:
        skip("shelves", "captures/cat_game_us.html absent")
        return True
    shelves = parser.find_shelves(html)
    ok &= check("[shelf] the page splits into several shelves",
                len(shelves) >= 5, str(len(shelves)))
    named = [t for t, _ in shelves if t]
    ok &= check("[shelf] most shelves are named", len(named) >= len(shelves) - 2,
                f"{len(named)} of {len(shelves)}")
    ok &= check("[shelf] a shelf name is a heading, not a token",
                all(len(t) <= 60 and not re.match(r"^[A-Za-z0-9%_+=/-]{24,}$", t)
                    for t in named),
                str(named[:3]))
    rows = parser.parse_grid(html, page=1)
    ranked = [r for r in rows if r.shelf and r.shelf_rank]
    ok &= check("[shelf] rows carry their shelf and their place in it",
                len(ranked) >= len(rows) // 2, f"{len(ranked)} of {len(rows)}")
    ok &= check("[shelf] shelf_rank restarts per shelf",
                any(r.shelf_rank == 1 for r in ranked))
    return ok


def test_page_state_on_every_capture():
    """Every capture must classify the way it actually is."""
    ok = True
    cases = [
        ("cat_game_us.html", 200, "grid", "content"),
        ("cat_game_de.html", 200, "grid", "content"),
        ("search_vpn_jp.html", 200, "grid", "content"),
        ("dev_us.html", 200, "grid", "content"),
        ("app_free_us.html", 200, "detail", "content"),
        # A package id that does not exist is an ANSWER, not a block. Calling
        # it one would send a reader to buy a proxy for a typo.
        ("notfound.html", 404, "detail", "not_found"),
    ]
    for name, status, expect, want in cases:
        html = capture(name)
        if html is None:
            skip("page-state", f"captures/{name} absent")
            continue
        url = ("https://play.google.com/store/apps/details?id=com.whatsapp"
               if expect == "detail" else "")
        state, reason = parser.detect_page_state(html, status, url, expect)
        ok &= check(f"[state] {name}", state == want, f"got {state} ({reason})")

    # An empty body is not content, whatever else is true of it.
    state, _ = parser.detect_page_state("", None, "", "grid")
    ok &= check("[state] an empty response is not content", state != "content")
    # Chromium's own network-error page carries the SITE's hostname in its
    # title, so a title check calls it a real page. Only "was this built out
    # of the site's own assets?" answers correctly.
    fake = "<html><title>play.google.com</title><body>ERR_PROXY_CONNECTION_FAILED</body></html>"
    state, reason = parser.detect_page_state(fake, 200, "", "grid")
    ok &= check("[state] a browser error page wearing the site's title is blocked",
                state == "blocked", f"got {state} ({reason})")
    return ok


# Captures of Play's own error pages. Real responses, and the controls for
# the structural signal: neither is built out of the store's images.
_REFUSAL_CAPTURES = ("notfound.html", "bad_request_gl.html")


def test_markers_score_zero_on_good_pages():
    """A marker that matches a page you know is good is not a marker.

    Every candidate below was counted across every capture before any of them
    was allowed into the set. The reCAPTCHA spellings are counted too and are
    deliberately NOT in `BOT_CHALLENGE_MARKERS`: they score zero everywhere,
    and a marker that has never matched anything is dead code that looks
    load-bearing.
    """
    ok = True
    served = [n for n in os.listdir(os.path.join(HERE, "captures"))
              if n.endswith(".html") and n not in _REFUSAL_CAPTURES]
    if not served:
        skip("markers", "no captures")
        return True
    for name in sorted(served):
        html = capture(name)
        hit = parser.challenge_marker(html)
        ok &= check(f"[marker] no refusal marker on served {name}",
                    hit is None, f"matched {hit!r}")
        ok &= check(f"[marker] {name} is built out of Play's own images",
                    parser.asset_reference_count(html) >= parser.MIN_ASSET_REFERENCES)
    # The 404 is the control: it is a real Play response and carries NONE of
    # the image host, which is what makes the structural signal usable.
    for name in _REFUSAL_CAPTURES:
        page = capture(name)
        if page is not None:
            ok &= check(f"[marker] {name} carries no image-host references",
                        parser.asset_reference_count(page) == 0,
                        str(parser.asset_reference_count(page)))
            ok &= check(f"[marker] {name} trips no refusal marker",
                        parser.challenge_marker(page) is None,
                        "it is the store answering, not refusing")
    ok &= check("[marker] the set carries no reCAPTCHA spelling",
                not any("recaptcha" in m.lower() or "sitekey" in m.lower()
                        for m in parser.BOT_CHALLENGE_MARKERS),
                str(parser.BOT_CHALLENGE_MARKERS))
    return ok


def test_reviews_one_parser_two_routes():
    ok = True
    html = capture("app_free_us.html")
    raw = None
    path = os.path.join(HERE, "captures", "reviews_p1.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    if html is None or raw is None:
        skip("reviews", "app capture and reviews_p1.txt both needed")
        return True

    embedded = parser.embedded_reviews(html)
    ok &= check("[reviews] the app page carries its own first page",
                len(embedded) >= 10, str(len(embedded)))

    payload = parser.parse_batchexecute(raw)
    records, token = parser.reviews_from_payload(payload)
    ok &= check("[reviews] batchexecute parsed", len(records) == 40, str(len(records)))
    ok &= check("[reviews] and carries a continuation token", bool(token))

    # The SAME parser must read both, or the two routes drift.
    a = parser.parse_review_record(embedded[0], "com.whatsapp")
    b = parser.parse_review_record(records[0], "com.whatsapp")
    for row, where in ((a, "embedded"), (b, "batchexecute")):
        ok &= check(f"[reviews] {where} row has an id", bool(row.review_id))
        ok &= check(f"[reviews] {where} row has a score in 1..5",
                    row.score in (1, 2, 3, 4, 5), str(row.score))
        ok &= check(f"[reviews] {where} row has an ISO date",
                    bool(row.posted_at) and row.posted_at.startswith("20"),
                    str(row.posted_at))
    rows = [parser.parse_review_record(r, "com.whatsapp", position=i + 1)
            for i, r in enumerate(records)]
    ok &= check("[reviews] ids are unique", len({r.review_id for r in rows}) == len(rows))
    ok &= check("[reviews] several scores are represented",
                len({r.score for r in rows}) >= 3)

    # --no-authors must drop ALL THREE personal fields, not one.
    anon = parser.parse_review_record(records[0], "com.whatsapp", include_author=False)
    ok &= check("[reviews] --no-authors drops name, id and avatar together",
                (anon.author_name, anon.author_id, anon.author_image) == (None, None, None))
    ok &= check("[reviews] and keeps what the mode is for",
                anon.text == b.text and anon.score == b.score)
    return ok


def test_request_builder_matches_what_the_store_answers():
    """The one request shape measured to work, pinned byte for byte."""
    ok = True
    form = parser.build_reviews_request("com.whatsapp", 40, None, "newest")
    ok &= check("[req] one form field", list(form) == ["f.req"], str(list(form)))
    ok &= check("[req] the RPC id", '"UsvDTd"' in form["f.req"])
    # The sort key is the SECOND slot of the sort block, after a constant 2.
    # Up to 0.2.0 this check pinned the key in the FIRST slot, which the
    # store reads as "relevance" for 2 and answers with nothing for 1 and 3 —
    # so the suite was defending the bug that made `--sort` do nothing.
    for name, key in (("relevant", 1), ("newest", 2), ("rating", 3)):
        inner = json.loads(json.loads(
            parser.build_reviews_request("com.whatsapp", 40, None, name)
            ["f.req"])[0][0][1])
        ok &= check(f"[req] --sort {name} sends key {key} in the second slot",
                    inner[2] == [2, key, [40, None, None], None, []],
                    str(inner[2]))
    ok &= check("[req] the app id and Play's type tag",
                '[\\"com.whatsapp\\",7]' in form["f.req"], form["f.req"])
    # Measured: asking for score 1 and score 5 each returned forty reviews
    # spanning all five. A filter that changes nothing must not be offered.
    newest = json.loads(json.loads(form["f.req"])[0][0][1])
    ok &= check("[req] no score filter is sent (none is implemented)",
                newest[2][3] is None, str(newest[2]))
    ok &= check("[req] all three of the store's orderings are offered",
                parser.REVIEW_SORTS == {"relevant": 1, "newest": 2, "rating": 3},
                str(parser.REVIEW_SORTS))
    ok &= check("[req] the default ordering is newest",
                parser.DEFAULT_REVIEW_SORT == "newest")
    try:
        parser.build_reviews_request("com.whatsapp", 40, None, "oldest")
        ok &= check("[req] an unknown sort is refused, not silently replaced",
                    False, "no error raised")
    except ValueError:
        pass
    with_token = parser.build_reviews_request("com.whatsapp", 40, "TOKEN", "newest")
    ok &= check("[req] a token goes into the pagination slot",
                "[40,null,\\\"TOKEN\\\"]" in with_token["f.req"], with_token["f.req"])
    return ok


def test_newest_reviews_are_newest_first():
    """`--sort newest` must come back newest first, and stay so over a page.

    The captures are taken by `make_fixtures.py` through
    `build_reviews_request(..., "newest")`, page 2 with page 1's token, so
    they are exactly what this repository asks the store for. Up to 0.2.0 that
    request put the sort key in the wrong slot and the store answered in
    relevance order: dates like 08-28, 08-31, 09-18, 07-26 — which no check
    looked at, because every check counted rows.
    """
    ok = True
    pages = []
    for name in ("reviews_p1.txt", "reviews_p2.txt"):
        path = os.path.join(HERE, "captures", name)
        if not os.path.exists(path):
            skip("newest-order", f"captures/{name} absent")
            return True
        with open(path, encoding="utf-8", errors="replace") as fh:
            records, token = parser.reviews_from_payload(
                parser.parse_batchexecute(fh.read()))
        pages.append([parser.parse_review_record(r, "com.whatsapp")
                      for r in records])
    dates = [r.posted_at for page in pages for r in page]
    ok &= check("[order] both pages carry rows", all(pages), str([len(p) for p in pages]))
    ok &= check("[order] newest is non-increasing within AND across pages",
                all(dates[i] >= dates[i + 1] for i in range(len(dates) - 1)),
                str(dates[:6]))
    ok &= check("[order] page 2 continues rather than restarts",
                not ({r.review_id for r in pages[0]} & {r.review_id for r in pages[1]}))
    return ok


def test_fixtures_carry_no_real_reviewers():
    """Guard by PATTERN, so the next capture is caught too.

    A committed review dump carries a real person's display name, their
    profile permalink id and their uploaded avatar. The repo's own credential
    grep matches none of those shapes.
    """
    ok = True
    path = os.path.join(HERE, "captures", "reviews_p1.txt")
    if not os.path.exists(path):
        skip("reviewer-pii", "no reviews capture")
        return True
    with open(path, encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
    # A Google profile id is 21 digits. A real avatar lives under /a-/ or /a/.
    ids = re.findall(r'"(\d{20,22})"', raw)
    avatars = re.findall(r'googleusercontent\.com/a-?/(?!VATAR-REDACTED)', raw)
    ok &= check("[pii] no real profile ids in the committed capture",
                not ids, f"{len(ids)} found, e.g. {ids[:1]}")
    ok &= check("[pii] no real reviewer avatars in the committed capture",
                not avatars, f"{len(avatars)} found")
    return ok


# ==========================================================================
# Structure — the checks that catch what a fixture cannot
# ==========================================================================

SHIPPED_PY = ("product_parser.py", "page_flow.py", "engine_core.py",
              "output_writer.py", "proxy_pool.py", "env_config.py",
              "captcha_solver.py", "fingerprint_client.py",
              "scraper_api_client.py", "diff_runs.py", "smoke_test.py",
              "playwright_scraper.py", "selenium_scraper.py",
              "puppeteer_scraper.py")

SHIPPED_TEXT = SHIPPED_PY + ("README.md", "CHANGELOG.md", ".env.example",
                             "CONTRIBUTING.md", "SECURITY.md")


def _read(name):
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def test_banned_wording():
    """Names this family does not use.

    The phrases are ASSEMBLED from pieces rather than written out, so this
    file can scan ITSELF — three repos in this family exempted `smoke_test.py`
    wholesale, making the file most likely to acquire a stray phrase the one
    file nobody scanned. It also means the release note describing a fix here
    does not fail the check the fix adds.
    """
    ok = True
    banned = [
        ("cloud " + "browser", "Scraping Browser API"),
        ("anti" + "detect browser", "Scraping Browser API"),
        ("gate." + "2prx.com", "2captcha.com/proxy"),
        ("--anti" + "detect", "removed — that endpoint was a placeholder"),
        ("ANTI" + "DETECT_LOCAL_API", "removed"),
    ]
    scanned = 0
    for name in SHIPPED_TEXT:
        text = _read(name)
        if text is None:
            continue
        scanned += 1
        lowered = text.lower()
        for phrase, instead in banned:
            ok &= check(f"[wording] {name} avoids a banned phrase",
                        phrase.lower() not in lowered,
                        f"write {instead!r} instead")
    ok &= check("[wording] the scan actually read something", scanned >= 5,
                f"scanned {scanned} files")
    return ok


ENGINES = ("playwright_scraper.py", "selenium_scraper.py", "puppeteer_scraper.py")


def test_engines_share_the_cli_and_the_loops():
    """Flag parity by construction rather than by comparison.

    Every repo in this family compares three hand-maintained CLIs with a test,
    and every repo in this family has at some point found twelve flags on one
    engine and not its twins. Here there is one CLI, so the check is that no
    engine has grown a private one.
    """
    import ast
    ok = True
    for name in ENGINES:
        src = _read(name)
        if src is None:
            ok &= check(f"[engines] {name} exists", False)
            continue
        tree = ast.parse(src)
        ok &= check(f"[engines] {name} imports engine_core",
                    any(isinstance(n, ast.Import)
                        and any(a.name == "engine_core" for a in n.names)
                        for n in ast.walk(tree)))
        ok &= check(f"[engines] {name} defines no ArgumentParser of its own",
                    "ArgumentParser(" not in src,
                    "the CLI lives in engine_core so the three cannot drift")
        # `options.add_argument` is Chrome's, not argparse's — the check is
        # about a private CLI, so it asks whether anything argparse-shaped is
        # being built here.
        ok &= check(f"[engines] {name} builds no argparse of its own",
                    not re.search(r"\bargparse\b|ap\.add_argument\(|"
                                  r"parser\.add_argument\(", src),
                    "the CLI lives in engine_core so the three cannot drift")
        ok &= check(f"[engines] {name} hands its launcher to engine_core.main",
                    "engine_core.main(launch)" in src)
        ok &= check(f"[engines] {name} exposes launch()",
                    re.search(r"^def launch\(", src, re.M) is not None)
    # The whole contract in one line: the flags the family names.
    cli = _read("engine_core.py") or ""
    for flag in ("--url", "--pages", "--format", "--out", "--delay",
                 "--retries", "--retry-delay", "--concurrency", "--proxy",
                 "--proxy-file", "--proxy-rotate", "--proxy-shuffle",
                 "--proxy-block-retries", "--twocaptcha-key", "--captcha-api",
                 "--solve-captcha", "--min-score", "--cdp-endpoint",
                 "--allow-empty", "--dump-html", "--headless", "--headful",
                 "--fingerprint", "--fp-country", "--fp-tags", "--locale",
                 "--mode"):
        ok &= check(f"[engines] the family flag {flag} is present",
                    f'"{flag}"' in cli)
    # --category is the family's, and on this site it names a Play genre.
    ok &= check("[engines] --category is present", '"--category"' in cli)
    return ok


def test_engines_import_their_driver_at_module_level():
    """Or the smoke suite's skip never fires and a broken import reaches a run.

    A sibling repo imported `launch`/`connect` inside the launch path, so the
    module imported cleanly with nothing installed: the group never skipped,
    and the CI job that exists to fail on unexpected skips could not have
    caught a broken import.
    """
    import ast
    ok = True
    wanted = {"playwright_scraper.py": "playwright",
              "selenium_scraper.py": "selenium",
              "puppeteer_scraper.py": "pyppeteer"}
    for name, library in wanted.items():
        src = _read(name)
        if src is None:
            continue
        tree = ast.parse(src)
        top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        names = []
        for node in top:
            if isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module.split(".")[0])
            elif isinstance(node, ast.Import):
                names.extend(a.name.split(".")[0] for a in node.names)
        ok &= check(f"[engines] {name} imports {library} at module level",
                    library in names, str(names))
    return ok


def test_no_statement_after_return():
    """A statement after return/raise/break/continue in the same block.

    Six repos in this family carried the same fifteen dead lines from their
    first commit: a function whose `def` line had been lost, leaving its body
    absorbed into the end of the function above it. It parses, it imports,
    `--help` works and `compileall` passes.
    """
    import ast
    ok = True
    scanned = 0
    for name in SHIPPED_PY:
        src = _read(name)
        if src is None:
            continue
        scanned += 1
        tree = ast.parse(src)
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            for block in (body, getattr(node, "orelse", None),
                          getattr(node, "finalbody", None)):
                if not isinstance(block, list):
                    continue
                for i, stmt in enumerate(block[:-1]):
                    if isinstance(stmt, (ast.Return, ast.Raise, ast.Break,
                                         ast.Continue)):
                        ok &= check(
                            f"[dead] {name}:{block[i + 1].lineno} is unreachable",
                            False, f"follows {type(stmt).__name__.lower()} "
                                   f"on line {stmt.lineno}")
    ok &= check("[dead] the walk actually scanned files", scanned >= 10,
                f"scanned {scanned}")
    return ok


def test_shared_module_calls_bind():
    """Every call into a shared module, bound against the real signature.

    `classify(html, status, url)` took `status` positionally while two of
    three engines called it `classify(html, url=…)` in a sibling repo, and
    BOTH crashed on their first fetch — invisible to import, `--help`,
    `compileall` and four hundred green assertions, because none of those
    calls a function the way a live run does.

    Two rules this needs, both learned the hard way:
      * a name bound anywhere in the calling file SHADOWS a same-named module;
      * a name the module does not define at all must FAIL, not be skipped —
        a sibling's version resolved with `getattr(m, n, None)` and silently
        skipped everything that came back None, which was the loudest thing
        it could have reported.
    """
    import ast
    import inspect
    ok = True
    shared = {"product_parser": parser, "page_flow": page_flow,
              "output_writer": output_writer}
    aliases = {"parser": "product_parser"}
    checked = 0
    for name in SHIPPED_PY:
        src = _read(name)
        if src is None or name in ("smoke_test.py",):
            continue
        tree = ast.parse(src)
        bound = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.arg):
                bound.add(node.arg)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                bound.add(node.id)
        # Every attribute access on a shared module, not only the calls.
        # A control that planted `page_flow.next_page_selector` — a name the
        # module does not define — left this check GREEN when it only walked
        # `ast.Call`, and an attribute access to a name that does not exist
        # raises at runtime exactly as hard as a call does.
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id not in bound):
                module = shared.get(aliases.get(node.value.id, node.value.id))
                if module is not None:
                    ok &= check(
                        f"[bind] {name}:{node.lineno} "
                        f"{node.value.id}.{node.attr} exists",
                        hasattr(module, node.attr),
                        f"{node.value.id} defines no {node.attr!r}")
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)):
                continue
            owner = node.func.value.id
            if owner in bound:
                continue           # a parameter, not the module
            module_name = aliases.get(owner, owner)
            module = shared.get(module_name)
            if module is None:
                continue
            attr = node.func.attr
            target = getattr(module, attr, None)
            ok &= check(f"[bind] {name}:{node.lineno} {owner}.{attr} exists",
                        target is not None,
                        f"{module_name} defines no {attr!r}")
            if target is None or not callable(target):
                continue
            try:
                signature = inspect.signature(target)
            except (TypeError, ValueError):
                continue
            args = [inspect.Parameter.empty] * len(node.args)
            kwargs = {kw.arg: None for kw in node.keywords if kw.arg}
            if any(kw.arg is None for kw in node.keywords):
                continue           # **kwargs — nothing to bind
            if any(isinstance(a, ast.Starred) for a in node.args):
                continue
            try:
                signature.bind(*args, **kwargs)
                checked += 1
            except TypeError as exc:
                ok &= check(f"[bind] {name}:{node.lineno} {owner}.{attr}(...)",
                            False, str(exc))
    ok &= check("[bind] the check actually bound something", checked >= 15,
                f"bound {checked} calls")
    return ok


def test_policy_constants_have_consumers():
    """A policy constant nothing reads is the same defect as dead code.

    A sibling repo's `RETRY_ON_BLOCKED` carried a paragraph of measured
    justification and no engine consulted it, so setting it False changed
    nothing while the prose read like enforcement.
    """
    import ast
    ok = True
    # What counts as a consumer is a CALL (or attribute read) of
    # `page_flow.<name>` in shipped code outside page_flow and this suite —
    # not the name appearing somewhere. The old version of this check passed
    # for a constant whose helper nothing called, and for one mentioned only
    # in another module's docstring.
    used = set()
    for name in SHIPPED_PY:
        if name in ("page_flow.py", "smoke_test.py"):
            continue
        src = _read(name)
        if not src:
            continue
        for node in ast.walk(ast.parse(src)):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "page_flow"):
                used.add(node.attr)
    # Each policy constant, and the helper through which it is enforced.
    for constant, helper in (("RETRY_ON_BLOCKED", "block_retries"),
                             ("BLOCK_RETRIES_WITHOUT_POOL", "block_retries"),
                             ("SOLVES_PER_PAGE", "SolveBudget"),
                             ("SEARCH_IS_PAGINATED", "warn_unpaginated"),
                             ("THROTTLE_BACKOFF_MS", "throttle_delay_ms"),
                             ("THROTTLE_RETRIES", "THROTTLE_RETRIES")):
        ok &= check(f"[policy] {constant} is enforced ({helper} has a caller)",
                    helper in used, "nothing outside page_flow reaches it")
    # And every public function page_flow defines is reached from outside it.
    # `solve_budget` is the one exception, and only because SolveBudget, which
    # IS reached, is its caller.
    tree = ast.parse(_read("page_flow.py") or "")
    for node in tree.body:
        if (isinstance(node, (ast.FunctionDef, ast.ClassDef))
                and not node.name.startswith("_") and node.name != "solve_budget"):
            ok &= check(f"[policy] page_flow.{node.name} has a consumer",
                        node.name in used, "dead policy reads like enforcement")
    ok &= check("[policy] every state has a policy row",
                set(page_flow.STATE_POLICY) == set(parser.PAGE_STATES),
                f"{sorted(page_flow.STATE_POLICY)} vs {sorted(parser.PAGE_STATES)}")
    # The triage itself: a 404 must not be a block, and an empty page must not
    # be retried.
    ok &= check("[policy] not_found is not blocked",
                not page_flow.counts_as_blocked("not_found"))
    ok &= check("[policy] not_found is not retried",
                not page_flow.should_retry("not_found"))
    ok &= check("[policy] empty is not retried", not page_flow.should_retry("empty"))
    ok &= check("[policy] blocked is retried and may solve",
                page_flow.should_retry("blocked") and page_flow.should_solve("blocked"))
    ok &= check("[policy] one solve per page", page_flow.solve_budget(0)
                and not page_flow.solve_budget(1))
    ok &= check("[policy] reviews refuses workers",
                page_flow.concurrency_limit("reviews") == 1,
                "page N+1's address is a token inside page N's response")
    ok &= check("[policy] grid modes allow workers",
                page_flow.concurrency_limit("listing") is None)
    return ok


def test_env_example_matches_what_the_code_reads():
    ok = True
    import env_config
    text = _read(".env.example")
    if text is None:
        ok &= check("[env] .env.example exists", False)
        return ok
    # A variable may be documented commented out (`# NAME=example`).
    documented = set(re.findall(r"^(?:#\s*)?([A-Z][A-Z0-9_]*)=", text, re.M))
    read = set(env_config.ENV_KEYS)
    ok &= check("[env] every variable the code reads is documented",
                read <= documented, f"missing {sorted(read - documented)}")
    ok &= check("[env] every documented variable is read",
                documented <= read, f"unread {sorted(documented - read)}")
    ok &= check("[env] the site prefix is this site's",
                all(k.startswith("GOOGLEPLAY_") or k == "TWOCAPTCHA_KEY"
                    for k in read), str(sorted(read)))
    return ok


def test_a_copied_env_example_reads_as_unset():
    """`cp .env.example .env` must not send `{login}-zone-…` to an API.

    The placeholder check was a literal set in this family, and the two
    credentialled URLs are documented the way the vendor documents them —
    with braces — so neither literal matched and a copied example produced a
    401 a long way from its cause.
    """
    ok = True
    import env_config
    text = _read(".env.example")
    if text is None:
        return True
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, ".env")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            env_config.load_env(path)
            for var in ("TWOCAPTCHA_KEY", "GOOGLEPLAY_CDP_ENDPOINT",
                        "GOOGLEPLAY_PROXY"):
                value = env_config.env_value(var)
                ok &= check(f"[env] a copied example leaves {var} unset",
                            not value, repr(value))
            # ...and the start URL is documented commented out, so a copied
            # example does not decide what a run reads.
            url = env_config.env_value("GOOGLEPLAY_URL")
        ok &= check("[env] a copied example sets no start URL", not url,
                    repr(url))
        ok &= check("[env] the warning names the variable, not the value",
                    "GOOGLEPLAY_PROXY" in buf.getvalue()
                    and "{password}" not in buf.getvalue())
        for var in ("TWOCAPTCHA_KEY", "GOOGLEPLAY_CDP_ENDPOINT",
                    "GOOGLEPLAY_PROXY", "GOOGLEPLAY_URL"):
            os.environ.pop(var, None)
    return ok


def test_dockerfile_copies_what_the_entrypoint_imports():
    """An explicit COPY list is right and falls behind.

    All three repos in this family once shipped an image that died with
    ModuleNotFoundError on every invocation, `--help` included, because one
    module was missing from the list. CI never built the image; this check
    needs no Docker.
    """
    import ast
    ok = True
    dockerfile = _read("Dockerfile")
    if dockerfile is None:
        skip("docker", "no Dockerfile")
        return True
    copied = set()
    # Join line continuations first: the COPY list is written across three
    # physical lines, and reading them one at a time drops every filename that
    # happens to sit at the end of one.
    joined = re.sub(r"\\\s*\n\s*", " ", dockerfile)
    for line in joined.splitlines():
        if line.strip().upper().startswith("COPY"):
            for token in line.split()[1:]:
                if token not in ("./", "."):
                    copied.add(os.path.basename(token))
    needed = set()
    queue = ["playwright_scraper.py"]
    seen = set()
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        src = _read(name)
        if src is None:
            continue
        needed.add(name)
        for node in ast.walk(ast.parse(src)):
            mod = None
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod = alias.name.split(".")[0]
                    if os.path.exists(os.path.join(HERE, mod + ".py")):
                        queue.append(mod + ".py")
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                mod = node.module.split(".")[0]
                if os.path.exists(os.path.join(HERE, mod + ".py")):
                    queue.append(mod + ".py")
    missing = sorted(needed - copied)
    ok &= check("[docker] every imported module is COPYed", not missing,
                f"missing {missing}")
    # ...and nothing that must never be in an image.
    for forbidden in (".env", "captures", "live"):
        ok &= check(f"[docker] the image carries no {forbidden}",
                    forbidden not in copied,
                    "a .env baked into an image is a credential published to "
                    "everyone who can pull it")
    return ok


def test_exit_codes():
    """Blocked, empty, partial and never-obtained are four different answers."""
    ok = True
    ok &= check("[exit] usage is 2", output_writer.EXIT_USAGE == 2)
    ok &= check("[exit] blocked is 3", output_writer.EXIT_BLOCKED == 3)
    ok &= check("[exit] zero products is 4", output_writer.EXIT_NO_PRODUCTS == 4)
    ok &= check("[exit] never obtained is 5", output_writer.EXIT_FETCH_FAILED == 5)
    ok &= check("[exit] partial is 6", output_writer.EXIT_PARTIAL == 6)
    ok &= check("[exit] reaching the real end of a listing is COMPLETE",
                "end_of_listing" in output_writer.COMPLETE_STOP_REASONS,
                "leaving it out reports exit 6 for a correct run")

    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "out")
        buf = io.StringIO()
        # Nothing gathered and the run did not finish: that is 5, not 4. A
        # consumer must be able to tell a dead proxy from an empty catalogue,
        # and the rule deliberately has no list of "fetch failure" reasons —
        # a list cannot cover a stop_reason nobody has added to it yet.
        with redirect_stdout(buf), redirect_stderr(buf):
            rc = output_writer.finish_run(
                [], prefix, "json", False, blocked=False,
                stop_reason="navigation_failed", pages_requested=3,
                pages_completed=0, start_url="u", final_url="u", mode="listing")
        ok &= check("[exit] nothing gathered, run unfinished -> 5", rc == 5, str(rc))
        ok &= check("[exit] and no output file was written",
                    not os.path.exists(prefix + ".json"),
                    "a failed run must not overwrite last night's good data")
        ok &= check("[exit] and no sidecar contradicts the good data",
                    not os.path.exists(prefix + ".meta.json"))
    return ok


def test_urls_and_refusals():
    ok = True
    ok &= check("[url] an app URL is rebuilt from the package id",
                parser.app_url("com.whatsapp", "en", "US") ==
                "https://play.google.com/store/apps/details?id=com.whatsapp&hl=en&gl=US")
    ok &= check("[url] the package id comes back out",
                parser.package_from_url(parser.app_url("com.whatsapp")) == "com.whatsapp")
    ok &= check("[url] a numeric developer id uses /dev",
                "/store/apps/dev?" in parser.developer_url("4772240228547998649"))
    ok &= check("[url] a named developer uses /developer",
                "/store/apps/developer?" in parser.developer_url("WhatsApp LLC"))
    ok &= check("[url] the market replaces rather than duplicates",
                parser.with_market(
                    "https://play.google.com/store/search?q=a&hl=xx&gl=YY",
                    "de", "DE").count("hl=") == 1)
    # Refusals must carry the REASON. "is not a Play site" would be false for
    # a Play storefront this repo simply does not implement.
    books = parser.refusal_reason("https://play.google.com/store/books")
    ok &= check("[url] a Play storefront we do not read is refused", bool(books))
    ok &= check("[url] and the reason says we do not implement it, not that "
                "it cannot be read",
                books and "does not implement" in books, str(books))
    other = parser.refusal_reason("https://example.com/store/apps")
    ok &= check("[url] another host is refused with its own reason",
                other and "not play.google.com" in other, str(other))
    ok &= check("[url] a route we do read is not refused",
                parser.refusal_reason(
                    "https://play.google.com/store/apps/category/GAME") is None)
    return ok


def test_price_and_number_parsing():
    ok = True
    ok &= check("[price] micros", parser.parse_offer([[[6990000, "USD", "$6.99"]]])[:2]
                == (6.99, "USD"))
    ok &= check("[price] free is 0.0, not None",
                parser.parse_offer([[[0, "USD", ""]]])[0] == 0.0)
    ok &= check("[price] an absent offer is None, not a defaulted currency",
                parser.parse_offer(None) == (None, None, None))
    ok &= check("[price] the currency is never guessed from a symbol",
                parser.parse_offer([[[100, "$", "$1"]]])[1] is None)
    ok &= check("[rating] the display string wins, locale-formatted",
                parser.parse_rating(["4,6", 4.6164712])[0] == 4.6)
    ok &= check("[rating] the raw float is kept for provenance",
                parser.parse_rating(["4.6", 4.6164712])[1] == 4.6164712)
    ok &= check("[installs] a locale-grouped bucket",
                parser.parse_installs("10.000.000.000+")[1] == 10000000000)
    ok &= check("[installs] the exact count when the site states one",
                parser.parse_installs(["10,000,000,000+", 10000000000,
                                       12322703000, "10B+"])[2] == 12322703000)
    ok &= check("[installs] an exact count below the floor is refused",
                parser.parse_installs(["1,000+", 1000, 5, "1K+"])[2] is None,
                "the two slots are not what they were taken for")
    ok &= check("[epoch] seconds become ISO, nanoseconds are dropped",
                parser.epoch_to_iso([1789778944, 484000000]).startswith("2026-"))
    return ok


def test_engine_modules_import():
    """Each engine, guarded, with the skip RECORDED.

    CI's `engine-smoke` job installs one engine per virtualenv and fails if
    the matching group reports a skip: "skipped, engine absent" reads
    identically to a real import error, and that hid a stale mock for a day
    in a sibling repo.
    """
    ok = True
    for module, library in (("playwright_scraper", "playwright"),
                            ("selenium_scraper", "selenium"),
                            ("puppeteer_scraper", "pyppeteer")):
        try:
            engine = __import__(module)
        except ImportError as exc:
            skip(module, f"{library} not installed ({exc})")
            continue
        ok &= check(f"[{module}] exposes launch()", callable(getattr(engine, "launch", None)))
        ok &= check(f"[{module}] names itself", isinstance(getattr(engine, "NAME", None), str))
    return ok


def test_sample_output_matches_the_schema():
    from dataclasses import fields
    ok = True
    sample = _read("sample_output.json")
    if sample is None:
        skip("sample-output", "sample_output.json absent")
        return True
    rows = json.loads(sample)
    ok &= check("[sample] it holds rows", isinstance(rows, list) and rows)
    if not rows:
        return False
    expected = [f.name for f in fields(App)]
    ok &= check("[sample] columns match App exactly",
                list(rows[0]) == expected,
                f"extra {set(rows[0]) - set(expected)} missing {set(expected) - set(rows[0])}")
    ok &= check("[sample] it is cut from a real run, not fabricated",
                all(r.get("source") == "play.google.com" for r in rows))
    ok &= check("[sample] no fabrication markers",
                not re.search(r"example\.com|lorem ipsum|foo\.bar|test-app",
                              sample, re.I))
    return ok


def test_credential_scan_runs_and_is_wired():
    """One implementation, invoked from both CI and this suite.

    A sibling repo shipped `.github/ci_checks.py` that NOTHING ran, while its
    workflow carried an inline grep doing a narrower version of the same job —
    two sources of truth, one dead and one holed.
    """
    ok = True
    script = os.path.join(HERE, ".github", "ci_checks.py")
    if not os.path.exists(script):
        # Trigger on the whole .github directory being absent, never on a file
        # inside it going missing: a check that quietly starts passing once
        # its input disappears is worse than no check. The Dockerfile builds
        # this suite into the image and deliberately COPYs no .github/.
        if not os.path.isdir(os.path.join(HERE, ".github")):
            skip("credential-scan", "no .github/ in this tree (the image)")
            return True
        return check("[creds] .github/ci_checks.py exists", False)

    result = subprocess.run([sys.executable, script, "--secret-check"],
                            cwd=HERE, capture_output=True, text=True)
    ok &= check("[creds] the credential scan passes on this tree",
                result.returncode == 0,
                (result.stdout + result.stderr)[-600:])

    workflow = _read(os.path.join(".github", "workflows", "tests.yml"))
    if workflow:
        ok &= check("[creds] CI calls the script rather than reimplementing it",
                    "ci_checks.py" in workflow)
    return ok


def test_credential_scan_sees_into_a_virtualenv_and_a_fixture():
    """Two holes this family has paid for, pinned so they cannot come back.

    A venv is recognised by `pyvenv.cfg`, not by being called `.venv` — the
    README tells people to make one per engine, so a differently-named one
    was walked into and pip's vendored hex read as a key on a stranger's first
    command. And the scan must still read UNTRACKED files: the case it exists
    for is an `.env.bak` nobody committed.
    """
    ok = True
    script = os.path.join(HERE, ".github", "ci_checks.py")
    if not os.path.exists(script):
        skip("credential-scan-holes", "no ci_checks.py")
        return True
    src = _read(os.path.join(".github", "ci_checks.py")) or ""
    ok &= check("[creds] a venv is recognised by pyvenv.cfg, not by name",
                "pyvenv.cfg" in src,
                "excluding '.venv' and 'venv' by name misses every other name")
    ok &= check("[creds] the scan reads untracked files too",
                "--others" in src and "--exclude-standard" in src,
                "an index-only scan loses the untracked .env.bak this exists "
                "for — `--cached` alone is the hole, `--cached --others "
                "--exclude-standard` is the fix")
    return ok


def test_gitignore_covers_every_env_spelling():
    """`.env` alone is not enough.

    Nineteen of twenty repos in this family listed `.env` and not `.env.*`, so
    a working copy renamed the way people actually rename them — `.env.bak`,
    `.env.local`, `.env.save` — was untracked but NOT ignored: one `git add -A`
    from being committed, and invisible to the person deciding whether running
    that is safe.
    """
    ok = True
    text = _read(".gitignore")
    if text is None:
        return check("[gitignore] exists", False)
    lines = [l.strip() for l in text.splitlines()]
    ok &= check("[gitignore] every .env spelling is ignored", ".env*" in lines,
                "`.env` alone leaves .env.bak untracked and unignored")
    ok &= check("[gitignore] but the example is not", "!.env.example" in lines)
    ok &= check("[gitignore] raw captures and run output stay out",
                any(l.startswith("live") for l in lines))
    return ok


def test_scraper_api_waitfor_object_and_http_code():
    """Measured 2026-09-23 against the live Scraper API: `waitFor` sent as
    a JSON-encoded string is refused with HTTP 422 and still billed, and
    the response's `status` is the API's verdict ("success") while the
    target's own code is `http_code`. Drives the real fetch_html with
    requests.post replaced, so no network and no money."""
    import argparse
    import logging
    import scraper_api_client as sac
    captured = {}

    class _Resp:
        status_code = 200
        headers = {}
        text = ""

        def json(self):
            return {"status": "success", "http_code": 403, "body": "<html></html>"}

    def _fake_post(url, **kw):
        captured["json"] = kw.get("json")
        return _Resp()

    logged = []

    class _H(logging.Handler):
        def emit(self, record):
            if str(record.msg).startswith("Upstream page status"):
                logged.append(record.args[0])

    h = _H()
    real_post = sac.requests.post
    sac.requests.post = _fake_post
    sac.logger.addHandler(h)
    result = None
    try:
        args = argparse.Namespace(url='https://play.google.com/store/apps/category/GAME', key="k", timeout=60, cdp_url=None,
                                  wait_text='Games', wait_element=None,
                                  wait_state=None)
        result = sac.fetch_html(args)
    finally:
        sac.requests.post = real_post
        sac.logger.removeHandler(h)
    # Where fetch_html returns (html, status) the status handed onward is
    # the second element; where it returns only the html, the log line is
    # the only place the status goes.
    handed = result[1] if isinstance(result, tuple) else (logged[0] if logged else None)
    wf = (captured.get("json") or {}).get("waitFor")
    ok = True
    ok &= check("Scraper API: --wait-text sends waitFor as an OBJECT, not a JSON string "
                "(a string is HTTP 422 and still billed, measured 2026-09-23)",
                isinstance(wf, dict) and wf.get("text") == 'Games')
    ok &= check("Scraper API: the target status handed onward is http_code (403, an int), "
                "not the API's own 'success' verdict",
                type(handed) is int and handed == 403 and logged == [403])
    return ok



# ==========================================================================
# The run contract, driven end to end with a fake driver
# ==========================================================================
#
# These go through `engine_core.main()` exactly as an engine does, with a
# driver that answers from the real captures. They need no engine library,
# so they run in every venv — including CI's offline job, where no engine is
# installed and an engine-gated check would skip.

class _FakeDriver:
    """Answers `fetch` from a script; records what was asked."""

    def __init__(self, world, proxy):
        self.world = world
        self.proxy = proxy
        self._url = ""

    def fetch(self, url, mode):
        self.world["fetches"].append((url, self.proxy))
        step = self.world["pages"].pop(0) if len(self.world["pages"]) > 1 \
            else self.world["pages"][0]
        if isinstance(step, BaseException):
            raise step
        self._url = url
        self.world["current"] = step
        return step

    def read(self, mode):
        after = self.world.get("after_solve")
        return after if after is not None else self.world["current"][0]

    def evaluate(self, js, arg=None):
        import captcha_solver
        if js == captcha_solver.RECAPTCHA_DISCOVERY_JS:
            return self.world.get("discovery")
        self.world["injected"].append(arg)
        return True

    def post_form(self, url, form):
        self.world["forms"].append(form)
        step = self.world["posts"].pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    def current_url(self):
        return self._url

    def close(self):
        self.world["closed"] += 1


def _run_main(argv, pages, posts=(), discovery=None, after_solve=None,
              launch_error=None, env=None, out_name=None, dump=None,
              prepare=None):
    """`(rc, world, out_prefix_files)` for one `engine_core.main` run."""
    import engine_core
    world = {"pages": list(pages), "posts": list(posts), "forms": [], "fetches": [],
             "launches": [], "closed": 0, "injected": [], "current": None,
             "discovery": discovery, "after_solve": after_solve}

    def launch(args, proxy_url):
        world["launches"].append(proxy_url)
        if launch_error is not None:
            raise launch_error
        return _FakeDriver(world, proxy_url)

    saved_sleep = engine_core._sleep
    engine_core._sleep = lambda seconds: world.setdefault("slept", []).append(seconds)
    saved_env = {k: os.environ.get(k) for k in (env or {})}
    os.environ.update(env or {})
    try:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = os.path.join(tmp, out_name or "o")
            extra = ["--out", prefix, "--format", "both"]
            if dump:
                extra += ["--dump-html", os.path.join(tmp, dump)]
            if prepare:
                prepare(tmp)
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(buf):
                try:
                    rc = engine_core.main(launch, list(argv) + extra)
                except SystemExit as exc:
                    rc = exc.code
                prefix = engine_core.output_prefix(prefix)
            world["log"] = buf.getvalue()
            world["written"] = sorted(
                os.path.relpath(os.path.join(d, f), tmp)
                for d, _dirs, fs in os.walk(tmp) for f in fs)
            files = {}
            for ext in (".json", ".csv", ".meta.json"):
                if os.path.exists(prefix + ext):
                    with open(prefix + ext, encoding="utf-8") as fh:
                        files[ext] = fh.read()
            return rc, world, files
    finally:
        engine_core._sleep = saved_sleep
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


_BLOCKED_PAGE = ("<html><body><div>Our systems have detected unusual traffic "
                 "from your computer network.</div><div class=\"g-recaptcha\">"
                 "</div></body></html>")
_SITEKEY = "6L" + "x" * 38
_DISCOVERY = {"found": True, "sitekey": _SITEKEY, "size": "normal",
              "renderParam": "explicit", "hints": []}


def test_run_contract_proxy_and_partial_rows():
    """`--proxy` runs, and rows gathered before a later failure are kept."""
    ok = True
    grid = capture("cat_game_us.html")
    if grid is None:
        skip("run-contract", "captures/cat_game_us.html absent")
        return True

    # A proxy used to crash before the first fetch (`pool.next()` does not
    # exist on ProxyPool) — a traceback and exit 1 on every --proxy run.
    rc, world, files = _run_main(
        ["--mode", "listing", "--category", "GAME", "--proxy",
         "http://user:pass@10.0.0.1:8080"], [(grid, 200)])
    ok &= check("[contract] --proxy runs to completion", rc == 0,
                f"rc={rc} {world['log'][-400:]}")
    ok &= check("[contract] the browser is launched on that proxy",
                world["launches"][:1] == ["http://user:pass@10.0.0.1:8080"],
                str(world["launches"]))
    ok &= check("[contract] the proxy password never reaches the log",
                "user:pass@" not in world["log"])

    with tempfile.TemporaryDirectory() as tmp:
        plist = os.path.join(tmp, "proxies.txt")
        with open(plist, "w") as fh:
            fh.write("http://10.0.0.1:8080\nhttp://10.0.0.2:8080\n")
        rc, world, _ = _run_main(
            ["--mode", "listing", "--category", "GAME", "--proxy-file", plist,
             "--pages", "2"], [(grid, 200), (grid, 200)])
        ok &= check("[contract] --proxy-file runs", rc in (0, 6),
                    f"rc={rc} {world['log'][-400:]}")
        ok &= check("[contract] per-page rotation is a fresh browser on the "
                    "next exit", world["launches"][:2] ==
                    ["http://10.0.0.1:8080", "http://10.0.0.2:8080"],
                    str(world["launches"]))

    # Page 2 dies after page 1 gave 64 apps: exit 6 with the rows written,
    # not exit 5 with everything thrown away.
    rc, world, files = _run_main(
        ["--mode", "listing", "--category", "GAME", "--pages", "3",
         "--retries", "0"], [(grid, 200), TimeoutError("page 2 timed out")])
    meta = json.loads(files.get(".meta.json", "{}"))
    ok &= check("[contract] a failure after rows is exit 6", rc == 6,
                f"rc={rc} {world['log'][-400:]}")
    ok &= check("[contract] and the gathered rows are written",
                len(json.loads(files.get(".json", "[]"))) > 0)
    ok &= check("[contract] and the sidecar says partial, naming page 2",
                meta.get("status") == "partial" and meta.get("pages_failed") == [2],
                str(meta))
    return ok


def test_run_contract_exit_codes_per_state():
    """Each thing a page can be gets its documented exit code."""
    ok = True
    notfound = capture("notfound.html")
    app = capture("app_free_us.html")
    if notfound is None or app is None:
        skip("run-contract", "captures absent")
        return True
    app_args = ["--mode", "app", "--app-id", "com.example.nothere"]

    # A package id that does not exist is an answer: exit 4, as the README
    # says. It used to be 5 ("never obtained").
    rc, world, _ = _run_main(app_args, [(notfound, 404)])
    ok &= check("[contract] a 404 for the app is exit 4", rc == 4, f"rc={rc}")
    # The same page with no status (Selenium) used to read as blocked, exit 3.
    rc, world, _ = _run_main(app_args, [(notfound, None)])
    ok &= check("[contract] the same 404 with no status is exit 4", rc == 4,
                f"rc={rc} {world['log'][-300:]}")
    # A served shell with the store's images and no apps: an empty answer.
    shell = ("<html>" + "<img src=\"https://play-lh.googleusercontent.com/x\">" * 30
             + "</html>")
    rc, world, _ = _run_main(["--mode", "listing", "--category", "GAME"],
                             [(shell, 200)])
    ok &= check("[contract] a served page with no apps is exit 4", rc == 4,
                f"rc={rc}")
    # A refusal: exit 3, after the block retry.
    rc, world, _ = _run_main(["--mode", "listing", "--category", "GAME",
                              "--solve-captcha", "never"],
                             [(_BLOCKED_PAGE, 200)])
    ok &= check("[contract] a refusal is exit 3", rc == 3, f"rc={rc}")
    ok &= check("[contract] and the block retry is a fresh browser",
                len(world["launches"]) == 1 + page_flow.BLOCK_RETRIES_WITHOUT_POOL,
                str(world["launches"]))
    # The launch itself failing: never obtained, exit 5.
    rc, world, _ = _run_main(["--mode", "listing", "--category", "GAME"], [],
                             launch_error=RuntimeError("no browser"))
    ok &= check("[contract] a browser that never came up is exit 5", rc == 5,
                f"rc={rc}")
    # --retries is EXTRA attempts: an unknown page is fetched 1 + N times.
    rc, world, _ = _run_main(["--mode", "listing", "--category", "GAME",
                              "--retries", "2"], [("", 503)])
    ok &= check("[contract] --retries 2 makes three attempts",
                len(world["fetches"]) == 3, str(len(world["fetches"])))
    ok &= check("[contract] and a page never obtained is exit 5", rc == 5,
                f"rc={rc}")
    # A refusal raised by an engine's launch (Selenium + --cdp-endpoint) is a
    # usage error, not a crash.
    import engine_core
    rc, world, _ = _run_main(["--mode", "listing", "--category", "GAME"], [],
                             launch_error=engine_core.UsageError("no CDP here"))
    ok &= check("[contract] an engine's UsageError is exit 2", rc == 2,
                f"rc={rc}")
    return ok


def test_run_contract_reviews_refusal_is_not_an_end():
    """A refused `batchexecute` call is a failed page, never "complete"."""
    ok = True
    app = capture("app_free_us.html")
    if app is None:
        skip("run-contract", "captures/app_free_us.html absent")
        return True
    page1 = _review_capture("reviews_p1.txt")
    if page1 is None:
        skip("run-contract", "captures/reviews_p1.txt absent")
        return True
    args = ["--mode", "reviews", "--app-id", "com.whatsapp", "--pages", "3"]
    for label, post in (
            ("a refusal body", (_BLOCKED_PAGE, 429)),
            ("an empty body", ("", 200))):
        rc, world, files = _run_main(args, [(app, 200)],
                                     posts=[(page1, 200), post])
        meta = json.loads(files.get(".meta.json", "{}"))
        ok &= check(f"[contract] reviews: {label} on page 2 is exit 6",
                    rc == 6, f"rc={rc} {meta.get('stop_reason')}")
        ok &= check(f"[contract] reviews: {label} keeps page 1's rows",
                    meta.get("status") == "partial" and meta.get("products", 0) > 0,
                    str(meta))
    return ok


def _review_capture(name):
    path = os.path.join(HERE, "captures", name)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _sent(form):
    """The sort block a posted form carried: `[2, sort, [n, null, token], …]`."""
    return json.loads(json.loads(form["f.req"])[0][0][1])[2]


def test_run_contract_reviews_pages_chain_on_the_token():
    """Page N+1 is asked for WITH page N's token, and every page is sorted.

    The defect this pins: up to 0.2.0 page 1 came from the reviews embedded in
    the app page — relevance-ordered, no token — so page 2 went out with no
    token at all, came back as the start of the list, and dedupe ate it. One
    reported run gave 20 / 1 / 19 rows over three pages.
    """
    ok = True
    app = capture("app_free_us.html")
    p1, p2 = _review_capture("reviews_p1.txt"), _review_capture("reviews_p2.txt")
    if None in (app, p1, p2):
        skip("run-contract", "app and both review captures needed")
        return True
    token1 = parser.reviews_from_payload(parser.parse_batchexecute(p1))[1]

    rc, world, files = _run_main(
        ["--mode", "reviews", "--app-id", "com.whatsapp", "--pages", "2",
         "--reviews-per-call", "40"],
        [(app, 200)], posts=[(p1, 200), (p2, 200)])
    forms = world["forms"]
    ok &= check("[reviews] two pages means two POSTs", len(forms) == 2, str(len(forms)))
    if len(forms) == 2:
        ok &= check("[reviews] page 1 asks for the start of the list",
                    _sent(forms[0])[2][2] is None, str(_sent(forms[0])))
        ok &= check("[reviews] page 2 carries page 1's continuation token",
                    _sent(forms[1])[2][2] == token1 and token1,
                    str(_sent(forms[1])[2][2])[:40])
        ok &= check("[reviews] every page asks for newest",
                    all(_sent(f)[:2] == [2, 2] for f in forms))
    rows = json.loads(files.get(".json", "[]"))
    ok &= check("[reviews] two full pages of rows, nothing eaten by dedupe",
                len(rows) == 80, str(len(rows)))
    ok &= check("[reviews] 40 rows on each page",
                [sum(1 for r in rows if r["page"] == n) for n in (1, 2)] == [40, 40])
    dates = [r["posted_at"] for r in rows]
    ok &= check("[reviews] the run's rows are newest first end to end",
                all(dates[i] >= dates[i + 1] for i in range(len(dates) - 1)))
    ok &= check("[reviews] the run completes", rc == 0, f"rc={rc}")

    # The ordering flag reaches the wire.
    rc, world, _ = _run_main(
        ["--mode", "reviews", "--app-id", "com.whatsapp", "--pages", "1",
         "--sort", "rating"], [(app, 200)], posts=[(p1, 200)])
    ok &= check("[reviews] --sort rating sends key 3",
                world["forms"] and _sent(world["forms"][0])[:2] == [2, 3],
                str(world["forms"] and _sent(world["forms"][0])))

    # An app page URL is the address of `--mode reviews` too.
    rc, world, _ = _run_main(
        ["--url", "https://play.google.com/store/apps/details?id=com.whatsapp",
         "--mode", "reviews", "--pages", "1"], [(app, 200)], posts=[(p1, 200)])
    ok &= check("[reviews] --url <app page> --mode reviews runs reviews",
                rc == 0 and len(world["forms"]) == 1, f"rc={rc}")
    return ok


def test_recaptcha_variant_on_googles_demos():
    """The variant decides which paid task is bought, so it is pinned per page.

    `captures/recaptcha_discovery.json` holds what the live discovery script
    returned on Google's own reCAPTCHA demos (2026-10-22, sitekeys replaced
    by a same-shape fake). Both v2 demos load `api.js` with no `render`
    parameter and used to come out as v3: the "no size, no render" case fell
    through to the last branch, and a v3 task bought for a v2 widget is
    refused by the solver (ERROR_CAPTCHA_UNSOLVABLE in the report that found
    it). With the corrected kind, both demos were solved and accepted.
    """
    import captcha_solver
    ok = True
    path = os.path.join(HERE, "captures", "recaptcha_discovery.json")
    if not os.path.exists(path):
        skip("recaptcha-variant", "captures/recaptcha_discovery.json absent")
        return True
    with open(path, encoding="utf-8") as fh:
        pages = json.load(fh)["pages"]
    ok &= check("[recaptcha] the fixture covers v2, v2-invisible and v3",
                {p["expected_kind"] for p in pages} ==
                {"recaptcha_v2", "recaptcha_v2_invisible", "recaptcha_v3"})
    for page in pages:
        info = page["discovery"]
        got = captcha_solver.detect_recaptcha_in_page(lambda js, i=info: i,
                                                      page["url"])
        ok &= check(f"[recaptcha] {page['name']} -> {page['expected_kind']}",
                    got is not None and got.kind == page["expected_kind"],
                    f"got {got.kind if got else None}")

    # The badge alone must NOT make an invisible v2: v3 draws it too. With the
    # loader's `render=<key>` present it is v3 whatever the badge says.
    v3 = next(p["discovery"] for p in pages if p["expected_kind"] == "recaptcha_v3")
    ok &= check("[recaptcha] v3 draws the badge too, and stays v3",
                v3.get("badge") is True and captcha_solver.detect_recaptcha_in_page(
                    lambda js: v3, "").kind == "recaptcha_v3")
    # A page whose loader was not seen keeps the old conservative fallback
    # rather than being read as implicit v2.
    no_loader = dict(v3, loaderSeen=False, renderParam=None, size=None)
    ok &= check("[recaptcha] no loader seen falls back instead of guessing v2",
                captcha_solver.detect_recaptcha_in_page(
                    lambda js: no_loader, "").kind == "recaptcha_v3")

    # The static twin: `grecaptcha.render` is v2's explicit call, `execute` is v3.
    key = "6L" + "x" * 38
    explicit = (f'<div data-sitekey="{key}"></div>'
                f'<script>grecaptcha.render("c", {{sitekey: "{key}"}})</script>')
    executed = (f"<script>grecaptcha.execute('{key}', {{action: 'login'}})</script>")
    got = captcha_solver.detect_recaptcha_v3(explicit, "https://x.test/")
    ok &= check("[recaptcha] static: grecaptcha.render is v2",
                got is not None and got.kind == "recaptcha_v2",
                f"got {got.kind if got else None}")
    got = captcha_solver.detect_recaptcha_v3(executed, "https://x.test/")
    ok &= check("[recaptcha] static: grecaptcha.execute is v3",
                got is not None and got.kind == "recaptcha_v3",
                f"got {got.kind if got else None}")
    return ok


def test_captures_are_committable():
    """Every file in captures/ must be one git would commit.

    The checks above read these files and SKIP when one is missing, and a
    skip reads as green. `.gitignore` carries `*.json` for run output, which
    silently swallowed the first JSON fixture added here: it existed on the
    machine that wrote the check and would not have existed in CI.
    """
    ok = True
    if not os.path.isdir(os.path.join(HERE, ".git")):
        skip("captures-committable", "not a git checkout (the image)")
        return True
    names = sorted(os.listdir(os.path.join(HERE, "captures")))
    ok &= check("[captures] the directory is not empty", bool(names))
    for name in names:
        rel = os.path.join("captures", name)
        # `--no-index`: ask whether an ignore RULE matches, whatever the
        # index says. Without it a file already tracked is never reported, so
        # the check only worked until the fixture was committed — and the
        # trap is the rule, which will swallow the NEXT capture added.
        r = subprocess.run(["git", "check-ignore", "-q", "--no-index", rel],
                           cwd=HERE)
        ok &= check(f"[captures] {rel} is not gitignored", r.returncode != 0,
                    "a fixture git will not commit makes its check skip in CI")
    return ok


def test_screenshot_count_on_both_routes():
    ok = True
    for name, package, want in (("app_free_us.html", "com.whatsapp", 9),
                                ("app_iap_us.html", "com.mojang.minecraftpe", 8)):
        html = capture(name)
        if html is None:
            skip("screenshots", f"captures/{name} absent")
            continue
        row = parser.parse_detail(html, package)
        ok &= check(f"[screenshots] {name}: {want} on the app page",
                    row is not None and row.screenshot_count == want,
                    str(row and row.screenshot_count))
    grid = capture("search_vpn_us.html")
    if grid is not None:
        rows = parser.parse_grid(grid)
        ok &= check("[screenshots] a grid tile still counts its own list",
                    rows and all((r.screenshot_count or 0) > 1 for r in rows[:5]),
                    str([r.screenshot_count for r in rows[:5]]))
    return ok


def test_run_contract_last_page_reason_survives():
    """A run's last page adding nothing is `no_new_products`, not `completed`.

    A line after the loop set `completed` whenever the page budget was met,
    which overwrote the true reason when the page that met it was the one
    that added nothing (30 / 20 / 0 rows over three pages in the report).
    """
    ok = True
    page1, shelf = capture("cat_game_us.html"), capture("cluster_us.html")
    if None in (page1, shelf):
        skip("run-contract", "category and cluster captures needed")
        return True
    rc, world, files = _run_main(
        ["--mode", "listing", "--category", "GAME", "--pages", "3"],
        [(page1, 200), (shelf, 200), (shelf, 200)])
    meta = json.loads(files.get(".meta.json", "{}"))
    ok &= check("[contract] three pages were fetched", len(world["fetches"]) == 3,
                str(len(world["fetches"])))
    ok &= check("[contract] the last page's reason survives",
                meta.get("stop_reason") == "no_new_products",
                str(meta.get("stop_reason")))
    ok &= check("[contract] and the run is still complete",
                rc == 0 and meta.get("status") == "complete",
                f"rc={rc} {meta.get('status')}")
    return ok


def test_run_contract_bad_gl_is_usage():
    """A country Play does not serve is the caller's mistake, not a block."""
    ok = True
    body = capture("bad_request_gl.html")
    if body is None:
        skip("run-contract", "captures/bad_request_gl.html absent")
        return True
    for status, why in ((400, "http_400"), (None, "bad_request_template")):
        state, reason = parser.detect_page_state(body, status, "", "grid")
        ok &= check(f"[gl] Play's 400 page with status {status} is bad_request",
                    (state, reason) == ("bad_request", why), f"{state} {reason}")

    # Malformed shape: refused by argparse before any browser starts.
    rc, world, _ = _run_main(["--mode", "listing", "--category", "GAME",
                              "--gl", "USA"], [(body, 400)])
    ok &= check("[gl] --gl USA is exit 2", rc == 2, f"rc={rc}")
    ok &= check("[gl] and launches nothing", world["launches"] == [],
                str(world["launches"]))

    # Well-shaped, but a code Play does not serve: its own 400.
    rc, world, files = _run_main(["--mode", "listing", "--category", "GAME",
                                  "--gl", "xx", "--retries", "2"], [(body, 400)])
    ok &= check("[gl] Play's 400 is exit 2, not 3", rc == 2, f"rc={rc}")
    ok &= check("[gl] and is not retried", len(world["fetches"]) == 1,
                f"{len(world['fetches'])} fetches")
    ok &= check("[gl] the message names the flag", "--gl XX" in world["log"],
                world["log"][-300:])
    ok &= check("[gl] no proxy advice for a typo", "[hint]" not in world["log"])
    ok &= check("[gl] no output written", not files, str(list(files)))
    return ok


def test_run_contract_output_paths():
    """`--out` with an extension, and paths into directories that do not exist."""
    ok = True
    page1 = capture("search_vpn_us.html")
    if page1 is None:
        skip("run-contract", "captures/search_vpn_us.html absent")
        return True
    argv = ["--mode", "search", "--query", "vpn"]

    rc, world, files = _run_main(argv, [(page1, 200)],
                                 out_name="nested/deeper/run.json",
                                 dump="dumps/here/p")
    written = world["written"]
    ok &= check("[out] the run succeeds into directories it had to create",
                rc == 0, f"rc={rc}")
    ok &= check("[out] --out run.json writes run.json, run.csv, run.meta.json",
                {"nested/deeper/run.json", "nested/deeper/run.csv",
                 "nested/deeper/run.meta.json"} <= set(written), str(written))
    ok &= check("[out] and no doubled suffix",
                not any(".json.json" in w or ".json.csv" in w for w in written),
                str(written))
    ok &= check("[out] --dump-html into a new directory writes its snapshot",
                any(w.startswith("dumps/here/p_") and w.endswith(".html")
                    for w in written), str(written))

    def make_blocker(tmp):
        with open(os.path.join(tmp, "blocker"), "w") as fh:
            fh.write("a file where a directory is wanted")
    rc, world, _ = _run_main(argv, [(page1, 200)], out_name="blocker/run",
                             prepare=make_blocker)
    ok &= check("[out] an --out that cannot be created is exit 2", rc == 2,
                f"rc={rc}")
    ok &= check("[out] and is refused before any browser starts",
                world["launches"] == [], str(world["launches"]))

    import engine_core
    with redirect_stderr(io.StringIO()):
        for given, want in (("x.json", "x"), ("x.CSV", "x"), ("x.meta.json", "x"),
                            ("run.2026", "run.2026"), (".json", ".json"), ("x", "x")):
            got = engine_core.output_prefix(given)
            ok &= check(f"[out] output_prefix({given!r}) == {want!r}", got == want, got)
    return ok


def test_run_contract_captcha_budget():
    """At most one paid solve per page, whatever retries and rotations do."""
    ok = True
    import captcha_solver
    calls = []
    saved = captcha_solver.solve_recaptcha

    def stub(challenge, key, api_version="v2", min_score=0.3):
        calls.append(challenge.sitekey)
        return "TOKEN"

    captcha_solver.solve_recaptcha = stub
    try:
        key = "a" * 31 + "b"
        with tempfile.TemporaryDirectory() as tmp:
            plist = os.path.join(tmp, "p.txt")
            with open(plist, "w") as fh:
                fh.write("http://10.0.0.1:1\nhttp://10.0.0.2:1\nhttp://10.0.0.3:1\n")
            # The page stays refused after the solve, and a pool of three
            # gives it three more fresh browsers. One purchase, not four.
            rc, world, _ = _run_main(
                ["--mode", "listing", "--category", "GAME", "--proxy-file",
                 plist, "--twocaptcha-key", key, "--retries", "2"],
                [(_BLOCKED_PAGE, 429)], discovery=_DISCOVERY)
        ok &= check("[captcha] one page buys at most one solve",
                    len(calls) == page_flow.SOLVES_PER_PAGE,
                    f"{len(calls)} solves, {len(world['fetches'])} fetches")
        ok &= check("[captcha] the token bought was injected",
                    world["injected"] == ["TOKEN"], str(world["injected"]))
        ok &= check("[captcha] and the page still refused is exit 3", rc == 3,
                    f"rc={rc}")

        # A solve that clears the page: the run continues on what it led to.
        calls.clear()
        grid = capture("cat_game_us.html")
        if grid is not None:
            rc, world, _ = _run_main(
                ["--mode", "listing", "--category", "GAME", "--twocaptcha-key",
                 key], [(_BLOCKED_PAGE, 200)], discovery=_DISCOVERY,
                after_solve=grid)
            ok &= check("[captcha] a cleared challenge yields the page's rows",
                        rc == 0 and len(calls) == 1, f"rc={rc} solves={len(calls)}")

        # No key: nothing bought, still exit 3.
        calls.clear()
        rc, world, _ = _run_main(["--mode", "listing", "--category", "GAME"],
                                 [(_BLOCKED_PAGE, 200)], discovery=_DISCOVERY)
        ok &= check("[captcha] no key, no purchase", not calls and rc == 3,
                    f"rc={rc} solves={len(calls)}")
        # --solve-captcha never: nothing bought even with a key.
        rc, world, _ = _run_main(["--mode", "listing", "--category", "GAME",
                                  "--twocaptcha-key", key, "--solve-captcha",
                                  "never"],
                                 [(_BLOCKED_PAGE, 200)], discovery=_DISCOVERY)
        ok &= check("[captcha] --solve-captcha never buys nothing", not calls,
                    f"solves={len(calls)}")
    finally:
        captcha_solver.solve_recaptcha = saved
    return ok


def test_cli_rejects_counts_that_cannot_run():
    """A count that cannot run is exit 2 naming the flag, in every engine.

    `--pages 0` used to fetch nothing and report exit 4 — the catalogue is
    empty — about a run that never asked. The engines share this CLI, so
    one check covers all three.
    """
    ok = True
    base = ["--mode", "listing", "--category", "GAME"]
    for flag, value in (("--pages", "0"), ("--pages", "-1"),
                        ("--reviews-per-call", "0"), ("--retries", "-1"),
                        ("--delay", "-1"), ("--retry-delay", "-2"),
                        ("--concurrency", "0"), ("--proxy-block-retries", "-1"),
                        ("--min-score", "2")):
        rc, world, _ = _run_main(base + [flag, value], [])
        ok &= check(f"[cli] {flag} {value} is a usage error naming the flag",
                    rc == 2 and "error:" in world["log"] and flag in world["log"]
                    and not world["fetches"],
                    f"rc={rc} {world['log'][-200:]}")
    rc, world, _ = _run_main(base + ["--retries", "0"],
                             [(capture("cat_game_us.html") or "", 200)])
    ok &= check("[cli] --retries 0 still makes one attempt",
                len(world["fetches"]) == 1, str(len(world["fetches"])))
    rc, world, _ = _run_main(base + ["--concurrency", "2"], [])
    ok &= check("[cli] --concurrency above 1 is refused, not ignored",
                rc == 2 and not world["fetches"], f"rc={rc}")
    rc, world, _ = _run_main(base + ["--fingerprint"], [])
    ok &= check("[cli] --fingerprint without a key is refused", rc == 2,
                f"rc={rc}")
    rc, world, _ = _run_main(base + ["--proxy-rotate", "on-block"], [])
    ok &= check("[cli] --proxy-rotate offers only what the pool accepts",
                rc == 2, f"rc={rc}")
    return ok


def test_env_url_does_not_override_a_typed_builder_flag():
    """GOOGLEPLAY_URL in `.env` must not replace a --category the user typed."""
    ok = True
    grid = capture("cat_game_us.html") or ""
    env = {"GOOGLEPLAY_URL": "https://play.google.com/store/apps/category/TOOLS"}
    rc, world, _ = _run_main(["--mode", "listing", "--category", "GAME"],
                             [(grid, 200)], env=env)
    asked = world["fetches"][0][0] if world["fetches"] else ""
    ok &= check("[env] --category GAME fetches GAME, not the .env URL",
                "/category/GAME" in asked, asked)
    rc, world, _ = _run_main(["--mode", "search", "--query", "vpn"],
                             [(capture("search_vpn_us.html") or "", 200)], env=env)
    ok &= check("[env] --mode search --query is not a usage error under it",
                rc != 2, f"rc={rc} {world['log'][-200:]}")
    return ok


def test_output_writes_are_atomic_and_keep_modes():
    """A failed write leaves the previous file whole; modes follow the umask."""
    ok = True
    import output_writer as ow
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "o.json")
        ow.write_json([App(sku="a", title="good")], path)
        before = open(path, encoding="utf-8").read()
        saved = ow.json.dump

        def explode(*a, **k):
            a[1].write('[{"partial": ')
            raise OSError("disk full")

        ow.json.dump = explode
        try:
            try:
                ow.write_json([App(sku="b")], path)
            except OSError:
                pass
            try:
                ow.write_run_meta(os.path.join(tmp, "o"), {"status": "x"})
            except OSError:
                pass
        finally:
            ow.json.dump = saved
        ok &= check("[write] an interrupted write leaves the old file whole",
                    open(path, encoding="utf-8").read() == before)
        ok &= check("[write] and no sidecar half-written",
                    not os.path.exists(os.path.join(tmp, "o.meta.json")))
        ok &= check("[write] and no temp file left behind",
                    sorted(os.listdir(tmp)) == ["o.json"], str(os.listdir(tmp)))

        for umask in (0o022, 0o002, 0o077):
            old = os.umask(umask)
            try:
                p = os.path.join(tmp, f"m{umask:o}.csv")
                ow.write_csv([App(sku="a")], p)
                mode = os.stat(p).st_mode & 0o777
            finally:
                os.umask(old)
            ok &= check(f"[write] a new file under umask {umask:03o} is "
                        f"{0o666 & ~umask:03o}", mode == 0o666 & ~umask,
                        f"{mode:03o}")
        p = os.path.join(tmp, "keep.json")
        ow.write_json([], p)
        os.chmod(p, 0o640)
        ow.write_json([App(sku="a")], p)
        ok &= check("[write] an existing file keeps its mode",
                    os.stat(p).st_mode & 0o777 == 0o640)
    return ok


def test_csv_neutralises_formula_cells():
    ok = True
    import csv as _csv
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "f")
        rows = [App(sku="a", title="=HYPERLINK(\"http://x\")", brand="+cmd|' /C calc'!A0",
                    description="@SUM(1)", price=-5.0, category="Arcade"),
                App(sku="b", title="\tTabbed", brand="-dash", description="ok")]
        buf = io.StringIO()
        with redirect_stdout(buf):
            output_writer.finish_run(rows, prefix, "both", False, blocked=False,
                                     stop_reason="completed", pages_requested=1,
                                     pages_completed=1, start_url="u",
                                     final_url="u", mode="listing",
                                     extra={"gl": "US"})
        with open(prefix + ".csv", encoding="utf-8", newline="") as fh:
            got = list(_csv.DictReader(fh))
        data = json.load(open(prefix + ".json", encoding="utf-8"))
        meta = json.load(open(prefix + ".meta.json", encoding="utf-8"))
    ok &= check("[csv] a formula cell is prefixed",
                got[0]["title"].startswith("'=") and got[0]["brand"].startswith("'+")
                and got[0]["description"].startswith("'@")
                and got[1]["title"].startswith("'\t")
                and got[1]["brand"] == "'-dash", str(got[0])[:200])
    ok &= check("[csv] a negative NUMBER stays a number", got[0]["price"] == "-5.0",
                got[0]["price"])
    ok &= check("[csv] a plain cell is untouched", got[0]["category"] == "Arcade")
    ok &= check("[csv] JSON keeps the site's bytes",
                data[0]["title"] == "=HYPERLINK(\"http://x\")")
    ok &= check("[csv] the sidecar counts the escaped cells",
                meta.get("csv_cells_escaped") == 5, str(meta.get("csv_cells_escaped")))
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "g")
        with redirect_stdout(io.StringIO()):
            output_writer.finish_run(rows, prefix, "csv", False, blocked=False,
                                     stop_reason="completed", pages_requested=1,
                                     pages_completed=1, start_url="u",
                                     final_url="u", mode="listing",
                                     extra={"csv_cells_escaped": "caller"})
        meta = json.load(open(prefix + ".meta.json", encoding="utf-8"))
    ok &= check("[csv] the count merges UNDER the caller's extra",
                meta.get("csv_cells_escaped") == "caller")
    return ok


def test_redaction_and_empty_documents():
    ok = True
    import engine_core
    text = engine_core._redact("ProxyError http://u:supersecret@1.2.3.4:8080 and "
                               "ws://login:supersecret@cb.2captcha.com:9222 key=abc")
    ok &= check("[redact] proxy and CDP credentials are masked",
                "supersecret" not in text and "abc" not in text,
                text)
    state, reason = parser.detect_page_state("", None, "", "grid")
    ok &= check("[state] an empty document is unknown, not blocked",
                state == "unknown", f"{state} ({reason})")
    return ok


def test_no_private_citations():
    """Public files state their rules in their own words.

    The tokens are ASSEMBLED from pieces so this file does not match itself.
    """
    ok = True
    banned = ("CLAUDE" + ".md", "claude_" + "2scraper", "2scraper_" + "repo_rules",
              "play" + "book", chr(0xA7))
    try:
        listed = subprocess.run(["git", "ls-files"], cwd=HERE, capture_output=True,
                                text=True, check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        listed = list(SHIPPED_TEXT)
    scanned = 0
    for name in listed:
        if name.startswith("captures/") or name.endswith((".png", ".ico")):
            continue
        text = _read(name)
        if text is None:
            continue
        scanned += 1
        for token in banned:
            ok &= check(f"[citations] {name} cites no private document",
                        token not in text, repr(token))
    ok &= check("[citations] the scan read something", scanned >= 10, str(scanned))
    return ok


def test_diff_refuses_two_markets_in_one_currency():
    """DE and FR are both EUR; their ratings are not comparable."""
    ok = True
    import argparse as _ap
    import diff_runs
    with tempfile.TemporaryDirectory() as tmp:
        paths = {}
        for gl in ("DE", "FR"):
            p = os.path.join(tmp, gl + ".json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump([{"sku": "a", "currency": "EUR", "gl": gl, "rating": 4.1}], fh)
            paths[gl] = p
        args = _ap.Namespace(old=paths["DE"], new=paths["FR"], force=False)
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            verdict = diff_runs._check_comparable(args)
        ok &= check("[diff] two markets sharing a currency are refused",
                    verdict is False and "different markets" in buf.getvalue(),
                    buf.getvalue()[-300:])
    return ok


def test_workflows_import_no_local_module_inline():
    """A workflow step calls ci_checks.py; it does not re-implement it.

    An inline heredoc that imports the row class is a second copy of a check,
    and nothing local ever runs a workflow, so it can only fail on a push.
    """
    ok = True
    wf_dir = os.path.join(HERE, ".github", "workflows")
    if not os.path.isdir(wf_dir):
        skip("workflows", "no .github/ in this tree (the image)")
        return True
    local = [n[:-3] for n in SHIPPED_PY]
    pattern = re.compile(r"^\s*(?:from|import)\s+(" + "|".join(local) + r")\b", re.M)
    for name in sorted(os.listdir(wf_dir)):
        text = _read(os.path.join(".github", "workflows", name)) or ""
        hit = pattern.search(text)
        ok &= check(f"[workflows] {name} imports no local module inline",
                    hit is None, hit.group(0).strip() if hit else "")
    return ok


def main():
    tests = [
        test_row_schema,
        test_detail_values,
        test_paid_app_values,
        test_the_market_is_a_property_of_the_row,
        test_grid_values,
        test_shelves_are_named,
        test_page_state_on_every_capture,
        test_markers_score_zero_on_good_pages,
        test_reviews_one_parser_two_routes,
        test_request_builder_matches_what_the_store_answers,
        test_newest_reviews_are_newest_first,
        test_fixtures_carry_no_real_reviewers,
        test_banned_wording,
        test_engines_share_the_cli_and_the_loops,
        test_engines_import_their_driver_at_module_level,
        test_no_statement_after_return,
        test_shared_module_calls_bind,
        test_policy_constants_have_consumers,
        test_env_example_matches_what_the_code_reads,
        test_a_copied_env_example_reads_as_unset,
        test_dockerfile_copies_what_the_entrypoint_imports,
        test_exit_codes,
        test_urls_and_refusals,
        test_price_and_number_parsing,
        test_engine_modules_import,
        test_sample_output_matches_the_schema,
        test_credential_scan_runs_and_is_wired,
        test_credential_scan_sees_into_a_virtualenv_and_a_fixture,
        test_gitignore_covers_every_env_spelling,
        test_scraper_api_waitfor_object_and_http_code,
        test_run_contract_proxy_and_partial_rows,
        test_run_contract_exit_codes_per_state,
        test_run_contract_reviews_refusal_is_not_an_end,
        test_run_contract_reviews_pages_chain_on_the_token,
        test_recaptcha_variant_on_googles_demos,
        test_captures_are_committable,
        test_screenshot_count_on_both_routes,
        test_run_contract_last_page_reason_survives,
        test_run_contract_bad_gl_is_usage,
        test_run_contract_output_paths,
        test_run_contract_captcha_budget,
        test_cli_rejects_counts_that_cannot_run,
        test_env_url_does_not_override_a_typed_builder_flag,
        test_output_writes_are_atomic_and_keep_modes,
        test_csv_neutralises_formula_cells,
        test_redaction_and_empty_documents,
        test_no_private_citations,
        test_diff_refuses_two_markets_in_one_currency,
        test_workflows_import_no_local_module_inline,
    ]
    ok = True
    for test in tests:
        ok &= bool(test())

    print()
    if SKIPS:
        print(f"{len(SKIPS)} skipped:")
        for line in SKIPS:
            print(f"  - {line}")
    if FAILURES:
        print(f"{len(FAILURES)} FAILED:")
        for line in FAILURES:
            print(f"  - {line}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
