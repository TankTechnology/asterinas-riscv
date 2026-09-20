#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Gate the Debian desktop profile on a virtio-gpu DRM device."""

from __future__ import annotations

import os
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from tools.riscv.debian.rootfs.contract import load_manifest
from tools.riscv.debian.rootfs.desktop_m3_gate import (
    DesktopM3Operations,
    _ANSI_ESCAPE_RE,
    capture_rendered_ppm,
    classify_desktop,
)
from tools.riscv.debian.rootfs.gate_runtime import GateTermination, TerminationSignalState
from tools.riscv.debian.rootfs.gate_protocol import GateResult, qemu_argv
from tools.riscv.debian.rootfs.rootfs_gate import GateConfig, GateFailure, parse_gate_args
from tools.riscv.debian.rootfs.rootfs_gate_backend import ConcreteOperations, _safe_output
from tools.riscv.debian.rootfs.systemd_m2_gate import orchestrate_systemd_m2_gate


DESKTOP_DRM_BOOTARGS = "console=ttyS0 loglevel=4 init=/init -- --root-init=systemd"

# Head-room between the guest giving up and the gate doing so, and the floor the
# guest deadline is never taken below. Under emulation a desktop boot takes
# minutes, so the floor has to be far above a safety margin rather than below it.
GUEST_DEADLINE_MARGIN_SECONDS = 60
GUEST_DEADLINE_MINIMUM_SECONDS = 120

# The kernel's virtio-gpu DRM driver synthesizes a single 1280x800 mode
# (kernel/src/device/drm/kms.rs), so the Xorg modesetting driver always
# drives the scanout at that geometry.
DESKTOP_DRM_EXPECTED_WIDTH = 1280
DESKTOP_DRM_EXPECTED_HEIGHT = 800

# Seconds to wait after the READY marker before capturing the framebuffer.
# The evidence script reports READY as soon as the session processes exist,
# but under TCG the desktop clients (openbox decorations, lxpanel, pcmanfm
# desktop) paint noticeably later; the strict content thresholds below make
# the capture loop retry until the desktop has actually painted, so the
# settle delay stays disabled by default and remains only a debugging knob.
DESKTOP_DRM_SETTLE_ENV = "ASTERINAS_DESKTOP_DRM_SETTLE_DELAY_SECONDS"
DESKTOP_DRM_SETTLE_DEFAULT_SECONDS = 0.0

# A fully painted desktop (wallpaper, panel, window decorations, anti-aliased
# text) is far richer than an xterm-only frame (2 sampled colors); these
# thresholds keep the capture retrying until the real desktop is visible.
DESKTOP_DRM_MIN_DISTINCT_COLORS = 8
DESKTOP_DRM_MIN_NON_BACKGROUND_RATIO = 0.05

# The virgl variant additionally requires the guest to prove that Mesa talks
# to the host virglrenderer instead of falling back to llvmpipe.
DESKTOP_DRM_GL_PREFIX = "DEBIAN_DESKTOP_DRM_GL renderer="
DESKTOP_DRM_VIRGL_MILESTONE = DESKTOP_DRM_GL_PREFIX + "virgl"

# Every renderer line begins with the prefix above, whatever value follows it.
#
# The gate waits for the prefix rather than for the `renderer=virgl` literal,
# and the difference is not cosmetic. Waiting on the literal made an llvmpipe
# answer -- a real finding, and the one this gate most needs to report --
# indistinguishable from a guest that never got that far: the wait ran out its
# entire window and the run ended as a bare protocol timeout, with the renderer
# never surfaced because the transcript is only written at teardown. Waiting on
# the prefix stops the run as soon as the guest answers at all, and leaves
# `classify_desktop_drm_virgl` to decide whether the answer was good enough.
DESKTOP_DRM_GL_RENDERER_RE = re.compile(r"DEBIAN_DESKTOP_DRM_GL renderer=(\S+)")

