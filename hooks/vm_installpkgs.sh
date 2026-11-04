# In-guest install script for omnios (piped into the guest sh by build.py
# with ANYVM_PKGS prepended; runs under set -e). The guest /bin/sh is ksh93 --
# keep this POSIX.
#
# Why it retries: 12 of 46 failed omnios-builder jobs (2026-08 .. 11-03) lost
# the plain `pkg install` to
#   Framework error: code: E_COULDNT_RESOLVE_HOST (6) reason: Could not
#   resolve host: pkg.omnios.org
# The whole pkg run failed in about 1 s, the failures came in clusters across
# runners, and in 12 of 17 cases the same error was back after build.py's
# reboot-and-retry about 2 minutes later. So a transport error is retried
# here for up to ANYVM_PKG_RETRY_SECONDS before giving up, and the guest's
# network state is printed so the log says whether the guest had lost DNS
# entirely (github.com fails too) or only the omnios.org name failed.
#
# pkg(1) EXIT STATUS: 0 = success, 4 = "No changes were made - nothing to
# do." (both are success here), 7 = image locked by another process (retried).
# Any other failure without a transport error in its output (e.g. a package
# name that does not exist) is returned at once: waiting cannot fix it.
# The loop is bounded and exits non-zero, so build.py's reboot + one more
# attempt still applies and a lasting outage still fails the build.

retry_seconds=${ANYVM_PKG_RETRY_SECONDS:-600}
retry_sleep=${ANYVM_PKG_RETRY_SLEEP:-30}
# Whole seconds only, and the sleep at least 1: the fallback clock below
# counts sleeps, so a zero sleep would never reach the deadline.
case $retry_seconds in ''|*[!0-9]*) retry_seconds=600 ;; esac
case $retry_sleep in ''|*[!0-9]*) retry_sleep=30 ;; esac
if [ "$retry_sleep" -lt 1 ]; then
    retry_sleep=1
fi
pkg_log=/tmp/anyvm-pkg-install.$$.log
pkg_rcf=/tmp/anyvm-pkg-install.$$.rc

# ksh93 and bash keep the shell's age in SECONDS (ksh93 as a float, hence the
# %.*); without it, count each try as one sleep.
start=${SECONDS:-}
start=${start%.*}

elapsed() {
    if [ -n "$start" ]; then
        now=${SECONDS%.*}
        echo $((now - start))
    else
        echo $((try * retry_sleep))
    fi
}

# Only the resolver lookups: one line per host, every try.
resolve_check() {
    for h in pkg.omnios.org github.com; do
        if out=`getent hosts "$h" 2>&1`; then
            echo "getent hosts $h: $out"
        else
            echo "getent hosts $h: FAILED rc=$? $out"
        fi
    done
}

# The full network picture. Every command is allowed to fail.
net_diag() {
    echo "--- /etc/resolv.conf"
    cat /etc/resolv.conf || true
    echo "--- /etc/nsswitch.conf hosts"
    grep '^hosts' /etc/nsswitch.conf || true
    nameservers=`awk '$1 == "nameserver" { print $2 }' /etc/resolv.conf \
        2>/dev/null` || true
    for ns in $nameservers; do
        if command -v dig >/dev/null 2>&1; then
            echo "--- dig @$ns pkg.omnios.org"
            dig +time=3 +tries=1 "@$ns" pkg.omnios.org A || true
        elif command -v nslookup >/dev/null 2>&1; then
            echo "--- nslookup pkg.omnios.org $ns"
            nslookup -timeout=3 -retry=1 pkg.omnios.org "$ns" || true
        fi
    done
    if command -v curl >/dev/null 2>&1; then
        echo "--- curl https://pkg.omnios.org/"
        curl -sS -o /dev/null --max-time 15 -w 'http %{http_code}\n' \
            https://pkg.omnios.org/ || true
    fi
    echo "--- netstat -rn"
    netstat -rn || true
    echo "--- ipadm show-addr"
    ipadm show-addr || true
    echo "--- dladm show-phys"
    dladm show-phys || true
    echo "--- dladm show-link"
    dladm show-link || true
    echo "--- svcs"
    svcs network/physical network/dns/client || true
    echo "--- pkg publisher"
    pkg publisher || true
}

try=0
while :; do
    try=$((try + 1))
    # pkg's own rc goes through a file: the pipeline's status is tee's.
    { rc=0; pkg install $ANYVM_PKGS || rc=$?; echo "$rc" >"$pkg_rcf"; } 2>&1 \
        | tee "$pkg_log"
    rc=`cat "$pkg_rcf" 2>/dev/null` || true
    rc=${rc:-1}
    if [ "$rc" = 0 ] || [ "$rc" = 4 ]; then
        rm -f "$pkg_log" "$pkg_rcf"
        exit 0
    fi

    transient=""
    if [ "$rc" = 7 ]; then
        transient="image locked"
    elif grep -i -E 'Framework error|Unable to contact|protocol error' \
            "$pkg_log" >/dev/null 2>&1; then
        transient="transport error"
    fi
    spent=`elapsed`
    echo "==== pkg install failed rc=$rc (try $try, ${spent}s of" \
        "${retry_seconds}s): ${transient:-not a transport error}"
    if [ -z "$transient" ]; then
        rm -f "$pkg_log" "$pkg_rcf"
        exit "$rc"
    fi
    # getent is bounded by the resolver's own timeout/retry settings, and
    # under ksh93 the deadline is checked on the wall clock (SECONDS), so a
    # slow lookup can only stretch the last try.
    date || true
    resolve_check || true
    if [ "$try" = 1 ] || [ "$spent" -ge "$retry_seconds" ]; then
        net_diag
    fi
    if [ "$spent" -ge "$retry_seconds" ]; then
        echo "==== pkg install still failing after ${spent}s; giving up"
        rm -f "$pkg_log" "$pkg_rcf"
        exit "$rc"
    fi
    sleep "$retry_sleep"
done
