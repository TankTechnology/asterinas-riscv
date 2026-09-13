#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Exercise the physical-graphics witness with QEMU keyboard and tablet input."""

from __future__ import annotations

import hashlib
import re
import secrets
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from tools.riscv.debian.rootfs.debug_console_qemu_gate import (
    DebugConsoleQemuOperations,
    classify_debug_console_qemu,
)
from tools.riscv.debian.rootfs.desktop_m3_gate import inspect_ppm
from tools.riscv.debian.rootfs.desktop_m5_qemu_gate import (
    DesktopM5QemuOperations,
    desktop_m5_qemu_argv,
)
from tools.riscv.debian.rootfs.gate_protocol import GateResult
from tools.riscv.debian.rootfs.gate_runtime import (
    GateTermination,
    TerminationSignalState,
)
from tools.riscv.debian.rootfs.rootfs_gate import (
    GateConfig,
    GateFailure,
    parse_gate_args,
)
from tools.riscv.debian.rootfs.rootfs_gate_backend import _safe_output
from tools.riscv.debian.rootfs.systemd_m2_gate import orchestrate_systemd_m2_gate
from tools.riscv.megrez_physical_graphics import (
    HostGateError,
    InteractionCycleEvidence,
    PHYSICAL_EXTERNAL_MARKER,
    PointerEvidenceMode,
    classify_interaction_transcript,
    extract_screenshot_frame,
    physical_cycle_command,
    physical_external_services_quiesce_command,
    physical_final_command,
    validate_physical_external_services_quiesced,
)
from tools.riscv.qemu_qmp import (
    ABSOLUTE_AXIS_MAX,
    capture_screendump,
    click_left_button,
    move_tablet,
)


QEMU_SCREEN_WIDTH = 1280
QEMU_SCREEN_HEIGHT = 1024
HMP_KEY_RELEASE_SECONDS = 0.12
QEMU_MARIONETTE_SETUP_TIMEOUT = 600.0
_NONCE = re.compile(r"[0-9a-f]{16}")
_SHA256 = r"[0-9a-f]{64}"
_BROWSER_IDENTITY = re.compile(
    r"__ASTERINAS_PHYSICAL_QEMU_BROWSER__ pid=([1-9][0-9]*) "
    r"service=active restarts=0"
)
_LOCAL_GRAPHICS_READY = b"BROWSER_WEB_DESKTOP_STAGE=x-socket-ready"
_MARIONETTE_LISTENING = b"Listening on port 2828"
_LOCAL_GRAPHICS_FAILURE_MARKERS = (
    b"DEBIAN_ROOTFS_FAIL reason=",
    b"DEBIAN_DESKTOP_M4_FAIL reason=",
    b"ASTERINAS_FIREFOX_WEB_FAIL reason=",
    b"Kernel panic",
    b"Uncaught panic:",
    b"Printing stack trace:",
)
_FINAL = re.compile(rf"__ASTERINAS_PHYSICAL_FINAL__ cycle=3 nonce_sha256=({_SHA256})")


@dataclass(frozen=True)
class QemuCycleArtifact:
    """Guest and rendered-pixel evidence retained for one QEMU input cycle."""

    cycle: int
    nonce_sha256: str
    guest_png_sha256: str
    rendered_ppm_sha256: str
    rendered: Mapping[str, int]


@dataclass(frozen=True)
class QemuInteractionResult:
    """Classifier result that can never be mistaken for physical evidence."""

    passed: bool
    reason: str
    physical: bool
    cycles: tuple[InteractionCycleEvidence, ...]


def _physical_graphics_qemu_bootargs() -> str:
    prefix, separator, initargs = DebugConsoleQemuOperations.BOOTARGS.partition(" -- ")
    if not separator or initargs != "--root-init=systemd --debug-console=root":
        raise ValueError("unexpected debug-console QEMU bootargs contract")
    return (
        f"{prefix} "
        "systemd.mask=asterinas-browser-web-evidence.service "
        "systemd.mask=asterinas-desktop-m5-network.service "
        "systemd.setenv=ASTERINAS_BROWSER_WEB_BASIC_ONLY=1 "
        f"-- {initargs}"
    )


