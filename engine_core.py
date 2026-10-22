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
        def fetch(url, mode) -> (html, status)   # navigate, wait, read
        def read(mode) -> html                   # wait, read the page as it is now
        def evaluate(js, arg=None) -> Any        # run an `(arg) => expr` function
        def post_form(url, form) -> (text, status)
        def current_url() -> str
        def close() -> None

and a `launch(args, proxy_url) -> Driver`, which raises `UsageError` for a
flag that engine cannot honour.

The retries, the proxy rotation, the throttle wait and the paid captcha
solve all live HERE, in `fetch_page()`, so there is one copy of each and one
paid-solve allowance per page whichever engine runs it.

Deliberately no JavaScript crosses this boundary — see `page_flow`. The
driver is asked for named OPERATIONS, never handed a snippet, because
Selenium's `execute_script` takes a function body with an explicit `return`
while Playwright and pyppeteer take `() => expr`.
"""

import argparse
import os
import re
import sys
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import captcha_solver
import env_config
import page_flow
import product_parser as parser
from output_writer import App, Review, finish_run, dedupe_by_key, EXIT_USAGE
from proxy_pool import ProxyError, ProxyPool, mask, from_args as proxy_from_args

DEFAULT_TIMEOUT_MS = 45_000
DEFAULT_OUT_PREFIX = "googleplay_apps"


class UsageError(Exception):
    """A flag combination an engine cannot honour. Exit 2, not a crash."""


def _int_at_least(minimum: int):
    def convert(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text!r} is not a whole number")
        if value < minimum:
            raise argparse.ArgumentTypeError(
                f"must be at least {minimum}, got {value}")
        return value
    return convert


def _float_between(low: float, high: Optional[float] = None):
    def convert(text: str) -> float:
        try:
            value = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text!r} is not a number")
        if value != value or value < low or (high is not None and value > high):
            span = f"between {low} and {high}" if high is not None else f"at least {low}"
            raise argparse.ArgumentTypeError(f"must be {span}, got {text}")
        return value
    return convert


# Each count the run spends is validated by argparse, so a bad value is exit 2
# naming the flag. `--pages 0` used to fetch nothing and report exit 4 — "the
# catalogue is empty" — about a run that never asked.
positive_int = _int_at_least(1)
non_negative_int = _int_at_least(0)
non_negative_float = _float_between(0.0)


def country_code(text: str) -> str:
    """`--gl`: two letters, upper-cased.

    Only the SHAPE is checked here. Whether Play serves that country is the
    store's answer, not a list this file could keep current — `ZZ` is two
    letters and Play answers it with a full page — so a well-shaped code Play
    refuses is caught from its HTTP 400 instead (see `fetch_page`).
    """
    if not (isinstance(text, str) and len(text) == 2 and text.isascii()
            and text.isalpha()):
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a two-letter country code (e.g. US, DE, JP)")
    return text.upper()


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
    ap.add_argument("--gl", default=None, type=country_code,
                    help="the MARKET, as a two-letter country code. Changes "
                         "the rating, the currency and which apps are listed. "
                         "A code Play does not serve is exit 2. Default: "
                         "the gl in --url, else US.")
    ap.add_argument("--hl", default=None,
                    help="the display language. Default: the hl in --url, "
                         "else en.")
    ap.add_argument("--pages", type=positive_int, default=1,
                    help="how many fetches to make. On a grid this follows "
                         "the page's own shelves; on reviews it follows the "
                         "continuation token.")
    ap.add_argument("--reviews-per-call", type=positive_int,
                    default=parser.REVIEWS_PER_CALL,
                    help=f"reviews to ask for per call (default "
                         f"{parser.REVIEWS_PER_CALL}; no ceiling was found up "
                         f"to 2000)")
    ap.add_argument("--sort", default=parser.DEFAULT_REVIEW_SORT,
                    choices=sorted(parser.REVIEW_SORTS),
                    help="review ordering: newest (posted_at descending, "
                         "kept across pages), relevant (the store's own "
                         "ranking) or rating. Default newest.")
    ap.add_argument("--no-authors", action="store_true",
                    help="drop reviewer name, profile id and avatar from "
                         "review rows")
    ap.add_argument("--format", default="json", choices=("json", "csv", "both"))
    ap.add_argument("--out", default=DEFAULT_OUT_PREFIX,
                    help="output prefix: writes PREFIX.json / .csv / "
                         ".meta.json. A trailing .json or .csv is dropped, "
                         "and missing directories are created.")
    ap.add_argument("--delay", type=non_negative_float, default=1.5,
                    help="seconds between fetches")
    ap.add_argument("--retries", type=non_negative_int, default=2,
                    help="EXTRA attempts per page after the first, on a "
                         "transport failure or a page nothing recognises "
                         "(0 = one attempt). Default 2.")
    ap.add_argument("--retry-delay", type=non_negative_float, default=3.0,
                    help="seconds to wait before each retry")
    ap.add_argument("--concurrency", type=positive_int, default=1,
                    help="only 1 is implemented in this repo: pages are "
                         "fetched one at a time, and a higher value is "
                         "refused rather than silently ignored")
    ap.add_argument("--proxy", help="a single proxy URL")
    ap.add_argument("--proxy-file", help="a file of proxy URLs, one per line")
    ap.add_argument("--proxy-rotate", default="per-page",
                    choices=("per-page", "per-run"),
                    help="with --proxy-file: a fresh browser on the next exit "
                         "for every grid page (per-page), or one exit for the "
                         "run (per-run). Either way a blocked page moves to "
                         "the next exit. A reviews run keeps one session, "
                         "because its continuation token belongs to it.")
    ap.add_argument("--proxy-shuffle", action="store_true")
    ap.add_argument("--proxy-block-retries", type=non_negative_int, default=None,
                    help="fresh browsers to try on a blocked page (default "
                         f"{page_flow.BLOCK_RETRIES_WITH_POOL} with a pool, "
                         f"{page_flow.BLOCK_RETRIES_WITHOUT_POOL} without)")
    ap.add_argument("--twocaptcha-key")
    ap.add_argument("--captcha-api", default="v2", choices=("v2", "v1"))
    ap.add_argument("--solve-captcha", default="when-blocked",
                    choices=("never", "when-blocked", "always"),
                    help="when-blocked: solve a reCAPTCHA only on a page the "
                         "run classified as refused; always: on any page "
                         "that carries one; never. At most one paid solve "
                         "per page either way.")
    ap.add_argument("--min-score", type=_float_between(0.0, 1.0), default=0.3)
    ap.add_argument("--cdp-endpoint")
    ap.add_argument("--fingerprint", action="store_true")
    ap.add_argument("--fp-country")
    ap.add_argument("--fp-tags", default="Windows")
    ap.add_argument("--locale")
    ap.add_argument("--allow-empty", action="store_true")
    ap.add_argument("--dump-html", metavar="PREFIX",
                    help="write each fetched page; on success too, not only "
                         "on failure. Missing directories are created.")
    headless = ap.add_mutually_exclusive_group()
    headless.add_argument("--headless", dest="headless", action="store_true",
                          default=True)
    headless.add_argument("--headful", dest="headless", action="store_false")
    return ap


DEFAULT_HL, DEFAULT_GL = "en", "US"


def _settle_market(args: argparse.Namespace) -> None:
    """The market of a `--url` run: the URL's own, unless a flag was typed.

    `--gl` and `--hl` used to DEFAULT to US and en and were then written over
    the URL, so `--url ".../search?q=vpn&hl=ja&gl=JP"` was fetched as
    `hl=en&gl=US` — measured 2026-10-22: USD prices and English titles, rows
    labelled US — with nothing said. A market in the URL is something the
    caller typed and outranks a default. A flag the caller typed outranks the
    URL, and says so.
    """
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(args.url).query))
    for flag, attr, default in (("--gl", "gl", DEFAULT_GL), ("--hl", "hl", DEFAULT_HL)):
        in_url = query.get(attr)
        if attr == "gl" and in_url:
            in_url = in_url.upper()
        typed = getattr(args, attr)
        if typed and in_url and typed != in_url:
            print(f"[info] {flag} {typed} replaces {attr}={in_url} in --url",
                  file=sys.stderr)
        setattr(args, attr, typed or in_url or default)


def resolve_target(args: argparse.Namespace) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """`(mode, url, error)` — what this invocation is actually asking for.

    Also settles the MARKET, and writes it back to `args.gl` / `args.hl` so the
    request, the rows and every message agree on it.
    """
    if args.url:
        reason = parser.refusal_reason(args.url)
        if reason:
            return None, None, reason
        _settle_market(args)
        mode = parser.mode_for_url(args.url)
        # An app page is the address of BOTH app-level modes: `--mode app`
        # reads the page, `--mode reviews` pages that app's reviews. Refusing
        # the second contradicted `--help`, which says the route decides the
        # mode unless --mode says otherwise.
        if mode == "app" and args.mode == "reviews":
            return "reviews", parser.with_market(args.url, args.hl, args.gl), None
        if args.mode and args.mode != "listing" and mode != args.mode:
            # An explicit --mode that disagrees with the URL is a mistake
            # worth naming rather than silently resolving one way.
            return None, None, (
                f"--mode {args.mode} disagrees with {args.url}, which is a "
                f"{mode} URL")
        return mode, parser.with_market(args.url, args.hl, args.gl), None

    args.hl, args.gl = args.hl or DEFAULT_HL, args.gl or DEFAULT_GL
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


def _sleep(seconds: float) -> None:
    """Every wait in the loop goes through here, so a test can count them."""
    if seconds > 0:
        time.sleep(seconds)


class Session:
    """The browser, the exit it uses, and the paid-solve allowance.

    A rotation is a fresh browser: cookies issued against one exit and
    replayed from another are a stronger signal than either address alone,
    so nothing is ever swapped under a live session. The solve allowance
    lives HERE rather than on the driver, so a fresh browser does not reset
    it.
    """

    def __init__(self, launch: Callable[[argparse.Namespace, Optional[str]], Any],
                 args: argparse.Namespace, pool: Optional[ProxyPool]) -> None:
        self._launch = launch
        self.args = args
        self.pool = pool
        self.driver: Any = None
        self.budget = page_flow.SolveBudget()
        self.solves_bought = 0

    def proxy_url(self) -> Optional[str]:
        return self.pool.current if self.pool else None

    def start(self) -> None:
        self.driver = self._launch(self.args, self.proxy_url())

    def relaunch(self, reason: str) -> None:
        """A fresh browser — on the next exit when there is a pool."""
        if self.pool:
            self.pool.advance(reason)
            print(f"[info] {reason}: fresh browser on exit "
                  f"{mask(self.pool.current)}", file=sys.stderr)
        else:
            print(f"[info] {reason}: fresh browser on the same address",
                  file=sys.stderr)
        self.close()
        self.start()

    def close(self) -> None:
        if self.driver is not None:
            driver, self.driver = self.driver, None
            try:
                driver.close()
            except Exception as exc:  # noqa: BLE001 — teardown must not mask the result
                print(f"[warn] closing the browser: {type(exc).__name__}: "
                      f"{_redact(str(exc))}", file=sys.stderr)


_PROXY_FAILURE_MARKERS = ("ERR_PROXY", "ERR_TUNNEL", "ERR_NO_SUPPORTED_PROXIES",
                          "ERR_SOCKS", "407")


def _is_proxy_failure(exc: BaseException) -> bool:
    """A dead exit wants a different exit, not another try at the same one."""
    text = str(exc)
    return any(m in text for m in _PROXY_FAILURE_MARKERS)


def _wants_solve(args: argparse.Namespace, state: str) -> bool:
    if args.solve_captcha == "never":
        return False
    if args.solve_captcha == "always":
        return True
    return page_flow.should_solve(state)


def _solve(session: Session, html: str, url: str, mode: str,
           page_num: int) -> Optional[str]:
    """Solve a reCAPTCHA on the current page; the page afterwards, or None.

    Every paid solve in this repo goes through here, and through
    `session.budget` BEFORE the purchase. A token is bought only when it can
    be used: no key, no way to run script in the page, or a spent allowance
    each stop the purchase rather than follow it.
    """
    args = session.args
    driver = session.driver
    evaluate = getattr(driver, "evaluate", None)
    static = captcha_solver.detect_recaptcha_v3(html or "", url)
    live = (captcha_solver.detect_recaptcha_in_page(evaluate, url)
            if evaluate is not None else None)
    challenge = captcha_solver.reconcile_detections(static, live)
    if challenge is None:
        return None
    if not args.twocaptcha_key:
        print(f"[warn] page {page_num} carries a reCAPTCHA ({challenge.kind}) "
              f"and no 2Captcha key is set — not solving", file=sys.stderr)
        return None
    if evaluate is None:
        print(f"[warn] page {page_num}: this engine cannot run script in the "
              f"page, so a token could not be used — not buying one",
              file=sys.stderr)
        return None
    if not session.budget.spend(page_num):
        print(f"[warn] page {page_num} already used its paid solve "
              f"({page_flow.SOLVES_PER_PAGE} per page) — not buying another",
              file=sys.stderr)
        return None
    session.solves_bought += 1
    try:
        token = captcha_solver.solve_recaptcha(
            challenge, args.twocaptcha_key, api_version=args.captcha_api,
            min_score=args.min_score)
        evaluate(captcha_solver.INJECT_TOKEN_JS, token)
        return driver.read(mode)
    except Exception as exc:  # noqa: BLE001 — a solver error is a warning
        print(f"[warn] captcha solve on page {page_num} failed: "
              f"{type(exc).__name__}: {_redact(str(exc))}", file=sys.stderr)
        return None


def fetch_page(session: Session, url: str, mode: str, page_num: int,
               dump_name: str) -> Tuple[str, Optional[int], str, str]:
    """One page, with every budget the run has: `(html, status, state, reason)`.

    * a transport failure or an `unknown` page spends `--retries` (EXTRA
      attempts, so 0 means one), and a dead proxy moves to the next exit;
    * a refused page gets `--proxy-block-retries` fresh browsers — on the
      next exit when there is a pool — without spending `--retries`;
    * a bare HTTP 429 waits at the SAME exit (`THROTTLE_BACKOFF_MS`), which
      is the opposite response to a block, and spends neither;
    * a reCAPTCHA is solved through `_solve()` — at most one paid solve per
      page however many attempts the page takes.

    A transport failure that outlives its retries is raised to the caller,
    which records the page as failed and keeps the rows it already has.
    """
    args = session.args
    attempts = args.retries + 1
    block_budget = (args.proxy_block_retries
                    if args.proxy_block_retries is not None
                    else page_flow.block_retries(session.pool is not None))
    attempt = blocks = throttles = 0
    while True:
        attempt += 1
        try:
            if session.driver is None:
                session.start()
            html, status = session.driver.fetch(url, mode)
        except UsageError:
            raise
        except Exception as exc:
            if attempt >= attempts:
                raise
            print(f"[warn] page {page_num} attempt {attempt}/{attempts}: "
                  f"{type(exc).__name__}: {_redact(str(exc))}", file=sys.stderr)
            if session.pool and _is_proxy_failure(exc):
                session.relaunch("proxy_failure")
            _sleep(args.retry_delay)
            continue

        _dump(args, dump_name, html)
        state, reason = page_flow.classify(html, status, url, mode)

        if state == "bad_request":
            # Play's own HTTP 400: the request is malformed, so no retry, no
            # solve and no other exit will change the answer. It used to be
            # read as a block — exit 3, fresh browsers, and advice to buy a
            # proxy — for a country code Play does not serve.
            raise UsageError(
                f"Play refused the request as malformed (HTTP 400) — "
                f"usually because --gl {args.gl} is not a country the store "
                f"serves. Use one it does, e.g. US, DE, JP.")

        if state != "content" and _wants_solve(args, state):
            solved = _solve(session, html, url, mode, page_num)
            if solved is not None:
                # The status was the interstitial's; the page now is
                # whatever the solve led to, and is judged on what it holds.
                html, status = solved, None
                _dump(args, dump_name + "_solved", html)
                state, reason = page_flow.classify(html, None, url, mode)

        if (state == "blocked" and reason == "http_429"
                and throttles < page_flow.THROTTLE_RETRIES):
            delay = page_flow.throttle_delay_ms(throttles)
            throttles += 1
            attempt -= 1
            print(f"[warn] page {page_num}: HTTP 429 — waiting {delay} ms at "
                  f"the same exit ({throttles}/{page_flow.THROTTLE_RETRIES})",
                  file=sys.stderr)
            _sleep(delay / 1000.0)
            continue

        if page_flow.counts_as_blocked(state) and page_flow.should_retry(state) \
                and blocks < block_budget:
            blocks += 1
            attempt -= 1
            print(f"[warn] page {page_num} refused ({reason}); block retry "
                  f"{blocks}/{block_budget}", file=sys.stderr)
            session.relaunch("blocked")
            continue

        if (not page_flow.counts_as_blocked(state) and page_flow.should_retry(state)
                and attempt < attempts):
            print(f"[warn] page {page_num} unrecognised ({reason}); retry "
                  f"{attempt}/{attempts - 1}", file=sys.stderr)
            _sleep(args.retry_delay)
            continue

        return html, status, state, reason


def _handle_state(out: RunOutcome, state: str, reason: str, page_num: int) -> bool:
    """Record what a page turned out to be. Returns True if it is parseable.

    A 404 for the package id on page 1, and a served page with no apps on
    it, are ANSWERS: they end the run as complete with nothing in it (exit 4)
    rather than as a page that failed. A 404 on a later shelf is a page that
    failed, since the run followed a link the store itself published.
    """
    if page_flow.should_parse(state):
        return True
    if state == "empty":
        out.stop_reason = "empty_page"
        return False
    if state == "not_found" and page_num == 1:
        out.stop_reason = "not_found"
        return False
    out.pages_failed.append(page_num)
    if page_flow.counts_as_blocked(state):
        out.blocked = True
        out.stop_reason = f"blocked:{reason}"
    elif state == "not_found":
        out.stop_reason = "not_found"
    else:
        out.stop_reason = f"unrecognised:{reason}"
    return False


def _fetch_failed(out: RunOutcome, exc: BaseException, page_num: int) -> None:
    """A page whose transport failed after its retries. Rows so far are kept."""
    print(f"[error] page {page_num}: {type(exc).__name__}: {_redact(str(exc))}",
          file=sys.stderr)
    out.pages_failed.append(page_num)
    out.stop_reason = f"navigation_failed:{type(exc).__name__}"


def run_grid(session: Session, start_url: str, mode: str,
             args: argparse.Namespace) -> RunOutcome:
    """listing / search / developer.

    Page 1 is always fetched first, because its shelves are what make pages
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

        if (page_num > 1 and session.pool is not None
                and session.pool.rotates_per_page()):
            try:
                session.relaunch("per-page rotation")
            except UsageError:
                raise
            except Exception as exc:
                _fetch_failed(out, exc, page_num)
                break
        try:
            html, status, state, reason = fetch_page(
                session, url, mode, page_num, f"{mode}_p{page_num}")
        except UsageError:
            raise
        except Exception as exc:
            _fetch_failed(out, exc, page_num)
            break
        out.final_url = session.driver.current_url() if session.driver else url

        if not _handle_state(out, state, reason, page_num):
            break

        rows = parser.parse_grid(html, hl=args.hl, gl=args.gl, page=page_num)
        if not rows:
            # Classified as content because it HOLDS app records, and parsed
            # to none: that is our parser, not an empty catalogue.
            out.pages_failed.append(page_num)
            out.stop_reason = "parser_found_nothing"
            break
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
            _sleep(args.delay)

    out.extra["shelves_seen"] = len(visited)
    return out


