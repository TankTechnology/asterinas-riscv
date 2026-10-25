#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# L4 source-package build probe: prove that a Debian source package can be
# compiled into a .deb inside the Asterinas guest.
#
# Runs inside the guest; the host injects it as /usr/local/bin/dpkg-build via
# daily_use_gate.sh --source-build. For each package, in this order:
#
#   apt-get build-dep -y <pkg>   install the declared Build-Depends
#   apt-get source <pkg>         fetch and extract the source package
#   dpkg-buildpackage -b -us -uc build binary packages (no source signing)
#
# The console carries one line per event so the host can scrape it:
#
#   DPKGB pkg=<n> start name=<pkg>
#   DPKGB pkg=<n> stage=<builddeps|source|build> rc=<n> dur=<s> attempt=<k>
#   DPKGB pkg=<n> artifact=<deb>
#   DPKGB pkg=<n> err=<summary>
#   DPKGB pkg=<n> evidence=<the four log lines before make's wrapper error>
#   DPKGB pkg=<n> done name=<pkg> result=<ok|fail|timeout|disk> attempts=<k> dur=<s>
#   DPKGB env <key>=<value>
#   DPKGB setup step=<name> rc=<n>
#   DPKGB summary total=<n> ok=<n> fail=<n>
#   DPKGB_DONE
#
# Discipline borrowed from popcon_packages.sh: each package is emitted as its
# own line, a sanitized error token explains a failure without reading back the
# guest disk, and every child gets </dev/null so an apt prompt cannot eat the
# package list.
#
# A package gets at most DPKGB_ATTEMPTS (default 2) attempts and at most
# DPKGB_PKG_TIMEOUT (default 900 s) of wall clock *in total*. A timeout ends the
# package immediately (retrying a 15-minute build only burns another 15
# minutes); other failures are retried while the budget allows.
#
# Sources: the base image has only `deb` entries, so the probe adds matching
# `deb-src` lines once and refreshes the index before the first build-dep.

PKGS=${DPKGB_PKGS:-"hello coreutils python3-minimal"}
ATTEMPTS=${DPKGB_ATTEMPTS:-2}
PKG_TIMEOUT=${DPKGB_PKG_TIMEOUT:-900}
# The build tree lives on /tmp, a 2 GiB tmpfs, on purpose. The ext4 driver
# encodes one JBD2 descriptor per transaction and allows at most 253 metadata
# blocks in it (kernel/core/src/fs/fs_impls/ext2/journal.rs:238). A full build
# leaves more than that dirty, so dpkg-deb's fsync of the finished .deb fails
# with E2BIG ("unable to sync file"). tmpfs has no journal and its sync is a
# no-op, so the same fsync succeeds. Logs and artifacts stay on the root ext2
# because tmpfs does not survive power-off.
BASE=${DPKGB_WORKDIR:-/tmp/dpkg-build}
ARTDIR=${DPKGB_ARTDIR:-/var/tmp/dpkg-artifacts}
LOGDIR=${DPKGB_LOGDIR:-/var/tmp/dpkg-logs}
MIN_FREE_KB=${DPKGB_MIN_FREE_KB:-262144}
SHIMS=${DPKGB_SHIMS:-1}
SHIMDIR=${DPKGB_SHIMDIR:-/var/tmp/dpkg-shims}

while [ $# -gt 0 ]; do
    case "$1" in
        --packages) PKGS="$2"; shift 2 ;;
        *) echo "dpkg-build: unknown argument: $1" >&2; exit 2 ;;
    esac
done

emit() { printf 'DPKGB %s\n' "$*" >/dev/console; }
# One console token: whitespace becomes '_', but trailing whitespace (the
# newline every command substitution leaves) is dropped rather than turned
# into a stray trailing underscore.
sanitize() {
    tr '\n\r\t' '   ' | sed 's/[[:space:]]*$//' |
        tr -c 'A-Za-z0-9._=+:/@,-' '_' | cut -c1-160
}
now() { cut -d' ' -f1 /proc/uptime; }
elapsed() { awk -v a="$1" -v b="$2" 'BEGIN { printf "%.2f", b - a }'; }
free_kb() { df -k "$1" 2>/dev/null | awk 'NR==2 { print $4 }'; }

