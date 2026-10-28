#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# make_megrez_package.sh -- build the Megrez board boot package in one command.
#
# Produces, in the output directory:
#
#   asterinas-megrez-<commit>-<date>.booti   RISC-V Linux Image (U-Boot `booti`)
#   rv-init-megrez-<commit>-<date>.cpio.gz   standard initramfs (PID 1 = /init)
#   SHA256SUMS                               sha256 of both
#   MANIFEST.md                              identities, U-Boot commands, limits
#
# and then runs the QEMU U-Boot preflight (`verify_megrez_sim.sh`, profile
# megrez-sv48-svade-fast) against the artifacts it just built, so a package is
# never produced without a boot-chain check.
#
# The kernel is built for Sv48 (the scheme the board boots with); the initramfs
# comes from the development container because it is a Nix build.
#
# Usage:
#   tools/riscv/make_megrez_package.sh [--out DIR] [--no-preflight]
#                                      [--initramfs FILE] [--image FILE]
#
#   --out DIR         output directory (default: target/megrez-package)
#   --no-preflight    skip the QEMU U-Boot preflight
#   --initramfs FILE  use this initramfs instead of building one in a container
#   --image FILE      use this kernel Image instead of building it

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
cd "${REPO_ROOT}"

OUT_DIR="${REPO_ROOT}/target/megrez-package"
RUN_PREFLIGHT=1
INITRAMFS=""
IMAGE=""
DEV_IMAGE="${ASTERINAS_DEV_IMAGE:-asterinas/asterinas:0.18.0-20260702}"

while [ $# -gt 0 ]; do
    case "$1" in
        --out) OUT_DIR="$2"; shift 2 ;;
        --no-preflight) RUN_PREFLIGHT=0; shift ;;
        --initramfs) INITRAMFS="$2"; shift 2 ;;
        --image) IMAGE="$2"; shift 2 ;;
        -h|--help) sed -n '4,30p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

COMMIT="$(git rev-parse --short origin/main)"
RUN_ID="$(date +%Y%m%d)"
BOOTI_NAME="asterinas-megrez-${COMMIT}-${RUN_ID}.booti"
INITRD_NAME="rv-init-megrez-${COMMIT}-${RUN_ID}.cpio.gz"

echo "== [1/5] kernel image =="
if [ -z "${IMAGE}" ]; then
    if ! git diff --quiet origin/main -- kernel/; then
        echo "warning: kernel/ differs from origin/main; the package will state" >&2
        echo "         commit ${COMMIT}, so commit the change first if it matters" >&2
    fi
    # OSDK resolves the initramfs path while loading its manifest, and the
    # in-tree path is a Nix symlink that does not resolve on the host.
    cp OSDK.toml /tmp/megrez-OSDK.toml.bak
    trap 'cp /tmp/megrez-OSDK.toml.bak OSDK.toml' EXIT
    sed -i 's#^initramfs = .*#initramfs = "/tmp/empty.cpio.gz"#' OSDK.toml
    printf '' | gzip -1 > /tmp/empty.cpio.gz
    cargo osdk build --target-arch riscv64 --scheme riscv
    cp /tmp/megrez-OSDK.toml.bak OSDK.toml
    trap - EXIT
    IMAGE="${REPO_ROOT}/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image"
fi
[ -f "${IMAGE}" ] || { echo "no kernel image at ${IMAGE}" >&2; exit 1; }

echo "== [2/5] initramfs =="
if [ -z "${INITRAMFS}" ]; then
    if ! command -v docker >/dev/null 2>&1; then
        echo "docker is required to build the initramfs (pass --initramfs to skip)" >&2
        exit 1
    fi
    docker run --rm -v "${REPO_ROOT}:/root/asterinas" -w /root/asterinas "${DEV_IMAGE}" \
        bash -lc 'make initramfs >/tmp/init.log 2>&1 && cp -f "$(readlink -f test/initramfs/build/initramfs.cpio.gz)" target/megrez/initramfs.cpio.gz' \
        || { echo "initramfs build failed; see the container output above" >&2; exit 1; }
    INITRAMFS="${REPO_ROOT}/target/megrez/initramfs.cpio.gz"
