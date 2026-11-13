#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# Real-workload timings on the board, next to the L1 micro-benchmark.
#
#   tools/riscv/perf/board_workload.sh [ssh-target]
#
# Micro-benchmarks say what one system call costs; these say what a workload a
# user actually runs costs. Each is reported as one `NAME=value unit` line so
# it can be collected the same way as the L1 numbers.
#
# Timings are taken inside the guest with CLOCK_MONOTONIC (Python), not with
# `date +%s%N` in the shell: that form silently produced a 2-hour "find" on
# this board, which is exactly the kind of number that poisons a baseline.
set -euo pipefail

TARGET=${1:-root@58.206.232.122}
PORT=${WORKLOAD_SSH_PORT:-2222}
KEY=${WORKLOAD_SSH_KEY:-$HOME/.ssh/asterinas_megrez}

ssh -i "$KEY" -p "$PORT" -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null -o BatchMode=yes "$TARGET" \
    'python3 - <<"PY"
import os, subprocess, tarfile, tempfile, time

def report(name, value, unit):
    print(f"{name}={value:.1f} {unit}")

work = tempfile.mkdtemp(prefix="wl.")
try:
    # 1. Metadata-heavy: pack and unpack 2000 small files.
    src = os.path.join(work, "src")
    os.makedirs(src)
    for i in range(2000):
        with open(os.path.join(src, f"f{i}.txt"), "w") as handle:
            handle.write("x" * 512)
    archive = os.path.join(work, "tree.tar")
    with tarfile.open(archive, "w") as tar:
        tar.add(src, arcname="src")
    out = os.path.join(work, "out")
    os.makedirs(out)
    start = time.monotonic()
    with tarfile.open(archive) as tar:
        tar.extractall(out, filter="data")
    report("tar_extract_2000_files", (time.monotonic() - start) * 1000, "ms")

    # 2. Directory traversal: what `du`, `find` and package tools pay.
    start = time.monotonic()
    files = 0
    for _root, _dirs, names in os.walk("/usr"):
        files += len(names)
    report("walk_usr_files", files, "files")
    report("walk_usr", (time.monotonic() - start) * 1000, "ms")

    # 3. Single path resolution, the unit the traversal is made of.
    iterations = 500
    start = time.monotonic()
    for _ in range(iterations):
        os.stat("/usr/bin/less")
    report("stat_cached_path", (time.monotonic() - start) * 1e6 / iterations, "us")

    # 4. The packaging path: dpkg installs a small package again.
    start = time.monotonic()
    result = subprocess.run(
        ["apt-get", "install", "--reinstall", "-y", "-q",
         "--no-install-recommends", "less"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    report("apt_reinstall_less", (time.monotonic() - start) * 1000, "ms")
    if result.returncode != 0:
        print("apt_reinstall_less_failed=1")
finally:
    subprocess.run(["rm", "-rf", work], check=False)
PY'
