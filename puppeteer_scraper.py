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
own identity, and `--fingerprint` is refused (exit 2) rather than
half-applied.
"""

import asyncio
import sys
import urllib.parse
from typing import Any, Dict, Optional, Tuple

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
}).then(r => r.text().then(t => ({status: r.status, text: t})))
"""


class PuppeteerDriver:
    name = NAME

    def __init__(self, loop, browser, page, connected: bool = False) -> None:
        self._loop = loop
        self._browser = browser
        self._page = page
        # Reached over --cdp-endpoint rather than launched here: the browser
        # is somebody else's, and only the page this run opened is ours.
        self._connected = connected

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def fetch(self, url: str, mode: str) -> Tuple[str, Optional[int]]:
        response = self._run(self._page.goto(
            url, timeout=engine_core.DEFAULT_TIMEOUT_MS,
            waitUntil="domcontentloaded"))
        status = response.status if response is not None else None
        return self.read(mode), status

    def read(self, mode: str) -> str:
        """Wait for the page as it is now to paint, then return its markup."""
        page_flow.wait_for_count(
            self._count, lambda ms: self._run(asyncio.sleep(ms / 1000.0)),
            page_flow.ready_selector(mode), page_flow.min_matches(mode),
            page_flow.content_timeout_ms(mode))
        return self._run(self._page.content())

    def evaluate(self, js: str, arg: Any = None) -> Any:
        """Run an `() => expr` or `(arg) => expr` function in the page."""
        if arg is None:
            return self._run(self._page.evaluate(js))
        return self._run(self._page.evaluate(js, arg))

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

    def post_form(self, url: str, form: Dict[str, str]) -> Tuple[str, Optional[int]]:
        """`(body, status)` — the status so a refusal is not read as an end."""
        body = urllib.parse.urlencode(form)
        if "play.google.com" not in (self._page.url or ""):
            self._run(self._page.goto("https://play.google.com/store/apps",
                                      waitUntil="domcontentloaded"))
        result = self._run(self._page.evaluate(_POST_FORM_JS, url, body)) or {}
        return result.get("text") or "", result.get("status")

    def current_url(self) -> str:
        return self._page.url or ""

    def close(self) -> None:
        """Leave the browser the way it was found.

        For a CONNECTED browser pyppeteer's `Browser.close()` sends CDP
        `Browser.close` — it shut down the browser on the other end of
        `--cdp-endpoint`, whoever's it was, and the next run then hung on a
        dead endpoint (measured 2026-10-22 on a local Chrome with
        --remote-debugging-port; reported on the Scraping Browser too). A
        connected browser now gets its page closed and the connection dropped;
        only a browser this engine launched is closed.

        The connection's own tasks are then let finish before the event loop
        is closed. Closing the loop under them printed `Event loop is closed`
        tracebacks after every CDP run.
        """
        try:
            if self._connected:
                try:
                    self._run(self._page.close())
                except Exception:   # the session may already be gone
                    pass
                self._run(self._browser.disconnect())
            else:
                self._run(self._browser.close())
        finally:
            _drain_and_close(self._loop)


