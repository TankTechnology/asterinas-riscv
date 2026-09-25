# SPDX-License-Identifier: MPL-2.0

"""Bounded, content-addressed Megrez desktop boot workflow."""

from __future__ import annotations

import argparse
import contextlib
import functools
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
import re
import secrets
import socket
import stat
import sys
import tempfile
import threading
import time
import zlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol, Sequence


GENERATION_ROOT = "/home/debian/asterinas/boot"
ARTIFACT_NAMES = ("kernel", "initramfs", "megrez_dtb")
PHASE_MARKERS = (
    ("kernel-entered", "Enter riscv_boot"),
    ("stage1-start", "DEBIAN_STAGE1_PROGRESS step=start "),
    ("root-found", "DEBIAN_STAGE1_PROGRESS step=root-found "),
    ("root-handoff", "DEBIAN_STAGE1_PROGRESS step=handoff-enter action=exec"),
    ("debug-console-ready", "ASTERINAS_DEBUG_CONSOLE_READY uid=0"),
    ("display-ready", "ASTERINAS_DESKTOP_DISPLAY_READY"),
    ("firefox-ready", "ASTERINAS_DESKTOP_FIREFOX_READY "),
    ("kernel-watchdog-disarmed", "ASTERINAS_SOFTWARE_REBOOT_DISARMED"),
    ("guest-watchdog-disarmed", "ASTERINAS_DESKTOP_WATCHDOG_DISARMED"),
    ("desktop-ready", "ASTERINAS_DESKTOP_BOOT_READY "),
)
# The board's lab link and the proxy it browses through.
#
# A desktop session is interactive, so unlike the isolation-oriented evidence
# gates it must carry a configured interface and a reachable proxy. Leaving
# them out gives the guest no interface at all, and the blackholed proxy the
# gates use leaves every page load failing: the operator then loses the network
# on every republish.
BOARD_LINK_BOOTARGS = (
    "asterinas.net=eic7700-rj45,10.100.19.200/21,10.100.16.1",
    "asterinas.neighbor=eic7700-rj45,10.100.19.216,04:7c:16:47:50:4e",
)

PROXY_HOST = "10.100.19.216"
PROXY_PORT = 17893

BOARD_PROXY_BOOTARGS = (
    f"systemd.setenv=ASTERINAS_DESKTOP_PROXY_HOST={PROXY_HOST}",
    f"systemd.setenv=ASTERINAS_DESKTOP_PROXY_PORT={PROXY_PORT}",
)

# The guest's own resolver tunnels through the same host, on the port the
# operator's TCP-to-UDP DNS forwarder listens on. Browsing does not need it,
# because the proxy resolves the names it is asked for, but nothing else on the
# guest can resolve anything without it.
DNS_PORT = 15354

BOARD_DNS_BOOTARGS = (
    f"systemd.setenv=ASTERINAS_DESKTOP_DNS_HOST={PROXY_HOST}",
    f"systemd.setenv=ASTERINAS_DESKTOP_DNS_PORT={DNS_PORT}",
)


def warn_if_dns_unreachable(timeout: float = 5.0) -> None:
    """Reports a missing DNS forwarder without refusing to boot.

    The guest's shim leaves /etc/resolv.conf alone when its tunnel does not
    answer, so a boot without this forwarder is the guest resolving nothing --
    exactly the state before the shim existed. That is worth a warning, since
    the operator cannot tell it apart from the shim being broken, but it is not
    worth failing a boot whose browser never asks the guest to resolve.
    """

    try:
        with socket.create_connection((PROXY_HOST, DNS_PORT), timeout=timeout):
            return
    except OSError as error:
        print(
            f"warning: no DNS forwarder is reachable at {PROXY_HOST}:{DNS_PORT}"
            f" ({error}); the guest will not resolve names until one is started",
            file=sys.stderr,
        )


def require_proxy_reachable(timeout: float = 5.0) -> None:
    """Refuses to boot a desktop whose proxy nothing answers on.

    The proxy is a bridge the operator starts on the host, so it can be absent
    while every artifact still verifies. Without this check the board boots,
    the browser loads, and the failure only surfaces as a page error after the
    operator is already at the board.
    """

    try:
        with socket.create_connection((PROXY_HOST, PROXY_PORT), timeout=timeout):
            return
    except OSError as error:
        raise DesktopBootError(
            f"no proxy is reachable at {PROXY_HOST}:{PROXY_PORT}: {error}. "
            "Start the host-side proxy bridge before booting the desktop."
        ) from error

