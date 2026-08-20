#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Build and run the isolated RISC-V LTP gate."""

from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from collections import Counter
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from ltp_manifest import select_manifest
from ltp_package import publish_package_identity, validate_package_identity
from ltp_suite import LtpSuite, suite_by_name, suite_names


REPO_ROOT = Path(__file__).resolve().parents[2]
_PROFILES_BY_SMP = {
    1: "generic-sv39-ltp-smp1",
    4: "generic-sv39-ltp-smp4",
}
_RUN_ID_RE = re.compile(r"[A-Za-z0-9._-]+")
_PROGRESS_RUN_RE = re.compile(r"^\[RUN\] (\d+) ([^\s]+)$")
_PROGRESS_VERDICT_RE = re.compile(
    r"^\[(PASS|FAIL|CONF|CRASH|TIMEOUT)\] ([^\s]+)$"
)


@dataclass(frozen=True)
class LtpRunPaths:
    """All caller-selected or gate-owned top-level paths for one run."""

    prepared_dir: Path
    result_dir: Path
    initramfs: Path
    kernel: Path

    def all_paths(self) -> tuple[Path, Path, Path, Path]:
        return self.prepared_dir, self.result_dir, self.initramfs, self.kernel


@dataclass(frozen=True)
class SubsetPackage:
    """A staged rootfs and its run-specific initramfs."""

    rootfs: Path
    manifest: Path
    unavailable: Path
    initramfs: Path


def profile_for_smp(smp: int) -> str:
    try:
        return _PROFILES_BY_SMP[smp]
    except KeyError as error:
        raise ValueError("SMP must be 1 or 4") from error


def run_paths(
    repo: Path,
    *,
    run_id: str,
    smp: int,
    kernel: Path | None = None,
) -> LtpRunPaths:
    if _RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError(
            "run id must contain only letters, digits, dot, underscore, or hyphen"
        )
    profile_for_smp(smp)
    resolved_repo = repo.resolve()
    target = resolved_repo / "target" / "ltp"
    return LtpRunPaths(
        prepared_dir=target / "qemu" / f"smp{smp}" / run_id,
        result_dir=target / "results" / run_id,
        initramfs=target / "ltp-initramfs.cpio.gz",
        kernel=(
            resolved_repo / "target/osdk/aster-kernel-osdk-bin.Image"
            if kernel is None
            else kernel.resolve()
        ),
    )


def exit_code(*, infrastructure_passed: bool, ltp_passed: bool, baseline: bool) -> int:
    if not infrastructure_passed:
        return 1
    return 0 if baseline or ltp_passed else 1


def _tree_entries(root: Path) -> tuple[Path, ...]:
    return (
        root,
        *sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix()),
    )


def tree_sha256(root: Path) -> str:
    """Hash paths, file bytes, modes, and symlink targets in one tree."""

    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"tree root must be a real directory: {root}")
    digest = hashlib.sha256()
    for path in _tree_entries(root):
        relative = "." if path == root else path.relative_to(root).as_posix()
        metadata = path.lstat()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(f"{stat.S_IMODE(metadata.st_mode):o}".encode())
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(b"link\0")
            digest.update(os.readlink(path).encode())
        elif path.is_file():
            digest.update(b"file\0")
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
        elif path.is_dir():
            digest.update(b"directory\0")
        else:
            digest.update(f"special:{metadata.st_mode}:{metadata.st_rdev}".encode())
        digest.update(b"\0")
    return digest.hexdigest()