def physical_graphics_qemu_argv(**arguments: Any) -> tuple[str, ...]:
    """Keep the graphical devices and add a private absolute-input QMP socket."""

    argv = desktop_m5_qemu_argv(**arguments)
    socket_path = arguments["monitor_socket"].with_name("physical-input.qmp")
    return (*argv, "-qmp", f"unix:{socket_path},server=on,wait=off")


def qemu_input_commands(nonce: str) -> tuple[str, ...]:
    """Return the only reviewed HMP input sequence accepted by this gate."""

    if not isinstance(nonce, str) or _NONCE.fullmatch(nonce) is None:
        raise ValueError("QEMU interaction nonce must be 16 lowercase hex digits")
    return tuple(f"sendkey {character}" for character in nonce)


def _classify_interaction(
    transcript: bytes,
    nonces: Sequence[str],
) -> QemuInteractionResult:
    try:
        cycles = classify_interaction_transcript(
            transcript,
            nonces,
            pointer_mode=PointerEvidenceMode.QEMU_TABLET,
        )
    except HostGateError as error:
        return QemuInteractionResult(False, str(error), False, ())
    return QemuInteractionResult(True, "pass", False, cycles)


def classify_physical_graphics_qemu(
    transcript: bytes,
    *,
    expected_debian_release: str,
    nonces: Sequence[str],
) -> QemuInteractionResult:
    """Compose the debug-console and QEMU-tablet interaction classifiers."""

    debug = classify_debug_console_qemu(
        transcript,
        expected_debian_release=expected_debian_release,
        expected_profile="browser-web",
        require_web_network=False,
    )
    if not debug.passed:
        return QemuInteractionResult(False, debug.reason, False, ())
    return _classify_interaction(transcript, nonces)


def _next_line(serial: Any, cursor: int, deadline: float) -> tuple[str, int]:
    while True:
        transcript = serial.transcript
        newline = transcript.find(b"\n", cursor)
        if newline >= 0:
            try:
                line = transcript[cursor:newline].rstrip(b"\r").decode("utf-8")
            except UnicodeDecodeError as error:
                raise GateFailure("QEMU interaction line is not UTF-8") from error
            return line, newline + 1
        serial.wait_for(b"\n", deadline, start=cursor)


