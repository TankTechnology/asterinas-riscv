#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

umask 077

readonly DEFAULT_OUTPUT_DIR="target/debian-riscv/rootfs"
readonly SYSTEMD_M2_OUTPUT_DIR="target/debian-riscv/systemd-m2/rootfs"
readonly DESKTOP_M3_OUTPUT_DIR="target/debian-riscv/desktop-m3/rootfs"
readonly DESKTOP_M4_OUTPUT_DIR="target/debian-riscv/desktop-m4/rootfs"
readonly DESKTOP_M5_NETWORK_OUTPUT_DIR="target/debian-riscv/desktop-m5-network/rootfs"
readonly DEFAULT_CACHE_DIR="target/debian-riscv/cache"
readonly DEFAULT_MIRROR="https://mirrors.tuna.tsinghua.edu.cn/debian"
readonly SUPPORTED_SUITE="trixie"
readonly DEBIAN_ARCHITECTURE="riscv64"
readonly DEBOOTSTRAP_VARIANT="minbase"
readonly DEBIAN_KEYRING="/usr/share/keyrings/debian-archive-keyring.gpg"
readonly ROOT_SIZE_BYTES="1073741824"
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
        minimal-m1 | systemd-m2 | desktop-m3 | desktop-m4 | desktop-m5-network) ;;
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
    ((${#profile_fields[@]} >= 3)) || die "invalid rootfs profile data: $PROFILE"
    ROOT_LABEL="${profile_fields[0]}"
    ROOT_UUID="${profile_fields[1]}"
    INSTALL_PACKAGES=("${profile_fields[@]:2}")
    if [[ "$PROFILE" == systemd-m2 && "$has_output_dir" == 0 ]]; then
        OUTPUT_DIR="$SYSTEMD_M2_OUTPUT_DIR"
    elif [[ "$PROFILE" == desktop-m3 && "$has_output_dir" == 0 ]]; then
        OUTPUT_DIR="$DESKTOP_M3_OUTPUT_DIR"
    elif [[ "$PROFILE" == desktop-m4 && "$has_output_dir" == 0 ]]; then
        OUTPUT_DIR="$DESKTOP_M4_OUTPUT_DIR"
    elif [[ "$PROFILE" == desktop-m5-network && "$has_output_dir" == 0 ]]; then
        OUTPUT_DIR="$DESKTOP_M5_NETWORK_OUTPUT_DIR"
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
    [[ "$SOURCE_DATE_EPOCH" =~ ^(0|[1-9][0-9]*)$ ]] ||
        die "SOURCE_DATE_EPOCH must be a canonical nonnegative decimal integer"
    decimal_is_at_most "$SOURCE_DATE_EPOCH" "$MAX_SOURCE_DATE_EPOCH" ||
        die "SOURCE_DATE_EPOCH exceeds the ext/newc-compatible u32 range"

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
    trap - EXIT INT TERM HUP
    if [[ -n "$WORK_DIR" && -d "$WORK_DIR" ]]; then
        chmod -R u+w -- "$WORK_DIR" 2>/dev/null || true
        rm -rf -- "$WORK_DIR"
    fi
    exit "$exit_status"
}

fetch_and_verify_release() {
    local inrelease="$WORK_DIR/source-metadata/InRelease"
    local release_url="$MIRROR/dists/$SUITE/InRelease"
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
    verify_riscv_binfmt
    log "phase 3/8: completing debootstrap second stage"
    chroot "$stage" /debootstrap/debootstrap --second-stage
}

verify_riscv_binfmt() {
    local registration="/proc/sys/fs/binfmt_misc/qemu-riscv64"

    [[ "$(uname -m)" != riscv64 ]] ||
        die "refusing a native RISC-V host; an enabled binfmt boundary is required"
    [[ -r /proc/sys/fs/binfmt_misc/status ]] &&
        grep -qx enabled /proc/sys/fs/binfmt_misc/status ||
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
    cp -L -- /etc/resolv.conf "$stage/etc/resolv.conf"
    mkdir -p -- "$stage/etc/ssl/certs"
    cp -L -- /etc/ssl/certs/ca-certificates.crt "$bootstrap_ca"
    chroot "$stage" /usr/bin/env \
        DEBIAN_FRONTEND=noninteractive \
        SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" \
        apt-get -o \
        Acquire::https::CaInfo=/etc/ssl/certs/asterinas-bootstrap-ca.crt update

    log "phase 5/8: installing explicit minbase additions"
    chroot "$stage" /usr/bin/env \
        DEBIAN_FRONTEND=noninteractive \
        SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" \
        apt-get -y --no-install-recommends install "${INSTALL_PACKAGES[@]}"
    rm -f -- "$bootstrap_ca"
    chroot "$stage" /usr/bin/env \
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

    log "phase 6/8: auditing package lock and signed-index checksums"
    verify_release_is_unchanged "$WORK_DIR" "$MIRROR" "$SUITE" "$DEBIAN_RELEASE"
    LC_ALL=C dpkg-query \
        "--admindir=$stage/var/lib/dpkg" \
        --show \
        '--showformat=${Package}\t${Architecture}\t${Version}\n' |
        LC_ALL=C sort -u >"$WORK_DIR/packages.lock"

    : >"$WORK_DIR/package-index"
    for package_list in "$stage"/var/lib/apt/lists/*_Packages*; do
        [[ -f "$package_list" ]] || continue
        package_list_name="${package_list##*/}"
        case "$package_list_name" in
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
        [[ "$authenticated_paths" != *$'\n'"$release_path"$'\n'* ]] ||
            die "ambiguous apt Packages index target: $release_path"
        authenticated_paths+=$'\n'"$release_path"$'\n'
        package_index="$WORK_DIR/package-index-$authenticated_index_count"
        chroot "$stage" /usr/lib/apt/apt-helper cat-file \
            "/var/lib/apt/lists/$package_list_name" >"$package_index"
        authenticate_package_index \
            "$package_index" \
            "$release_path" \
            "$WORK_DIR/source-metadata/InRelease"
        cat "$package_index" >>"$WORK_DIR/package-index"
        printf '\n' >>"$WORK_DIR/package-index"
        ((authenticated_index_count += 1))
    done
    ((authenticated_index_count > 0)) || die "no authenticated package index is available"
    extract_package_index_checksums \
        "$WORK_DIR/package-index" \
        "$WORK_DIR/package-index-checksums"
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
    local -a matching_rows=()

    : >"$WORK_DIR/source-metadata/package-checksums"
    for archive in "$WORK_DIR"/debs/*.deb; do
        archive_sha256="$(sha256sum "$archive")"
        archive_sha256="${archive_sha256%% *}"
        mapfile -t matching_rows < <(
            awk -F '\t' -v sha256="$archive_sha256" '$4 == sha256' \
                "$WORK_DIR/package-index-checksums"
        )
        ((${#matching_rows[@]} == 1)) ||
            die "downloaded package hash is not unique in the verified package index: ${archive##*/}"
        printf '%s\n' "${matching_rows[0]}" >> \
            "$WORK_DIR/source-metadata/package-checksums"

        copy_into_content_cache "$archive" "$archive_sha256"
    done
    LC_ALL=C sort -u \
        "$WORK_DIR/source-metadata/package-checksums" \
        -o "$WORK_DIR/source-metadata/package-checksums"
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
    fi
    : >"$stage/etc/machine-id"
    printf 'nameserver 1.1.1.1\n' >"$stage/etc/resolv.conf"
    rm -f -- "$stage/var/lib/dbus/machine-id"
    rm -f -- "$stage/var/lib/dpkg/lock" "$stage/var/lib/dpkg/lock-frontend"
    rm -f -- "$stage/usr/bin/qemu-riscv64-static"
    [[ ! -e "$stage/usr/bin/qemu-riscv64-static" ]] ||
        die "qemu-riscv64-static remains in staged rootfs"
    rm -rf -- \
        "$stage/debootstrap" \
        "$stage/var/cache/apt/archives/"* \
        "$stage/var/lib/apt/lists/"* \
        "$stage/var/log/"* \
        "$stage/tmp/"* \
        "$stage/var/tmp/"*
    find "$stage" -xdev -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +
}

