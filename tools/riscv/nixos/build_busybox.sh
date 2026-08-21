#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Cross-compile a static riscv64 BusyBox for the Asterinas NixOS track (M1).
#
# Builds the applet set the M1 smoke test needs: sh (ash), ls, cat, mount, ps,
# plus a small handful of helpers (mkdir/rm/echo/test/grep/find/dd/df/free/...)
# to exercise the syscall surface and make the interactive shell usable.
#
# The config starts from `allnoconfig` so the resulting binary is deterministic
# and small, then flips exactly the applets below to =y. Output lands in
# target/nixos/busybox (the stripped static ELF).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
BUILD_ROOT="${REPO_ROOT}/target/nixos"
SRC_DIR="${BUILD_ROOT}/busybox-1.36.1"
TARBALL="${BUILD_ROOT}/busybox-1.36.1.tar.bz2"
OUTPUT="${1:-${BUILD_ROOT}/busybox}"

BUSYBOX_VERSION=1.36.1
BUSYBOX_URL="https://busybox.net/downloads/busybox-${BUSYBOX_VERSION}.tar.bz2"

CROSS_PREFIX="riscv64-linux-gnu-"
CC="${CROSS_PREFIX}gcc"
JOBS="$(nproc)"

# Applets to enable on top of allnoconfig. Each is a busybox Kconfig symbol
# whose allnoconfig line reads `# CONFIG_<NAME> is not set`.
readonly -a APPLETS=(
    ASH                             # sh (ash)
    LS CAT MOUNT PS                 # M1 core smoke-test commands
    UMOUNT MOUNTPOINT               # mount lifecycle
    MKDIR RM RMDIR LN MKNOD CHMOD CHOWN
    ECHO PRINTF TEST GREP FIND HEAD TAIL DD
    DF FREE UNAME SYNC STAT SLEEP KILL PIDOF
    TRUE FALSE YES
    CP                          # LTP helpers shell out to cp (execve02, execveat01)
    GETTY LOGIN                 # serial console login (systemd getty@ttyS0)
    LOGGER HOSTNAME             # journald injection + hostname helper
)

mkdir -p "${BUILD_ROOT}"

if [[ ! -f "${TARBALL}" ]]; then
    echo "downloading busybox-${BUSYBOX_VERSION}"
    curl -fsSL "${BUSYBOX_URL}" -o "${TARBALL}"
fi

if [[ ! -d "${SRC_DIR}" ]]; then
    tar -xjf "${TARBALL}" -C "${BUILD_ROOT}"
fi

cd "${SRC_DIR}"

# Deterministic baseline config.
make CC="${CC}" CROSS_COMPILE="${CROSS_PREFIX}" allnoconfig >/dev/null

# Static linking and the standalone-shell helper.
sed -i 's/^# CONFIG_STATIC is not set$/CONFIG_STATIC=y/' .config
sed -i 's/^# CONFIG_FEATURE_SH_STANDALONE is not set$/CONFIG_FEATURE_SH_STANDALONE=y/' .config

# Internal /etc/passwd + /etc/group parsing. Static glibc cannot dlopen the NSS
# "files" module, so libc getpwnam()/getgrnam() always fail in a static binary.
# This makes busybox's login/getty read the passwd/group files directly.
sed -i 's/^# CONFIG_USE_BB_PWD_GRP is not set$/CONFIG_USE_BB_PWD_GRP=y/' .config

# Internal DES crypt(). glibc >= 2.41 dropped libcrypt/crypt.h, but busybox's
# login applet still compiles pw_encrypt.c, which #includes <crypt.h> unless
# USE_BB_CRYPT is set. Using busybox's own crypt avoids the missing header and
# the static-NSS password issue in one move (empty root password never calls it).
sed -i 's/^# CONFIG_USE_BB_CRYPT is not set$/CONFIG_USE_BB_CRYPT=y/' .config

# Flip each applet from "not set" to =y in place.
for applet in "${APPLETS[@]}"; do
    sed -i "s/^# CONFIG_${applet} is not set$/CONFIG_${applet}=y/" .config
done

# Resolve Kconfig dependencies non-interactively (defaults for any new symbols).
make CC="${CC}" CROSS_COMPILE="${CROSS_PREFIX}" oldconfig </dev/null >/dev/null

make CC="${CC}" CROSS_COMPILE="${CROSS_PREFIX}" -j"${JOBS}" busybox

# Verify the result is a static RISC-V ELF, then publish it.
cp busybox "${OUTPUT}"
"${CROSS_PREFIX}strip" "${OUTPUT}"

echo "built ${OUTPUT}"
file "${OUTPUT}"
