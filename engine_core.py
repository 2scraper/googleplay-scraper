"""
engine_core.py
--------------
The CLI, the mode loops and `main()` — everything the three engines must
agree about, in one place instead of three.

Why this is shared rather than copied
-------------------------------------
The family's rule is that all engines must agree on exit codes, run status,
and whether a run crashes or spends money. Every repo in this family
enforces that with a test that compares three hand-maintained copies, and
every repo in this family has at some point found the copies disagreeing —
twelve flags on one engine and not its twins in one audit, a triage that
reported exit 3 on one engine and exit 0 on another for the same page.

So here the agreement is structural. An engine supplies a DRIVER: how to
launch a browser, how to fetch a URL, how to POST a form. It supplies no
policy, no flags and no loop. A flag added here appears in all three at once
and cannot appear in only one, which is the property the test was
approximating.

What an engine must implement
-----------------------------
    class Driver:
        name: str
        def fetch(url, mode) -> (html, status)
        def post_form(url, form) -> str
        def current_url() -> str
        def close() -> None

and a `launch(args, proxy_url) -> Driver`.

Deliberately no JavaScript crosses this boundary — see `page_flow`. The
driver is asked for named OPERATIONS, never handed a snippet, because
Selenium's `execute_script` takes a function body with an explicit `return`
while Playwright and pyppeteer take `() => expr`.
"""

import argparse
import re
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import env_config
import page_flow
import product_parser as parser
from output_writer import App, Review, finish_run, dedupe_by_key, EXIT_USAGE
from proxy_pool import ProxyPool, mask, from_args as proxy_from_args

DEFAULT_TIMEOUT_MS = 45_000
DEFAULT_OUT_PREFIX = "googleplay_apps"


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Scrape Google Play listings, apps, developers and reviews.")
    ap.add_argument("--mode", default="listing", choices=list(parser.MODES),
                    help="what to read. listing/search/developer read a grid; "
                         "app reads one store page; reviews pages an app's "
                         "reviews.")
    ap.add_argument("--url", help="a play.google.com URL; its route decides "
                                  "the mode unless --mode says otherwise")
    ap.add_argument("--category", help="a Play category key, e.g. GAME or TOOLS")
    ap.add_argument("--query", help="a search query (--mode search)")
    ap.add_argument("--app-id", help="an Android package id, e.g. com.whatsapp")
    ap.add_argument("--developer", help="a developer id or name")
    ap.add_argument("--gl", default="US",
                    help="the MARKET. Changes the rating, the currency and "
                         "which apps are listed. Default US.")
    ap.add_argument("--hl", default="en", help="the display language. Default en.")
    ap.add_argument("--pages", type=int, default=1,
                    help="how many fetches to make. On a grid this follows "
                         "the page's own shelves; on reviews it follows the "
                         "continuation token.")
    ap.add_argument("--reviews-per-call", type=int,
                    default=parser.REVIEWS_PER_CALL,
                    help=f"reviews to ask for per call (default "
                         f"{parser.REVIEWS_PER_CALL}; no ceiling was found up "
                         f"to 2000)")
    ap.add_argument("--sort", default=parser.DEFAULT_REVIEW_SORT,
                    choices=sorted(parser.REVIEW_SORTS),
                    help="review ordering. Only 'newest' is implemented: the "
                         "store's other two keys return an empty payload in "
                         "this request shape.")
    ap.add_argument("--no-authors", action="store_true",
                    help="drop reviewer name, profile id and avatar from "
                         "review rows")
    ap.add_argument("--format", default="json", choices=("json", "csv", "both"))
    ap.add_argument("--out", default=DEFAULT_OUT_PREFIX)
    ap.add_argument("--delay", type=float, default=1.5,
                    help="seconds between fetches")
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--retry-delay", type=float, default=3.0)
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--proxy", help="a single proxy URL")
    ap.add_argument("--proxy-file", help="a file of proxy URLs, one per line")
    ap.add_argument("--proxy-rotate", default="per-page",
                    choices=("per-page", "per-run", "on-block"))
    ap.add_argument("--proxy-shuffle", action="store_true")
    ap.add_argument("--proxy-block-retries", type=int, default=None)
    ap.add_argument("--twocaptcha-key")
    ap.add_argument("--captcha-api", default="v2", choices=("v2", "v1"))
    ap.add_argument("--solve-captcha", default="when-blocked",
                    choices=("never", "when-blocked", "always"))
    ap.add_argument("--min-score", type=float, default=0.3)
    ap.add_argument("--cdp-endpoint")
    ap.add_argument("--fingerprint", action="store_true")
    ap.add_argument("--fp-country")
    ap.add_argument("--fp-tags", default="Windows")
    ap.add_argument("--locale")
    ap.add_argument("--allow-empty", action="store_true")
    ap.add_argument("--dump-html", metavar="PREFIX",
                    help="write each fetched page; on success too, not only "
                         "on failure")
    headless = ap.add_mutually_exclusive_group()
    headless.add_argument("--headless", dest="headless", action="store_true",
                          default=True)
    headless.add_argument("--headful", dest="headless", action="store_false")
    return ap


