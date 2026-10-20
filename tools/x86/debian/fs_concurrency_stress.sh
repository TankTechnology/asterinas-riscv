#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Concurrency stress for the ext2/ext4 implementation.
#
# A driver starts N worker processes in the guest.  Every worker owns a
# directory and churns it with create / rewrite / unlink / rename / truncate /
# xattr operations, while all workers also create and unlink files in one
# directory they share.  Each worker keeps an on-disk manifest of the files it
# expects to exist and the SHA-256 of their contents, and periodically reads a
# random owned file back to catch inode-reuse cross-talk.
#
# When the workers finish, the driver re-reads every manifest, compares the
# directory contents with it, re-hashes every file and writes a result.  The
# host then requires:
#
#   * the in-guest verification to report ok=True,
#   * a clean shutdown, and
#   * `e2fsck -fn` to accept the image.
#
# The point is not throughput but correctness under interleaving: no deadlock
# (the boot would time out), no lost or duplicated directory entries, no
# content from another worker's file, and no leaked blocks or inodes.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: fs_concurrency_stress.sh --kernel ELF --initramfs CPIO --base-root IMG
                                --workspace DIR [OPTIONS]

Options:
  --kernel PATH       Asterinas x86_64 multiboot2 ELF (required).
  --initramfs PATH    Stage1 cpio initramfs (required).
  --base-root PATH    Debian ext2/ext4 image to copy per run (required).
  --workspace DIR     Image, manifests and transcripts.
  --workers N         Worker processes (default: 8).
  --seconds N         Seconds each worker runs (default: 45).
  --crash-after N     Kill the VM N seconds after the workers start, then
                      boot the image again and require the recovery boot to
                      reach multi-user with `e2fsck -fn` clean.  Without this
                      option the run is clean: the driver verifies contents
                      and shuts down normally.
  --timeout SECONDS   Per-boot deadline (default: 1800).
  --loglevel N        Kernel log level (default: 4, Linux numbering).
  --accel MODE        'kvm' or 'tcg' (default: kvm).
  --qemu PATH         QEMU binary or wrapper (default: qemu-system-x86_64).

Exit status is 0 only if the in-guest verification passes and `e2fsck -fn`
accepts the image after a clean shutdown.
EOF
}

die_usage() {
    printf 'error: %s\n' "$1" >&2
    usage >&2
    exit 2
}

KERNEL=""
INITRAMFS=""
BASE_ROOT=""
WORKSPACE=""
WORKERS=8
SECONDS=45
CRASH_AFTER=""
TIMEOUT=1800
LOGLEVEL=4
ACCEL="kvm"
QEMU="qemu-system-x86_64"

while (( $# > 0 )); do
    case "$1" in
        --kernel | --initramfs | --base-root | --workspace | --workers | \
            --seconds | --crash-after | --timeout | --loglevel | --accel | --qemu)
            [[ $# -ge 2 && -n "$2" ]] || die_usage "missing value for $1"
            case "$1" in
                --kernel) KERNEL="$2" ;;
                --initramfs) INITRAMFS="$2" ;;
                --base-root) BASE_ROOT="$2" ;;
                --workspace) WORKSPACE="$2" ;;
                --workers) WORKERS="$2" ;;
                --seconds) SECONDS="$2" ;;
                --crash-after) CRASH_AFTER="$2" ;;
                --timeout) TIMEOUT="$2" ;;
                --loglevel) LOGLEVEL="$2" ;;
                --accel) ACCEL="$2" ;;
                --qemu) QEMU="$2" ;;
            esac
            shift 2
            ;;
        -h | --help) usage; exit 0 ;;
        *) die_usage "unknown argument: $1" ;;
    esac
done

[[ -n "$KERNEL" && -n "$INITRAMFS" && -n "$BASE_ROOT" && -n "$WORKSPACE" ]] ||
    die_usage "--kernel, --initramfs, --base-root and --workspace are required"
[[ "$WORKERS" =~ ^[1-9][0-9]*$ ]] || die_usage "--workers must be a positive integer"
(( WORKERS <= 64 )) || die_usage "--workers must not exceed 64"
[[ "$SECONDS" =~ ^[1-9][0-9]*$ ]] || die_usage "--seconds must be a positive integer"
[[ -z "$CRASH_AFTER" || "$CRASH_AFTER" =~ ^[1-9][0-9]*$ ]] ||
    die_usage "--crash-after must be a positive integer"
