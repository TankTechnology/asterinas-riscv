#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# NIXOS-STAGE2-M1: assemble the NixOS-style systemd desktop rootfs.
#
# Layers the nix profile onto the SYSTEMD-DESKTOP-M1 rootfs so that
# nix-profile-installed software is referenced from the systemd environment in
# the canonical NixOS way: `/nix/var/nix/profiles/default` is the active
# profile, `/run/current-system/sw` is the symlink farm, and an activation
# script (`/etc/activate`, a minimal switch-to-configuration) links the profile
# into `/usr/local/bin` and generates `/etc/profile` + `/etc/environment` +
# `/etc/environment.d/10-nix.conf`.
#
# The nix *products* (binaries + their musl runtime closure) are copied from the
# sibling asterinas-riscv-nixos tree (session A's M9), which already cross-built
# them: `hello`/`nixos-info`/`fortune`/`heartbeat` (musl PIE) and the real
# Alpine packages `curl` 8.21.0 + `jq` 1.8.2. We do NOT re-run nix in the guest
# (that is M9's slow ~60 s path) and we do NOT compile anything here — we
# arrange the prebuilt products into a synthesized `/nix/store` + profile
# exactly as `nix profile install` would leave them. Store-path names are
# human-readable placeholders (content hashes are not computed; see report §Nix
# store).
#
# Output is the same raw newc cpio (no gzip) the desktop milestone proved.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Session A's tree — the source of the nix products. Overridable.
NIXOS_REPO="${NIXOS_REPO:-$(cd "${REPO_ROOT}/.." && pwd)/asterinas-riscv-nixos}"
NIX_M9="${NIXOS_REPO}/target/nixos/m9/rootfs"

OUTPUT="${REPO_ROOT}/target/qemu-uboot/systemd-desktop-nix-initramfs.cpio"

STRIP_MUSL="${RISC_V_MUSL_STRIP:-riscv64-linux-musl-strip}"

[[ -d "${NIX_M9}/m9/prebuilt" ]] || { echo "missing nix products: ${NIX_M9}" >&2; exit 2; }

echo "=== assembling systemd + desktop base rootfs (--no-pack) ==="
bash "${SRC_DIR}/build_systemd_desktop.sh" --no-pack
# The base rootfs path is deterministic (target/systemd-desktop/rootfs).
ROOTFS="${REPO_ROOT}/target/systemd-desktop/rootfs"

echo "=== layering nix profile ==="

# 1. musl runtime into /lib (the nix products are musl-PIE binaries; the desktop
#    rootfs only carries glibc). musl bundles libc into the loader, so
#    ld-musl-riscv64.so.1 is both interpreter and libc.
cp -a "${NIX_M9}/lib/ld-musl-riscv64.so.1" "${ROOTFS}/lib/"
cp -a "${NIX_M9}/lib/libc.musl-riscv64.so.1" "${ROOTFS}/lib/"

# 1b. Replace the minimal busybox (built with only the `sh` applet — enough for
#     the desktop milestone, which execs binaries by absolute path) with session
#     A's full musl busybox. The nix activation script shells out to
#     mkdir/ln/rm/cat/..., which the minimal build does not have compiled in.
cp "${NIX_M9}/bin/busybox" "${ROOTFS}/bin/busybox"
for a in sh mkdir ln rm cat ls echo head grep test sleep find mount umount \
         mountpoint mknod ps df free sync kill tail true false; do
    ln -sfn busybox "${ROOTFS}/bin/${a}"
done

# 2. The two profile generations' binaries into the store.
STORE="${ROOTFS}/nix/store"
mkdir -p "${STORE}/m9-core-1.0/bin" "${STORE}/m9-real-1.0/bin"
for b in hello nixos-info fortune heartbeat; do
    cp "${NIX_M9}/m9/prebuilt/${b}" "${STORE}/m9-core-1.0/bin/${b}"
    "${STRIP_MUSL}" --strip-unneeded "${STORE}/m9-core-1.0/bin/${b}" 2>/dev/null || true
done
for b in curl jq; do
    cp "${NIX_M9}/m9/pkg/${b}" "${STORE}/m9-real-1.0/bin/${b}"