def run_app(session: Session, url: str, args: argparse.Namespace) -> RunOutcome:
    """One store page -> one row, with the detail-only columns filled."""
    out = RunOutcome()
    try:
        html, status, state, reason = fetch_page(session, url, "app", 1, "app")
    except UsageError:
        raise
    except Exception as exc:
        _fetch_failed(out, exc, 1)
        return out
    out.final_url = session.driver.current_url() if session.driver else url
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


def run_reviews(session: Session, url: str, args: argparse.Namespace) -> RunOutcome:
    """One app's reviews.

    Strictly sequential, and that is a property of the site rather than a
    choice: page N+1's address is a continuation token inside page N's
    response, so there is nothing for a second worker to fetch — and the
    session is kept for the whole run, since the token belongs to it.

    The app page is fetched first, but only to establish that the app exists
    in this market (Play answers a missing id with a clean 404) and to put the
    session on Play's own origin for the POSTs that follow. EVERY page of rows
    comes from `batchexecute`, page 1 included.

    Up to 0.2.0 page 1 was taken from the reviews the app page embeds. Those
    are in relevance order whatever `--sort` says and carry no continuation
    token, so page 2 was requested without one, came back as the start of the
    list again, and was mostly eaten by dedupe — 20 / 1 / 19 rows over three
    pages in one reported run, and "end_of_listing" after one page in another.
    """
    out = RunOutcome()
    package = parser.package_from_url(url)
    if not package:
        out.stop_reason = "no_app_id"
        return out

    try:
        html, status, state, reason = fetch_page(session, url, "reviews", 1,
                                                 "reviews_app")
    except UsageError:
        raise
    except Exception as exc:
        _fetch_failed(out, exc, 1)
        return out
    out.final_url = session.driver.current_url() if session.driver else url
    if not _handle_state(out, state, reason, 1):
        return out

    seen: set = set()
    token: Optional[str] = None
    endpoint = parser.batchexecute_url(args.hl, args.gl)
    while out.pages_completed < args.pages:
        page_num = out.pages_completed + 1
        form = parser.build_reviews_request(
            package, count=args.reviews_per_call, token=token, sort=args.sort)
        try:
            body, post_status = session.driver.post_form(endpoint, form)
        except Exception as exc:  # a driver-specific transport failure
            print(f"[error] reviews page {page_num}: {type(exc).__name__}: "
                  f"{_redact(str(exc))}", file=sys.stderr)
            out.pages_failed.append(page_num)
            out.stop_reason = f"batchexecute_failed:{type(exc).__name__}"
            break
        _dump(args, f"reviews_p{page_num}", body or "")
        payload = parser.parse_batchexecute(body or "")
        if payload is None or post_status not in (None, 200):
            # The store did not answer with a reviews payload. That is a
            # page that failed — a refusal if it says so — and never "the
            # reviews ran out", which is what an unread body used to report.
            out.pages_failed.append(page_num)
            marker = parser.challenge_marker(body or "")
            if marker or post_status in (403, 429):
                out.blocked = True
                out.stop_reason = (f"blocked:challenge_marker:{marker}" if marker
                                   else f"blocked:http_{post_status}")
            else:
                out.stop_reason = f"batchexecute_unreadable:http_{post_status}"
            break
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
        _sleep(args.delay)

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


