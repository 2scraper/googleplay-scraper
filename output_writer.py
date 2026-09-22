"""
output_writer.py
-----------------
Shared row models + JSON/CSV writers used by all three scrapers.

Five modes, two row shapes
--------------------------
    --mode listing     a genre grid or an expanded shelf   -> App
    --mode search      a keyword query                     -> App
    --mode app         one /store/apps/details page        -> App, with the
                       detail-only columns populated
    --mode developer   one developer's grid                -> App
    --mode reviews     one app's reviews                   -> Review

The four app modes yield the SAME class, and on this site that is
load-bearing rather than tidy. Play states an app's identity as its Android
package id, it is in the URL of every route, and it does not change between
markets — so a listing row, a search row and an app row for one app carry a
byte-identical `sku` and a consumer can JOIN the files on it.

What that does NOT buy is a cross-mode DIFF, and `diff_runs.py` still
refuses one (`--force` overrides). The ids line up; the row SETS do not. A
64-row category run against a 1-row app run would report 63 apps removed and
every line of it would be an artefact of the two runs covering different
things.

`reviews` is the exception in both directions: it is a different KIND of
object, so it gets its own class, and many of its rows share one `sku`, so
it dedupes on `review_id` instead.

There is deliberately no `--mode books` or `--mode movies`. Both storefronts
exist and answer (measured 2026-09-22: 1.8 MB and 1.3 MB of served markup),
and neither has been captured, parsed or run — so a mode for them would ship
untested against markup nobody has read. `product_parser` refuses those URLs
with that reason rather than half-reading them.

`App` keeps the family's first eighteen columns in the family's order, with
Play's own appended after `position`, so a consumer written against another
repo in this family still reads the prefix unchanged.

Everything below is row-class-agnostic: pass `row_cls` so an empty CSV still
gets the right header for the mode that produced it.
"""

import csv
import json
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timezone
from typing import Optional, List, Set, Sequence, Any, Type


# The hostname a row came from. Every route this repo reads is on one host,
# so this column is `play.google.com` on every row of every run. Which ROUTE
# a row came from is recorded by `price_source`, and which MARKET it
# describes by `gl`/`hl` — on this site the market is not decoration, since
# the rating and the currency both follow it.
#
# It is kept in this position because the family's schema has it here and
# consumers read the columns by name across repos.
SOURCE_DEFAULT = "play.google.com"


