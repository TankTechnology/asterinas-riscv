#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run one bounded, unattended Megrez Firefox homepage transaction."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import time
from typing import Any, Protocol

from tools.riscv.debian.rootfs.browser_web_marionette_gate import (
    validate_baidu_home,
)
from tools.riscv.debian.rootfs.gate_runtime import PinnedOutputDirectory
from tools.riscv.debian.rootfs.physical_graphics_gate import (
    GateError as GuestGateError,
    validate_png_screenshot,
)
from tools.riscv.megrez_boot_stability import (
    BootReadinessEvidence,
    RealBootCycleOperations,
)
from tools.riscv.megrez_physical_graphics import HostGateError, physical_bootargs


MAX_PAGE_JSON_BYTES = 1024 * 1024
MAX_SCREENSHOT_BYTES = 2 * 1024 * 1024
MAX_SERIAL_BYTES = 8 * 1024 * 1024
MAX_DIAGNOSTICS_BYTES = 256 * 1024
# The first board trace reached Marionette after 244 guest seconds and completed
# NewSession after another 156.  Keep 225 seconds for DOM/screenshot work while
# remaining inside the kernel's fixed 900-second recovery lifetime.
MAX_BAIDU_HOME_GATE_SECONDS = 625
_NONCE = re.compile(r"\A[0-9a-f]{16}\Z")
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_FILE_NAME = re.compile(r"\Abaidu-home\.(json|png)\Z")
_FILE_BEGIN = re.compile(
    r"\A__ASTERINAS_BROWSE_FILE_BEGIN__ nonce=([0-9a-f]{16}) "
    r"name=(baidu-home\.(?:json|png)) size=([0-9]+) sha256=([0-9a-f]{64})\Z"
)
_FILE_END = re.compile(
    r"\A__ASTERINAS_BROWSE_FILE_END__ nonce=([0-9a-f]{16}) "
    r"name=(baidu-home\.(?:json|png)) status=([0-9]+)\Z"
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")


def _failure_reason(error: BaseException) -> str:
    name = re.sub(r"(?<!^)(?=[A-Z])", "-", type(error).__name__).lower()
    detail = re.sub(r"[^a-z0-9]+", "-", str(error).lower()).strip("-")[:120]
    return f"{name}-{detail or 'unspecified'}"


def _transcript_bytes(value: str | bytes) -> bytes:
    if isinstance(value, str):
        return value.encode()
    if isinstance(value, bytes):
        return value
    raise HostGateError("Firefox browse transcript must be text or bytes")


def firefox_browse_bootargs(plan: Any) -> str:
    """Keep the frozen network/proxy path and arm the 900-second safety reboot."""

    bootargs = physical_bootargs(plan)
    tokens = bootargs.split()
    required_prefixes = (
        "asterinas.net=",
        "asterinas.neighbor=",
        "systemd.setenv=ASTERINAS_DESKTOP_PROXY_URL=",
        "systemd.setenv=ASTERINAS_DESKTOP_PROXY_HOST=",
        "systemd.setenv=ASTERINAS_DESKTOP_PROXY_PORT=",
    )
    if any(
        not any(token.startswith(prefix) for token in tokens)
        for prefix in required_prefixes
    ):
        raise HostGateError("Firefox browse plan lacks the frozen network/proxy path")
    if "asterinas.reboot_after=900" not in tokens:
        raise HostGateError("Firefox browse safety reboot is missing")
    if any("mmc_write_partition2" in token for token in tokens):
        raise HostGateError("Firefox browse must not write partition 2")
    return bootargs


@dataclass(frozen=True)
class FirefoxBrowseConfig:
    open_timeout: float = 60.0
    artifact_timeout: float = 120.0
    boot_timeout: float = 180.0
    readiness_timeout: float = 300.0
    clock_timeout: float = 45.0
    browse_timeout: float = 645.0
    transfer_timeout: float = 180.0
    diagnostics_timeout: float = 60.0
    reboot_timeout: float = 30.0
    recovery_timeout: float = 180.0

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 < value <= 1200
            for value in asdict(self).values()
        ):
            raise ValueError("Firefox browse deadlines must be in (0, 1200]")


