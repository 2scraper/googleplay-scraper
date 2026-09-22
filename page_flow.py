"""
page_flow.py
------------
The retry / solve / blocked decision, and the readiness policy, as DATA
rather than as three copies of an if-chain.

Google Play answers a request five ways — a served page, a clean 404 for an
id that does not exist, an empty-but-served page, an interstitial nobody has
met from this address yet, and a transport failure — and four of them want a
different response. Three copies of that triage across three engines would
drift, and the drift would be silent: one engine reporting exit 3 where its
twin reports exit 0 on the same page.

Deliberately no JavaScript crosses this boundary. Selenium's
`execute_script` takes a function BODY with an explicit `return` while
Playwright and pyppeteer take `() => expr`, so the engines pass their own
primitives in and this module names the OPERATION.
"""

from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
import urllib.parse

import product_parser as parser


# --------------------------------------------------------------------------
# Readiness
# --------------------------------------------------------------------------

# What "the grid has painted" looks like. An anchor to a store details page
# is the one thing every grid route paints and no interstitial does, and it
# is a URL pattern rather than a class: Play's class names are build hashes
# (`VfPpkd-WsjYwc-OWXEXe-INsAgc`) and churn on every deploy.
READY_SELECTOR_GRID = 'a[href*="/store/apps/details?id="]'

# An app page paints the same anchors (for its similar-apps shelf), so the
# readiness anchor there is the install button's own container, which only
# the subject app has.
READY_SELECTOR_DETAIL = 'a[href*="/store/apps/details?id="], [itemprop="name"]'

_READY = {
    "listing": READY_SELECTOR_GRID,
    "search": READY_SELECTOR_GRID,
    "developer": READY_SELECTOR_GRID,
    "app": READY_SELECTOR_DETAIL,
    "reviews": READY_SELECTOR_DETAIL,
}

# How many matches mean "rendered". Must be > 1: waiting for one resolves on
# an unrelated link long before the grid paints — a sibling repo in this
# family spent a release discovering that.
#
# 8 on a grid, because the smallest real shelf measured here holds 4 tiles
# and each tile links to its app twice (image and title), so 8 anchors is one
# genuinely painted shelf. 2 on an app page, where the subject app plus one
# similar app is already proof the page is real.
MIN_CARD_MATCHES = 8
_MIN = {"listing": MIN_CARD_MATCHES, "search": MIN_CARD_MATCHES,
        "developer": MIN_CARD_MATCHES, "app": 2, "reviews": 2}

CONTENT_TIMEOUT_MS = 20_000


def ready_selector(mode: str = "listing") -> str:
    return _READY.get(mode, READY_SELECTOR_GRID)


def min_matches(mode: str = "listing", expected: Optional[int] = None) -> int:
    """How many matches to wait for before calling the page painted.

    `expected` lets a caller that already knows the page is small — a
    developer with three apps, a search with two results — lower the bar
    rather than time out on a page that is complete.
    """
    floor = _MIN.get(mode, MIN_CARD_MATCHES)
    if expected is not None and expected > 0:
        return max(1, min(floor, expected))
    return floor


def content_timeout_ms(mode: str = "listing") -> int:
    return CONTENT_TIMEOUT_MS


def wait_for_count(count: Callable[[str], int], sleep: Callable[[int], None],
                   selector: str, threshold: int, timeout_ms: int,
                   step_ms: int = 500) -> int:
    """Poll `count(selector)` until it reaches `threshold` or time runs out.

    Polling rather than waiting on an evaluated string, and that is not
    stylistic: a sibling site's Content-Security-Policy has no `unsafe-eval`,
    so Playwright's `wait_for_function` — which hands the browser a STRING —
    died with an `EvalError` and took the run down with it. Counting through
    the driver's own selector API is a protocol call and works under any CSP.

    Returns as soon as `found >= threshold`. The comparison is `>=` and not
    `>`: equality is the SUCCESS case, and a sibling repo reported a fully
    painted page as unpainted for exactly that off-by-one, visible only on a
    query with fewer results than the readiness floor.
    """
    waited = 0
    found = count(selector)
    while found < threshold and waited < timeout_ms:
        sleep(step_ms)
        waited += step_ms
        found = count(selector)
    return found


# --------------------------------------------------------------------------
# Classification, and what each state costs
# --------------------------------------------------------------------------

