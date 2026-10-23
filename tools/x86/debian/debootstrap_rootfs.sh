#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# L5 debootstrap probe: build a Debian root filesystem from scratch inside the
# Asterinas guest, then run debootstrap's second stage through chroot.
#
# Three stages, each reported with rc, duration and the free space of the
# filesystem holding /target:
#
#   install : apt-get install -y debootstrap (skipped when already present)
#   foreign : debootstrap --foreign --variant=minbase trixie /target MIRROR
#   second  : chroot /target /debootstrap/debootstrap --second-stage
#
# Console protocol (one `DBST ` line per event):
#
#   DBST env <key>=<value>
#   DBST stage=<install|foreign|second> rc=<n> dur=<s>
#   DBST disk_free_kb=<n> stage=<name> phase=<before|after|after_cleanup>
#   DBST err=<one-line summary>
#   DBST verify debian_version=<v> sh=<path> ok=<0|1>
#   DBST target_kb=<n>
#   DBST_DONE
#
# The image has a single ~4 GiB ext4 root and only ~2 GiB free, so the target
# lives on that same filesystem (/target). debootstrap caches downloads inside
# /target/var/cache/apt/archives, and the caches are removed at the end.

TARGET=${DBST_TARGET:-/target}
SUITE=${DBST_SUITE:-trixie}
MIRROR=${DBST_MIRROR:-http://mirrors.tuna.tsinghua.edu.cn/debian/}
VARIANT=${DBST_VARIANT:-minbase}
MIN_FREE_KB=${DBST_MIN_FREE_KB:-307200}
INSTALL_TIMEOUT=${DBST_INSTALL_TIMEOUT:-600}
FOREIGN_TIMEOUT=${DBST_FOREIGN_TIMEOUT:-1800}
SECOND_TIMEOUT=${DBST_SECOND_TIMEOUT:-1800}
LOGDIR=${DBST_LOGDIR:-/var/tmp/dbst}

while [ $# -gt 0 ]; do
    case "$1" in
        --target) TARGET="$2"; shift 2 ;;
        *) echo "debootstrap-rootfs: unknown argument: $1" >&2; exit 2 ;;
    esac
done

# The probe wipes the target before the foreign stage. Never accept a relative
# path or "/" here.
case "$TARGET" in
    /?*) ;;
    *) echo "debootstrap-rootfs: target must be an absolute non-root path" >&2; exit 2 ;;
esac

emit() { printf 'DBST %s\n' "$*" >/dev/console; }
sanitize() {
    tr '\n\r\t' '   ' | sed 's/[[:space:]]*$//' |
        tr -c 'A-Za-z0-9._=+:/@,-' '_' | cut -c1-200
}
now() { cut -d' ' -f1 /proc/uptime; }
elapsed() { awk -v a="$1" -v b="$2" 'BEGIN { printf "%.2f", b - a }'; }
# Free space of the filesystem holding the target (or "/" before it exists),
# so a tmpfs target reports tmpfs rather than the root ext2.
free_kb() {
    dir=${1:-/}
    [ -d "$dir" ] || dir=/
    df -k "$dir" 2>/dev/null | awk 'NR==2 { print $4 }'
}

# debootstrap and chroot live in /usr/sbin, which is not always on the PATH
# inherited by `sudo sh -c`. Resolve explicitly.
resolve_cmd() {
    if command -v "$1" >/dev/null 2>&1; then
        command -v "$1"
        return 0
    fi
    for dir in /usr/sbin /sbin /usr/bin /bin; do
        if [ -x "$dir/$1" ]; then
            printf '%s' "$dir/$1"
            return 0
        fi
    done
    return 1
}

disk_report() {
    emit "disk_free_kb=$(free_kb "$TARGET") stage=$1 phase=$2"
}

err_summary() {
    log=$1
    rc=$2
    msg=$(grep -a -E '^(E:|W: |error:|fatal:|.*: error:|.*: fatal error:|.*No space left|.*Function not implemented|.*Could not resolve|.*Failed to fetch)' "$log" 2>/dev/null |
        tail -2 | sanitize)
    [ -n "$msg" ] || msg=$(grep -a -i -E 'error|failed|not found|cannot|unable' "$log" 2>/dev/null |
        tail -2 | sanitize)
    [ -n "$msg" ] || msg=$(tail -2 "$log" 2>/dev/null | sanitize)
    [ -n "$msg" ] || msg="rc_${rc}_no_log"
    printf '%s' "$msg"
}