def resolve_target(args: argparse.Namespace) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """`(mode, url, error)` — what this invocation is actually asking for."""
    if args.url:
        reason = parser.refusal_reason(args.url)
        if reason:
            return None, None, reason
        mode = parser.mode_for_url(args.url)
        if args.mode and args.mode != "listing" and mode != args.mode:
            # An explicit --mode that disagrees with the URL is a mistake
            # worth naming rather than silently resolving one way.
            return None, None, (
                f"--mode {args.mode} disagrees with {args.url}, which is a "
                f"{mode} URL")
        return mode, parser.with_market(args.url, args.hl, args.gl), None

    if args.mode == "listing":
        if not args.category:
            return None, None, "--mode listing needs --category (e.g. GAME) or --url"
        return "listing", parser.listing_url(args.category, args.hl, args.gl), None
    if args.mode == "search":
        if not args.query:
            return None, None, "--mode search needs --query"
        return "search", parser.search_url(args.query, args.hl, args.gl), None
    if args.mode in ("app", "reviews"):
        if not args.app_id:
            return None, None, f"--mode {args.mode} needs --app-id (e.g. com.whatsapp)"
        return args.mode, parser.app_url(args.app_id, args.hl, args.gl), None
    if args.mode == "developer":
        if not args.developer:
            return None, None, "--mode developer needs --developer"
        return "developer", parser.developer_url(args.developer, args.hl, args.gl), None
    return None, None, f"unknown mode {args.mode}"

class RunOutcome:
    """What a mode's loop achieved, in the terms `finish_run()` needs."""

    def __init__(self) -> None:
        self.rows: List[Any] = []
        self.pages_completed = 0
        self.pages_failed: List[int] = []
        self.stop_reason = "completed"
        self.blocked = False
        self.final_url = ""
        self.extra: Dict[str, Any] = {}


def _handle_state(out: RunOutcome, state: str, reason: str, page_num: int) -> bool:
    """Record what a page turned out to be. Returns True if it is parseable."""
    if page_flow.should_parse(state):
        return True
    out.pages_failed.append(page_num)
    if page_flow.counts_as_blocked(state):
        out.blocked = True
        out.stop_reason = f"blocked:{reason}"
    elif state == "not_found":
        out.stop_reason = "not_found"
    elif state == "empty":
        out.stop_reason = "empty_page"
    else:
        out.stop_reason = f"unrecognised:{reason}"
    return False


def run_grid(driver, start_url: str, mode: str,
             args: argparse.Namespace) -> RunOutcome:
    """listing / search / developer.

    Page 1 is always fetched alone, because its shelves are what make pages
    2..N addressable at all. The loop then follows those shelves, and stops
    on DATA — a fetch that added no new package id — never on a selector.
    """
    out = RunOutcome()
    queue: List[str] = [start_url]
    visited: List[str] = []
    seen_skus: set = set()

    while queue and out.pages_completed < args.pages:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.append(url)
        page_num = out.pages_completed + 1

        html, status = driver.fetch(url, mode)
        _dump(args, f"{mode}_p{page_num}", html)
        state, reason = page_flow.classify(html, status, url, mode)
        out.final_url = driver.current_url()

        if not _handle_state(out, state, reason, page_num):
            if not page_flow.should_retry(state):
                break
            continue

        rows = parser.parse_grid(html, hl=args.hl, gl=args.gl, page=page_num)
        fresh = dedupe_by_key(rows, seen_skus, key="sku")
        out.rows.extend(fresh)
        out.pages_completed += 1

        if page_flow.stop_because_no_new(len(fresh), mode) and page_num > 1:
            out.stop_reason = "no_new_products"
            break

        for candidate in page_flow.next_page_candidates(url, html, args.hl,
                                                        args.gl, visited):
            if candidate not in queue:
                queue.append(candidate)

        if not queue:
            out.stop_reason = "end_of_listing"
        if out.pages_completed < args.pages and queue:
            time.sleep(args.delay)

    if out.pages_completed >= args.pages and not out.pages_failed:
        out.stop_reason = "completed"
    out.extra["shelves_seen"] = len(visited)
    return out