_OUTPUT_SUFFIXES = (".meta.json", ".json", ".csv")


def output_prefix(out: str) -> str:
    """`--out x.json` writes `x.json`, `x.csv` and `x.meta.json`.

    `--out` is a PREFIX — the run writes up to three files from it — and
    passing a filename with its extension is the most common way people use
    such a flag. Taken literally it produced `x.json.json`, `x.json.csv` and
    `x.json.meta.json`. A recognised output suffix is now dropped; anything
    else (`--out run.2026`) is left alone.
    """
    lowered = out.lower()
    for suffix in _OUTPUT_SUFFIXES:
        if lowered.endswith(suffix) and len(out) > len(suffix):
            trimmed = out[:-len(suffix)]
            print(f"[info] --out {out}: writing {trimmed}.json/.csv/.meta.json",
                  file=sys.stderr)
            return trimmed
    return out


def ensure_parent_dir(prefix: Optional[str]) -> Optional[str]:
    """Create the directory a path prefix writes into; say why if it cannot.

    Checked BEFORE any browser starts. A `--dump-html` into a directory that
    did not exist used to raise mid-run, after the page had been fetched, and
    the run reported it as `navigation_failed`, exit 5 — "the content was
    never obtained" — about content it had obtained.
    """
    if not prefix:
        return None
    parent = os.path.dirname(os.path.expanduser(prefix))
    if not parent:
        return None
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as exc:
        return f"cannot create {parent}: {exc.strerror or exc}"
    if not os.access(parent, os.W_OK):
        return f"{parent} is not writable"
    return None


