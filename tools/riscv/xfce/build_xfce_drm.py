#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Build an Xfce initramfs that uses Asterinas DRM and Mesa virgl."""

from __future__ import annotations

import argparse
import gzip
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
DEFAULT_BASE = (
    REPO.parent
    / "asterinas-riscv"
    / "target/qemu-uboot/systemd-desktop-xfce-initramfs.cpio"
)
DEFAULT_RUNTIME = REPO / "target/m19/rootfs"
BUILD = REPO / "target/xfce-drm"
ROOTFS = BUILD / "rootfs"
INITRAMFS = BUILD / "xfce-drm-initramfs.cpio.gz"
STAGE1_INITRAMFS = BUILD / "stage1-initramfs.cpio"
BOOT_DISK = BUILD / "boot.ext4"
ROOT_DISK = BUILD / "root.ext2"
MULTIARCH_LIB = Path("usr/lib/riscv64-linux-gnu")


def run(argv: list[str], **kwargs: object) -> None:
    subprocess.run(argv, check=True, **kwargs)


def elf_dependencies(path: Path) -> list[str]:
    result = subprocess.run(
        ["readelf", "-d", path],
        check=False,
        capture_output=True,
        text=True,
    )
    dependencies = []
    for line in result.stdout.splitlines():
        if "NEEDED" in line and "Shared library:" in line:
            dependencies.append(line.split("[", 1)[1].split("]", 1)[0])
    return dependencies


def runtime_path(runtime: Path, relative: Path) -> Path:
    return runtime / relative


def find_library(runtime: Path, soname: str) -> Path | None:
    for directory in (
        MULTIARCH_LIB,
        Path("usr/lib"),
        Path("lib/riscv64-linux-gnu"),
        Path("lib"),
    ):
        candidate = runtime_path(runtime, directory / soname)
        if candidate.exists():
            return candidate.relative_to(runtime)
    return None


def collect_elf_closure(runtime: Path, seeds: list[Path]) -> set[Path]:
    collected: set[Path] = set()
    visited: set[Path] = set()

    def visit(relative: Path) -> None:
        source = runtime_path(runtime, relative)
        if not source.exists() or relative in visited:
            return
        visited.add(relative)
        collected.add(relative)

        if source.is_symlink():
            resolved = source.resolve().relative_to(runtime.resolve())
            visit(resolved)
            return

        for soname in elf_dependencies(source):
            dependency = find_library(runtime, soname)
            if dependency is not None:
                visit(dependency)

    for seed in seeds:
        visit(seed)
    return collected


def copy_entry(runtime: Path, rootfs: Path, relative: Path) -> None:
    source = runtime_path(runtime, relative)
    destination = rootfs / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        destination.unlink()
    if source.is_symlink():
        destination.symlink_to(os.readlink(source))
    else:
        shutil.copy2(source, destination)


def install_drm_runtime(runtime: Path, rootfs: Path) -> None:
    xorg_root = runtime / "usr/lib/xorg"
    seeds = [Path("usr/lib/xorg/Xorg")]
    seeds.extend(path.relative_to(runtime) for path in xorg_root.rglob("*.so"))

    runtime_seeds = [
        MULTIARCH_LIB / "dri/libdril_dri.so",
        MULTIARCH_LIB / "gbm/dri_gbm.so",
        MULTIARCH_LIB / "libEGL.so.1",
        MULTIARCH_LIB / "libEGL_mesa.so.0",
        MULTIARCH_LIB / "libgbm.so.1",
        MULTIARCH_LIB / "libGL.so.1",
        MULTIARCH_LIB / "libGLX_mesa.so.0",
        MULTIARCH_LIB / "libGLESv2.so.2",
    ]
    seeds.extend(seed for seed in runtime_seeds if (runtime / seed).exists())

    closure = collect_elf_closure(runtime, seeds)
    for relative in sorted(closure):
        copy_entry(runtime, rootfs, relative)

    shutil.copytree(xorg_root, rootfs / "usr/lib/xorg", dirs_exist_ok=True, symlinks=True)
    shutil.copy2(runtime / "usr/bin/Xorg", rootfs / "usr/bin/Xorg")

    for relative_dir in (
        MULTIARCH_LIB / "dri",
        MULTIARCH_LIB / "gbm",
        Path("usr/share/glvnd"),
        Path("usr/share/drirc.d"),
    ):
        source = runtime / relative_dir
        if source.exists():
            shutil.copytree(
                source,
                rootfs / relative_dir,
                dirs_exist_ok=True,
                symlinks=True,
            )


def install_x11_ready_probe(runtime: Path, rootfs: Path) -> None:
    libx11 = runtime / MULTIARCH_LIB / "libX11.so.6"
    output = rootfs / "usr/bin/x11-ready"
    run(
        [
            "riscv64-linux-gnu-gcc",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            f"-Wl,-rpath-link,{runtime / MULTIARCH_LIB}",
            "-o",
            str(output),
            str(Path(__file__).with_name("x11_ready.c")),
            str(libx11),
        ]
    )