@dataclass(frozen=True)
class FirefoxBrowseResult:
    schema_version: int
    passed: bool
    physical: bool
    reason: str
    failure: str
    plan_sha256: str
    bootargs_sha256: str
    recovered: bool
    physical_boots: int
    readiness: BootReadinessEvidence | None
    clock_evidence: Mapping[str, object] | None
    page_marker: Mapping[str, object] | None
    proxy_bridge: Mapping[str, object]
    transport: tuple[str, ...]
    serial_sha256: str
    diagnostics_sha256: str
    page_json_sha256: str
    screenshot_sha256: str
    total_seconds: float

    def __post_init__(self) -> None:
        digests = (
            self.plan_sha256,
            self.bootargs_sha256,
            self.serial_sha256,
            self.diagnostics_sha256,
            self.page_json_sha256,
            self.screenshot_sha256,
        )
        if (
            self.schema_version != 1
            or self.physical is not True
            or not isinstance(self.passed, bool)
            or not isinstance(self.recovered, bool)
            or self.physical_boots not in (0, 1)
            or not self.reason
            or any(_SHA256.fullmatch(value) is None for value in digests)
            or not math.isfinite(self.total_seconds)
            or self.total_seconds < 0
        ):
            raise HostGateError("Firefox browse result is invalid")
        if self.passed and (
            self.reason != "baidu-home-ready"
            or self.failure
            or not self.recovered
            or self.readiness is None
            or self.clock_evidence is None
            or self.page_marker is None
            or not self.transport
        ):
            raise HostGateError("passing Firefox browse result is incomplete")

    def canonical_bytes(self) -> bytes:
        return _canonical_json(asdict(self))


class FirefoxBrowseOperations(Protocol):
    @property
    def guest_started(self) -> bool: ...

    @property
    def transcript(self) -> str | bytes: ...

    def open(self, timeout: float) -> None: ...
    def ensure_artifacts(self, plan: Any, timeout: float) -> tuple[str, ...]: ...
    def boot(self, plan: Any, bootargs: str, timeout: float) -> None: ...
    def prove_boot_readiness(self, timeout: float) -> BootReadinessEvidence: ...
    def synchronize_clock(
        self, browser_pid: int, timeout: float
    ) -> dict[str, object]: ...
    def run_baidu_home(
        self, browser_pid: int, nonce: str, timeout: float
    ) -> dict[str, object]: ...
    def retrieve_evidence(self, nonce: str, name: str, timeout: float) -> bytes: ...
    def collect_diagnostics(self, timeout: float) -> bytes: ...
    def request_reboot(self, timeout: float) -> None: ...
    def await_recovery(self, timeout: float) -> None: ...
    def close(self) -> None: ...


class FirefoxBrowsePublisher(Protocol):
    def invalidate(self) -> None: ...
    def publish(
        self,
        result: FirefoxBrowseResult,
        serial: bytes,
        diagnostics: bytes,
        page: bytes,
        screenshot: bytes,
        proxy: Mapping[str, object],
    ) -> None: ...


class ProxyLifecycle(Protocol):
    def start(self) -> object: ...
    def close(self) -> None: ...
    def summary(self) -> dict[str, object]: ...