@dataclass
class App:
    """One row per APP, in one market.

    The first eighteen fields are the family prefix, byte-identical and in
    the same order as every other repo in this family, so one consumer reads
    a googleplay run and a mediamarkt run with the same code. Everything
    after `position` is Google Play's own.

    `Product` is an alias for this class, kept because the family's shared
    CI imports that name.

    Four of the five modes produce this row (`listing`, `search`, `app`,
    `developer`); `reviews` produces `Review` instead.

    Two things about the prefix need saying out loud rather than being
    quietly wrong on this site:

      * **`rating` is a property of the MARKET, not of the app.** Google Play
        computes it per country and the same package comes back differently
        from each. Measured 2026-09-22 on `com.rovio.baba` from ONE address,
        changing only `gl`: US 4.08, DE 3.90, JP 3.96, IN 4.13, BR 4.49 —
        while `review_count` was 6,312,298 in all five. So a row is only
        meaningful together with the `gl`/`hl` it was read under, and those
        two are columns rather than run metadata. Joining rows from two
        markets on `sku` is correct; averaging their ratings is not.
      * **`brand` is the developer.** Play has no brand of its own, the
        developer is what a consumer actually groups by, and reusing the
        family column means cross-repo tooling keeps working. `developer_id`
        carries the id that `--mode developer` takes.

    `rating` is the figure the store DISPLAYS (4.6), not the float it ships
    beside it. Play publishes both, and its raw value is float32 noise that
    differs between the two places one page states it — 4.616471 in the page
    payload against 4.616447925567627 in the same page's JSON-LD, for one
    app in one fetch. Writing either through would make two runs, or two
    modes, diff on every row for no reason. `rating_raw` keeps the payload's
    own number so the rounding stays auditable.
    """
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # The app's canonical store URL, rebuilt from the package id rather than
    # taken from the page: Play's own anchors are relative and carry a
    # per-impression tracking tail, so two runs of one listing would never
    # produce the same string. `hl`/`gl` are preserved because the page they
    # address is market-specific.
    url: str = ""
    # The Android package id — `com.whatsapp`. Play's own identifier, stable
    # across markets and across all four app-producing modes, and present in
    # the URL of every route, so a listing row and an app row for the same
    # app carry a byte-identical `sku` and can be joined.
    sku: Optional[str] = None
    title: Optional[str] = None
    # The developer's display name, as the store prints it in this locale.
    brand: Optional[str] = None
    # 0.0 for a free app rather than None: Play states the offer explicitly
    # (`[[0, "USD", ""]]`), so zero here is a fact the site published, not a
    # missing read. An app whose offer node is absent gets None.
    price: Optional[float] = None
    currency: Optional[str] = None
    original_price: Optional[float] = None
    discount_pct: Optional[float] = None
    # The DISPLAYED star rating for this market — see the class docstring.
    rating: Optional[float] = None
    # The number of RATINGS, which is not the number of written reviews. Play
    # publishes both and they differ by two orders of magnitude: 244,140,056
    # against 1,952,258 on one app measured 2026-09-22. This column keeps the
    # family's meaning (how many people scored it); `review_text_count` holds
    # the other one.
    review_count: Optional[int] = None
    # Whether the store offers this app for install in this market. Left None
    # where the route does not say so rather than defaulted to True.
    in_stock: Optional[bool] = None
    # The app icon. Play serves every image off play-lh.googleusercontent.com
    # at a requested size; the URL is kept exactly as published.
    image_url: Optional[str] = None
    # The genre as the store displays it in this locale ("Arcade",
    # "Kommunikation"). It is display text and differs per language, so a
    # cross-market comparison joins on `genre_id`, never on this.
    category: Optional[str] = None
    # Which route produced this row: "listing" (a category, search or
    # developer grid tile), "detail" (an app page's own payload), or
    # "detail+jsonld" where the app page's JSON-LD confirmed it.
    price_source: Optional[str] = None
    page: Optional[int] = None
    position: Optional[int] = None

    # ---- Google Play's own ----------------------------------------------
    # The market this row describes. Load-bearing rather than metadata,
    # because `rating`, `price` and `currency` are all per-market.
    gl: Optional[str] = None
    hl: Optional[str] = None
    # The developer id `--mode developer` takes. Play publishes two shapes —
    # a numeric id (`5700313618786177705`) and a name — and both address the
    # same grid; whichever the page carried is what lands here.
    developer_id: Optional[str] = None
    # The locale-independent genre key ("GAME_ARCADE"). This is what a
    # cross-market join uses.
    genre_id: Optional[str] = None
    # The install count exactly as displayed ("1,000,000,000+") and the
    # integer floor it states. Play publishes no exact figure on any route
    # measured, so the floor is the honest reading of a bucketed number.
    installs: Optional[str] = None
    installs_min: Optional[int] = None
    # Play's EXACT install count, which it publishes beside the bucketed one
    # on the app route and nowhere else: 12,322,703,000 against a displayed
    # "10,000,000,000+". Measured 2026-09-22 across 330 distinct listing
    # tiles from six captures: zero carry it, so this column is null on every
    # listing, search and developer row by the site's own design rather than
    # by a missing read.
    installs_exact: Optional[int] = None
    content_rating: Optional[str] = None
    # The in-app purchase price range as the store prints it
    # ("$0.99 - $99.99 per item"), and whether one was published at all.
    #
    # There is deliberately no `contains_ads` column. The phrase "Contains
    # ads" appears zero times in both app captures taken 2026-09-22, and
    # "In-app purchases" appears exactly once in BOTH — including on an app
    # that publishes no IAP range — so the rendered labels are template text
    # and cannot be read as flags. A column that is wrong on every row is
    # worse than a missing one; add it when a payload field is measured.
    iap_price_range: Optional[str] = None
    offers_iap: Optional[bool] = None
    # The payload's own float, kept for provenance against `rating`.
    rating_raw: Optional[float] = None
    # How many people wrote text, as opposed to how many scored it.
    review_text_count: Optional[int] = None
    # The five star buckets, one column each, published only on the app
    # route. Null on a listing row — `price_source` says which route this is.
    histogram_1: Optional[int] = None
    histogram_2: Optional[int] = None
    histogram_3: Optional[int] = None
    histogram_4: Optional[int] = None
    histogram_5: Optional[int] = None
    # App route only.
    version: Optional[str] = None
    updated_at: Optional[str] = None
    description: Optional[str] = None
    developer_email: Optional[str] = None
    developer_website: Optional[str] = None
    privacy_policy_url: Optional[str] = None
    screenshot_count: Optional[int] = None
    # Which named shelf on the page this row came from ("Top free",
    # "Top grossing"), and the row's 1-based place inside it. Play's own
    # ordering is the product here: the same category page carries several
    # shelves, an app can sit in more than one, and `position` alone cannot
    # say which ranking it describes. Null on routes that publish no shelf.
    shelf: Optional[str] = None
    shelf_rank: Optional[int] = None
    # What the APP page says about its own standing — Play prints "#1" next
    # to "top free communication" — as opposed to `shelf`/`shelf_rank`, which
    # are this parser's count of where the row sat in a grid we fetched. Two
    # separate pairs on purpose: one is the site's claim, the other is our
    # observation, and collapsing them would present the second as the first.
    chart_name: Optional[str] = None
    chart_rank: Optional[int] = None