def run_app(driver, url: str, args: argparse.Namespace) -> RunOutcome:
    """One store page -> one row, with the detail-only columns filled."""
    out = RunOutcome()
    html, status = driver.fetch(url, "app")
    _dump(args, "app", html)
    out.final_url = driver.current_url()
    state, reason = page_flow.classify(html, status, url, "app")
    if not _handle_state(out, state, reason, 1):
        return out

    package = parser.package_from_url(url)
    row = parser.parse_detail(html, package, hl=args.hl, gl=args.gl, page=1,
                              position=1)
    if row is None:
        # Served, links to an app, and parsed to nothing: that is OUR bug,
        # and reporting it as "no products" would send the reader to check
        # the URL instead of the parser.
        out.stop_reason = "parser_found_nothing"
        out.pages_failed.append(1)
        return out

    for note in parser.jsonld_disagreements(row, html):
        print(f"[warn] payload and JSON-LD disagree — {note}", file=sys.stderr)

    out.rows.append(row)
    out.pages_completed = 1
    return out


def run_reviews(driver, url: str, args: argparse.Namespace) -> RunOutcome:
    """One app's reviews.

    Strictly sequential, and that is a property of the site rather than a
    choice: page N+1's address is a continuation token inside page N's
    response, so there is nothing for a second worker to fetch.

    Page 1 comes out of the app page's own payload — no second request — and
    every page after it from `batchexecute`. Both are the same structure.
    """
    out = RunOutcome()
    package = parser.package_from_url(url)
    if not package:
        out.stop_reason = "no_app_id"
        return out

    html, status = driver.fetch(url, "reviews")
    _dump(args, "reviews_p1", html)
    out.final_url = driver.current_url()
    state, reason = page_flow.classify(html, status, url, "reviews")
    if not _handle_state(out, state, reason, 1):
        return out

    seen: set = set()
    records = parser.embedded_reviews(html)
    out.rows.extend(_review_rows(records, package, args, page_num=1,
                                 offset=0, seen=seen))
    out.pages_completed = 1

    token: Optional[str] = None
    endpoint = parser.batchexecute_url(args.hl, args.gl)
    while out.pages_completed < args.pages:
        page_num = out.pages_completed + 1
        form = parser.build_reviews_request(
            package, count=args.reviews_per_call, token=token, sort=args.sort)
        try:
            body = driver.post_form(endpoint, form)
        except Exception as exc:  # a driver-specific transport failure
            out.pages_failed.append(page_num)
            out.stop_reason = f"batchexecute_failed:{type(exc).__name__}"
            break
        payload = parser.parse_batchexecute(body)
        records, token = parser.reviews_from_payload(payload)
        fresh = _review_rows(records, package, args, page_num=page_num,
                             offset=len(out.rows), seen=seen)
        if not fresh:
            out.stop_reason = "end_of_listing"
            break
        out.rows.extend(fresh)
        out.pages_completed += 1
        if not token:
            out.stop_reason = "end_of_listing"
            break
        time.sleep(args.delay)

    if out.pages_completed >= args.pages and not out.pages_failed:
        out.stop_reason = "completed"
    out.extra["reviews_per_call"] = args.reviews_per_call
    out.extra["authors_included"] = not args.no_authors
    return out


def _review_rows(records: Sequence[Any], package: str,
                 args: argparse.Namespace, page_num: int, offset: int,
                 seen: set) -> List[Review]:
    rows: List[Review] = []
    for i, record in enumerate(records):
        row = parser.parse_review_record(
            record, package, hl=args.hl, gl=args.gl, page=page_num,
            position=offset + len(rows) + 1,
            include_author=not args.no_authors)
        if row is None or row.review_id in seen:
            continue
        seen.add(row.review_id)
        rows.append(row)
    return rows


