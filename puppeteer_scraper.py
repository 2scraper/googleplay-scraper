"""
puppeteer_scraper.py
--------------------
The pyppeteer mirror. Same CLI, same rows, same exit codes as
`playwright_scraper.py` — by construction rather than by discipline: the
flags, the mode loops and `main()` come from `engine_core`, and this file
holds only the driver plumbing.

    python3 puppeteer_scraper.py --mode listing --category GAME --pages 3

pyppeteer is effectively unmaintained and its own README points at
Playwright. It is here for parity, and it is demoted in priority rather than
in correctness: it must agree with its twins about every exit code.

One thing is deliberately NOT done here, and it is the opposite of what looks
obvious. A sibling repo in this family set a user agent through pyppeteer's
CDP override and found the site serving navigation 1 and refusing navigations
2 to 4 — including when the claimed UA agreed with the real platform, so it
was the override rather than the value. Nothing here overrides the browser's
own identity, and `--fingerprint` warns instead of half-applying one.
"""

import asyncio
import sys
import urllib.parse
from typing import Dict, Optional, Tuple

# Module level on purpose — see the note in playwright_scraper.py.
from pyppeteer import launch as pyppeteer_launch
from pyppeteer.errors import PyppeteerError

import engine_core
import page_flow
from proxy_pool import split_credentials, mask

NAME = "puppeteer"

# An expression, not a body: pyppeteer takes `() => expr` where Selenium takes
# a statement list. Kept in the engine for that reason.
_POST_FORM_JS = """
(url, body) => fetch(url, {
  method: 'POST',
  headers: {'content-type': 'application/x-www-form-urlencoded;charset=UTF-8'},
  body: body,
  credentials: 'include'
}).then(r => r.text())
"""


class PuppeteerDriver:
    name = NAME

    def __init__(self, loop, browser, page) -> None:
        self._loop = loop
        self._browser = browser
        self._page = page

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def fetch(self, url: str, mode: str) -> Tuple[str, Optional[int]]:
        response = self._run(self._page.goto(
            url, timeout=engine_core.DEFAULT_TIMEOUT_MS,
            waitUntil="domcontentloaded"))
        status = response.status if response is not None else None
        page_flow.wait_for_count(
            self._count, lambda ms: self._run(asyncio.sleep(ms / 1000.0)),
            page_flow.ready_selector(mode), page_flow.min_matches(mode),
            page_flow.content_timeout_ms(mode))
        return self._run(self._page.content()), status

    def _count(self, selector: str) -> int:
        """How many nodes match, with a destroyed context counted as none.

        Found by running this engine live rather than by reading it: Play
        hydrates the page under the poll, and pyppeteer raises
        `NetworkError: Execution context was destroyed, most likely because of
        a navigation` from inside `querySelectorAll` when that happens. It
        took the whole run down with exit 5 on a page that was about to be
        perfectly readable.

        Zero is the honest answer for "the context this question was asked in
        no longer exists", and the next poll asks again in the new one.
        Playwright's locator survives the same navigation, which is why only
        this engine needs it — and why "mirror them exactly" is a design rule
        and not a verification.
        """
        try:
            return len(self._run(self._page.querySelectorAll(selector)))
        except Exception:
            return 0

    def post_form(self, url: str, form: Dict[str, str]) -> str:
        body = urllib.parse.urlencode(form)
        if "play.google.com" not in (self._page.url or ""):
            self._run(self._page.goto("https://play.google.com/store/apps",
                                      waitUntil="domcontentloaded"))
        return self._run(self._page.evaluate(_POST_FORM_JS, url, body)) or ""

    def current_url(self) -> str:
        return self._page.url or ""

    def close(self) -> None:
        try:
            self._run(self._browser.close())
        finally:
            self._loop.close()


def launch(args, proxy_url: Optional[str]) -> PuppeteerDriver:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    if args.cdp_endpoint:
        from pyppeteer import connect
        browser = loop.run_until_complete(
            connect(browserWSEndpoint=args.cdp_endpoint))
        pages = loop.run_until_complete(browser.pages())
        page = pages[0] if pages else loop.run_until_complete(browser.newPage())
        return PuppeteerDriver(loop, browser, page)

    launch_args = ["--disable-blink-features=AutomationControlled",
                   "--window-size=1440,2400"]
    user = password = None
    if proxy_url:
        user, password, stripped = split_credentials(proxy_url)
        launch_args.append(f"--proxy-server={stripped}")
    if args.locale:
        launch_args.append(f"--lang={args.locale}")

    if args.fingerprint:
        print("[warn] --fingerprint is not applied on the pyppeteer engine: a "
              "bare CDP user-agent override leaves the client hints reporting "
              "the real browser, and a sibling site refused every navigation "
              "after the first because of exactly that", file=sys.stderr)

    browser = loop.run_until_complete(pyppeteer_launch(
        headless=args.headless, args=launch_args,
        handleSIGINT=False, handleSIGTERM=False, handleSIGHUP=False))
    page = loop.run_until_complete(browser.newPage())
    if user or password:
        # Credentials go through the driver's own field, never onto the
        # browser's command line.
        loop.run_until_complete(page.authenticate(
            {"username": user or "", "password": password or ""}))
    return PuppeteerDriver(loop, browser, page)


if __name__ == "__main__":
    sys.exit(engine_core.main(launch))
