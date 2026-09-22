#!/usr/bin/env python3
"""
scrub_fixtures.py
-----------------
Replace the personal material in a captured Play response with obvious
placeholders, in place, by STRUCTURE rather than by regex.

Play publishes a reviewer's display name, avatar and stable 21-digit profile
id beside their words. Republishing those in a public repo is a separate act
from the store showing them on its own page, and the checks in
`smoke_test.py` need the SHAPE of a review, not the person.

Run this on any new capture before committing it. `smoke_test.py` guards the
result by pattern, so a capture that skips this step fails the suite rather
than reaching a commit.

    python3 scrub_fixtures.py captures/*.html captures/reviews_*.txt
"""

import json
import re
import sys

NAMES = ["Reviewer One", "Reviewer Two", "Reviewer Three", "Reviewer Four",
         "Reviewer Five"]
AVATAR = "https://play-lh.googleusercontent.com/AVATAR-REDACTED"
BODY = ("Placeholder review text used as a fixture. The wording here is not a "
        "real person&#39;s; everything the store generates around it is "
        "untouched, including the HTML entities and the <br> breaks the "
        "payload really carries.<br>A second line, so entity and break "
        "handling stays exercised.")

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
                   r"[0-9a-f]{12}$", re.I)
_PROFILE_ID = re.compile(r"^\d{20,22}$")

# A reviewer avatar is `/a-/…` or `/a/…` — the SLASH is what distinguishes
# it from an app image, whose id can begin `a-` by coincidence
# (`googleusercontent.com/a-9Odoyp954La88oEr0xIeBezKqhK58aoWuBfl6UnC` is a
# game icon on one capture). A looser pattern flags six app icons as
# reviewer photos, and a guard people have to argue with is one they learn
# to switch off.
_AVATAR_URL = re.compile(r"googleusercontent\.com/a-?/")


def _is_review(node):
    return (isinstance(node, list) and len(node) >= 11
            and isinstance(node[0], str) and _UUID.match(node[0])
            and isinstance(node[2], int) and 1 <= node[2] <= 5)


def _scrub_avatars(node):
    """Any Play avatar URL anywhere under `node`."""
    if isinstance(node, list):
        for i, v in enumerate(node):
            if isinstance(v, str) and _AVATAR_URL.search(v):
                node[i] = AVATAR
            else:
                _scrub_avatars(v)


def scrub_tree(node, counter):
    """Walk any parsed payload and scrub every review record in it."""
    if not isinstance(node, list):
        return
    if _is_review(node):
        name = NAMES[counter["n"] % len(NAMES)]
        pid = str(100000000000000000000 + counter["n"])
        counter["n"] += 1
        # slot 1: [display name, [avatar envelope]]
        if isinstance(node[1], list) and node[1] and isinstance(node[1][0], str):
            node[1][0] = name
        # slot 4: the review body
        if isinstance(node[4], str):
            node[4] = BODY
        # slot 9: [profile id, display name, ..., [avatar envelope]]
        if isinstance(node[9], list) and node[9]:
            if isinstance(node[9][0], str) and _PROFILE_ID.match(node[9][0]):
                node[9][0] = pid
            if len(node[9]) > 1 and isinstance(node[9][1], str):
                node[9][1] = name
        _scrub_avatars(node)
        return
    for child in node:
        scrub_tree(child, counter)


def scrub_batchexecute(text, counter):
    start = text.find('[["wrb.fr"')
    if start < 0:
        return text, 0
    envelope, end = json.JSONDecoder().raw_decode(text[start:])
    changed = 0
    for entry in envelope:
        if (isinstance(entry, list) and len(entry) >= 3
                and entry[0] == "wrb.fr" and isinstance(entry[2], str)):
            inner = json.loads(entry[2])
            before = counter["n"]
            scrub_tree(inner, counter)
            changed += counter["n"] - before
            entry[2] = json.dumps(inner, ensure_ascii=False,
                                  separators=(",", ":"))
    rebuilt = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    return text[:start] + rebuilt + text[start + end:], changed


_AF = re.compile(r"(AF_initDataCallback\(\{.*?data:)(\s*\[.*?\])(\s*,\s*sideChannel)",
                 re.S)


def scrub_html(text, counter):
    changed = [0]

    def replace(match):
        try:
            data = json.loads(match.group(2))
        except ValueError:
            return match.group(0)
        before = counter["n"]
        scrub_tree(data, counter)
        changed[0] += counter["n"] - before
        if counter["n"] == before:
            return match.group(0)
        return match.group(1) + json.dumps(data, ensure_ascii=False,
                                           separators=(",", ":")) + match.group(3)

    out = _AF.sub(replace, text)
    # Any avatar left in the rendered markup, outside a data block.
    out = re.sub(r"https://play-lh\.googleusercontent\.com/a[-/][A-Za-z0-9_\-/=]+",
                 AVATAR, out)
    return out, changed[0]


def main(paths):
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        counter = {"n": 0}
        if path.endswith(".txt"):
            out, changed = scrub_batchexecute(text, counter)
        else:
            out, changed = scrub_html(text, counter)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(out)
        print(f"{path}: {changed} review records scrubbed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