def _dump(args: argparse.Namespace, name: str, html: str) -> None:
    """Write a snapshot — on success too, not only on failure.

    A run can return the right count with a column silently unpopulated, and
    then the exact bytes are the only way to tell a parsing bug from a
    too-early snapshot.
    """
    if not args.dump_html:
        return
    path = f"{args.dump_html}_{name}.html"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"[dump] {path} ({len(html)} bytes)", file=sys.stderr)


def main(launch: Callable[[argparse.Namespace, Optional[str]], Any],
         argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    env_config.apply(args)

    mode, url, error = resolve_target(args)
    if error:
        print(f"[error] {error}", file=sys.stderr)
        return EXIT_USAGE
    args.mode = mode

    cap = page_flow.concurrency_limit(mode, url)
    if cap is not None and args.concurrency > cap:
        print(f"[error] --concurrency {args.concurrency} is not available for "
              f"--mode {mode}: this route's pages are not independently "
              f"addressable, so page N+1's address does not exist until page "
              f"N has come back", file=sys.stderr)
        return EXIT_USAGE
    if args.concurrency > 1 and not (args.proxy or args.proxy_file):
        print("[warn] --concurrency above 1 without a proxy pool sends N times "
              "the traffic from one address, which is a faster way to be "
              "scored than to gather data", file=sys.stderr)
    if args.cdp_endpoint and (args.proxy or args.proxy_file):
        print("[error] --cdp-endpoint already proxies; attaching --proxy to it "
              "manufactures a contradiction rather than better cover",
              file=sys.stderr)
        return EXIT_USAGE

    pool = proxy_from_args(args)
    proxy_url = pool.next() if pool else None
    if proxy_url:
        print(f"[info] exit {mask(proxy_url)}", file=sys.stderr)

    unpaginated = page_flow.warn_unpaginated(mode, args.pages)
    if unpaginated:
        print(f"[warn] {unpaginated}", file=sys.stderr)

    print(f"[info] mode={mode} gl={args.gl} hl={args.hl} pages={args.pages}",
          file=sys.stderr)

    out = RunOutcome()
    driver = None
    try:
        driver = launch(args, proxy_url)
        try:
            if mode in parser.GRID_MODES:
                out = run_grid(driver, url, mode, args)
            elif mode == "app":
                out = run_app(driver, url, args)
            else:
                out = run_reviews(driver, url, args)
        finally:
            driver.close()
    except Exception as exc:
        # The content was never obtained. That is exit 5, not "zero products":
        # a consumer must be able to tell a dead proxy from an empty
        # catalogue.
        print(f"[error] {type(exc).__name__}: {_redact(str(exc))}", file=sys.stderr)
        return finish_run(
            [], args.out, args.format, args.allow_empty, blocked=False,
            stop_reason="navigation_failed", pages_requested=args.pages,
            pages_completed=0, start_url=url, final_url=url, mode=mode)

    if out.blocked:
        for tip in page_flow.block_advice(
                None, args.headless, bool(pool), bool(args.twocaptcha_key),
                cdp=bool(args.cdp_endpoint), fingerprint=args.fingerprint):
            print(f"[hint] {tip}", file=sys.stderr)

    print(f"[info] {len(out.rows)} rows over {out.pages_completed} page(s), "
          f"stop_reason={out.stop_reason}", file=sys.stderr)

    return finish_run(
        out.rows, args.out, args.format, args.allow_empty,
        blocked=out.blocked, stop_reason=out.stop_reason,
        pages_requested=args.pages, pages_completed=out.pages_completed,
        start_url=url, final_url=out.final_url or url,
        pages_failed=out.pages_failed or None, mode=mode,
        extra=dict(out.extra, gl=args.gl, hl=args.hl))


_SECRET_RE = None


def _redact(text: str) -> str:
    """Strip anything key-shaped out of a message before it is printed.

    An EXCEPTION MESSAGE is a log. `requests` puts the full URL — query string
    included — into the text of every connection error, and Playwright repeats
    a CDP endpoint five times in one error, so a masker that handles the first
    occurrence prints the password the other four times and looks like it is
    working.
    """
    import re
    global _SECRET_RE
    if _SECRET_RE is None:
        _SECRET_RE = re.compile(
            r"((?:client)?key|token|api[_-]?key)=[^&\s\"']+", re.I)
    text = _SECRET_RE.sub(r"\1=<redacted>", text)
    text = re.sub(r"(ws{1,2}?://)[^:/@\s]+:[^@\s]+@", r"\1<redacted>@", text)
    return text