# First line of a log that looks like the cause, or its tail when nothing
# matches. The console token must stay on one line and contain no spaces.
err_summary() {
    log=$1
    rc=$2
    msg=$(grep -a -E '^(E:|W: |dpkg-source: error|dpkg-buildpackage: error|dpkg-gencontrol: error|make(\[[0-9]+\])?: \*\*\*|.*: error:|.*: fatal error:)' "$log" 2>/dev/null |
        tail -2 | sanitize)
    [ -n "$msg" ] || msg=$(grep -a -i -E 'no space left|function not implemented|operation not supported|command not found|unable to locate|no such file' "$log" 2>/dev/null |
        tail -2 | sanitize)
    [ -n "$msg" ] || msg=$(tail -2 "$log" 2>/dev/null | sanitize)
    [ -n "$msg" ] || msg="rc_${rc}_no_log"
    printf '%s' "$msg"
}

# The cause line is usually *before* make's and dpkg-buildpackage's own
# wrapper lines, so drop those and keep the four lines that precede them.
evidence() {
    log=$1
    msg=$(grep -a -v -E '^(make(\[[0-9]+\])?: \*\*\*|dpkg-buildpackage: (error|info))' "$log" 2>/dev/null |
        tail -4 | sanitize)
    [ -n "$msg" ] || msg=$(tail -4 "$log" 2>/dev/null | sanitize)
    printf '%s' "$msg"
}

# `timeout` for the rest of this package's budget, in whole seconds.
remaining() {
    awk -v t="$PKG_TIMEOUT" -v a="$pkg_start" -v b="$(now)" \
        'BEGIN { r = t - (b - a); if (r < 1) r = 1; printf "%d", r }'
}

emit "env arch=$(uname -m | sanitize)"
emit "env kernel=$(uname -r | sanitize)"
emit "env cpus=$(nproc 2>/dev/null || echo unknown)"
emit "env gcc=$(gcc --version 2>/dev/null | head -1 | sanitize)"
emit "env dpkg=$(dpkg --version 2>/dev/null | head -1 | sanitize)"
emit "env dpkg_buildpackage=$(dpkg-buildpackage --version 2>/dev/null | head -1 | sanitize)"
emit "env debian=$(cat /etc/debian_version 2>/dev/null | sanitize)"
emit "env packages=$PKGS"
emit "env pkg_timeout=$PKG_TIMEOUT attempts=$ATTEMPTS"

mkdir -p "$BASE" "$ARTDIR" "$LOGDIR" 2>/dev/null
emit "setup step=mkdir rc=$?"
emit "env logdir=$LOGDIR workdir=$BASE"
emit "env disk_free_kb=$(free_kb "$BASE")"

# ---------------------------------------------------------------- sources
# The image ships `deb https://... trixie main` and nothing else. `apt-get
# source` and `build-dep` both read the source index, which a `deb`-only
# sources.list does not provide.
added=0
for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list; do
    [ -f "$f" ] || continue
    grep -q '^[[:space:]]*deb-src[[:space:]]' "$f" && continue
    if grep -q '^[[:space:]]*deb[[:space:]]' "$f"; then
        grep '^[[:space:]]*deb[[:space:]]' "$f" |
            sed -E 's/^([[:space:]]*)deb[[:space:]]/\1deb-src /' >>"$f"
        added=1
    fi
