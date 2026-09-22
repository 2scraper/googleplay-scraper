# googleplay-scraper

[![release](https://img.shields.io/github/v/release/2scraper/googleplay-scraper?sort=semver)](https://github.com/2scraper/googleplay-scraper/releases)
[![tests](https://github.com/2scraper/googleplay-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/googleplay-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/googleplay-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/googleplay-scraper/actions/workflows/canary.yml)
[![python](https://img.shields.io/badge/python-3.9%20%7C%203.13-blue)](pyproject.toml)
[![licence](https://img.shields.io/badge/licence-MIT-green)](LICENSE)
[![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20pyppeteer%20%7C%20CDP-informational)](#engines)
[![runs without an account](https://img.shields.io/badge/runs%20without-an%20account-brightgreen)](#do-you-need-any-of-the-paid-products)

Scrapes [Google Play](https://play.google.com) to JSON or CSV: app listings,
keyword search, full app pages, a developer's catalogue and unbounded review
histories — with install counts, rating histograms, in-app-purchase ranges,
content ratings and per-market pricing, four interchangeable browser back
ends and one row schema shared with the rest of the
[2scraper](https://github.com/2scraper) family.

```bash
pip install -r requirements.txt -r requirements-playwright.txt
playwright install chromium

python3 playwright_scraper.py --mode listing --category GAME --pages 3
```

That is the whole setup. No key, no proxy, no account — see below.

## The one thing to know first

**Google Play computes a rating per COUNTRY, and almost nothing that scrapes
it says so.**

Measured 2026-09-22 on `com.rovio.baba` from ONE datacentre address in
Nuremberg, with nothing changed between the rows but `gl`:

| `--gl` | rating | currency | ratings counted |
|---|---|---|---|
| US | **4.08** | USD | 6,312,298 |
| DE | **3.90** | EUR | 6,312,298 |
| JP | **3.96** | JPY | 6,312,298 |
| IN | **4.13** | INR | 6,312,298 |
| BR | **4.49** | BRL | 6,312,298 |

Read it by column. The *rating* moves by more than half a star; the *count*
is identical in all five, because the count is global and the score is not.
So "the rating of this app" is not a number — it is a number per market, and
a row that does not record which market it describes is not interpretable.

This scraper puts `gl` and `hl` in **every row**, joins across markets on the
package id, and never averages one market's rating into another's. And you do
not need an exit in each country to read them: all five rows above came from
one address.

## Do you need any of the paid products?

**No, and this is measured rather than asserted.** From one datacentre
address (netcup, Nuremberg, 2026-09-22), with plain `curl`, no cookies and no
key, every route this repo reads answered in full:

| route | status | size | content |
|---|---|---|---|
| `/store/apps/category/GAME` | 200 | 2.6 MB | 64 apps, 7 shelves |
| `/store/apps/details?id=com.whatsapp` | 200 | 1.2 MB | the full app record |
| `/store/search?q=vpn&c=apps` | 200 | 1.3 MB | 30 apps |
| `/store/apps/dev?id=…` | 200 | 1.0 MB | the developer's grid |
| `/_/PlayStoreUi/data/batchexecute` | 200 | 43 KB | 40 reviews + a token |
| a package id that does not exist | **404** | 1.6 KB | a clean refusal |

Both under `curl`'s own User-Agent and under a Chrome one. No captcha was met
on any of them: counted across all twelve captures in `captures/`,
`recaptcha/api.js`, `g-recaptcha`, `grecaptcha`, `data-sitekey`,
`cf-turnstile` and `challenges.cloudflare.com` each appear **zero** times.

So the honest positioning is: **the paid products buy volume and geography
here, not access.** Many addresses if you are reading the store at scale, a
specific country if you want a market whose language you cannot get from
`gl` alone, and no browser infrastructure of your own if you use the
Scraping Browser API. The captcha solver is wired and has never been needed
from this address — if Google does challenge you, it will be reCAPTCHA, and
`captcha_solver.py` builds `RecaptchaV2TaskProxyless` /
`RecaptchaV3TaskProxyless` for it.

The canary is the standing test of that claim: it runs a real three-page
scrape **daily from a bare GitHub runner with no secrets** and is expected to
be green. If Play ever puts these routes behind a key or a challenge, the
badge goes red the next morning and the sentence above gets retested without
anyone remembering to.

## The five modes

```bash
# a genre grid, following the page's own shelves
python3 playwright_scraper.py --mode listing --category GAME --pages 3

# a keyword search
python3 playwright_scraper.py --mode search --query "password manager"

# one app, with everything the store page publishes
python3 playwright_scraper.py --mode app --app-id com.whatsapp

# a developer's catalogue
python3 playwright_scraper.py --mode developer --developer 5700313618786177705

# reviews, as many as you ask for
python3 playwright_scraper.py --mode reviews --app-id com.whatsapp --pages 10
```

`--mode listing`, `search`, `app` and `developer` all produce the **same row
class**, keyed on the package id, so a search run and an app run of the same
app join on `sku`. `--mode reviews` produces a different one and dedupes on
`review_id`.

### What each route actually publishes

This matters more than it sounds, because it decides which mode you need.

**A grid tile already carries most of an app record** — measured across 330
distinct tiles from six captures: title, developer, genre, price and
currency, description, install bucket, content rating, the in-app-purchase
range, the icon, the screenshot count, and the rating both as the string the
store displays and as the float beside it. On a US grid that is 100%
populated; on a German one the rating is on 92 of 128 tiles and the install
bucket on 108 of 128, because unrated and newly published apps genuinely lack
them.

**Only the app page carries** the rating histogram, the split between ratings
and written reviews, the version, the update date, the developer's email and
website, the privacy policy, Play's own chart standing — and the **exact
install count**. Play prints "10,000,000,000+" and publishes 12,322,703,000
beside it. Measured: **0 of those 330 listing tiles carry the exact figure**,
so `installs_exact` is null on every listing row by the store's design rather
than by a missing read.

**Ratings and written reviews are different numbers.** For one app on one
day: 244,140,056 ratings against 1,952,258 written reviews — two orders of
magnitude apart. They are `review_count` and `review_text_count`, and
conflating them is the easiest mistake to make here.

### Pagination, honestly

There is no `?page=N` anywhere on this site, so there is none in this
scraper either.

* **Grids** publish their shelves as addressable URLs
  (`/store/apps/collection/cluster?gsr=…`), all of them present on page 1.
  `--pages N` follows them, and the run stops on DATA — a fetch that added no
  new package id — never on a selector. Because the addresses are known after
  page 1, `--concurrency` is available on these modes.
* **Search does not paginate at all.** Measured in a real browser, scrolling
  to the bottom four times per query and re-counting: `game` 22 results,
  `vpn` 30, `a` 50, with the page height unchanged across every round and
  zero shelf links on the page. `--pages` above 1 on that mode prints a
  warning saying exactly that, rather than quietly stopping at page 1 and
  reporting success.
* **Reviews** paginate by a continuation token that only exists inside the
  previous response, so that mode is strictly sequential and refuses
  `--concurrency` above 1 with that reason.

### Reviews: how many, and whose

The store's own front end asks for 40 reviews at a time. The endpoint is not
capped there. Measured 2026-09-22 on one app: **100 → 100, 200 → 200,
500 → 500, 1000 → 1000, 2000 → 2000** (2.07 MB), every one of them with a
continuation token. No ceiling was found. The default here is 200;
`--reviews-per-call` raises it.

Page 1 costs no extra request at all — the app page already embeds it.

Only `--sort newest` is implemented. The store's front end also offers "most
relevant" and "rating", and in this request shape both return an empty
payload: measured on two apps, sort key 1 → 0 reviews, key 2 → 40 and a
token, key 3 → 0. That is a TODO about this request shape, not a limit of the
store, and the flag offers only what was measured to work. There is likewise
no score filter: the payload has a slot for one and setting it changes
nothing (asking for score 1 and for score 5 each returned forty reviews
spanning all five).

**Reviews are about identifiable people.** Play publishes a reviewer's
display name, avatar and a stable 21-digit profile id beside their words.
`--no-authors` drops all three and keeps everything the mode is for. The
committed fixtures are scrubbed — see [`captures/README.md`](captures/README.md).

## Engines

All four back ends produce **identical rows**. Verified live on 2026-09-22,
the same category run through three of them: 52 rows, and the three sets of
package ids were identical.

| engine | install | notes |
|---|---|---|
| **Playwright** (primary) | `requirements-playwright.txt` | the one to use |
| Selenium | `requirements-selenium.txt` | no authenticated remote CDP endpoint; `--proxy-server` cannot authenticate at all, so credentials are stripped and a warning printed; exposes no HTTP status, so classification leans on the structural signals |
| pyppeteer | `requirements-puppeteer.txt` | effectively unmaintained upstream; here for parity |
| Scraping Browser API | `--cdp-endpoint` | a managed browser; never stack `--proxy` or `--fingerprint` on it |

**Install exactly one per virtualenv.** Playwright and pyppeteer declare
mutually unsatisfiable pins (`pyee` <12 vs ≥13) and pyppeteer and selenium
collide on `urllib3` (<2.0 vs ≥2.6).

The CLI, the mode loops and `main()` live in **`engine_core.py`**, and the
engines hold only their driver plumbing. That is deliberate: every repo in
this family compares three hand-maintained CLIs with a test, and every one of
them has at some point found the copies disagreeing. Here a flag cannot exist
on one engine and not its twins.

## Output

One JSON or CSV file plus a `<out>.meta.json` sidecar per run. The first
eighteen columns are the family prefix, byte-identical and in order across
every repo in this family; Play's own follow. See
[`sample_output.json`](sample_output.json).

Exit codes: `0` ok · `1` crash · `2` bad usage · `3` blocked · `4` zero
apps · `5` **the content was never obtained** · `6` partial.

`5` and `4` are the distinction worth knowing: a run that never got its page
must not report "the catalogue is empty", or a dead proxy and an empty
category look the same to whatever reads the file. A run that gathered
nothing and did not finish reports `5` and **writes no file at all**, so last
night's good output is still there.

## Traps that look like bugs

* **Two runs of a popular app differ on every counter.** WhatsApp gained 10
  ratings in the 4.5 minutes between two engine runs here. `diff_runs.py` will
  show it. It is the store moving, not the scraper.
* **A package id that does not exist gives exit 4, not exit 3.** Play answers
  it with a clean HTTP 404, and this scraper calls that `not_found` — an
  answer, not a refusal. Check the id before you check your proxy.
* **The genre name changes with the language and the key does not.** Join
  cross-market data on `genre_id` (`COMMUNICATION`), never on `category`
  ("Kommunikation").
* **`brand` is the developer, not the seller.** The store's own payload holds
  a seller-of-record beside the developer, and in the EU it says "Google
  Commerce Ltd". This reads the developer on every market. If you see
  Google's name in that column, something has changed.
* **`rating` is the figure the store displays, not the float beside it.** The
  same app page states 4.616471 in its payload and 4.616447925567627 in its
  own JSON-LD, in one fetch. `rating` is 4.6, and `rating_raw` keeps the
  payload's number so the rounding stays auditable.
* **`/store/books` and `/store/movies` are refused, with the reason.** They
  are real Play storefronts; this repo has never captured or parsed them, and
  reading them with the apps parser would report an empty catalogue.

## Configuration

Credentials live in `.env` beside the scripts, never on a command line.
`python3 env_config.py` prints what was picked up without printing secrets —
run it first when a key "isn't working". See [`.env.example`](.env.example).

## Tests

```bash
python3 smoke_test.py          # no engine library needed
pytest                          # the same checks, wrapped
```

The fixtures are cut from the real captures in `captures/`, and the checks
pin **values** rather than coverage: a column can be 100% populated and
entirely wrong.

## Licence

MIT. Scraping may be restricted by the target site's terms; you are
responsible for how you use this.