class CDPConnectError(ConnectionError):
    """The CDP endpoint could not be connected to, with the reason it gave."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


async def _connect(endpoint: str, timeout_s: float):
    """pyppeteer's `connect()`, but a refused handshake RAISES.

    pyppeteer opens the WebSocket in a background task and then waits for the
    answer to its first CDP command. When the handshake fails — a refused
    port, an HTTP 500 from a managed browser — that background task dies with
    the reason and the command is never answered, so `connect()` waited
    forever: the "next run hangs" in the report. Measured 2026-10-22 against
    a local server answering 500 and against a closed port: both hung, with
    the real error visible only in a "Task exception was never retrieved"
    traceback at exit.

    This waits on the first command AND on the background task, and raises
    whichever finishes first with an error, bounded by `timeout_s`. It relies
    on `Connection._recv_fut`, a private attribute present in every pyppeteer
    release this repository supports; without it, it degrades to the timeout.
    """
    from pyppeteer.browser import Browser
    from pyppeteer.connection import Connection

    loop = asyncio.get_event_loop()
    connection = Connection(endpoint, loop, 0)
    # `send()` returns a Future, not a coroutine, so `ensure_future` and not
    # `create_task`.
    first = asyncio.ensure_future(connection.send("Target.getBrowserContexts"))
    receiver = getattr(connection, "_recv_fut", None)
    waiting = {first} | ({receiver} if receiver is not None else set())
    done, _pending = await asyncio.wait(waiting, timeout=timeout_s,
                                        return_when=asyncio.FIRST_COMPLETED)
    if first not in done:
        reason = None
        if receiver is not None and receiver in done and not receiver.cancelled():
            reason = receiver.exception()
        # Dispose BEFORE touching `first`: it is the very future pyppeteer
        # keeps for the pending command, and `dispose()` fails it with
        # "Target closed" — cancelling it first made that an InvalidStateError
        # that replaced the real reason.
        try:
            await connection.dispose()
        except Exception:
            pass
        if first.done() and not first.cancelled():
            first.exception()       # retrieved, so it is not reported at exit
        else:
            first.cancel()
        if isinstance(reason, (asyncio.TimeoutError, TimeoutError)):
            raise CDPConnectError(
                "the CDP endpoint accepted the connection and never completed "
                "the WebSocket handshake") from None
        if reason is not None:
            raise CDPConnectError(
                f"the CDP endpoint refused the connection: "
                f"{type(reason).__name__}: {reason}",
                page_flow.cdp_connect_status(reason)) from None
        raise CDPConnectError(
            f"no answer from the CDP endpoint within {timeout_s:.0f}s")
    contexts = first.result().get("browserContextIds", [])
    return await Browser.create(connection, contexts, False,
                                {"width": 800, "height": 600}, None,
                                lambda: connection.send("Browser.close"))


def _connect_with_retry(loop, endpoint: str):
    timeout_s = engine_core.DEFAULT_TIMEOUT_MS / 1000.0
    attempt = 0
    while True:
        attempt += 1
        try:
            return loop.run_until_complete(_connect(endpoint, timeout_s))
        except CDPConnectError as exc:
            if (attempt >= page_flow.CDP_CONNECT_ATTEMPTS
                    or not page_flow.cdp_connect_retriable(exc)):
                raise
            delay = page_flow.cdp_connect_delay(attempt)
            print(f"[warn] the CDP endpoint refused the connection (HTTP "
                  f"{exc.status_code}); retrying in {delay:.0f}s "
                  f"({attempt}/{page_flow.CDP_CONNECT_ATTEMPTS})", file=sys.stderr)
            loop.run_until_complete(asyncio.sleep(delay))


def _drain_and_close(loop) -> None:
    """Let the connection's own tasks finish, then close the loop."""
    try:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True))
    finally:
        loop.close()


def launch(args, proxy_url: Optional[str]) -> PuppeteerDriver:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    if args.cdp_endpoint:
        try:
            browser = _connect_with_retry(loop, args.cdp_endpoint)
            # A NEW page, always. This used to take `pages()[0]` — the
            # remote browser's own first tab — and navigate it away.
            page = loop.run_until_complete(browser.newPage())
        except BaseException:
            _drain_and_close(loop)
            raise
        return PuppeteerDriver(loop, browser, page, connected=True)

    launch_args = ["--disable-blink-features=AutomationControlled",
                   "--window-size=1440,2400"]
    user = password = None
    if proxy_url:
        user, password, stripped = split_credentials(proxy_url)
        launch_args.append(f"--proxy-server={stripped}")
    if args.locale:
        launch_args.append(f"--lang={args.locale}")

    if args.fingerprint:
        # Refused rather than warned about: a run that carries on without the
        # identity the caller asked for is the flag doing nothing.
        raise engine_core.UsageError(
            "--fingerprint is not available on the pyppeteer engine: a bare "
            "CDP user-agent override leaves the client hints reporting the "
            "real browser, and a sibling site refused every navigation after "
            "the first because of exactly that. Use the Playwright engine.")

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
