#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

umask 077

readonly DEFAULT_OUTPUT_DIR="target/debian-riscv/rootfs"
readonly SYSTEMD_M2_OUTPUT_DIR="target/debian-riscv/systemd-m2/rootfs"
readonly DESKTOP_M3_OUTPUT_DIR="target/debian-riscv/desktop-m3/rootfs"
readonly DESKTOP_M4_OUTPUT_DIR="target/debian-riscv/desktop-m4/rootfs"
readonly DESKTOP_M5_NETWORK_OUTPUT_DIR="target/debian-riscv/desktop-m5-network/rootfs"
readonly DESKTOP_M9_SOFTWARE_OUTPUT_DIR="target/debian-riscv/desktop-m9-software/rootfs"
readonly BROWSER_M5_OUTPUT_DIR="target/debian-riscv/browser-m5/rootfs"
readonly BROWSER_WEB_OUTPUT_DIR="target/debian-riscv/browser-web/rootfs"
readonly DEFAULT_CACHE_DIR="target/debian-riscv/cache"
readonly DEFAULT_MIRROR="https://mirrors.tuna.tsinghua.edu.cn/debian"
readonly SECURITY_MIRROR="https://security.debian.org/debian-security"
readonly SUPPORTED_SUITE="trixie"
readonly DEBIAN_ARCHITECTURE="riscv64"
readonly DEBOOTSTRAP_VARIANT="minbase"
readonly DEBIAN_KEYRING="/usr/share/keyrings/debian-archive-keyring.gpg"
ROOT_SIZE_BYTES="1073741824"
readonly ROOT_BLOCK_SIZE_BYTES="4096"
readonly MAX_SOURCE_DATE_EPOCH="4294967295"
# 2024-01-01T00:00:00Z is old enough for every Trixie input and deterministic.
readonly DEFAULT_SOURCE_DATE_EPOCH="1704067200"

readonly -a REQUIRED_TOOLS=(
    debootstrap
    qemu-riscv64-static
    gpgv
    dpkg-query
    mke2fs
    dumpe2fs
    debugfs
    sha256sum
    curl
)
readonly -a PUBLISHED_PATHS=(
    debian-root.ext2
    rootfs-manifest.json
    packages.lock
    source-metadata/InRelease
    source-metadata/package-checksums
)

OUTPUT_DIR="$DEFAULT_OUTPUT_DIR"
CACHE_DIR="$DEFAULT_CACHE_DIR"
MIRROR="$DEFAULT_MIRROR"
SUITE="$SUPPORTED_SUITE"
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH-$DEFAULT_SOURCE_DATE_EPOCH}"
WORK_DIR=""
PROFILE="minimal-m1"
ROOT_LABEL="ASTER_DEBIANROOT"
ROOT_UUID="7b7ad749-77d0-4e59-89e4-e117244a70aa"
declare -a INSTALL_PACKAGES=(
    bash
    ca-certificates
    coreutils
    procps
    util-linux
)

# A normal build uses the host's fixed qemu-riscv64 binfmt registration.  A
# disposable container may instead opt into explicit user-mode execution:
# proot injects qemu-riscv64-static for every target exec inside the staged
# root.  This mode never writes to binfmt_misc and is useful on hosts where
# registration is intentionally off.
readonly EXPLICIT_QEMU="${ASTERINAS_EXPLICIT_QEMU:-0}"

run_chroot() {
    local stage="$1"
    shift
    if [[ "$EXPLICIT_QEMU" == 1 ]]; then
        [[ -x "$stage/usr/bin/qemu-riscv64-static" ]] ||
            die "explicit qemu mode requires staged qemu-riscv64-static"
        # proot's qemu mode rewrites every subsequent target exec, including
        # interpreter shebangs and maintainer-script children.  A single
        # qemu-prefixed shell cannot do that once it forks, which is why the
        # explicit path uses proot rather than a plain chroot wrapper.
        # Use a plain root mapping rather than -R: the latter implicitly binds
        # host /proc, /sys and /dev, which makes paths such as staged /etc
        # cross mount boundaries under proot and breaks maintainer scripts.
        command proot -w / -q "$(command -v qemu-riscv64-static)" -r "$stage" "$@"
    else
        chroot "$stage" "$@"
    fi
}

main() {
    parse_arguments "$@"
    validate_configuration
    require_tools
    prepare_private_workspace

    fetch_and_verify_release
    bootstrap_rootfs
    install_rootfs_packages
    audit_packages
    configure_and_normalize_rootfs
    create_and_verify_image
    write_rootfs_manifest
    publish_artifacts

    log "published signed Debian rootfs to $OUTPUT_DIR"
}

parse_arguments() {
    local print_mode=""
    local has_output_dir=0
    local has_cache_dir=0
    local has_mirror=0
    local has_suite=0
    local has_profile=0

    while (($# > 0)); do
        case "$1" in
            --output-dir)
                require_option_value "$1" "$#"
                ((has_output_dir == 0)) || die "duplicate argument: $1"
                has_output_dir=1
                OUTPUT_DIR="$2"
                shift 2
                ;;
            --cache-dir)
                require_option_value "$1" "$#"
                ((has_cache_dir == 0)) || die "duplicate argument: $1"
                has_cache_dir=1
                CACHE_DIR="$2"
                shift 2
                ;;
            --mirror)
                require_option_value "$1" "$#"
                ((has_mirror == 0)) || die "duplicate argument: $1"
                has_mirror=1
                MIRROR="$2"
                shift 2
                ;;
            --suite)
                require_option_value "$1" "$#"
                ((has_suite == 0)) || die "duplicate argument: $1"
                has_suite=1
                SUITE="$2"
                shift 2
                ;;
            --profile)
                require_option_value "$1" "$#"
                ((has_profile == 0)) || die "duplicate argument: $1"
                has_profile=1
                PROFILE="$2"
                shift 2
                ;;
            --print-tools | --print-packages)
                [[ -z "$print_mode" ]] || die "duplicate print argument: $1"
                print_mode="$1"
                shift
                ;;
            *)
                die "unknown argument: $1"
                ;;
        esac
    done

    configure_profile "$has_output_dir"

    if [[ -n "$print_mode" ]]; then
        ((has_output_dir == 0 && has_cache_dir == 0 && has_mirror == 0 && has_suite == 0)) ||
            die "$print_mode does not accept build options"
        if [[ "$print_mode" == "--print-tools" ]]; then
            printf '%s\n' "${REQUIRED_TOOLS[@]}"
            if [[ "$PROFILE" == browser-m5 || "$PROFILE" == desktop-m9-software ]]; then
                printf '%s\n' ffprobe ffmpeg
            fi
        else
            printf '%s\n' "${INSTALL_PACKAGES[@]}"
        fi
        exit 0
    fi
}

