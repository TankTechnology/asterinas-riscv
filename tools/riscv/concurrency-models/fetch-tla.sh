#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Fetch TLC (`tla2tools.jar`) into target/tla/ for `run.py`.
#
# The jar comes from the tlaplus release; the upstream asset is replaced in
# place, so this prints the checksum it got instead of pinning one. Requires a
# Java runtime (11+) to run TLC.

set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
out_dir="$repo/target/tla"
url="https://github.com/tlaplus/tlaplus/releases/download/v1.8.0/tla2tools.jar"

mkdir -p "$out_dir"
tmp="$out_dir/tla2tools.jar.part"

echo "fetching $url"
if command -v curl >/dev/null 2>&1; then
    curl -fsSL -o "$tmp" "$url"
else
    wget -q -O "$tmp" "$url"
fi
mv "$tmp" "$out_dir/tla2tools.jar"

echo "wrote $out_dir/tla2tools.jar"
if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$out_dir/tla2tools.jar"
fi

if command -v java >/dev/null 2>&1; then
    java -cp "$out_dir/tla2tools.jar" tlc2.TLC -help 2>&1 | sed -n '2,4p' || true
else
    echo "warning: no java found; TLC needs a JRE to run" >&2
fi
