"""Pin, stage, and publish immutable QEMU execution evidence."""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from megrez_contract import artifact_identity
from qemu_uboot_secure_io import (
    PinnedOutputDirectory,
    PinnedPublication as PinnedPublication,
    PinnedRegularInput,
    PreparedPublication,
    file_identity,
)


@dataclass(frozen=True)
class StagedExecutionInputs:
    """Private copies consumed by validation and QEMU."""

    uboot: Path
    boot_disk: Path
    manifest: Path
    dtb_audit: Path | None
    source_dtb: Path | None
    variant_audit: Path | None


@dataclass(frozen=True)
class _RunPaths:
    uboot: Path
    boot_disk: Path
    manifest: Path
    dtb_audit: Path | None
    source_dtb: Path | None
    variant_audit: Path | None
    serial_log: Path
    marker_event: Path
    result_path: Path


@dataclass(frozen=True)
class _PinnedOutput:
    path: Path
    directory: PinnedOutputDirectory

    @property
    def name(self) -> str:
        return self.path.name


_STAGED_FILENAMES = {
    "uboot": "u-boot",
    "boot_disk": "boot.ext4",
    "manifest": "manifest.json",
    "dtb_audit": "qemu-dtb-audit.json",
    "source_dtb": "qemu-virt.source.dtb",
    "variant_audit": "qemu-dtb-variant-audit.json",
}


def _paths_overlap(left: Path, right: Path) -> bool:
    if left == right or left in right.parents or right in left.parents:
        return True
    try:
        return left.samefile(right)
    except (FileNotFoundError, OSError):
        return False


def _resolve_run_paths(
    *,
    uboot: Path,
    boot_disk: Path,
    manifest: Path,
    dtb_audit: Path | None,
    source_dtb: Path | None,
    variant_audit: Path | None,
    serial_log: Path,
    marker_event: Path,
    result_path: Path,
) -> _RunPaths:
    inputs = {
        "uboot": uboot,
        "boot_disk": boot_disk,
        "manifest": manifest,
        **({"dtb_audit": dtb_audit} if dtb_audit is not None else {}),
        **({"source_dtb": source_dtb} if source_dtb is not None else {}),
        **({"variant_audit": variant_audit} if variant_audit is not None else {}),
    }
    outputs = {
        "serial_log": serial_log,
        "marker_event": marker_event,
        "result_path": result_path,
    }
    resolved = {
        name: Path(os.path.abspath(path)) for name, path in (inputs | outputs).items()
    }
    for output_index, output_name in enumerate(outputs):
        output = resolved[output_name]
        for input_name in inputs:
            if _paths_overlap(output, resolved[input_name]):
                raise ValueError(f"{output_name} overlaps read-only input {input_name}")
        for other_name in tuple(outputs)[output_index + 1 :]:
            if _paths_overlap(output, resolved[other_name]):
                raise ValueError(
                    f"writable outputs {output_name} and {other_name} overlap"
                )
    return _RunPaths(
        uboot=resolved["uboot"],
        boot_disk=resolved["boot_disk"],
        manifest=resolved["manifest"],
        dtb_audit=resolved.get("dtb_audit"),
        source_dtb=resolved.get("source_dtb"),
        variant_audit=resolved.get("variant_audit"),
        serial_log=resolved["serial_log"],
        marker_event=resolved["marker_event"],
        result_path=resolved["result_path"],
    )


def _input_items(paths: _RunPaths) -> tuple[tuple[str, Path], ...]:
    items: list[tuple[str, Path]] = [
        ("uboot", paths.uboot),
        ("boot_disk", paths.boot_disk),
        ("manifest", paths.manifest),
    ]
    for name in ("dtb_audit", "source_dtb", "variant_audit"):
        path = getattr(paths, name)
        if path is not None:
            items.append((name, path))
    return tuple(items)


def _pin_run_inputs(
    paths: _RunPaths,
    stack: ExitStack,
) -> dict[str, PinnedRegularInput]:
    return {
        name: stack.enter_context(PinnedRegularInput.open(path, label=name))
        for name, path in _input_items(paths)
    }


def _pin_run_outputs(
    paths: _RunPaths,
    stack: ExitStack,
    *,
    forbidden_identities: set[tuple[int, int]],
) -> dict[str, _PinnedOutput]:
    directories: dict[Path, PinnedOutputDirectory] = {}
    outputs: dict[str, _PinnedOutput] = {}
    existing_identities: set[tuple[int, int]] = set()
    for name in ("serial_log", "marker_event", "result_path"):
        path = getattr(paths, name)
        directory = directories.get(path.parent)
        if directory is None:
            directory = stack.enter_context(PinnedOutputDirectory.open(path.parent))
            directories[path.parent] = directory
        output = _PinnedOutput(path=path, directory=directory)
        metadata = directory.entry_metadata(output.name)
        if metadata is not None:
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError(f"{name} must not be a symbolic link")
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"{name} must be a regular file when it exists")
            identity = file_identity(metadata)
            if identity in forbidden_identities:
                raise ValueError(f"{name} overlaps a read-only input")
            if identity in existing_identities:
                raise ValueError(f"writable output {name} overlaps another output")
            existing_identities.add(identity)
        outputs[name] = output
    return outputs


def _stage_run_inputs(
    pinned: dict[str, PinnedRegularInput],
    directory: Path,
) -> StagedExecutionInputs:
    staged: dict[str, Path] = {}
    for name, source in pinned.items():
        destination = directory / _STAGED_FILENAMES[name]
        source.copy_to(destination)
        staged[name] = destination
    return StagedExecutionInputs(
        uboot=staged["uboot"],
        boot_disk=staged["boot_disk"],
        manifest=staged["manifest"],
        dtb_audit=staged.get("dtb_audit"),
        source_dtb=staged.get("source_dtb"),
        variant_audit=staged.get("variant_audit"),
    )


