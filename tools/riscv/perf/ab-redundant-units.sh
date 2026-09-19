#!/usr/bin/env bash
# Build one arm of the redundant-units A/B and run the desktop gate on it,
# timed by measure-desktop.sh.
#
# Both arms go through the same steps -- toggle the mask in the base image,
# re-materialize a derived image, run the gate -- so the only difference
# between them is the mask itself. The mask has to live in the base image
# because the gate recomputes a derived image's hash from (base, spec).
#
# Usage: ab-redundant-units.sh on|off <run-name>
set -euo pipefail

readonly STATE="${1:?usage: ab-redundant-units.sh on|off <run-name>}"
readonly NAME="${2:?usage: ab-redundant-units.sh on|off <run-name>}"

readonly ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
readonly BASE="$ROOT/target/debian-riscv/desktop-drm/rootfs"
readonly DERIVED="$ROOT/target/drm-mesa/rootfs-ab-$STATE"

"$(dirname "${BASH_SOURCE[0]}")/toggle-redundant-units.sh" "$STATE"

sudo rm -rf "$DERIVED"
sudo python3 -m tools.riscv.debian.rootfs.dev_overlay materialize \
    --base-dir "$BASE" \
    --spec "$ROOT/target/drm-mesa/spec.json" \
    --output-dir "$DERIVED" >/dev/null

printf '=== arm %s (%s) ===\n' "$STATE" "$NAME"
exec "$(dirname "${BASH_SOURCE[0]}")/measure-desktop.sh" "$NAME" \
    "DEBIAN_DRM_ROOT_IMAGE=$DERIVED/debian-root.ext2" \
    "DEBIAN_DRM_ROOT_MANIFEST=$DERIVED/rootfs-manifest.json" \
    DEBIAN_DRM_GRAPHICS_DEVICE=virtio-gpu-device \
    DEBIAN_DESKTOP_BOOT_TIMEOUT=${DEBIAN_DESKTOP_BOOT_TIMEOUT:-1800}
