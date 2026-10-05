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
# A directory is NOT a release. Observed 2026-10-03..05: upstream created
# media/r151060/ EMPTY (just the "../" link) weeks before the release,
# while omnios.org/download still named r151058 as the current stable.
# Reporting r151060 then made watch.py derive
# media/r151060/omnios-r151060.iso, whose HEAD 404'd, and the whole run
# went red every night (issue #4) although nothing was wrong. So a
# version is reported only when its directory listing carries the
# omnios-<version>.iso the confs point at; an empty or ISO-less
# directory is skipped and the newest populated one is reported instead.
# A populated directory looks like
#   <td class="name"><a href="omnios-r151058.iso" ...>omnios-r151058.iso</a></td>
# next to .usb-dd, .zfs.xz, .cloud.* siblings (checked 2026-10-05).
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


def has_iso(listing, version):
    """True when the directory listing links the installer ISO.

    The confs' VM_ISO_LINK is media/<version>/omnios-<version>.iso, so
    that exact filename is the one that decides "released". Anchored on
    the href so a stray mention elsewhere on the page cannot match.
    """
    return re.search(r'href="omnios-%s\.iso"' % re.escape(version),
                     listing) is not None


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
    # Newest first; stop at the first directory that really holds the ISO.
    # Normally that is the very first one (one extra fetch); a pre-created
    # empty directory costs one more and is reported on stderr so the run
    # log says why the newer name was passed over.
    skipped = []
    for version in sorted(set(versions), key=key, reverse=True):
        dir_url = URL + version + "/"
        try:
            listing = fetch(dir_url)
        except Exception as e:
            sys.stderr.write("upstream_check: fetch of %s failed: %s\n"
                             % (dir_url, e))
            return 1
        if has_iso(listing, version):
            for empty in skipped:
                sys.stderr.write("upstream_check: %s%s/ exists but has no "
                                 "omnios-%s.iso yet; not released\n"
                                 % (URL, empty, empty))
            print(version)
            return 0
        skipped.append(version)
    sys.stderr.write("upstream_check: none of %s carries an omnios-<ver>.iso "
                     "under %s; page shape may have changed\n"
                     % (", ".join(sorted(set(versions), key=key)), URL))
    return 1


if __name__ == "__main__":
    sys.exit(main())
