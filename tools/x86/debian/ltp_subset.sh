#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# L3 syscall-consistency probe (bounded LTP subset).
#
# Two phases, selected by LTP_PHASE:
#   check : feasibility only. Reports apt availability, GitHub reachability and
#           the guest toolchain as `LTPCHK ` lines, then `LTP_DONE phase=check`.
#   run   : downloads the LTP release, builds only the syscall directories
#           below (no full `make`), runs 40 selected cases and reports one
#           `LTP case=...` line each plus `LTP summary` and
#           `LTP_DONE phase=run`.
#
# Route A (apt package) is not possible on Debian trixie (no `ltp` package),
# so this script implements route B (build in the guest) and records the
# evidence that made that choice.

PHASE=${LTP_PHASE:-check}
VER=${LTP_VERSION:-20260930}
URL=${LTP_URL:-https://github.com/linux-test-project/ltp/releases/download/$VER/ltp-full-$VER.tar.bz2}
BUILD=${LTP_BUILD:-/tmp/ltp-build}
SYSROOT=testcases/kernel/syscalls

# The bounded subset: <= 40 cases, all basic syscalls. Order is the run order.
CASES="
getpid/getpid01
close/close01 close/close02
fstat/fstat02 fstat/fstat03
read/read01 read/read02 read/read03 read/read04
write/write01 write/write02 write/write03 write/write04 write/write05 write/write06
pipe/pipe01 pipe/pipe02 pipe/pipe03 pipe/pipe04 pipe/pipe05 pipe/pipe06 pipe/pipe07 pipe/pipe08 pipe/pipe09
open/open01 open/open02 open/open03 open/open04 open/open06 open/open07 open/open08 open/open09
mmap/mmap01 mmap/mmap02 mmap/mmap03 mmap/mmap04 mmap/mmap05 mmap/mmap06 mmap/mmap08 mmap/mmap09
"

sanitize() {
    tr '\n\r\t' '   ' | tr -c 'A-Za-z0-9._=+:/@,-' '_' | cut -c1-160
}

chk() { printf 'LTPCHK %s\n' "$*" >/dev/console; }
run() { printf 'LTP %s\n' "$*" >/dev/console; }

if [ "$PHASE" = "check" ]; then
    chk "phase=check"
    for pkg in ltp ltp-full; do
        policy=$(apt-cache policy "$pkg" 2>/dev/null | head -6 | tr '\n' '|' | sanitize)
        [ -n "$policy" ] || policy=absent
        chk "apt_$pkg=$policy"
    done
    if apt-cache show ltp 2>/dev/null | grep -qx 'Package: ltp'; then
        chk "apt_available=yes"
    else
        chk "apt_available=no"
    fi
    code=$(timeout 30 curl -sSIL -o /dev/null -w '%{http_code}' "$URL" 2>/dev/null)
    chk "github_http=${code:-fail} url=$URL"
    size=$(timeout 30 curl -sSIL "$URL" 2>/dev/null |
        awk 'tolower($1) == "content-length:" { gsub(/\r/, "", $2); v = $2 }
             END { print v }')
    chk "github_size=${size:-unknown}"
    chk "gcc=$(gcc --version 2>/dev/null | head -1 | sanitize)"
    chk "make=$(make --version 2>/dev/null | head -1 | sanitize)"
    chk "autoconf=$(autoconf --version 2>/dev/null | head -1 | sanitize)"
    chk "curl=$(curl --version 2>/dev/null | head -1 | sanitize)"
    chk "tar=$(tar --version 2>/dev/null | head -1 | sanitize)"
    chk "bzip2=$(command -v bzip2 2>/dev/null || echo no)"
    chk "python3=$(python3 --version 2>&1 | head -1 | sanitize)"
    chk "disk_free_kb=$(df -k /tmp 2>/dev/null | awk 'NR==2 { print $4 }')"
    chk "ltp_installed=$(command -v ltp >/dev/null 2>&1 && echo yes || echo no)"
    printf 'LTP_DONE phase=check\n' >/dev/console
    exit 0
fi

# ---------------------------------------------------------------- run phase
setup_fail() {
    run "setup step=$1 rc=$2 log=$(tail -3 "/tmp/ltp-$1.log" 2>/dev/null | sanitize)"
    printf 'LTP_DONE phase=run status=setup-failed\n' >/dev/console
    exit 0
}

start_all=$(cut -d' ' -f1 /proc/uptime)
mkdir -p "$BUILD" || setup_fail mkdir $?
cd "$BUILD" || setup_fail cd $?

# The release is .tar.bz2; install bzip2 if the image lacks it, and fall back
# to Python's tarfile module if apt is unavailable.
if ! command -v bzip2 >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive timeout 180 apt-get install -y \
        --no-install-recommends -o DPkg::Lock::Timeout=120 bzip2 \
        >/tmp/ltp-apt-bzip2.log 2>&1 </dev/null
    run "setup step=apt_bzip2 rc=$?"
fi

TARBALL="ltp-full-$VER.tar.bz2"
if [ ! -f "$TARBALL" ]; then
    curl -L --retry 2 --max-time 300 -o "$TARBALL" "$URL" >/tmp/ltp-download.log 2>&1
    rc=$?
    run "setup step=download rc=$rc size=$(stat -c %s "$TARBALL" 2>/dev/null || echo 0)"
    [ "$rc" -eq 0 ] || { printf 'LTP_DONE phase=run status=setup-failed\n' >/dev/console; exit 0; }
fi

if [ ! -d "ltp-full-$VER" ]; then
    if command -v bzip2 >/dev/null 2>&1; then
        tar xjf "$TARBALL" >/tmp/ltp-extract.log 2>&1
    else
        python3 -m tarfile -e "$TARBALL" . >/tmp/ltp-extract.log 2>&1
    fi
    rc=$?
    run "setup step=extract rc=$rc"
    [ "$rc" -eq 0 ] || { printf 'LTP_DONE phase=run status=setup-failed\n' >/dev/console; exit 0; }
fi
cd "ltp-full-$VER" || setup_fail cd-tree $?

# configure hard-requires pkg-config (the base image omits it).
if ! command -v pkg-config >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive timeout 180 apt-get install -y \
        --no-install-recommends -o DPkg::Lock::Timeout=120 pkg-config \
        >/tmp/ltp-apt-pkgconfig.log 2>&1 </dev/null
    run "setup step=apt_pkgconfig rc=$?"
fi

if [ ! -f include/config.h ]; then
    ./configure --prefix="$BUILD/install" --without-numa --without-tirpc \
        --without-modules >/tmp/ltp-configure.log 2>&1
    rc=$?
    run "setup step=configure rc=$rc"
    [ "$rc" -eq 0 ] || setup_fail configure "$rc"
fi

make -C include -j4 >/tmp/ltp-build-include.log 2>&1
run "build dir=include rc=$?"
make -C lib -j4 >/tmp/ltp-build-lib.log 2>&1
run "build dir=lib rc=$?"
make -C testcases/lib -j4 >/tmp/ltp-build-tclib.log 2>&1
run "build dir=testcases/lib rc=$?"

for d in getpid close fstat read write pipe open mmap; do
    make -C "$SYSROOT/$d" -j4 >"/tmp/ltp-build-$d.log" 2>&1
    run "build dir=$d rc=$?"
done

RUNDIR="$BUILD/run"
mkdir -p "$RUNDIR" "$BUILD/tmp"
cd "$RUNDIR" || setup_fail rundir $?
export LTPROOT="$BUILD/ltp-full-$VER"
export TMPDIR="$BUILD/tmp"
export PATH="$LTPROOT/testcases/bin:$PATH"

total=0 pass=0 fail=0 skip=0
for pair in $CASES; do
    now=$(cut -d' ' -f1 /proc/uptime)
    elapsed=$(awk -v a="$start_all" -v b="$now" 'BEGIN { printf "%d", b - a }')
    if [ "$elapsed" -ge 1400 ]; then
        run "stopped reason=time_limit elapsed=${elapsed}s"
        break
    fi
    d=${pair%%/*}
    c=${pair##*/}
    total=$((total + 1))
    bin="$LTPROOT/$SYSROOT/$d/$c"
    if [ ! -x "$bin" ]; then
        skip=$((skip + 1))
        run "case=$c suite=$d rc=127 status=skip dur=0.00 reason=binary_missing"
        continue
    fi
    t0=$(cut -d' ' -f1 /proc/uptime)
    timeout 120 "$bin" >"/tmp/ltp-run-$c.log" 2>&1
    rc=$?
    t1=$(cut -d' ' -f1 /proc/uptime)
    dur=$(awk -v a="$t0" -v b="$t1" 'BEGIN { printf "%.2f", b - a }')
    if [ "$rc" -eq 0 ]; then
        status=pass; pass=$((pass + 1)); reason=""
    elif [ "$rc" -eq 32 ]; then
        status=skip; skip=$((skip + 1))
        reason=$(grep -m1 'TCONF' "/tmp/ltp-run-$c.log" 2>/dev/null | sanitize)
        [ -n "$reason" ] || reason=tconf
    else
        status=fail; fail=$((fail + 1))
        reason=$(grep -E 'TFAIL|TBROK|TWARN' "/tmp/ltp-run-$c.log" 2>/dev/null |
            head -2 | sanitize)
        [ -n "$reason" ] || reason=$(tail -2 "/tmp/ltp-run-$c.log" 2>/dev/null | sanitize)
        [ -n "$reason" ] || reason="rc_${rc}"
    fi
    run "case=$c suite=$d rc=$rc status=$status dur=$dur reason=$reason"
done

run "summary total=$total pass=$pass fail=$fail skip=$skip"

# Minimal diagnostics for the failure classes the subset exposed. Kept in a
# separate process so earlier tests cannot affect the fd table or credentials.
DIAG_C=/tmp/ltp-diag.c
cat >"$DIAG_C" <<'EOF'
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <sys/mman.h>
#include <sys/types.h>
#include <unistd.h>

int main(void)
{
    int fd;

    fd = open("/tmp/diag-noatime", O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd >= 0)
        close(fd);
    if (seteuid(65534) == 0) {
        errno = 0;
        fd = open("/tmp/diag-noatime", O_RDONLY | O_NOATIME);
        printf("noatime rc=%d errno=%d\n", fd >= 0 ? 0 : -1, errno);
        if (fd >= 0)
            close(fd);
        seteuid(0);
    } else {
        printf("noatime seteuid_errno=%d\n", errno);
    }

    fd = open("/tmp/diag-mmap", O_RDWR | O_CREAT | O_TRUNC, 0600);
    if (fd >= 0) {
        int saved = fd;
        ftruncate(fd, 4096);
        close(fd);
        errno = 0;
        void *p = mmap(NULL, 4096, PROT_WRITE, MAP_SHARED, saved, 0);
        printf("mmap_closed result=%s errno=%d\n",
               p == MAP_FAILED ? "fail" : "ok", errno);
        if (p != MAP_FAILED)
            munmap(p, 4096);
    }

    void *p = mmap(NULL, 4096, PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (p == MAP_FAILED) {
        printf("maps_perm map_failed errno=%d\n", errno);
    } else {
        FILE *f = fopen("/proc/self/maps", "r");
        char line[512];
        while (f && fgets(line, sizeof(line), f)) {
            unsigned long a, b;
            char perms[8];
            if (sscanf(line, "%lx-%lx %7s", &a, &b, perms) == 3 &&
                a <= (unsigned long)p && (unsigned long)p < b) {
                printf("maps_perm=%s\n", perms);
                break;
            }
        }
        if (f)
            fclose(f);
        munmap(p, 4096);
    }

    /* Last: if a fd limit is enforced, this exhausts the table. */
    long n = 0;
    while (n < 20000) {
        fd = open("/dev/null", O_RDONLY);
        if (fd < 0) {
            printf("fdlimit opened=%ld errno=%d\n", n, errno);
            break;
        }
        n++;
    }
    if (n >= 20000)
        printf("fdlimit opened=20000 errno=0\n");
    return 0;
}
EOF
gcc -O2 -o /tmp/ltp-diag "$DIAG_C" >/tmp/ltp-diag-build.log 2>&1
run "diag build rc=$?"
if [ -x /tmp/ltp-diag ]; then
    /tmp/ltp-diag 2>&1 | while read -r dline; do
        run "diag $dline"
    done
fi

printf 'LTP_DONE phase=run\n' >/dev/console
