#!/usr/bin/env bash
# Mask or unmask the desktop session in the base rootfs image.
#
# The per-operation benchmark is emitted by asterinas-desktop-drm-evidence.service
# and the desktop session is asterinas-desktop-drm.service; both are
# `WantedBy=graphical.target`, so they start together and the benchmark runs
# while Xorg and Mesa are coming up.  On the Debian control the same benchmark
# finishes in about two seconds, before its desktop matters, so comparing the
# two numbers compares a loaded kernel against an idle one.
#
# Masking the session leaves the guest otherwise idle while the evidence
# service still runs and still emits its BENCH lines, which is what makes the
# two kernels comparable.
#
# Like the redundant-unit masks, this has to live in the base image: the gate
# recomputes a derived image's hash from (base, spec), and dev_overlay cannot
# create the symlinks a mask requires.  debugfs is not byte-reproducible, so
# the manifest hash is re-synced afterwards.
#
# ---------------------------------------------------------------------------
# This script used to `rm` the unit and then create the /dev/null symlink at
# the same path, and to `rm` again when unmasking.  A mask is a symlink that
# *replaces* the unit, so the first `on` deleted the real unit file and the
# `off` that followed deleted the mask too, leaving no unit at all.  The
# symptom was a desktop that never started (`session=no xorg-log=no`) with
# every device present, which reads like a kernel regression and is really a
# missing file.
#
# So: the original is *moved aside*, never deleted, and unmasking restores it.
# Nothing here removes a file without having saved its contents first, and the
# script refuses to mask a unit it could not back up.
# ---------------------------------------------------------------------------
#
# Usage: toggle-desktop-session.sh on|off
set -euo pipefail

readonly ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
readonly REAL_IMG="$ROOT/target/debian-riscv/desktop-drm/rootfs/debian-root.ext2"
# Overridable so the masking logic can be exercised against a throwaway copy.
# It edits the image in place and there is no undo, so testing it on the real
# base image is how the unit file got destroyed twice.
readonly IMG="${ASTERINAS_DESKTOP_SESSION_IMG:-$REAL_IMG}"
readonly MANIFEST="$ROOT/target/debian-riscv/desktop-drm/rootfs/rootfs-manifest.json"
readonly UNIT="/etc/systemd/system/asterinas-desktop-drm.service"
# Outside the unit directories on purpose: systemd never scans /etc itself, so
# a stray backup there cannot be loaded as a unit by accident.
readonly BACKUP_IMG="/etc/asterinas-desktop-drm.service.orig"
readonly BACKUP_HOST="/tmp/asterinas-desktop-drm.service.orig"

type_of() {
    sudo debugfs -R "stat $1" "$IMG" 2>/dev/null |
        sed -n 's/.*Type: \([a-z]*\).*/\1/p' | head -1
}

unit_type() { type_of "$UNIT"; }

sync_manifest_hash() {
    # Only the real base image has a manifest entry to keep in step; a
    # throwaway copy used for testing must not rewrite it.
    if [[ "$IMG" != "$REAL_IMG" ]]; then
        printf '(skipping manifest sync: %s is not the base image)\n' "$IMG"
        return 0
    fi
    local hash
    hash="$(sudo sha256sum "$IMG" | cut -d' ' -f1)"
    python3 - "$MANIFEST" "$hash" <<'PY'
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

case "${1:?usage: toggle-desktop-session.sh on|off}" in
on)
    case "$(unit_type)" in
    symlink)
        printf 'already masked  %s\n' "$UNIT"
        ;;
    regular)
        # Preserve the original before anything replaces it.
        sudo rm -f "$BACKUP_HOST"
        sudo debugfs -R "dump $UNIT $BACKUP_HOST" "$IMG" >/dev/null 2>&1
        if [[ ! -s "$BACKUP_HOST" ]]; then
            printf 'ERROR: could not back up %s; refusing to mask it\n' "$UNIT" >&2
            exit 1
        fi
        sudo debugfs -w -R "write $BACKUP_HOST $BACKUP_IMG" "$IMG" >/dev/null
        sudo debugfs -w -R "sif $BACKUP_IMG mode 0100644" "$IMG" >/dev/null
        sudo debugfs -w -R "rm $UNIT" "$IMG" >/dev/null
        sudo debugfs -w -R "symlink $UNIT /dev/null" "$IMG" >/dev/null
        printf 'masked   %s (original saved to %s, %s bytes)\n' \
            "$UNIT" "$BACKUP_IMG" "$(wc -c <"$BACKUP_HOST")"
        ;;
    *)
        printf 'ERROR: %s not found in %s; nothing to mask\n' "$UNIT" "$IMG" >&2
        exit 1
        ;;
    esac
    ;;
off)
    case "$(unit_type)" in
    symlink)
        # Check the saved original is really there *before* removing anything,
        # and restore it in the same breath as removing the mask.  An earlier
        # version removed the mask and then only checked for the backup without
        # ever writing it back, which deleted the unit a second time.
        if [[ "$(type_of "$BACKUP_IMG")" != "regular" ]]; then
            printf 'ERROR: no saved original at %s; refusing to unmask %s\n' \
                "$BACKUP_IMG" "$UNIT" >&2
            exit 1
        fi
        sudo rm -f "$BACKUP_HOST"
        sudo debugfs -R "dump $BACKUP_IMG $BACKUP_HOST" "$IMG" >/dev/null 2>&1
        if [[ ! -s "$BACKUP_HOST" ]]; then
            printf 'ERROR: could not read %s; refusing to touch %s\n' \
                "$BACKUP_IMG" "$UNIT" >&2
            exit 1
        fi
        sudo debugfs -w -R "rm $UNIT" "$IMG" >/dev/null
        sudo debugfs -w -R "write $BACKUP_HOST $UNIT" "$IMG" >/dev/null
        sudo debugfs -w -R "sif $UNIT mode 0100644" "$IMG" >/dev/null
        sudo debugfs -w -R "rm $BACKUP_IMG" "$IMG" >/dev/null 2>&1 || true
        printf 'unmasked %s (restored, %s bytes)\n' "$UNIT" "$(wc -c <"$BACKUP_HOST")"
        ;;
    regular)
        printf 'already unmasked  %s\n' "$UNIT"
        ;;
    *)
        printf 'ERROR: %s missing from %s and no mask to remove\n' "$UNIT" "$IMG" >&2
        exit 1
        ;;
    esac
    ;;
*)
    echo "usage: toggle-desktop-session.sh on|off" >&2
    exit 2
    ;;
esac

sync_manifest_hash
