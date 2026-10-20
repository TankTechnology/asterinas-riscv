#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPOSITORY_ROOT="$(cd -- "$SCRIPT_DIR/../../../.." && pwd -P)"
SOURCE_ROOT=""
DSC_PATH=""
OUTPUT_DIR=""
BUILDER_IMAGE="asterinas/debian-riscv-builder@sha256:d53fb72d643698efb412985650161fc5deff60eba30c2457e173dca9ce9f1acd"
MIN_FREE_BYTES=12884901888

die() {
    printf 'FIREFOX_ESR_BUILD FAIL reason=%s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Usage: build_firefox_esr.sh --source-root DIR --dsc FILE --output-dir DIR
    [--builder-image IMAGE@sha256:DIGEST]

The source root and .dsc must already be downloaded and verified. The builder
image must be immutable and contain all Debian Firefox Build-Depends. This
driver never downloads packages and never modifies the source root.
EOF
}

while (($# > 0)); do
    case "$1" in
        --source-root)
            (($# >= 2)) || die "--source-root requires a value"
            SOURCE_ROOT="$2"
            shift 2
            ;;
        --dsc)
            (($# >= 2)) || die "--dsc requires a value"
            DSC_PATH="$2"
            shift 2
            ;;
        --output-dir)
            (($# >= 2)) || die "--output-dir requires a value"
            OUTPUT_DIR="$2"
            shift 2
            ;;
        --builder-image)
            (($# >= 2)) || die "--builder-image requires a value"
            BUILDER_IMAGE="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            die "unknown argument: $1"
            ;;
    esac
done

[[ -n "$SOURCE_ROOT" && -n "$DSC_PATH" && -n "$OUTPUT_DIR" ]] || {
    usage >&2
    die "source root, dsc, and output directory are required"
}
[[ "$BUILDER_IMAGE" == *@sha256:* ]] ||
    die "builder image must use an immutable digest"
[[ -d "$SOURCE_ROOT" && ! -L "$SOURCE_ROOT" ]] ||
    die "source root must be an existing non-symlink directory"
[[ -f "$DSC_PATH" && ! -L "$DSC_PATH" ]] ||
    die "dsc must be an existing non-symlink file"
mkdir -p -- "$OUTPUT_DIR"
[[ -d "$OUTPUT_DIR" && ! -L "$OUTPUT_DIR" ]] ||
    die "output directory must be a non-symlink directory"

MANIFEST="$SCRIPT_DIR/source_package_manifest.json"
PATCH_MANIFEST="$SCRIPT_DIR/source_patch_manifest.json"
for input in "$MANIFEST" "$PATCH_MANIFEST" \
    "$SCRIPT_DIR/0001-recognize-virgl-webgl-renderer.patch" \
    "$SCRIPT_DIR/0002-prefer-drm-render-node-for-webrender.patch"; do
    [[ -f "$input" && ! -L "$input" ]] || die "missing compatibility input: $input"
done

PYTHONPATH="$REPOSITORY_ROOT" python3 "$SCRIPT_DIR/source_package_gate.py" \
    --manifest "$MANIFEST" --dsc "$DSC_PATH"
PYTHONPATH="$REPOSITORY_ROOT" python3 "$SCRIPT_DIR/source_patch_gate.py" \
    --source-root "$SOURCE_ROOT"

free_bytes="$(df --block-size=1 --output=avail -- "$OUTPUT_DIR" | tail -n 1)"
free_bytes="${free_bytes//[[:space:]]/}"
[[ "$free_bytes" =~ ^[0-9]+$ ]] || die "cannot determine free space"
((free_bytes >= MIN_FREE_BYTES)) ||
    die "insufficient free space: ${free_bytes} bytes, need ${MIN_FREE_BYTES}"

command -v docker >/dev/null 2>&1 || die "docker is unavailable"
docker image inspect "$BUILDER_IMAGE" >/dev/null 2>&1 ||
    die "builder image is not available locally: $BUILDER_IMAGE"

# Keep the large disposable source copy and package output on the same
# filesystem whose free-space floor was checked above.  Using ${TMPDIR} here
# can silently put several GiB on the host root filesystem even when the
# caller deliberately selected a spacious output volume.
work_dir="$(mktemp -d "$OUTPUT_DIR/.firefox-esr-build.XXXXXX")"
cleanup() {
    rm -rf -- "$work_dir"
}
trap cleanup EXIT
mkdir -p -- "$work_dir/out"

docker run --rm --network=none --entrypoint /bin/bash \
    --mount "type=bind,src=$SOURCE_ROOT,dst=/input,readonly" \
    --mount "type=bind,src=$SCRIPT_DIR,dst=/compat,readonly" \
    --mount "type=bind,src=$work_dir,dst=/work" \
    "$BUILDER_IMAGE" -lc '
set -euo pipefail
cp -a /input /work/source
cd /work/source
patch --batch --forward --fuzz=0 -p1 < /compat/0001-recognize-virgl-webgl-renderer.patch
patch --batch --forward --fuzz=0 -p1 < /compat/0002-prefer-drm-render-node-for-webrender.patch
if ! dpkg-checkbuilddeps -a riscv64 -B debian/control; then
    printf "%s\\n" \
        "FIREFOX_ESR_BUILD FAIL reason=target dependency or Rust target check failed" \
        >&2
    exit 1
fi
if ! printf "%s\\n" "pub fn target_gate() {}" \
    | rustc --target=riscv64gc-unknown-linux-gnu --crate-type=lib \
        --emit=metadata -o /tmp/asterinas-firefox-riscv64-target.rmeta -; then
    printf "%s\\n" \
        "FIREFOX_ESR_BUILD FAIL reason=target dependency or Rust target check failed" \
        >&2
    exit 1
fi
dpkg-buildpackage -B -a riscv64 -us -uc
shopt -s nullglob
packages=(/work/firefox-esr_*_riscv64.deb)
(( ${#packages[@]} == 1 ))
cp -- "${packages[0]}" /work/out/
'

mapfile -t packages < <(find "$work_dir/out" -maxdepth 1 -type f \
    -name 'firefox-esr_*_riscv64.deb' -print)
(( ${#packages[@]} == 1 )) || die "builder did not produce exactly one Firefox package"
package_path="${packages[0]}"
final_package="$OUTPUT_DIR/$(basename -- "$package_path")"
cp -- "$package_path" "$final_package"
provenance="$final_package.provenance.json"

python3 - "$final_package" "$provenance" "$PATCH_MANIFEST" "$MANIFEST" \
    "$DSC_PATH" "$BUILDER_IMAGE" <<'PY'
import hashlib
import json
import subprocess
import sys
from pathlib import Path

package, provenance, patch_manifest, source_manifest, dsc, builder = map(Path, sys.argv[1:])

def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()

fields = {}
result = subprocess.run(
    ["dpkg-deb", "--field", str(package), "Package", "Architecture", "Version"],
    check=True, capture_output=True, text=True,
)
for line in result.stdout.splitlines():
    name, _, value = line.partition(":")
    fields[name] = value.strip()
manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
document = {
    "schema_version": 1,
    "package": {
        "name": fields["Package"],
        "architecture": fields["Architecture"],
        "version": fields["Version"],
        "sha256": digest(package),
    },
    "source_manifest": {
        "path": source_manifest.name,
        "sha256": digest(source_manifest),
    },
    "patches": manifest["patches"],
    "build": {
        "source_dsc_sha256": digest(dsc),
        "builder_image_digest": str(builder).split("@", 1)[1],
    },
}
provenance.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

PYTHONPATH="$REPOSITORY_ROOT" python3 "$SCRIPT_DIR/package_provenance_gate.py" \
    --package "$final_package" --provenance "$provenance" --compat-root "$SCRIPT_DIR"
printf 'FIREFOX_ESR_BUILD PASS package=%s provenance=%s\n' \
    "$(basename -- "$final_package")" "$(basename -- "$provenance")"