def _staged_hashes(staged: StagedExecutionInputs) -> dict[str, str]:
    hashes: dict[str, str] = {}
    try:
        for name in _STAGED_FILENAMES:
            path = getattr(staged, name)
            if path is not None:
                hashes[name] = artifact_identity(path).sha256
    except OSError as error:
        raise RuntimeError("a private staged input changed during the run") from error
    return hashes


@dataclass
class ExecutionWorkspace:
    """Held execution inputs, output directories, and publications."""

    staged: StagedExecutionInputs
    _pinned_inputs: dict[str, PinnedRegularInput]
    _outputs: dict[str, _PinnedOutput]
    _staging_directory: Path
    _temporary_directory: tempfile.TemporaryDirectory[str]
    _initial_hashes: dict[str, str]
    _stack: ExitStack
    _is_staging_cleaned: bool = False

    @contextmanager
    def capture_serial(self) -> Iterator[tuple[Path, BinaryIO]]:
        path = self._staging_directory / "serial.capture"
        with path.open("x+b") as stream:
            os.chmod(path, 0o600)
            yield path, stream

    def publish_evidence(self, name: str, payload: bytes) -> tuple[int, int]:
        if name not in ("serial_log", "marker_event"):
            raise ValueError(f"unsupported pre-result evidence output: {name}")
        output = self._outputs[name]
        publication = self._stack.enter_context(
            output.directory.atomic_write(output.name, payload)
        )
        return publication.identity

    def verify_and_cleanup_staging(
        self,
        *,
        serial_identity: tuple[int, int],
        marker_identity: tuple[int, int],
    ) -> None:
        """Verify retained evidence and remove private staging before commit."""

        if self._is_staging_cleaned:
            raise RuntimeError("private execution staging is already cleaned")
        if _staged_hashes(self.staged) != self._initial_hashes:
            raise ValueError("a prepared or private staged input changed")
        for source in self._pinned_inputs.values():
            source.verify_unchanged()
        for output in self._outputs.values():
            output.directory.verify_current()
        serial = self._outputs["serial_log"]
        serial.directory.verify_entry(serial.name, serial_identity)
        marker = self._outputs["marker_event"]
        marker.directory.verify_entry(marker.name, marker_identity)
        self._temporary_directory.cleanup()
        self._is_staging_cleaned = True

    @contextmanager
    def prepare_result(self, payload: bytes) -> Iterator[PreparedPublication]:
        """Prepare the result while every retained run resource remains pinned."""

        if not self._is_staging_cleaned:
            raise RuntimeError("private execution staging is not cleaned")
        output = self._outputs["result_path"]
        with output.directory.prepare_atomic_write(output.name, payload) as prepared:
            yield prepared

    def publish_result(self, prepared: PreparedPublication) -> None:
        """Atomically expose a prepared result without syncing its parent."""

        if not self._is_staging_cleaned:
            raise RuntimeError("private execution staging is not cleaned")
        prepared.publish()

    def sync_result(self) -> None:
        """Persist the published result after its inode has been retained."""

        if not self._is_staging_cleaned:
            raise RuntimeError("private execution staging is not cleaned")
        self._outputs["result_path"].directory.sync()

    def verify_after_result(
        self,
        *,
        serial_identity: tuple[int, int],
        marker_identity: tuple[int, int],
        result_identity: tuple[int, int],
    ) -> None:
        """Recheck every retained input and output after result publication."""

        if not self._is_staging_cleaned:
            raise RuntimeError("private execution staging is not cleaned")
        for source in self._pinned_inputs.values():
            source.verify_unchanged()
        for output in self._outputs.values():
            output.directory.verify_current()
        for name, identity in (
            ("serial_log", serial_identity),
            ("marker_event", marker_identity),
            ("result_path", result_identity),
        ):
            output = self._outputs[name]
            output.directory.verify_entry(output.name, identity)


@contextmanager
def open_execution_workspace(
    *,
    uboot: Path,
    boot_disk: Path,
    manifest: Path,
    dtb_audit: Path | None,
    source_dtb: Path | None,
    variant_audit: Path | None,
    serial_log: Path,
    marker_event: Path,
    result_path: Path,
) -> Iterator[ExecutionWorkspace]:
    """Hold immutable materials and output parents for one execution."""

    paths = _resolve_run_paths(
        uboot=uboot,
        boot_disk=boot_disk,
        manifest=manifest,
        dtb_audit=dtb_audit,
        source_dtb=source_dtb,
        variant_audit=variant_audit,
        serial_log=serial_log,
        marker_event=marker_event,
        result_path=result_path,
    )
    with ExitStack() as stack:
        pinned_inputs = _pin_run_inputs(paths, stack)
        outputs = _pin_run_outputs(
            paths,
            stack,
            forbidden_identities={item.identity for item in pinned_inputs.values()},
        )
        temporary_directory = tempfile.TemporaryDirectory(prefix="qemu-booti-inputs-")
        workspace: ExecutionWorkspace | None = None
        try:
            staging_directory = Path(temporary_directory.name)
            os.chmod(staging_directory, 0o700)
            staged = _stage_run_inputs(pinned_inputs, staging_directory)
            workspace = ExecutionWorkspace(
                staged=staged,
                _pinned_inputs=pinned_inputs,
                _outputs=outputs,
                _staging_directory=staging_directory,
                _temporary_directory=temporary_directory,
                _initial_hashes=_staged_hashes(staged),
                _stack=stack,
            )
            yield workspace
        finally:
            if workspace is None or not workspace._is_staging_cleaned:
                temporary_directory.cleanup()