configure_desktop_m5_network() {
    local stage="$1"
    local script_directory
    local service_name="asterinas-desktop-m5-network"
    local browser_service_name="asterinas-desktop-m6-browser"
    local baidu_service_name="asterinas-desktop-m7-baidu"

    script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
    install -D -m 0755 -- \
        "$script_directory/desktop_m5_network_evidence.sh" \
        "$stage/usr/lib/asterinas/desktop-m5-network-evidence"
    cat >"$stage/etc/systemd/system/$service_name.service" <<'EOF'
[Unit]
Description=Asterinas Debian M5 wired-network evidence
After=local-fs.target
Before=asterinas-desktop-m4.service

[Service]
Type=oneshot
ExecStart=/usr/lib/asterinas/desktop-m5-network-evidence
RemainAfterExit=yes

[Install]
WantedBy=graphical.target
EOF
    chmod 0644 -- "$stage/etc/systemd/system/$service_name.service"
    ln -s -- \
        "../$service_name.service" \
        "$stage/etc/systemd/system/graphical.target.wants/$service_name.service"

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
ExecStart=/usr/lib/asterinas/desktop-m7-baidu-evidence
RemainAfterExit=yes

[Install]
WantedBy=graphical.target
EOF
    chmod 0644 -- "$stage/etc/systemd/system/$baidu_service_name.service"
    ln -s -- \
        "../$baidu_service_name.service" \
        "$stage/etc/systemd/system/graphical.target.wants/$baidu_service_name.service"
}

