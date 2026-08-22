#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Assemble a minimal Debian riscv64 rootfs for the DRM-M19 virgl EGL demo.
#
# Alpine's Mesa does not build the virgl gallium driver on any arch, so the
# EGL demo uses Debian trixie riscv64 instead, whose mesa-libgallium ships
# virgl. This script resolves the runtime dependencies of the seed packages
# against the Debian Packages index, downloads the .debs, and extracts them
# into target/m19/rootfs/.
#
# Usage:
#     bash fetch_debian_rootfs.sh
#
# Everything is contained in target/m19/ (downloads, index, rootfs).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
WORK="${REPO_ROOT}/target/m19"
ROOTFS="${WORK}/rootfs"
DEBS="${WORK}/debs"
INDEX="${WORK}/Packages"
MIRROR="https://deb.debian.org/debian"

SEEDS="${SEEDS:-kmscube libegl1 libgbm1 libegl-mesa0 libgl1-mesa-dri mesa-libgallium libgles2 busybox-static}"

mkdir -p "${ROOTFS}" "${DEBS}"

if [[ ! -f "${INDEX}" ]]; then
    echo "==> Downloading Packages index"
    curl -sfL "${MIRROR}/dists/trixie/main/binary-riscv64/Packages.xz" \
        -o "${INDEX}.xz"
    xz -d "${INDEX}.xz"
fi

# package_field <name> <field>: print the field value from the stanza.
package_field() {
    awk -v pkg="$1" -v field="$2" '
        $0 == "Package: " pkg { found = 1 }
        found && $0 ~ "^" field ": " { sub("^" field ": ", ""); print; exit }
        found && /^$/ { exit }
    ' "${INDEX}"
}

# provides_map: build "virtual -> real" mapping once.
PROVIDES_MAP="${WORK}/provides.map"
if [[ ! -f "${PROVIDES_MAP}" ]]; then
    echo "==> Building provides map"
    awk '
        /^Package: / { pkg = substr($0, 10) }
        /^Provides: / {
            line = substr($0, 11)
            n = split(line, parts, ",")
            for (i = 1; i <= n; i++) {
                gsub(/^\s+|\s+$/, "", parts[i])
                sub(/ .*/, "", parts[i])
                sub(/ \(.*$/, "", parts[i])
                print parts[i] " " pkg
            }
        }
    ' "${INDEX}" | sort -u > "${PROVIDES_MAP}"
fi

resolve_virtual() {
    # Returns the real package name for a possibly-virtual package.
    local name="$1"
    if grep -q "^Package: ${name}\$" "${INDEX}"; then
        echo "$name"
    else
        awk -v v="$1" '$1 == v { print $2; exit }' "${PROVIDES_MAP}"
    fi
}

declare -A DONE
QUEUE=()
for seed in ${SEEDS}; do QUEUE+=("$seed"); done

while ((${#QUEUE[@]})); do
    pkg="${QUEUE[0]}"
    QUEUE=("${QUEUE[@]:1}")

    real="$(resolve_virtual "$pkg")"
    if [[ -z "${real}" ]]; then
        echo "    WARN: cannot resolve dependency '$pkg' (virtual, skipping)"
        continue
    fi
    [[ -n "${DONE[$real]:-}" ]] && continue
    DONE[$real]=1

    filename="$(package_field "$real" Filename)"
    if [[ -z "${filename}" ]]; then
        echo "    WARN: no Filename for $real, skipping"
        continue
    fi

    echo "==> $real"
    deb="${DEBS}/$(basename "$filename")"
    if [[ ! -f "$deb" ]]; then
        curl -sfL "${MIRROR}/${filename}" -o "$deb"
    fi
    (cd "${ROOTFS}" && ar x "$deb" data.tar.xz && tar -xJf data.tar.xz && rm -f data.tar.xz)

    # Queue dependencies (first alternative, stripped of version/arch tags).
    deps="$(package_field "$real" Depends || true)"
    deps="${deps//:any/}"
    IFS=',' read -ra dep_list <<< "${deps}"
    for dep in "${dep_list[@]}"; do
        dep="${dep%%|*}"
        dep="${dep%% (*}"
        dep="${dep%% [*}"
        dep="$(echo "$dep" | xargs)"
        [[ -n "$dep" && -z "${DONE[$dep]:-}" ]] && QUEUE+=("$dep")
    done
done

echo "==> Done. Rootfs at ${ROOTFS}"
du -sh "${ROOTFS}"