def main(launch: Callable[[argparse.Namespace, Optional[str]], Any],
         argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    # Captured BEFORE the environment fills anything: after `apply()` there is
    # no telling a typed --url from GOOGLEPLAY_URL in `.env`, and a URL from
    # the environment must not override --category/--query/--app-id/
    # --developer, which the caller DID type. It used to: with the shipped
    # `.env.example` copied, `--category GAME` returned TOOLS.
    typed_url = args.url
    env_config.apply(args)
    if not typed_url and args.url and any(
            (args.category, args.query, args.app_id, args.developer)):
        print("[info] ignoring GOOGLEPLAY_URL from the environment: a "
              "--category/--query/--app-id/--developer was given",
              file=sys.stderr)
        args.url = None

    mode, url, error = resolve_target(args)
    if error:
        print(f"[error] {error}", file=sys.stderr)
        return EXIT_USAGE
    args.mode = mode

    # Expanded once, here, so the directory created and the file written are
    # the same path even when a `~` arrived quoted and the shell left it.
    args.out = os.path.expanduser(output_prefix(args.out))
    if args.dump_html:
        args.dump_html = os.path.expanduser(args.dump_html)
    for flag, prefix in (("--out", args.out), ("--dump-html", args.dump_html)):
        problem = ensure_parent_dir(prefix)
        if problem:
            print(f"[error] {flag} {prefix}: {problem}", file=sys.stderr)
            return EXIT_USAGE

    if args.concurrency > 1:
        cap = page_flow.concurrency_limit(mode, url)
        if cap is not None:
            print(f"[error] --concurrency {args.concurrency} is not available "
                  f"for --mode {mode}: this route's pages are not "
                  f"independently addressable, so page N+1's address does not "
                  f"exist until page N has come back", file=sys.stderr)
        else:
            print(f"[error] --concurrency {args.concurrency}: workers are not "
                  f"implemented in this repo, and pages are fetched one at a "
                  f"time. Refused rather than ignored, so a run never claims a "
                  f"parallelism it did not have.", file=sys.stderr)
        return EXIT_USAGE
    if args.cdp_endpoint and (args.proxy or args.proxy_file):
        print("[error] --cdp-endpoint already proxies; attaching --proxy to it "
              "manufactures a contradiction rather than better cover",
              file=sys.stderr)
        return EXIT_USAGE
    if args.fingerprint and args.cdp_endpoint:
        print("[error] --fingerprint cannot be applied over --cdp-endpoint: the "
              "managed browser brings its own identity, and stacking a second "
              "one manufactures a contradiction", file=sys.stderr)
        return EXIT_USAGE
    if args.fingerprint and not args.twocaptcha_key:
        print("[error] --fingerprint needs a 2Captcha key (--twocaptcha-key or "
              "TWOCAPTCHA_KEY); refused rather than run without one",
              file=sys.stderr)
        return EXIT_USAGE

    try:
        pool = proxy_from_args(args)
    except (ProxyError, OSError) as exc:
        print(f"[error] {_redact(str(exc))}", file=sys.stderr)
        return EXIT_USAGE
    if pool:
        # A reviews run keeps one session for its whole length — the
        # continuation token belongs to it — so a per-page policy does not
        # apply there and the log must not claim it does.
        rotation = ("one exit for the run: the review token belongs to the "
                    "session" if mode == "reviews" else pool.rotate)
        print(f"[info] exit {mask(pool.current)} (rotation: {rotation})",
              file=sys.stderr)

    unpaginated = page_flow.warn_unpaginated(mode, args.pages)
    if unpaginated:
        print(f"[warn] {unpaginated}", file=sys.stderr)

    print(f"[info] mode={mode} gl={args.gl} hl={args.hl} pages={args.pages}",
          file=sys.stderr)

    session = Session(launch, args, pool)
    try:
        session.start()
        if mode in parser.GRID_MODES:
            out = run_grid(session, url, mode, args)
        elif mode == "app":
            out = run_app(session, url, args)
        else:
            out = run_reviews(session, url, args)
    except UsageError as exc:
        session.close()
        print(f"[error] {exc}", file=sys.stderr)
        return EXIT_USAGE
    except Exception as exc:
        session.close()
        # The browser never came up, so the content was never obtained. That
        # is exit 5, not "zero products": a consumer must be able to tell a
        # dead proxy from an empty catalogue. A failure AFTER rows were
        # gathered never reaches here — the loops record the page as failed
        # and keep the rows, which is a partial run (exit 6).
        print(f"[error] {type(exc).__name__}: {_redact(str(exc))}", file=sys.stderr)
        return finish_run(
            [], args.out, args.format, args.allow_empty, blocked=False,
            stop_reason="navigation_failed", pages_requested=args.pages,
            pages_completed=0, start_url=url, final_url=url, mode=mode)
    session.close()

    if out.blocked:
        for tip in page_flow.block_advice(
                None, args.headless, bool(pool), bool(args.twocaptcha_key),
                cdp=bool(args.cdp_endpoint), fingerprint=args.fingerprint):
            print(f"[hint] {tip}", file=sys.stderr)

    print(f"[info] {len(out.rows)} rows over {out.pages_completed} page(s), "
          f"stop_reason={out.stop_reason}", file=sys.stderr)

    extra = dict(out.extra, gl=args.gl, hl=args.hl)
    if session.solves_bought:
        extra["captcha_solves"] = session.solves_bought
    if pool:
        extra["proxy_rotations"] = pool.rotations
    return finish_run(
        out.rows, args.out, args.format, args.allow_empty,
        blocked=out.blocked, stop_reason=out.stop_reason,
        pages_requested=args.pages, pages_completed=out.pages_completed,
        start_url=url, final_url=out.final_url or url,
        pages_failed=out.pages_failed or None, mode=mode,
        extra=extra)


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
    # Credentials in ANY URL — a CDP endpoint and a proxy alike.
    text = re.sub(r"((?:wss?|https?|socks5h?)://)[^:/@\s]+:[^@\s]+@",
                  r"\1<redacted>@", text)
    return text

