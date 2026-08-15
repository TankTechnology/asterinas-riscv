#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Assemble the ALSA initramfs: the Alpine prebuilt musl + alsa-lib + alsa-utils
# userspace (no cross-compile), a 440 Hz / 48 kHz / S16LE / stereo WAV test tone,
# and a static /init that forks + execs `aplay -D hw:0,0 /sine.wav`.
#
# The APKs are pulled from the TUNA Alpine mirror (direct-connect, free) on
# first use and cached under target/nixos/audio/alpine/.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="${REPO_ROOT}/target/nixos/audio"
CACHE_DIR="${BUILD_ROOT}/alpine"
ROOTFS="${BUILD_ROOT}/alsa-rootfs"
OUTPUT="${1:-${BUILD_ROOT}/alsa-initramfs.cpio.gz}"

MIRROR="https://mirrors.tuna.tsinghua.edu.cn/alpine/v3.22/main/riscv64"
MUSL="musl-1.2.5-r12"
ALSA_LIB="alsa-lib-1.2.14-r0"
ALSA_UTILS="alsa-utils-1.2.14-r0"

CC="${RISC_V_CC:-riscv64-linux-gnu-gcc}"

# --- 1. fetch + unpack APKs (cached) -------------------------------------------------
mkdir -p "${CACHE_DIR}"
for pkg in "${MUSL}" "${ALSA_LIB}" "${ALSA_UTILS}"; do
    if [[ ! -d "${CACHE_DIR}/${pkg}.d" ]]; then
        curl -sS --max-time 120 -o "${CACHE_DIR}/${pkg}.apk" "${MIRROR}/${pkg}.apk"
        mkdir -p "${CACHE_DIR}/${pkg}.d"
        tar -xzf "${CACHE_DIR}/${pkg}.apk" -C "${CACHE_DIR}/${pkg}.d"
    fi
done

# --- 2. assemble rootfs --------------------------------------------------------------
rm -rf "${ROOTFS}"
mkdir -p "${ROOTFS}/lib" "${ROOTFS}/usr/lib" "${ROOTFS}/usr/bin" \
         "${ROOTFS}/usr/share/alsa" "${ROOTFS}/etc/alsa" \
         "${ROOTFS}/dev/snd" "${ROOTFS}/proc" "${ROOTFS}/sys" "${ROOTFS}/tmp"

# musl loader + libc (soname symlink to the loader).
cp -a "${CACHE_DIR}/${MUSL}.d/lib/ld-musl-riscv64.so.1" "${ROOTFS}/lib/"
ln -sf ld-musl-riscv64.so.1 "${ROOTFS}/lib/libc.musl-riscv64.so.1"

# alsa-lib: libasound + libatopology + config tree.
cp -a "${CACHE_DIR}/${ALSA_LIB}.d/usr/lib/libasound.so.2" \
      "${CACHE_DIR}/${ALSA_LIB}.d/usr/lib/libasound.so.2.0.0" "${ROOTFS}/usr/lib/"
cp -a "${CACHE_DIR}/${ALSA_LIB}.d/usr/lib/libatopology.so.2" \
      "${CACHE_DIR}/${ALSA_LIB}.d/usr/lib/libatopology.so.2.0.0" "${ROOTFS}/usr/lib/"
cp -a "${CACHE_DIR}/${ALSA_LIB}.d/usr/share/alsa/." "${ROOTFS}/usr/share/alsa/"
cp -a "${CACHE_DIR}/${ALSA_LIB}.d/etc/alsa/." "${ROOTFS}/etc/alsa/"

# alsa-utils: only the binaries we exercise (aplay / speaker-test / amixer).
for b in aplay speaker-test amixer; do
    cp -a "${CACHE_DIR}/${ALSA_UTILS}.d/usr/bin/${b}" "${ROOTFS}/usr/bin/"
done

# --- 3. generate the 440 Hz test tone WAV -------------------------------------------
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

# --- 4. static /init ----------------------------------------------------------------
"${CC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${ROOTFS}/init" "${SRC_DIR}/init_alsa.c"

# --- 5. pack ------------------------------------------------------------------------
( cd "${ROOTFS}" && find . | cpio -o -H newc 2>/dev/null | gzip -9 > "${OUTPUT}" )

echo "built ${OUTPUT}"