BOOTARGS = " ".join(
    (
        "console=ttyS0",
        "loglevel=info",
        "asterinas.klog_capture=info",
        "init=/init",
        "asterinas.mmc_write_partition2",
        "asterinas.reboot_after=300",
        "systemd.mask=asterinas-browser-web-evidence.service",
        "systemd.mask=asterinas-desktop-m5-network.service",
        "systemd.mask=serial-getty@ttyS0.service",
        "systemd.mask=console-getty.service",
        *BOARD_LINK_BOOTARGS,
        "systemd.setenv=ASTERINAS_BROWSER_WEB_BASIC_ONLY=1",
        "systemd.setenv=ASTERINAS_WEB_NETWORK_MODE=proxy",
        *BOARD_PROXY_BOOTARGS,
        *BOARD_DNS_BOOTARGS,
        "--",
        "--root-init=systemd",
        "--debug-console=isolated-root",
        "--volatile-home",
    )
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_NONCE = re.compile(r"[0-9a-f]{16,64}\Z")
_BASE_URL = re.compile(r"https?://[A-Za-z0-9._:-]+\Z")


class DesktopBootError(RuntimeError):
    pass


class PrepareOperations(Protocol):
    publication_transcript: bytes

    def open(self, timeout: float) -> None: ...

    def boot_rockos(self, timeout: float) -> None: ...

    def login(self, username: str, password: str, timeout: float) -> None: ...

    def publish(
        self, commands: Sequence[str], password: str, timeout: float
    ) -> None: ...

    def reboot_and_recover(self, password: str, timeout: float) -> None: ...

    def close(self) -> None: ...


class StartOperations(Protocol):
    @property
    def transcript(self) -> bytes: ...

    @property
    def phase_times(self) -> dict[str, float]: ...

    @property
    def boot_epoch_started(self) -> bool: ...

    def open(self, timeout: float) -> None: ...

    def load(
        self, manifest: "DesktopBootManifest", timeout: float
    ) -> dict[str, int]: ...

    def boot(self, manifest: "DesktopBootManifest", timeout: float) -> None: ...

    def wait_ready(self, timeout: float) -> bytes: ...

    def probe(self, timeout: float) -> dict[str, Any]: ...

    def await_recovery(self, timeout: float) -> bytes: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class DesktopBootArtifact:
    name: str
    source: str
    basename: str
    size: int
    sha256: str
    crc32: str
    load_address: int
    mmc_path: str = ""


@dataclass(frozen=True)
class DesktopBootManifest:
    schema_version: int
    stage1_protocol_version: int
    plan_sha256: str
    expected_root_sha256: str
    bootargs: str
    artifacts: dict[str, DesktopBootArtifact]
    generation_sha256: str
    generation_directory: str

    @classmethod
    def from_plan(cls, plan_path: Path) -> "DesktopBootManifest":
        payload = plan_path.read_bytes()
        try:
            plan: dict[str, Any] = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DesktopBootError(f"invalid plan: {error}") from error
        rows = plan.get("artifacts")
        if plan.get("schema_version") != 2 or not isinstance(rows, list):
            raise DesktopBootError("unsupported desktop boot plan")
        names = [row.get("name") for row in rows if isinstance(row, dict)]
        if len(names) != len(set(names)):
            raise DesktopBootError("plan contains duplicate artifact names")
        by_name = {row.get("name"): row for row in rows if isinstance(row, dict)}
        missing = set((*ARTIFACT_NAMES, "root_image")) - by_name.keys()
        if missing:
            raise DesktopBootError(f"plan is missing artifacts: {sorted(missing)}")

        artifacts: dict[str, DesktopBootArtifact] = {}
        for name in ARTIFACT_NAMES:
            row = by_name[name]
            source = Path(row.get("path", ""))
            try:
                metadata = source.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    raise DesktopBootError(f"{name}: source is a symbolic link")
                content = source.read_bytes()
            except OSError as error:
                raise DesktopBootError(
                    f"{name}: cannot read source: {error}"
                ) from error
            actual_sha = hashlib.sha256(content).hexdigest()
            actual_crc = f"{zlib.crc32(content):08x}"
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size != row.get("size")
                or actual_sha != row.get("sha256")
                or actual_crc != row.get("crc32")
            ):
                raise DesktopBootError(f"{name}: identity mismatch")
            suffix = {"kernel": "Image", "initramfs": "cpio", "megrez_dtb": "dtb"}[name]
            basename = f"{name}-{actual_sha[:16]}.{suffix}"
            artifacts[name] = DesktopBootArtifact(
                name=name,
                source=str(source.resolve()),
                basename=basename,
                size=metadata.st_size,
                sha256=actual_sha,
                crc32=actual_crc,
                load_address=row.get("load_address"),
            )
        root_sha = by_name["root_image"].get("sha256")
        if not isinstance(root_sha, str) or _SHA256.fullmatch(root_sha) is None:
            raise DesktopBootError("root image identity is invalid")
        identity = {
            "schema_version": 1,
            "stage1_protocol_version": 1,
            "plan_sha256": hashlib.sha256(payload).hexdigest(),
            "expected_root_sha256": root_sha,
            "bootargs": BOOTARGS,
            "artifacts": {
                name: _published_artifact(artifacts[name], include_path=False)
                for name in ARTIFACT_NAMES
            },
        }
        generation_sha = hashlib.sha256(_canonical(identity)).hexdigest()
        generation_directory = f"{GENERATION_ROOT}/{generation_sha[:16]}"
        artifacts = {
            name: replace(
                artifact,
                mmc_path=f"{generation_directory}/{artifact.basename}",
            )
            for name, artifact in artifacts.items()
        }
        return cls(
            **identity
            | {
                "artifacts": artifacts,
                "generation_sha256": generation_sha,
                "generation_directory": generation_directory,
            }
        )

    def canonical_bytes(self) -> bytes:
        return _canonical(
            {
                "schema_version": self.schema_version,
                "stage1_protocol_version": self.stage1_protocol_version,
                "plan_sha256": self.plan_sha256,
                "expected_root_sha256": self.expected_root_sha256,
                "bootargs": self.bootargs,
                "artifacts": {
                    name: _published_artifact(self.artifacts[name], include_path=True)
                    for name in ARTIFACT_NAMES
                },
                "generation_sha256": self.generation_sha256,
                "generation_directory": self.generation_directory,
            }
        )


