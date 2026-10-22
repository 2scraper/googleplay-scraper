"""
playwright_scraper.py
---------------------
The primary engine. Reads Google Play to JSON or CSV.

    python3 playwright_scraper.py --mode listing --category GAME --pages 3
    python3 playwright_scraper.py --mode search --query "vpn" --gl DE --hl de
    python3 playwright_scraper.py --mode app --app-id com.whatsapp
    python3 playwright_scraper.py --mode reviews --app-id com.whatsapp --pages 5
    python3 playwright_scraper.py --mode developer --developer 5700313618786177705
    python3 playwright_scraper.py --url "https://play.google.com/store/apps/category/TOOLS"

Measured 2026-09-22 from a datacentre address in Nuremberg: every route this
engine reads answers without a key, a proxy or an account. What the paid
products buy here is volume from many addresses and a specific country — not
access. See the README.

`--gl` is not a convenience on this site. Play computes a rating PER COUNTRY,
so `--gl US` and `--gl DE` describe the same app differently and both are
correct. The market a row describes is a column, not run metadata.

This file holds the Playwright plumbing and nothing else. The CLI, the mode
loops and `main()` live in `engine_core.py`, so the three engines cannot
drift apart on a flag, an exit code or a stop reason — see that module's
docstring for why that is shared rather than copied.
"""

import sys
import time
from typing import Any, Dict, Optional, Tuple

# Imported at MODULE level on purpose: the smoke suite must be able to skip
# this engine when the library is absent, and an import hidden inside the
# launch path makes the module import cleanly with nothing installed, so the
# skip never fires and a broken import reaches a live run instead of CI.
from playwright.sync_api import sync_playwright, Error as PlaywrightError

import engine_core
import page_flow
from proxy_pool import to_playwright

NAME = "playwright"


class PlaywrightDriver:
    """The three operations `engine_core` needs, in Playwright's dialect."""

    name = NAME

    def __init__(self, pw_ctx, browser, context, page, connected: bool = False,
                 owns_context: bool = True) -> None:
        self._pw_ctx = pw_ctx
        self._browser = browser
        self._context = context
        self._page = page
        # Whether the browser was reached over --cdp-endpoint rather than
        # launched here, and so which parts of it this run may tear down.
        self._connected = connected
        self._owns_context = owns_context
        self._owns_page = True

    def fetch(self, url: str, mode: str) -> Tuple[str, Optional[int]]:
        """One navigation.

        `networkidle` is deliberately not used: Play keeps fetching and
        beaconing long after the content is there, so waiting for it costs the
        full timeout on a page that was complete in two seconds.
        """
        response = self._page.goto(url, timeout=engine_core.DEFAULT_TIMEOUT_MS,
                                   wait_until="domcontentloaded")
        status = response.status if response is not None else None
        return self.read(mode), status

    def read(self, mode: str) -> str:
        """Wait for the page as it is now to paint, then return its markup."""
        page_flow.wait_for_count(
            lambda sel: self._page.locator(sel).count(),
            lambda ms: self._page.wait_for_timeout(ms),
            page_flow.ready_selector(mode), page_flow.min_matches(mode),
            page_flow.content_timeout_ms(mode))
        return self._page.content()

    def evaluate(self, js: str, arg: Any = None) -> Any:
        """Run an `() => expr` or `(arg) => expr` function in the page."""
        if arg is None:
            return self._page.evaluate(js)
        return self._page.evaluate(js, arg)

    def post_form(self, url: str, form: Dict[str, str]) -> Tuple[str, Optional[int]]:
        """POST from inside the browser's own session; `(body, status)`.

        Playwright is the one engine of the three with a real request context
        attached to the browsing context, so this needs no page script at all.
        The status goes back with the body: a refused reviews call must not
        read as "the reviews ran out".
        """
        response = self._context.request.post(
            url, form=form, timeout=engine_core.DEFAULT_TIMEOUT_MS)
        return response.text(), response.status

    def current_url(self) -> str:
        return self._page.url

    def close(self) -> None:
        """Leave the browser the way it was found.

        A browser this engine LAUNCHED is closed. One it CONNECTED to over
        `--cdp-endpoint` belongs to somebody else: only the page — and the
        context, if this run created it — are closed, and then the connection
        is dropped. `Browser.close()` on a CDP connection only disconnects, so
        the page opened for the run stayed open in the remote browser: one
        more tab per run (1 -> 2 -> 3 over two runs, measured 2026-10-22 on a
        local Chrome with --remote-debugging-port).
        """
        try:
            if self._connected:
                for owned in (self._page if self._owns_page else None,
                              self._context if self._owns_context else None):
                    if owned is None:
                        continue
                    try:
                        owned.close()
                    except PlaywrightError:
                        pass        # already gone with the remote session
            self._browser.close()
        finally:
            self._pw_ctx.__exit__(None, None, None)