def _validated_page(page_payload: bytes, marker: Mapping[str, object]) -> None:
    try:
        page = json.loads(page_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HostGateError("Baidu homepage JSON is invalid") from error
    validate_baidu_home(page)
    if not isinstance(page, dict):
        raise HostGateError("Baidu homepage JSON is not an object")
    title = page.get("title")
    url = page.get("url")
    if (
        not isinstance(title, str)
        or marker.get("marker") != "DEBIAN_BROWSER_WEB_BAIDU_HOME_READY"
        or marker.get("scope") != "baidu-home"
        or marker.get("tls") != "verified"
        or marker.get("url") != url
        or marker.get("title_sha256") != hashlib.sha256(title.encode()).hexdigest()
    ):
        raise HostGateError("Baidu homepage marker does not match its JSON evidence")


def run_firefox_browse(
    plan: Any,
    config: FirefoxBrowseConfig,
    operations: FirefoxBrowseOperations,
    publisher: FirefoxBrowsePublisher,
    proxy: ProxyLifecycle,
    *,
    nonce: str | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> FirefoxBrowseResult:
    """Execute one physical boot and always attempt a fresh U-Boot recovery."""

    selected_nonce = secrets.token_hex(8) if nonce is None else nonce
    if _NONCE.fullmatch(selected_nonce) is None:
        raise ValueError("Firefox browse nonce must be 16 lowercase hex digits")
    start = clock()
    bootargs = firefox_browse_bootargs(plan)
    readiness: BootReadinessEvidence | None = None
    clock_evidence: dict[str, object] | None = None
    page_marker: dict[str, object] | None = None
    page_payload = b""
    screenshot = b""
    diagnostics = b""
    transport: tuple[str, ...] = ()
    recovered = False
    failures: list[str] = []
    interruption: BaseException | None = None
    proxy_summary: dict[str, object] = {"schema_version": 1, "state": "not-started"}
    try:
        publisher.invalidate()
        try:
            proxy.start()
            operations.open(config.open_timeout)
            transport = operations.ensure_artifacts(plan, config.artifact_timeout)
            if transport != ("kernel:mmc", "initramfs:mmc", "megrez_dtb:mmc"):
                raise HostGateError("Firefox browse attempted a non-MMC transport")
            operations.boot(plan, bootargs, config.boot_timeout)
            readiness = operations.prove_boot_readiness(config.readiness_timeout)
            clock_evidence = operations.synchronize_clock(
                readiness.browser_pid, config.clock_timeout
            )
            if clock_evidence.get("marker") != "ASTERINAS_CLOCK_SYNC_READY":
                raise HostGateError("guest clock synchronization evidence is invalid")
            page_marker = operations.run_baidu_home(
                readiness.browser_pid, selected_nonce, config.browse_timeout
            )
            page_payload = operations.retrieve_evidence(
                selected_nonce, "baidu-home.json", config.transfer_timeout
            )
            screenshot = operations.retrieve_evidence(
                selected_nonce, "baidu-home.png", config.transfer_timeout
            )
            _validated_page(page_payload, page_marker)
            try:
                validate_png_screenshot(screenshot, expected_dimensions=(1920, 1080))
            except GuestGateError as error:
                raise HostGateError(f"Baidu screenshot is invalid: {error}") from error
        except BaseException as error:
            if not isinstance(error, Exception):
                interruption = error
            failures.append(_failure_reason(error))
            if operations.guest_started:
                try:
                    diagnostics = operations.collect_diagnostics(
                        config.diagnostics_timeout
                    )
                    if not isinstance(diagnostics, bytes):
                        raise HostGateError("Firefox browse diagnostics must be bytes")
                except BaseException as diagnostic_error:
                    failures.append("diagnostics-" + _failure_reason(diagnostic_error))
                    if (
                        not isinstance(diagnostic_error, Exception)
                        and interruption is None
                    ):
                        interruption = diagnostic_error
        finally:
            if operations.guest_started:
                try:
                    operations.request_reboot(config.reboot_timeout)
                    operations.await_recovery(config.recovery_timeout)
                    recovered = True
                except BaseException as recovery_error:
                    failures.append("recovery-" + _failure_reason(recovery_error))
                    if (
                        not isinstance(recovery_error, Exception)
                        and interruption is None
                    ):
                        interruption = recovery_error
            try:
                proxy.close()
            except BaseException as proxy_error:
                failures.append("proxy-close-" + _failure_reason(proxy_error))
                if not isinstance(proxy_error, Exception) and interruption is None:
                    interruption = proxy_error
            try:
                proxy_summary = proxy.summary()
            except Exception as summary_error:
                failures.append("proxy-summary-" + _failure_reason(summary_error))

        serial = _transcript_bytes(operations.transcript)
        if len(serial) > MAX_SERIAL_BYTES:
            failures.append("serial-transcript-oversized")
        if len(diagnostics) > MAX_DIAGNOSTICS_BYTES:
            failures.append("diagnostics-oversized")
        if operations.guest_started and not recovered:
            failures.append("recovery-incomplete")
        passed = not failures and interruption is None
        reason = (
            "baidu-home-ready"
            if passed
            else "manual-reset-required"
            if operations.guest_started and not recovered
            else "baidu-home-incomplete"
        )
        elapsed = clock() - start
        if not math.isfinite(elapsed) or elapsed < 0:
            raise HostGateError("monotonic clock moved backwards")
        result = FirefoxBrowseResult(
            schema_version=1,
            passed=passed,
            physical=True,
            reason=reason,
            failure=";".join(dict.fromkeys(failures)),
            plan_sha256=plan.plan_sha256,
            bootargs_sha256=_sha256(bootargs.encode()),
            recovered=recovered,
            physical_boots=1 if operations.guest_started else 0,
            readiness=readiness,
            clock_evidence=clock_evidence,
            page_marker=page_marker,
            proxy_bridge=proxy_summary,
            transport=transport,
            serial_sha256=_sha256(serial),
            diagnostics_sha256=_sha256(diagnostics),
            page_json_sha256=_sha256(page_payload),
            screenshot_sha256=_sha256(screenshot),
            total_seconds=round(elapsed, 3),
        )
        publisher.publish(
            result,
            serial,
            diagnostics,
            page_payload,
            screenshot,
            proxy_summary,
        )
        if interruption is not None:
            raise interruption
        return result
    finally:
        operations.close()


def evidence_frame_command(nonce: str, name: str) -> str:
    if _NONCE.fullmatch(nonce) is None or _FILE_NAME.fullmatch(name) is None:
        raise ValueError("Firefox browse evidence identity is invalid")
    maximum = MAX_PAGE_JSON_BYTES if name.endswith(".json") else MAX_SCREENSHOT_BYTES
    path = f"/run/asterinas-browse-{nonce}/{name}"
    zeros = "0" * 64
    return (
        f'_f={path}; _z=0; [ -f "$_f" ] && _z=$(wc -c <"$_f"); _s=1; '
        f'case "$_z" in \'\'|*[!0-9]*) _z=0;; esac; [ "$_z" -le {maximum} ] '
        '&& [ -f "$_f" ] && _s=0; '
        'if [ "$_s" -eq 0 ]; then _h=$(sha256sum "$_f" | cut -d\' \' -f1); '
        f"else _h={zeros}; fi; printf '__ASTERINAS_BROWSE_FILE_BEGIN__ "
        f'nonce={nonce} name={name} size=%s sha256=%s\\n\' "$_z" "$_h"; '
        'if [ "$_s" -eq 0 ]; then base64 -w 0 "$_f"; printf \'\\n\'; fi; '
        f"printf '__ASTERINAS_BROWSE_FILE_END__ nonce={nonce} name={name} "
        'status=%s\\n\' "$_s"'
    )


def parse_evidence_frame(transcript: str | bytes, nonce: str, name: str) -> bytes:
    if _NONCE.fullmatch(nonce) is None or _FILE_NAME.fullmatch(name) is None:
        raise ValueError("Firefox browse evidence identity is invalid")
    try:
        text = _transcript_bytes(transcript).decode("utf-8")
    except UnicodeDecodeError as error:
        raise HostGateError("Firefox browse evidence frame is not UTF-8") from error
    lines = tuple(line.rstrip("\r") for line in text.split("\n"))
    begins = [
        (index, match)
        for index, line in enumerate(lines)
        if (match := _FILE_BEGIN.fullmatch(line)) is not None
        and match.group(1) == nonce
        and match.group(2) == name
    ]
    ends = [
        (index, match)
        for index, line in enumerate(lines)
        if (match := _FILE_END.fullmatch(line)) is not None
        and match.group(1) == nonce
        and match.group(2) == name
    ]
    if len(begins) != 1 or len(ends) != 1:
        raise HostGateError("Firefox browse evidence frame is missing or duplicated")
    begin_index, begin = begins[0]
    end_index, end = ends[0]
    if end_index != begin_index + 2 or end.group(3) != "0":
        raise HostGateError(
            "Firefox browse evidence frame status or ordering is invalid"
        )
    expected_size = int(begin.group(3))
    maximum = MAX_PAGE_JSON_BYTES if name.endswith(".json") else MAX_SCREENSHOT_BYTES
    if not 0 < expected_size <= maximum:
        raise HostGateError("Firefox browse evidence size is outside the contract")
    try:
        payload = base64.b64decode(lines[begin_index + 1], validate=True)
    except (binascii.Error, ValueError) as error:
        raise HostGateError(
            "Firefox browse evidence is not canonical base64"
        ) from error
    if len(payload) != expected_size or _sha256(payload) != begin.group(4):
        raise HostGateError("Firefox browse evidence identity mismatch")
    return payload


def _single_json_marker(payload: bytes, expected_marker: str) -> dict[str, object]:
    matches: list[dict[str, object]] = []
    for raw_line in payload.splitlines():
        try:
            value = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and value.get("marker") == expected_marker:
            matches.append(value)
    if len(matches) != 1:
        raise HostGateError(f"{expected_marker} marker is missing or duplicated")
    return matches[0]


class RealFirefoxBrowseOperations(RealBootCycleOperations):
    """MMC-only serial adapter for the lightweight homepage transaction."""

    def ensure_artifacts(self, plan: Any, timeout: float) -> tuple[str, ...]:
        outcomes = super().ensure_artifacts(plan, timeout)
        if outcomes != ("kernel:mmc", "initramfs:mmc", "megrez_dtb:mmc"):
            raise HostGateError("Firefox browse attempted a non-MMC transport")
        return outcomes

    def _run_long_step(
        self, command: str, step: str, nonce: str, timeout: float
    ) -> None:
        serial = self._require_serial()
        deadline = self._guest_phase_deadline(timeout)
        marker_prefix = f"__ASTERINAS_BROWSE_STEP__ nonce={nonce} step={step} status="
        cursor = serial.checkpoint()
        try:
            self._send_bounded(
                serial,
                f"{command}; _s=$?; printf '{marker_prefix}%s\\n' \"$_s\"",
                deadline,
            )
            while True:
                line, next_cursor = self._next_line(serial, cursor, deadline)
                cursor = next_cursor
                if not line.startswith(marker_prefix):
                    continue
                status = line.removeprefix(marker_prefix)
                if status != "0":
                    raise HostGateError(
                        f"Firefox browse {step} failed with status {status}"
                    )
                break
        except TimeoutError:
            self._abort_guest_shell()
            raise
        self._sync_serial_log()

    def _step_payload(self, start: int) -> bytes:
        serial = self._require_serial()
        return bytes(serial.transcript[start:])

    def synchronize_clock(self, browser_pid: int, timeout: float) -> dict[str, object]:
        serial = self._require_serial()
        start = serial.checkpoint()
        command = (
            f"/usr/bin/timeout 30 /usr/bin/nsenter -t {browser_pid} -n "
            "/run/asterinas-tools/megrez-clock-sync "
            "--proxy http://10.100.19.216:17893 --timeout 15"
        )
        self._run_long_step(command, "clock", secrets.token_hex(8), timeout)
        return _single_json_marker(
            self._step_payload(start), "ASTERINAS_CLOCK_SYNC_READY"
        )

    def run_baidu_home(
        self, browser_pid: int, nonce: str, timeout: float
    ) -> dict[str, object]:
        if _NONCE.fullmatch(nonce) is None:
            raise ValueError("Firefox browse nonce is invalid")
        guest_timeout = max(1, min(int(timeout) - 15, MAX_BAIDU_HOME_GATE_SECONDS))
        directory = f"/run/asterinas-browse-{nonce}"
        serial = self._require_serial()
        start = serial.checkpoint()
        command = (
            f"install -d -m 0700 {directory}; /usr/bin/timeout {guest_timeout + 10} "
            f"/usr/bin/nsenter -t {browser_pid} -n /usr/bin/env -i "
            "PATH=/usr/bin:/bin HOME=/home/asterinas PYTHONPATH=/usr/lib/asterinas "
            "/run/asterinas-tools/browser-web-marionette-gate --scope baidu-home "
            f"--firefox-pid {browser_pid} --timeout {guest_timeout} "
            f"--evidence-dir {directory}"
        )
        self._run_long_step(command, "baidu-home", nonce, timeout)
        return _single_json_marker(
            self._step_payload(start), "DEBIAN_BROWSER_WEB_BAIDU_HOME_READY"
        )

    def retrieve_evidence(self, nonce: str, name: str, timeout: float) -> bytes:
        serial = self._require_serial()
        start = serial.checkpoint()
        self._run_long_step(
            evidence_frame_command(nonce, name), f"file-{name}", nonce, timeout
        )
        return parse_evidence_frame(self._step_payload(start), nonce, name)


class RealFirefoxBrowsePublisher:
    """Publish verified homepage evidence atomically, with result.json last."""

    def __init__(self, bundle: Any, output_directory: Path) -> None:
        self._bundle = bundle
        self._output_directory = output_directory
        self._evidence_root = Path(bundle.evidence_root)
        self._output: PinnedOutputDirectory | None = None

    def invalidate(self) -> None:
        if self._output is not None:
            raise HostGateError("Firefox browse output is already active")
        if self._output_directory.parent != self._evidence_root:
            raise HostGateError("Firefox browse output must be under evidence root")
        self._evidence_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if (
            self._evidence_root.is_symlink()
            or self._evidence_root.stat().st_mode & 0o077
        ):
            raise HostGateError("Firefox browse evidence root must be private")
        if self._output_directory.exists():
            raise HostGateError("Firefox browse output directory already exists")
        self._output_directory.mkdir(mode=0o700)
        output = PinnedOutputDirectory(self._output_directory)
        try:
            output.lock_exclusive()
        except BaseException:
            output.close()
            raise
        self._output = output

    def close(self) -> None:
        if self._output is not None:
            self._output.close()
            self._output = None

    def publish(
        self,
        result: FirefoxBrowseResult,
        serial: bytes,
        diagnostics: bytes,
        page: bytes,
        screenshot: bytes,
        proxy: Mapping[str, object],
    ) -> None:
        if self._output is None:
            raise HostGateError("Firefox browse output is not pinned")
        if (
            result.plan_sha256 != self._bundle.plan_sha256
            or len(serial) > MAX_SERIAL_BYTES
            or len(diagnostics) > MAX_DIAGNOSTICS_BYTES
            or len(page) > MAX_PAGE_JSON_BYTES
            or len(screenshot) > MAX_SCREENSHOT_BYTES
            or result.serial_sha256 != _sha256(serial)
            or result.diagnostics_sha256 != _sha256(diagnostics)
            or result.page_json_sha256 != _sha256(page)
            or result.screenshot_sha256 != _sha256(screenshot)
        ):
            raise HostGateError("Firefox browse publication identity is invalid")
        output, self._output = self._output, None
        try:
            payloads = {
                "bundle.json": self._bundle.canonical_bytes(),
                "physical.serial.log": serial,
                "diagnostics.log": diagnostics,
                "baidu-home.json": page,
                "baidu-home.png": screenshot,
                "proxy-bridge.json": _canonical_json(dict(proxy)),
            }
            for name, payload in payloads.items():
                output.atomic_write(name, payload, mode=0o600)
            result_payload = result.canonical_bytes()
            sums = [f"{_sha256(payload)}  {name}" for name, payload in payloads.items()]
            sums.append(f"{_sha256(result_payload)}  result.json")
            output.atomic_write(
                "sha256sums.txt", ("\n".join(sums) + "\n").encode(), mode=0o600
            )
            output.atomic_write("result.json", result_payload, mode=0o600)
        finally:
            output.close()