configure_desktop() {
    local stage="$1"
    local generation="$2"
    local script_directory
    local session_source
    local evidence_source
    local service_name="asterinas-desktop-$generation"

    script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
    session_source="$script_directory/desktop_${generation}_session.sh"
    evidence_source="$script_directory/desktop_${generation}_evidence.sh"
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
            "$stage/home/asterinas/Asterinas Files"
        install -D -m 0644 -- \
            "$script_directory/desktop_m4_welcome.html" \
            "$stage/usr/share/asterinas/desktop-m4-welcome.html"
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
After=local-fs.target dbus.service systemd-udevd.service systemd-logind.service
Wants=dbus.service systemd-udevd.service systemd-logind.service
Conflicts=getty@tty1.service

[Service]
Type=simple
User=asterinas
SupplementaryGroups=video input
PAMName=login
TTYPath=/dev/tty1
StandardInput=tty
StandardOutput=journal+console
StandardError=journal+console
TTYReset=yes
TTYVHangup=yes
TTYVTDisallocate=yes
Environment=HOME=/home/asterinas
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
    cat >"$stage/etc/systemd/system/$service_name-evidence.service" <<EOF
[Unit]
Description=Asterinas Debian desktop evidence
After=basic.target
Wants=$service_name.service

[Service]
Type=oneshot
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
    ln -s -- \
        ../$service_name-evidence.service \
        "$stage/etc/systemd/system/graphical.target.wants/$service_name-evidence.service"
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
    truncate -s 1G "$root_image"
    mke2fs -q -F -t ext2 -b "$ROOT_BLOCK_SIZE_BYTES" \
        -L "$ROOT_LABEL" -U "$ROOT_UUID" -d "$stage" "$root_image"
    [[ "$(stat -c '%s' "$root_image")" == "$ROOT_SIZE_BYTES" ]] ||
        die "root image is not exactly 1 GiB"

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

    script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
    repository_root="$(cd -- "$script_directory/../../../.." && pwd -P)"
    build_timestamp="$(date -u -d "@$SOURCE_DATE_EPOCH" '+%Y-%m-%dT%H:%M:%SZ')"
    debootstrap_version="$(debootstrap --version 2>&1 | head -n 1)"
    mke2fs_version="$(mke2fs -V 2>&1 | head -n 1)"
    qemu_version="$(qemu-riscv64-static --version 2>&1 | head -n 1)"

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
        --build-timestamp "$build_timestamp" \
        --tool-version "debootstrap=$debootstrap_version" \
        --tool-version "mke2fs=$mke2fs_version" \
        --tool-version "qemu-riscv64-static=$qemu_version"
}

publish_artifacts() {
    local script_directory
    local repository_root

    script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
    repository_root="$(cd -- "$script_directory/../../../.." && pwd -P)"

    # The helper rolls back ordinary failures and termination signals. The
    # five-file set is intentionally not claimed to be power-loss atomic.
    PYTHONPATH="$repository_root" python3 -m tools.riscv.debian.rootfs.fsops \
        publish-set \
        --output-dir "$OUTPUT_DIR" \
        --source-root "$WORK_DIR"
}

log() {
    printf '[debian-rootfs] %s\n' "$*" >&2
}

die() {
    printf 'build_rootfs.sh: %s\n' "$*" >&2
    exit 2
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