fi
[ -f "${INITRAMFS}" ] || { echo "no initramfs at ${INITRAMFS}" >&2; exit 1; }

echo "== [3/5] packaging into ${OUT_DIR} =="
mkdir -p "${OUT_DIR}"
cp -f "${IMAGE}" "${OUT_DIR}/${BOOTI_NAME}"
cp -f "${INITRAMFS}" "${OUT_DIR}/${INITRD_NAME}"
chmod 644 "${OUT_DIR}/${BOOTI_NAME}" "${OUT_DIR}/${INITRD_NAME}"

python3 - "${OUT_DIR}" "${BOOTI_NAME}" "${INITRD_NAME}" <<'PY'
import hashlib, os, sys, zlib
out, booti, initrd = sys.argv[1:4]
rows = []
for name in (booti, initrd):
    path = os.path.join(out, name)
    blob = open(path, "rb").read()
    rows.append((name, len(blob), hashlib.sha256(blob).hexdigest(),
                 format(zlib.crc32(blob) & 0xFFFFFFFF, "08x")))
with open(os.path.join(out, "SHA256SUMS"), "w") as f:
    for name, _, sha, _ in rows:
        f.write(f"{sha}  {name}\n")
with open(os.path.join(out, "identities.tsv"), "w") as f:
    for row in rows:
        f.write("\t".join(str(x) for x in row) + "\n")
for name, size, sha, crc in rows:
    print(f"  {name}: {size} bytes sha256={sha} crc32={crc}")
PY

echo "== [4/5] manifest =="
{
    echo "# Megrez 上板产物包（${RUN_ID}）"
    echo
    echo "内核源码提交：\`${COMMIT}\`。"
    echo
    echo "| 文件 | 大小 (字节) | SHA-256 | CRC32 |"
    echo "|---|---|---|---|"
    while IFS=$'\t' read -r name size sha crc; do
        echo "| \`${name}\` | ${size} | \`${sha}\` | \`${crc}\` |"
    done < "${OUT_DIR}/identities.tsv"
    cat <<'TEXT'

`.booti` 是 RISC-V Linux Image v0.2（magic `RISCV`@0x30，version 2），
U-Boot 用 `booti` 加载；`.cpio.gz` 是标准 initramfs，PID 1 = `/init`。

## U-Boot 侧（网络传输）

```text
setenv ipaddr 10.100.19.200
setenv serverip 10.100.19.1
tftpboot 0x80200000 <booti 文件名>
setenv aster_size ${filesize}
crc32 0x80200000 ${aster_size}          # 与上表 CRC32 核对
tftpboot 0x83000000 <initramfs 文件名>
setenv initrd_size ${filesize}
crc32 0x83000000 ${initrd_size}         # 与上表 CRC32 核对
booti 0x80200000 0x83000000:${initrd_size}
```

没有网线时用 `tools/riscv/paced_serial_bridge.py` + `loady`，或
`megrez_board_session.py`（自带 YMODEM）。**绝不 `saveenv`。**

## 仍然缺的

- 板级 DTB：从板子 SD 的 `/boot` 取，用 `tools/riscv/megrez_patch_dtb.py` 修补。
- 主机有线网卡（当前只有 WiFi）。
- 真机首启验证：main 这条支线尚未在 EIC7700 上跑过。
TEXT
} > "${OUT_DIR}/MANIFEST.md"
rm -f "${OUT_DIR}/identities.tsv"

if [ "${RUN_PREFLIGHT}" = "1" ]; then
    echo "== [5/5] QEMU U-Boot preflight =="
    QEMU_UBOOT_PROFILE=megrez-sv48-svade-fast \
    ASTERINAS_RISCV_BOOTI="${OUT_DIR}/${BOOTI_NAME}" \
        tools/riscv/verify_megrez_sim.sh
else
    echo "== [5/5] preflight skipped (--no-preflight) =="
fi

echo
echo "package: ${OUT_DIR}"
ls -l "${OUT_DIR}"