mkdir -p "$LOGDIR" 2>/dev/null
emit "env arch=$(uname -m | sanitize) kernel=$(uname -r | sanitize)"
emit "env debian=$(cat /etc/debian_version 2>/dev/null | sanitize)"
emit "env suite=$SUITE variant=$VARIANT target=$TARGET mirror=$MIRROR"
emit "env root_free_kb=$(free_kb /) min_free_kb=$MIN_FREE_KB"
disk_report setup before

stop=0
install_rc=""
foreign_rc=""
second_rc=""

# ------------------------------------------------------------- stage: install
# debootstrap downloads http(s) mirrors with wget, not curl; the base image
# ships curl only. Both tools are resolved explicitly afterwards.
need_install=0
resolve_cmd debootstrap >/dev/null 2>&1 || need_install=1
resolve_cmd wget >/dev/null 2>&1 || need_install=1
if [ "$need_install" -eq 0 ]; then
    install_rc=0
    install_dur=0.00
    emit "stage=install rc=0 dur=0.00 preinstalled=yes"
else
    disk_report install before
    t0=$(now)
    timeout -k 10 "$INSTALL_TIMEOUT" env DEBIAN_FRONTEND=noninteractive \
        apt-get install -y --no-install-recommends \
        -o DPkg::Lock::Timeout=120 -o Acquire::Retries=2 debootstrap wget \
        >"$LOGDIR/install.log" 2>&1 </dev/null
    install_rc=$?
    install_dur=$(elapsed "$t0" "$(now)")
    emit "stage=install rc=$install_rc dur=$install_dur preinstalled=no"
    disk_report install after
    if [ "$install_rc" -ne 0 ]; then
        emit "err=$(err_summary "$LOGDIR/install.log" "$install_rc")"
        stop=1
    fi
fi
if [ "$stop" -eq 0 ]; then
    DEBOOTSTRAP_BIN=$(resolve_cmd debootstrap)
    CHROOT_BIN=$(resolve_cmd chroot)
    if [ -z "$DEBOOTSTRAP_BIN" ] || [ -z "$CHROOT_BIN" ]; then
        emit "err=missing_tool debootstrap=${DEBOOTSTRAP_BIN:-none} chroot=${CHROOT_BIN:-none}"
        stop=1
    else
        emit "env debootstrap=$DEBOOTSTRAP_BIN version=$("$DEBOOTSTRAP_BIN" --version 2>&1 | head -1 | sanitize)"
        emit "env chroot=$CHROOT_BIN wget=$(resolve_cmd wget)"
    fi
fi

free=$(free_kb "$TARGET")
if [ "$stop" -eq 0 ] && [ -n "$free" ] && [ "$free" -lt "$MIN_FREE_KB" ]; then
    emit "err=disk_low_free_${free}kb"
    stop=1
fi

# ------------------------------------------------------------- stage: foreign
if [ "$stop" -eq 0 ]; then
    rm -rf "$TARGET"
    mkdir -p "$TARGET"
    emit "env target_mount=$(df -P "$TARGET" 2>/dev/null | awk 'NR==2 { print $6 }') target_fs=$(df -PT "$TARGET" 2>/dev/null | awk 'NR==2 { print $2 }')"
    disk_report foreign before
    t0=$(now)
    timeout -k 10 "$FOREIGN_TIMEOUT" env DEBIAN_FRONTEND=noninteractive \
        "$DEBOOTSTRAP_BIN" --foreign --variant="$VARIANT" "$SUITE" "$TARGET" "$MIRROR" \
        >"$LOGDIR/foreign.log" 2>&1 </dev/null
    foreign_rc=$?
    foreign_dur=$(elapsed "$t0" "$(now)")
    emit "stage=foreign rc=$foreign_rc dur=$foreign_dur"
    disk_report foreign after
    if [ "$foreign_rc" -ne 0 ]; then
        emit "err=$(err_summary "$LOGDIR/foreign.log" "$foreign_rc")"
        stop=1
    fi
fi

free=$(free_kb "$TARGET")
if [ "$stop" -eq 0 ] && [ -n "$free" ] && [ "$free" -lt "$MIN_FREE_KB" ]; then
    emit "err=disk_low_free_${free}kb"
    stop=1
fi

