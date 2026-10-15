"""
selenium_scraper.py
-------------------
The Selenium mirror. Same CLI, same rows, same exit codes as
`playwright_scraper.py` — by construction rather than by discipline: the
flags, the mode loops and `main()` come from `engine_core`, and this file
holds only the driver plumbing.

    python3 selenium_scraper.py --mode listing --category GAME --pages 3

Two limits are Selenium's rather than this site's, and both are stated here
rather than left to be discovered:

  * **No authenticated remote CDP endpoint.** Playwright's
    `connect_over_cdp` and Puppeteer's `browserWSEndpoint` take a full
    `ws://user:pass@host:port` and authenticate on the WebSocket upgrade;
    chromedriver's `debuggerAddress` takes a bare `host:port` with nowhere to
    put a password. So `--cdp-endpoint` is refused here with that reason
    (exit 2, a usage error), rather than half-working.
  * **`--proxy-server` cannot authenticate at all.** Credentials are stripped
    and a warning is printed; a user must not be left believing a
    `user:pass@` URL is doing something.

There is no HTTP status here either. Selenium does not expose one, so
`classify()` gets None and leans on the structural signals — which is why
those exist.
"""

import sys
import urllib.parse
from typing import Any, Dict, Optional, Tuple

# Module level on purpose — see the note in playwright_scraper.py.
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.common.exceptions import WebDriverException

import engine_core
import page_flow
from proxy_pool import split_credentials, mask

NAME = "selenium"

# Fetch a form POST from inside the page, and hand the body back through
# Selenium's async-script callback.
#
# This is the one place a driver's dialect is unavoidable: Selenium's
# `execute_async_script` takes a function BODY with the callback as the last
# argument, where Playwright and pyppeteer take an expression. It stays in
# the engine for exactly that reason — `page_flow` names the operation and
# never sees this string.
_POST_FORM_JS = """
var url = arguments[0], body = arguments[1], done = arguments[arguments.length - 1];
fetch(url, {
  method: 'POST',
  headers: {'content-type': 'application/x-www-form-urlencoded;charset=UTF-8'},
  body: body,
  credentials: 'include'
}).then(function (r) {
    return r.text().then(function (t) { done({status: r.status, text: t}); });
  })
  .catch(function (e) { done({error: String(e)}); });
"""


class SeleniumDriver:
    name = NAME

    def __init__(self, driver) -> None:
        self._driver = driver

    def fetch(self, url: str, mode: str) -> Tuple[str, Optional[int]]:
        self._driver.get(url)
        # No status: see the module docstring.
        return self.read(mode), None

    def read(self, mode: str) -> str:
        """Wait for the page as it is now to paint, then return its markup."""
        page_flow.wait_for_count(
            lambda sel: len(self._driver.find_elements(By.CSS_SELECTOR, sel)),
            lambda ms: self._sleep(ms),
            page_flow.ready_selector(mode), page_flow.min_matches(mode),
            page_flow.content_timeout_ms(mode))
        return self._driver.page_source

    def evaluate(self, js: str, arg: Any = None) -> Any:
        """Run an `() => expr` or `(arg) => expr` function in the page.

        Selenium's dialect takes a function BODY, so the expression is
        wrapped and called with Selenium's own `arguments`.
        """
        script = "return (" + js + ").apply(null, arguments);"
        if arg is None:
            return self._driver.execute_script(script)
        return self._driver.execute_script(script, arg)

    def post_form(self, url: str, form: Dict[str, str]) -> Tuple[str, Optional[int]]:
        body = urllib.parse.urlencode(form)
        # The POST must come from Play's own origin, and after a `fetch()` the
        # driver is already there — but a reviews run that was handed a bare
        # endpoint would not be, so this is explicit rather than assumed.
        if "play.google.com" not in (self._driver.current_url or ""):
            self._driver.get("https://play.google.com/store/apps")
        self._driver.set_script_timeout(engine_core.DEFAULT_TIMEOUT_MS / 1000)
        result = self._driver.execute_async_script(_POST_FORM_JS, url, body) or {}
        if result.get("error"):
            raise WebDriverException(result["error"])
        return result.get("text") or "", result.get("status")

    def current_url(self) -> str:
        return self._driver.current_url or ""

    def close(self) -> None:
        self._driver.quit()

    def _sleep(self, ms: int) -> None:
        import time
        time.sleep(ms / 1000.0)


def launch(args, proxy_url: Optional[str]) -> SeleniumDriver:
    if args.cdp_endpoint:
        raise engine_core.UsageError(
            "--cdp-endpoint is not available on the Selenium engine: "
            "chromedriver's debuggerAddress takes a bare host:port and has "
            "nowhere to put the endpoint's credentials. Use the Playwright or "
            "pyppeteer engine for a managed browser.")

    options = Options()
    if args.headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1440,2400")
    if args.locale:
        options.add_argument(f"--lang={args.locale}")

    if proxy_url:
        user, password, stripped = split_credentials(proxy_url)
        if user or password:
            print(f"[warn] Selenium cannot authenticate a proxy: sending "
                  f"{mask(proxy_url)} without credentials, which this exit "
                  f"will almost certainly refuse", file=sys.stderr)
        options.add_argument(f"--proxy-server={stripped}")

    if args.fingerprint:
        raise engine_core.UsageError(
            "--fingerprint is not available on the Selenium engine: a bare "
            "user-agent override without the matching client hints is a "
            "self-contradicting identity, which is worse than none. Use the "
            "Playwright engine.")

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(engine_core.DEFAULT_TIMEOUT_MS / 1000)
    return SeleniumDriver(driver)


if __name__ == "__main__":
    sys.exit(engine_core.main(launch))