[[ "$ACCEL" == "tcg" || "$ACCEL" == "kvm" ]] ||
    die_usage "--accel must be 'tcg' or 'kvm'"
for tool in debugfs e2fsck "$QEMU"; do
    command -v "$tool" >/dev/null 2>&1 || {
        printf 'error: required tool not found: %s\n' "$tool" >&2
        exit 1
    }
done
for path in "$KERNEL" "$INITRAMFS" "$BASE_ROOT" "$WORKSPACE"; do
    [[ "$path" != *$'\n'* && "$path" != *$'\r'* && "$path" != *' '* ]] ||
        die_usage "path must not contain whitespace or newlines: $path"
done
[[ -f "$KERNEL" && -f "$INITRAMFS" && -f "$BASE_ROOT" ]] ||
    die_usage "an input path does not name a file"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../../.." && pwd)"
BOOT_SCRIPT="$REPO_ROOT/tools/x86/debian/boot_debian.sh"
[[ -x "$BOOT_SCRIPT" ]] || {
    printf 'error: boot script is missing: %s\n' "$BOOT_SCRIPT" >&2
    exit 1
}

mkdir -p -- "$WORKSPACE"
IMAGE="$WORKSPACE/concurrency.ext2"
TRANSCRIPT="$WORKSPACE/boot.txt"
STRESS_PY="$WORKSPACE/stress.py"
STRESS_SH="$WORKSPACE/stress.sh"
STRESS_UNIT="$WORKSPACE/stress.service"
SHUTDOWN_SH="$WORKSPACE/shutdown.sh"
SHUTDOWN_UNIT="$WORKSPACE/shutdown.service"

cat >"$STRESS_PY" <<'PY'
#!/usr/bin/env python3
# Concurrency stress driver and worker.  Runs inside the guest.
import hashlib
import json
import os
import random
import subprocess
import sys
import time
import traceback

MANIFEST = "manifest.json"
BLOCK = 4096


def sha(data):
    return hashlib.sha256(data).hexdigest()