def _connect_over_cdp(pw, endpoint: str):
    """`connect_over_cdp`, retrying the handshake refusals worth retrying.

    Playwright reports a refused handshake at once ("WebSocket error: ...
    500 Internal Server Error"), so the policy in `page_flow` decides whether
    to wait and try again; anything else is raised to the caller as it was.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return pw.chromium.connect_over_cdp(
                endpoint, timeout=engine_core.DEFAULT_TIMEOUT_MS)
        except PlaywrightError as exc:
            if (attempt >= page_flow.CDP_CONNECT_ATTEMPTS
                    or not page_flow.cdp_connect_retriable(exc)):
                raise
            delay = page_flow.cdp_connect_delay(attempt)
            print(f"[warn] the CDP endpoint refused the connection (HTTP "
                  f"{page_flow.cdp_connect_status(exc)}); retrying in "
                  f"{delay:.0f}s ({attempt}/{page_flow.CDP_CONNECT_ATTEMPTS})",
                  file=sys.stderr)
            time.sleep(delay)


def launch(args, proxy_url: Optional[str]) -> PlaywrightDriver:
    """A browser, a context and a page.

    A rotation is a fresh browser: cookies Play issued against one exit and
    replayed from another are a stronger signal than either address alone, so
    nothing is ever swapped under a live session.
    """
    pw_ctx = sync_playwright()
    pw = pw_ctx.__enter__()

    if args.cdp_endpoint:
        # Never set a UA, a fingerprint or a proxy over CDP: the remote
        # browser brings its own identity and stacking a second one
        # manufactures a contradiction rather than better cover.
        try:
            browser = _connect_over_cdp(pw, args.cdp_endpoint)
        except BaseException:
            pw_ctx.__exit__(None, None, None)
            raise
        owns_context = not browser.contexts
        context = browser.new_context() if owns_context else browser.contexts[0]
        # A NEW page, always: the remote browser's existing tabs are its
        # owner's, and this run closes only what it opened.
        return PlaywrightDriver(pw_ctx, browser, context, context.new_page(),
                                connected=True, owns_context=owns_context)

    context_kwargs: Dict[str, Any] = {}
    init_script = None
    if args.fingerprint:
        # engine_core refuses --fingerprint without a key, so one is here.
        import fingerprint_client
        fp = fingerprint_client.get_fingerprint(
            args.twocaptcha_key, tags=args.fp_tags, country=args.fp_country)
        context_kwargs.update(fingerprint_client.playwright_context_kwargs(fp))
        init_script = fingerprint_client.playwright_init_script(fp)

    launch_kwargs: Dict[str, Any] = {"headless": args.headless}
    if proxy_url:
        # Credentials go through Playwright's own fields, never onto the
        # browser's command line where anything that can run `ps` reads them.
        launch_kwargs["proxy"] = to_playwright(proxy_url)

    browser = pw.chromium.launch(**launch_kwargs)
    if args.locale:
        context_kwargs.setdefault("locale", args.locale)
    context = browser.new_context(**context_kwargs)
    if init_script:
        context.add_init_script(init_script)
    return PlaywrightDriver(pw_ctx, browser, context, context.new_page())


if __name__ == "__main__":
    sys.exit(engine_core.main(launch))