done
chmod 755 "${STORE}"/m9-*-1.0/bin/*

# 3. The musl shared-library closure of curl + jq into /usr/lib (preserving the
#    X.Y -> X.Y.Z soname symlink chain the loader expects). We copy the exact
#    closure, not the whole ~50 MB m9 /usr/lib (which also carries the nix
#    daemon libs we do not need).
mkdir -p "${ROOTFS}/usr/lib"
for soname in libcurl.so.4 libz.so.1 libssl.so.3 libcrypto.so.3 libnghttp2.so.14 \
              libbrotlicommon.so.1 libbrotlidec.so.1 libcares.so.2 libidn2.so.0 \
              libunistring.so.5 libpsl.so.5 libzstd.so.1 libjq.so.1 libonig.so.5; do
    src="${NIX_M9}/usr/lib/${soname}"
    [[ -e "${src}" ]] || { echo "WARNING: missing closure lib ${soname}" >&2; continue; }
    if [[ -L "${src}" ]]; then
        # X.Y -> X.Y.Z soname chain: copy the real file, recreate the symlink.
        real="$(readlink -f "${src}")"
        cp -a "${real}" "${ROOTFS}/usr/lib/"
        ln -sfn "$(basename "${real}")" "${ROOTFS}/usr/lib/${soname}"
    else
        # Some musl libs (libssl.so.3, libcrypto.so.3) ship unversioned.
        cp -a "${src}" "${ROOTFS}/usr/lib/${soname}"
    fi
done

# 4. The profile — a "user environment" store path whose bin/ symlinks into the
#    two generations, plus the /nix/var/nix/profiles/default symlink chain that
#    `nix profile install` leaves behind. Symlink *targets* are guest-absolute
#    (/nix/store/...) — the store lives at /nix/store in the guest, not at the
#    host staging path.
PROFILE_ENV="${STORE}/m9-profile-1.0"
mkdir -p "${PROFILE_ENV}/bin"
for b in hello nixos-info fortune heartbeat; do
    ln -sfn "/nix/store/m9-core-1.0/bin/${b}" "${PROFILE_ENV}/bin/${b}"
done
for b in curl jq; do
    ln -sfn "/nix/store/m9-real-1.0/bin/${b}" "${PROFILE_ENV}/bin/${b}"
done
mkdir -p "${ROOTFS}/nix/var/nix/profiles"
ln -sfn "/nix/store/m9-profile-1.0" "${ROOTFS}/nix/var/nix/profiles/default-1-link"
ln -sfn "default-1-link" "${ROOTFS}/nix/var/nix/profiles/default"

# 5. Activation + smoke scripts, and the nix units.
cp "${SRC_DIR}/nixos/activate"     "${ROOTFS}/etc/activate"
cp "${SRC_DIR}/nixos/nix-smoke.sh" "${ROOTFS}/etc/nix-smoke.sh"
chmod 755 "${ROOTFS}/etc/activate" "${ROOTFS}/etc/nix-smoke.sh"
cp "${SRC_DIR}"/units/nix-activation.service \
   "${SRC_DIR}"/units/nix-smoke.service \
   "${ROOTFS}/etc/systemd/system/"
# Re-apply the unit set so the updated graphical.target (which now Wants the
# nix units) is picked up.
cp "${SRC_DIR}"/units/*.target "${SRC_DIR}"/units/*.service \
    "${ROOTFS}/etc/systemd/system/"

# 6. Pack as raw newc cpio.
( cd "${ROOTFS}" && find . | cpio -o -H newc 2>/dev/null > "${OUTPUT}" )

echo "built ${OUTPUT}"
echo "  profile: ${PROFILE_ENV}/bin/ -> $(ls "${PROFILE_ENV}/bin" | tr '\n' ' ')"
echo "  store paths: $(ls "${STORE}")"
echo "  nix binaries: $(file -b "${STORE}/m9-core-1.0/bin/hello" | cut -c1-60)"
echo "  curl:         $(file -b "${STORE}/m9-real-1.0/bin/curl" | cut -c1-60)"
du -sh "${ROOTFS}"
echo "  initramfs: $(du -h "${OUTPUT}" | cut -f1)"
