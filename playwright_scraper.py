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

    def __init__(self, pw_ctx, browser, context, page) -> None:
        self._pw_ctx = pw_ctx
        self._browser = browser
        self._context = context
        self._page = page

    def fetch(self, url: str, mode: str) -> Tuple[str, Optional[int]]:
        """One navigation.

        `networkidle` is deliberately not used: Play keeps fetching and
        beaconing long after the content is there, so waiting for it costs the
        full timeout on a page that was complete in two seconds.
        """
        response = self._page.goto(url, timeout=engine_core.DEFAULT_TIMEOUT_MS,
                                   wait_until="domcontentloaded")
        status = response.status if response is not None else None
        page_flow.wait_for_count(
            lambda sel: self._page.locator(sel).count(),
            lambda ms: self._page.wait_for_timeout(ms),
            page_flow.ready_selector(mode), page_flow.min_matches(mode),
            page_flow.content_timeout_ms(mode))
        return self._page.content(), status

    def post_form(self, url: str, form: Dict[str, str]) -> str:
        """POST from inside the browser's own session.

        Playwright is the one engine of the three with a real request context
        attached to the browsing context, so this needs no page script at all.
        """
        response = self._context.request.post(
            url, form=form, timeout=engine_core.DEFAULT_TIMEOUT_MS)
        return response.text()

    def current_url(self) -> str:
        return self._page.url

    def close(self) -> None:
        try:
            self._browser.close()
        finally:
            self._pw_ctx.__exit__(None, None, None)


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
        browser = pw.chromium.connect_over_cdp(
            args.cdp_endpoint, timeout=engine_core.DEFAULT_TIMEOUT_MS)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        return PlaywrightDriver(pw_ctx, browser, context, context.new_page())

    context_kwargs: Dict[str, Any] = {}
    init_script = None
    if args.fingerprint:
        if not args.twocaptcha_key:
            print("[warn] --fingerprint needs a 2Captcha key; continuing "
                  "without one", file=sys.stderr)
        else:
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