# The family's shared tooling and CI import the name `Product`. This repo
# reads apps, so the class is called `App` and the family name stays as an
# alias rather than as a second definition that could drift.
Product = App


@dataclass
class Review:
    """One row per REVIEW, produced only by `--mode reviews`.

    A second row class rather than a widened `App`, for the reason
    amazon-scraper needed one: many rows share a `sku`, so `review_id` is
    what is unique and what dedupe and `diff_runs.py` must key on.

    One parser fills this from two places that cannot drift, because they are
    the same structure: the first page of reviews is embedded in the app
    page's own payload, and every page after it comes from Play's
    `batchexecute` endpoint. Measured 2026-09-22: 40 reviews per call, a
    continuation token on every page, page 2 sharing none of page 1's ids.

    **These rows are about identifiable people.** Play publishes a reviewer's
    display name, avatar URL and a stable numeric profile id beside the text.
    `--no-authors` drops all three, and the committed fixtures carry
    placeholders in their place.
    """
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    url: str = ""
    # The app the review is about, so a reviews run joins onto an apps run.
    sku: Optional[str] = None
    # Play's own review uuid. Unique, and what this mode dedupes on.
    review_id: Optional[str] = None
    score: Optional[int] = None
    text: Optional[str] = None
    # ISO 8601, converted from the payload's epoch-seconds pair.
    posted_at: Optional[str] = None
    thumbs_up: Optional[int] = None
    # The app version the reviewer had installed, where Play states it.
    app_version: Optional[str] = None
    developer_reply: Optional[str] = None
    developer_replied_at: Optional[str] = None
    author_name: Optional[str] = None
    author_id: Optional[str] = None
    author_image: Optional[str] = None
    gl: Optional[str] = None
    hl: Optional[str] = None
    page: Optional[int] = None
    position: Optional[int] = None


# Four modes describe an app; one describes a review. The mapping is what
# keeps an empty CSV's header right for the mode that produced it.
ROW_CLASS_BY_MODE = {
    "listing": App,
    "search": App,
    "app": App,
    "developer": App,
    "reviews": Review,
}

# Modes whose rows are one-per-sku, and therefore safe to dedupe on `sku`
# and to hand to diff_runs.py. `reviews` is deliberately absent: many of its
# rows share a `sku`, so deduping it on that key would keep one review per
# app and throw the rest away.
UNIQUE_BY_SKU_MODES = ("listing", "search", "app", "developer")