def _canonical(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _published_artifact(
    artifact: DesktopBootArtifact, *, include_path: bool
) -> dict[str, Any]:
    result = {
        "name": artifact.name,
        "basename": artifact.basename,
        "size": artifact.size,
        "sha256": artifact.sha256,
        "crc32": artifact.crc32,
        "load_address": artifact.load_address,
    }
    if include_path:
        result["mmc_path"] = artifact.mmc_path
    return result


def publication_script(manifest: DesktopBootManifest, base_url: str, nonce: str) -> str:
    """Return a fail-closed RockOS script for one immutable p3 generation."""

    if _BASE_URL.fullmatch(base_url) is None or _NONCE.fullmatch(nonce) is None:
        raise DesktopBootError("unsafe publication transport input")
    final = manifest.generation_directory
    manifest_sha = hashlib.sha256(manifest.canonical_bytes()).hexdigest()
    checks = [
        f"{artifact.sha256}  $WORK/{artifact.basename}"
        for artifact in (manifest.artifacts[name] for name in ARTIFACT_NAMES)
    ]
    final_checks = [
        f"{artifact.sha256}  {final}/{artifact.basename}"
        for artifact in (manifest.artifacts[name] for name in ARTIFACT_NAMES)
    ]
    downloads = [
        f"curl -fSs --max-time 180 {base_url}/{artifact.basename} -o $WORK/{artifact.basename}"
        for artifact in (manifest.artifacts[name] for name in ARTIFACT_NAMES)
    ]
    return "\n".join(
        (
            "set -eu",
            f"GENERATION_ROOT={GENERATION_ROOT}",
            f"FINAL={final}",
            "mkdir -p -- $GENERATION_ROOT",
            "if test -d $FINAL; then",
            f"  printf '%s  %s\\n' {manifest_sha} $FINAL/manifest.json | sha256sum -c -",
            *(f"  printf '%s\\n' '{line}' | sha256sum -c -" for line in final_checks),
            f"  echo ASTERINAS_DESKTOP_GENERATION_READY generation={manifest.generation_sha256} status=idempotent",
            "  exit 0",
            "fi",
            f"WORK=$(mktemp -d $GENERATION_ROOT/.{manifest.generation_sha256[:16]}.{nonce}.XXXXXX)",
            "trap 'rm -rf -- $WORK' EXIT HUP INT TERM",
            *downloads,
            f"curl -fSs --max-time 30 {base_url}/manifest.json -o $WORK/manifest.json",
            *(f"printf '%s\\n' \"{line}\" | sha256sum -c -" for line in checks),
            f"printf '%s  %s\\n' {manifest_sha} $WORK/manifest.json | sha256sum -c -",
            "sync $WORK",
            "mv -T -- $WORK $FINAL",
            "trap - EXIT HUP INT TERM",
            "sync $GENERATION_ROOT",
            f"echo ASTERINAS_DESKTOP_GENERATION_READY generation={manifest.generation_sha256} status=published",
            "",
        )
    )


def prepare_generation(
    manifest: DesktopBootManifest,
    operations: PrepareOperations,
    *,
    username: str,
    password: str,
    base_url: str,
    nonce: str,
) -> bytes:
    """Publish one generation through RockOS and recover to U-Boot."""

    script = publication_script(manifest, base_url, nonce)
    script_name = f"publish-{manifest.generation_sha256[:16]}.sh"
    script_sha = hashlib.sha256(script.encode()).hexdigest()
    launcher = (
        f"curl -fSs --max-time 30 {base_url}/{script_name} "
        "-o /tmp/asterinas-desktop-publish.sh "
        f"&& printf '%s  %s\\n' {script_sha} /tmp/asterinas-desktop-publish.sh "
        "| sha256sum -c - && sh /tmp/asterinas-desktop-publish.sh"
    )
    must_recover = False
    try:
        # A software RockOS reboot can spend over 30 seconds shutting down
        # and enumerating U-Boot USB before the prompt is available.
        operations.open(120)
        operations.boot_rockos(240)
        operations.login(username, password, 60)
        must_recover = True
        try:
            operations.publish((launcher,), password, 600)
        finally:
            operations.reboot_and_recover(password, 180)
            must_recover = False
        transcript = operations.publication_transcript
        marker = (
            "ASTERINAS_DESKTOP_GENERATION_READY "
            f"generation={manifest.generation_sha256}"
        ).encode()
        if marker not in transcript:
            raise DesktopBootError("RockOS did not confirm the desktop generation")
        return transcript
    finally:
        if must_recover:
            # This path is only reachable when recovery itself raised; avoid a
            # second blind serial command and release the exclusive descriptor.
            pass
        operations.close()


def observe_ready_phases(
    transcript: bytes, *, now: Any = time.monotonic
) -> dict[str, float]:
    if not isinstance(transcript, bytes) or len(transcript) > 8 * 1024 * 1024:
        raise DesktopBootError("serial readiness transcript is invalid")
    try:
        text = transcript.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DesktopBootError("serial readiness transcript is not UTF-8") from error
    positions: list[int] = []
    observed: dict[str, float] = {}
    for name, marker in PHASE_MARKERS:
        count = text.count(marker)
        if count > 1:
            raise DesktopBootError(f"phase {name} is duplicated")
        if count == 0:
            raise DesktopBootError(f"phase {name} is missing")
        position = text.find(marker)
        if positions and position <= positions[-1]:
            raise DesktopBootError(f"phase {name} is out of order")
        positions.append(position)
        observed[name] = now()
    return observed


def _validate_admission(evidence: dict[str, Any]) -> None:
    required_true = ("debug_console", "x11_socket")
    if any(evidence.get(name) is not True for name in required_true):
        raise DesktopBootError("read-only desktop admission predicate failed")
    if (
        not isinstance(evidence.get("firefox_pid"), int)
        or evidence["firefox_pid"] <= 1
        or evidence.get("firefox_uid") != 1000
        or not isinstance(evidence.get("visible_windows"), int)
        or evidence["visible_windows"] < 1
        or evidence.get("watchdog") != 0
        or not isinstance(evidence.get("boot_id"), str)
        or re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            evidence["boot_id"],
        )
        is None
    ):
        raise DesktopBootError("read-only desktop admission evidence is invalid")