# The second, independent question: did anything reach the framebuffer?
#
# The renderer line answers "which Gallium driver did Mesa choose", which is a
# statement about a decision, not about output. A winsys that initialises and
# then renders nothing, or a command stream that never lands, reports
# `renderer=virgl` exactly as happily as a working one -- so on its own it
# cannot tell a working GPU from one that is being talked to and not
# answering. The guest draws a known shape and reads the pixels back; this is
# that verdict.
#
# Required for a 3D run alongside the renderer line, and named by value in the
# reason for the same reason the renderer is: `ok=no left=000000 right=000000`
# names the failure, "missing milestone" does not.
DESKTOP_DRM_PIXEL_PREFIX = "DEBIAN_DESKTOP_DRM_PIXEL "
DESKTOP_DRM_PIXEL_MILESTONE = DESKTOP_DRM_PIXEL_PREFIX + "ok=yes"
DESKTOP_DRM_PIXEL_RE = re.compile(rb"DEBIAN_DESKTOP_DRM_PIXEL (ok=.*)")

DESKTOP_DRM_MILESTONES = (
    "DEBIAN_DESKTOP_DRM_UDEV state=active",
    "DEBIAN_DESKTOP_DRM_LOGIND state=active",
    "DEBIAN_DESKTOP_DRM_SESSION user=asterinas tty=tty1",
    "DEBIAN_DESKTOP_DRM_INPUT keyboard=evdev pointer=evdev",
    "DEBIAN_DESKTOP_DRM_XORG driver=modesetting device=virtio-gpu drm=active display=:0",
    "DEBIAN_DESKTOP_DRM_CLIENTS window-manager=openbox file-manager=pcmanfm panel=lxpanel terminal=xterm",
    "DEBIAN_DESKTOP_DRM_READY user=asterinas display=:0",
)

#: The pixel verdict is listed *before* the renderer line because that is the
#: order the guest emits them in, and `classify_desktop` rejects a transcript
#: whose milestones are out of order. The guest emits it first on purpose: the
#: gate stops waiting as soon as the renderer line arrives, so anything after
#: it risks being cut off at teardown and never reaching the transcript at all.
DESKTOP_DRM_VIRGL_MILESTONES = DESKTOP_DRM_MILESTONES + (
    DESKTOP_DRM_PIXEL_MILESTONE,
    DESKTOP_DRM_VIRGL_MILESTONE,
)


def _qemu_trace_arguments() -> tuple[str, ...]:
    """Return optional `-trace` arguments for local diagnostics.

    Defaults to nothing, so gate behaviour is unchanged unless
    `ASTERINAS_QEMU_TRACE` is set explicitly (for example
    `enable=virtio_gpu_*,file=/tmp/vgpu-trace.log`).
    """

    specification = os.environ.get("ASTERINAS_QEMU_TRACE")
    if not specification:
        return ()
    return ("-trace", specification)


def desktop_drm_qemu_argv(**arguments: Any) -> tuple[str, ...]:
    """Return the graphical QEMU contract with virtio-gpu instead of bochs."""

    arguments.setdefault("smp", 4)
    arguments.setdefault("dtb_enabled_cpu_count", 4)
    arguments["graphical"] = True
    arguments.setdefault("graphics_device", "virtio-gpu-device")
    # The display backend follows the device rather than the caller, because
    # the wrong pairing does not fail: a virgl device under a headless backend
    # comes up quietly without the 3D feature, and a gate that then reported
    # "no 3D" would be reporting its own misconfiguration.
    arguments["display"] = (
        "egl-headless,gl=on"
        if arguments["graphics_device"] == "virtio-gpu-gl-device"
        else "none"
    )
    return qemu_argv(**arguments) + _qemu_trace_arguments()


def desktop_drm_virgl_qemu_argv(**arguments: Any) -> tuple[str, ...]:
    """Return the graphical QEMU contract with the virgl (3D) virtio-gpu."""

    arguments.setdefault("smp", 4)
    arguments.setdefault("dtb_enabled_cpu_count", 4)
    arguments["graphical"] = True
    arguments["graphics_device"] = "virtio-gpu-gl-device"
    arguments["display"] = "egl-headless,gl=on"
    return qemu_argv(**arguments)


def classify_desktop_drm(
    transcript: bytes, *, expected_debian_release: str
) -> GateResult:
    return classify_desktop(
        transcript,
        expected_debian_release=expected_debian_release,
        milestones=DESKTOP_DRM_MILESTONES,
        failure_marker=b"DEBIAN_DESKTOP_DRM_FAIL reason=",
    )