def _pack_initramfs(rootfs: Path, output: Path) -> None:
    names = [
        "." if path == rootfs else f"./{path.relative_to(rootfs).as_posix()}"
        for path in _tree_entries(rootfs)
    ]
    completed = subprocess.run(
        ["cpio", "--create", "--format=newc", "--quiet"],
        cwd=rootfs,
        input=("\n".join(names) + "\n").encode(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as archive:
        archive.write(gzip.compress(completed.stdout, compresslevel=9, mtime=0))


@contextmanager
def _package_lock(repo: Path) -> Iterator[None]:
    """Serializes publication and snapshotting of shared LTP package files."""

    target = repo.resolve() / "target/ltp"
    target.mkdir(parents=True, exist_ok=True)
    lock_path = target / "package.lock"
    descriptor = os.open(
        lock_path,
        os.O_CLOEXEC | os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR,
        0o600,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _validate_packaged_suite(repo: Path, suite: LtpSuite) -> tuple[Path, Path]:
    """Verifies that packaged runtime inputs exactly match a closed suite."""

    rootfs = repo / "target/ltp/rootfs"
    runtest = repo / "target/ltp/src/runtest/syscalls"
    manifest = rootfs / "opt/ltp/runtest/syscalls"
    binaries = rootfs / "opt/ltp/testcases/bin"
    unavailable = repo / "target/ltp/unavailable-tests.json"
    initramfs = repo / "target/ltp/ltp-initramfs.cpio.gz"
    identity = repo / "target/ltp/package.json"
    for path, name in (
        (suite.enabled, "suite manifest"),
        (runtest, "upstream runtest manifest"),
        (manifest, "packaged manifest"),
        (unavailable, "unavailable evidence"),
        (initramfs, "packaged initramfs"),
        (identity, "package identity"),
    ):
        if not path.is_file():
            raise ValueError(f"{name} is missing: {path}")
    if not binaries.is_dir():
        raise ValueError(f"packaged binary directory is missing: {binaries}")

    available = {entry.name for entry in binaries.iterdir() if entry.is_file()}
    selection = select_manifest(
        suite.enabled.read_text(),
        runtest.read_text(),
        available,
    )
    if len(selection.lines) != suite.expected_selected:
        raise ValueError(
            f"suite expects {suite.expected_selected} selected tests, "
            f"got {len(selection.lines)}"
        )
    if manifest.read_text().splitlines() != list(selection.lines):
        raise ValueError("packaged manifest does not match the selected suite")
    if len(selection.unavailable) != suite.expected_unavailable:
        raise ValueError(
            f"suite expects {suite.expected_unavailable} unavailable tests, "
            f"got {len(selection.unavailable)}"
        )
    expected_unavailable = [
        {"name": item.name, "reason": item.reason}
        for item in selection.unavailable
    ]
    if json.loads(unavailable.read_text()) != expected_unavailable:
        raise ValueError("unavailable evidence does not match the selected suite")
    validate_package_identity(
        suite=suite.name,
        initramfs=initramfs,
        manifest=manifest,
        unavailable=unavailable,
        identity=identity,
    )
    return manifest, unavailable


def _subset_selection(repo: Path, tags: Sequence[str], suite: LtpSuite):
    rootfs = repo / "target/ltp/rootfs"
    runtest = repo / "target/ltp/src/runtest/syscalls"
    binaries = rootfs / "opt/ltp/testcases/bin"
    available = {entry.name for entry in binaries.iterdir() if entry.is_file()}
    return select_manifest(
        suite.enabled.read_text(),
        runtest.read_text(),
        available,
        subset=tags,
    )


def package_subset(
    repo: Path,
    *,
    tags: Sequence[str],
    workspace: Path,
    suite: LtpSuite | None = None,
) -> SubsetPackage:
    """Copy the full rootfs, replace only its manifest, and pack a subset."""

    if not tags:
        raise ValueError("subset requires at least one tag")
    resolved_repo = repo.resolve()
    selected_suite = (
        suite_by_name(resolved_repo, "syscalls") if suite is None else suite
    )
    rootfs = resolved_repo / "target/ltp/rootfs"
    # Validate all requested tags and binaries before creating staging files.
    selection = _subset_selection(resolved_repo, tags, selected_suite)
    workspace.mkdir(parents=True, exist_ok=True)
    if any(workspace.iterdir()):
        raise FileExistsError(f"subset workspace is not empty: {workspace}")

    before = tree_sha256(rootfs)
    staged_rootfs = workspace / "rootfs"
    staged_manifest = staged_rootfs / "opt/ltp/runtest/syscalls"
    unavailable = workspace / "unavailable-tests.json"
    initramfs = workspace / "ltp-initramfs.cpio.gz"
    try:
        shutil.copytree(rootfs, staged_rootfs, symlinks=True)
        staged_manifest.unlink()
        command = [
            sys.executable,
            str(Path(__file__).with_name("ltp_manifest.py")),
            "select",
            "--enabled",
            str(selected_suite.enabled),
            "--runtest",
            str(resolved_repo / "target/ltp/src/runtest/syscalls"),
            "--bin-dir",
            str(staged_rootfs / "opt/ltp/testcases/bin"),
            "--output",
            str(staged_manifest),
            "--unavailable-output",
            str(unavailable),
        ]
        for tag in tags:
            command.extend(("--tag", tag))
        subprocess.run(command, cwd=resolved_repo, check=True)
        if staged_manifest.read_text().splitlines() != list(selection.lines):
            raise RuntimeError("staged subset manifest changed during selection")
        _pack_initramfs(staged_rootfs, initramfs)
    finally:
        if tree_sha256(rootfs) != before:
            raise RuntimeError("full LTP rootfs changed during subset packaging")
    return SubsetPackage(staged_rootfs, staged_manifest, unavailable, initramfs)


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return parsed


def _default_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--skip-compile", action="store_true")
    build.add_argument("--suite", choices=suite_names(), default="syscalls")
    status = subparsers.add_parser("status")
    status.add_argument("--run-id", required=True)

    run = subparsers.add_parser("run")
    run.add_argument("--kernel", type=Path, required=True)
    run.add_argument("--smp", type=int, choices=(1, 4), default=4)
    run.add_argument("--suite", choices=suite_names(), default="syscalls")
    run.add_argument("--run-id", default=None)
    run.add_argument("--skip-build", action="store_true")
    run.add_argument("--baseline", action="store_true")
    run.add_argument("--boot-timeout", type=_positive_float)
    run.add_argument(
        "--source-commit",
        default=os.environ.get("ASTERINAS_SOURCE_COMMIT"),
        help="full Git object ID (needed when containerized worktree metadata is absent)",
    )
    run.add_argument("--tag", action="append", default=[])
    run.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def _show_status(repo: Path, run_id: str) -> int:
    if _RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError(
            "run id must contain only letters, digits, dot, underscore, or hyphen"
        )
    result_dir = repo / "target/ltp/results" / run_id
    _require_within(result_dir, repo / "target/ltp", "result directory")
    manifest = result_dir / "manifest.txt"
    if not manifest.is_file():
        manifest = result_dir / "selected-syscalls"
    progress = result_dir / "progress.log"
    if not manifest.is_file() or not progress.is_file():
        raise FileNotFoundError(f"run has no live progress evidence: {run_id}")

    expected = sum(1 for line in manifest.read_text().splitlines() if line.strip())
    lines = progress.read_text(errors="replace").replace("\r", "").splitlines()
    runs = [match for line in lines if (match := _PROGRESS_RUN_RE.fullmatch(line))]
    verdicts = [
        match for line in lines if (match := _PROGRESS_VERDICT_RE.fullmatch(line))
    ]
    counts = Counter(match.group(1).lower() for match in verdicts)
    completed_names = {match.group(2) for match in verdicts}
    current = "-"
    if runs and runs[-1].group(2) not in completed_names:
        current = runs[-1].group(2)
    if "__LTP_GATE_DONE__" in lines:
        state = "COMPLETE"
    elif any(line.startswith("[BROK] LTP runner") for line in lines):
        state = "BROKEN"
    else:
        state = "RUNNING"
    print(
        f"state={state} completed={len(verdicts)}/{expected} current={current}"
    )
    print(
        f"pass={counts['pass']} fail={counts['fail']} conf={counts['conf']} "
        f"crash={counts['crash']} timeout={counts['timeout']}"
    )
    return 0


def _require_within(path: Path, root: Path, name: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"{name} must resolve below {root}")
    return resolved


def _require_kernel(kernel: Path, repo: Path, *, dry_run: bool) -> Path:
    resolved = _require_within(kernel, repo, "kernel")
    if not dry_run and (not resolved.is_file() or resolved.stat().st_size == 0):
        raise ValueError(f"kernel must be a non-empty file: {resolved}")
    return resolved


def _qemu_environment(repo: Path, paths: LtpRunPaths, profile: str, initramfs: Path):
    environment = os.environ.copy()
    cache = repo / "target/ltp/qemu/cache"
    environment.update(
        {
            "QEMU_UBOOT_PROFILE": profile,
            "QEMU_UBOOT_OUT_DIR": str(paths.prepared_dir),
            "QEMU_UBOOT_CACHE_DIR": str(cache),
            "QEMU_UBOOT_SOURCE_DIR": str(cache / "u-boot"),
            "QEMU_UBOOT_BUILD_DIR": str(cache / "u-boot-build"),
            "ASTERINAS_RISCV_BOOTI": str(paths.kernel),
            "ASTERINAS_INITRAMFS": str(initramfs),
        }
    )
    return environment


def _qemu_command(
    repo: Path,
    paths: LtpRunPaths,
    profile: str,
    *,
    boot_timeout: float | None,
) -> list[str]:
    cache = repo / "target/ltp/qemu/cache"
    command = [
        sys.executable,
        str(repo / "tools/riscv/qemu_uboot_booti.py"),
        "run",
        "--profile",
        profile,
        "--uboot",
        str(cache / "u-boot-build/u-boot"),
        "--boot-disk",
        str(paths.prepared_dir / "boot.ext4"),
        "--manifest",
        str(paths.prepared_dir / "artifacts.json"),
        "--dtb-audit",
        str(paths.prepared_dir / "qemu-dtb-audit.json"),
        "--serial-log",
        str(paths.result_dir / "serial.log"),
        "--progress-log",
        str(paths.result_dir / "progress.log"),
        "--marker-event",
        str(paths.result_dir / "marker-event.txt"),
        "--result",
        str(paths.result_dir / "boot-result.json"),
    ]
    if boot_timeout is not None:
        command.extend(("--boot-timeout", str(boot_timeout)))
    return command


def _normalizer_command(
    repo: Path,
    paths: LtpRunPaths,
    commit: str,
    smp: int,
    suite: LtpSuite,
):
    return [
        sys.executable,
        str(repo / "tools/riscv/ltp_result.py"),
        "write",
        "--serial",
        str(paths.result_dir / "serial.log"),
        "--boot-result",
        str(paths.result_dir / "boot-result.json"),
        "--manifest",
        str(paths.result_dir / "manifest.txt"),
        "--result",
        str(paths.result_dir / "result.json"),
        "--summary",
        str(paths.result_dir / "summary.txt"),
        "--git-commit",
        commit,
        "--suite",
        suite.name,
        "--smp",
        str(smp),
    ]


def _print_command(
    command: Sequence[str], *, environment: dict[str, str] | None = None
) -> None:
    if environment is None:
        print(shlex.join(command))
        return
    names = (
        "QEMU_UBOOT_PROFILE",
        "QEMU_UBOOT_OUT_DIR",
        "QEMU_UBOOT_CACHE_DIR",
        "QEMU_UBOOT_SOURCE_DIR",
        "QEMU_UBOOT_BUILD_DIR",
        "ASTERINAS_RISCV_BOOTI",
        "ASTERINAS_INITRAMFS",
    )
    assignments = " ".join(f"{name}={shlex.quote(environment[name])}" for name in names)
    print(f"{assignments} {shlex.join(command)}")


def _publish_copy(source: Path, destination: Path) -> None:
    with source.open("rb") as input_file, destination.open("xb") as output_file:
        shutil.copyfileobj(input_file, output_file)
        output_file.flush()
        os.fsync(output_file.fileno())


def _git_commit(repo: Path) -> str:
    completed = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={repo.resolve()}",
            "rev-parse",
            "HEAD",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    commit = completed.stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError("git rev-parse did not return a full object id")
    return commit


def _source_commit(repo: Path, explicit: str | None) -> str:
    if explicit is None:
        return _git_commit(repo)
    if re.fullmatch(r"[0-9a-f]{40}", explicit) is None:
        raise ValueError("source commit must be a full lowercase Git object ID")
    return explicit


def _write_sha256s(repo: Path, output: Path, candidates: Sequence[Path]) -> None:
    lines: list[str] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if not candidate.is_file():
            continue
        resolved = _require_within(candidate, repo, "checksum input")
        if resolved in seen:
            continue
        seen.add(resolved)
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        relative = resolved.relative_to(repo.resolve()).as_posix()
        lines.append(f"{digest}  {relative}\n")
    with output.open("x", encoding="utf-8") as checksum_file:
        checksum_file.writelines(lines)
        checksum_file.flush()
        os.fsync(checksum_file.fileno())


def _prepared_artifact_paths(
    prepared_dir: Path,
    document: Mapping[str, object],
) -> tuple[Path, ...]:
    """Verify and return the run-owned payloads named by a result document."""

    artifacts = document.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise ValueError("result artifacts must be an object")
    candidates = (
        (prepared_dir / "fs-root/asterinas.booti", "kernel_sha256"),
        (prepared_dir / "fs-root/initramfs.cpio.gz", "initrd_sha256"),
        (prepared_dir / "fs-root/qemu-virt.dtb", "dtb_sha256"),
        (prepared_dir / "boot.ext4", "boot_disk_sha256"),
    )
    for path, identity in candidates:
        expected = artifacts.get(identity)
        if (
            not isinstance(expected, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected) is None
        ):
            raise ValueError(f"{identity} must be a lowercase SHA-256")
        if not path.is_file():
            raise ValueError(f"prepared artifact is missing: {path}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"prepared artifact does not match {identity}: {path}")
    return tuple(path for path, _ in candidates)


def _run_evidence_candidates(
    paths: LtpRunPaths,
) -> tuple[Path, ...]:
    """Return every run-owned evidence path that may exist after execution."""

    final_checksums = paths.result_dir / "SHA256SUMS"
    return tuple(
        sorted(
            (
                candidate
                for root in (paths.prepared_dir, paths.result_dir)
                if root.is_dir()
                for candidate in root.rglob("*")
                if candidate.is_file() and candidate != final_checksums
            ),
            key=lambda path: path.as_posix(),
        )
    )


def _validate_run_paths(repo: Path, paths: LtpRunPaths) -> None:
    owned = repo / "target/ltp"
    _require_within(owned, repo, "LTP target root")
    if paths.result_dir.is_symlink():
        raise ValueError("result directory must not be a symlink")
    for name, path in (
        ("prepared directory", paths.prepared_dir),
        ("result directory", paths.result_dir),
        ("initramfs", paths.initramfs),
        ("LTP build directory", owned / "build"),
        ("LTP rootfs", owned / "rootfs"),
        ("QEMU cache", owned / "qemu/cache"),
    ):
        _require_within(path, owned, name)


def _dry_run(
    repo: Path,
    paths: LtpRunPaths,
    args: argparse.Namespace,
    profile: str,
    suite: LtpSuite,
) -> int:
    build_script = repo / "tools/riscv/nixos/ltp/build_ltp.sh"
    if not args.skip_build:
        _print_command([str(build_script), "--suite", suite.name])
    selected_initramfs = paths.result_dir / "ltp-initramfs.cpio.gz"
    if args.tag:
        print(
            "# validate and package subset: "
            + " ".join(shlex.quote(tag) for tag in args.tag)
        )
    else:
        print("# validate and snapshot the complete named suite")
    environment = _qemu_environment(repo, paths, profile, selected_initramfs)
    _print_command(
        [str(repo / "tools/riscv/prepare_qemu_uboot_booti.sh"), "prepare"],
        environment=environment,
    )
    _print_command(
        _qemu_command(
            repo,
            paths,
            profile,
            boot_timeout=args.boot_timeout,
        )
    )
    _print_command(_normalizer_command(repo, paths, "0" * 40, args.smp, suite))
    print(f"# write {paths.result_dir / 'SHA256SUMS'}")
    return 0


def _run_gate(repo: Path, args: argparse.Namespace) -> int:
    suite = suite_by_name(repo, args.suite)
    run_id = _default_run_id() if args.run_id is None else args.run_id
    paths = run_paths(repo, run_id=run_id, smp=args.smp, kernel=args.kernel)
    profile = profile_for_smp(args.smp)
    _validate_run_paths(repo, paths)
    _require_kernel(paths.kernel, repo, dry_run=args.dry_run)
    if args.dry_run:
        return _dry_run(repo, paths, args, profile, suite)

    commit = _source_commit(repo, args.source_commit)

    if paths.result_dir.exists() or paths.result_dir.is_symlink():
        raise FileExistsError(f"result directory already exists: {paths.result_dir}")
    paths.result_dir.parent.mkdir(parents=True, exist_ok=True)
    _require_within(paths.result_dir.parent, repo / "target/ltp", "results root")
    paths.result_dir.mkdir(exist_ok=False)

    selected_initramfs = paths.result_dir / "ltp-initramfs.cpio.gz"
    manifest_evidence = paths.result_dir / "manifest.txt"
    unavailable_evidence = paths.result_dir / "unavailable-tests.json"
    package_identity_evidence = paths.result_dir / "package.json"
    with ExitStack() as stack:
        stack.enter_context(_package_lock(repo))
        build_script = repo / "tools/riscv/nixos/ltp/build_ltp.sh"
        if not args.skip_build:
            build_environment = os.environ.copy()
            build_environment["ASTERINAS_LTP_PACKAGE_LOCK_HELD"] = "1"
            subprocess.run(
                [str(build_script), "--suite", suite.name],
                cwd=repo,
                env=build_environment,
                check=True,
            )

        selected_manifest, selected_unavailable = _validate_packaged_suite(
            repo, suite
        )
        package_initramfs = paths.initramfs
        if args.tag:
            build_root = repo / "target/ltp/build"
            build_root.mkdir(parents=True, exist_ok=True)
            workspace_name = stack.enter_context(
                tempfile.TemporaryDirectory(prefix="subset-", dir=build_root)
            )
            package = package_subset(
                repo,
                tags=args.tag,
                workspace=Path(workspace_name),
                suite=suite,
            )
            package_initramfs = package.initramfs
            selected_manifest = package.manifest
            selected_unavailable = package.unavailable
        _publish_copy(package_initramfs, selected_initramfs)
        _publish_copy(selected_manifest, manifest_evidence)
        _publish_copy(selected_unavailable, unavailable_evidence)
        publish_package_identity(
            suite=suite.name,
            initramfs=selected_initramfs,
            manifest=manifest_evidence,
            unavailable=unavailable_evidence,
            output=package_identity_evidence,
        )

    if not selected_initramfs.is_file() or selected_initramfs.stat().st_size == 0:
        raise ValueError(f"initramfs must be a non-empty file: {selected_initramfs}")

    environment = _qemu_environment(
        repo,
        paths,
        profile,
        selected_initramfs,
    )
    subprocess.run(
        [str(repo / "tools/riscv/prepare_qemu_uboot_booti.sh"), "prepare"],
        cwd=repo,
        env=environment,
        check=True,
    )
    qemu = subprocess.run(
        _qemu_command(
            repo,
            paths,
            profile,
            boot_timeout=args.boot_timeout,
        ),
        cwd=repo,
        check=False,
    )
    normalized = subprocess.run(
        _normalizer_command(repo, paths, commit, args.smp, suite),
        cwd=repo,
        check=False,
    )

    result_path = paths.result_dir / "result.json"
    checksum_inputs = _run_evidence_candidates(paths)
    if (
        qemu.returncode != 0
        or normalized.returncode != 0
        or not result_path.is_file()
    ):
        _write_sha256s(
            repo,
            paths.result_dir / "SHA256SUMS",
            checksum_inputs,
        )
        return 1
    document = json.loads(result_path.read_text())
    _prepared_artifact_paths(paths.prepared_dir, document)
    _write_sha256s(repo, paths.result_dir / "SHA256SUMS", checksum_inputs)
    infrastructure_passed = document.get("infrastructure_passed") is True
    ltp_passed = document.get("ltp_passed") is True
    return exit_code(
        infrastructure_passed=infrastructure_passed,
        ltp_passed=ltp_passed,
        baseline=args.baseline,
    )


def main(argv: Sequence[str] | None = None, *, repo: Path = REPO_ROOT) -> int:
    args = _parse_args(argv)
    resolved_repo = repo.resolve()
    if args.command == "build":
        command = [
            str(resolved_repo / "tools/riscv/nixos/ltp/build_ltp.sh"),
            "--suite",
            args.suite,
        ]
        if args.skip_compile:
            command.append("--skip-compile")
        subprocess.run(command, cwd=resolved_repo, check=True)
        return 0
    if args.command == "status":
        return _show_status(resolved_repo, args.run_id)
    if args.command == "run":
        return _run_gate(resolved_repo, args)
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