def _readiness_failure_context(transcript: bytes) -> str:
    matches = re.findall(
        rb"ASTERINAS_DESKTOP_BOOT_(?:WAIT|FAIL) "
        rb"reason=([a-z0-9-]+)(?: remaining=([0-9]+))?",
        transcript,
    )
    if not matches:
        return ""
    reason, remaining = matches[-1]
    detail = f"; last readiness reason={reason.decode()}"
    if remaining:
        detail += f" remaining={remaining.decode()}"
    return detail


def start_generation(
    manifest: DesktopBootManifest, operations: StartOperations
) -> dict[str, Any]:
    """Execute one bounded start, returning success or proven recovery."""

    require_proxy_reachable()
    warn_if_dns_unreachable()
    started = time.monotonic()
    try:
        operations.open(30)
        sizes = operations.load(manifest, 90)
        expected_sizes = {
            name: manifest.artifacts[name].size for name in ARTIFACT_NAMES
        }
        if sizes != expected_sizes:
            raise DesktopBootError("partition-3 artifact byte count mismatch")
        operations.boot(manifest, 30)
        transcript = operations.wait_ready(300)
        observe_ready_phases(transcript)
        phases = operations.phase_times
        expected_phase_names = tuple(name for name, _ in PHASE_MARKERS)
        if tuple(phases) != expected_phase_names:
            raise DesktopBootError("host phase timestamps are incomplete")
        evidence = operations.probe(30)
        _validate_admission(evidence)
        return {
            "schema_version": 1,
            "status": "pass",
            "reason": "desktop-ready",
            "generation_sha256": manifest.generation_sha256,
            "plan_sha256": manifest.plan_sha256,
            "expected_root_sha256": manifest.expected_root_sha256,
            "elapsed_seconds": time.monotonic() - started,
            "boot_epoch_started": True,
            "phases": phases,
            "artifacts": _result_artifacts(manifest),
            **evidence,
            "serial_sha256": hashlib.sha256(transcript).hexdigest(),
        }
    except (DesktopBootError, OSError, RuntimeError, TimeoutError) as error:
        if not operations.boot_epoch_started:
            return {
                "schema_version": 1,
                "status": "fail",
                "reason": str(error),
                "generation_sha256": manifest.generation_sha256,
                "plan_sha256": manifest.plan_sha256,
                "expected_root_sha256": manifest.expected_root_sha256,
                "elapsed_seconds": time.monotonic() - started,
                "boot_epoch_started": False,
                "phases": operations.phase_times,
                "artifacts": _result_artifacts(manifest),
                "recovered_to_uboot": True,
                "recovery_sha256": hashlib.sha256(b"").hexdigest(),
                "serial_sha256": hashlib.sha256(operations.transcript).hexdigest(),
            }
        recovery_error = None
        try:
            recovery = operations.await_recovery(360)
        except (DesktopBootError, OSError, RuntimeError, TimeoutError) as failure:
            recovery = b""
            recovery_error = str(failure)
        recovered = all(
            marker in recovery for marker in (b"OpenSBI", b"U-Boot", b"=> ")
        )
        reason = str(error) + _readiness_failure_context(operations.transcript)
        if recovery_error is not None:
            reason = f"{reason}; recovery failed: {recovery_error}"
        return {
            "schema_version": 1,
            "status": "fail",
            "reason": reason,
            "generation_sha256": manifest.generation_sha256,
            "plan_sha256": manifest.plan_sha256,
            "expected_root_sha256": manifest.expected_root_sha256,
            "elapsed_seconds": time.monotonic() - started,
            "boot_epoch_started": True,
            "phases": operations.phase_times,
            "artifacts": _result_artifacts(manifest),
            "recovered_to_uboot": recovered,
            "recovery_sha256": hashlib.sha256(recovery).hexdigest(),
            "serial_sha256": hashlib.sha256(operations.transcript).hexdigest(),
        }
    finally:
        operations.close()


