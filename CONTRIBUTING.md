# Contributing

Bug reports, site-change reports and pull requests are all welcome. This file
covers the few things specific to a scraper, which are not the usual ones.

## Before you open anything

Run the offline suite. It needs no network, no browser and no API key, and takes
about a second:

```bash
pip install -r requirements.txt
python3 smoke_test.py
```

It prints its own check count, and lists any group it had to skip because an
engine library is absent.

**The suite must pass with no engine installed at all.** CI installs only
`beautifulsoup4` and `requests`, so any import of `playwright_scraper`,
`puppeteer_scraper` or `selenium_scraper` in a test has to sit inside
`try/except ImportError` with the skip recorded. This is easy to get wrong
locally, where you almost certainly have an engine installed and an unguarded
import passes.

If the suite fails on a clean clone, that is itself the bug — say so.

## Never commit a credential

`.env` is in `.gitignore`. Keep it there.

The scrapers mask `user:pass@` in their own log lines, but three things are **not**
masked: raw HTML dumps, the Scraper API's `x-debug` response header, and your
shell history. Before pasting any output into an issue or a PR, replace keys,
proxy passwords and full `ws://user:pass@host:9222` endpoints with `***`.

CI fails the build if something that looks like a credential is committed. That
check is a backstop, not a review — a leaked key has to be rotated whether or
not the check caught it.

## Reporting a site change

Google Play changing its payload is the normal way this stops working, and it
has its own issue template. The detail that saves the most time is WHICH source
broke, because this scraper reads the store's own JSON rather than its DOM:

1. **The grid and app records.** The `AF_initDataCallback(...)` blocks in the
   first response. Records are recognised by SHAPE — a list whose first
   element is `[<package id>, 7]` — and never by block key, because the key is
   `ds:3` on a category page, `ds:4` on a search page and `ds:8` on an app
   page. If that shape moves, a run reports 0 rows.
2. **The app page's detail body.** The source of every column only the app
   page carries: the rating histogram, the exact install count, the version,
   the developer's contact details. A renumbered slot there costs one column,
   not the row.
3. **Reviews.** Page 1 is embedded in the app page; every page after it comes
   from `/_/PlayStoreUi/data/batchexecute`, RPC `UsvDTd`. Both are the same
   record, so if page 1 parses and page 2 does not, the RPC moved.

