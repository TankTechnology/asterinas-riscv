#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# SYSTEMD-BOOT-M1: assemble an initramfs that boots systemd (v257.5, riscv64
# glibc) as PID 1 on Asterinas RISC-V.
#
# The systemd binaries were cross-compiled in the sibling tree
# /home/arch-anjie/Program/asterinas-riscv (see that tree's
# tools/riscv/systemd/SYSTEMD-M2-report.md — 878/878 targets). The output is
# dynamically-linked PIE, so this script also copies the glibc runtime
# (ld-linux + libc.so.6 + libm/libdl/libpthread/librt/libgcc_s) from the proven
# xorg-rootfs dynamic-glibc image in that same tree. systemd's internal shared
# libs (libsystemd-core-257.so / libsystemd-shared-257.so) are copied from its
# build tree.
#
# The rootfs carries a static /init launcher that exec()s systemd, plus a
# minimal unit set (default.target -> basic.target -> sysinit.target) and a
# busybox helper shell for the emergency path.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="${REPO_ROOT}/target/nixos/systemd"
ROOTFS="${BUILD_ROOT}/rootfs"
OUTPUT="${1:-${BUILD_ROOT}/systemd-initramfs.cpio.gz}"

# The sibling tree that cross-compiled systemd and owns the proven glibc runtime.
B_TREE="${ASTERINAS_RISCV_B_TREE:-/home/arch-anjie/Program/asterinas-riscv}"
SD_BUILD="${B_TREE}/target/riscv-cross/src/systemd-257.5/build-riscv"
GLIBC_LIB="${B_TREE}/target/xorg-rootfs/lib"

CC="${RISC_V_CC:-riscv64-linux-gnu-gcc}"
BUSYBOX="${REPO_ROOT}/target/nixos/busybox"

[[ -d "${SD_BUILD}" ]] || { echo "missing systemd build: ${SD_BUILD}" >&2; exit 2; }
[[ -d "${GLIBC_LIB}" ]] || { echo "missing glibc runtime: ${GLIBC_LIB}" >&2; exit 2; }

echo "=== assembling systemd rootfs ==="
rm -rf "${ROOTFS}"
mkdir -p \
    "${ROOTFS}/lib" \
    "${ROOTFS}/usr/lib/systemd" \
    "${ROOTFS}/usr/bin" \
    "${ROOTFS}/bin" \
    "${ROOTFS}/etc/systemd/system" \
    "${ROOTFS}/dev" "${ROOTFS}/proc" "${ROOTFS}/sys" "${ROOTFS}/tmp" \
    "${ROOTFS}/run" "${ROOTFS}/var/log" "${ROOTFS}/var/tmp" \
    "${ROOTFS}/root" "${ROOTFS}/home" "${ROOTFS}/mnt" "${ROOTFS}/srv" \
    "${ROOTFS}/sys/fs/cgroup"

# 1. Static /init launcher (becomes PID 1, exec()s systemd).
"${CC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${ROOTFS}/init" "${SRC_DIR}/init.c"

# 2. glibc dynamic runtime (the exact closure the xorg-rootfs image proved
#    works with this kernel's ELF loader: glibc 2.41, riscv64-linux-gnu 15.1).
for lib in ld-linux-riscv64-lp64d.so.1 libc.so.6 libm.so.6 \
           libdl.so.2 librt.so.1 libpthread.so.0 libgcc_s.so.1; do
    cp "${GLIBC_LIB}/${lib}" "${ROOTFS}/lib/"
done

# 3. systemd pid1 + every helper binary it was cross-built with (69 ELF
#    executables). Because this build was never `meson install`ed, the binary
#    paths are baked into config.h as the *host* prefix
#    `/home/arch-anjie/Program/asterinas-riscv/target/riscv-cross/usr/...`
#    (see SYSTEMD_EXECUTOR_BINARY_PATH etc.). We bridge that with a single
#    symlink so every baked path resolves to the guest's canonical /usr, then
#    place the binaries under /usr/lib/systemd (libexec) and /usr/bin.
mkdir -p "${ROOTFS}/home/arch-anjie/Program/asterinas-riscv/target/riscv-cross"
ln -sfn /usr "${ROOTFS}/home/arch-anjie/Program/asterinas-riscv/target/riscv-cross/usr"

