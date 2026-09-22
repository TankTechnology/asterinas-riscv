#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly UBOOT_COMMIT=ece349ade2973e220f524ce59e59711cc919263f
# Leave room for the three payloads without creating a large sparse test disk.
readonly BOOT_DISK_SIZE=64M
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
readonly -a REQUIRED_TOOLS=(
    bc
    bison
    debugfs
    dtc
    fdtget
    fdtput
    flex
    flock
    git
    make
    mkfs.ext4
    python3
    qemu-system-riscv64
    realpath
    riscv64-linux-gnu-gcc
    sha256sum
    swig
    truncate
)
readonly -a PAYLOAD_NAMES=(asterinas.booti initramfs.cpio.gz qemu-virt.dtb)

canonical_repo_path() {
    local candidate="$1"
    if [[ "${candidate}" != /* ]]; then
        candidate="${REPO_ROOT}/${candidate}"
    fi
    realpath -m -- "${candidate}"
}

canonical_output_dir() {
    local candidate="$1"
    local qemu_output_root
    local ltp_output_root
    local resolved
    qemu_output_root="$(canonical_repo_path "target/qemu-uboot")"
    ltp_output_root="$(canonical_repo_path "target/ltp/qemu")"
    resolved="$(canonical_repo_path "${candidate}")"
    case "${resolved}" in
        "${qemu_output_root}"/*|"${ltp_output_root}"/*)
            printf '%s\n' "${resolved}"
            ;;
        *)
            printf 'QEMU_UBOOT_OUT_DIR must resolve below %s or %s\n' \
                "${qemu_output_root}" "${ltp_output_root}" >&2
            return 2
            ;;
    esac
}

profile_field() {
    local selected_profile="$1"
    local selected_field="$2"
    python3 "${SCRIPT_DIR}/qemu_uboot_booti.py" profile-field \
        --profile "${selected_profile}" --field "${selected_field}"
}

if [[ "${1:-}" == "--print-uboot-commit" ]]; then
    printf '%s\n' "${UBOOT_COMMIT}"
    exit 0
fi

if [[ "${1:-}" == "--print-payload-manifest" ]]; then
    printf '%s\n' "${PAYLOAD_NAMES[@]}"
    exit 0
fi

if [[ "${1:-}" == "--check-tools" ]]; then
    missing=0
    for tool in "${REQUIRED_TOOLS[@]}"; do
        if ! command -v "${tool}" >/dev/null 2>&1; then
            printf 'missing tool: %s\n' "${tool}" >&2
            missing=1
        fi
    done
    if command -v python3 >/dev/null 2>&1 \
        && ! python3 -c 'import setuptools' >/dev/null 2>&1; then
        printf '%s\n' 'missing Python module: setuptools' >&2
        missing=1
    fi
    if command -v python3 >/dev/null 2>&1 \
        && ! python3 -c 'import pathlib, sysconfig; raise SystemExit(not (pathlib.Path(sysconfig.get_path("include")) / "Python.h").is_file())' \
            >/dev/null 2>&1; then
        printf '%s\n' 'missing Python development headers' >&2
        missing=1
    fi
    if [[ ! -f /usr/include/openssl/evp.h ]]; then
        printf '%s\n' 'missing OpenSSL development headers' >&2
        missing=1
    fi
    exit "${missing}"
fi

if [[ "${1:-}" == "--canonical-output-dir" ]]; then
    if [[ -z "${2:-}" ]]; then
        printf '%s\n' 'missing output directory' >&2
        exit 2
    fi
    canonical_output_dir "$2"
    exit $?
fi

if [[ "${1:-}" == "--canonical-build-dir" ]]; then
    if [[ -z "${2:-}" ]]; then
        printf '%s\n' 'missing build directory' >&2
        exit 2
    fi
    canonical_repo_path "$2"
    exit $?
fi

if [[ "${1:-}" == "--verify-uboot-source" ]]; then
    source_dir="${2:-}"
    expected_commit="${3:-${UBOOT_COMMIT}}"
    if [[ -z "${source_dir}" ]]; then
        printf '%s\n' 'missing U-Boot source directory' >&2
        exit 2
    fi
    actual_commit="$(git -c "safe.directory=${source_dir}" -C "${source_dir}" \
        rev-parse HEAD 2>/dev/null || true)"
    if [[ "${actual_commit}" != "${expected_commit}" ]]; then
        printf 'U-Boot commit mismatch: expected %s, got %s\n' \
            "${expected_commit}" "${actual_commit:-not-a-git-checkout}" >&2
        exit 1
    fi
    if [[ -n "$(git -c "safe.directory=${source_dir}" -C "${source_dir}" \
        status --porcelain --untracked-files=all)" ]]; then
        printf '%s\n' 'U-Boot checkout is not clean' >&2
        exit 1
    fi
    exit 0
fi

if [[ "${1:-}" == "prepare" ]]; then
    missing_input=0
    if [[ -z "${ASTERINAS_RISCV_BOOTI:-}" ]]; then
        printf '%s\n' 'missing environment variable: ASTERINAS_RISCV_BOOTI' >&2
        missing_input=1
    fi
    if [[ -z "${ASTERINAS_INITRAMFS:-}" ]]; then
        printf '%s\n' 'missing environment variable: ASTERINAS_INITRAMFS' >&2
        missing_input=1
    fi
    if [[ "${missing_input}" -ne 0 ]]; then
        exit 2
    fi
    profile="${QEMU_UBOOT_PROFILE:-generic-sv39}"
    readonly profile
    variant="${QEMU_UBOOT_VARIANT:-}"
    readonly variant
    if [[ -n "${variant}" ]]; then
        if ! python3 "${SCRIPT_DIR}/qemu_uboot_variants.py" validate \
            --variant "${variant}" >/dev/null; then
            printf 'unknown QEMU U-Boot variant: %s\n' "${variant}" >&2
            exit 2
        fi
        python3 "${SCRIPT_DIR}/qemu_uboot_dtb.py" validate-selection \
            --profile "${profile}" \
            --variant "${variant}" >/dev/null
    fi
    bash "$0" --check-tools
    python3 "${SCRIPT_DIR}/qemu_uboot_dtb.py" validate-profile \
        --profile "${profile}" >/dev/null
    dtb_filename="$(profile_field "${profile}" dtb-filename)"
    readonly dtb_filename
    uboot_defconfig="$(profile_field "${profile}" uboot-defconfig)"
    readonly uboot_defconfig
    uboot_binary="$(profile_field "${profile}" uboot-binary)"
    readonly uboot_binary
    uboot_build_mode="$(profile_field "${profile}" uboot-build-mode)"
    readonly uboot_build_mode
    storage_transport="$(profile_field "${profile}" storage-transport)"
    readonly storage_transport

    out_dir="$(canonical_output_dir \
        "${QEMU_UBOOT_OUT_DIR:-${REPO_ROOT}/target/qemu-uboot/current}")"
    readonly out_dir
    cache_dir="$(canonical_repo_path \
        "${QEMU_UBOOT_CACHE_DIR:-${REPO_ROOT}/target/qemu-uboot/cache}")"
    readonly cache_dir
    source_dir="$(canonical_repo_path \
        "${QEMU_UBOOT_SOURCE_DIR:-${cache_dir}/u-boot}")"
    readonly source_dir
    build_dir="$(canonical_repo_path \
        "${QEMU_UBOOT_BUILD_DIR:-${cache_dir}/u-boot-build}")"
    readonly build_dir
    if [[ ! -s "${ASTERINAS_RISCV_BOOTI}" ]]; then
        printf 'missing or empty booti image: %s\n' "${ASTERINAS_RISCV_BOOTI}" >&2
        exit 2
    fi
    if [[ ! -s "${ASTERINAS_INITRAMFS}" ]]; then
        printf 'missing or empty initramfs: %s\n' "${ASTERINAS_INITRAMFS}" >&2
        exit 2
    fi

    mkdir -p "${out_dir}" "${cache_dir}"
    exec {prepare_lock_fd}>"${cache_dir}/prepare.lock"
    flock "${prepare_lock_fd}"
    rm -f "${out_dir}/result.json" \
        "${out_dir}/serial.log" \
        "${out_dir}/marker-event.txt" \
        "${out_dir}/artifacts.json" \
        "${out_dir}/SHA256SUMS" \
        "${out_dir}/u-boot-commit.txt" \
        "${out_dir}/u-boot.config" \
        "${out_dir}/qemu-version.txt" \
        "${out_dir}/qemu-dtb-audit.json" \
        "${out_dir}/qemu-dtb-variant-audit.json" \
        "${out_dir}/qemu-virt.source.dtb" \
        "${out_dir}/qemu-virt.dtb" \
        "${out_dir}/qemu-virt.dts" \
        "${out_dir}/qemu-sifive-u.dtb" \
        "${out_dir}/qemu-sifive-u.dts"
    if [[ ! -d "${source_dir}/.git" ]]; then
        git clone --filter=blob:none --no-checkout \
            https://github.com/u-boot/u-boot.git "${source_dir}"
    fi
    if [[ "$(git -c "safe.directory=${source_dir}" -C "${source_dir}" \
        rev-parse HEAD 2>/dev/null || true)" \
        != "${UBOOT_COMMIT}" ]]; then
        git -c "safe.directory=${source_dir}" -C "${source_dir}" \
            fetch --depth=1 origin "${UBOOT_COMMIT}"
        git -c "safe.directory=${source_dir}" -C "${source_dir}" \
            checkout --detach "${UBOOT_COMMIT}"
    fi
    bash "$0" --verify-uboot-source "${source_dir}"

    make -C "${source_dir}" O="${build_dir}" \
        CROSS_COMPILE=riscv64-linux-gnu- "${uboot_defconfig}"
    case "${uboot_build_mode}" in
        standard-smode) ;;
        board-smode)
            "${source_dir}/scripts/config" --file "${build_dir}/.config" \
                --enable OF_BOARD \
                --disable BINMAN_FDT \
                --disable SPL
            make -C "${source_dir}" O="${build_dir}" \
                CROSS_COMPILE=riscv64-linux-gnu- olddefconfig
            ;;
        *)
            printf 'unsupported U-Boot build mode: %s\n' \
                "${uboot_build_mode}" >&2
            exit 2
            ;;
    esac

    # Boot with no autoboot delay.  The stock defconfig sets CONFIG_BOOTDELAY=2,
    # so every boot spends two seconds printing "Hit any key to stop autoboot:
    # 2/1/0" and waiting for a keypress that no automated boot ever sends.  It
    # is 2 of the desktop's ~15 seconds, measured at ~2.8 s on the guest's
    # basic.target (7.92/8.67 s before, 5.39/5.57 s after).
    #
    # This is safe for the gate's U-Boot interaction, which is worth stating
    # because it looks like it should not be.  The gate drives the U-Boot
    # prompt with `serial.wait_for(b"=> ")`, and it reaches that prompt because
    # the default `bootcmd` fails to find a bootable device and falls back to
    # it -- not because anything interrupts the countdown.  The
    # "Hit any key to stop autoboot" line is still printed (as ": 0"), so the
    # reboot synchronisation marker in debian/rootfs/systemd_m2_gate.py still
    # matches.  Both were checked against a BOOTDELAY=0 build before landing.
    "${source_dir}/scripts/config" --file "${build_dir}/.config" \
        --set-val BOOTDELAY 0

    # The bochs display's resolution is a *compile-time* property of U-Boot,
    # not something QEMU's `xres=`/`yres=` device properties decide.
    # `drivers/video/bochs.c:16-17` initialises its mode from
    # CONFIG_VIDEO_BOCHS_SIZE_X/Y, and `pci display 0.1.0` in the framebuffer
    # plan makes U-Boot program the device itself -- so whatever the QEMU
    # command line asked for is overwritten with the value U-Boot was built
    # with. Those defaults are 1280x1024, which is why every device contract in
    # this tree has carried `BOCHS_XRGB8888` and why a contract with any other
    # geometry fails the capture-dimension check *after* the guest has drawn
    # correctly. Rebuilding U-Boot is the only lever, so it is a build input.
    #
    # Leave this unset for the ordinary 1280x1024 U-Boot; a run that needs a
    # different geometry must also use its own QEMU_UBOOT_BUILD_DIR, because
    # the two configurations cannot share one.
    if [[ -n "${QEMU_UBOOT_BOCHS_SIZE:-}" ]]; then
        if [[ ! "${QEMU_UBOOT_BOCHS_SIZE}" =~ ^[1-9][0-9]*x[1-9][0-9]*$ ]]; then
            printf 'QEMU_UBOOT_BOCHS_SIZE must be <width>x<height>, got %s\n' \
                "${QEMU_UBOOT_BOCHS_SIZE}" >&2
            exit 2
        fi
        "${source_dir}/scripts/config" --file "${build_dir}/.config" \
            --set-val VIDEO_BOCHS_SIZE_X "${QEMU_UBOOT_BOCHS_SIZE%%x*}" \
            --set-val VIDEO_BOCHS_SIZE_Y "${QEMU_UBOOT_BOCHS_SIZE##*x}"
    fi
    # `yes ''` rather than a plain stdin: kconfig asks for new symbols
    # interactively, and when stdin is not a tty it re-reads EOF and loops
    # forever instead of failing.  Feeding blank lines accepts the defaults and
    # terminates.  This is not hypothetical -- getting it wrong once spun `conf`
    # in a tight loop that wrote tens of gigabytes and filled the disk.
    #
    # `{ yes '' || true; }` and not a bare `yes ''`: this script runs under
    # `set -o pipefail`, and `yes` has no way to exit on its own -- it is killed
    # by SIGPIPE the moment `make` closes the pipe, which is the normal end of
    # this pipeline.  A bare `yes` therefore makes the whole pipeline report
    # 141 even when the configuration succeeded, and `set -e` aborts the
    # prepare right there.  Swallowing only `yes`'s status keeps `make`'s, so a
    # genuinely failed `olddefconfig` still stops the script.
    { yes '' || true; } | make -C "${source_dir}" O="${build_dir}" \
        CROSS_COMPILE=riscv64-linux-gnu- olddefconfig

    make -C "${source_dir}" O="${build_dir}" \
        CROSS_COMPILE=riscv64-linux-gnu- -j"$(nproc)" "${uboot_binary}"
    test -s "${build_dir}/${uboot_binary}"
    grep -q '^CONFIG_BOOTDELAY=0$' "${build_dir}/.config"
    if [[ -n "${QEMU_UBOOT_BOCHS_SIZE:-}" ]]; then
        grep -q "^CONFIG_VIDEO_BOCHS_SIZE_X=${QEMU_UBOOT_BOCHS_SIZE%%x*}$" \
            "${build_dir}/.config"
        grep -q "^CONFIG_VIDEO_BOCHS_SIZE_Y=${QEMU_UBOOT_BOCHS_SIZE##*x}$" \
            "${build_dir}/.config"
    fi
    grep -q '^CONFIG_CMD_BOOTI=y$' "${build_dir}/.config"
    grep -q '^CONFIG_CMD_EXT4=y$' "${build_dir}/.config"
    case "${storage_transport}" in
        virtio-ext4) grep -q '^CONFIG_VIRTIO_BLK=y$' "${build_dir}/.config" ;;
        mmc-ext4)
            grep -q '^CONFIG_MMC=y$' "${build_dir}/.config"
            grep -q '^CONFIG_CMD_MMC=y$' "${build_dir}/.config"
            ;;
        *)
            printf 'unsupported storage transport: %s\n' \
                "${storage_transport}" >&2
            exit 2
            ;;
    esac
    cp "${build_dir}/.config" "${out_dir}/u-boot.config"
    printf '%s\n' "${UBOOT_COMMIT}" > "${out_dir}/u-boot-commit.txt"
    qemu-system-riscv64 --version > "${out_dir}/qemu-version.txt"

    if [[ -n "${variant}" ]]; then
        python3 "${SCRIPT_DIR}/qemu_uboot_dtb.py" generate \
            --profile "${profile}" \
            --dtb "${out_dir}/qemu-virt.source.dtb" \
            --dts "${out_dir}/qemu-virt.dts" \
            >/dev/null
        python3 "${SCRIPT_DIR}/qemu_uboot_dtb.py" derive-variant \
            --profile "${profile}" \
            --variant "${variant}" \
            --source-dtb "${out_dir}/qemu-virt.source.dtb" \
            --payload-dtb "${out_dir}/${dtb_filename}" \
            --audit-output "${out_dir}/qemu-dtb-variant-audit.json"
        python3 "${SCRIPT_DIR}/qemu_uboot_dtb.py" audit-existing \
            --profile "${profile}" \
            --dtb "${out_dir}/${dtb_filename}" \
            --audit-output "${out_dir}/qemu-dtb-audit.json"
    else
        python3 "${SCRIPT_DIR}/qemu_uboot_dtb.py" generate \
            --profile "${profile}" \
            --dtb "${out_dir}/${dtb_filename}" \
            --dts "${out_dir}/${dtb_filename%.dtb}.dts" \
            > "${out_dir}/qemu-dtb-audit.json"
    fi

    readonly stage_dir="${out_dir}/fs-root"
    readonly verify_dir="${out_dir}/fs-verify"
    rm -rf "${stage_dir}" "${verify_dir}"
    rm -f "${out_dir}/boot.ext4"
    mkdir -p "${stage_dir}" "${verify_dir}"
    cp "${ASTERINAS_RISCV_BOOTI}" "${stage_dir}/asterinas.booti"
    cp "${ASTERINAS_INITRAMFS}" "${stage_dir}/initramfs.cpio.gz"
    cp "${out_dir}/${dtb_filename}" "${stage_dir}/${dtb_filename}"
    test "$(find "${stage_dir}" -maxdepth 1 -type f | wc -l)" -eq 3
    truncate -s "${BOOT_DISK_SIZE}" "${out_dir}/boot.ext4"
    mkfs.ext4 -q -F -d "${stage_dir}" "${out_dir}/boot.ext4"
    readonly -a payload_names=(
        asterinas.booti
        initramfs.cpio.gz
        "${dtb_filename}"
    )
    for payload in "${payload_names[@]}"; do
        debugfs -R "dump -p /${payload} ${verify_dir}/${payload}" \
            "${out_dir}/boot.ext4" >/dev/null 2>&1
        cmp "${stage_dir}/${payload}" "${verify_dir}/${payload}"
    done

    python3 "${SCRIPT_DIR}/qemu_uboot_booti.py" write-manifest \
        --kernel "${stage_dir}/asterinas.booti" \
        --dtb "${stage_dir}/${dtb_filename}" \
        --initrd "${stage_dir}/initramfs.cpio.gz" \
        --output "${out_dir}/artifacts.json"
    if [[ -n "${variant}" ]]; then
        sha256sum \
            "${stage_dir}/asterinas.booti" \
            "${stage_dir}/initramfs.cpio.gz" \
            "${stage_dir}/${dtb_filename}" \
            "${build_dir}/${uboot_binary}" \
            "${out_dir}/u-boot.config" \
            "${out_dir}/u-boot-commit.txt" \
            "${out_dir}/qemu-version.txt" \
            "${out_dir}/qemu-dtb-audit.json" \
            "${out_dir}/qemu-virt.source.dtb" \
            "${out_dir}/${dtb_filename}" \
            "${out_dir}/qemu-dtb-variant-audit.json" \
            "${out_dir}/boot.ext4" \
            > "${out_dir}/SHA256SUMS"
        printf 'prepared=%s\n' "${out_dir}"
        exit 0
    fi
    sha256sum \
        "${stage_dir}/asterinas.booti" \
        "${stage_dir}/initramfs.cpio.gz" \
        "${stage_dir}/${dtb_filename}" \
        "${build_dir}/${uboot_binary}" \
        "${out_dir}/u-boot.config" \
        "${out_dir}/u-boot-commit.txt" \
        "${out_dir}/qemu-version.txt" \
        "${out_dir}/qemu-dtb-audit.json" \
        "${out_dir}/boot.ext4" \
        > "${out_dir}/SHA256SUMS"
    printf 'prepared=%s\n' "${out_dir}"
    exit 0
fi

printf 'usage: %s {prepare|--print-uboot-commit|--print-payload-manifest|--check-tools|--canonical-output-dir DIR|--canonical-build-dir DIR|--verify-uboot-source DIR}\n' \
    "$0" >&2
exit 2