def observed_desktop_drm_renderer(transcript: bytes) -> str | None:
    """Return the GL renderer the guest reported, if it got that far."""

    clean = _ANSI_ESCAPE_RE.sub(b"", transcript).decode("utf-8", "replace")
    match = DESKTOP_DRM_GL_RENDERER_RE.search(clean)
    return match.group(1) if match else None


def observed_desktop_drm_pixels(transcript: bytes) -> bytes | None:
    """Return the guest's pixel verdict, or None if it never reported one.

    None and `ok=no` are different findings and are kept apart deliberately:
    the first says the run never got as far as drawing, the second says it drew
    and the pixels that came back were wrong.
    """
    match = DESKTOP_DRM_PIXEL_RE.search(transcript)
    return match.group(1) if match else None


def classify_desktop_drm_virgl(
    transcript: bytes, *, expected_debian_release: str
) -> GateResult:
    """Classify a 3D run, where a software renderer is a failure.

    The milestone list alone cannot say this. Every other marker is identical
    on llvmpipe -- the desktop comes up, Xorg runs, all five clients appear --
    so the renderer line is the only thing separating a virgl run from one
    that quietly fell back, which is why it is both a required milestone here
    and named by value in the reason.
    """

    result = classify_desktop(
        transcript,
        expected_debian_release=expected_debian_release,
        milestones=DESKTOP_DRM_VIRGL_MILESTONES,
        failure_marker=b"DEBIAN_DESKTOP_DRM_FAIL reason=",
    )
    if result.passed:
        return result
    # Only when the guest actually answered: a missing renderer line is the
    # generic "never got there" case and is better reported as the milestone
    # that is missing than as a renderer that was never observed.
    renderer = observed_desktop_drm_renderer(transcript)
    if renderer is not None and renderer != "virgl":
        return GateResult(False, f"GL renderer was {renderer}, not virgl", None)
    # The renderer answered and was virgl, so the milestone that is missing is
    # the pixel one. Name the pixels rather than let the generic
    # missing-milestone message stand -- a renderer that was chosen and drew
    # nothing is a different problem from one that was never chosen.
    pixels = observed_desktop_drm_pixels(transcript)
    if pixels is not None and not pixels.startswith(b"ok=yes"):
        return GateResult(
            False,
            "renderer was virgl but nothing was drawn: "
            + pixels.decode("utf-8", "replace"),
            None,
        )
    return result