def _result_artifacts(manifest: DesktopBootManifest) -> dict[str, dict[str, Any]]:
    return {
        name: _published_artifact(manifest.artifacts[name], include_path=True)
        for name in ARTIFACT_NAMES
    }


def uboot_load_commands(manifest: DesktopBootManifest) -> tuple[str, ...]:
    return tuple(
        f"ext4load mmc 1:3 0x{artifact.load_address:x} {artifact.mmc_path}"
        for artifact in (manifest.artifacts[name] for name in ARTIFACT_NAMES)
    )


@contextlib.contextmanager
def _publication_server(
    manifest: DesktopBootManifest, address: str, port: int, nonce: str
):
    with tempfile.TemporaryDirectory(prefix="asterinas-desktop-publish-") as raw:
        directory = Path(raw)
        for name in ARTIFACT_NAMES:
            artifact = manifest.artifacts[name]
            (directory / artifact.basename).symlink_to(artifact.source)
        (directory / "manifest.json").write_bytes(manifest.canonical_bytes())
        script_name = f"publish-{manifest.generation_sha256[:16]}.sh"
        (directory / script_name).write_text(
            publication_script(manifest, f"http://{address}:{port}", nonce)
        )
        handler = functools.partial(SimpleHTTPRequestHandler, directory=str(directory))
        server = ThreadingHTTPServer((address, port), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class RealStartOperations:
    """Exclusive, non-interactive serial implementation of one start epoch."""

    def __init__(self, device: str, *, progress_stream: Any | None = None) -> None:
        self._device = device
        self._progress_stream = progress_stream or sys.stdout
        self._fd: int | None = None
        self._session: Any = None
        self._serial: Any = None
        self._log = io.StringIO()
        self._preboot_log_length: int | None = None
        self._boot_started: float | None = None
        self._boot_epoch_started = False
        self._phase_times: dict[str, float] = {}
        self._phase_tail = b""

    @property
    def transcript(self) -> bytes:
        log_payload = self._log.getvalue().encode()
        if self._serial is None:
            return log_payload
        split = self._preboot_log_length
        if split is None:
            split = len(log_payload)
        return log_payload[:split] + self._serial.transcript + log_payload[split:]

    @property
    def phase_times(self) -> dict[str, float]:
        return dict(self._phase_times)

    @property
    def boot_epoch_started(self) -> bool:
        return self._boot_epoch_started

    def _record_phase(self, name: str) -> None:
        if name in self._phase_times or self._boot_started is None:
            return
        elapsed = time.monotonic() - self._boot_started
        self._phase_times[name] = elapsed
        print(
            f"Megrez desktop phase: {name} elapsed={elapsed:.3f}s",
            file=self._progress_stream,
            flush=True,
        )

    def _observe_serial(self, chunk: bytes) -> None:
        combined = self._phase_tail + chunk
        for name, marker in PHASE_MARKERS[1:]:
            if marker.encode() in combined:
                self._record_phase(name)
        tail_length = max(len(marker) for _, marker in PHASE_MARKERS) - 1
        self._phase_tail = combined[-tail_length:]

    def open(self, timeout: float) -> None:
        from tools.riscv.megrez_board_session import BoardSession, open_serial
        from tools.riscv.megrez_debug_board import _lock_serial

        fd = open_serial(self._device)
        try:
            _lock_serial(fd)
            session = BoardSession.from_fd(
                fd, None, confirm=False, log_stream=self._log
            )
            os.write(fd, b"\x03")
            session.wait_for_uboot_prompt(timeout)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        self._session = session

    def _require_session(self) -> Any:
        if self._session is None:
            raise DesktopBootError("serial session is not open")
        return self._session

    def load(self, manifest: DesktopBootManifest, timeout: float) -> dict[str, int]:
        from tools.riscv.megrez_board_session import LOAD_RESULT_PATTERN

        session = self._require_session()
        deadline = time.monotonic() + timeout
        session.command("mmc dev 1", timeout=min(15, timeout))
        session.command("mmc rescan", timeout=min(15, timeout))
        sizes: dict[str, int] = {}
        for name in ARTIFACT_NAMES:
            artifact = manifest.artifacts[name]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("partition-3 artifact validation timed out")
            command = (
                f"ext4load mmc 1:3 0x{artifact.load_address:x} {artifact.mmc_path}"
            )
            try:
                output = session.command(command, timeout=remaining)
            except (OSError, RuntimeError, TimeoutError) as error:
                raise DesktopBootError(
                    f"generation {manifest.generation_sha256[:16]} unavailable "
                    "on partition 3; run `python3 -m "
                    "tools.riscv.megrez_desktop_boot prepare`: "
                    f"{error}"
                ) from error
            sizes[name] = session._verify_loaded_artifact(
                name,
                artifact.load_address,
                artifact.crc32,
                output,
                LOAD_RESULT_PATTERN,
            )
        return sizes

    def boot(self, manifest: DesktopBootManifest, timeout: float) -> None:
        from tools.riscv.megrez_board_session import uboot_bootargs_commands
        from tools.riscv.debian.rootfs.gate_runtime import SerialConsole

        session = self._require_session()
        initramfs = manifest.artifacts["initramfs"]
        deadline = time.monotonic() + timeout
        commands = (
            "fdt addr 0xf0000000",
            "fdt resize 0x1000",
            f"setenv initrd_size 0x{initramfs.size:x}",
            *uboot_bootargs_commands(manifest.bootargs),
        )
        for command in commands:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("U-Boot desktop preparation timed out")
            session.command(command, timeout=remaining)
        kernel = manifest.artifacts["kernel"]
        dtb = manifest.artifacts["megrez_dtb"]
        command = (
            f"booti 0x{kernel.load_address:x} "
            f"0x{initramfs.load_address:x}:0x{initramfs.size:x} "
            f"0x{dtb.load_address:x}"
        )
        self._boot_started = time.monotonic()
        self._boot_epoch_started = True
        session.start_boot_attempt()
        session.command(
            command,
            expect="Enter riscv_boot",
            timeout=max(1, deadline - time.monotonic()),
        )
        self._record_phase("kernel-entered")
        self._preboot_log_length = len(self._log.getvalue().encode())
        assert self._fd is not None
        self._serial = SerialConsole(
            self._fd,
            max_bytes=8 * 1024 * 1024,
            tx_delay=0.005,
            observer=self._observe_serial,
        )

    def wait_ready(self, timeout: float) -> bytes:
        if self._serial is None or self._boot_started is None:
            raise DesktopBootError("guest serial protocol is not active")
        deadline = min(
            time.monotonic() + timeout,
            self._boot_started + 300,
        )
        for name, marker in PHASE_MARKERS[1:]:
            self._serial.wait_for(marker.encode(), deadline)
            self._record_phase(name)
        return self.transcript

    def probe(self, timeout: float) -> dict[str, Any]:
        if self._serial is None:
            raise DesktopBootError("guest serial protocol is not active")
        nonce = secrets.token_hex(8)
        marker = f"__ASTERINAS_DESKTOP_ADMISSION_{nonce}__"
        end_marker = f"__ASTERINAS_DESKTOP_ADMISSION_END_{nonce}__"
        command = (
            "pid=$(systemctl show -p MainPID --value asterinas-browser-web.service); "
            "uid=$(awk '/^Uid:/{print $2}' /proc/$pid/status); "
            "windows=$(DISPLAY=:0 XAUTHORITY=/home/asterinas/.Xauthority "
            "xdotool search --onlyvisible --class firefox 2>/dev/null | wc -l); "
            "x11=0; test -S /tmp/.X11-unix/X0 && x11=1; "
            "printf '__ASTERINAS_DESKTOP_ADMISSION_%s__ boot_id=%s "
            "firefox_pid=%s firefox_uid=%s visible_windows=%s watchdog=%s "
            "debug_console=1 x11_socket=%s "
            "__ASTERINAS_DESKTOP_ADMISSION_END_%s__\\n' "
            f"'{nonce}' "
            '"$(cat /proc/sys/kernel/random/boot_id)" "$pid" "$uid" "$windows" '
            '"$(cat /proc/sys/kernel/asterinas_reboot_watchdog)" "$x11" '
            f"'{nonce}'"
        )
        start = self._serial.checkpoint()
        deadline = time.monotonic() + timeout
        self._serial.send((command + "\n").encode(), deadline)
        self._serial.wait_for(end_marker.encode(), deadline, start=start)
        text = self._serial.transcript[start:].decode("utf-8", errors="replace")
        pattern = re.compile(
            re.escape(marker) + r" boot_id=([0-9a-f-]{36}) firefox_pid=([0-9]+) "
            r"firefox_uid=([0-9]+) visible_windows=([0-9]+) "
            r"watchdog=([01]) debug_console=1 x11_socket=([01]) "
            + re.escape(end_marker)
        )
        matches = pattern.findall(text)
        if len(matches) != 1:
            raise DesktopBootError("read-only desktop admission response is missing")
        boot_id, pid, uid, windows, watchdog, x11 = matches[0]
        return {
            "boot_id": boot_id,
            "firefox_pid": int(pid),
            "firefox_uid": int(uid),
            "visible_windows": int(windows),
            "watchdog": int(watchdog),
            "debug_console": True,
            "x11_socket": x11 == "1",
        }

    def await_recovery(self, timeout: float) -> bytes:
        if self._boot_started is None:
            raise DesktopBootError("guest boot epoch was not established")
        remaining = min(timeout, self._boot_started + timeout - time.monotonic())
        if remaining <= 0:
            raise TimeoutError("firmware recovery deadline expired")
        recovery = self._require_session().wait_for_uboot_prompt(remaining)
        return recovery.encode()

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
        self._fd = None
        self._session = None


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare or start one bounded Megrez Asterinas desktop boot"
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    prepare = subparsers.add_parser(
        "prepare", help="publish one immutable generation to RockOS partition 3"
    )
    prepare.add_argument("--plan", type=Path, required=True)
    prepare.add_argument("--device", required=True)
    prepare.add_argument("--host-address", default="10.100.19.216")
    prepare.add_argument("--port", type=int, default=18080)
    prepare.add_argument("--username", default="debian")
    credential = prepare.add_mutually_exclusive_group(required=True)
    credential.add_argument("--factory-login", action="store_true")
    credential.add_argument("--password-fd", type=int)
    prepare.add_argument("--output", type=Path, required=True)
    start = subparsers.add_parser(
        "start", help="start an already-published generation from partition 3"
    )
    start.add_argument("--plan", type=Path, required=True)
    start.add_argument("--device", required=True)
    start.add_argument("--output", type=Path, required=True)
    return parser


def _read_password(descriptor: int | None, factory_login: bool) -> str:
    if factory_login:
        return "debian"
    if descriptor is None:
        raise DesktopBootError("RockOS password source is required")
    payload = os.read(descriptor, 1024)
    if len(payload) == 1024 or b"\x00" in payload:
        raise DesktopBootError("RockOS password is invalid")
    try:
        password = payload.decode().rstrip("\r\n")
    except UnicodeDecodeError as error:
        raise DesktopBootError("RockOS password is not UTF-8") from error
    if not password or "\n" in password or "\r" in password:
        raise DesktopBootError("RockOS password is invalid")
    return password


def _prepare_main(args: argparse.Namespace) -> int:
    from tools.riscv.megrez_rockos_attestation import (
        RealRockOsAttestationOperations,
    )

    manifest = DesktopBootManifest.from_plan(args.plan)
    password = _read_password(args.password_fd, args.factory_login)
    nonce = secrets.token_hex(16)
    base_url = f"http://{args.host_address}:{args.port}"
    operations = RealRockOsAttestationOperations(args.device)
    with _publication_server(manifest, args.host_address, args.port, nonce):
        transcript = prepare_generation(
            manifest,
            operations,
            username=args.username,
            password=password,
            base_url=base_url,
            nonce=nonce,
        )
    _atomic_write(args.output / "manifest.json", manifest.canonical_bytes())
    _atomic_write(args.output / "publication.serial.log", transcript)
    sums = (
        f"{hashlib.sha256(manifest.canonical_bytes()).hexdigest()}  manifest.json\n"
        f"{hashlib.sha256(transcript).hexdigest()}  publication.serial.log\n"
    ).encode()
    _atomic_write(args.output / "sha256sums.txt", sums)
    print(
        "prepared Megrez desktop generation "
        f"{manifest.generation_sha256} at {manifest.generation_directory}"
    )
    return 0


def _start_main(args: argparse.Namespace) -> int:
    manifest = DesktopBootManifest.from_plan(args.plan)
    operations = RealStartOperations(args.device)
    try:
        result = start_generation(manifest, operations)
    finally:
        transcript = operations.transcript
        _atomic_write(args.output / "serial.log", transcript)
    result["serial_sha256"] = hashlib.sha256(transcript).hexdigest()
    _atomic_write(args.output / "result.json", _canonical(result))
    print(
        f"Megrez desktop start {result['status']}: {result['reason']} "
        f"generation={manifest.generation_sha256[:16]}"
    )
    return 0 if result["status"] == "pass" else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.action == "prepare":
            return _prepare_main(args)
        if args.action == "start":
            return _start_main(args)
        raise AssertionError(args.action)
    except (DesktopBootError, OSError, RuntimeError, TimeoutError) as error:
        print(f"Megrez desktop boot {args.action} failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