def dedupe_by_key(rows: Sequence[Any], seen: Set[str], key: str = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages —
    a stale or repeating next-page link then re-parses a page without
    duplicating its rows into the final output. On this site this DOES fire
    on healthy runs: page 1 and page 2 of one category listing shared
    exactly 3 products, all three from the "cheaper products" carousel that
    appears on every page of a listing. So a small non-zero drop count here
    is expected and a large one is not.

    A row with no key is always kept: there is nothing to check a duplicate
    against, and dropping it would be a silent data loss rather than a
    duplicate removal.

    Both of this repo's modes are one row per `sku`, so `key` is never
    overridden here — the parameter exists because the rest of the family
    shares this function and one of them needs it.
    """
    fresh = []
    for r in rows:
        val = getattr(r, key, None)
        if val is None or val not in seen:
            if val is not None:
                seen.add(val)
            fresh.append(r)
    return fresh


# Kept under its old name: the engines and smoke tests in this family all
# call it, and a listing run does dedupe by sku.
def dedupe_by_sku(rows: Sequence[Any], seen: Set[str]) -> List[Any]:
    return dedupe_by_key(rows, seen, key="sku")


# CSV cannot hold a list. Joining with " | " keeps the cell readable in a
# spreadsheet and round-trippable by splitting on the same separator; the
# JSON output keeps the real list, so nothing is lost for a consumer that
# wants structure. `repr()` of a Python list (the default if this is not
# handled) is neither readable nor parseable by anything but Python.
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    # `tags` and each entry of `variants` are mappings, and a Python repr
    # of one is neither readable in a spreadsheet nor parseable by anything
    # but Python.
    # Compact JSON is both, and round-trips through json.loads.
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    return v


def write_json(rows: Sequence[Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = Product) -> None:
    # An empty result still gets the header row. A zero-byte file makes a
    # consumer fail on read (no columns to parse) instead of reading a valid
    # table with zero rows — and "an empty result is still a well-formed
    # result" is the same principle as `save` refusing to overwrite good data.
    #
    # The header comes from `row_cls`, not from the first row, so an empty
    # run still writes the columns of the mode that produced it.
    fieldnames = [f.name for f in fields(row_cls)]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: _csv_value(v) for k, v in asdict(r).items()})


# Exit code used when a run completes but produced nothing. Distinct from 1
# (crash) so a caller can tell "ran, found nothing" from "blew up".
EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked by a bot-check/challenge page before parsing
# even started — distinct from EXIT_NO_PRODUCTS so a caller can tell "the
# search genuinely matched nothing" from "something stood between us and the
# content". See product_parser.detect_bot_challenge.
#
# On this site this code specifically does NOT cover the three ways to get a
# real page with no products on it: a `/p/<slug>` discovery hub, which
# answers 200 with banners and carousels and no grid; a search whose query
# matches nothing ("Oops, produk nggak ditemukan"); and one page past the
# end of a category listing. All three are EXIT_NO_PRODUCTS — the request
# was served exactly as asked and simply has no products on it. Reporting
# any of them as blocked would send a user hunting for a proxy problem that
# does not exist.
#
# What EXIT_BLOCKED means here is unusually literal: this site refuses a
# address it has scored NOTHING at all. No status code, no interstitial, no
# vendor marker — the HTTP/2 stream is reset and the run sees a connection
# error rather than a page.
# Bad usage — a flag combination or a missing argument the run cannot
# proceed on. argparse already exits 2 for a malformed command line; this
# names the same number so a hand-written usage error agrees with it instead
# of returning a bare literal. The family's contract has always had this
# code; no repo in it had a name for it.
EXIT_USAGE = 2

EXIT_BLOCKED = 3

# Exit code for a run that gathered SOME rows and then stopped early — a
# page-load timeout, a 503 throttle, or a challenge on page 3 of 10. The
# output file is still written (throwing away three good pages would be
# worse), but it is not a complete picture, and a consumer that cannot tell
# the difference will read the pages that were never fetched as products that
# disappeared from the catalogue. See write_run_meta.
# A REMOTE service failed — the Scraping Browser refusing the connection
# (`profile_locked` is the common one: a profile allows a single live
# connection), or the Scraper API answering an error. Distinct from 1 (a
# crash in this code) and from 2 (bad usage) because it means "try again, or
# use a different profile", not "there is a bug here". Defined once, here,
# because the browser engines and scraper_api_client.py both return it and
# two definitions of the same code is exactly how a family's exit contract
# drifts.
EXIT_API_ERROR = 5

EXIT_PARTIAL = 6


# Exit code for a run that never GOT its pages: a navigation timeout, a dead
# or unauthenticated proxy, a DNS failure, or an edge answering with
# something that is not the page that was asked for.
#
# Distinct from EXIT_NO_PRODUCTS because those are opposite facts. Exit 4 is
# a statement about the CATALOGUE — "we asked, and the answer was nothing" —
# so handing it to a run that never reached the site tells a pipeline the
# listing is empty when nothing was read at all.
#
# 5 rather than a new number, and 5 rather than EXIT_PARTIAL:
#
#   * this family's contract already reserves 5 for a transport failure
#     (scraper_api_client has used it for a remote API error since it was
#     written), so this needs no new code and no per-repo table for a caller
#     driving more than one of these scrapers;
#   * EXIT_PARTIAL (6) means "some rows were gathered and the output is
#     incomplete". A run holding nothing writes no output at all, so a
#     consumer that reads the file on a 6 finds either nothing or the
#     PREVIOUS run's good data, which `save` deliberately does not
#     overwrite. Exit 5 promises no file.
#
# Deliberately NOT applied when rows WERE gathered: a timeout on page 7 of
# 10 is a partial run (exit 6, output written), which is already right. This
# decides only what a run holding nothing reports.
EXIT_FETCH_FAILED = 5


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write a run-metadata sidecar next to the output, return its path.

    Deliberately a separate `<out>.meta.json` rather than columns on every
    row: this describes the RUN, not the product, and repeating it across
    every row would both bloat the output and change the schema every
    consumer of this project already parses.

    diff_runs.py reads it to refuse a comparison between runs that are not
    both complete, and between runs of different `mode`.
    """
    path = f"{out_prefix}.meta.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             mode: str = "listing", source: str = SOURCE_DEFAULT,
             extra: Optional[dict] = None) -> dict:
    """Build the metadata dict for a finished run.

    `status` is the field a consumer branches on:
      complete — every requested page was fetched, or the site's own
                 pagination genuinely ran out (nothing more existed to get)
      partial  — rows were gathered, then the run stopped early
      failed   — nothing was gathered at all

    `mode` and `source` are recorded because `mode` is not implied by the
    repo: the same output prefix can hold a listing run or a product run,
    and those populate different columns — `sold` is a FLOOR on a listing
    row and exact on a product row, so diffing one against the other would
    report every row as changed. diff_runs.py refuses a pair whose modes or
    sources differ. `source` is `play.google.com` on every row of every run
    here, since the site has one storefront and one currency; it is kept
    because consumers read these columns by name across the family.

    `extra` carries facts about the run that are not about any single row.
    `--mode shop` uses it for the SELLER's own name, location, rating and
    review count: a run covers exactly one shop, so those belong to the run
    rather than repeated down a column, and the shop's review count (16679
    on the captured seller) is a different number from its listings' own
    (827 on one of them) — putting them in one column would make the schema
    lie.

    `pages_failed` lists the pages that did not yield data, by number.
    `pages_completed` alone was enough only while pages were fetched strictly
    in order, where "3 of 10 completed" could only mean 1-2-3: a count is not
    a description once pages can be fetched independently and page 3 can fail
    while 4 and 5 succeed. Recording the numbers keeps the sidecar honest
    about WHICH part of the catalogue is missing, not just how much.
    """
    meta = {
        "source": source,
        "mode": mode,
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        "products": products,
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        # Merged rather than nested under a key, so a consumer reads
        # `shop_rating` at the top level beside `products`. Run fields win a
        # name collision: a caller cannot accidentally overwrite `status`.
        meta.update({k: v for k, v in extra.items() if k not in meta})
    return meta


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = Product) -> int:
    """Write JSON/CSV and return a process exit code.

    Returns 0 when rows were written, EXIT_NO_PRODUCTS when there were none.
    Callers are expected to exit with it.

    On zero rows, nothing is written at all unless `allow_empty`. Two reasons,
    and a live run demonstrated both. A page-load timeout produced
    `Saved 0 products -> out.json` and exit 0: a two-byte `[]` that a
    consuming pipeline reads as a successful run with no stock. Worse, if the
    file already held a good result from an earlier run, that result is now
    gone — the failure destroyed the last known good data. So an empty result
    leaves the previous file intact and says why.

    `allow_empty=True` is for the legitimate case: a filter that genuinely
    matches nothing, where an empty file is the answer.
    """
    if not rows and not allow_empty:
        print(f"[!] 0 products — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected answer.")
        return EXIT_NO_PRODUCTS

    if fmt in ("json", "both"):
        write_json(rows, f"{out_prefix}.json")
        print(f"[+] Saved {len(rows)} products -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        write_csv(rows, f"{out_prefix}.csv", row_cls=row_cls)
        print(f"[+] Saved {len(rows)} products -> {out_prefix}.csv")
    return 0 if rows else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see. Anything
# else ended the page loop early, so the result is only a partial view.
#
# "no_new_products" belongs here and "pagination_exhausted" is kept for the
# engines that still stop on a missing next-link: the first is a property of
# the DATA (a page contributed nothing not already seen, so the listing is
# over), while the second is a property of a CSS SELECTOR and is therefore
# the weaker signal — a renamed attribute looks identical to a short
# catalogue. On this site that ordering is not a preference, it is the only
# thing that works: the site publishes NO `link[rel=next]` and no numbered
# anchors anywhere, a CATEGORY listing is addressable by `?page=N`, and a
# SEARCH is not addressable at all — `?page=2` there returns an empty result
# set rather than page 2. So "no new products" is the one termination
# condition available on a search. See page_flow.pagination_is_addressable.
#
# "single_page_mode" is complete by construction: --mode product reads one
# page because one page is all there is.
# `end_of_listing` is a COMPLETE result and leaving it out of this tuple is a
# bug worth naming, because it produced exit 6 for a correct run. On this
# site a listing does not end with an error or an empty page: Google Play answers
# a request past the last page by serving page 1 again under HTTP 200, and
# the engine detects that from the offset the server states rather than from
# its own request. Having found the real end of the results, the run has
# everything the site will give it.
COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted", "no_new_products",
                         "single_page_mode", "end_of_listing")


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               mode: str = "listing", source: str = SOURCE_DEFAULT,
               extra: Optional[dict] = None) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all three browser engines so the status/exit-code mapping
    cannot drift between them.

    The metadata sidecar is written ONLY when the row file was written.
    Otherwise a failed run would leave a "status": "failed" sidecar next to
    the previous run's still-intact good output (which `save` deliberately
    does not overwrite) — the two files would contradict each other, and
    diff_runs.py would refuse to compare data that is in fact fine.
    """
    complete = stop_reason in COMPLETE_STOP_REASONS
    row_cls = ROW_CLASS_BY_MODE.get(mode, Product)
    rc = save(rows, out_prefix, fmt, allow_empty=allow_empty, row_cls=row_cls)
    wrote_output = bool(rows) or allow_empty

    if wrote_output:
        status = "complete" if (rows and complete) else (
            "partial" if rows else "failed")
        write_run_meta(out_prefix, run_meta(
            status=status, stop_reason=stop_reason,
            pages_requested=pages_requested, pages_completed=pages_completed,
            pages_failed=pages_failed, mode=mode, source=source,
            start_url=start_url, final_url=final_url, products=len(rows),
            extra=extra))

    if not rows:
        # Nothing gathered at all, and WHY decides the code. The three
        # outcomes are different facts and a pipeline branches on them
        # (blocked is not empty is not "never reached"):
        #
        #   blocked            something stood between the run and the content
        #   did not complete   we never got the pages — a dead proxy, a load
        #                      timeout, an edge serving something else
        #   completed          we asked, and the answer was nothing
        #
        # Keyed on `not complete` rather than on a list of stop reasons, on
        # purpose: a list cannot cover a reason nobody has added to it yet,
        # so a new one falls silently through to "the catalogue is empty" —
        # which is the defect this branch exists to prevent.
        if blocked:
            return EXIT_BLOCKED
        if not complete:
            print(f"[!] Nothing was gathered and the run did not finish "
                  f"({stop_reason}) — exit {EXIT_FETCH_FAILED}, NOT an empty "
                  f"result (exit {EXIT_NO_PRODUCTS}). Nothing can be "
                  f"concluded about the catalogue from this run.")
            return EXIT_FETCH_FAILED
        return rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
