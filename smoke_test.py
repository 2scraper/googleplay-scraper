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
              if n.endswith(".html") and n != "notfound.html"]
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
    nf = capture("notfound.html")
    if nf is not None:
        ok &= check("[marker] the store's 404 carries no image-host references",
                    parser.asset_reference_count(nf) == 0,
                    str(parser.asset_reference_count(nf)))
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
    ok &= check("[req] sort key 2 — the only one that returns anything",
                "[2,null,[40,null,null]" in form["f.req"], form["f.req"])
    ok &= check("[req] the app id and Play's type tag",
                '[\\"com.whatsapp\\",7]' in form["f.req"], form["f.req"])
    # Measured: asking for score 1 and score 5 each returned forty reviews
    # spanning all five. A filter that changes nothing must not be offered.
    ok &= check("[req] no score filter is sent",
                "score_filter" not in str(parser.build_reviews_request.__doc__ or "")
                or "does NOTHING" in (parser.build_reviews_request.__doc__ or ""))
    ok &= check("[req] only the measured sort is offered",
                set(parser.REVIEW_SORTS) == {"newest"}, str(parser.REVIEW_SORTS))
    with_token = parser.build_reviews_request("com.whatsapp", 40, "TOKEN", "newest")
    ok &= check("[req] a token goes into the pagination slot",
                "[40,null,\\\"TOKEN\\\"]" in with_token["f.req"], with_token["f.req"])
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
    ok = True
    consumers = "\n".join(filter(None, (_read(n) for n in SHIPPED_PY
                                        if n != "page_flow.py")))
    own = _read("page_flow.py") or ""
    for constant in ("RETRY_ON_BLOCKED", "BLOCK_RETRIES_WITHOUT_POOL",
                     "SOLVES_PER_PAGE", "SEARCH_IS_PAGINATED",
                     "THROTTLE_BACKOFF_MS"):
        used_elsewhere = constant in consumers
        used_via_helper = own.count(constant) >= 2
        ok &= check(f"[policy] {constant} is actually read",
                    used_elsewhere or used_via_helper,
                    "no consumer outside its own definition")
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
    documented = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", text, re.M))
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
            # ...while a non-credential default stays usable.
            url = env_config.env_value("GOOGLEPLAY_URL")
        ok &= check("[env] and keeps the non-credential default usable",
                    bool(url) and "play.google.com" in url, repr(url))
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