for f in "${SD_BUILD}"/*; do
    [ -f "$f" ] || continue
    [ -x "$f" ] || continue
    file "$f" | grep -q ELF || continue
    b="$(basename "$f")"
    cp "$f" "${ROOTFS}/usr/lib/systemd/$b"
    ln -sf "../lib/systemd/$b" "${ROOTFS}/usr/bin/$b"
done

# 4. systemd's internal shared libraries. They carry an rpath of
#    $ORIGIN/src/{core,shared} (a meson placeholder that is never rewritten
#    because this build was never installed), so place them in /lib — the
#    loader's default search path — which is consulted after RUNPATH misses.
cp "${SD_BUILD}/src/core/libsystemd-core-257.so"   "${ROOTFS}/lib/"
cp "${SD_BUILD}/src/shared/libsystemd-shared-257.so" "${ROOTFS}/lib/"

# 5. busybox helper (static) as /bin/sh + a handful of applet symlinks so the
#    emergency shell and any ExecStart=-/bin/sh unit can actually run commands.
if [[ -f "${BUSYBOX}" ]]; then
    cp "${BUSYBOX}" "${ROOTFS}/bin/busybox"
    ln -sf busybox "${ROOTFS}/bin/sh"
    for applet in ls cat echo mount umount mkdir rm ln mknod ps mountpoint \
                  head tail grep find test true false sleep kill sync df free \
                  getty login logger hostname; do
        ln -sf busybox "${ROOTFS}/bin/${applet}"
    done
else
    echo "WARNING: ${BUSYBOX} not found; no helper shell" >&2
fi

# 6. Identity + release files.
cat > "${ROOTFS}/etc/os-release" <<'EOF'
NAME="Asterinas"
ID=asterinas
PRETTY_NAME="Asterinas RISC-V (systemd bootstrap)"
ANSI_COLOR="0;32"
HOME_URL="https://github.com/asterinas/asterinas"
EOF
printf 'a1b2c3d4e5f60718293a4b5c6d7e8f90\n' > "${ROOTFS}/etc/machine-id"
printf 'asterinas-riscv\n' > "${ROOTFS}/etc/hostname"
cat > "${ROOTFS}/etc/passwd" <<'EOF'
root::0:0:root:/root:/bin/sh
nobody:x:65534:65534:nobody:/:/sbin/nologin
EOF
cat > "${ROOTFS}/etc/group" <<'EOF'
root:x:0:
nobody:x:65534:
tty:x:5:
EOF
cat > "${ROOTFS}/etc/hosts" <<'EOF'
127.0.0.1 localhost localhost.localdomain
::1       localhost localhost.localdomain
EOF
# Login shell profile: distinctive prompt so the smoke driver can reliably
# detect the interactive root shell, plus a sane PATH for systemctl/journalctl.
cat > "${ROOTFS}/etc/profile" <<'EOF'
export PATH=/usr/bin:/usr/lib/systemd:/bin:/sbin
export PS1='root@asterinas:~# '
echo "___LOGIN_SHELL_READY___"
EOF

# 7. Minimal unit set: default.target -> basic.target -> sysinit.target.
#    This is enough for systemd to print its banner, mount the pseudo-filesystems,
#    and report "Reached target Basic System".
cat > "${ROOTFS}/etc/systemd/system/basic.target" <<'EOF'
[Unit]
Description=Basic System
Requires=sysinit.target
After=sysinit.target
EOF
cat > "${ROOTFS}/etc/systemd/system/sysinit.target" <<'EOF'
[Unit]
Description=System Initialization
Requires=local-fs.target swap.target
Conflicts=emergency.service emergency.target
EOF
cat > "${ROOTFS}/etc/systemd/system/local-fs.target" <<'EOF'
[Unit]
Description=Local File Systems
EOF
cat > "${ROOTFS}/etc/systemd/system/swap.target" <<'EOF'
[Unit]
Description=Swap
EOF
ln -sf basic.target "${ROOTFS}/etc/systemd/system/default.target"

# Emergency shell (the "also a milestone" fallback): drop to busybox sh.
cat > "${ROOTFS}/etc/systemd/system/emergency.target" <<'EOF'
[Unit]
Description=Emergency Mode
Requires=emergency.service
After=emergency.service
AllowIsolate=yes
EOF
cat > "${ROOTFS}/etc/systemd/system/emergency.service" <<'EOF'
[Unit]
Description=Emergency Shell
DefaultDependencies=no

[Service]
ExecStart=-/bin/sh
StandardInput=tty
StandardOutput=tty
StandardError=tty
EOF

# 7b. M2 test programs: two lifecycle services (Type=simple / Type=forking) and
#     a socket-activation pair. Static, so they need no runtime beyond libc.
mkdir -p "${ROOTFS}/usr/bin"
for prog in simpletest forktest socktest sockclient; do
    "${CC}" -O2 -static -no-pie -fno-stack-protector \
        -o "${ROOTFS}/usr/bin/${prog}" "${SRC_DIR}/src/${prog}.c"
done

# 7c. M2 unit set (multi-user/getty + lifecycle + socket + journald). Copied from
#     the units/ dir so they're reviewable as files rather than heredocs.
cp "${SRC_DIR}/units/"*.target "${SRC_DIR}/units/"*.service \
   "${SRC_DIR}/units/"*.socket "${ROOTFS}/etc/systemd/system/" 2>/dev/null || true

# default.target -> multi-user.target -> basic.target (+ getty.target).
ln -sf multi-user.target "${ROOTFS}/etc/systemd/system/default.target"
mkdir -p "${ROOTFS}/etc/systemd/system/multi-user.target.wants"
ln -sf ../getty.target "${ROOTFS}/etc/systemd/system/multi-user.target.wants/getty.target"
mkdir -p "${ROOTFS}/etc/systemd/system/getty.target.wants"
ln -sf ../getty@.service "${ROOTFS}/etc/systemd/system/getty.target.wants/getty@ttyS0.service"

# 8. Pack as newc cpio (raw, no gzip — the kernel's zune-inflate decoder hangs
#    non-deterministically on >16 MB gzip inputs; see M3-report.md).
( cd "${ROOTFS}" && find . | cpio -o -H newc 2>/dev/null > "${OUTPUT}" )

echo "built ${OUTPUT}"
echo "  systemd:  $(file -b "${ROOTFS}/usr/lib/systemd/systemd" | cut -c1-70)"
echo "  ld-linux: $(file -b "${ROOTFS}/lib/ld-linux-riscv64-lp64d.so.1" | cut -c1-70)"
du -sh "${ROOTFS}"