def classify(html: Optional[str], status: Optional[int] = None,
             url: str = "", mode: str = "listing") -> Tuple[str, str]:
    """`(state, reason)` — the single place any engine decides what it got.

    `status` is positional and second because every engine that has one
    passes it there; Selenium, which has none, passes None and leans on the
    structural signals underneath.
    """
    expect = {"app": "detail", "reviews": "reviews"}.get(mode, "grid")
    return parser.detect_page_state(html or "", status, url, expect)


# The triage, as data. Each state maps to what a run should DO about it, and
# an engine that disagrees with its twins now disagrees with this table
# instead — which a test can see.
STATE_POLICY: Dict[str, Dict[str, bool]] = {
    # parse   retry   solve   blocked
    "content":   {"parse": True,  "retry": False, "solve": False, "blocked": False},
    # A 404 is an ANSWER: this package id does not exist in this market. It
    # is not worth a retry, not worth a solve, and emphatically not a block —
    # calling it one would send a reader to buy a proxy for a typo.
    "not_found": {"parse": False, "retry": False, "solve": False, "blocked": False},
    "blocked":   {"parse": False, "retry": True,  "solve": True,  "blocked": True},
    # Served, and holds nothing we recognise. A real answer for a category
    # with no apps or a search with no hits, so it is not retried: retrying an
    # empty listing just spends the budget to be told the same thing.
    "empty":     {"parse": False, "retry": False, "solve": False, "blocked": False},
    "unknown":   {"parse": False, "retry": True,  "solve": False, "blocked": False},
}