def extract_base(base: Path, rootfs: Path) -> None:
    shutil.rmtree(rootfs, ignore_errors=True)
    rootfs.mkdir(parents=True)
    with base.open("rb") as archive:
        run(["cpio", "-id", "--quiet"], cwd=rootfs, stdin=archive)


def pack_initramfs(rootfs: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        find_process = subprocess.Popen(
            ["find", ".", "-print0"], cwd=rootfs, stdout=subprocess.PIPE
        )
        cpio_process = subprocess.Popen(
            ["cpio", "-o", "-H", "newc", "--null", "--quiet"],
            cwd=rootfs,
            stdin=find_process.stdout,
            stdout=subprocess.PIPE,
        )
        assert find_process.stdout is not None
        find_process.stdout.close()
        assert cpio_process.stdout is not None
        with gzip.open(temporary_path, "wb", compresslevel=6) as compressed:
            shutil.copyfileobj(cpio_process.stdout, compressed)
        if cpio_process.wait() != 0 or find_process.wait() != 0:
            raise RuntimeError("failed to pack the Xfce DRM initramfs")
        temporary_path.replace(output)
    finally:
        temporary_path.unlink(missing_ok=True)


def pack_stage1() -> None:
    with tempfile.TemporaryDirectory() as stage_name:
        stage = Path(stage_name)
        (stage / "dev").mkdir()
        run(
            [
                "riscv64-linux-gnu-gcc",
                "-O2",
                "-static",
                "-no-pie",
                "-fno-stack-protector",
                "-o",
                str(stage / "init"),
                str(Path(__file__).with_name("stage1.c")),
            ]
        )
        with STAGE1_INITRAMFS.open("wb") as archive:
            find_process = subprocess.Popen(
                ["find", ".", "-print0"], cwd=stage, stdout=subprocess.PIPE
            )
            cpio_process = subprocess.Popen(
                ["cpio", "-o", "-H", "newc", "--null", "--quiet"],
                cwd=stage,
                stdin=find_process.stdout,
                stdout=archive,
            )
            assert find_process.stdout is not None
            find_process.stdout.close()
            if cpio_process.wait() != 0 or find_process.wait() != 0:
                raise RuntimeError("failed to pack the stage-1 initramfs")


def pack_root_disk(rootfs: Path) -> None:
    run(["truncate", "-s", "768M", str(ROOT_DISK)])
    run(["mkfs.ext2", "-q", "-F", "-d", str(rootfs), str(ROOT_DISK)])


def pack_boot_disk(initramfs: Path) -> None:
    kernel = REPO / "target/osdk/aster-kernel-osdk-bin.Image"
    dtb = REPO / "target/drm-m19/qemu-virt.dtb"
    uboot = REPO / "target/drm-m19/u-boot"
    for path in (kernel, dtb, uboot):
        if not path.exists():
            raise SystemExit(f"missing boot artifact: {path}")

    BUILD.mkdir(parents=True, exist_ok=True)
    shutil.copy2(uboot, BUILD / "u-boot")
    with tempfile.TemporaryDirectory() as stage_name:
        stage = Path(stage_name)
        shutil.copy2(kernel, stage / "asterinas.booti")
        shutil.copy2(dtb, stage / "qemu-virt.dtb")
        shutil.copy2(initramfs, stage / "initramfs.cpio.gz")
        size_mb = 128
        run(["truncate", "-s", f"{size_mb}M", str(BOOT_DISK)])
        run(["mkfs.ext4", "-q", "-F", "-d", str(stage), str(BOOT_DISK)])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE)
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument(
        "--debug-initramfs",
        action="store_true",
        help="also pack the full rootfs as a compressed debug initramfs",
    )
    args = parser.parse_args()

    for path in (args.base, args.runtime / "usr/lib/xorg/Xorg"):
        if not path.exists():
            raise SystemExit(f"missing input: {path}")

    print(f"[extract] {args.base}")
    extract_base(args.base, ROOTFS)
    print(f"[overlay] DRM runtime from {args.runtime}")
    install_drm_runtime(args.runtime, ROOTFS)
    install_x11_ready_probe(args.runtime, ROOTFS)

    shutil.copy2(Path(__file__).with_name("xorg-drm.conf"), ROOTFS / "etc/xorg.conf")
    shutil.copy2(
        Path(__file__).with_name("units") / "xorg-drm.service",
        ROOTFS / "etc/systemd/system/xorg.service",
    )
    graphical_target = Path(__file__).with_name("units") / "graphical-drm.target"
    shutil.copy2(graphical_target, ROOTFS / "etc/systemd/system/graphical.target")
    shutil.copy2(graphical_target, ROOTFS / "etc/systemd/system/default.target")

    print(f"[pack] persistent root {ROOT_DISK}")
    pack_root_disk(ROOTFS)
    print(f"[pack] stage-1 {STAGE1_INITRAMFS}")
    pack_stage1()
    pack_boot_disk(STAGE1_INITRAMFS)
    if args.debug_initramfs:
        print(f"[pack] debug initramfs {INITRAMFS}")
        pack_initramfs(ROOTFS, INITRAMFS)
    print(
        f"[done] rootfs={ROOTFS} root-disk={ROOT_DISK} "
        f"boot-disk={BOOT_DISK}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
