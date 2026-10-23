#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# L1 package-ecosystem probe: install each popcon top package with apt and run
# a smoke check. One line per package on the console so the host can scrape it.
#
# Runs inside the guest; the package list is /usr/local/share/popcon_top50.txt
# (injected by daily_use_gate.sh --popcon). Each package gets at most
# POPCON_ATTEMPTS (default 2) install attempts; a failure is recorded and the
# probe moves on. The console line carries the failure reason so the host can
# attribute it without reading back the guest disk:
#
#   POPCON pkg=<name> rc=<n> smoke=<ok|cli|lib|no|na> dur=<s> tries=<n> err=<text>
#
# smoke=ok   a same-named binary runs --version/--help and exits 0
# smoke=cli  the binary ran and rejected the flag (no --version interface)
# smoke=lib  the package installs no binary under its own name (data/library)
# smoke=no   a binary exists but neither ran nor reported a usage/option error
# smoke=na   the install failed, so there was nothing to smoke-test

LIST=${POPCON_LIST:-/usr/local/share/popcon_top50.txt}
OUT=${POPCON_OUT:-/tmp/popcon-result.json}
ATTEMPTS=${POPCON_ATTEMPTS:-2}
INSTALL_TIMEOUT=${POPCON_INSTALL_TIMEOUT:-300}

# Packages whose executable is not named after the package. Without this the
# smoke column would say "lib" for tools the user would actually run.
smoke_command() {
    case "$1" in
        perl-base) echo perl ;;
        libc-bin) echo ldconfig ;;
        ncurses-bin) echo infocmp ;;
        openssh-client) echo ssh ;;
        gettext-base) echo envsubst ;;
        debianutils) echo which ;;
        *) echo "$1" ;;
    esac
}

# Console-safe token: no whitespace, quotes, or backslashes, bounded length.
sanitize() {
    tr '\n\r\t' '   ' | tr -c 'A-Za-z0-9._=+:/@,-' '_' | cut -c1-120
}

: > "$OUT"

# tzdata and friends must never open a debconf prompt on the serial console.
export DEBIAN_FRONTEND=noninteractive
APT_OPTS="-o DPkg::Lock::Timeout=120 -o Acquire::Retries=2"

updated=0
total_start=$(cut -d' ' -f1 /proc/uptime)

# The list lives on fd 3 and every child gets </dev/null: an apt postinst that
# reads stdin would otherwise eat the next package names off the list.
while read -r pkg <&3; do
    [ -n "$pkg" ] || continue
    case "$pkg" in \#*) continue ;; esac

    start=$(cut -d' ' -f1 /proc/uptime)
    rc=1
    tries=0
    while [ "$tries" -lt "$ATTEMPTS" ]; do
        tries=$((tries + 1))
        # shellcheck disable=SC2086 # APT_OPTS is intentionally word-split
        if timeout "$INSTALL_TIMEOUT" apt-get install -y --no-install-recommends \
            $APT_OPTS "$pkg" >"/tmp/popcon-$pkg.log" 2>&1 </dev/null; then
            rc=0
            break
        fi
        rc=$?
        # A missing package index is retried once after `apt-get update`;
        # everything else is retried as-is (transient mirror/lock errors).
        if [ "$rc" -ne 124 ] && [ "$updated" -eq 0 ] &&
            grep -q 'Unable to locate package' "/tmp/popcon-$pkg.log" 2>/dev/null; then
            timeout 300 apt-get update >>/tmp/popcon-update.log 2>&1 </dev/null
            updated=1
        fi
        [ "$tries" -lt "$ATTEMPTS" ] && sleep 1
    done
    end=$(cut -d' ' -f1 /proc/uptime)
    dur=$(awk -v a="$start" -v b="$end" 'BEGIN{printf "%.2f", b-a}')

    smoke=na
    err=""
    if [ "$rc" -eq 0 ]; then
        cmd=$(smoke_command "$pkg")
        if command -v "$cmd" >/dev/null 2>&1; then
            v_out=$(timeout 10 "$cmd" --version 2>&1 </dev/null)
            v_rc=$?
            h_out=$(timeout 10 "$cmd" --help 2>&1 </dev/null)
            h_rc=$?
            V_out=$(timeout 10 "$cmd" -V 2>&1 </dev/null)
            V_rc=$?
            if [ "$v_rc" -eq 0 ] || [ "$h_rc" -eq 0 ] || [ "$V_rc" -eq 0 ]; then
                smoke=ok
            elif printf '%s\n' "$v_out" "$h_out" "$V_out" |
                grep -qiE 'invalid option|unknown option|illegal option|unrecognized option|usage:'; then
                # The process ran and parsed argv; it just has no version flag
                # (cron, debconf, debianutils). That is not a compatibility bug.
                smoke=cli
                err=$(printf '%s\n' "$v_out" | head -1 | sanitize)
            else
                smoke=no
                err=$(printf '%s\n' "$v_out" | head -1 | sanitize)
            fi
        else
            smoke=lib
        fi
    else
        if [ "$rc" -eq 124 ]; then
            err="install_timeout_${INSTALL_TIMEOUT}s"
        else
            err=$(grep -E '^(E:|dpkg:|Err:|W: )' "/tmp/popcon-$pkg.log" 2>/dev/null |
                tail -2 | sanitize)
            [ -n "$err" ] || err=$(tail -2 "/tmp/popcon-$pkg.log" 2>/dev/null | sanitize)
        fi
        [ -n "$err" ] || err="rc_${rc}_no_log"
    fi

    echo "POPCON pkg=$pkg rc=$rc smoke=$smoke dur=$dur tries=$tries err=$err" >/dev/console
    printf '{"pkg":"%s","rc":%d,"smoke":"%s","dur":%s,"tries":%d,"err":"%s"}\n' \
        "$pkg" "$rc" "$smoke" "$dur" "$tries" "$err" >>"$OUT"
done 3<"$LIST"

total_end=$(cut -d' ' -f1 /proc/uptime)
total=$(awk -v a="$total_start" -v b="$total_end" 'BEGIN{printf "%.2f", b-a}')
count=$(grep -c '"pkg"' "$OUT" 2>/dev/null)
ok=$(grep -c '"rc":0' "$OUT" 2>/dev/null)
smoke_ok=$(grep -c '"smoke":"ok"' "$OUT" 2>/dev/null)
smoke_cli=$(grep -c '"smoke":"cli"' "$OUT" 2>/dev/null)
echo "POPCON_DONE count=$count install_ok=$ok smoke_ok=$smoke_ok smoke_cli=$smoke_cli total_dur=$total" >/dev/console