def should_parse(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["parse"]


def should_retry(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["retry"]


def should_solve(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["solve"]


def counts_as_blocked(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["blocked"]


def is_unpainted(state: str, html: Optional[str]) -> bool:
    """Served, but the grid has not arrived yet — wait, do not retry.

    A page Play plainly served, with its own images all over it and no app
    records in it, is either genuinely empty or not painted. Distinguishing
    them is what stops a run refetching a page that was seconds from being
    ready, and it costs one substring: the store's own shell is there, so it
    was served.
    """
    if state != "empty" or not html:
        return False
    return parser.asset_reference_count(html) >= parser.MIN_ASSET_REFERENCES


# --------------------------------------------------------------------------
# Budgets
# --------------------------------------------------------------------------

# Whether a blocked page is worth trying again at all. Read by the engines
# through `block_retries()` — a policy constant no engine consults is the
# same defect as dead code, and this family has shipped one.
RETRY_ON_BLOCKED = True

# Three from a pool, because each attempt is a different address and is
# therefore a genuinely different question. One without, because a second
# identical attempt from one address asks the same question again.
BLOCK_RETRIES_WITHOUT_POOL = 1
BLOCK_RETRIES_WITH_POOL = 3


def block_retries(has_pool: bool) -> int:
    if not RETRY_ON_BLOCKED:
        return 0
    return BLOCK_RETRIES_WITH_POOL if has_pool else BLOCK_RETRIES_WITHOUT_POOL


# A throttle is not a block and wants the opposite response: wait longer at
# the SAME address rather than rotating away from it.
THROTTLE_RETRIES = 3
THROTTLE_BACKOFF_MS = (6_000, 15_000, 30_000)


def throttle_delay_ms(attempt: int) -> int:
    idx = max(0, min(attempt, len(THROTTLE_BACKOFF_MS) - 1))
    return THROTTLE_BACKOFF_MS[idx]


# One paid solve per page. Enforced through `solve_budget()`, which every
# call site goes through: a sibling repo in this family had this exact
# constant, two call sites per attempt and a counter on only one of them, and
# one page bought three solves.
SOLVES_PER_PAGE = 1


def solve_budget(spent: int) -> bool:
    return spent < SOLVES_PER_PAGE


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------

# There is no `?page=N` on any route this repo reads, so there is no
# `page_url()` here and its absence is the design rather than an omission.
#
# A grid page answers with its tiles AND with the addressable shelves beside
# them (`/store/apps/collection/cluster?gsr=<token>`). Those tokens are all
# on page 1, so pages 2..N become independently addressable the moment page 1
# is in hand — which is the family's concurrency model exactly, and why
# `concurrency_limit()` below allows workers on grid modes.
#
# Reviews paginate by a continuation token that is only knowable from the
# previous response, so that mode is strictly sequential and says so.

def next_page_candidates(current_url: str, html: Optional[str],
                         hl: str = "en", gl: str = "US",
                         seen: Optional[Iterable[str]] = None) -> List[str]:
    """The shelves this page publishes that we have not fetched yet."""
    already = set(seen or ())
    out = []
    for link in parser.cluster_links(html or "", hl, gl):
        if link not in already and link != current_url:
            out.append(link)
    return out


# Search is not paginated AT ALL, and that is the site rather than a gap in
# this engine. Measured 2026-09-22 in a real browser, scrolling to the bottom
# four times per query and re-counting: `game` 22 records, `vpn` 30, `a` 50,
# with `document.body.scrollHeight` unchanged across every round and ZERO
# shelf links on the page. So a search answers once, with a per-query number
# of results, and there is nothing further to fetch.
#
# `--pages` above 1 on that mode is therefore meaningless, and saying so is
# the point: a run that quietly stops at page 1 and reports "complete" is the
# shape of failure this family has paid for before.
SEARCH_IS_PAGINATED = False


def warn_unpaginated(mode: str, pages: int) -> Optional[str]:
    """The warning to print when a mode cannot use the --pages it was given."""
    if mode == "search" and pages > 1 and not SEARCH_IS_PAGINATED:
        return ("--pages %d has no effect on --mode search: Play answers a "
                "query once, with everything it intends to show (22 to 50 "
                "apps across the queries measured), and publishes no further "
                "pages and no shelves to follow" % pages)
    return None


def pagination_is_addressable(mode: str) -> bool:
    """Can pages 2..N be planned up front, or must they be chained?

    Grid modes: yes, once page 1 is in hand. Reviews: no — page 2's address
    is a token inside page 1's response.
    """
    return mode in parser.GRID_MODES


def concurrency_limit(mode: str, url: str = "") -> Optional[int]:
    """The highest `--concurrency` this mode may use, or None for unlimited.

    1 for reviews, with the reason: page N+1 of a continuation-token listing
    cannot be handed to a worker, because its address does not exist until
    page N has come back. A worker pool there would not be slower, it would
    be wrong.
    """
    if mode == "reviews":
        return 1
    if mode == "app":
        return 1
    return None


def stop_because_no_new(new_ids: int, mode: str) -> bool:
    """The terminating condition, and it is about DATA.

    "This fetch added no new package id" is a property of the catalogue. "No
    next link matched" is a property of markup, and this family has shipped a
    run that reported success holding a third of the data because it trusted
    the second.
    """
    return new_ids == 0


# --------------------------------------------------------------------------
# Advice
# --------------------------------------------------------------------------

def block_advice(html: Optional[str], headless: bool,
                 has_pool: bool, has_key: bool,
                 cdp: bool = False, fingerprint: bool = False) -> List[str]:
    """What to actually try, printed when a run reports exit 3.

    Written against what was MEASURED on this site rather than against the
    family's instincts, because on Play the usual first guess is wrong.
    Measured 2026-09-22 from one datacentre address in Nuremberg, with plain
    `curl` and no cookies: every route this repo reads answered HTTP 200 with
    its full payload, under curl's own User-Agent and under a Chrome one
    alike. So a refusal here is not the ordinary "buy a residential exit"
    case and should not be advertised as one before the cheaper checks.
    """
    tips: List[str] = []
    if cdp and (fingerprint or not headless):
        tips.append(
            "you are stacking an identity on a managed browser that already "
            "brings its own — drop --fingerprint and let the remote browser "
            "be itself")
    tips.append(
        "this site answered a plain HTTP client from a datacentre address on "
        "every route when this repo was written, so check the boring causes "
        "first: a wrong --gl for the market you meant, an app id that does "
        "not exist there (Play answers those with a clean 404, which this "
        "scraper reports as not_found rather than blocked), and your own "
        "request rate")
    if not has_pool:
        tips.append(
            "if it really is the address, --proxy-file with residential "
            "exits is the lever; --concurrency above 1 from a single address "
            "is the fastest way to be scored rather than served")
    if not has_key:
        tips.append(
            "no 2Captcha key is set. No challenge has been observed on this "
            "site from this address, so a key is not the first thing to buy "
            "here — but the solver path is wired and will use one if a "
            "challenge does appear")
    return tips