done
for f in /etc/apt/sources.list.d/*.sources; do
    [ -f "$f" ] || continue
    grep -q '^Types:.*deb-src' "$f" && continue
    if grep -q '^Types:.*deb' "$f"; then
        sed -i -E 's/^Types:([[:space:]].*)?$/Types: deb deb-src/' "$f"
        added=1
    fi
done
emit "setup step=deb_src added=$added"
if [ "$added" -eq 1 ]; then
    # shellcheck disable=SC2086
    timeout 300 apt-get update -o Acquire::Retries=2 >"$LOGDIR/apt-update.log" 2>&1 </dev/null
    emit "setup step=apt_update rc=$?"
fi

# ------------------------------------------------------------- toolchain
missing=""
for tool in dpkg-buildpackage dpkg-source fakeroot gcc make; do
    command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
done
if [ -n "$missing" ]; then
    emit "setup step=toolchain_missing tools=$(printf '%s' "$missing" | sanitize)"
    # shellcheck disable=SC2086
    timeout 600 apt-get install -y --no-install-recommends \
        -o DPkg::Lock::Timeout=120 -o Acquire::Retries=2 \
        dpkg-dev build-essential fakeroot >"$LOGDIR/apt-toolchain.log" 2>&1 </dev/null
    rc=$?
    emit "setup step=apt_toolchain rc=$rc"
    if [ "$rc" -ne 0 ]; then
        emit "fatal reason=toolchain_install err=$(err_summary "$LOGDIR/apt-toolchain.log" "$rc")"
        printf 'DPKGB_DONE\n' >/dev/console
        exit 0
    fi
fi
emit "env gcc=$(gcc --version 2>/dev/null | head -1 | sanitize)"
emit "env dpkg=$(dpkg --version 2>/dev/null | head -1 | sanitize)"
emit "env dpkg_buildpackage=$(dpkg-buildpackage --version 2>/dev/null | head -1 | sanitize)"
emit "env fakeroot=$(fakeroot --version 2>/dev/null | head -1 | sanitize)"

# ------------------------------------------------------------------- shims
# Two kernel gaps break the Debian build tools before any package is compiled.
# Both are kernel features this port does not implement, so the probe works
# around them in the guest instead of touching the kernel:
#
#   splice(2)     GNU grep 3.11's drain_input() splices pipe input to stdout
#                 when stdout is /dev/null and accepts only EINVAL as "try
#                 read() instead" (src/grep.c:1772-1790). Asterinas answers
#                 ENOSYS, which grep reports as a read error, and autom4te's
#                 `m4 --help | grep reload-state >/dev/null` then concludes
#                 that m4 is too old. The shim gives grep a regular file when
#                 its stdout is /dev/null, so it takes the read() path.
#
#   SYSV IPC      fakeroot's default transport uses SysV message channels;
#                 the kernel reports "CLONE_SYSVSEM is not supported" and
#                 fakeroot exits with ENOSYS. fakeroot-tcp uses TCP sockets.
#                 Packages with Rules-Requires-Root: no never invoke it.
install_shims() {
    [ "$SHIMS" = 1 ] || return 0
    mkdir -p "$SHIMDIR" || return 1
    cat >"$SHIMDIR/grep" <<'EOF'
#!/bin/sh
# See dpkg_build.sh: avoid GNU grep's splice() drain path.
# Keep a private copy of stdout on fd 3: the fd 1 seen through a command
# substitution is the substitution's pipe, not this script's stdout.
exec 3>&1
if [ "$(readlink /proc/self/fd/3 2>/dev/null)" = "/dev/null" ]; then
    tmp="/var/tmp/dpkg-shims/grep-out.$$"
    /usr/bin/grep "$@" >"$tmp"
    rc=$?
    rm -f "$tmp"
    exit $rc
fi
exec /usr/bin/grep "$@"
EOF
    chmod 0755 "$SHIMDIR/grep"

    # fakeroot's sysv transport needs SysV message queues (unimplemented) and
    # its tcp transport dies at listen() with EINVAL here. The gate runs as
    # real uid 0, so fakeroot's only job -- making root-owned files appear
    # root-owned -- is already true; dpkg-buildpackage just needs a command
    # that runs debian/rules. Record both failures for the report.
    if command -v fakeroot-tcp >/dev/null 2>&1; then
        cat >"$SHIMDIR/fakeroot" <<'EOF'
#!/bin/sh
# Running as real root: no credential fakery is needed.
exec "$@"
EOF
        chmod 0755 "$SHIMDIR/fakeroot"
    fi
    PATH="$SHIMDIR:$PATH"
    export PATH
    emit "setup step=shims grep=splice fakeroot=root-exec path=$SHIMDIR"
}

# ---------------------------------------------------------- toolchain probe
# Optional, cheap reproduction of the environment facts a build failure turns
# on. `hello`'s autoreconf path died with autom4te claiming m4 was too old, so
# this pins down whether m4 itself runs and whether grep can read a pipe at all.
if [ "${DPKGB_SELFTEST:-0}" = 1 ]; then
    m4 --version >"$LOGDIR/selftest-m4-version.log" 2>&1 </dev/null
    rc=$?
    emit "selftest cmd=m4_version rc=$rc out=$(head -2 "$LOGDIR/selftest-m4-version.log" | sanitize)"

    printf 'define(x, 42)\nx\n' >"$LOGDIR/selftest-m4-in.txt"
    m4 "$LOGDIR/selftest-m4-in.txt" >"$LOGDIR/selftest-m4-file.log" 2>&1 </dev/null
    rc=$?
    emit "selftest cmd=m4_file rc=$rc out=$(head -2 "$LOGDIR/selftest-m4-file.log" | sanitize)"

    printf 'GNU M4 1.4.19\n' | grep 'GNU M4' >"$LOGDIR/selftest-grep-pipe.log" 2>&1
    rc=$?
    emit "selftest cmd=grep_pipe rc=$rc out=$(head -2 "$LOGDIR/selftest-grep-pipe.log" | sanitize)"

    m4 --version 2>&1 | grep 'GNU M4' >"$LOGDIR/selftest-m4-grep.log" 2>&1
    rc=$?
    emit "selftest cmd=m4_to_grep rc=$rc out=$(head -2 "$LOGDIR/selftest-m4-grep.log" | sanitize)"

    printf 'plain pipe payload\n' | cat >"$LOGDIR/selftest-cat-pipe.log" 2>&1
    rc=$?
    emit "selftest cmd=cat_pipe rc=$rc out=$(head -1 "$LOGDIR/selftest-cat-pipe.log" | sanitize)"

    emit "selftest cmd=autom4te_version rc=$(autom4te --version >"$LOGDIR/selftest-autom4te.log" 2>&1 </dev/null; echo $?) out=$(head -2 "$LOGDIR/selftest-autom4te.log" | sanitize)"

    # autom4te's GNU-m4 check is exactly this pipeline (autom4te:96-97):
    #   m4 --help </dev/null 2>&1 | grep reload-state
    # Reproduce it and split the variables: --help vs --version, and pipe vs
    # regular file.
    sh -c '/usr/bin/m4 --help </dev/null 2>&1 | grep reload-state' \
        >"$LOGDIR/selftest-m4help-grep.log" 2>&1 </dev/null
    rc=$?
    emit "selftest cmd=m4help_grep rc=$rc out=$(head -2 "$LOGDIR/selftest-m4help-grep.log" | sanitize)"

    /usr/bin/m4 --help >"$LOGDIR/selftest-m4help.out" 2>&1 </dev/null
    rc=$?
    emit "selftest cmd=m4help_file rc=$rc bytes=$(wc -c <"$LOGDIR/selftest-m4help.out" 2>/dev/null) out=$(head -1 "$LOGDIR/selftest-m4help.out" | sanitize)"
    grep reload-state "$LOGDIR/selftest-m4help.out" >"$LOGDIR/selftest-m4help-file-grep.log" 2>&1
    rc=$?
    emit "selftest cmd=m4help_to_file_grep rc=$rc out=$(head -2 "$LOGDIR/selftest-m4help-file-grep.log" | sanitize)"

    /usr/bin/m4 --help 2>&1 | wc -l >"$LOGDIR/selftest-m4help-wc.log" 2>&1
    rc=$?
    emit "selftest cmd=m4help_pipe_wc rc=$rc out=$(head -1 "$LOGDIR/selftest-m4help-wc.log" | sanitize)"

    # dpkg-buildpackage runs debian/rules under fakeroot; its LD_PRELOAD is a
    # second variable in the same pipe question.
    fakeroot sh -c "printf 'pipe-under-fakeroot\n' | grep 'pipe-under-fakeroot'" \
        >"$LOGDIR/selftest-fakeroot-grep.log" 2>&1 </dev/null
    rc=$?
    emit "selftest cmd=fakeroot_grep_pipe rc=$rc out=$(head -2 "$LOGDIR/selftest-fakeroot-grep.log" | sanitize)"
    fakeroot m4 --version >"$LOGDIR/selftest-fakeroot-m4.log" 2>&1 </dev/null
    rc=$?
    emit "selftest cmd=fakeroot_m4 rc=$rc out=$(head -2 "$LOGDIR/selftest-fakeroot-m4.log" | sanitize)"

    # coreutils' debian/rules is the only one of the three that includes the
    # whole /usr/share/dpkg/default.mk (and sets DPKG_EXPORT_BUILDFLAGS), and
    # its `debian/rules clean` dies with SIGSEGV before printing anything.
    # Include each fragment alone to find which expansion crashes make.
    mk_probe() {
        label=$1
        fragment=$2
        pre=$3
        f="$LOGDIR/selftest-$label.mk"
        if [ -n "$pre" ]; then
            printf '%s\n' "$pre" >"$f"
        else
            : >"$f"
        fi
        printf 'include /usr/share/dpkg/%s\nall:\n\t@echo ok\n' "$fragment" >>"$f"
        # A runaway export expansion must not eat the gate's whole deadline.
        timeout 60 make -f "$f" all >"$LOGDIR/selftest-$label.log" 2>&1 </dev/null
        rc=$?
        emit "selftest cmd=$label rc=$rc out=$(head -2 "$LOGDIR/selftest-$label.log" | sanitize)"
    }
    mk_probe mk_arch architecture.mk ""
    mk_probe mk_arch_bash architecture.mk "SHELL = /bin/bash"
    # dpkg-buildpackage puts the DEB_* answers in the environment, so
    # architecture.mk's `?=` leaves simple values instead of lazy recursive
    # ones. Reproduce both halves of that condition.
    {
        printf 'include /usr/share/dpkg/architecture.mk\nall:\n\t@echo ok\n'
    } >"$LOGDIR/selftest-mk_arch_env.mk"
    ( eval "$(dpkg-architecture -s)" &&
        timeout 60 make -f "$LOGDIR/selftest-mk_arch_env.mk" all ) \
        >"$LOGDIR/selftest-mk_arch_env.log" 2>&1 </dev/null
    rc=$?
    emit "selftest cmd=mk_arch_env rc=$rc out=$(head -2 "$LOGDIR/selftest-mk_arch_env.log" | sanitize)"

    # buildflags.mk has no `?=` protection: DPKG_EXPORT_BUILDFLAGS exports 22
    # recursive variables whose values run dpkg-buildflags. coreutils sets it.
    {
        printf 'DPKG_EXPORT_BUILDFLAGS = 1\n'
        printf 'include /usr/share/dpkg/buildflags.mk\nall:\n\t@echo ok\n'
    } >"$LOGDIR/selftest-mk_buildflags_export.mk"
    timeout 60 make -f "$LOGDIR/selftest-mk_buildflags_export.mk" all \
        >"$LOGDIR/selftest-mk_buildflags_export.log" 2>&1 </dev/null
    rc=$?
    emit "selftest cmd=mk_buildflags_export rc=$rc out=$(head -2 "$LOGDIR/selftest-mk_buildflags_export.log" | sanitize)"
    mk_probe mk_buildflags buildflags.mk ""
    mk_probe mk_buildtools buildtools.mk ""
    mk_probe mk_buildapi buildapi.mk ""
    mk_probe mk_buildopts buildopts.mk ""
    mk_probe mk_pkginfo pkg-info.mk ""
    mk_probe mk_vendor vendor.mk ""
    mk_probe mk_default default.mk ""

    dpkg-buildflags --get CFLAGS >"$LOGDIR/selftest-buildflags.log" 2>&1 </dev/null
    rc=$?
    emit "selftest cmd=dpkg_buildflags rc=$rc out=$(head -2 "$LOGDIR/selftest-buildflags.log" | sanitize)"

    # architecture.mk is the fragment that kills make. Split the causes:
    # the Perl helper itself, make expanding exported recursive $(shell)
    # variables, and the fragment with the `export` keyword removed.
    for q in DEB_HOST_ARCH DEB_HOST_GNU_TYPE DEB_BUILD_ARCH_OS; do
        dpkg-architecture -q"$q" >"$LOGDIR/selftest-arch-$q.log" 2>&1 </dev/null
        rc=$?
        emit "selftest cmd=dpkg_arch_$q rc=$rc out=$(head -2 "$LOGDIR/selftest-arch-$q.log" | sanitize)"
    done

    {
        i=1
        while [ "$i" -le 36 ]; do
            printf 'export A%s ?= $(shell echo a%s)\n' "$i" "$i"
            i=$((i + 1))
        done
        printf 'all:\n\t@echo ok\n'
    } >"$LOGDIR/selftest-export-vars.mk"
    sed 's/^dpkg_architecture_setvar = export /dpkg_architecture_setvar = /' \
        /usr/share/dpkg/architecture.mk >"$LOGDIR/arch-noexport.mk"
    printf 'all:\n\t@echo ok\n' >>"$LOGDIR/arch-noexport.mk"
    timeout 60 make -f "$LOGDIR/arch-noexport.mk" all \
        >"$LOGDIR/selftest-arch-noexport.log" 2>&1 </dev/null
    rc=$?
    emit "selftest cmd=mk_arch_noexport rc=$rc out=$(head -2 "$LOGDIR/selftest-arch-noexport.log" | sanitize)"

    # Last: this construct is pathological and may loop until the timeout.
    timeout 30 make -f "$LOGDIR/selftest-export-vars.mk" all \
        >"$LOGDIR/selftest-export-vars.log" 2>&1 </dev/null
    rc=$?
    emit "selftest cmd=mk_export_vars rc=$rc out=$(head -2 "$LOGDIR/selftest-export-vars.log" | sanitize)"

    # Same checks after the two shims, to prove they address the cause.
    install_shims
    rc=$?
    emit "selftest cmd=install_shims rc=$rc"

    autom4te --version >"$LOGDIR/selftest-autom4te-shim.log" 2>&1 </dev/null
    rc=$?
    emit "selftest cmd=autom4te_shim rc=$rc out=$(head -2 "$LOGDIR/selftest-autom4te-shim.log" | sanitize)"

    fakeroot sh -c "printf 'pipe-under-fakeroot\n' | grep 'pipe-under-fakeroot'" \
        >"$LOGDIR/selftest-fakeroot-shim.log" 2>&1 </dev/null
    rc=$?
    emit "selftest cmd=fakeroot_shim rc=$rc out=$(head -2 "$LOGDIR/selftest-fakeroot-shim.log" | sanitize)"

    # Why fakeroot-tcp is not a fallback: listen() on its sockets.
    python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(1); print("inet-listen-ok")' \
        >"$LOGDIR/selftest-inet-listen.log" 2>&1 </dev/null
    rc=$?
    emit "selftest cmd=inet_listen rc=$rc out=$(head -2 "$LOGDIR/selftest-inet-listen.log" | sanitize)"
    python3 -c 'import os, socket; p="/var/tmp/dpkg-shims/t.sock"; \
os.path.exists(p) and os.unlink(p); s=socket.socket(socket.AF_UNIX); s.bind(p); s.listen(1); print("unix-listen-ok")' \
        >"$LOGDIR/selftest-unix-listen.log" 2>&1 </dev/null
    rc=$?
    emit "selftest cmd=unix_listen rc=$rc out=$(head -2 "$LOGDIR/selftest-unix-listen.log" | sanitize)"
    dpkg-buildpackage --help >"$LOGDIR/selftest-dpkgbp-help.log" 2>&1 </dev/null
    rc=$?
    emit "selftest cmd=dpkgbp_help rc=$rc rules_requires_root=$(grep -c 'rules-requires-root' "$LOGDIR/selftest-dpkgbp-help.log" 2>/dev/null)"

    printf 'DPKGB_DONE\n' >/dev/console
    exit 0
fi

install_shims

# ------------------------------------------------------------------ builds
index=0
total=0
ok=0
failed=0

for name in $PKGS; do
    index=$((index + 1))
    total=$((total + 1))
    pkg_start=$(now)
    pkgdir="$BASE/pkg$index"
    attempt=1
    result=fail
    err=""
    artifacts=""
    emit "pkg=$index start name=$name"

    while [ "$attempt" -le "$ATTEMPTS" ]; do
        rm -rf "$pkgdir"
        mkdir -p "$pkgdir"
        if ! cd "$pkgdir"; then
            emit "pkg=$index err=cd_failed_dir=$pkgdir"
            result=fail
            break
        fi

        free=$(free_kb "$BASE")
        emit "pkg=$index disk_free_kb=$free attempt=$attempt"
        if [ -n "$free" ] && [ "$free" -lt "$MIN_FREE_KB" ]; then
            emit "pkg=$index stage=builddeps rc=1 dur=0.00 attempt=$attempt"
            emit "pkg=$index err=disk_low_free_${free}kb"
            result=disk
            err="disk_low_free_${free}kb"
            break
        fi

        # -- stage: build-deps -----------------------------------------
        t0=$(now)
        timeout -k 10 "$(remaining)" env DEBIAN_FRONTEND=noninteractive \
            apt-get build-dep -y -o DPkg::Lock::Timeout=120 \
            -o Acquire::Retries=2 "$name" >"$LOGDIR/pkg$index-builddeps.log" 2>&1 </dev/null
        rc=$?
        t1=$(now)
        emit "pkg=$index stage=builddeps rc=$rc dur=$(elapsed "$t0" "$t1") attempt=$attempt"
        if [ "$rc" -ne 0 ]; then
            err=$(err_summary "$LOGDIR/pkg$index-builddeps.log" "$rc")
            emit "pkg=$index err=$err"
            emit "pkg=$index evidence=$(evidence "$LOGDIR/pkg$index-builddeps.log")"
            if [ "$rc" -eq 124 ]; then
                result=timeout
                break
            fi
            attempt=$((attempt + 1))
            continue
        fi
        # Keep the apt cache from competing with the build for the disk.
        apt-get clean

        # -- stage: source ---------------------------------------------
        t0=$(now)
        timeout -k 10 "$(remaining)" env DEBIAN_FRONTEND=noninteractive \
            apt-get source "$name" >"$LOGDIR/pkg$index-source.log" 2>&1 </dev/null
        rc=$?
        t1=$(now)
        emit "pkg=$index stage=source rc=$rc dur=$(elapsed "$t0" "$t1") attempt=$attempt"
        if [ "$rc" -ne 0 ]; then
            err=$(err_summary "$LOGDIR/pkg$index-source.log" "$rc")
            emit "pkg=$index err=$err"
            emit "pkg=$index evidence=$(evidence "$LOGDIR/pkg$index-source.log")"
            if [ "$rc" -eq 124 ]; then
                result=timeout
                break
            fi
            attempt=$((attempt + 1))
            continue
        fi

        # apt-get source names the directory after the *source* package, which
        # is not always the binary name (python3-minimal -> python3-defaults).
        srcdir=$(ls -1dt "$pkgdir"/*/ 2>/dev/null | head -1)
        srcdir=${srcdir%/}
        if [ -z "$srcdir" ] || [ ! -d "$srcdir/debian" ]; then
            err=source_tree_missing_after_rc0
            emit "pkg=$index err=$err"
            attempt=$((attempt + 1))
            continue
        fi

        # -- stage: build ----------------------------------------------
        cd "$srcdir" || {
            err=cd_source_tree_failed
            emit "pkg=$index err=$err"
            attempt=$((attempt + 1))
            continue
        }
        t0=$(now)
        # nocheck: the goal is the .deb, not the package's own test suite;
        # parallel=4 matches the gate's four vCPUs.
        # FORCE_UNSAFE_CONFIGURE: the gate builds as root. coreutils'
        # configure refuses to run as root while probing whether mknod can
        # create a fifo; the bypass is the package's own documented escape
        # hatch and does not change what is built.
        timeout -k 10 "$(remaining)" env DEBIAN_FRONTEND=noninteractive \
            DEB_BUILD_OPTIONS="nocheck parallel=4" \
            FORCE_UNSAFE_CONFIGURE=1 \
            dpkg-buildpackage -b -us -uc >"$LOGDIR/pkg$index-build.log" 2>&1 </dev/null
        rc=$?
        t1=$(now)
        emit "pkg=$index stage=build rc=$rc dur=$(elapsed "$t0" "$t1") attempt=$attempt"
        if [ "$rc" -ne 0 ]; then
            err=$(err_summary "$LOGDIR/pkg$index-build.log" "$rc")
            emit "pkg=$index err=$err"
            emit "pkg=$index evidence=$(evidence "$LOGDIR/pkg$index-build.log")"
            if [ "$rc" -eq 124 ]; then
                result=timeout
                break
            fi
            attempt=$((attempt + 1))
            continue
        fi

        # The .debs land next to, not inside, the source directory.
        for deb in "$pkgdir"/*.deb; do
            [ -f "$deb" ] || continue
            base=$(basename "$deb" | sanitize)
            cp -f "$deb" "$ARTDIR/$base" 2>/dev/null
            emit "pkg=$index artifact=$base"
            artifacts="$artifacts $base"
        done
        if [ -z "$artifacts" ]; then
            err=build_rc0_but_no_deb
            emit "pkg=$index err=$err"
            attempt=$((attempt + 1))
            continue
        fi
        result=ok
        break
    done

    cd / || true
    rm -rf "$pkgdir"
    apt-get clean
    if [ "$result" = "ok" ]; then
        ok=$((ok + 1))
    else
        failed=$((failed + 1))
    fi
    # The loop exits with attempt = ATTEMPTS + 1 after the last failure; the
    # console must report attempts actually made, not the terminating value.
    attempts_used=$attempt
    [ "$attempts_used" -gt "$ATTEMPTS" ] && attempts_used=$ATTEMPTS
    emit "pkg=$index done name=$name result=$result attempts=$attempts_used dur=$(elapsed "$pkg_start" "$(now)")"
done

emit "summary total=$total ok=$ok fail=$failed"
printf 'DPKGB_DONE\n' >/dev/console
