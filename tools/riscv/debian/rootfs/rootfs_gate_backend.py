#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path
from types import TracebackType
from typing import Any, Mapping
from tools.riscv.debian.rootfs.contract import (
    _load_package_checksums,
    load_manifest,
    validate_frozen_root,
)
from tools.riscv.debian.rootfs.gate_protocol import (
    MAX_TRANSCRIPT_BYTES,
    classify_boot,
    qemu_argv,
    shell_commands,
)
from tools.riscv.debian.rootfs.gate_runtime import (
    GateTermination,
    HmpMonitor,
    MonitorError,
    PinnedOutputDirectory,
    SerialConsole,
    TerminationSignalState,
    launch_process,
)
from tools.riscv.debian.rootfs.rootfs_gate import (
    GateConfig,
    GateFailure,
    build_boot_image,
    copy_sparse_root,
    orchestrate_gate,
    parse_gate_args,
    verify_four_hart_dtb,
)

INPUT_FIELDS = (
    "kernel",
    "u_boot",
    "dtb",
    "stage1_initramfs",
    "root_image",
    "manifest",
    "packages_lock",
    "package_checksums",
)
ARTIFACT_NAMES = (
    "boot.ext4",
    "debian-root.run.ext2",
    "boot1.serial.log",
    "boot2.serial.log",
    "result.json",
)


