#!/usr/bin/env python3
# Print the newest OmniOS release with published media, e.g. "r151058".
# Empty output means "nothing detected" and is not an error; a non-zero
# exit means detection itself is broken (network error, HTTP error, or a
# page that no longer matches the expected shape) and must be reported by
# the caller, never swallowed. A failure must NEVER print a
# plausible-but-wrong version -- the version is only printed after every
# step below has succeeded.
#
# Source of truth: https://downloads.omnios.org/media/
# Fetched and confirmed by hand (2026-07-26): the directory is a custom
# (non-Apache) HTML table, one <tr class="directory"> row per subdirectory,
# e.g.
#   <tr class="directory">
#   <td class="icon">...</td>
#   <td class="name"><a href="r151058/" class="orange-text">r151058/</a></td>
#   ...
# Numbered release directories sit directly under media/ (r151030 through
# r151058 at the time of checking), alongside unrelated channel directories
# archive/, bloody/, braich/, lts/, lx/, misc/ that are NOT release numbers
# and must never be picked up. Only "r" followed by digits, immediately
# followed by the closing slash, counts -- this also excludes bloody/braich
# which are named without the r151NNN shape.
#
# stdlib only (urllib.request, re, sys, os) -- no external dependencies.

import os
import re
import sys
import urllib.request

URL = "https://downloads.omnios.org/media/"
TIMEOUT = 60
USER_AGENT = "anyvm-org-upstream-watcher/1.0"

# Captures the whole "r151058" token (including the "r" prefix), which is
# exactly the VM_RELEASE form the confs use.
PATTERN = re.compile(r'href="(r\d+)/"')


def resolve_natural_key():
    """Return the engine's own natural_key, or fail loudly.

    watch.yml clones base-builder INTO the builder repo root, so at
    detection time it sits at "base-builder/" (relative to this hook's
    cwd, the builder repo root). A local checkout instead has it as a
    sibling, "../base-builder". Try both, in that order.

    There is deliberately NO local fallback copy. Ordering must be the
    single rule the engine uses -- a per-hook duplicate would have to be
    kept in sync by hand across every builder and would drift silently,
    and a hook that ranks versions differently from watch.py is worse
    than one that refuses to run. Both real contexts (CI and a local
    sibling checkout) always provide base-builder, so an ImportError here
    means the environment is wrong: report it as broken detection rather
    than guessing an order.
    """
    for candidate in ("base-builder", os.path.join("..", "base-builder")):
        if not os.path.isdir(candidate):
            continue
        path = os.path.abspath(candidate)
        if path not in sys.path:
            sys.path.insert(0, path)
        try:
            import gendata
            return gendata.natural_key
        except ImportError:
            continue
    raise ImportError(
        "base-builder/gendata.py not importable from %s; expected it at "
        "./base-builder (CI) or ../base-builder (local checkout)"
        % os.getcwd())


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", "replace")


def main():
    try:
        key = resolve_natural_key()
    except ImportError as e:
        sys.stderr.write("upstream_check: %s\n" % e)
        return 1
    try:
        html = fetch(URL)
    except Exception as e:
        sys.stderr.write("upstream_check: fetch of %s failed: %s\n"
                         % (URL, e))
        return 1
    versions = PATTERN.findall(html)
    if not versions:
        sys.stderr.write("upstream_check: no rNNNNNN directory found in "
                         "%s; page shape may have changed\n" % URL)
        return 1
    newest = sorted(set(versions), key=key)[-1]
    print(newest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
