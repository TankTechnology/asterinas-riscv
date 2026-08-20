#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Build and run the isolated RISC-V LTP gate."""

from __future__ import annotations

import argparse
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
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from ltp_manifest import select_manifest


REPO_ROOT = Path(__file__).resolve().parents[2]
_PROFILES_BY_SMP = {
    1: "generic-sv39-ltp-smp1",
    4: "generic-sv39-ltp-smp4",
}
_RUN_ID_RE = re.compile(r"[A-Za-z0-9._-]+")


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
        prepared_dir=target / "qemu" / f"smp{smp}",
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


def _subset_selection(repo: Path, tags: Sequence[str]):
    rootfs = repo / "target/ltp/rootfs"
    enabled = repo / "test/initramfs/src/conformance/ltp/testcases/all.txt"
    manifest = rootfs / "opt/ltp/runtest/syscalls"
    binaries = rootfs / "opt/ltp/testcases/bin"
    available = {entry.name for entry in binaries.iterdir() if entry.is_file()}
    return select_manifest(
        enabled.read_text(),
        manifest.read_text(),
        available,
        subset=tags,
    )


def package_subset(
    repo: Path,
    *,
    tags: Sequence[str],
    workspace: Path,
) -> SubsetPackage:
    """Copy the full rootfs, replace only its manifest, and pack a subset."""

    if not tags:
        raise ValueError("subset requires at least one tag")
    resolved_repo = repo.resolve()
    rootfs = resolved_repo / "target/ltp/rootfs"
    # Validate all requested tags and binaries before creating staging files.
    selection = _subset_selection(resolved_repo, tags)
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
            str(resolved_repo / "test/initramfs/src/conformance/ltp/testcases/all.txt"),
            "--runtest",
            str(rootfs / "opt/ltp/runtest/syscalls"),
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
    return SubsetPackage(staged_rootfs, staged_manifest, initramfs)


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

    run = subparsers.add_parser("run")
    run.add_argument("--kernel", type=Path, required=True)
    run.add_argument("--smp", type=int, choices=(1, 4), default=1)
    run.add_argument("--run-id", default=None)
    run.add_argument("--skip-build", action="store_true")
    run.add_argument("--baseline", action="store_true")
    run.add_argument("--boot-timeout", type=_positive_float)
    run.add_argument("--tag", action="append", default=[])
    run.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


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
        "--marker-event",
        str(paths.result_dir / "marker-event.txt"),
        "--result",
        str(paths.result_dir / "boot-result.json"),
    ]
    if boot_timeout is not None:
        command.extend(("--boot-timeout", str(boot_timeout)))
    return command


def _normalizer_command(repo: Path, paths: LtpRunPaths, commit: str, smp: int):
    return [
        sys.executable,
        str(repo / "tools/riscv/ltp_result.py"),
        "write",
        "--serial",
        str(paths.result_dir / "serial.log"),
        "--boot-result",
        str(paths.result_dir / "boot-result.json"),
        "--result",
        str(paths.result_dir / "result.json"),
        "--summary",
        str(paths.result_dir / "summary.txt"),
        "--git-commit",
        commit,
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
) -> int:
    build_script = repo / "tools/riscv/nixos/ltp/build_ltp.sh"
    if not args.skip_build:
        _print_command([str(build_script)])
    selected_initramfs = paths.initramfs
    if args.tag:
        print(
            "# validate and package subset: "
            + " ".join(shlex.quote(tag) for tag in args.tag)
        )
        selected_initramfs = paths.result_dir / "ltp-initramfs.cpio.gz"
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
    _print_command(_normalizer_command(repo, paths, "0" * 40, args.smp))
    print(f"# write {paths.result_dir / 'SHA256SUMS'}")
    return 0


def _run_gate(repo: Path, args: argparse.Namespace) -> int:
    run_id = _default_run_id() if args.run_id is None else args.run_id
    paths = run_paths(repo, run_id=run_id, smp=args.smp, kernel=args.kernel)
    profile = profile_for_smp(args.smp)
    _validate_run_paths(repo, paths)
    _require_kernel(paths.kernel, repo, dry_run=args.dry_run)
    if args.dry_run:
        return _dry_run(repo, paths, args, profile)

    if paths.result_dir.exists() or paths.result_dir.is_symlink():
        raise FileExistsError(f"result directory already exists: {paths.result_dir}")
    paths.result_dir.parent.mkdir(parents=True, exist_ok=True)
    _require_within(paths.result_dir.parent, repo / "target/ltp", "results root")
    paths.result_dir.mkdir(exist_ok=False)

    build_script = repo / "tools/riscv/nixos/ltp/build_ltp.sh"
    if not args.skip_build:
        subprocess.run([str(build_script)], cwd=repo, check=True)

    selected_initramfs = paths.initramfs
    selected_manifest = repo / "target/ltp/rootfs/opt/ltp/runtest/syscalls"
    with ExitStack() as stack:
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
            )
            selected_initramfs = paths.result_dir / "ltp-initramfs.cpio.gz"
            _publish_copy(package.initramfs, selected_initramfs)
            selected_manifest = package.manifest
        if not selected_initramfs.is_file() or selected_initramfs.stat().st_size == 0:
            raise ValueError(
                f"initramfs must be a non-empty file: {selected_initramfs}"
            )
        manifest_evidence = paths.result_dir / "selected-syscalls"
        _publish_copy(selected_manifest, manifest_evidence)

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
        commit = _git_commit(repo)
        normalized = subprocess.run(
            _normalizer_command(repo, paths, commit, args.smp),
            cwd=repo,
            check=False,
        )

        result_path = paths.result_dir / "result.json"
        checksum_inputs = (
            paths.kernel,
            selected_initramfs,
            manifest_evidence,
            paths.prepared_dir / "boot.ext4",
            paths.prepared_dir / "artifacts.json",
            paths.prepared_dir / "qemu-dtb-audit.json",
            paths.result_dir / "serial.log",
            paths.result_dir / "marker-event.txt",
            paths.result_dir / "boot-result.json",
            result_path,
            paths.result_dir / "summary.txt",
        )
        _write_sha256s(repo, paths.result_dir / "SHA256SUMS", checksum_inputs)
        if (
            qemu.returncode != 0
            or normalized.returncode != 0
            or not result_path.is_file()
        ):
            return 1
        document = json.loads(result_path.read_text())
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
        command = [str(resolved_repo / "tools/riscv/nixos/ltp/build_ltp.sh")]
        if args.skip_compile:
            command.append("--skip-compile")
        subprocess.run(command, cwd=resolved_repo, check=True)
        return 0
    if args.command == "run":
        return _run_gate(resolved_repo, args)
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
