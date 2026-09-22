#!/usr/bin/env python3
"""
make_fixtures.py
----------------
Re-take the capture set in `captures/`, then scrub it.

Run this when Play changes something and the fixtures stop describing the
site. It fetches with plain `requests` and no cookies — which is how the
original set was taken, and is itself part of what the README claims — then
hands every capture to `scrub_fixtures.py`, so a new set cannot reach a
commit carrying a real reviewer's name.

    python3 make_fixtures.py            # re-take everything
    python3 make_fixtures.py app_free_us cat_game_de

`smoke_test.py` pins VALUES against these files, so a re-capture will fail
the suite wherever the store has genuinely moved — a rating, an install
count, a version. That is the point: the failure is the notification, and the
fix is to re-read what changed and update the expected value with today's
date beside it, not to loosen the check.
"""

import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CAPTURES = os.path.join(HERE, "captures")

sys.path.insert(0, HERE)
import product_parser as parser  # noqa: E402

# A real browser's User-Agent. Not a disguise: this site serves curl's own UA
# just as happily, and the point of using a browser string here is that the
# captures should look like what a run actually fetches.
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/140.0.0.0 Safari/537.36")

PAGES = {
    "cat_game_us":    (parser.listing_url("GAME", "en", "US"), 200),
    "cat_game_de":    (parser.listing_url("GAME", "de", "DE"), 200),
    "cat_tools_us":   (parser.listing_url("TOOLS", "en", "US"), 200),
    "search_vpn_us":  (parser.search_url("vpn", "en", "US"), 200),
    "search_vpn_jp":  (parser.search_url("vpn", "ja", "JP"), 200),
    "app_free_us":    (parser.app_url("com.whatsapp", "en", "US"), 200),
    "app_free_de":    (parser.app_url("com.whatsapp", "de", "DE"), 200),
    "app_iap_us":     (parser.app_url("com.mojang.minecraftpe", "en", "US"), 200),
    "dev_us":         (parser.developer_url("5700313618786177705", "en", "US"), 200),
    "apps_home_us":   ("https://play.google.com/store/apps?hl=en&gl=US", 200),
    # The control for the structural signal: a package id that does not exist.
    # Play answers it with a clean 404 carrying NONE of its own image host,
    # which is what makes an asset count usable as "was this served".
    "notfound":       (parser.app_url("com.does.not.exist.zzz", "en", "US"), 404),
}

# Taken from whatever the category page publishes today, because a `gsr`
# token is not stable and pasting one here would leave a line that 404s.
CLUSTER_FROM = "cat_game_us"


def fetch(url, expect_status=200):
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return response.read().decode("utf-8", "replace"), response.status
    except urllib.error.HTTPError as exc:
        return exc.read().decode("utf-8", "replace"), exc.code


def write(name, text):
    path = os.path.join(CAPTURES, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"  {name}: {len(text)} bytes")
    return path


def main(only):
    os.makedirs(CAPTURES, exist_ok=True)
    written = []
    html_by_name = {}

    for name, (url, expect) in PAGES.items():
        if only and name not in only:
            continue
        text, status = fetch(url, expect)
        if status != expect:
            print(f"  {name}: expected HTTP {expect}, got {status} — NOT written")
            continue
        html_by_name[name] = text
        written.append(write(name + ".html", text))
        time.sleep(1.5)

    if not only or "cluster_us" in only:
        source = html_by_name.get(CLUSTER_FROM)
        if source is None:
            source, _ = fetch(PAGES[CLUSTER_FROM][0])
        links = parser.cluster_links(source, "en", "US")
        if links:
            text, status = fetch(links[0])
            if status == 200:
                written.append(write("cluster_us.html", text))
        else:
            print("  cluster_us: the category page published no shelf links")
        time.sleep(1.5)

    if not only or "reviews" in only:
        token = None
        for page in (1, 2):
            form = parser.build_reviews_request("com.whatsapp", 40, token, "newest")
            body = urllib.parse.urlencode(form).encode()
            request = urllib.request.Request(
                parser.batchexecute_url("en", "US"), data=body,
                headers={"User-Agent": UA,
                         "content-type": "application/x-www-form-urlencoded;charset=UTF-8"})
            with urllib.request.urlopen(request, timeout=45) as response:
                text = response.read().decode("utf-8", "replace")
            written.append(write(f"reviews_p{page}.txt", text))
            _records, token = parser.reviews_from_payload(parser.parse_batchexecute(text))
            if not token:
                break
            time.sleep(1.5)

    if written:
        print("\nscrubbing personal material out of the new captures:")
        subprocess.run([sys.executable, os.path.join(HERE, "scrub_fixtures.py")]
                       + written, check=True)
    print(f"\n{len(written)} capture(s) rewritten. Run `python3 smoke_test.py` "
          f"next: where the store has genuinely moved, a pinned value will "
          f"fail, and that failure is the notification.")
    return 0


if __name__ == "__main__":
    sys.exit(main(set(sys.argv[1:])))
