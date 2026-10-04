#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
rootfs=${1:?usage: $0 ROOTFS_IMAGE ARTIFACT_DIR}
artifact_root=${2:?usage: $0 ROOTFS_IMAGE ARTIFACT_DIR}
rootfs=$(realpath "$rootfs")
artifact_root=$(realpath -m "$artifact_root")

test -f "$rootfs"
mkdir -p "$artifact_root"

for stage in 1 2 3 4; do
    run_dir="$repo_dir/target/ext4-fault-stage$stage"
    artifact_dir="$artifact_root/stage$stage"
    mkdir -p "$run_dir/logs" "$artifact_dir"
    cp "$rootfs" "$run_dir/debian-root.ext2"

    set +e
    (
        cd "$repo_dir"
        tools/docker/run_dev_container.sh -- bash -lc \
            "EXT4_JOURNAL_FAULT_STAGE=$stage \
             ASTERINAS_EXT2_DRIVE_FILE=/root/asterinas/target/ext4-fault-stage$stage/debian-root.ext2 \
             ASTERINAS_QEMU_LOG_DIR=/root/asterinas/target/ext4-fault-stage$stage/logs \
             make run_kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode \
             AUTO_TEST=ext4_regression RELEASE=1"
    ) >"$artifact_dir/qemu.log" 2>&1
    run_rc=$?
    set -e
    printf 'run_rc=%s\n' "$run_rc" >"$artifact_dir/result.txt"

    mkdir -p "$run_dir/logs/recovery"
    (
        cd "$repo_dir"
        tools/docker/run_dev_container.sh -- bash -lc \
            "ASTERINAS_EXT2_DRIVE_FILE=/root/asterinas/target/ext4-fault-stage$stage/debian-root.ext2 \
             ASTERINAS_QEMU_LOG_DIR=/root/asterinas/target/ext4-fault-stage$stage/logs/recovery \
             make run_kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode \
             AUTO_TEST=ext4_recovery_verify RELEASE=1"
    ) >"$artifact_dir/recovery.log" 2>&1

    e2fsck -fn "$run_dir/debian-root.ext2" >"$artifact_dir/e2fsck.txt" 2>&1
    if ! grep -q 'ASTERINAS_EXT4_RECOVERY_OK' "$artifact_dir/recovery.log"; then
        echo "stage $stage: recovery marker missing" >&2
        exit 1
    fi
    if grep -q 'Filesystem still has errors' "$artifact_dir/e2fsck.txt"; then
        echo "stage $stage: e2fsck reported errors" >&2
        exit 1
    fi
    sha256sum "$run_dir/debian-root.ext2" >"$artifact_dir/rootfs.sha256"
    echo "ASTERINAS_EXT4_FAULT_GATE_STAGE_OK stage=$stage"
done

echo 'ASTERINAS_EXT4_FAULT_GATE_OK stages=1,2,3,4'
