# Changelog

All notable changes to this project are documented here.

The format is [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project follows [Semantic Versioning](https://semver.org/) as closely as
a CLI toolkit can. A **patch** means fixes — it does not mean every flag and
default is frozen, so a behaviour-changing default can land in one, and when
it does it is called out at the top of the entry rather than left to be
discovered from a bill or a diff.

## [Unreleased]

## [0.2.0] — 2026-10-15

> **Behaviour changes for existing callers.**
> - A package id that does not exist, or a page Play served with no apps on
>   it, now exits **4** (as the README always said), not 5. On Selenium the
>   same 404 used to exit **3**; it is now 4 as well.
> - A run that gathered rows and then failed (a timeout on page 2, a refused
>   reviews call) now exits **6 and writes those rows**. It used to exit 5
>   and throw them away — or, for a refused reviews call, exit 0 as
>   "complete".
> - Newly rejected with exit 2 naming the flag: `--pages 0` (it used to
>   fetch nothing and exit 4), negative `--retries`, `--delay`,
>   `--retry-delay`, `--proxy-block-retries`, `--reviews-per-call 0`,
>   `--min-score` outside 0..1, `--concurrency` above 1 (it was accepted and
>   ignored), `--proxy-rotate on-block` (it crashed), `--fingerprint` without
>   a key or with `--cdp-endpoint`, and `--fingerprint` on the Selenium and
>   pyppeteer engines (they warned and carried on). Selenium with
>   `--cdp-endpoint` now exits 2 instead of 1.
> - Output files are written atomically; a new file gets the mode your umask
>   gives it, an existing file keeps its mode.
> - In CSV output only, text cells starting with `=`, `+`, `-`, `@`, a tab
>   or a line break are prefixed with `'`.

### Fixed

- **`--proxy` and `--proxy-file` crashed before the first fetch**
  (`AttributeError: 'ProxyPool' object has no attribute 'next'`). Both run
  now, with `--proxy-rotate per-page` a fresh browser on the next exit for
  every grid page, and a refused page moving to the next exit.
- **`--retries`, `--retry-delay`, `--proxy-block-retries`, `--solve-captcha`,
  `--captcha-api` and `--min-score` were accepted and read by nothing.**
  `engine_core.fetch_page()` now applies them: `--retries` is extra attempts
  on a transport failure or an unrecognised page (0 = one attempt), a
  refused page gets fresh browsers without spending `--retries`, and a bare
  HTTP 429 waits at the same exit.
- **The captcha solver was never called**, although the README and
  `.env.example` said it was wired. A reCAPTCHA on a refused page is now
  detected, solved with a 2Captcha key, injected, and the page that follows
  is judged — at most one paid solve per page, an allowance that neither a
  retry nor a fresh exit resets. Tested offline against a stubbed solver; no
  live solve has been run on this site.
- **A refused or unreadable reviews call ended the run as complete.** The
  status of the `batchexecute` call is now read on all three engines, and
  such a page is recorded as failed (blocked when it says so).
- **`GOOGLEPLAY_URL` from `.env` overrode `--category`** (and made
  `--mode search --query …` a usage error). It is now used only when no
  `--url` and no `--category`/`--query`/`--app-id`/`--developer` is typed,
  and `.env.example` ships it commented out.
- **An interrupted write truncated the previous output** (a good 1,105-byte
  JSON left as 13 bytes of invalid JSON). Writes go to a temporary file
  beside the target and are renamed into place.
- **CSV cells written by app developers and reviewers could run as
  spreadsheet formulas.** Escaped in CSV only; `csv_cells_escaped` in the
  sidecar counts them.
- **An empty document was classified as blocked.** It is now an
  unrecognised page that is retried.
- Error messages mask credentials in `http(s)://user:pass@` proxy URLs too.
- `diff_runs.py` refuses two runs of different `--gl` markets even when they
  share a currency (DE and FR).
- `.env.example` no longer says Scraping Browser credentials "live about a
  day"; how long they last varies.
- Public files no longer refer to a private document; the rules are stated
  in this repo's own words, and the suite fails if such a reference returns.
- `tests.yml` no longer carries an inline copy of the sample check;
  `ci_checks.py --sample-check` is the one implementation.
- **Scraper API: `waitFor` is now sent as a JSON object.** Measured
  2026-09-23 against `scraper.2captcha.com/tasks/sync`: the JSON-encoded
  string this client sent (the form older docs described) is refused with
  HTTP 422 "params.waitFor must be an object" -- and the task is still
  billed ($0.0005) -- so every run with `--wait-text` or its sibling wait flags
  failed with exit 5. The object form answers HTTP 200.
- **Scraper API: the target page's status is read from `http_code`.** The
  response's `status` field is the API's own verdict string (`"success"`),
  not the target site's HTTP code, so a target 403/503 was never seen by
  this client. `http_code` (an int) is read now, with `status` kept as a
  fallback only when it is an int.
- **`diff_runs.py` told users Google Play "prices everything in JPY for every
  visitor".** That text, and the `--price-tolerance-pct` help, came from the
  repo this one was ported from; Play prices per market. A currency mismatch
  between two runs is now explained as two different `--gl` markets.
  `TRACKED_FIELDS` / `PRICE_FIELDS` no longer name columns this schema does
  not have (`points`, `price_max`, `shipping_fee`, ...).
- **`scraper_api_client.py` could never report a block.** It compared the
  `(state, reason)` tuple from `detect_page_state` to `"blocked"`, which never
  matches. Its docstring, `--help` and error messages also described another
  site (45 products, a 43-byte Akamai deny, `?p=N`, "these pages DO need
  JavaScript"). They now say this path has not been run on Google Play yet.
- **Issue templates and CONTRIBUTING.md described another site** (payload
  keys, JPY rate-limit page, EUC-JP, sponsored slots, a 6,750-result cap).
  Rewritten from this repo's README and `product_parser.py`. The broken
  TROUBLESHOOTING.md links now point at the README.
- Donor prose removed from `captcha_solver.py`, `output_writer.py`,
  `env_config.py`, `fingerprint_client.py`, `SECURITY.md` and
  `.github/ci_checks.py`. The lessons that came from siblings now name the
  sibling. `ci_checks.py` no longer says the captures live outside the repo.
  They are committed here, scrubbed.
- `.gitignore` / `.dockerignore` ignored another repo's output prefix
  (`dubizzle_listings.*`). They now ignore this repo's own `googleplay_apps.*`.
  A `!fixtures_generated.json` exception for a file this repo does not have
  was removed, and the Dockerfile example no longer writes `/out/coffee`.
- `SECURITY.md` said this project has no releases or version tags; it has
  both. "Supported versions" now names the latest release and `main`.
- `captcha_solver.py`'s docstring pointed at a "No DataDome solver" section
  that does not exist in this repo (it came with the copied core). Removed.

## [0.1.0] — 2026-09-22

First release. Five modes, four back ends, and one row schema shared with the
rest of the 2scraper family.

### Added

- **`--mode listing`** — a Play genre grid, following the page's own
  addressable shelves (`/store/apps/collection/cluster?gsr=…`) rather than a
  page number, because this site publishes none.
- **`--mode search`** — a keyword query.
- **`--mode app`** — one store page, with the columns only that route
  publishes: the rating histogram, the split between ratings and written
  reviews, the version, the update date, the developer's contact details, the
  privacy policy, Play's own chart standing, and the **exact install count**
  the store prints beside its bucketed one.
- **`--mode developer`** — one developer's catalogue, on both spellings of
  that route (`/store/apps/dev?id=<numeric>` and
  `/store/apps/developer?id=<name>`).
- **`--mode reviews`** — an app's reviews, unbounded. Page 1 is embedded in
  the app page and costs no extra request; every page after it comes from
  Play's own `batchexecute` endpoint, which answers without cookies or a key.
- **`--gl` / `--hl` as first-class inputs, and as COLUMNS on every row.** Play
  computes a rating per country: measured 2026-09-22 on one app from one
  address, US 4.08, DE 3.90, JP 3.96, IN 4.13, BR 4.49, with an identical
  global rating count in all five. A row that does not record its market is
  not interpretable, so the market is not run metadata here.
- **`--no-authors`** — drops a reviewer's display name, profile id and avatar
  together, keeping everything the mode is for.
- **`--reviews-per-call`** — the store's front end asks for 40 and the
  endpoint is not capped there: 2000 in one call returned 2000, with a
  continuation token. The default is 200.
- Four back ends — Playwright (primary), Selenium, pyppeteer and the
  Scraping Browser API over `--cdp-endpoint` — verified live to produce
  identical rows: the same category run through three of them gave 52 rows
  and three identical sets of package ids.
- `engine_core.py`, holding the CLI, the mode loops and `main()`, so a flag
  cannot exist on one engine and not its twins.
- A daily canary that runs a real three-page listing scrape **and a real
  reviews run, from a bare GitHub runner with no secrets**, and is expected
  to be green — the standing test of the README's central claim.
- `scrub_fixtures.py`, which replaces reviewer names, profile ids, avatars
  and review bodies in a capture by structure, and a suite check that guards
  the result by pattern so the next capture is caught too.

### Notes on what is deliberately absent

None of these is a limitation of the store, and each is stated as what it is:

- **No `--mode books` or `--mode movies`.** Both storefronts answer (1.8 MB
  and 1.3 MB of served markup on 2026-09-22) and neither has been captured or
  parsed. `product_parser` refuses those URLs with that reason rather than
  reading them with the apps parser and reporting an empty catalogue.
- **Only `--sort newest`.** The store's front end also offers "most relevant"
  and "rating"; in this request shape both return an empty payload — measured
  on two apps, sort key 1 → 0 reviews, key 2 → 40 and a token, key 3 → 0. A
  flag with three values of which two silently return nothing is worse than a
  flag with one.
- **No review score filter.** The payload has a slot for one and setting it
  changes nothing: asking for score 1 and for score 5 each returned forty
  reviews spanning all five.
- **No `contains_ads` column.** "Contains ads" appears zero times and
  "In-app purchases" exactly once in both app captures — including one that
  publishes no IAP range — so the rendered labels are template text and
  cannot be read as flags.
- **`--pages` above 1 warns on `--mode search`.** Play answers a query once:
  measured in a real browser scrolling to the bottom four times per query,
  `game` 22 results, `vpn` 30, `a` 50, page height unchanged and zero shelf
  links. The warning is the point — a run that quietly stops at page 1 and
  reports success is the failure this family has paid for before.
- **`--concurrency` above 1 is refused on `--mode reviews` and `--mode app`,**
  with the reason: a reviews page N+1 has no address until page N has come
  back.

### Added to the family core

- `EXIT_USAGE = 2` in `output_writer.py`. The family's contract has always
  had this code and no repo in it had a name for it.
- A run with failed pages now reports `partial` and exit 6, not `complete`
  and exit 0. `finish_run` decided completeness from `stop_reason` alone, and
  a named list of reasons cannot cover a failure recorded somewhere else —
  `pages_failed` is somewhere else — so a sidecar could say `status:
  complete` with a non-empty `pages_failed` in the same file. Landed here as
  part of a family-wide fix measured across 32 repos by CALLING each one's
  `finish_run` rather than grepping for it.

[Unreleased]: https://github.com/2scraper/googleplay-scraper/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/2scraper/googleplay-scraper/releases/tag/v0.2.0
[0.1.0]: https://github.com/2scraper/googleplay-scraper/releases/tag/v0.1.0
