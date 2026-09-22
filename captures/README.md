# captures/

Real responses from `play.google.com`, taken 2026-09-22 from a datacentre
address (netcup, Nuremberg) with plain `curl` and no cookies. Every number in
the README and in `product_parser.py`'s comments can be re-measured from
these.

| file | route | what it is for |
|---|---|---|
| `cat_game_us.html` | `/store/apps/category/GAME` | the grid, and seven named shelves |
| `cat_game_de.html` | the same, `hl=de&gl=DE` | the second locale, which found the `brand` bug |
| `cat_tools_us.html` | `/store/apps/category/TOOLS` | a second, smaller category |
| `search_vpn_us.html` | `/store/search?q=vpn&c=apps` | a flat result list with no shelves |
| `search_vpn_jp.html` | the same, `hl=ja&gl=JP` | non-Latin titles |
| `app_free_us.html` | `/store/apps/details?id=com.whatsapp` | a free app, and 20 embedded reviews |
| `app_free_de.html` | the same, `hl=de&gl=DE` | proves the rating is per-market |
| `app_iap_us.html` | Minecraft | a paid app with in-app purchases |
| `dev_us.html` | `/store/apps/dev?id=…` | a developer grid |
| `apps_home_us.html` | `/store/apps` | the store front, with the chart shelves |
| `cluster_us.html` | `/store/apps/collection/cluster?gsr=…` | an expanded shelf — what `--pages` follows |
| `notfound.html` | a package id that does not exist | HTTP 404, and the control for the asset-count signal |
| `reviews_p1.txt` | `batchexecute` RPC `UsvDTd` | 40 reviews and a continuation token |
| `reviews_p2.txt` | the same, with that token | 40 more, sharing none of page 1's ids |

## What is NOT verbatim

**The two `reviews_*.txt` files are scrubbed, and deliberately.** Play
publishes a reviewer's display name, their avatar and a stable 21-digit
profile id beside their words, and republishing those in a public repo is a
separate act from the store showing them on its own page.

Replaced with obvious placeholders:

* every display name -> `Reviewer One` … `Reviewer Five`
* every profile id -> a sequential fake in the same 21-digit shape
* every avatar URL -> `…/AVATAR-REDACTED`, keeping the host
* every review body -> one placeholder paragraph that keeps the HTML entities
  and `<br>` breaks the real payload carries, so the entity handling stays
  exercised

Everything the store generates around them — the record shape, the scores,
the timestamps, the thumbs-up counts, the app versions, the continuation
token — is untouched, because that is what the checks actually read.

`smoke_test.py` guards this by PATTERN rather than by these literals
(`test_fixtures_carry_no_real_reviewers`), so the next capture is caught too.

## What is TRIMMED

Every `.html` file is cut down to the parts the parser reads: the
`AF_initDataCallback` data blocks, the single JSON-LD block an app page
carries, the shelf links, and a handful of image-host references so the
structural "was this served" signal still has something to count. That takes
the set from **18.2 MB to 4.8 MB**.

Each file was verified to parse **byte-identically** to the original it was
cut from before it was written back — same rows, same column values, same
shelves, same cluster links, same page state — and `make_fixtures.py` plus
`scrub_fixtures.py` reproduce the whole set from the live site.

`notfound.html` is left verbatim: it is 1.6 KB, and its value is precisely
that it carries none of the store's own image host.

All other content is exactly what the store served.