def _hash_fd(descriptor: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while chunk := os.pread(descriptor, 1024 * 1024, offset):
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()


class ConcreteOperations:
    def __init__(self, config: GateConfig) -> None:
        self.config = config
        self._input_fds: dict[str, int] = {}
        self._output: PinnedOutputDirectory | None = None
        self._attempted_argv: list[tuple[str, ...]] = []
        self._transcripts = [b"", b""]
        self._identity: Mapping[str, object] = {}
        try:
            for name in INPUT_FIELDS:
                path = getattr(config, name)
                descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    os.close(descriptor)
                    raise GateFailure(f"input is not a regular file: {path}")
                self._input_fds[name] = descriptor
            self._output = PinnedOutputDirectory(config.output_directory)
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> ConcreteOperations:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()

    @property
    def input_paths(self) -> Mapping[str, Path]:
        return {
            name: Path(f"/proc/self/fd/{descriptor}")
            for name, descriptor in self._input_fds.items()
        }

    def close(self) -> None:
        if self._output is not None:
            self._output.close()
            self._output = None
        for descriptor in self._input_fds.values():
            os.close(descriptor)
        self._input_fds.clear()

    def _require_config(self, config: GateConfig) -> None:
        if config is not self.config and config != self.config:
            raise GateFailure("backend configuration changed")

    def _require_output(self) -> PinnedOutputDirectory:
        if self._output is None:
            raise GateFailure("backend is closed")
        return self._output

    def invalidate(self, config: GateConfig) -> None:
        self._require_config(config)
        self._require_output().invalidate(*ARTIFACT_NAMES)

    def snapshot_inputs(self, config: GateConfig) -> Mapping[str, str]:
        self._require_config(config)
        return {name: _hash_fd(fd) for name, fd in self._input_fds.items()}

    def _materialize(self, name: str, destination: Path) -> None:
        source = self._input_fds[name]
        with destination.open("xb") as stream:
            offset = 0
            while chunk := os.pread(source, 1024 * 1024, offset):
                stream.write(chunk)
                offset += len(chunk)

    def validate_inputs(
        self, config: GateConfig, snapshots: Mapping[str, str]
    ) -> Mapping[str, object]:
        self._require_config(config)
        paths = self.input_paths
        manifest = load_manifest(paths["manifest"])
        validate_frozen_root(paths["root_image"], manifest, paths["packages_lock"])
        checksums = _load_package_checksums(paths["package_checksums"])
        if checksums != manifest.downloaded_packages:
            raise GateFailure("package-checksums do not match the manifest")
        with tempfile.TemporaryDirectory(prefix="debian-gate-dtb-") as directory:
            dtb = Path(directory) / "qemu-virt.dtb"
            self._materialize("dtb", dtb)
            verify_four_hart_dtb(dtb)
        if dict(snapshots) != self.snapshot_inputs(config):
            raise GateFailure("an immutable gate input changed during validation")
        self._identity = {
            "suite": manifest.suite,
            "architecture": manifest.architecture,
            "debian_release": manifest.debian_release,
            "root_image_sha256": manifest.root_image_sha256,
            "packages_lock_sha256": manifest.packages_lock_sha256,
            "packages": manifest.gate_packages,
        }
        return self._identity

    def prepare(
        self,
        config: GateConfig,
        snapshots: Mapping[str, str],
        identity: Mapping[str, object],
    ) -> Mapping[str, Path]:
        del identity
        self._require_config(config)
        if dict(snapshots) != self.snapshot_inputs(config):
            raise GateFailure("an immutable gate input changed before preparation")
        output = self._require_output()
        with tempfile.TemporaryDirectory(prefix="debian-gate-prepare-") as directory:
            staging = Path(directory)
            for name, filename in (
                ("kernel", "asterinas.booti"),
                ("dtb", "qemu-virt.dtb"),
                ("stage1_initramfs", "stage1-initramfs.cpio"),
            ):
                self._materialize(name, staging / filename)
            boot = staging / "boot.ext4"
            build_boot_image(
                staging / "asterinas.booti",
                staging / "qemu-virt.dtb",
                staging / "stage1-initramfs.cpio",
                boot,
            )
            output.atomic_copy("boot.ext4", boot)
        pinned_parent = Path(f"/proc/self/fd/{output._operation_fd}")
        root = pinned_parent / "debian-root.run.ext2"
        before, after, copied = copy_sparse_root(
            self._input_fds["root_image"],
            root,
            destination_directory_fd=output._operation_fd,
        )
        if before != snapshots["root_image"] or before != after or before != copied:
            raise GateFailure("writable root copy does not match its snapshot")
        manifest = load_manifest(self.input_paths["manifest"])
        validate_frozen_root(root, manifest, self.input_paths["packages_lock"])
        if dict(snapshots) != self.snapshot_inputs(config):
            raise GateFailure("an immutable gate input changed during preparation")
        return {
            "boot_disk": config.output_directory / "boot.ext4",
            "root_disk": config.output_directory / "debian-root.run.ext2",
        }

    def hash_final_root(self, config: GateConfig, prepared: Any) -> str:
        del prepared
        self._require_config(config)
        return self._require_output().sha256("debian-root.run.ext2")

    def publish(
        self,
        config: GateConfig,
        prepared: Any | None,
        transcripts: tuple[bytes, bytes],
        result: dict[str, object],
    ) -> None:
        del prepared
        self._require_config(config)
        output = self._require_output()
        result["qemu_argv"] = self._attempted_argv
        transcripts = tuple(
            saved or supplied for saved, supplied in zip(self._transcripts, transcripts)
        )
        output.atomic_write("boot1.serial.log", transcripts[0])
        output.atomic_write("boot2.serial.log", transcripts[1])
        document = json.dumps(result, indent=2, sort_keys=True) + "\n"
        output.atomic_write("result.json", document.encode("utf-8"))

    def launch(
        self, config: GateConfig, prepared: Any, boot_number: int
    ) -> dict[str, Any]:
        self._require_config(config)
        output = self._require_output()
        pinned_status = os.fstat(output._operation_fd)
        if not os.path.samestat(config.output_directory.lstat(), pinned_status):
            raise GateFailure("output directory identity changed before launch")
        directory = Path(
            tempfile.mkdtemp(
                prefix=f".debian-qemu-b{boot_number}-",
                dir=config.output_directory,
            )
        )
        if not os.path.samestat(directory.parent.lstat(), pinned_status):
            shutil.rmtree(directory, ignore_errors=True)
            raise GateFailure("output directory identity changed during launch")
        os.chmod(directory, 0o700)
        master = slave = -1
        process = None
        try:
            uboot = directory / "u-boot"
            self._materialize("u_boot", uboot)
            boot = directory / "boot.ext4"
            root = directory / "debian-root.run.ext2"
            os.link(
                "boot.ext4",
                boot,
                src_dir_fd=output._operation_fd,
                follow_symlinks=False,
            )
            os.link(
                "debian-root.run.ext2",
                root,
                src_dir_fd=output._operation_fd,
                follow_symlinks=False,
            )
            monitor_path = directory / "monitor.sock"
            argv = self._qemu_argv(
                uboot=uboot,
                boot_disk=boot,
                root_disk=root,
                monitor_socket=monitor_path,
                smp=config.smp,
                dtb_enabled_cpu_count=4,
            )
            self._attempted_argv.append(argv)
            master, slave = os.openpty()
            process = launch_process(argv, stdio_fd=slave)
            os.close(slave)
            slave = -1
            serial = SerialConsole(
                master, process=process, max_bytes=MAX_TRANSCRIPT_BYTES
            )
            monitor = HmpMonitor.connect(
                monitor_path,
                time.monotonic() + config.boot_timeout,
                max_response_bytes=64 * 1024,
            )
            return {
                "argv": argv,
                "boot_number": boot_number,
                "directory": directory,
                "master_fd": master,
                "process": process,
                "serial": serial,
                "monitor": monitor,
            }
        except BaseException:
            if slave >= 0:
                os.close(slave)
            if process is not None:
                now = time.monotonic()
                process.terminate_group(
                    now + config.cleanup_timeout, now + 2 * config.cleanup_timeout
                )
            if master >= 0:
                os.close(master)
            shutil.rmtree(directory, ignore_errors=True)
            raise

    @staticmethod
    def _qemu_argv(**arguments: Any) -> tuple[str, ...]:
        return qemu_argv(**arguments)

    @staticmethod
    def _send_uboot(
        session: Mapping[str, Any], command: str, index: int, deadline: float
    ) -> None:
        token = secrets.token_hex(8).upper()
        status = f"__ASTERINAS_UBOOT_{index}_{token}_STATUS__"
        done = f"__ASTERINAS_UBOOT_{index}_{token}_DONE__"
        status_a, status_b = status[: len(status) // 2], status[len(status) // 2 :]
        done_a, done_b = done[: len(done) // 2], done[len(done) // 2 :]
        payload = (
            f"setenv ast_sa {status_a}; setenv ast_sb {status_b}; "
            f"setenv ast_da {done_a}; setenv ast_db {done_b}; "
            f"if {command}; then echo ${{ast_sa}}${{ast_sb}}0; else echo "
            "${ast_sa}${ast_sb}1; fi; echo ${ast_da}${ast_db}\n"
        ).encode()
        session["serial"].send(payload, deadline)
        transcript = session["serial"].wait_for(done.encode(), deadline)
        if f"{status}0".encode() not in transcript:
            raise GateFailure(f"U-Boot command {index} failed")

    def drive_uboot(self, session: Mapping[str, Any], config: GateConfig) -> None:
        deadline = time.monotonic() + config.boot_timeout
        serial = session["serial"]
        serial.wait_for(b"=> ", deadline)
        commands = (
            "virtio scan",
            "ext4load virtio 0:0 0x80200000 /asterinas.booti",
            "ext4load virtio 0:0 0x88000000 /qemu-virt.dtb",
            "fdt addr 0x88000000",
            "ext4load virtio 0:0 0x83000000 /stage1-initramfs.cpio",
            "setenv initrd_size ${filesize}",
            'setenv bootargs "console=ttyS0 loglevel=4 init=/init"',
        )
        for index, command in enumerate(commands, 1):
            self._send_uboot(session, command, index, deadline)
        marker = f"__ASTERINAS_UBOOT_BOOT_{secrets.token_hex(8).upper()}__"
        split = len(marker) // 2
        serial.send(
            (
                f"setenv ast_ba {marker[:split]}; "
                f"setenv ast_bb {marker[split:]}; "
                "echo ${ast_ba}${ast_bb}; booti 0x80200000 "
                "0x83000000:${initrd_size} 0x88000000\n"
            ).encode(),
            deadline,
        )
        serial.wait_for(marker.encode(), deadline)
        serial.wait_for(b"Starting kernel ...", deadline)

    def enter_debian(self, session: Mapping[str, Any], config: GateConfig) -> None:
        session["serial"].wait_for(
            b"__DEBIAN_ROOTFS_SHELL_READY__",
            time.monotonic() + config.boot_timeout,
        )

    def execute_checks(
        self,
        session: dict[str, Any],
        config: GateConfig,
        identity: Mapping[str, object],
        nonce: str,
    ) -> None:
        commands = shell_commands(boot_number=session["boot_number"], nonce=nonce)
        for command in commands:
            deadline = time.monotonic() + config.command_timeout
            done = f"__ASTERINAS_SHELL_DONE_{secrets.token_hex(12).upper()}__"
            split = len(done) // 2
            payload = (
                command.payload + f"; printf '%s%s\\n' "
                f"'{done[:split]}' '{done[split:]}'\n"
            ).encode()
            session["serial"].send(payload, deadline)
            session["serial"].wait_for(done.encode(), deadline)
        session.update(commands=commands, identity=identity, nonce=nonce)

    def request_quit(self, session: Mapping[str, Any], config: GateConfig) -> None:
        try:
            session["monitor"].command(
                "quit", time.monotonic() + config.cleanup_timeout
            )
        except MonitorError as error:
            if str(error) == "HMP closed before prompt":
                return
            raise
        raise GateFailure("HMP quit unexpectedly returned a prompt")

    def close_monitor(self, session: dict[str, Any]) -> None:
        session["monitor"].close()
        session["monitor"] = None

    def cleanup_process(self, session: Mapping[str, Any], config: GateConfig) -> None:
        now = time.monotonic()
        session["process"].terminate_group(
            now + config.cleanup_timeout, now + 2 * config.cleanup_timeout
        )

    def drain_serial(self, session: Mapping[str, Any], config: GateConfig) -> bytes:
        serial = session["serial"]
        try:
            drain_failure = None
            try:
                serial.drain(time.monotonic() + config.cleanup_timeout)
            except BaseException as error:
                drain_failure = error
            transcript = serial.transcript
            boot = session["boot_number"]
            nonce = session.get("nonce", "")
            redacted = (
                transcript.replace(nonce.encode(), b"<nonce-redacted>")
                if nonce
                else transcript
            )
            self._transcripts[boot - 1] = redacted
            if drain_failure is not None:
                raise drain_failure
            if "commands" in session:
                identity = session["identity"]
                result = classify_boot(
                    transcript,
                    session["commands"],
                    boot_number=boot,
                    expected_debian_release=identity["debian_release"],
                    expected_packages=identity["packages"],
                    expected_nonce=session["nonce"],
                )
                if not result.passed:
                    raise GateFailure(result.reason)
            return redacted
        finally:
            os.close(session["master_fd"])
            shutil.rmtree(session["directory"], ignore_errors=True)


def _safe_output(path: Path) -> None:
    if os.geteuid() != 0:
        raise GateFailure("the Debian rootfs gate must run as root")
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise GateFailure("output directory must be root-owned and mode 0700")


def main(arguments: list[str] | None = None) -> int:
    try:
        config = parse_gate_args(arguments)
        _safe_output(config.output_directory)
        with TerminationSignalState(), ConcreteOperations(config) as operations:
            result = orchestrate_gate(config, operations, nonce=secrets.token_hex(32))
        return 0 if result["passed"] else 1
    except SystemExit as error:
        return int(error.code or 0)
    except GateTermination as error:
        print(
            f"debian-rootfs-gate: terminated by signal {error.signum}", file=sys.stderr
        )
        return 128 + error.signum
    except BaseException as error:
        reason = error.reason if isinstance(error, GateFailure) else str(error)
        print(f"debian-rootfs-gate: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
