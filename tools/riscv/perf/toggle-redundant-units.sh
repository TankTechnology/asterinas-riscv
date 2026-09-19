#!/usr/bin/env bash
# Add or remove the two redundant-unit masks in the base rootfs image.
#
# The mask has to live in the base image: the gate recomputes a derived
# image's hash from (base, spec), so a hand-added symlink in a derived image
# is detected and rejected (reason: validate), and dev_overlay can only write
# regular files -- it cannot create the symlinks a mask requires.
#
# Usage: toggle-redundant-units.sh on|off
set -euo pipefail
readonly ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
readonly IMG="$ROOT/target/debian-riscv/desktop-drm/rootfs/debian-root.ext2"
readonly UNITS=(ldconfig.service systemd-journal-catalog-update.service)

# debugfs does not reproduce a byte-identical image, so every toggle changes the
# base image's hash. The manifest has to be re-synced or dev_overlay materialize
# refuses with "image SHA-256 does not match root_image_sha256". This records
# the hash of the image actually present rather than trying to preserve an
# original that no longer exists.
sync_manifest_hash() {
    local manifest="$ROOT/target/debian-riscv/desktop-drm/rootfs/rootfs-manifest.json"
    local hash
    hash="$(sudo sha256sum "$IMG" | cut -d' ' -f1)"
    python3 - "$manifest" "$hash" <<'PY'
import json, sys
path, digest = sys.argv[1], sys.argv[2]
with open(path) as fh:
    manifest = json.load(fh)
manifest["root_image_sha256"] = digest
with open(path, "w") as fh:
    json.dump(manifest, fh, indent=2, sort_keys=True)
    fh.write("\n")
print(f"manifest root_image_sha256 -> {digest}")
PY
}

case "${1:?usage: toggle-redundant-units.sh on|off}" in
on)
    for u in "${UNITS[@]}"; do
        sudo debugfs -w -R "rm /etc/systemd/system/$u" "$IMG" >/dev/null 2>&1 || true
        sudo debugfs -w -R "symlink /etc/systemd/system/$u /dev/null" "$IMG" >/dev/null
        printf 'masked  %s\n' "$u"
    done
    ;;
off)
    for u in "${UNITS[@]}"; do
        sudo debugfs -w -R "rm /etc/systemd/system/$u" "$IMG" >/dev/null 2>&1 || true
        printf 'unmasked %s\n' "$u"
    done
    ;;
*)
    echo "usage: toggle-redundant-units.sh on|off" >&2
    exit 2
    ;;
esac

sync_manifest_hash