class DesktopDRMOperations(DesktopM3Operations):
    """Reuse the signed-root lifecycle while changing only display evidence."""

    SCHEMA_VERSION = 8
    PROFILE_NAME = "desktop-drm"
    ARTIFACT_PREFIX = "desktop-drm"
    MILESTONES = DESKTOP_DRM_MILESTONES
    FAILURE_MARKER = b"DEBIAN_DESKTOP_DRM_FAIL reason="
    BOOTARGS = DESKTOP_DRM_BOOTARGS

    #: Whether a framebuffer capture can be taken at all.
    #:
    #: Not a policy choice: QEMU cannot screendump an `egl-headless` GL console
    #: ("no surface"), and VNC readback delivers no frames for a virgl scanout
    #: either. Asking for one there fails the run on a protocol error that says
    #: nothing about the guest, so the capture is skipped and the evidence is
    #: what the guest reports about itself.
    _capture_screenshot: bool = True
    _milestones: tuple[str, ...] = DESKTOP_DRM_MILESTONES

    @property
    def CAPTURE_SCREENSHOT(self) -> bool:  # type: ignore[override]
        return self._capture_screenshot

    @property
    def MILESTONES(self) -> tuple[str, ...]:  # type: ignore[override]
        return self._milestones

    @staticmethod
    def _qemu_argv(**arguments: Any) -> tuple[str, ...]:
        return desktop_drm_qemu_argv(**arguments)

    def __init__(self, config: GateConfig, **arguments: Any) -> None:
        super().__init__(config, **arguments)
        # Derived from the device, not from `config.display`: the display is
        # chosen inside `desktop_drm_qemu_argv`, after this config is built, so
        # reading it here would still see the default and try to capture.
        self._capture_screenshot = config.graphics_device != "virtio-gpu-gl-device"
        # Asking for the GL device is asking for the 3D path, so the run has to
        # prove it got there: a desktop that came up on llvmpipe satisfies
        # every other milestone identically, and would otherwise be reported as
        # a passing virgl run.
        self._requires_virgl = config.graphics_device == "virtio-gpu-gl-device"
        self._milestones = (
            DESKTOP_DRM_VIRGL_MILESTONES
            if self._requires_virgl
            else DESKTOP_DRM_MILESTONES
        )

    @property
    def REQUIRES_VIRGL(self) -> bool:
        return self._requires_virgl

    @property
    def TERMINAL_MARKER(self) -> bytes:
        """The marker whose arrival means the guest has said all it is going to.

        For a 3D run that is the renderer line's *prefix*, so the run ends on
        the answer however the answer reads; waiting for `renderer=virgl`
        itself would only ever end early on the outcome that needs no
        reporting. The value is graded afterwards, not waited on.
        """

        return (
            DESKTOP_DRM_GL_PREFIX.encode()
            if self.REQUIRES_VIRGL
            else self.MILESTONES[-1].encode()
        )

    def serial_observer(
        self, config: GateConfig, boot_number: int
    ) -> Callable[[bytes], None] | None:
        """Tee serial bytes to a file while the boot is still running.

        The transcript is otherwise written in one piece at teardown, so from
        outside a guest that has stalled and a guest that is merely slow look
        exactly alike -- both are silence, both cost the same half hour to
        discover, and the run that would tell them apart is the one already
        running. A live copy costs one write per chunk and makes the boot
        observable with `tail -f` while it happens.
        """

        del boot_number
        path = Path(config.output_directory) / f"{self.ARTIFACT_PREFIX}.live.log"
        try:
            handle = open(path, "ab", buffering=0)
        except OSError:
            # Observability is a convenience; it must never fail the run it is
            # trying to make visible.
            return None

        def observe(chunk: bytes) -> None:
            try:
                handle.write(chunk)
            except OSError:
                pass

        return observe

    def invalidate(self, config: GateConfig) -> None:
        self._require_config(config)
        self._require_output().invalidate(
            "boot.ext4",
            "debian-root.run.ext2",
            f"{self.ARTIFACT_PREFIX}.serial.log",
            f"{self.ARTIFACT_PREFIX}.live.log",
            f"{self.ARTIFACT_PREFIX}.ppm",
            "result.json",
        )

    def validate_inputs(
        self, config: GateConfig, snapshots: Mapping[str, str]
    ) -> Mapping[str, object]:
        identity = dict(ConcreteOperations.validate_inputs(self, config, snapshots))
        manifest = load_manifest(self.input_paths["manifest"])
        if manifest.schema_version != self.SCHEMA_VERSION or manifest.profile != self.PROFILE_NAME:
            raise GateFailure("rootfs manifest is not the desktop-drm profile")
        identity["profile"] = manifest.profile
        return identity

    def _boot_commands(self, config: GateConfig) -> tuple[str, ...]:
        guest_deadline = self._guest_deadline_seconds(config.boot_timeout)
        return (
            "virtio scan",
            "ext4load virtio 0:0 0x80200000 /asterinas.booti",
            "ext4load virtio 0:0 0x90000000 /qemu-virt.dtb",
            "fdt addr 0x90000000",
            "fdt resize 0x1000",
            "ext4load virtio 0:0 0x83000000 /stage1-initramfs.cpio",
            "setenv initrd_size ${filesize}",
            f'setenv bootargs "{self._bootargs(guest_deadline)}"',
        )

    @staticmethod
    def _guest_deadline_seconds(boot_timeout: float) -> int:
        """Return the guest's deadline as an absolute uptime, in seconds.

        It has to be absolute, not a duration: the gate's window starts when
        QEMU starts, while the guest's script only starts once the boot has
        reached basic.target.  A duration measured from the script's own start
        would therefore always expire after the gate's deadline, and the guest
        would never get to report which condition was unmet.
        """

        deadline = int(boot_timeout) - GUEST_DEADLINE_MARGIN_SECONDS
        return max(GUEST_DEADLINE_MINIMUM_SECONDS, deadline)

    @classmethod
    def _bootargs(cls, guest_deadline: int | None = None) -> str:
        # Debugging knob: ASTERINAS_DESKTOP_DRM_BOOTARGS replaces the kernel
        # command line, e.g. to raise the log level and turn on
        # asterinas.trace_syscall_errors for ioctl-level diagnosis.
        command_line = os.environ.get("ASTERINAS_DESKTOP_DRM_BOOTARGS", cls.BOOTARGS)
        if guest_deadline is None:
            return command_line
        # Only the kernel's own arguments go before the `--` that separates
        # them from the stage-1 arguments.
        kernel_arguments, separator, init_arguments = command_line.partition(" -- ")
        kernel_arguments = (
            f"{kernel_arguments} asterinas.desktop_drm_deadline={guest_deadline}"
        )
        return (
            f"{kernel_arguments}{separator}{init_arguments}"
            if separator
            else kernel_arguments
        )

    def run_protocol(self, session: dict[str, Any], config: GateConfig) -> None:
        serial = session["serial"]
        deadline = time.monotonic() + config.boot_timeout
        serial.wait_for(b"=> ", deadline)
        for index, command in enumerate(self._boot_commands(config), 1):
            self._send_uboot(session, command, index, deadline)

        marker = f"__ASTERINAS_DESKTOP_DRM_BOOT_{secrets.token_hex(8).upper()}__"
        serial.send(
            (
                f"echo {marker}; booti 0x80200000 "
                "0x83000000:${initrd_size} 0x90000000\n"
            ).encode(),
            deadline,
        )
        serial.wait_for(marker.encode(), deadline)
        serial.wait_for(b"Starting kernel ...", deadline)
        completion = serial.wait_for_any(
            (self.TERMINAL_MARKER, self.FAILURE_MARKER),
            time.monotonic() + config.boot_timeout,
        )
        if completion.startswith(self.FAILURE_MARKER.split(b" reason=", 1)[0]):
            raise GateFailure("guest reported DRM desktop failure")

        settle = float(os.environ.get(DESKTOP_DRM_SETTLE_ENV, DESKTOP_DRM_SETTLE_DEFAULT_SECONDS))
        if settle > 0:
            time.sleep(settle)

        if not self.CAPTURE_SCREENSHOT:
            return

        screenshot = session["directory"] / f"{self.ARTIFACT_PREFIX}.ppm"
        self._screenshot, self._screenshot_metadata = capture_rendered_ppm(
            session["monitor"],
            screenshot,
            time.monotonic() + config.command_timeout,
            expected_width=DESKTOP_DRM_EXPECTED_WIDTH,
            expected_height=DESKTOP_DRM_EXPECTED_HEIGHT,
            min_distinct_colors=DESKTOP_DRM_MIN_DISTINCT_COLORS,
            min_non_background_ratio=DESKTOP_DRM_MIN_NON_BACKGROUND_RATIO,
        )


def orchestrate_desktop_drm_gate(
    config: GateConfig,
    operations: DesktopDRMOperations,
    *,
    classifier: Any = None,
) -> dict[str, object]:
    # A 3D run is graded on whether the renderer is virgl; a 2D run has no
    # renderer to grade and must not acquire a requirement for one.
    if classifier is None:
        classifier = (
            classify_desktop_drm_virgl
            if operations.REQUIRES_VIRGL
            else classify_desktop_drm
        )
    return orchestrate_systemd_m2_gate(config, operations, classifier=classifier)


def main(arguments: list[str] | None = None) -> int:
    try:
        config = parse_gate_args(arguments)
        _safe_output(config.output_directory)
        with TerminationSignalState(), DesktopDRMOperations(config) as operations:
            result = orchestrate_desktop_drm_gate(config, operations)
        return 0 if result["passed"] else 1
    except SystemExit as error:
        return int(error.code or 0)
    except GateTermination as error:
        print(f"debian-desktop-drm-gate: terminated by signal {error.signum}", file=sys.stderr)
        return 128 + error.signum
    except BaseException as error:
        reason = error.reason if isinstance(error, GateFailure) else str(error)
        print(f"debian-desktop-drm-gate: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
