"""Pin, stage, and publish immutable QEMU execution evidence."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from megrez_contract import artifact_identity
from qemu_uboot_secure_io import (
    PinnedOutputDirectory,
    PinnedPublication,
    PinnedRegularInput,
    PreparedPublication,
    file_identity,
)
from qemu_uboot_devices import RuntimeDevicePaths


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
    progress_log: Path | None
    screenshot: Path | None
    display_audit: Path | None


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
    progress_log: Path | None = None,
    screenshot: Path | None = None,
    display_audit: Path | None = None,
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
        **({"progress_log": progress_log} if progress_log is not None else {}),
        **({"screenshot": screenshot} if screenshot is not None else {}),
        **({"display_audit": display_audit} if display_audit is not None else {}),
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
        progress_log=resolved.get("progress_log"),
        screenshot=resolved.get("screenshot"),
        display_audit=resolved.get("display_audit"),
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
    names = ("serial_log", "marker_event", "result_path")
    if paths.progress_log is not None:
        names = (*names, "progress_log")
    if paths.screenshot is not None:
        names = (*names, "screenshot")
    if paths.display_audit is not None:
        names = (*names, "display_audit")
    for name in names:
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


class _MirroredSerialStream:
    """Write authoritative capture bytes to a live, non-authoritative mirror."""

    def __init__(self, capture: BinaryIO, progress: BinaryIO) -> None:
        self._capture = capture
        self._progress = progress

    def write(self, payload: bytes) -> int:
        captured = self._capture.write(payload)
        if captured is None:
            captured = len(payload)
        if captured != len(payload):
            raise OSError("short write to serial evidence capture")
        mirrored = self._progress.write(payload)
        if mirrored is None:
            mirrored = len(payload)
        if mirrored != len(payload):
            raise OSError("short write to live serial progress log")
        return captured

    def flush(self) -> None:
        self._capture.flush()
        self._progress.flush()


@dataclass(frozen=True)
class DisplayCaptureWorkspace:
    """Private fixed paths used by one framebuffer capture."""

    _root: Path
    _qmp_socket: Path
    _screenshot: Path

    def runtime_paths(self) -> RuntimeDevicePaths:
        return RuntimeDevicePaths(
            capture_root=self._root,
            monitor_socket=self._qmp_socket,
        )

    def capture(self, capture_screendump: Callable[..., bytes]) -> bytes:
        return capture_screendump(
            self._qmp_socket,
            self._screenshot,
            capture_root=self._root,
        )


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
    _pinned_evidence: dict[str, PinnedRegularInput]
    display_capture: DisplayCaptureWorkspace | None = None
    _is_staging_cleaned: bool = False

    @contextmanager
    def capture_serial(self) -> Iterator[tuple[Path, BinaryIO, BinaryIO]]:
        path = self._staging_directory / "serial.capture"
        with path.open("x+b") as stream:
            os.chmod(path, 0o600)
            progress = self._outputs.get("progress_log")
            if progress is None:
                yield path, stream, stream
                return
            progress.directory.remove_entry(progress.name)
            with progress.directory.create_exclusive(progress.name) as live:
                os.fchmod(live.fileno(), 0o644)
                mirrored = _MirroredSerialStream(stream, live)
                try:
                    yield path, stream, mirrored
                finally:
                    mirrored.flush()
                    os.fsync(live.fileno())
                    progress.directory.verify_open_file(progress.name, live)
                    progress.directory.verify_current()

    def publish_evidence(self, name: str, payload: bytes) -> tuple[int, int]:
        if name not in ("serial_log", "marker_event"):
            raise ValueError(f"unsupported pre-result evidence output: {name}")
        output = self._outputs[name]
        publication = self._stack.enter_context(
            output.directory.atomic_write(output.name, payload)
        )
        self._pin_published_evidence(
            name,
            publication.identity,
            hashlib.sha256(payload).hexdigest(),
        )
        return publication.identity

    def _verify_published_evidence(
        self,
        name: str,
        identity: tuple[int, int],
    ) -> None:
        output = self._outputs[name]
        output.directory.verify_entry(output.name, identity)
        evidence = self._pinned_evidence.get(name)
        if evidence is None or evidence.identity != identity:
            raise RuntimeError(f"missing pinned evidence: {name}")
        # This execution-local bracket closes the hash/read TOCTOU without
        # changing the shared PinnedRegularInput API.
        before = os.fstat(evidence._fd)
        evidence.verify_unchanged()
        after = os.fstat(evidence._fd)
        if any(
            getattr(before, field) != getattr(after, field)
            for field in ("st_size", "st_mtime_ns", "st_ctime_ns")
        ):
            raise RuntimeError(f"pinned evidence changed during verification: {name}")
        output.directory.verify_entry(output.name, identity)

    def _final_output_sweep(
        self,
        *,
        serial_identity: tuple[int, int],
        marker_identity: tuple[int, int],
        result_identity: tuple[int, int] | None,
        check_result: bool,
        screenshot_identity: tuple[int, int] | None,
        display_audit_identity: tuple[int, int] | None,
    ) -> None:
        """Recheck paths after every retained inode has been hashed."""

        entries = [
            ("serial_log", serial_identity),
            ("marker_event", marker_identity),
            ("screenshot", screenshot_identity),
            ("display_audit", display_audit_identity),
        ]
        if check_result:
            entries.append(("result_path", result_identity))
        directories: list[PinnedOutputDirectory] = []
        for name, _identity in entries:
            output = self._outputs.get(name)
            if output is not None and all(output.directory is not item for item in directories):
                directories.append(output.directory)
        for directory in directories:
            directory.verify_current()
        for name, identity in entries:
            output = self._outputs.get(name)
            if output is None:
                continue
            if identity is None:
                if output.directory.entry_metadata(output.name) is not None:
                    raise RuntimeError(f"unexpected output entry: {name}")
            else:
                output.directory.verify_entry(output.name, identity)
        for directory in directories:
            directory.verify_current()

    def _pin_published_evidence(
        self,
        name: str,
        identity: tuple[int, int],
        expected_sha256: str,
    ) -> None:
        """Pin a published output inode for later identity and content checks."""

        output = self._outputs[name]
        evidence = PinnedRegularInput.open(
            output.directory.path / output.name,
            label=name,
        )
        try:
            if evidence.identity != identity:
                raise RuntimeError(f"output entry changed during publication: {name}")
            # PinnedRegularInput hashes the held inode while opening it; compare
            # that trusted snapshot with the payload committed by this workspace.
            if evidence._initial_sha256 != expected_sha256:
                raise RuntimeError(f"published output changed before pinning: {name}")
            self._pinned_evidence[name] = self._stack.enter_context(evidence)
        except BaseException:
            evidence.close()
            raise

    def _remove_output(self, name: str) -> None:
        """Remove one configured evidence output through its pinned directory."""

        output = self._outputs[name]
        output.directory.verify_current()
        output.directory.remove_entry(output.name)
        output.directory.sync()
        output.directory.verify_current()

    def _clear_display_entries_after_result_invalidation(self) -> None:
        """Best-effort clear a display generation after its result was removed."""

        for name in ("result_path", "screenshot", "display_audit"):
            try:
                self._remove_output(name)
            except Exception:
                pass

    def _invalidate_result_and_clear_display_entries(self) -> None:
        """Durably invalidate a result, then clear its display generation."""

        self._remove_output("result_path")
        try:
            self._remove_output("screenshot")
            self._remove_output("display_audit")
        except BaseException:
            self._clear_display_entries_after_result_invalidation()
            raise

    def _verify_display_entries(
        self,
        *,
        screenshot_identity: tuple[int, int] | None,
        display_audit_identity: tuple[int, int] | None,
    ) -> None:
        """Verify configured display entries, including expected absence."""

        for name, identity in (
            ("screenshot", screenshot_identity),
            ("display_audit", display_audit_identity),
        ):
            output = self._outputs.get(name)
            if output is None:
                continue
            if identity is None:
                if output.directory.entry_metadata(output.name) is not None:
                    raise RuntimeError(f"unexpected output entry: {name}")
                continue
            self._verify_published_evidence(name, identity)

    def publish_display_evidence(
        self,
        screenshot_payload: bytes | None,
        audit_payload: bytes | None,
    ) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
        """Publish a display pair after invalidating any result generation.

        The result is removed first, so no result can cite a partial or stale
        display generation while either member is being replaced or cleared.
        """

        if (screenshot_payload is None) != (audit_payload is None):
            raise ValueError("display evidence must be a complete pair")
        if screenshot_payload is None:
            self._invalidate_result_and_clear_display_entries()
            return None, None
        screenshot = self._outputs["screenshot"]
        audit = self._outputs["display_audit"]
        screenshot_publication: PinnedPublication | None = None
        audit_publication: PinnedPublication | None = None
        result_invalidated = False
        try:
            with (
                screenshot.directory.prepare_atomic_write(screenshot.name, screenshot_payload) as prepared_screenshot,
                audit.directory.prepare_atomic_write(audit.name, audit_payload) as prepared_audit,
            ):
                screenshot_publication = self._stack.enter_context(prepared_screenshot.retain())
                audit_publication = self._stack.enter_context(prepared_audit.retain())
                self._remove_output("result_path")
                result_invalidated = True
                self._remove_output("screenshot")
                self._remove_output("display_audit")
                prepared_screenshot.publish()
                prepared_audit.publish()
                screenshot.directory.sync()
                audit.directory.sync()
                screenshot.directory.verify_entry(screenshot.name, screenshot_publication.identity)
                audit.directory.verify_entry(audit.name, audit_publication.identity)
                self._pin_published_evidence(
                    "screenshot",
                    screenshot_publication.identity,
                    hashlib.sha256(screenshot_payload).hexdigest(),
                )
                self._pin_published_evidence(
                    "display_audit",
                    audit_publication.identity,
                    hashlib.sha256(audit_payload).hexdigest(),
                )
                return screenshot_publication.identity, audit_publication.identity
        except BaseException:
            if result_invalidated:
                self._clear_display_entries_after_result_invalidation()
            raise

    def verify_and_cleanup_staging(
        self,
        *,
        serial_identity: tuple[int, int],
        marker_identity: tuple[int, int],
        screenshot_identity: tuple[int, int] | None = None,
        display_audit_identity: tuple[int, int] | None = None,
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
        self._verify_published_evidence("serial_log", serial_identity)
        self._verify_published_evidence("marker_event", marker_identity)
        self._verify_display_entries(
            screenshot_identity=screenshot_identity,
            display_audit_identity=display_audit_identity,
        )
        self._final_output_sweep(
            serial_identity=serial_identity,
            marker_identity=marker_identity,
            result_identity=None,
            check_result="screenshot" in self._outputs,
            screenshot_identity=screenshot_identity,
            display_audit_identity=display_audit_identity,
        )
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

    def publish_result(self, prepared: PreparedPublication, payload: bytes) -> None:
        """Atomically expose a prepared result without syncing its parent."""

        if not self._is_staging_cleaned:
            raise RuntimeError("private execution staging is not cleaned")
        prepared.publish()
        self._pin_published_evidence(
            "result_path",
            prepared.identity,
            hashlib.sha256(payload).hexdigest(),
        )

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
        screenshot_identity: tuple[int, int] | None = None,
        display_audit_identity: tuple[int, int] | None = None,
    ) -> None:
        """Recheck every retained input and output after result publication."""

        if not self._is_staging_cleaned:
            raise RuntimeError("private execution staging is not cleaned")
        for source in self._pinned_inputs.values():
            source.verify_unchanged()
        for output in self._outputs.values():
            output.directory.verify_current()
        self._verify_published_evidence("serial_log", serial_identity)
        self._verify_published_evidence("marker_event", marker_identity)
        self._verify_published_evidence("result_path", result_identity)
        self._verify_display_entries(
            screenshot_identity=screenshot_identity,
            display_audit_identity=display_audit_identity,
        )
        self._final_output_sweep(
            serial_identity=serial_identity,
            marker_identity=marker_identity,
            result_identity=result_identity,
            check_result=True,
            screenshot_identity=screenshot_identity,
            display_audit_identity=display_audit_identity,
        )


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
    progress_log: Path | None = None,
    screenshot: Path | None = None,
    display_audit: Path | None = None,
) -> Iterator[ExecutionWorkspace]:
    """Hold immutable materials and output parents for one execution."""

    if (screenshot is None) != (display_audit is None):
        raise ValueError("screenshot and display audit must be provided together")
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
        progress_log=progress_log,
        screenshot=screenshot,
        display_audit=display_audit,
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
            display_capture = None
            if screenshot is not None:
                capture_root = staging_directory / "capture"
                capture_root.mkdir(mode=0o700)
                os.chmod(capture_root, 0o700)
                display_capture = DisplayCaptureWorkspace(
                    capture_root,
                    capture_root / "qmp.sock",
                    capture_root / "shot.ppm",
                )
            staged = _stage_run_inputs(pinned_inputs, staging_directory)
            workspace = ExecutionWorkspace(
                staged=staged,
                _pinned_inputs=pinned_inputs,
                _outputs=outputs,
                _staging_directory=staging_directory,
                _temporary_directory=temporary_directory,
                _initial_hashes=_staged_hashes(staged),
                _stack=stack,
                _pinned_evidence={},
                display_capture=display_capture,
            )
            yield workspace
        finally:
            if workspace is None or not workspace._is_staging_cleaned:
                temporary_directory.cleanup()