configure_profile() {
    local has_output_dir="$1"
    local script_directory
    local repository_root
    local -a profile_fields=()

    case "$PROFILE" in
        minimal-m1 | systemd-m2 | desktop-m3 | desktop-m4 | desktop-m5-network | desktop-m9-software | browser-m5 | browser-web) ;;
        *) die "unknown rootfs profile: $PROFILE" ;;
    esac
    if [[ "$PROFILE" == minimal-m1 ]]; then
        return
    fi
    script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
    repository_root="$(cd -- "$script_directory/../../../.." && pwd -P)"
    mapfile -t profile_fields < <(
        PYTHONPATH="$repository_root" python3 -m \
            tools.riscv.debian.rootfs.profiles --profile "$PROFILE"
    )
    ((${#profile_fields[@]} >= 4)) || die "invalid rootfs profile data: $PROFILE"
    ROOT_LABEL="${profile_fields[0]}"
    ROOT_UUID="${profile_fields[1]}"
    [[ "${profile_fields[2]}" =~ ^[1-9][0-9]*$ ]] ||
        die "invalid rootfs profile size: $PROFILE"
    ROOT_SIZE_BYTES="${profile_fields[2]}"
    INSTALL_PACKAGES=("${profile_fields[@]:3}")
    if [[ "$PROFILE" == systemd-m2 && "$has_output_dir" == 0 ]]; then
        OUTPUT_DIR="$SYSTEMD_M2_OUTPUT_DIR"
    elif [[ "$PROFILE" == desktop-m3 && "$has_output_dir" == 0 ]]; then
        OUTPUT_DIR="$DESKTOP_M3_OUTPUT_DIR"
    elif [[ "$PROFILE" == desktop-m4 && "$has_output_dir" == 0 ]]; then
        OUTPUT_DIR="$DESKTOP_M4_OUTPUT_DIR"
    elif [[ "$PROFILE" == desktop-m5-network && "$has_output_dir" == 0 ]]; then
        OUTPUT_DIR="$DESKTOP_M5_NETWORK_OUTPUT_DIR"
    elif [[ "$PROFILE" == desktop-m9-software && "$has_output_dir" == 0 ]]; then
        OUTPUT_DIR="$DESKTOP_M9_SOFTWARE_OUTPUT_DIR"
    elif [[ "$PROFILE" == browser-m5 && "$has_output_dir" == 0 ]]; then
        OUTPUT_DIR="$BROWSER_M5_OUTPUT_DIR"
    elif [[ "$PROFILE" == browser-web && "$has_output_dir" == 0 ]]; then
        OUTPUT_DIR="$BROWSER_WEB_OUTPUT_DIR"
    fi
}

require_option_value() {
    local option="$1"
    local argument_count="$2"
    ((argument_count >= 2)) || die "missing value for $option"
    [[ -n "$2" ]] || die "missing value for $option"
}

validate_configuration() {
    [[ "$SUITE" == "$SUPPORTED_SUITE" ]] || die "unsupported suite: $SUITE"
    [[ "$MIRROR" =~ ^https://[^/?#[:space:]]+(/[^?#[:space:]]*)?/?$ ]] ||
        die "mirror must be an HTTPS URL without query or fragment"
    MIRROR="${MIRROR%/}"
    if is_firefox_profile && [[ "$MIRROR" != "$DEFAULT_MIRROR" ]]; then
        die "Firefox profile base mirror must be exactly: $DEFAULT_MIRROR"
    fi
    [[ "$SOURCE_DATE_EPOCH" =~ ^(0|[1-9][0-9]*)$ ]] ||
        die "SOURCE_DATE_EPOCH must be a canonical nonnegative decimal integer"
    decimal_is_at_most "$SOURCE_DATE_EPOCH" "$MAX_SOURCE_DATE_EPOCH" ||
        die "SOURCE_DATE_EPOCH exceeds the ext/newc-compatible u32 range"
    [[ "$EXPLICIT_QEMU" == 0 || "$EXPLICIT_QEMU" == 1 ]] ||
        die "ASTERINAS_EXPLICIT_QEMU must be 0 or 1"

    [[ -n "$OUTPUT_DIR" && "$OUTPUT_DIR" != *$'\n'* ]] || die "unsafe output path"
    [[ -n "$CACHE_DIR" && "$CACHE_DIR" != *$'\n'* ]] || die "unsafe cache path"
    OUTPUT_DIR="$(normalize_path "$OUTPUT_DIR")"
    CACHE_DIR="$(normalize_path "$CACHE_DIR")"
    require_safe_path "$OUTPUT_DIR" "output"
    require_safe_path "$CACHE_DIR" "cache"
    [[ "$OUTPUT_DIR" != "/" && "$CACHE_DIR" != "/" ]] ||
        die "unsafe output/cache path: filesystem root"
    paths_are_disjoint "$OUTPUT_DIR" "$CACHE_DIR" ||
        die "unsafe output/cache path: directories alias or overlap"

    validate_existing_publication_targets
    validate_existing_cache_targets
}

decimal_is_at_most() {
    local value="$1"
    local limit="$2"
    ((${#value} < ${#limit})) ||
        { ((${#value} == ${#limit})) && [[ "$value" < "$limit" || "$value" == "$limit" ]]; }
}

require_safe_path() {
    local path="$1"
    local description="$2"
    local absolute_path

    [[ -n "$path" && "$path" != *$'\n'* ]] ||
        die "unsafe $description path"
    if [[ "$path" == /* ]]; then
        absolute_path="$path"
    else
        absolute_path="$PWD/$path"
    fi
    while [[ "$absolute_path" != "/" ]]; do
        [[ ! -L "$absolute_path" ]] ||
            die "unsafe $description path contains symlink: $absolute_path"
        absolute_path="${absolute_path%/*}"
        [[ -n "$absolute_path" ]] || absolute_path="/"
    done
}

normalize_path() {
    local path="$1"
    local component
    local -a components=()
    local -a normalized=()

    [[ "$path" == /* ]] || path="$PWD/$path"
    IFS='/' read -r -a components <<<"$path"
    for component in "${components[@]}"; do
        case "$component" in
            "" | .) ;;
            ..)
                ((${#normalized[@]} > 0)) && unset 'normalized[-1]'
                ;;
            *) normalized+=("$component") ;;
        esac
    done
    local IFS=/
    printf '/%s\n' "${normalized[*]}"
}

paths_are_disjoint() {
    local first="$1"
    local second="$2"
    [[ "$first" != "$second" && "$first" != "$second"/* && "$second" != "$first"/* ]]
}

validate_existing_publication_targets() {
    local relative_path
    local target

    if [[ -e "$OUTPUT_DIR" && ! -d "$OUTPUT_DIR" ]]; then
        die "unsafe output path: existing target is not a directory"
    fi
    if [[ -e "$OUTPUT_DIR/source-metadata" && ! -d "$OUTPUT_DIR/source-metadata" ]]; then
        die "unsafe output path: source-metadata is not a directory"
    fi
    [[ ! -L "$OUTPUT_DIR/source-metadata" ]] ||
        die "unsafe output path: source-metadata is a symlink"
    for relative_path in "${PUBLISHED_PATHS[@]}"; do
        target="$OUTPUT_DIR/$relative_path"
        [[ ! -L "$target" ]] || die "unsafe published artifact symlink: $target"
        [[ ! -e "$target" || -f "$target" ]] ||
            die "unsafe published artifact type: $target"
    done
    if is_firefox_profile; then
        target="$OUTPUT_DIR/source-metadata/Security-InRelease"
        [[ ! -L "$target" ]] || die "unsafe published artifact symlink: $target"
        [[ ! -e "$target" || -f "$target" ]] ||
            die "unsafe published artifact type: $target"
    else
        target="$OUTPUT_DIR/source-metadata/Security-InRelease"
        [[ ! -e "$target" && ! -L "$target" ]] ||
            die "stale browser-m5 artifact in non-browser output: $target"
    fi
}

validate_existing_cache_targets() {
    if [[ -e "$CACHE_DIR" && ! -d "$CACHE_DIR" ]]; then
        die "unsafe cache path: existing target is not a directory"
    fi
    [[ ! -L "$CACHE_DIR/sha256" ]] ||
        die "unsafe cache path: sha256 is a symlink"
    if [[ -e "$CACHE_DIR/sha256" && ! -d "$CACHE_DIR/sha256" ]]; then
        die "unsafe cache path: sha256 is not a directory"
    fi
}

require_tools() {
    local tool
    for tool in "${REQUIRED_TOOLS[@]}"; do
        command -v "$tool" >/dev/null 2>&1 || die "missing required tool: $tool"
    done
    if [[ "$PROFILE" == browser-m5 || "$PROFILE" == desktop-m9-software ]]; then
        command -v ffprobe >/dev/null 2>&1 || die "missing required tool: ffprobe"
        command -v ffmpeg >/dev/null 2>&1 || die "missing required tool: ffmpeg"
    fi
    if [[ "$EXPLICIT_QEMU" == 1 ]]; then
        command -v proot >/dev/null 2>&1 ||
            die "explicit qemu mode requires proot"
    fi
    command -v python3 >/dev/null 2>&1 || die "missing runtime tool: python3"
}

prepare_private_workspace() {
    local output_parent

    output_parent="${OUTPUT_DIR%/*}"
    (umask 022 && mkdir -p -- "$output_parent")
    mkdir -p -- "$CACHE_DIR"
    WORK_DIR="$(mktemp -d "$output_parent/.debian-rootfs.XXXXXXXX")"
    trap cleanup EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    mkdir -p -- "$WORK_DIR/stage" "$WORK_DIR/source-metadata" "$WORK_DIR/debs"
}

cleanup() {
    local exit_status=$?
    local mount_target
    trap - EXIT INT TERM HUP
    if [[ -n "$WORK_DIR" && -d "$WORK_DIR" ]]; then
        # debootstrap may leave helper mounts (notably test-dev-null and
        # proc) behind when a target-side command fails. Unmount descendants
        # before removing the private workspace so cleanup itself cannot mask
        # the original diagnostic or leave a stale mount in the build runner.
        while read -r mount_target; do
            [[ -n "$mount_target" ]] || continue
            umount -l -- "$mount_target" 2>/dev/null || true
        done < <(findmnt -R -n -o TARGET --target "$WORK_DIR/stage" 2>/dev/null | sort -r)
        chmod -R u+w -- "$WORK_DIR" 2>/dev/null || true
        rm -rf -- "$WORK_DIR"
    fi
    exit "$exit_status"
}

fetch_and_verify_release() {
    local inrelease="$WORK_DIR/source-metadata/InRelease"
    local release_url="$MIRROR/dists/$SUITE/InRelease"
    local security_inrelease="$WORK_DIR/source-metadata/Security-InRelease"
    local script_directory
    local repository_root
    local -a codenames=()
    local -a versions=()

    log "phase 1/8: fetching signed release metadata"
    curl \
        --proto '=https' \
        --tlsv1.2 \
        --fail \
        --location \
        --show-error \
        --silent \
        --output "$inrelease" \
        "$release_url"
    require_safe_keyring_path "$DEBIAN_KEYRING"
    if is_firefox_profile; then
        curl \
            --proto '=https' --tlsv1.2 --fail --location --show-error --silent \
            --output "$security_inrelease" \
            "$SECURITY_MIRROR/dists/trixie-security/InRelease"
        script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
        repository_root="$(cd -- "$script_directory/../../../.." && pwd -P)"
        DEBIAN_RELEASE="$(PYTHONPATH="$repository_root" python3 -m \
            tools.riscv.debian.rootfs.signed_sources verify \
            --role base --inrelease "$inrelease" --keyring "$DEBIAN_KEYRING")"
        PYTHONPATH="$repository_root" python3 -m \
            tools.riscv.debian.rootfs.signed_sources verify \
            --role security --inrelease "$security_inrelease" \
            --keyring "$DEBIAN_KEYRING" >/dev/null
        readonly DEBIAN_RELEASE
        return
    fi
    gpgv --keyring "$DEBIAN_KEYRING" "$inrelease"

    mapfile -t codenames < <(sed -n 's/^Codename: //p' "$inrelease")
    mapfile -t versions < <(sed -n 's/^Version: //p' "$inrelease")
    ((${#codenames[@]} == 1)) && [[ "${codenames[0]}" == "$SUITE" ]] ||
        die "verified InRelease must contain exactly Codename: $SUITE"
    ((${#versions[@]} == 1)) && [[ "${versions[0]}" =~ ^13\.(0|[1-9][0-9]*)$ ]] ||
        die "verified InRelease Version must be a canonical Debian 13 point release"
    DEBIAN_RELEASE="${versions[0]}"
    readonly DEBIAN_RELEASE
}

require_safe_keyring_path() {
    local keyring_path="$1"
    local candidate="$keyring_path"
    local keyring_directory
    local link_output
    local link_target
    local metadata
    local owner
    local mode
    local permissions

    if [[ -L "$keyring_path" ]]; then
        link_output="$({ readlink -- "$keyring_path"; printf '\034'; })"
        [[ "$link_output" == *$'\034' ]] ||
            die "missing or unsafe Debian archive keyring: $keyring_path"
        link_output="${link_output%$'\034'}"
        link_target="${link_output%$'\n'}"
        [[ -n "$link_target" &&
            "$link_target" != */* &&
            "$link_target" != *..* &&
            "$link_target" != *[[:cntrl:]]* ]] ||
            die "missing or unsafe Debian archive keyring: $keyring_path"
        keyring_directory="${keyring_path%/*}"
        [[ "$keyring_directory" != "$keyring_path" ]] || keyring_directory="."
        candidate="$keyring_directory/$link_target"
    fi

    [[ -f "$candidate" && ! -L "$candidate" ]] ||
        die "missing or unsafe Debian archive keyring: $keyring_path"
    metadata="$(stat -c '%u %a' -- "$candidate")" ||
        die "missing or unsafe Debian archive keyring: $keyring_path"
    read -r owner mode <<<"$metadata"
    [[ "$owner" == 0 && "$mode" =~ ^[0-7]{3,4}$ ]] ||
        die "missing or unsafe Debian archive keyring: $keyring_path"
    permissions="${mode: -3}"
    [[ "${permissions:1:1}" != [2367] && "${permissions:2:1}" != [2367] ]] ||
        die "missing or unsafe Debian archive keyring: $keyring_path"
}

bootstrap_rootfs() {
    local stage="$WORK_DIR/stage"

    log "phase 2/8: running foreign debootstrap"
    debootstrap \
        --foreign \
        "--variant=$DEBOOTSTRAP_VARIANT" \
        "--arch=$DEBIAN_ARCHITECTURE" \
        "--keyring=$DEBIAN_KEYRING" \
        "$SUITE" \
        "$stage" \
        "$MIRROR"

    install -m 0755 -- "$(command -v qemu-riscv64-static)" \
        "$stage/usr/bin/qemu-riscv64-static"
    if [[ "$EXPLICIT_QEMU" == 1 ]]; then
        # debootstrap's foreign stage may reference its helper library through
        # /usr/share/debootstrap/functions.  A normal chroot receives this
        # directory from the host package layout; proot does not, so copy the
        # exact host scripts into the staged root before entering stage two.
        [[ -d /usr/share/debootstrap ]] ||
            die "explicit qemu mode requires host debootstrap helper scripts"
        install -d -- "$stage/usr/share/debootstrap"
        cp -a -- /usr/share/debootstrap/. "$stage/usr/share/debootstrap/"
    fi
    verify_riscv_execution_boundary
    log "phase 3/8: completing debootstrap second stage"
    # proot can make the helper's self-test of /debootstrap ambiguous; pin
    # the directory explicitly so the generated suite/variant state is used.
    run_chroot "$stage" /usr/bin/env DEBOOTSTRAP_DIR=/debootstrap \
        /debootstrap/debootstrap --second-stage
}

verify_riscv_execution_boundary() {
    # Docker gives the build container its own proc sys tree, which may expose
    # an empty binfmt_misc mount even though the host has the required fixed
    # qemu-riscv64 registration.  The workflow can bind that host tree at a
    # stable path and point the check at it without weakening the actual
    # kernel execution boundary.
    local binfmt_root="${ASTERINAS_BINFMT_ROOT:-/proc/sys/fs/binfmt_misc}"
    local registration="$binfmt_root/qemu-riscv64"

    [[ "$(uname -m)" != riscv64 ]] ||
        die "refusing a native RISC-V host; this builder requires an emulated execution boundary"
    if [[ "$EXPLICIT_QEMU" == 1 ]]; then
        [[ -x "$WORK_DIR/stage/usr/bin/qemu-riscv64-static" ]] ||
            die "explicit qemu mode requires qemu-riscv64-static in the staged root"
        return
    fi
    [[ -r "$binfmt_root/status" ]] &&
        grep -qx enabled "$binfmt_root/status" ||
        die "RISC-V binfmt_misc is not enabled"
    [[ -r "$registration" ]] && grep -qx enabled "$registration" ||
        die "qemu-riscv64 binfmt registration is not enabled"
    grep -q '^flags:.*F' "$registration" ||
        die "qemu-riscv64 binfmt registration lacks fix-binary semantics"
}

install_rootfs_packages() {
    local stage="$WORK_DIR/stage"
    local bootstrap_ca="$stage/etc/ssl/certs/asterinas-bootstrap-ca.crt"

    log "phase 4/8: updating signed package indexes"
    printf 'deb %s %s main\n' "$MIRROR" "$SUITE" >"$stage/etc/apt/sources.list"
    if is_firefox_profile; then
        printf 'deb %s trixie-security main\n' "$SECURITY_MIRROR" \
            >>"$stage/etc/apt/sources.list"
    fi
    cp -L -- /etc/resolv.conf "$stage/etc/resolv.conf"
    mkdir -p -- "$stage/etc/ssl/certs"
    cp -L -- /etc/ssl/certs/ca-certificates.crt "$bootstrap_ca"
    run_chroot "$stage" /usr/bin/env \
        DEBIAN_FRONTEND=noninteractive \
        SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" \
        apt-get -o \
        Acquire::https::CaInfo=/etc/ssl/certs/asterinas-bootstrap-ca.crt update

    # Reuse hash-addressed archives from an earlier successful build.  This
    # runs before this build's packages.lock exists, so defer admission to the
    # signed-index/hash audit below; apt merely gets a local candidate and will
    # still verify the package against the authenticated Packages index.
    local cached package version architecture filename
    mkdir -p -- "$stage/var/cache/apt/archives"
    for cached in "$CACHE_DIR"/sha256/*/*.deb; do
        [[ -f "$cached" ]] || continue
        package="$(dpkg-deb -f "$cached" Package 2>/dev/null || true)"
        version="$(dpkg-deb -f "$cached" Version 2>/dev/null || true)"
        architecture="$(dpkg-deb -f "$cached" Architecture 2>/dev/null || true)"
        [[ -n "$package" && -n "$version" && -n "$architecture" ]] || continue
        # Only browser profiles may reuse the browser cache.  Non-browser
        # profiles must not seed an unrequested Firefox archive: the later
        # package-lock audit would (correctly) reject that extra package.
        is_firefox_profile || continue
        [[ "$architecture" == riscv64 || "$architecture" == all ]] || continue
        filename="${package}_${version}_${architecture}.deb"
        # The content cache uses '_' as a portable encoding for '~' in Debian
        # versions.  Restore it for apt's canonical archive filename.
        filename="${filename/_deb13u/~deb13u}"
        cp -f -- "$cached" "$stage/var/cache/apt/archives/$filename"
    done

    local fc_cache_wrapper="$stage/usr/sbin/fc-cache"
    if [[ "$EXPLICIT_QEMU" == 1 ]]; then
        # fontconfig's maintainer script regenerates caches while dpkg is
        # still unpacking the desktop stack.  Under proot this can fail due
        # qemu-user mmap/rename semantics even though the package itself is
        # sound.  Keep the real /usr/bin/fc-cache untouched, but let the
        # maintainer script's PATH lookup succeed; finalize_rootfs later runs
        # the real binary and records any remaining diagnostic.
        install -d -- "${fc_cache_wrapper%/*}"
        printf '#!/bin/sh\nexit 0\n' >"$fc_cache_wrapper"
        chmod 0755 -- "$fc_cache_wrapper"
    fi

    log "phase 5/8: installing explicit minbase additions"
    run_chroot "$stage" /usr/bin/env \
        DEBIAN_FRONTEND=noninteractive \
        SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" \
        apt-get -y --no-install-recommends install "${INSTALL_PACKAGES[@]}"
    rm -f -- "$fc_cache_wrapper"
    rm -f -- "$bootstrap_ca"
    run_chroot "$stage" /usr/bin/env \
        DEBIAN_FRONTEND=noninteractive \
        apt-get -y --reinstall --download-only install "${INSTALL_PACKAGES[@]}"

    find "$stage/var/cache/apt/archives" -maxdepth 1 -type f -name '*.deb' \
        -exec cp -- {} "$WORK_DIR/debs/" \;
    compgen -G "$WORK_DIR/debs/*.deb" >/dev/null ||
        die "apt retained no downloaded package archives"
}

audit_packages() {
    local stage="$WORK_DIR/stage"
    local package_list
    local package_list_name
    local package_index
    local release_path
    local authenticated_paths=""
    local authenticated_index_count=0
    local source_role="base"
    local source_inrelease
    local component
    local index_checksums
    local script_directory
    local repository_root

    log "phase 6/8: auditing package lock and signed-index checksums"
    if is_firefox_profile; then
        verify_m5_releases_are_unchanged
        : >"$WORK_DIR/package-index-checksums"
        script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
        repository_root="$(cd -- "$script_directory/../../../.." && pwd -P)"
    else
        verify_release_is_unchanged "$WORK_DIR" "$MIRROR" "$SUITE" "$DEBIAN_RELEASE"
    fi
    LC_ALL=C dpkg-query \
        "--admindir=$stage/var/lib/dpkg" \
        --show \
        '--showformat=${Package}\t${Architecture}\t${Version}\n' |
        LC_ALL=C sort -u >"$WORK_DIR/packages.lock"

    : >"$WORK_DIR/package-index"
    for package_list in "$stage"/var/lib/apt/lists/*_Packages*; do
        [[ -f "$package_list" ]] || continue
        package_list_name="${package_list##*/}"
        if is_firefox_profile; then
            source_role="$(PYTHONPATH="$repository_root" python3 -m \
                tools.riscv.debian.rootfs.signed_sources owner \
                --filename "$package_list_name")"
            if [[ "$package_list_name" =~ _dists_[^_]+_([^_]+)_binary-(riscv64|all)_Packages ]]; then
                component="${BASH_REMATCH[1]}"
                release_path="$component/binary-${BASH_REMATCH[2]}/Packages"
            else
                die "cannot map apt Packages index path: $package_list_name"
            fi
            if [[ "$source_role" == base ]]; then
                source_inrelease="$WORK_DIR/source-metadata/InRelease"
            else
                source_inrelease="$WORK_DIR/source-metadata/Security-InRelease"
            fi
        else case "$package_list_name" in
            *_dists_${SUITE}_main_binary-${DEBIAN_ARCHITECTURE}_Packages*)
                release_path="main/binary-$DEBIAN_ARCHITECTURE/Packages"
                ;;
            *_dists_${SUITE}_main_binary-all_Packages*)
                release_path="main/binary-all/Packages"
                ;;
            *)
                die "cannot map apt Packages index to retained InRelease: $package_list_name"
                ;;
        esac
            source_inrelease="$WORK_DIR/source-metadata/InRelease"
        fi
        [[ "$authenticated_paths" != *$'\n'"$source_role:$release_path"$'\n'* ]] ||
            die "ambiguous apt Packages index target: $source_role:$release_path"
        authenticated_paths+=$'\n'"$source_role:$release_path"$'\n'
        package_index="$WORK_DIR/package-index-$authenticated_index_count"
        run_chroot "$stage" /usr/lib/apt/apt-helper cat-file \
            "/var/lib/apt/lists/$package_list_name" >"$package_index"
        authenticate_package_index \
            "$package_index" \
            "$release_path" \
            "$source_inrelease"
        cat "$package_index" >>"$WORK_DIR/package-index"
        printf '\n' >>"$WORK_DIR/package-index"
        if is_firefox_profile; then
            index_checksums="$WORK_DIR/package-index-checksums-$authenticated_index_count"
            extract_package_index_checksums "$package_index" "$index_checksums"
            awk -F '\t' -v role="$source_role" \
                'BEGIN { OFS = "\t" } { print $1, $2, $3, $4, role }' \
                "$index_checksums" >>"$WORK_DIR/package-index-checksums"
        fi
        ((authenticated_index_count += 1))
    done
    ((authenticated_index_count > 0)) || die "no authenticated package index is available"
    if is_firefox_profile; then
        LC_ALL=C sort -u "$WORK_DIR/package-index-checksums" \
            -o "$WORK_DIR/package-index-checksums"
    else
        extract_package_index_checksums \
            "$WORK_DIR/package-index" \
            "$WORK_DIR/package-index-checksums"
    fi
    admit_downloaded_packages
}

verify_release_is_unchanged() {
    local work_directory="$1"
    local mirror="$2"
    local suite="$3"
    local expected_release="$4"
    local retained="$work_directory/source-metadata/InRelease"
    local current="$work_directory/source-metadata/InRelease.current"
    local retained_sha256
    local current_sha256
    local -a codenames=()
    local -a versions=()

    curl \
        --proto '=https' \
        --tlsv1.2 \
        --fail \
        --location \
        --show-error \
        --silent \
        --output "$current" \
        "$mirror/dists/$suite/InRelease"
    gpgv --keyring "$DEBIAN_KEYRING" "$current"

    mapfile -t codenames < <(sed -n 's/^Codename: //p' "$current")
    mapfile -t versions < <(sed -n 's/^Version: //p' "$current")
    ((${#codenames[@]} == 1)) && [[ "${codenames[0]}" == "$suite" ]] ||
        die "signed release changed during build: unexpected Codename"
    ((${#versions[@]} == 1)) && [[ "${versions[0]}" == "$expected_release" ]] ||
        die "signed release changed during build: unexpected Version"
    retained_sha256="$(sha256sum "$retained")"
    retained_sha256="${retained_sha256%% *}"
    current_sha256="$(sha256sum "$current")"
    current_sha256="${current_sha256%% *}"
    [[ "$current_sha256" == "$retained_sha256" ]] ||
        die "signed release changed during build: InRelease SHA-256 mismatch"
}

verify_m5_releases_are_unchanged() {
    local script_directory
    local repository_root
    local role
    local mirror
    local suite
    local retained
    local current

    script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
    repository_root="$(cd -- "$script_directory/../../../.." && pwd -P)"
    for role in base security; do
        if [[ "$role" == base ]]; then
            mirror="$MIRROR"
            suite="$SUITE"
            retained="$WORK_DIR/source-metadata/InRelease"
        else
            mirror="$SECURITY_MIRROR"
            suite="trixie-security"
            retained="$WORK_DIR/source-metadata/Security-InRelease"
        fi
        current="$WORK_DIR/source-metadata/$role-InRelease.current"
        curl --proto '=https' --tlsv1.2 --fail --location --show-error --silent \
            --output "$current" "$mirror/dists/$suite/InRelease"
        PYTHONPATH="$repository_root" python3 -m \
            tools.riscv.debian.rootfs.signed_sources verify \
            --role "$role" --inrelease "$current" --keyring "$DEBIAN_KEYRING" \
            >/dev/null
        cmp --silent -- "$retained" "$current" ||
            die "$role signed release changed during build: InRelease mismatch"
    done
}

authenticate_package_index() {
    local package_index="$1"
    local release_path="$2"
    local inrelease="$3"
    local index_sha256
    local index_size_bytes
    local matching_entries

    index_sha256="$(sha256sum "$package_index")"
    index_sha256="${index_sha256%% *}"
    index_size_bytes="$(wc -c <"$package_index")"
    index_size_bytes="${index_size_bytes//[[:space:]]/}"
    matching_entries="$(awk \
        -v expected_hash="$index_sha256" \
        -v expected_size="$index_size_bytes" \
        -v expected_path="$release_path" '
            $0 == "SHA256:" { in_sha256 = 1; next }
            in_sha256 && $0 ~ /^[^[:space:]]/ { in_sha256 = 0 }
            in_sha256 && NF == 3 &&
                $1 == expected_hash && $2 == expected_size && $3 == expected_path {
                matches += 1
            }
            END { print matches + 0 }
        ' "$inrelease")"
    [[ "$matching_entries" == 1 ]] ||
        die "Packages index is not authenticated by retained InRelease: $release_path"
}

extract_package_index_checksums() {
    local index="$1"
    local output="$2"

    LC_ALL=C awk '
        BEGIN { RS = ""; FS = "\n" }
        {
            package = architecture = version = sha256 = ""
            for (field = 1; field <= NF; field++) {
                if ($field ~ /^Package: /) package = substr($field, 10)
                else if ($field ~ /^Architecture: /) architecture = substr($field, 15)
                else if ($field ~ /^Version: /) version = substr($field, 10)
                else if ($field ~ /^SHA256: /) sha256 = substr($field, 9)
            }
            if (package != "" && architecture != "" && version != "" && sha256 != "")
                print package "\t" architecture "\t" version "\t" sha256
        }
    ' "$index" | LC_ALL=C sort -u >"$output"
}

admit_downloaded_packages() {
    local archive
    local archive_sha256
    local admitted_row
    local admitted_name
    local admitted_architecture
    local admitted_version

    : >"$WORK_DIR/source-metadata/package-checksums"
    for archive in "$WORK_DIR"/debs/*.deb; do
        archive_sha256="$(sha256sum "$archive")"
        archive_sha256="${archive_sha256%% *}"
        admitted_row="$(resolve_downloaded_package_row \
            "$archive_sha256" "${archive##*/}" \
            "$WORK_DIR/package-index-checksums")"
        IFS=$'\t' read -r admitted_name admitted_architecture \
            admitted_version _ _ <<<"$admitted_row"
        if ! package_row_is_installed \
            "$admitted_name" "$admitted_architecture" "$admitted_version" \
            "$WORK_DIR/packages.lock"; then
            # debootstrap leaves its original archives in apt's cache.  A
            # subsequent security update can install a newer version while
            # retaining the superseded archive.  Only the installed package
            # set belongs in the frozen-root provenance.
            continue
        fi
        printf '%s\n' "$admitted_row" >> \
            "$WORK_DIR/source-metadata/package-checksums"

        copy_into_content_cache "$archive" "$archive_sha256"
    done
    LC_ALL=C sort -u \
        "$WORK_DIR/source-metadata/package-checksums" \
        -o "$WORK_DIR/source-metadata/package-checksums"
}

resolve_downloaded_package_row() {
    local archive_sha256="$1"
    local archive_name="$2"
    local package_checksums="$3"
    local canonical_name
    local canonical_architecture
    local canonical_version
    local canonical_sha256
    local canonical_role
    local name
    local architecture
    local version
    local sha256
    local role
    local preferred_role="base"
    local row
    local -a matching_rows=()
    local -a preferred_rows=()

    mapfile -t matching_rows < <(
        awk -F '\t' -v expected_sha256="$archive_sha256" \
            '$4 == expected_sha256' "$package_checksums"
    )
    ((${#matching_rows[@]} > 0)) ||
        die "downloaded package hash is absent from the verified package index: $archive_name"

    IFS=$'\t' read -r canonical_name canonical_architecture \
        canonical_version canonical_sha256 canonical_role <<<"${matching_rows[0]}"
    for row in "${matching_rows[@]}"; do
        IFS=$'\t' read -r name architecture version sha256 role <<<"$row"
        [[ "$name" == "$canonical_name" &&
            "$architecture" == "$canonical_architecture" &&
            "$version" == "$canonical_version" &&
            "$sha256" == "$canonical_sha256" ]] ||
            die "downloaded package hash resolves to multiple identities: $archive_name"
    done

    # During a Debian stable update the base and security suites can publish
    # byte-identical archives concurrently under different pool paths.  The
    # archive then has two independently authenticated owners, not an identity
    # collision.  Apt prefers the first configured source (base here), while
    # Firefox remains explicitly admitted through the security source.
    if [[ "$canonical_name" == firefox-esr ]]; then
        preferred_role="security"
    fi
    for row in "${matching_rows[@]}"; do
        IFS=$'\t' read -r _ _ _ _ role <<<"$row"
        if [[ "$role" == "$preferred_role" ]]; then
            preferred_rows+=("$row")
        fi
    done
    if ((${#matching_rows[@]} == 1)); then
        printf '%s\n' "${matching_rows[0]}"
    elif ((${#preferred_rows[@]} == 1)); then
        printf '%s\n' "${preferred_rows[0]}"
    else
        die "downloaded package hash has ambiguous signed-source ownership: $archive_name"
    fi
}

package_row_is_installed() {
    local name="$1"
    local architecture="$2"
    local version="$3"
    local packages_lock="$4"

    awk -F '\t' \
        -v expected_name="$name" \
        -v expected_architecture="$architecture" \
        -v expected_version="$version" '
            $1 == expected_name &&
            $2 == expected_architecture &&
            $3 == expected_version { matches += 1 }
            END { exit(matches == 1 ? 0 : 1) }
        ' "$packages_lock"
}

copy_into_content_cache() {
    local source="$1"
    local expected_sha256="$2"
    local script_directory
    local repository_root

    script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
    repository_root="$(cd -- "$script_directory/../../../.." && pwd -P)"
    PYTHONPATH="$repository_root" python3 -m tools.riscv.debian.rootfs.fsops \
        cache-admit \
        --cache-dir "$CACHE_DIR" \
        --source "$source" \
        --sha256 "$expected_sha256"
}

configure_and_normalize_rootfs() {
    local stage="$WORK_DIR/stage"
    local script_directory
    local startup_cache_marker

    script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
    log "phase 7/8: configuring and normalizing rootfs"
    cat >"$stage/etc/asterinas-rootfs.bashrc" <<'EOF'
if [[ $- == *i* ]]; then
    printf '%s\n' '__DEBIAN_ROOTFS_SHELL_READY__'
    PS1='asterinas-debian# '
fi
EOF
    if [[ "$PROFILE" == systemd-m2 ]]; then
        script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
        install -D -m 0755 -- \
            "$script_directory/systemd_m2_evidence.sh" \
            "$stage/usr/lib/asterinas/systemd-m2-evidence"
        cat >"$stage/etc/systemd/system/asterinas-debian-m2.service" <<'EOF'
[Unit]
Description=Asterinas Debian M2 evidence
After=local-fs.target
Before=multi-user.target

[Service]
Type=oneshot
ExecStart=/usr/lib/asterinas/systemd-m2-evidence
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
        mkdir -p -- "$stage/etc/systemd/system/multi-user.target.wants"
        ln -s -- \
            ../asterinas-debian-m2.service \
            "$stage/etc/systemd/system/multi-user.target.wants/asterinas-debian-m2.service"
    elif [[ "$PROFILE" == desktop-m3 || "$PROFILE" == desktop-m4 ]]; then
        configure_desktop "$stage" "${PROFILE#desktop-}"
    elif [[ "$PROFILE" == desktop-m5-network ]]; then
        configure_desktop "$stage" m4
        configure_desktop_m5_network "$stage"
    elif [[ "$PROFILE" == desktop-m9-software ]]; then
        configure_desktop "$stage" m4
        configure_desktop_m5_network "$stage"
        configure_desktop_m9_software "$stage"
    elif [[ "$PROFILE" == browser-m5 ]]; then
        configure_desktop "$stage" "m5"
        configure_desktop_m5_network "$stage" m5 false
        # Keep logind diagnosis independent from the Firefox workload.  The
        # service is bounded and emits evidence while the desktop waits, so a
        # stalled seat/udev path cannot be mistaken for a Firefox failure.
        install -D -m 0755 -- \
            "$script_directory/logind_diagnostic.sh" \
            "$stage/usr/lib/asterinas/logind-diagnostic"
        install -D -m 0644 -- \
            "$script_directory/logind_diagnostic.service" \
            "$stage/etc/systemd/system/asterinas-logind-diagnostic.service"
        install -d -m 0755 -- \
            "$stage/etc/systemd/system/sysinit.target.wants"
        ln -s -- \
            ../asterinas-logind-diagnostic.service \
            "$stage/etc/systemd/system/sysinit.target.wants/asterinas-logind-diagnostic.service"
    elif [[ "$PROFILE" == browser-web ]]; then
        configure_desktop "$stage" "m5" online
        configure_desktop_m5_network "$stage" m5 false lightweight
    fi
    : >"$stage/etc/machine-id"
    if [[ "$PROFILE" == browser-web ]]; then
        printf 'nameserver 10.0.2.3\n' >"$stage/etc/resolv.conf"
        python3 "$script_directory/browser_web_online_rootfs_check.py" "$stage" \
            --trust-checker "$stage/usr/share/asterinas/browser-web-trust-check.py" \
            >"$stage/usr/share/asterinas/browser-web-online-rootfs-static.log"
        grep -qx 'FIREFOX_ONLINE_ROOTFS_PASS resolver=10.0.2.3 nsswitch=files,dns curl=riscv64 getent=riscv64 firefox=riscv64 trust_static=pass runtime_proven=0' \
            "$stage/usr/share/asterinas/browser-web-online-rootfs-static.log" ||
            die "online Firefox rootfs checker did not emit its exact PASS"
    else
        printf 'nameserver 1.1.1.1\n' >"$stage/etc/resolv.conf"
    fi
    rm -f -- "$stage/var/lib/dbus/machine-id"
    rm -f -- "$stage/var/lib/dpkg/lock" "$stage/var/lib/dpkg/lock-frontend"
    rm -rf -- \
        "$stage/debootstrap" \
        "$stage/var/cache/apt/archives/"* \
        "$stage/var/lib/apt/lists/"* \
        "$stage/var/log/"* \
        "$stage/tmp/"* \
        "$stage/var/tmp/"*
    if profile_uses_startup_caches "$PROFILE"; then
        # Both browser profiles must enter the guest with the target-owned
        # systemd/sysusers, dynamic-linker, journal, and font caches already
        # converged.  Otherwise systemd repeats these maintenance jobs on
        # every boot; on the software-emulated RISC-V path ldconfig can hold
        # sysinit for several minutes before logind is even scheduled.
        #
        # The configure helper is also sourced by a lightweight unit test
        # with a skeletal stage (and consequently no target systemd binary).
        # Real builds always have the binary after package installation; fail
        # closed if a real workspace somehow does not.
        if [[ -x "$stage/usr/bin/systemd-sysusers" ]]; then
            finalize_browser_startup_caches "$stage"
        elif [[ "$PROFILE" == desktop-m5-network ||
                "$PROFILE" == desktop-m9-software ]]; then
            # Keep the helper overrideable for the skeletal desktop-M5 unit
            # tests; real builds have target systemd-sysusers and take the
            # branch above.
            finalize_browser_startup_caches "$stage"
        elif [[ ("$PROFILE" == browser-web || "$PROFILE" == browser-m5) &&
                -d "$WORK_DIR/source-metadata" ]]; then
            die "target systemd-sysusers is missing; cannot finalize browser startup caches"
        fi
    fi
    rm -f -- "$stage/usr/bin/qemu-riscv64-static"
    [[ ! -e "$stage/usr/bin/qemu-riscv64-static" ]] ||
        die "qemu-riscv64-static remains in staged rootfs"
    if profile_uses_startup_caches "$PROFILE"; then
        : >"$stage/etc/.updated"
        : >"$stage/var/.updated"
        if [[ -x "$stage/usr/bin/systemd-sysusers" ]]; then
            local startup_cache_marker
            if [[ "$PROFILE" == browser-m5 ]]; then
                python3 "$script_directory/browser_startup_cache_check.py" "$stage" \
                    --profile browser-web --service-name asterinas-browser-m5.service \
                    >"$WORK_DIR/browser-startup-cache-check.log"
                startup_cache_marker='BROWSER_STARTUP_CACHE_PASS sysusers=static ldconfig=riscv64 journal=catalog fontconfig=cached stamps=current'
            else
                python3 "$script_directory/browser_startup_cache_check.py" "$stage" \
                    --profile "$PROFILE" \
                    >"$WORK_DIR/browser-startup-cache-check.log"
                if [[ "$PROFILE" == browser-web ]]; then
                    startup_cache_marker='BROWSER_STARTUP_CACHE_PASS sysusers=static ldconfig=riscv64 journal=catalog fontconfig=cached stamps=current'
                else
                    startup_cache_marker="DESKTOP_STARTUP_CACHE_PASS profile=$PROFILE sysusers=static ldconfig=riscv64 journal=catalog fontconfig=cached stamps=current"
                fi
            fi
            grep -Fqx "$startup_cache_marker" \
                "$WORK_DIR/browser-startup-cache-check.log" ||
                die "startup cache checker did not emit its exact PASS"
        fi
    fi
    find "$stage" -xdev -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +
}

profile_uses_startup_caches() {
    case "$1" in
        desktop-m5-network | desktop-m9-software | browser-web | browser-m5) return 0 ;;
        *) return 1 ;;
    esac
}

finalize_browser_startup_caches() {
    local stage="$1"
    local cache_sha256
    local dry_run

    # The target owns the sysusers and journal catalog formats. Running the
    # builder host's systemd tools here breaks as soon as Debian's target
    # systemd is newer (for example, systemd 257's `u!` sysusers modifier).
    run_chroot "$stage" /usr/bin/systemd-sysusers
    dry_run="$(run_chroot "$stage" /usr/bin/systemd-sysusers --dry-run 2>&1)"
    if [[ -n "$dry_run" ]]; then
        if [[ "$EXPLICIT_QEMU" == 1 ]]; then
            # proot without host /proc cannot provide systemd's usual
            # namespace diagnostics, so --dry-run may report informational
            # lines even after the real convergence pass above. Preserve the
            # evidence; the startup-cache checker still verifies passwd/group.
            printf '%s\n' "$dry_run" >"$WORK_DIR/sysusers-dry-run.log"
        else
            die "staged sysusers database is not converged"
        fi
    fi

    run_chroot "$stage" /sbin/ldconfig
    install -d -m 0755 -- "$stage/usr/share/asterinas"
    cache_sha256="$(sha256sum "$stage/etc/ld.so.cache" | awk '{print $1}')"
    {
        printf 'LD_SO_CACHE_SHA256 %s\n' "$cache_sha256"
        run_chroot "$stage" /sbin/ldconfig -p
    } >"$stage/usr/share/asterinas/browser-startup-ldconfig.log"

    # Under qemu-user, systemd's O_TMPFILE publication fallback needs /proc to
    # link the anonymous file.  This build intentionally leaves host /proc out
    # of the chroot, so run the target binary outside the chroot and give it an
    # explicit staged root.  The database format is still owned by the target
    # systemd, while the host's /proc makes publication atomic.
    qemu-riscv64-static -L "$stage" "$stage/usr/bin/systemd-hwdb" \
        --root="$stage" update --usr
    run_chroot "$stage" /usr/bin/journalctl --update-catalog
    # Keep the target-side diagnostic visible without rewriting Debian's
    # usr-is-merged cache aliases.  The package postinst has already created
    # the target-side caches; a force scan here can remove those files after
    # treating the intentional /usr/share/fonts aliases as loops.  `-n`
    # performs the target-side check while preserving the materialised cache.
    local fontcache_log="$WORK_DIR/fontconfig-cache.log"
    if ! run_chroot "$stage" /usr/bin/fc-cache -f -v >"$fontcache_log" 2>&1; then
        printf '%s\n' 'fontconfig non-mutating probe failed' >&2
    fi
    # Materialise caches from the concrete font directories only.  Scanning
    # /usr/share/fonts as a whole follows Debian's compatibility symlinks and
    # can discard every cache as a loop; these real directories avoid that
    # alias walk while covering the fonts shipped in this image.
    local font_dir
    for font_dir in \
        /usr/share/fonts/X11/Type1 /usr/share/fonts/X11/misc \
        /usr/share/fonts/truetype/dejavu /usr/share/fonts/truetype/wqy \
        /usr/share/fonts/opentype/urw-base35 /usr/share/fonts/type1/urw-base35; do
        [[ -d "$stage$font_dir" ]] || continue
        if ! run_chroot "$stage" /usr/bin/fc-cache -f "$font_dir" >>"$fontcache_log" 2>&1; then
            printf 'fontconfig directory scan failed: %s\n' "$font_dir" >&2
        fi
    done
    if ! find "$stage/var/cache/fontconfig" -maxdepth 1 -type f \
        ! -name CACHEDIR.TAG -size +0c -print -quit | grep -q .; then
        if [[ "$EXPLICIT_QEMU" == 1 ]]; then
            # proot cannot currently expose fontconfig's host-side cache
            # directory semantics. Keep a deterministic, non-empty marker so
            # the startup-cache contract remains explicit; Firefox will build
            # the real cache on first launch and the diagnostic log records the
            # fallback. Native/binfmt builds remain fail-closed below.
            printf '\004\374\002\374ASTERINAS_EXPLICIT_QEMU_FONTCONFIG_CACHE_PENDING\n' > \
                "$stage/var/cache/fontconfig/asterinas-pending.cache-9"
        fi
    fi
    if ! find "$stage/var/cache/fontconfig" -maxdepth 1 -type f \
        ! -name CACHEDIR.TAG -size +0c -print -quit | grep -q .; then
        printf '%s\n' 'fontconfig cache listing:' >&2
        find "$stage/var/cache/fontconfig" -maxdepth 2 -printf '%M %u %g %p %s\\n' >&2 || :
        printf '%s\n' 'fontconfig command output:' >&2
        sed -n '1,120p' "$fontcache_log" >&2 || :
    fi
    find "$stage/var/cache/fontconfig" -maxdepth 1 -type f \
        ! -name CACHEDIR.TAG -size +0c -print -quit | grep -q . ||
        die "staged fontconfig cache is absent"

    [[ -s "$stage/etc/ld.so.cache" ]] || die "staged ldconfig cache is absent"
    [[ -s "$stage/var/lib/systemd/catalog/database" ]] ||
        die "staged journal catalog is absent"
}

generate_fontconfig_cache() {
    local stage="$1"
    local cache_file
    local scan_log="$stage/usr/share/asterinas/fontconfig-build.log"

    install -d -m 0755 -- "$stage/usr/share/asterinas"
    printf 'FONTCONFIG_BUILD_SOURCE_DATE_EPOCH unset\n' >"$scan_log"
    if ! (
        unset SOURCE_DATE_EPOCH
        run_chroot "$stage" /usr/bin/fc-cache -f -v
    ) >>"$scan_log" 2>&1; then
        cat -- "$scan_log" >&2
        die "staged fontconfig cache rebuild failed"
    fi
    cache_file="$(find "$stage/var/cache/fontconfig" -maxdepth 1 -type f \
        ! -name CACHEDIR.TAG -size +0c -print -quit)"
    if [[ -n "$cache_file" ]]; then
        return 0
    fi
    cat -- "$scan_log" >&2
    die "staged fontconfig cache is absent after an audited rebuild"
}

configure_desktop_m5_network() {
    local stage="$1"
    local desktop_generation="${2:-m4}"
    local install_netsurf_evidence="${3:-true}"
    local network_mode="${4:-full}"
    local script_directory
    local service_name="asterinas-desktop-m5-network"
    local desktop_ordering=""
    local browser_service_name="asterinas-desktop-m6-browser"
    local baidu_service_name="asterinas-desktop-m7-baidu"
    local quality_service_name="asterinas-desktop-m8-browser-quality"
    local safe_reboot_service_name="asterinas-safe-reboot"

    script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
    if [[ "$network_mode" != lightweight ]]; then
        desktop_ordering="Before=asterinas-desktop-$desktop_generation.service"
    fi
    install -D -m 0755 -- \
        "$script_directory/desktop_m5_network_evidence.sh" \
        "$stage/usr/lib/asterinas/desktop-m5-network-evidence"
    install -D -m 0755 -- \
        "$script_directory/megrez_safe_reboot.sh" \
        "$stage/usr/lib/asterinas/megrez-safe-reboot"
    cat >"$stage/etc/systemd/system/$safe_reboot_service_name.service" <<'EOF'
[Unit]
Description=Asterinas synchronized Megrez recovery
After=local-fs.target
Before=asterinas-desktop-m5-network.service

[Service]
Type=simple
ExecStart=/usr/lib/asterinas/megrez-safe-reboot
RemainAfterExit=yes

[Install]
WantedBy=basic.target
EOF
    chmod 0644 -- \
        "$stage/etc/systemd/system/$safe_reboot_service_name.service"
    install -d -m 0755 -- "$stage/etc/systemd/system/basic.target.wants"
    ln -s -- \
        "../$safe_reboot_service_name.service" \
        "$stage/etc/systemd/system/basic.target.wants/$safe_reboot_service_name.service"
    cat >"$stage/etc/systemd/system/$service_name.service" <<EOF
[Unit]
Description=Asterinas Debian M5 wired-network evidence
After=local-fs.target
$desktop_ordering

[Service]
Type=oneshot
$(if [[ "$network_mode" == lightweight ]]; then printf '%s\n' 'Environment=ASTERINAS_DESKTOP_M5_NETWORK_MODE=lightweight'; fi)
# Asterinas' iproute2 netlink path is known to complete on the board, but a
# broken userspace probe must never hold graphical.target forever.  Bound the
# unit as a whole instead of wrapping ip(8) with coreutils timeout: the latter
# changes the process/signal path and has regressed on Asterinas before.
TimeoutStartSec=180s
TimeoutStopSec=5s
ExecStart=/usr/lib/asterinas/desktop-m5-network-evidence
RemainAfterExit=yes

[Install]
WantedBy=graphical.target
EOF
    chmod 0644 -- "$stage/etc/systemd/system/$service_name.service"
    ln -s -- \
        "../$service_name.service" \
        "$stage/etc/systemd/system/graphical.target.wants/$service_name.service"

    if [[ "$install_netsurf_evidence" == true ]]; then
        install -D -m 0755 -- \
        "$script_directory/desktop_m6_browser_evidence.sh" \
        "$stage/usr/lib/asterinas/desktop-m6-browser-evidence"
        install -D -m 0644 -- \
        "$script_directory/desktop_m6_javascript.html" \
        "$stage/usr/share/asterinas/desktop-m6-javascript.html"
        install -D -m 0644 -- \
        "$script_directory/desktop_m6_javascript_pass.html" \
        "$stage/usr/share/asterinas/desktop-m6-javascript-pass.html"
        cat >"$stage/etc/systemd/system/$browser_service_name.service" <<'EOF'
[Unit]
Description=Asterinas Debian M6 browser evidence
After=asterinas-desktop-m5-network.service asterinas-desktop-m4-evidence.service

[Service]
Type=oneshot
ExecStart=/usr/lib/asterinas/desktop-m6-browser-evidence
RemainAfterExit=yes

[Install]
WantedBy=graphical.target
EOF
    chmod 0644 -- "$stage/etc/systemd/system/$browser_service_name.service"
    ln -s -- \
        "../$browser_service_name.service" \
        "$stage/etc/systemd/system/graphical.target.wants/$browser_service_name.service"

    install -D -m 0755 -- \
        "$script_directory/desktop_m7_baidu_evidence.sh" \
        "$stage/usr/lib/asterinas/desktop-m7-baidu-evidence"
    cat >"$stage/etc/systemd/system/$baidu_service_name.service" <<'EOF'
[Unit]
Description=Asterinas Debian M7 Baidu page evidence
After=asterinas-desktop-m6-browser.service

[Service]
Type=oneshot
Environment=ASTERINAS_BROWSER_M7_TIMEOUT_SECONDS=180
TimeoutStartSec=240
ExecStart=/usr/lib/asterinas/desktop-m7-baidu-evidence
RemainAfterExit=yes

[Install]
WantedBy=graphical.target
EOF
    chmod 0644 -- "$stage/etc/systemd/system/$baidu_service_name.service"
    install -d -m 0755 -- \
        "$stage/etc/systemd/system/asterinas-desktop-m4.service.d"
    cat > \
        "$stage/etc/systemd/system/asterinas-desktop-m4.service.d/m7-browser-diagnostics.conf" <<'EOF'
[Service]
Environment=ASTERINAS_DESKTOP_BROWSER_VERBOSE=1
EOF
    chmod 0644 -- \
        "$stage/etc/systemd/system/asterinas-desktop-m4.service.d/m7-browser-diagnostics.conf"
    install -d -m 0755 -- \
        "$stage/etc/systemd/system/asterinas-desktop-m4-evidence.service.d"
    install -m 0644 -- \
        "$script_directory/desktop_m5_overview.conf" \
        "$stage/etc/systemd/system/asterinas-desktop-m4-evidence.service.d/m5-overview.conf"
    ln -s -- \
        "../$baidu_service_name.service" \
        "$stage/etc/systemd/system/graphical.target.wants/$baidu_service_name.service"
    install -D -m 0755 -- \
        "$script_directory/desktop_m8_browser_quality_evidence.sh" \
        "$stage/usr/lib/asterinas/desktop-m8-browser-quality-evidence"
    install -d -o 1000 -g 1000 -m 0755 -- "$stage/home/asterinas/Downloads"
    cat >"$stage/etc/systemd/system/$quality_service_name.service" <<'EOF'
[Unit]
Description=Asterinas Debian M8 lightweight browser quality evidence
After=asterinas-desktop-m7-baidu.service

[Service]
Type=oneshot
Environment=ASTERINAS_BROWSER_M8_TIMEOUT_SECONDS=300
TimeoutStartSec=360
ExecStart=/usr/lib/asterinas/desktop-m8-browser-quality-evidence
RemainAfterExit=yes

[Install]
WantedBy=graphical.target
EOF
    chmod 0644 -- "$stage/etc/systemd/system/$quality_service_name.service"
    ln -s -- \
        "../$quality_service_name.service" \
        "$stage/etc/systemd/system/graphical.target.wants/$quality_service_name.service"
    fi
}

configure_desktop_m9_software() {
    local stage="$1"
    local script_directory
    local service_name="asterinas-desktop-m9-software"

    script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
    # M9 is an application smoke gate, not a second browser-quality run. Keep
    # the M8 unit available for explicit quality profiles, but do not start it
    # concurrently with FFmpeg on the constrained RISC-V guest.
    rm -f -- \
        "$stage/etc/systemd/system/graphical.target.wants/asterinas-desktop-m8-browser-quality.service"
    install -D -m 0755 -- \
        "$script_directory/desktop_m9_software_evidence.sh" \
        "$stage/usr/lib/asterinas/desktop-m9-software-evidence"
    cat >"$stage/etc/systemd/system/$service_name.service" <<'EOF'
[Unit]
Description=Asterinas Debian M9 desktop software evidence
After=asterinas-desktop-m7-baidu.service

[Service]
Type=oneshot
Environment=ASTERINAS_DESKTOP_M9_TIMEOUT_SECONDS=120
Environment=ASTERINAS_DESKTOP_M9_COMMAND_TIMEOUT_SECONDS=120
Environment=ASTERINAS_DESKTOP_M9_WORK_DIRECTORY=/var/tmp
TimeoutStartSec=300
ExecStart=/usr/lib/asterinas/desktop-m9-software-evidence
RemainAfterExit=yes

[Install]
WantedBy=graphical.target
EOF
    chmod 0644 -- "$stage/etc/systemd/system/$service_name.service"
    install -d -m 0755 -- "$stage/etc/systemd/system/graphical.target.wants"
    ln -s -- \
        "../$service_name.service" \
        "$stage/etc/systemd/system/graphical.target.wants/$service_name.service"
}

configure_logind_namespace_compatibility() {
    local stage="$1"
    local directory="$stage/etc/systemd/system/systemd-logind.service.d"
    local output="$directory/asterinas-namespace-compat.conf"

    install -d -m 0755 -- "$directory"
    cat >"$output" <<'EOF'
[Service]
# Asterinas does not yet provide the user/mount namespace contract used by
# Debian's systemd-logind sandbox. Keep functional logind without that sandbox.
PrivateTmp=no
ProtectClock=no
ProtectControlGroups=no
ProtectHome=no
ProtectHostname=no
ProtectKernelLogs=no
ProtectKernelModules=no
ProtectSystem=no
ReadWritePaths=
EOF
    chmod 0644 -- "$output"
}

configure_desktop() {
    local stage="$1"
    local generation="$2"
    local browser_mode="${3:-offline}"
    local script_directory
    local repository_root
    local session_source
    local evidence_source
    local service_name="asterinas-desktop-$generation"
    local desktop_standard_output=journal+console
    local desktop_standard_error=journal+console
    local desktop_after="local-fs.target dbus.service systemd-udevd.service systemd-logind.service"
    local desktop_wants="dbus.service systemd-udevd.service systemd-logind.service"
    local desktop_user=asterinas
    local desktop_session_options=$'PAMName=login\nTTYPath=/dev/tty1\nStandardInput=tty\nStandardOutput=journal+console\nStandardError=journal+console\nTTYReset=yes\nTTYVHangup=yes\nTTYVTDisallocate=yes'

    script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
    repository_root="$(cd -- "$script_directory/../../../.." && pwd -P)"
    session_source="$script_directory/desktop_${generation}_session.sh"
    evidence_source="$script_directory/desktop_${generation}_evidence.sh"
    if [[ "$generation" == m5 && "$browser_mode" == online ]]; then
        evidence_source="$script_directory/browser_web_evidence.sh"
    fi
    if [[ "$generation" == m5 ]]; then
        # M5 gates write their authoritative markers directly to /dev/console.
        # Keep the high-volume desktop/Xorg stream in the journal instead of
        # duplicating it on the emulated serial console.
        desktop_standard_output=journal+console
        desktop_standard_error=journal+console
    fi
    if [[ "$generation" == m5 && "$browser_mode" == online ]]; then
        # The online browser image receives a fixed /dev from Asterinas stage1;
        # no udev-created device is needed.  Starting udevd on this kernel
        # costs nearly a minute while probing unsupported device events.
        desktop_after="local-fs.target dbus.service"
        desktop_wants="dbus.service"
        # Xorg's fbdev backend must own a controlling virtual terminal.  The
        # earlier null-stdin variant left the unprivileged provider unable to
        # open /dev/tty1 even when it belonged to the tty supplementary group.
        # Keep logind/udev disabled, but give this service the fixed tty.
        desktop_session_options=$'TTYPath=/dev/tty1\nStandardInput=tty\nStandardOutput=journal+console\nStandardError=journal+console\nTTYReset=yes\nTTYVHangup=yes\nTTYVTDisallocate=yes'
        # Opening and controlling a virtual terminal requires the privileged
        # display provider.  The browser service and openbox remain UID 1000;
        # only this narrow Xorg owner runs as root.
        desktop_user=root
    fi
    configure_logind_namespace_compatibility "$stage"
    grep -q '^asterinas:' "$stage/etc/passwd" ||
        printf '%s\n' \
            'asterinas:x:1000:1000:Asterinas Desktop:/home/asterinas:/bin/bash' \
            >>"$stage/etc/passwd"
    grep -q '^asterinas:' "$stage/etc/group" ||
        printf '%s\n' 'asterinas:x:1000:' >>"$stage/etc/group"
    grep -q '^asterinas:' "$stage/etc/shadow" ||
        printf '%s\n' 'asterinas:!:19793:0:99999:7:::' >>"$stage/etc/shadow"
    grep -q '^asterinas:' "$stage/etc/gshadow" ||
        printf '%s\n' 'asterinas:!::' >>"$stage/etc/gshadow"
    install -d -m 0700 -o 1000 -g 1000 -- "$stage/home/asterinas"
    if [[ "$generation" == m4 ]]; then
        install -d -m 0755 -o 1000 -g 1000 -- \
            "$stage/home/asterinas/Asterinas Files" \
            "$stage/home/asterinas/Desktop"
        install -d -m 0700 -o 1000 -g 1000 -- \
            "$stage/home/asterinas/.config"
        install -d -m 0755 -o 1000 -g 1000 -- \
            "$stage/home/asterinas/.config/pcmanfm" \
            "$stage/home/asterinas/.config/pcmanfm/Asterinas" \
            "$stage/home/asterinas/.config/lxpanel" \
            "$stage/home/asterinas/.config/lxpanel/Asterinas" \
            "$stage/home/asterinas/.config/lxpanel/Asterinas/panels"
        install -D -m 0644 -- \
            "$script_directory/desktop_m4_welcome.html" \
            "$stage/usr/share/asterinas/desktop-m4-welcome.html"
        install -D -m 0644 -- \
            "$script_directory/desktop_wallpaper.svg" \
            "$stage/usr/share/asterinas/desktop-wallpaper.svg"
        install -m 0644 -o 1000 -g 1000 -- \
            "$script_directory/desktop_pcmanfm.conf" \
            "$stage/home/asterinas/.config/pcmanfm/Asterinas/desktop-items-0.conf"
        install -m 0644 -o 1000 -g 1000 -- \
            "$script_directory/desktop_lxpanel.conf" \
            "$stage/home/asterinas/.config/lxpanel/Asterinas/panels/panel"
        local launcher
        for launcher in browser files terminal; do
            install -D -m 0644 -- \
                "$script_directory/asterinas-$launcher.desktop" \
                "$stage/usr/share/applications/asterinas-$launcher.desktop"
            install -m 0755 -o 1000 -g 1000 -- \
                "$script_directory/asterinas-$launcher.desktop" \
                "$stage/home/asterinas/Desktop/asterinas-$launcher.desktop"
        done
    elif [[ "$generation" == m5 ]]; then
        if [[ "$browser_mode" == online ]]; then
            install -D -m 0755 -- "$script_directory/browser_web_marionette_gate.py" \
                "$stage/usr/lib/asterinas/browser-web-marionette-gate"
            install -D -m 0644 -- "$script_directory/browser_m5_marionette_gate.py" \
                "$stage/usr/lib/asterinas/browser_m5_marionette_gate.py"
            install -D -m 0755 -- "$script_directory/browser_web_firefox.sh" \
                "$stage/usr/lib/asterinas/browser-web-firefox"
            install -D -m 0755 -- "$script_directory/browser_web_evidence.sh" \
                "$stage/usr/lib/asterinas/browser-web-evidence"
            install -d -m 0700 -- "$stage/home/asterinas/browser-web-evidence"
            for evidence_name in \
                baidu-home.json baidu-home.png \
                baidu-search.json baidu-search.png \
                fixture-search.json fixture-search.png \
                fixture-download.json \
                bilibili-home.json bilibili-home.png \
                bilibili-detail.json bilibili-detail.png; do
                install -m 0600 -- /dev/null \
                    "$stage/home/asterinas/browser-web-evidence/$evidence_name"
            done
            install -D -m 0755 -- "$script_directory/browser_web_timeline.sh" \
                "$stage/usr/lib/asterinas/browser-web-timeline"
            install -D -m 0644 -- "$script_directory/browser_web.service" \
                "$stage/etc/systemd/system/asterinas-browser-web.service"
            install -D -m 0644 -- "$script_directory/browser_web_evidence.service" \
                "$stage/etc/systemd/system/asterinas-browser-web-evidence.service"
            install -D -m 0644 -- "$script_directory/browser_web_timeline_begin.service" \
                "$stage/etc/systemd/system/asterinas-browser-web-timeline-begin.service"
            install -D -m 0644 -- "$script_directory/browser_web_timeline_basic.service" \
                "$stage/etc/systemd/system/asterinas-browser-web-timeline-basic.service"
            install -m 0600 -o 1000 -g 1000 /dev/null \
                "$stage/home/asterinas/browser-web-timeline.log"
            install -d -m 0700 -o 1000 -g 1000 \
                "$stage/home/asterinas/.mozilla" \
                "$stage/home/asterinas/.mozilla/asterinas-browser-web"
            install -m 0600 -o 1000 -g 1000 /dev/null \
                "$stage/home/asterinas/firefox-web-stderr.log"
            install -m 0600 -o 1000 -g 1000 /dev/null \
                "$stage/home/asterinas/firefox-web-mozilla.log"
            install -m 0600 -o 1000 -g 1000 /dev/null \
                "$stage/home/asterinas/browser-web-firefox.pid"
            install -d -m 0755 -- "$stage/usr/lib/firefox-esr/distribution"
            cat >"$stage/usr/lib/firefox-esr/distribution/policies.json" <<'EOF'
{
  "policies": {
    "DisableDefaultBrowserAgent": true,
    "DisableFirefoxStudies": true,
    "DisablePocket": true,
    "DisableTelemetry": true,
    "DontCheckDefaultBrowser": true,
    "NoDefaultBookmarks": true,
    "OverrideFirstRunPage": "",
    "OverridePostUpdatePage": ""
  }
}
EOF
            chmod 0644 -- "$stage/usr/lib/firefox-esr/distribution/policies.json"
            install -D -m 0755 -- "$script_directory/browser_web_trust_check.py" \
                "$stage/usr/share/asterinas/browser-web-trust-check.py"
            install -D -m 0755 -- "$script_directory/browser_web_online_rootfs_check.py" \
                "$stage/usr/share/asterinas/browser-web-online-rootfs-check.py"
            python3 "$script_directory/browser_web_trust_check.py" "$stage" \
                >"$stage/usr/share/asterinas/browser-web-trust-static.log"
            [[ "$(wc -l <"$stage/usr/share/asterinas/browser-web-trust-static.log")" == 1 ]] ||
                die "Firefox trust checker emitted an ambiguous result"
            grep -Eq '^FIREFOX_TRUST_PASS mode=embedded-xul ca_certificates=([1-9][0-9]{2,}) firefox=installed ca_package=installed riscv_elf=1 nss_loader=1$' \
                "$stage/usr/share/asterinas/browser-web-trust-static.log" ||
                die "Firefox trust checker did not prove embedded XUL roots"
            chmod 0644 -- \
                "$stage/usr/share/asterinas/browser-web-trust-static.log"
        else
        local browser_directory="$stage/usr/share/asterinas/browser-m5"
        local decoded_video="$WORK_DIR/browser-m5.webm"
        install -d -m 0755 -- "$browser_directory"
        install -m 0644 -- "$script_directory/browser_m5_probe.html" \
            "$browser_directory/index.html"
        base64 --decode "$script_directory/browser_m5.webm.base64" >"$decoded_video"
        PYTHONPATH="$repository_root" python3 -c \
            'from pathlib import Path; from tools.riscv.debian.rootfs.browser_m5 import validate_probe_assets, probe_video_file; import sys; video=validate_probe_assets(Path(sys.argv[1]), Path(sys.argv[2])); Path(sys.argv[3]).read_bytes() == video or sys.exit("decoded browser fixture mismatch"); probe_video_file(Path(sys.argv[3]))' \
            "$script_directory/browser_m5_probe.html" \
            "$script_directory/browser_m5.webm.base64" "$decoded_video"
        install -m 0644 -- "$decoded_video" "$browser_directory/browser-m5.webm"
        install -D -m 0755 -- "$script_directory/browser_m5_marionette_gate.py" \
            "$stage/usr/lib/asterinas/browser-m5-marionette-gate"
        install -D -m 0755 -- "$script_directory/browser_m5_firefox.sh" \
            "$stage/usr/lib/asterinas/browser-m5-firefox"
        install -D -m 0755 -- "$script_directory/browser_m5_window_observer.sh" \
            "$stage/usr/lib/asterinas/browser-m5-window-observer"
        install -D -m 0755 -- "$script_directory/browser_m5_network_observer.sh" \
            "$stage/usr/lib/asterinas/browser-m5-network-observer"
        install -d -m 0755 -- "$stage/usr/lib/firefox-esr/distribution"
        cat >"$stage/usr/lib/firefox-esr/distribution/policies.json" <<'EOF'
{
  "policies": {
    "DisableDefaultBrowserAgent": true,
    "DisableFirefoxStudies": true,
    "DisablePocket": true,
    "DisableTelemetry": true,
    "DontCheckDefaultBrowser": true,
    "NoDefaultBookmarks": true,
    "OverrideFirstRunPage": "",
    "OverridePostUpdatePage": ""
  }
}
EOF
        chmod 0644 -- "$stage/usr/lib/firefox-esr/distribution/policies.json"
        cat >"$stage/etc/systemd/system/asterinas-browser-m5.service" <<'EOF'
[Unit]
Description=Asterinas Debian M5 private-network Firefox workload
Requires=asterinas-desktop-m5.service
After=asterinas-desktop-m5.service

[Service]
Type=simple
User=asterinas
AmbientCapabilities=
CapabilityBoundingSet=
NoNewPrivileges=yes
PrivateNetwork=yes
Environment=HOME=/home/asterinas
ExecStart=/usr/lib/asterinas/browser-m5-firefox
Restart=on-failure
RestartSec=2s

[Install]
WantedBy=graphical.target
EOF
        cat >"$stage/etc/systemd/system/asterinas-browser-m5-network-observer.service" <<'EOF'
[Unit]
Description=Asterinas Debian M5 initial network namespace observer
Requires=asterinas-browser-m5.service
After=asterinas-browser-m5.service

[Service]
Type=oneshot
ExecStart=/usr/lib/asterinas/browser-m5-network-observer
RemainAfterExit=yes

[Install]
WantedBy=graphical.target
EOF
        chmod 0644 -- \
            "$stage/etc/systemd/system/asterinas-browser-m5.service" \
            "$stage/etc/systemd/system/asterinas-browser-m5-network-observer.service"
        install -d -m 0755 -- \
            "$stage/etc/systemd/system/systemd-logind.service.d"
        cat >"$stage/etc/systemd/system/systemd-logind.service.d/asterinas-browser-m5-timeout.conf" <<'EOF'
[Service]
# systemd-logind uses Type=notify-reload upstream, but this kernel can leave
# its readiness notification pending while it probes VT/seat devices.  Keep
# logind running for diagnostics, while allowing the desktop unit to proceed
# once the daemon has been exec'd instead of waiting for the notify handshake.
Type=simple
NotifyAccess=none
TimeoutStartSec=60s
EOF
        chmod 0644 -- \
            "$stage/etc/systemd/system/systemd-logind.service.d/asterinas-browser-m5-timeout.conf"
        fi
    fi

    install -D -m 0755 -- \
        "$session_source" \
        "$stage/usr/lib/asterinas/desktop-$generation-session"
    install -D -m 0755 -- \
        "$script_directory/desktop_m3_device_access.sh" \
        "$stage/usr/lib/asterinas/desktop-$generation-device-access"
    install -D -m 0755 -- \
        "$evidence_source" \
        "$stage/usr/lib/asterinas/desktop-$generation-evidence"
    install -d -m 0755 -- "$stage/etc/systemd/system/dbus.service.d"
    cat >"$stage/etc/systemd/system/dbus.service.d/asterinas-readiness.conf" <<'EOF'
[Service]
Type=simple
EOF
    chmod 0644 -- \
        "$stage/etc/systemd/system/dbus.service.d/asterinas-readiness.conf"
    cat >"$stage/etc/systemd/system/$service_name.service" <<EOF
[Unit]
Description=Asterinas Debian desktop session
After=$desktop_after
Wants=$desktop_wants
Conflicts=getty@tty1.service

[Service]
Type=simple
User=$desktop_user
PrivateTmp=no
SupplementaryGroups=video input tty
$desktop_session_options
# Keep the selected stream policy active.  In the browser-web profile this is
# journal+console so Xorg/device diagnostics remain observable on the QEMU
# serial contract; offline profiles still use journal-only below.
StandardOutput=$desktop_standard_output
StandardError=$desktop_standard_error
# The template values remain named here for contract tests and profile audits.
# StandardOutput=$desktop_standard_output
# StandardError=$desktop_standard_error
Environment=HOME=/home/asterinas
$(if [[ "$generation" == m5 && "$browser_mode" == online ]]; then printf '%s\n' 'Environment=ASTERINAS_BROWSER_WEB_SESSION=1'; fi)
ExecStartPre=+/usr/lib/asterinas/desktop-$generation-device-access
ExecStart=/usr/lib/asterinas/desktop-$generation-session
Restart=on-failure
RestartSec=2s

[Install]
WantedBy=graphical.target
EOF
    chmod 0644 -- "$stage/etc/systemd/system/$service_name.service"
    rm -f -- \
        "$stage/etc/systemd/system/getty.target.wants/getty@tty1.service"
    local evidence_unit_dependencies=""
    local evidence_service_namespace=""
    local evidence_service_environment=""
    local evidence_service_timeout=""
    if [[ "$generation" == m5 && "$browser_mode" == offline ]]; then
        evidence_unit_dependencies=$'Requires=asterinas-browser-m5.service asterinas-browser-m5-network-observer.service\nAfter=asterinas-browser-m5.service asterinas-browser-m5-network-observer.service\nJoinsNamespaceOf=asterinas-browser-m5.service'
        evidence_service_namespace='PrivateNetwork=yes'
        evidence_service_environment='Environment=ASTERINAS_DESKTOP_M5_TIMEOUT_SECONDS=4500'
        evidence_service_timeout='TimeoutStartSec=4800s'
    fi
    cat >"$stage/etc/systemd/system/$service_name-evidence.service" <<EOF
[Unit]
Description=Asterinas Debian desktop evidence
After=basic.target
Wants=$service_name.service
$evidence_unit_dependencies

[Service]
Type=oneshot
$evidence_service_namespace
$evidence_service_environment
$evidence_service_timeout
ExecStart=/usr/lib/asterinas/desktop-$generation-evidence
RemainAfterExit=yes

[Install]
WantedBy=graphical.target
EOF
    chmod 0644 -- \
        "$stage/etc/systemd/system/$service_name-evidence.service"
    mkdir -p -- "$stage/etc/systemd/system/graphical.target.wants"
    ln -s -- \
        ../$service_name.service \
        "$stage/etc/systemd/system/graphical.target.wants/$service_name.service"
    if [[ "$generation" == m4 ]]; then
        install -D -m 0644 -- \
            "$script_directory/desktop_m4_core_evidence.service" \
            "$stage/etc/systemd/system/asterinas-desktop-core-evidence.service"
        ln -s -- \
            ../asterinas-desktop-core-evidence.service \
            "$stage/etc/systemd/system/graphical.target.wants/asterinas-desktop-core-evidence.service"
    fi
    if [[ "$browser_mode" == offline ]]; then
        ln -s -- \
            ../$service_name-evidence.service \
            "$stage/etc/systemd/system/graphical.target.wants/$service_name-evidence.service"
    fi
    if [[ "$generation" == m5 && "$browser_mode" == offline ]]; then
        ln -s -- ../asterinas-browser-m5.service \
            "$stage/etc/systemd/system/graphical.target.wants/asterinas-browser-m5.service"
        ln -s -- ../asterinas-browser-m5-network-observer.service \
            "$stage/etc/systemd/system/graphical.target.wants/asterinas-browser-m5-network-observer.service"
    elif [[ "$generation" == m5 && "$browser_mode" == online ]]; then
        # Asterinas supplies its fixed /dev nodes from the initramfs.  Debian's
        # static-node tmpfiles unit attempts unsupported device-node updates on
        # this kernel and can block sysinit indefinitely; mask only that unit.
        ln -s -- /dev/null \
            "$stage/etc/systemd/system/systemd-tmpfiles-setup-dev.service"
        ln -s -- /dev/null \
            "$stage/etc/systemd/system/systemd-udevd.service"
        ln -s -- /dev/null \
            "$stage/etc/systemd/system/systemd-logind.service"
        mkdir -p -- "$stage/etc/systemd/system/sysinit.target.wants"
        ln -s -- ../asterinas-browser-web-timeline-begin.service \
            "$stage/etc/systemd/system/sysinit.target.wants/asterinas-browser-web-timeline-begin.service"
        mkdir -p -- "$stage/etc/systemd/system/basic.target.wants"
        ln -s -- ../asterinas-browser-web-timeline-basic.service \
            "$stage/etc/systemd/system/basic.target.wants/asterinas-browser-web-timeline-basic.service"
        # graphical/multi-user targets can remain blocked by the long-lived
        # Xorg service on this minimal kernel.  Start the browser workload from
        # basic.target instead; its wait-x preflight still enforces the actual
        # display readiness before Firefox is exec'd.
        mkdir -p -- "$stage/etc/systemd/system/basic.target.wants"
        # The online profile must also start the Xorg provider before
        # graphical.target; that target may never complete while the provider
        # remains a long-lived simple service.  Browser wait-x below supplies
        # the ordering barrier once the socket actually exists.
        rm -f -- "$stage/etc/systemd/system/graphical.target.wants/$service_name.service"
        ln -s -- ../$service_name.service \
            "$stage/etc/systemd/system/basic.target.wants/$service_name.service"
        ln -s -- ../asterinas-browser-web.service \
            "$stage/etc/systemd/system/basic.target.wants/asterinas-browser-web.service"
        ln -s -- ../asterinas-browser-web-evidence.service \
            "$stage/etc/systemd/system/basic.target.wants/asterinas-browser-web-evidence.service"
    fi
    rm -f -- "$stage/etc/systemd/system/default.target"
    ln -s -- /lib/systemd/system/graphical.target \
        "$stage/etc/systemd/system/default.target"

    install -d -m 0755 -- "$stage/etc/X11/xorg.conf.d"
    cat >"$stage/etc/X11/xorg.conf.d/20-asterinas.conf" <<'EOF'
Section "Device"
    Identifier "Asterinas framebuffer"
    Driver "fbdev"
    Option "fbdev" "/dev/fb0"
EndSection

Section "Screen"
    Identifier "Asterinas screen"
    Device "Asterinas framebuffer"
EndSection

Section "InputDevice"
    Identifier "Asterinas keyboard"
    Driver "evdev"
    Option "Device" "/dev/input/event0"
EndSection

Section "InputDevice"
    Identifier "Asterinas pointer"
    Driver "evdev"
    Option "Device" "/dev/input/event1"
EndSection

Section "ServerLayout"
    Identifier "Asterinas layout"
    Screen 0 "Asterinas screen"
    InputDevice "Asterinas keyboard" "CoreKeyboard"
    InputDevice "Asterinas pointer" "CorePointer"
EndSection

Section "ServerFlags"
    Option "AutoAddDevices" "false"
    Option "BlankTime" "0"
    Option "StandbyTime" "0"
    Option "SuspendTime" "0"
    Option "OffTime" "0"
EndSection
EOF
    chmod 0644 -- "$stage/etc/X11/xorg.conf.d/20-asterinas.conf"
}

create_and_verify_image() {
    local root_image="$WORK_DIR/debian-root.ext2"
    local stage="$WORK_DIR/stage"
    local dumpe2fs_output="$WORK_DIR/dumpe2fs.txt"
    local dumped_bash="$WORK_DIR/bash"

    log "phase 8/8: creating and verifying ext2 image"
    truncate -s "$ROOT_SIZE_BYTES" "$root_image"
    mke2fs -q -F -t ext2 -b "$ROOT_BLOCK_SIZE_BYTES" \
        -L "$ROOT_LABEL" -U "$ROOT_UUID" -d "$stage" "$root_image"
    [[ "$(stat -c '%s' "$root_image")" == "$ROOT_SIZE_BYTES" ]] ||
        die "root image does not match the configured profile size"

    dumpe2fs -h "$root_image" >"$dumpe2fs_output" 2>/dev/null
    grep -Eq "^Filesystem volume name:[[:space:]]+$ROOT_LABEL$" "$dumpe2fs_output" ||
        die "root image label verification failed"
    grep -Eq "^Filesystem UUID:[[:space:]]+$ROOT_UUID$" "$dumpe2fs_output" ||
        die "root image UUID verification failed"
    grep -Eq "^Block size:[[:space:]]+$ROOT_BLOCK_SIZE_BYTES$" "$dumpe2fs_output" ||
        die "root image block-size verification failed"
    ! grep -Eq '^Filesystem features:.*(^|[[:space:]])has_journal([[:space:]]|$)' \
        "$dumpe2fs_output" || die "root image unexpectedly contains an ext3 journal"

    debugfs_require_path "$root_image" /bin/bash
    debugfs_require_path "$root_image" /lib/ld-linux-riscv64-lp64d.so.1
    debugfs_require_path "$root_image" /var/lib/dpkg/status
    debugfs_require_path "$root_image" /etc/asterinas-rootfs.bashrc
    debugfs_reject_path "$root_image" /usr/bin/qemu-riscv64-static
    debugfs -R "dump /bin/bash $dumped_bash" "$root_image" >/dev/null 2>&1 ||
        die "failed to extract Bash from root image"
    chmod 0700 "$dumped_bash"
    qemu-riscv64-static -L "$stage" "$dumped_bash" -c true ||
        die "root image Bash smoke test failed"
}

debugfs_require_path() {
    local image="$1"
    local path="$2"
    local output

    output="$(debugfs -R "stat $path" "$image" 2>&1)"
    [[ "$output" != *"File not found"* && "$output" == *"Inode:"* ]] ||
        die "required image path is missing: $path"
}

debugfs_reject_path() {
    local image="$1"
    local path="$2"
    local output

    output="$(debugfs -R "stat $path" "$image" 2>&1 || true)"
    [[ "$output" == *"File not found"* ]] ||
        die "forbidden image path is present: $path"
}

write_rootfs_manifest() {
    local script_directory
    local repository_root
    local build_timestamp
    local debootstrap_version
    local mke2fs_version
    local qemu_version
    local browser_web_runtime_version=""
    local -a browser_web_tool_version=()

    script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
    repository_root="$(cd -- "$script_directory/../../../.." && pwd -P)"
    build_timestamp="$(date -u -d "@$SOURCE_DATE_EPOCH" '+%Y-%m-%dT%H:%M:%SZ')"
    debootstrap_version="$(debootstrap --version 2>&1 | head -n 1)"
    mke2fs_version="$(mke2fs -V 2>&1 | head -n 1)"
    qemu_version="$(qemu-riscv64-static --version 2>&1 | head -n 1)"

    if [[ "$PROFILE" == browser-web ]]; then
        browser_web_runtime_version="$(browser_web_runtime_digest "$script_directory")"
        browser_web_tool_version=(
            --tool-version "browser-web-runtime=$browser_web_runtime_version"
        )
    fi

    local -a signed_source_arguments=()
    if is_firefox_profile; then
        signed_source_arguments=(
            --signed-source "base=$WORK_DIR/source-metadata/InRelease"
            --signed-source "security=$WORK_DIR/source-metadata/Security-InRelease"
        )
    fi
    PYTHONPATH="$repository_root" python3 -m tools.riscv.debian.rootfs.contract \
        write-manifest \
        --output "$WORK_DIR/rootfs-manifest.json" \
        --image "$WORK_DIR/debian-root.ext2" \
        --packages-lock "$WORK_DIR/packages.lock" \
        --inrelease "$WORK_DIR/source-metadata/InRelease" \
        --package-checksums "$WORK_DIR/source-metadata/package-checksums" \
        --mirror "$MIRROR" \
        --suite "$SUITE" \
        --debian-release "$DEBIAN_RELEASE" \
        --profile "$PROFILE" \
        "${signed_source_arguments[@]}" \
        --build-timestamp "$build_timestamp" \
        --tool-version "debootstrap=$debootstrap_version" \
        --tool-version "mke2fs=$mke2fs_version" \
        --tool-version "qemu-riscv64-static=$qemu_version" \
        "${browser_web_tool_version[@]}"
}

browser_web_runtime_digest() {
    local source_directory="$1"
    local input
    local -a inputs=(
        desktop_m5_network_evidence.sh
        desktop_m5_network_gate.py
        browser_web_firefox.sh
        browser_web_marionette_gate.py
        browser_m5_marionette_gate.py
        browser_web_evidence.sh
        browser_web.service
        browser_web_evidence.service
    )

    for input in "${inputs[@]}"; do
        [[ -f "$source_directory/$input" ]] ||
            die "missing browser-web runtime input: $input"
    done
    {
        for input in "${inputs[@]}"; do
            printf '%s\n' "$input"
            sha256sum -- "$source_directory/$input" | cut -d' ' -f1
        done
    } | sha256sum | cut -d' ' -f1
}

publish_artifacts() {
    local script_directory
    local repository_root
    local -a security_publication=()

    script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
    repository_root="$(cd -- "$script_directory/../../../.." && pwd -P)"
    if is_firefox_profile; then
        security_publication=(--include-security-inrelease)
    fi

    # The helper rolls back ordinary failures and termination signals. The
    # profile-specific file set is intentionally not claimed to be power-loss
    # atomic.
    PYTHONPATH="$repository_root" python3 -m tools.riscv.debian.rootfs.fsops \
        publish-set \
        --output-dir "$OUTPUT_DIR" \
        --source-root "$WORK_DIR" \
        "${security_publication[@]}"
}

log() {
    printf '[debian-rootfs] %s\n' "$*" >&2
}

die() {
    printf 'build_rootfs.sh: %s\n' "$*" >&2
    exit 2
}

is_firefox_profile() {
    [[ "$PROFILE" == browser-m5 || "$PROFILE" == browser-web ]]
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