Things about this site that look like bugs and are not, so please check them
before filing (the README's "Traps that look like bugs" has more):

* **The rating differs by `--gl`.** Play computes a rating per country while
  the rating count is global. Compare rows of the same market.
* **`installs_exact` is null on every listing row.** No listing tile carries
  it (0 of 330 measured); it arrives on `--mode app`.
* **`genre_id` is null on every listing row.** A tile publishes the genre
  only as display text; the key arrives on `--mode app`.
* **A package id that does not exist gives exit 4.** Play answers it with a
  clean HTTP 404, which is an answer, not a refusal.

## Before this repository goes public

One item cannot be undone later, so it belongs on a checklist rather than in
someone's head. **A commit on top cannot reach what a published tag and a
merged PR's refs already hold** — those stay attached to the PR and cannot be
deleted from it. Afterwards, only a fresh repository removes anything.

```bash
python3 .github/ci_checks.py --history-check
```

That applies the same credential rules CI enforces to **every blob that has
ever existed**, not just the working tree. It is deliberately not part of
`--all` and not run by CI: it shells out to git once per object, and a dirty
history needs a decision, not a red check on every push.

Then the rest of the presentation, in the order that matters:

1. `python3 smoke_test.py` green, and the canary dispatched at least once by
   hand before anyone trusts the badge. This canary needs **no secret** and
   runs daily on a schedule, which is deliberate: the README's central claim
   is that you need no key, no proxy and no account to read this site, and a scheduled, ungated, real three-page run is that claim
   under test every morning. The family's rule that a canary which cannot
   pass must SKIP has a second half — a canary that CAN pass without a
   credential must never be gated on one, or the badge goes green every day
   while testing nothing.

   Note that `workflow_dispatch` needs the workflow to exist on the DEFAULT
   branch: from a feature branch `gh workflow run` answers
   `HTTP 404: workflow canary.yml not found on the default branch`, which
   reads like a typo in the filename. So the order is forced — merge first,
   dispatch second.
2. The repo description, homepage and topics set (see the family notes on
   what those should say).
3. Only then the row in the org profile README — and check it with an
   ANONYMOUS request rather than your own logged-in browser. A row pointing
   at a private repo is a 404 for every visitor, which costs more trust than
   the missing row.

## Pull requests

**Add a test for the behaviour you are changing.** `smoke_test.py` is a single
file of plain functions; its fixtures are the real, trimmed and scrubbed
captures in `captures/` (see `captures/README.md` and `make_fixtures.py`).
Copy the nearest existing check and edit it.

The properties below exist because they were once absent or wrong, and cost
real time. Tests pin them, so a PR that breaks one will fail rather than
silently regress:

- **The page's own JSON is the primary source, and it is found by shape.**
  Block keys (`ds:N`) differ between page kinds, so nothing selects a block
  by name.
- **JSON-LD is a cross-check, not a source.** An app page carries exactly one
  `application/ld+json` block and a grid carries none; the payload and the
  JSON-LD disagree about the same rating in the same fetch (4.616471 against
  4.616447925567627), so the payload wins.
- **`brand` is the developer, not the seller of record.** The seller slot is
  locale-dependent and says "Google Commerce Ltd" in the EU; the second
  locale capture is what found that.
- **`gl` and `hl` are on every row.** A rating is a property of the market,
  so a row that does not record its market is not interpretable.
- **Pagination follows the shelves page 1 links to** and stops on DATA — a
  fetch that added no new package id — never on a selector. There is no
  `?page=N` on any route. Search does not paginate at all, and `--pages`
  above 1 there warns rather than quietly stopping.
- **Reviews paginate by a continuation token** that exists only inside the
  previous response, so that mode refuses `--concurrency` above 1.
- **Reviewers are identifiable people.** `--no-authors` drops the name,
  profile id and avatar together, and `scrub_fixtures.py` replaces them in
  any capture before it is committed.

### Style

- **Match the file you are editing.** No formatter is enforced.
- **Comments explain *why*.** What the code does is visible; why it does it that
  way, especially where the obvious version is wrong, is not.
- **A timeout on every remote call.** Every browser library used here has needed
  an explicit timeout its own API does not provide, and each has needed its own
  route out of the runtime — reporting a timeout is not the same as exiting on
  one. If you add a call to a remote browser or API, bound it.
- **Fail loudly.** A function that returns an empty list on error, or logs
  success without checking that the thing it wanted actually happened, is the
  single most common bug class in this codebase's history. A selector that
  matches the *wrong* element is worse than one that matches nothing, because
  the second one tells you.

### If your change needs a live run

Most do not — the suite covers the parser, the writers, the captcha classifier
and the CLI contract against inline fixtures. If yours genuinely needs
play.google.com, say in the PR what you ran, which URL, `--mode`, `--hl` and
`--gl`, from which exit, and what you got. Row counts differ by category, by
market and by how many shelves the run followed, so a bare "worked for me" is
not reproducible.

**Run more than the primary engine.** "Mirror them exactly" is a design rule,
not a verification: the first live run of the pyppeteer engine crashed on its
FIRST fetch on a signature mismatch that four separate offline checks and 400
green assertions had not caught.

## Scope

This repo scrapes **public pages** on Google Play: category grids, search
results, app pages, developer pages and app reviews, exactly as an anonymous
visitor is served them.
Out of scope: anything behind a login, anything that submits a form, and
anything that defeats a protection rather than passing it the way an ordinary
browser does.

## Licence

MIT. By opening a pull request you agree your contribution ships under it.