def payload(worker, seq, size=None):
    if size is None:
        size = 65536 if seq % 7 == 0 else BLOCK
    seed = ("%d:%d" % (worker, seq)).encode()
    block = hashlib.sha256(seed).digest()
    return (block * (size // len(block) + 1))[:size]


def fsync_path(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_file(path, data):
    with open(path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def save_manifest(path, manifest):
    tmp = path + ".new"
    with open(tmp, "w") as f:
        json.dump(manifest, f, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fsync_path(os.path.dirname(path))


def read_manifest(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def worker(wid, seconds, root):
    rng = random.Random(0x5EED0000 + wid)
    workdir = os.path.join(root, "worker-%d" % wid)
    shared = os.path.join(root, "shared")
    os.makedirs(workdir, exist_ok=True)
    manifest = {"files": {}, "shared": {}, "error": None, "ops": 0}
    problems = []
    try:
        seq = 0
        deadline = time.time() + seconds
        while time.time() < deadline:
            seq += 1
            names = list(manifest["files"])
            op = rng.choices(
                ["create", "rewrite", "unlink", "rename", "truncate", "xattr", "shared"],
                weights=[30, 20, 15, 15, 8, 6, 6],
            )[0]
            if op == "create" or not names:
                name = "f-%d-%d" % (wid, seq)
                data = payload(wid, seq)
                write_file(os.path.join(workdir, name), data)
                manifest["files"][name] = {"sha256": sha(data), "size": len(data)}
            elif op == "rewrite":
                name = rng.choice(names)
                data = payload(wid, seq)
                write_file(os.path.join(workdir, name), data)
                manifest["files"][name] = {"sha256": sha(data), "size": len(data)}
            elif op == "unlink":
                name = rng.choice(names)
                os.unlink(os.path.join(workdir, name))
                del manifest["files"][name]
                fsync_path(workdir)
            elif op == "rename":
                name = rng.choice(names)
                new_name = "r-%d-%d" % (wid, seq)
                os.rename(
                    os.path.join(workdir, name),
                    os.path.join(workdir, new_name),
                )
                manifest["files"][new_name] = manifest["files"].pop(name)
                fsync_path(workdir)
            elif op == "truncate":
                name = rng.choice(names)
                size = rng.randrange(0, BLOCK + 1)
                path = os.path.join(workdir, name)
                os.truncate(path, size)
                with open(path, "rb") as f:
                    data = f.read()
                manifest["files"][name] = {"sha256": sha(data), "size": len(data)}
            elif op == "xattr":
                name = rng.choice(names)
                key = "user.stress-%d" % wid
                value = ("%d:%d" % (wid, seq)).encode()
                path = os.path.join(workdir, name)
                os.setxattr(path, key, value)
                if os.getxattr(path, key) != value:
                    problems.append("xattr read-back mismatch on %s" % name)
            elif op == "shared":
                sname = "s-%d-%d" % (wid, seq)
                spath = os.path.join(shared, sname)
                data = payload(wid, seq, BLOCK)
                write_file(spath, data)
                if rng.random() < 0.5:
                    os.unlink(spath)
                    fsync_path(shared)
                else:
                    manifest["shared"][sname] = {
                        "sha256": sha(data),
                        "size": len(data),
                    }
            manifest["ops"] += 1
            # Read a random owned file back: inode reuse must never make a
            # file answer with another file's contents.
            if manifest["ops"] % 17 == 0 and manifest["files"]:
                check = rng.choice(list(manifest["files"]))
                entry = manifest["files"][check]
                with open(os.path.join(workdir, check), "rb") as f:
                    data = f.read()
                if len(data) != entry["size"] or sha(data) != entry["sha256"]:
                    problems.append("cross-talk on %s" % check)
    except Exception:
        manifest["error"] = traceback.format_exc()
    if problems:
        manifest["error"] = (manifest["error"] or "") + "\n".join(problems)
    save_manifest(os.path.join(workdir, MANIFEST), manifest)
    fsync_path(shared)
    print(
        "FS_CONCURRENCY_WORKER_DONE worker=%d ops=%d files=%d shared=%d error=%s"
        % (
            wid,
            manifest["ops"],
            len(manifest["files"]),
            len(manifest["shared"]),
            bool(manifest["error"]),
        ),
        flush=True,
    )
    return 1 if manifest["error"] else 0


def verify(root, workers):
    problems = []
    shared_expected = {}
    total_files = 0
    for wid in range(workers):
        workdir = os.path.join(root, "worker-%d" % wid)
        manifest = read_manifest(os.path.join(workdir, MANIFEST))
        if manifest is None:
            problems.append("worker %d: manifest missing or unreadable" % wid)
            continue
        if manifest.get("error"):
            problems.append("worker %d: %s" % (wid, manifest["error"]))
        expected = dict(manifest.get("files", {}))
        try:
            actual = set(os.listdir(workdir)) - {MANIFEST, MANIFEST + ".new"}
        except OSError as err:
            problems.append("worker %d: listdir failed: %s" % (wid, err))
            continue
        for name in sorted(set(expected) - actual):
            problems.append("worker %d: %s is missing" % (wid, name))
        for name in sorted(actual - set(expected)):
            problems.append("worker %d: unexpected entry %s" % (wid, name))
        for name in sorted(set(expected) & actual):
            entry = expected[name]
            try:
                with open(os.path.join(workdir, name), "rb") as f:
                    data = f.read()
            except OSError as err:
                problems.append("worker %d: %s unreadable: %s" % (wid, name, err))
                continue
            if len(data) != entry["size"] or sha(data) != entry["sha256"]:
                problems.append("worker %d: %s content mismatch" % (wid, name))
        shared_expected.update(manifest.get("shared", {}))
        total_files += len(expected)
    shared_dir = os.path.join(root, "shared")
    try:
        actual_shared = set(os.listdir(shared_dir))
    except OSError as err:
        problems.append("shared directory unreadable: %s" % err)
        actual_shared = set()
    for name in sorted(set(shared_expected) - actual_shared):
        problems.append("shared: %s is missing" % name)
    for name in sorted(actual_shared - set(shared_expected)):
        problems.append("shared: unexpected entry %s" % name)
    for name in sorted(set(shared_expected) & actual_shared):
        entry = shared_expected[name]
        try:
            with open(os.path.join(shared_dir, name), "rb") as f:
                data = f.read()
        except OSError as err:
            problems.append("shared: %s unreadable: %s" % (name, err))
            continue
        if len(data) != entry["size"] or sha(data) != entry["sha256"]:
            problems.append("shared: %s content mismatch" % name)
    ok = not problems
    print(
        "FS_CONCURRENCY_RESULT ok=%s workers=%d files=%d problems=%d"
        % (ok, workers, total_files, len(problems)),
        flush=True,
    )
    for problem in problems[:40]:
        print("FS_CONCURRENCY_PROBLEM %s" % problem, flush=True)
    return ok


def run(workers, seconds, root):
    os.makedirs(os.path.join(root, "shared"), exist_ok=True)
    children = []
    for wid in range(workers):
        children.append(
            subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), "worker",
                 str(wid), str(seconds), root]
            )
        )
    statuses = [child.wait() for child in children]
    ok = all(status == 0 for status in statuses) and verify(root, workers)
    result = "ok=%s workers=%d seconds=%d statuses=%s\n" % (
        ok, workers, seconds, ",".join(str(s) for s in statuses))
    with open("/opt/fs-stress/result.txt", "w") as f:
        f.write(result)
        f.flush()
        os.fsync(f.fileno())
    print("FS_CONCURRENCY_DRIVER_DONE " + result.strip(), flush=True)
    return 0 if ok else 1


def main(argv):
    if len(argv) != 5 or argv[1] not in ("run", "worker"):
        print("usage: stress.py run|worker ARG ARG ROOT", file=sys.stderr)
        return 2
    if argv[1] == "run":
        return run(int(argv[2]), int(argv[3]), argv[4])
    return worker(int(argv[2]), int(argv[3]), argv[4])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
PY

# In crash mode the workers run until the host cuts power; the loop below is
# only a safety net if the host somehow misses the VM.
HOLD=0
[[ -n "$CRASH_AFTER" ]] && HOLD=1

cat >"$STRESS_SH" <<STRESS
#!/bin/sh
# Only the stress boot runs the workload; the recovery boot skips it.
grep -q 'asterinas.fs_concurrency=run' /proc/cmdline || exit 0
echo "FS_CONCURRENCY_BEGIN"
python3 /opt/fs-stress/stress.py run $WORKERS $SECONDS /opt/fs-stress/work
rc=\$?
if [ $HOLD = 1 ]; then
    echo "FS_CONCURRENCY_HOLD"
    while true; do sleep 1; done
fi
sync
echo "FS_CONCURRENCY_EXIT \$rc"
sync
systemctl poweroff --no-block
STRESS

# The recovery boot has no stress marker, so this unit reaches multi-user,
# confirms it, and powers off.  On the stress boot it exits immediately.
cat >"$SHUTDOWN_SH" <<'SHUTDOWN'
#!/bin/sh
grep -q 'asterinas.fs_concurrency=run' /proc/cmdline && exit 0
echo "FS_CONCURRENCY_RECOVERY"
sync
systemctl poweroff --no-block
SHUTDOWN

cat >"$SHUTDOWN_UNIT" <<'UNIT'
[Unit]
Description=Filesystem concurrency recovery shutdown
After=multi-user.target

[Service]
Type=oneshot
KeyringMode=inherit
ExecStart=/opt/fs-stress/shutdown.sh
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
UNIT

cat >"$STRESS_UNIT" <<'UNIT'
[Unit]
Description=Filesystem concurrency stress
After=multi-user.target

[Service]
Type=oneshot
KeyringMode=inherit
ExecStart=/opt/fs-stress/stress.sh
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
UNIT

rm -f -- "$IMAGE"
cp --sparse=always -- "$BASE_ROOT" "$IMAGE"
{
    printf 'mkdir /opt/fs-stress\n'
    printf 'write %s /opt/fs-stress/stress.py\n' "$STRESS_PY"
    printf 'sif /opt/fs-stress/stress.py mode 0100755\n'
    printf 'write %s /opt/fs-stress/stress.sh\n' "$STRESS_SH"
    printf 'sif /opt/fs-stress/stress.sh mode 0100755\n'
    printf 'write %s /etc/systemd/system/fs-concurrency-stress.service\n' "$STRESS_UNIT"
    printf 'symlink /etc/systemd/system/multi-user.target.wants/fs-concurrency-stress.service /etc/systemd/system/fs-concurrency-stress.service\n'
    printf 'write %s /opt/fs-stress/shutdown.sh\n' "$SHUTDOWN_SH"
    printf 'sif /opt/fs-stress/shutdown.sh mode 0100755\n'
    printf 'write %s /etc/systemd/system/fs-concurrency-shutdown.service\n' "$SHUTDOWN_UNIT"
    printf 'symlink /etc/systemd/system/multi-user.target.wants/fs-concurrency-shutdown.service /etc/systemd/system/fs-concurrency-shutdown.service\n'
    printf 'rm /etc/systemd/system/multi-user.target.wants/asterinas-debian-ext4.service\n'
    printf 'rm /etc/systemd/system/asterinas-debian-ext4.service\n'
} | debugfs -w -f - "$IMAGE" >/dev/null 2>&1

if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-prepared.txt" 2>&1; then
    printf 'FAIL (prepared image is not clean; see %s)\n' \
        "$WORKSPACE/fsck-prepared.txt"
    exit 1
fi

if [[ -z "$CRASH_AFTER" ]]; then
    printf 'concurrency stress: booting (%d workers, %ds)\n' "$WORKERS" "$SECONDS"
    if ! timeout "$TIMEOUT" "$BOOT_SCRIPT" \
        --kernel "$KERNEL" \
        --initramfs "$INITRAMFS" \
        --root-image "$IMAGE" \
        --output "$TRANSCRIPT" \
        --loglevel "$LOGLEVEL" \
        --accel "$ACCEL" \
        --qemu "$QEMU" \
        --append-extra "asterinas.fs_concurrency=run" \
        --wait-for "Powering off."; then
        printf 'FAIL (the boot did not shut down; see %s)\n' "$TRANSCRIPT"
        exit 1
    fi

    if ! debugfs -R "dump /opt/fs-stress/result.txt $WORKSPACE/result.txt" \
        "$IMAGE" >/dev/null 2>&1 || [[ ! -f "$WORKSPACE/result.txt" ]]; then
        printf 'FAIL (the driver wrote no result; see %s)\n' "$TRANSCRIPT"
        exit 1
    fi

    if ! grep -q '^ok=True' "$WORKSPACE/result.txt"; then
        printf 'FAIL (in-guest verification): %s\n' "$(cat "$WORKSPACE/result.txt")"
        exit 1
    fi
else
    printf 'concurrency stress: crash boot (%d workers, power cut after %ss)\n' \
        "$WORKERS" "$CRASH_AFTER"
    mapfile -t argv < <("$BOOT_SCRIPT" --print-argv \
        --kernel "$KERNEL" \
        --initramfs "$INITRAMFS" \
        --root-image "$IMAGE" \
        --output "$TRANSCRIPT" \
        --loglevel "$LOGLEVEL" \
        --accel "$ACCEL" \
        --qemu "$QEMU" \
        --append-extra "asterinas.fs_concurrency=run" \
        --wait-for "Powering off.")
    "${argv[@]}" >"$TRANSCRIPT" 2>&1 &
    qemu_pid=$!

    waited=0
    while (( waited < TIMEOUT )); do
        grep -aq "FS_CONCURRENCY_BEGIN" "$TRANSCRIPT" && break
        if ! kill -0 "$qemu_pid" 2>/dev/null; then
            break
        fi
        sleep 1
        waited=$((waited + 1))
    done
    if ! grep -aq "FS_CONCURRENCY_BEGIN" "$TRANSCRIPT"; then
        printf 'FAIL (the workers never started; see %s)\n' "$TRANSCRIPT"
        kill -9 "$qemu_pid" 2>/dev/null || true
        wait "$qemu_pid" 2>/dev/null || true
        exit 1
    fi

    sleep "$CRASH_AFTER"
    kill -9 "$qemu_pid" 2>/dev/null || true
    wait "$qemu_pid" 2>/dev/null || true

    printf 'concurrency stress: recovery boot\n'
    if ! timeout "$TIMEOUT" "$BOOT_SCRIPT" \
        --kernel "$KERNEL" \
        --initramfs "$INITRAMFS" \
        --root-image "$IMAGE" \
        --output "$WORKSPACE/recovery.txt" \
        --loglevel "$LOGLEVEL" \
        --accel "$ACCEL" \
        --qemu "$QEMU" \
        --wait-for "Powering off."; then
        printf 'FAIL (the recovery boot did not shut down; see %s)\n' \
            "$WORKSPACE/recovery.txt"
        exit 1
    fi
    if ! grep -aq "FS_CONCURRENCY_RECOVERY" "$WORKSPACE/recovery.txt"; then
        printf 'FAIL (the recovery boot never reached multi-user; see %s)\n' \
            "$WORKSPACE/recovery.txt"
        exit 1
    fi
fi

if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-after.txt" 2>&1; then
    printf 'FAIL (e2fsck -fn; see %s)\n' "$WORKSPACE/fsck-after.txt"
    exit 1
fi

if [[ -z "$CRASH_AFTER" ]]; then
    printf 'PASS (%s)\n' "$(cat "$WORKSPACE/result.txt")"
else
    printf 'PASS (recovered from a power cut after %ss of concurrent churn)\n' \
        "$CRASH_AFTER"
fi
rm -f -- "$IMAGE"
