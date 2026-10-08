#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# POLISH-M7: layer the ALSA userspace onto the systemd rootfs.
#
# systemd (PID 1, riscv64 glibc) boots the full system; the ALSA userspace is
# the Alpine prebuilt riscv64-musl trio (musl loader + libasound + aplay/
# speaker-test/amixer), which coexists with glibc because each dynamic binary
# carries its own interpreter path. This script:
#   1. (re)builds the base systemd rootfs via build_systemd_boot.sh, then
#   2. layers the musl loader + libc + libasound + alsa-utils + a 440 Hz WAV,
#   3. re-packs as a raw newc cpio (the kernel's zune-inflate decoder hangs
#      non-deterministically on >16 MB gzip inputs — see M3-report.md).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="${REPO_ROOT}/target/nixos/systemd"
ROOTFS="${BUILD_ROOT}/rootfs"
OUTPUT="${1:-${BUILD_ROOT}/systemd-alsa-initramfs.cpio.gz}"

# Alpine prebuilt ALSA userspace cache (built by tools/riscv/nixos/audio/build_alsa.sh).
ALSA_CACHE="${REPO_ROOT}/target/nixos/audio/alpine"
MUSL="musl-1.2.5-r12"
ALSA_LIB="alsa-lib-1.2.14-r0"
ALSA_UTILS="alsa-utils-1.2.14-r0"

# --- 1. base systemd rootfs (glibc runtime + systemd + units) -----------------------
echo "=== building base systemd rootfs ==="
bash "${SRC_DIR}/build_systemd_boot.sh" >/dev/null
[[ -d "${ROOTFS}" ]] || { echo "base systemd rootfs missing: ${ROOTFS}" >&2; exit 2; }

for pkg in "${MUSL}" "${ALSA_LIB}" "${ALSA_UTILS}"; do
    [[ -d "${ALSA_CACHE}/${pkg}.d" ]] || {
        echo "missing ALSA cache ${ALSA_CACHE}/${pkg}.d — run tools/riscv/nixos/audio/build_alsa.sh first" >&2
        exit 2
    }
done

# --- 2. layer the ALSA userspace ----------------------------------------------------
echo "=== layering ALSA userspace (musl + libasound + alsa-utils) ==="
mkdir -p "${ROOTFS}/usr/share/alsa" "${ROOTFS}/etc/alsa" "${ROOTFS}/dev/snd"

# musl loader + libc (soname symlink to the loader), alongside the glibc runtime.
cp -a "${ALSA_CACHE}/${MUSL}.d/lib/ld-musl-riscv64.so.1" "${ROOTFS}/lib/"
ln -sf ld-musl-riscv64.so.1 "${ROOTFS}/lib/libc.musl-riscv64.so.1"

# alsa-lib: libasound + libatopology + config tree.
cp -a "${ALSA_CACHE}/${ALSA_LIB}.d/usr/lib/libasound.so.2" \
      "${ALSA_CACHE}/${ALSA_LIB}.d/usr/lib/libasound.so.2.0.0" "${ROOTFS}/usr/lib/"
cp -a "${ALSA_CACHE}/${ALSA_LIB}.d/usr/lib/libatopology.so.2" \
      "${ALSA_CACHE}/${ALSA_LIB}.d/usr/lib/libatopology.so.2.0.0" "${ROOTFS}/usr/lib/"
cp -a "${ALSA_CACHE}/${ALSA_LIB}.d/usr/share/alsa/." "${ROOTFS}/usr/share/alsa/"
cp -a "${ALSA_CACHE}/${ALSA_LIB}.d/etc/alsa/." "${ROOTFS}/etc/alsa/"

# alsa-utils: only the binaries we exercise (aplay / speaker-test / amixer).
for b in aplay speaker-test amixer; do
    cp -a "${ALSA_CACHE}/${ALSA_UTILS}.d/usr/bin/${b}" "${ROOTFS}/usr/bin/"
done

# --- 3. 440 Hz / 48 kHz / S16LE / stereo WAV test tone -----------------------------
python3 - "${ROOTFS}/sine.wav" <<'PY'
import math, struct, sys, wave

rate, ch, sec, freq = 48000, 2, 1, 440.0
frames = rate * sec
data = bytearray()
for i in range(frames):
    s = int(16383.0 * math.sin(2.0 * math.pi * freq * i / rate))
    for _ in range(ch):
        data += struct.pack("<h", s)

with wave.open(sys.argv[1], "wb") as w:
    w.setnchannels(ch)
    w.setsampwidth(2)
    w.setframerate(rate)
    w.writeframes(bytes(data))
print(f"generated {sys.argv[1]}: {len(data)} PCM bytes @ {rate} Hz, {ch} ch")
PY

# --- 4. pack (raw newc, no gzip) ---------------------------------------------------
( cd "${ROOTFS}" && find . | cpio -o -H newc 2>/dev/null > "${OUTPUT}" )

echo "built ${OUTPUT}"
echo "  aplay:     $(file -b "${ROOTFS}/usr/bin/aplay" | cut -c1-70)"
echo "  ld-musl:   $(file -b "${ROOTFS}/lib/ld-musl-riscv64.so.1" | cut -c1-70)"
echo "  libasound: $(file -b "${ROOTFS}/usr/lib/libasound.so.2" | cut -c1-70)"
du -sh "${ROOTFS}"