class PhysicalGraphicsQemuOperations(DebugConsoleQemuOperations):
    """Drive three nonce-bound browser cycles through QEMU's input devices."""

    ARTIFACT_PREFIX = "physical-graphics-qemu"
    BOOTARGS = _physical_graphics_qemu_bootargs()
    CAPTURE_DEBUG_SCREENSHOT = False
    REQUIRE_FIXTURE_EVIDENCE = False
    _CYCLE_ARTIFACTS = tuple(
        name
        for cycle in range(1, 4)
        for name in (
            f"physical-graphics-qemu-cycle-{cycle}.png",
            f"physical-graphics-qemu-cycle-{cycle}.ppm",
        )
    )

    def __init__(self, config: GateConfig) -> None:
        self._nonces: tuple[str, ...] = ()
        self._browser_pid: int | None = None
        self._cycle_artifacts: list[tuple[QemuCycleArtifact, bytes, bytes]] = []
        self._classified_cycles: tuple[InteractionCycleEvidence, ...] = ()
        super().__init__(config)

    @classmethod
    def _accepted_profile_identities(cls) -> tuple[tuple[int, str], ...]:
        return ((7, "browser-web"),)

    @staticmethod
    def _qemu_argv(**arguments: Any) -> tuple[str, ...]:
        return physical_graphics_qemu_argv(**arguments)

    def invalidate(self, config: GateConfig) -> None:
        self._nonces = ()
        self._browser_pid = None
        self._cycle_artifacts.clear()
        self._classified_cycles = ()
        super().invalidate(config)
        self._require_output().invalidate(*self._CYCLE_ARTIFACTS)

    @staticmethod
    def _browser_identity_command() -> str:
        return (
            "_asterinas_qemu_pid=$(systemctl show --property MainPID --value "
            "asterinas-browser-web.service 2>/dev/null || true); "
            "_asterinas_qemu_service=$(systemctl is-active "
            "asterinas-browser-web.service 2>/dev/null || true); "
            "_asterinas_qemu_restarts=$(systemctl show --property NRestarts --value "
            "asterinas-browser-web.service 2>/dev/null || true); "
            "printf '__ASTERINAS_PHYSICAL_QEMU_BROWSER__ pid=%s service=%s "
            'restarts=%s\\n\' "$_asterinas_qemu_pid" '
            '"$_asterinas_qemu_service" "$_asterinas_qemu_restarts"'
        )

    def _query_browser_pid(self, serial: Any, deadline: float) -> int:
        cursor = serial.checkpoint()
        serial.send((self._browser_identity_command() + "\n").encode(), deadline)
        while True:
            line, cursor = _next_line(serial, cursor, deadline)
            match = _BROWSER_IDENTITY.fullmatch(line)
            if match is not None:
                return int(match.group(1))
            if line.startswith("__ASTERINAS_PHYSICAL_QEMU_BROWSER__"):
                raise GateFailure("QEMU Firefox service identity is incomplete")

    @staticmethod
    def _external_services_quiesce_command() -> str:
        return physical_external_services_quiesce_command()

    def _quiesce_external_services(self, serial: Any, deadline: float) -> None:
        """Stop external-network and competing Marionette work."""

        cursor = serial.checkpoint()
        serial.send(
            (self._external_services_quiesce_command() + "\n").encode(), deadline
        )
        while True:
            line, cursor = _next_line(serial, cursor, deadline)
            if not line.startswith(PHYSICAL_EXTERNAL_MARKER):
                continue
            try:
                validate_physical_external_services_quiesced(line)
            except HostGateError as error:
                raise GateFailure(f"QEMU {error}") from error
            return

    @staticmethod
    def _wait_for_local_graphics(serial: Any, deadline: float) -> None:
        completion = serial.wait_for_any(
            (_LOCAL_GRAPHICS_READY, *_LOCAL_GRAPHICS_FAILURE_MARKERS), deadline
        )
        if completion != _LOCAL_GRAPHICS_READY:
            raise GateFailure("guest failed before local graphics readiness")

    @staticmethod
    def _wait_for_marionette(serial: Any, deadline: float) -> None:
        completion = serial.wait_for_any(
            (_MARIONETTE_LISTENING, *_LOCAL_GRAPHICS_FAILURE_MARKERS), deadline
        )
        if completion != _MARIONETTE_LISTENING:
            raise GateFailure("guest failed before Marionette readiness")

    @staticmethod
    def _inject_hmp_input(monitor: Any, nonce: str, deadline: float) -> None:
        for command in qemu_input_commands(nonce):
            monitor.command(command, deadline)
            if command.startswith("sendkey "):
                remaining = deadline - time.monotonic()
                if remaining < HMP_KEY_RELEASE_SECONDS:
                    raise GateFailure("QEMU keyboard injection deadline expired")
                time.sleep(HMP_KEY_RELEASE_SECONDS)

    def _run_interaction_cycle(
        self,
        session: Mapping[str, Any],
        config: GateConfig,
        *,
        cycle: int,
        nonce: str,
    ) -> QemuCycleArtifact:
        serial = session["serial"]
        monitor = session["monitor"]
        browser_pid = self._browser_pid
        if browser_pid is None:
            raise GateFailure("QEMU Firefox identity was not established")
        timeout = min(config.command_timeout, 300.0)
        deadline = time.monotonic() + QEMU_MARIONETTE_SETUP_TIMEOUT + timeout + 10.0
        start = serial.checkpoint()
        command = physical_cycle_command(
            cycle,
            nonce,
            timeout,
            expected_browser_pid=browser_pid,
            expected_width=QEMU_SCREEN_WIDTH,
            expected_height=QEMU_SCREEN_HEIGHT,
            setup_timeout=QEMU_MARIONETTE_SETUP_TIMEOUT,
        )
        serial.send((command + "\n").encode(), deadline)
        nonce_sha256 = hashlib.sha256(nonce.encode()).hexdigest()
        ready = (
            f"ASTERINAS_PHYSICAL_GRAPHICS_READY cycle={cycle} "
            f"nonce_sha256={nonce_sha256}"
        )
        cursor = start
        while True:
            line, cursor = _next_line(serial, cursor, deadline)
            if line == ready:
                break
            if line.startswith("ASTERINAS_PHYSICAL_GRAPHICS_FAIL"):
                raise GateFailure(f"QEMU interaction cycle {cycle} failed before READY")
            if line.startswith("__ASTERINAS_PHYSICAL_COMMAND_STATUS__"):
                raise GateFailure(f"QEMU interaction cycle {cycle} exited before READY")

        buffered_after_ready = serial.transcript[cursor:]
        if (
            b"ASTERINAS_PHYSICAL_GRAPHICS_" in buffered_after_ready
            or b"__ASTERINAS_PHYSICAL_" in buffered_after_ready
        ):
            raise GateFailure(
                f"QEMU interaction cycle {cycle} advanced before HMP input"
            )
        self._inject_hmp_input(monitor, nonce, deadline)
        key_ready = (
            f"ASTERINAS_PHYSICAL_GRAPHICS_KEY_READY cycle={cycle} "
            f"nonce_sha256={nonce_sha256}"
        )
        while True:
            line, cursor = _next_line(serial, cursor, deadline)
            if line == key_ready:
                break
            if line.startswith(
                "ASTERINAS_PHYSICAL_GRAPHICS_POINTER_READY"
            ) or line.startswith("ASTERINAS_PHYSICAL_GRAPHICS_PASS"):
                raise GateFailure(
                    f"QEMU interaction cycle {cycle} advanced before pointer input"
                )
            if line.startswith("ASTERINAS_PHYSICAL_GRAPHICS_FAIL"):
                raise GateFailure(f"QEMU interaction cycle {cycle} guest failure")
            if line.startswith("__ASTERINAS_PHYSICAL_COMMAND_STATUS__"):
                raise GateFailure(
                    f"QEMU interaction cycle {cycle} exited before key READY"
                )

        input_socket = session["directory"] / "physical-input.qmp"
        move_tablet(
            input_socket,
            # Interior of the frozen 1280x1024 page's button (y=428..575).
            # Distinct endpoints retain motion even if a browser coalesces the
            # origin reset and final move across two consecutive cycles.
            x=(640 + (cycle - 1) * 32) * ABSOLUTE_AXIS_MAX // (QEMU_SCREEN_WIDTH - 1),
            y=500 * ABSOLUTE_AXIS_MAX // (QEMU_SCREEN_HEIGHT - 1),
            timeout=min(10.0, deadline - time.monotonic()),
        )
        pointer_ready = f"ASTERINAS_PHYSICAL_GRAPHICS_POINTER_READY cycle={cycle}"
        while True:
            line, cursor = _next_line(serial, cursor, deadline)
            if line == pointer_ready:
                break
            if line.startswith("ASTERINAS_PHYSICAL_GRAPHICS_PASS"):
                raise GateFailure(
                    f"QEMU interaction cycle {cycle} advanced before button input"
                )
            if line.startswith("ASTERINAS_PHYSICAL_GRAPHICS_FAIL"):
                raise GateFailure(f"QEMU interaction cycle {cycle} guest failure")
            if line.startswith("__ASTERINAS_PHYSICAL_COMMAND_STATUS__"):
                raise GateFailure(
                    f"QEMU interaction cycle {cycle} exited before pointer READY"
                )

        click_left_button(
            input_socket,
            timeout=min(10.0, deadline - time.monotonic()),
        )
        passed = f"ASTERINAS_PHYSICAL_GRAPHICS_PASS cycle={cycle}"
        while True:
            line, cursor = _next_line(serial, cursor, deadline)
            if line == passed:
                break
            if line.startswith("ASTERINAS_PHYSICAL_GRAPHICS_FAIL"):
                raise GateFailure(f"QEMU interaction cycle {cycle} guest failure")
            if line.startswith("__ASTERINAS_PHYSICAL_COMMAND_STATUS__"):
                raise GateFailure(f"QEMU interaction cycle {cycle} exited before PASS")
        status = f"__ASTERINAS_PHYSICAL_COMMAND_STATUS__cycle={cycle} status=0"
        while True:
            line, cursor = _next_line(serial, cursor, deadline)
            if line == status:
                break
            if line.startswith("__ASTERINAS_PHYSICAL_COMMAND_STATUS__"):
                raise GateFailure(f"QEMU interaction cycle {cycle} returned nonzero")

        segment = serial.transcript[start:cursor]
        guest_png = extract_screenshot_frame(
            segment,
            cycle,
            expected_dimensions=(QEMU_SCREEN_WIDTH, QEMU_SCREEN_HEIGHT),
        )
        ppm_path = session["directory"] / f"physical-graphics-qemu-cycle-{cycle}.ppm"
        try:
            rendered_ppm = capture_screendump(
                input_socket,
                ppm_path,
                capture_root=session["directory"],
                timeout=min(30.0, deadline - time.monotonic()),
            )
            rendered = inspect_ppm(
                rendered_ppm,
                expected_width=QEMU_SCREEN_WIDTH,
                expected_height=QEMU_SCREEN_HEIGHT,
            )
        except (OSError, TimeoutError, ValueError) as error:
            raise GateFailure(
                f"QEMU interaction cycle {cycle} framebuffer capture failed: "
                f"{type(error).__name__}"
            ) from error
        evidence = QemuCycleArtifact(
            cycle=cycle,
            nonce_sha256=nonce_sha256,
            guest_png_sha256=hashlib.sha256(guest_png).hexdigest(),
            rendered_ppm_sha256=hashlib.sha256(rendered_ppm).hexdigest(),
            rendered=rendered,
        )
        self._cycle_artifacts.append((evidence, guest_png, rendered_ppm))
        return evidence

    def _verify_final_state(self, serial: Any, config: GateConfig) -> None:
        if self._browser_pid is None or len(self._nonces) != 3:
            raise GateFailure("QEMU terminal state lacks browser identity or nonces")
        timeout = min(config.command_timeout, 300.0)
        deadline = time.monotonic() + QEMU_MARIONETTE_SETUP_TIMEOUT + timeout + 10.0
        cursor = serial.checkpoint()
        serial.send(
            (
                physical_final_command(
                    self._nonces[-1],
                    self._browser_pid,
                    timeout,
                    setup_timeout=QEMU_MARIONETTE_SETUP_TIMEOUT,
                )
                + "\n"
            ).encode(),
            deadline,
        )
        expected_hash = hashlib.sha256(self._nonces[-1].encode()).hexdigest()
        final_seen = False
        while True:
            line, cursor = _next_line(serial, cursor, deadline)
            match = _FINAL.fullmatch(line)
            if match is not None:
                if final_seen or match.group(1) != expected_hash:
                    raise GateFailure("QEMU terminal DOM identity is mismatched")
                final_seen = True
            elif line == "__ASTERINAS_PHYSICAL_FINAL_STATUS__ status=0":
                if not final_seen:
                    raise GateFailure("QEMU terminal DOM marker is missing")
                return
            elif line.startswith("ASTERINAS_PHYSICAL_GRAPHICS_FAIL") or line.startswith(
                "__ASTERINAS_PHYSICAL_FINAL_STATUS__"
            ):
                raise GateFailure("QEMU terminal DOM verification failed")

    @staticmethod
    def _emit_complete(serial: Any, deadline: float) -> None:
        marker = "ASTERINAS_PHYSICAL_GRAPHICS_COMPLETE cycles=3"
        cursor = serial.checkpoint()
        serial.send((f"printf '{marker}\\n'\n").encode(), deadline)
        while True:
            line, cursor = _next_line(serial, cursor, deadline)
            if line == marker:
                return

    def run_protocol(self, session: Mapping[str, Any], config: GateConfig) -> None:
        DesktopM5QemuOperations.run_protocol(self, session, config)
        self._screenshot = b""
        self._screenshot_metadata = {}
        serial = session["serial"]
        self._quiesce_external_services(
            serial, time.monotonic() + config.command_timeout
        )
        self._wait_for_local_graphics(serial, time.monotonic() + config.boot_timeout)
        self._run_debug_console_probe(session, config)
        self._wait_for_marionette(
            serial, time.monotonic() + min(config.boot_timeout, 900.0)
        )
        self._browser_pid = self._query_browser_pid(
            serial, time.monotonic() + config.command_timeout
        )
        self._nonces = tuple(secrets.token_hex(8) for _ in range(3))
        if len(set(self._nonces)) != 3:
            raise GateFailure("QEMU interaction nonces are not distinct")
        self._cycle_artifacts.clear()
        for cycle, nonce in enumerate(self._nonces, start=1):
            self._run_interaction_cycle(
                session,
                config,
                cycle=cycle,
                nonce=nonce,
            )
        rendered_hashes = {
            evidence.rendered_ppm_sha256
            for evidence, _png, _ppm in self._cycle_artifacts
        }
        if len(rendered_hashes) != 3:
            raise GateFailure("QEMU rendered cycle screenshots are not distinct")
        self._verify_final_state(serial, config)
        self._emit_complete(
            serial, time.monotonic() + min(config.command_timeout, 300.0)
        )

    def classify_transcript(
        self, transcript: bytes, *, expected_debian_release: str
    ) -> QemuInteractionResult:
        # This gate deliberately quiesces external network services before it
        # drives Firefox.  Reuse the debug-console lifecycle classifier, but do
        # not inherit the base browser-web gate's ten-layer Internet contract.
        debug: GateResult = classify_debug_console_qemu(
            transcript,
            expected_debian_release=expected_debian_release,
            expected_profile=self._validated_profile or "",
            require_web_network=False,
        )
        if not debug.passed:
            return QemuInteractionResult(False, debug.reason, False, ())
        if len(self._nonces) != 3:
            return QemuInteractionResult(
                False, "QEMU interaction nonces are incomplete", False, ()
            )
        result = _classify_interaction(transcript, self._nonces)
        if not result.passed:
            return result
        artifacts = getattr(self, "_cycle_artifacts", ())
        if artifacts:
            guest_hashes = tuple(
                evidence.guest_png_sha256 for evidence, _png, _ppm in artifacts
            )
            marker_hashes = tuple(cycle.screenshot_sha256 for cycle in result.cycles)
            if guest_hashes != marker_hashes:
                return QemuInteractionResult(
                    False, "QEMU guest screenshot identities do not match", False, ()
                )
        self._classified_cycles = result.cycles
        return result

    def publish(
        self,
        config: GateConfig,
        prepared: Any,
        transcript: bytes,
        result: dict[str, object],
    ) -> None:
        output = self._require_output()
        result["physical"] = False
        result["interaction_cycles"] = [
            asdict(cycle) for cycle in self._classified_cycles
        ]
        result["cycle_artifacts"] = [
            asdict(evidence) for evidence, _png, _ppm in self._cycle_artifacts
        ]
        for evidence, guest_png, rendered_ppm in self._cycle_artifacts:
            output.atomic_write(
                f"physical-graphics-qemu-cycle-{evidence.cycle}.png", guest_png
            )
            output.atomic_write(
                f"physical-graphics-qemu-cycle-{evidence.cycle}.ppm", rendered_ppm
            )
        super().publish(config, prepared, transcript, result)


def orchestrate_physical_graphics_qemu_gate(
    config: GateConfig,
    operations: PhysicalGraphicsQemuOperations,
) -> dict[str, object]:
    """Run one bounded QEMU process and publish the non-physical result."""

    return orchestrate_systemd_m2_gate(
        config,
        operations,
        classifier=operations.classify_transcript,
    )


def main(arguments: list[str] | None = None) -> int:
    try:
        config = parse_gate_args(arguments)
        _safe_output(config.output_directory)
        with (
            TerminationSignalState(),
            PhysicalGraphicsQemuOperations(config) as operations,
        ):
            result = orchestrate_physical_graphics_qemu_gate(config, operations)
        return 0 if result["passed"] else 1
    except SystemExit as error:
        return int(error.code or 0)
    except GateTermination as error:
        print(
            f"physical-graphics-qemu-gate: terminated by signal {error.signum}",
            file=sys.stderr,
        )
        return 128 + error.signum
    except BaseException as error:
        reason = error.reason if isinstance(error, GateFailure) else str(error)
        print(f"physical-graphics-qemu-gate: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