# ------------------------------------------------------- chroot capability
# Independent of debootstrap: execute the new rootfs's own /bin/sh through
# chroot(2). A working shell proves the syscall and the extracted runtime.
if [ "$stop" -eq 0 ]; then
    t0=$(now)
    timeout 60 "$CHROOT_BIN" "$TARGET" /bin/sh -c 'echo DBST_CHROOT_OK' \
        >"$LOGDIR/chroot.log" 2>&1 </dev/null
    chroot_rc=$?
    emit "chroot_probe rc=$chroot_rc dur=$(elapsed "$t0" "$(now)") out=$(head -2 "$LOGDIR/chroot.log" | sanitize)"
    if [ "$chroot_rc" -ne 0 ]; then
        emit "err=chroot_probe_$(head -1 "$LOGDIR/chroot.log" | sanitize)"
    fi
fi

# ------------------------------------------------------- mount/unshare state
# debootstrap's second stage mounts /proc when it can; record whether this
# kernel supports the operations it relies on.
if [ "$stop" -eq 0 ]; then
    mkdir -p "$TARGET/proc"
    timeout 30 mount -t proc proc "$TARGET/proc" \
        >"$LOGDIR/mount.log" 2>&1 </dev/null
    mount_rc=$?
    emit "mount_probe rc=$mount_rc out=$(head -2 "$LOGDIR/mount.log" | sanitize)"
    if [ "$mount_rc" -eq 0 ]; then
        timeout 30 umount "$TARGET/proc" >"$LOGDIR/umount.log" 2>&1 </dev/null
        emit "umount_probe rc=$? out=$(head -2 "$LOGDIR/umount.log" | sanitize)"
    fi
    timeout 30 unshare -m /bin/true >"$LOGDIR/unshare.log" 2>&1 </dev/null
    emit "unshare_probe rc=$? out=$(head -2 "$LOGDIR/unshare.log" | sanitize)"
fi

# -------------------------------------------------------------- stage: second
# The core of this round: can dpkg configure the new root from inside it?
# TMPDIR stays on /target so no second partition is used.
if [ "$stop" -eq 0 ]; then
    disk_report second before
    t0=$(now)
    timeout -k 10 "$SECOND_TIMEOUT" env TMPDIR="$TARGET/tmp" \
        "$CHROOT_BIN" "$TARGET" /debootstrap/debootstrap --second-stage \
        >"$LOGDIR/second.log" 2>&1 </dev/null
    second_rc=$?
    second_dur=$(elapsed "$t0" "$(now)")
    emit "stage=second rc=$second_rc dur=$second_dur"
    disk_report second after
    if [ "$second_rc" -ne 0 ]; then
        emit "err=$(err_summary "$LOGDIR/second.log" "$second_rc")"
        # The target tree is on ext2 and may not survive power-off, so put the
        # cause on the wire before the guest goes away.
        tail -6 "$LOGDIR/second.log" 2>/dev/null | while read -r line; do
            emit "log second=$(printf '%s' "$line" | sanitize)"
        done
        tail -6 "$TARGET/debootstrap/debootstrap.log" 2>/dev/null | while read -r line; do
            emit "log target=$(printf '%s' "$line" | sanitize)"
        done
    fi
fi

# ------------------------------------------------------------------ verify
if [ "$second_rc" = 0 ]; then
    dv=$(cat "$TARGET/etc/debian_version" 2>/dev/null | sanitize)
    shpath=$(ls -l "$TARGET/bin/sh" 2>/dev/null | sanitize)
    ok=0
    [ -n "$dv" ] && [ -e "$TARGET/bin/sh" ] && ok=1
    emit "verify debian_version=$dv sh=$shpath ok=$ok"
fi
emit "target_kb=$(du -sk "$TARGET" 2>/dev/null | cut -f1)"

# ---------------------------------------------------------------- cleanup
# Keep the rootfs, drop the download cache and package lists (the task's disk
# budget is ~2 GiB).
rm -rf "$TARGET"/var/cache/apt/archives/*.deb 2>/dev/null
rm -rf "$TARGET"/var/lib/apt/lists/* 2>/dev/null
rm -rf "$TARGET"/var/cache/apt/*.bin 2>/dev/null
disk_report final after_cleanup
emit "target_kb_after_cleanup=$(du -sk "$TARGET" 2>/dev/null | cut -f1)"

emit "summary install_rc=${install_rc:-skip} foreign_rc=${foreign_rc:-skip} second_rc=${second_rc:-skip}"
printf 'DBST_DONE\n' >/dev/console
