#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Gate the Debian desktop profile on a virtio-gpu DRM device."""

from __future__ import annotations

import os
import re
from functools import partial
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from tools.riscv.qemu_uboot_devices import BOCHS_XRGB8888
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

#: The DRM driver sysfs binds to the card node, per display device.
#:
#: The value is the kernel's spelling -- `virtio_gpu` with an underscore, not
#: the QEMU device name -- because it is what the guest reads from
#: `/sys/dev/char/<maj>:<min>/device/uevent` and reports.
_DRM_DRIVER_FOR_DEVICE = {
    "bochs-display": "simpledrm",
    "virtio-gpu-device": "virtio_gpu",
    "virtio-gpu-gl-device": "virtio_gpu",
}


def desktop_drm_milestones(
    graphics_device: str, *, virgl: bool = False
) -> tuple[str, ...]:
    """The guest markers a run on `graphics_device` has to produce.

    The Xorg line names the DRM driver, and which driver that is depends on the
    display device: a virtio-gpu presents through `virtio_gpu`, while a bochs
    display has no GPU at all and what presents is the firmware backend's
    `simpledrm`.

    Naming the driver here, and having the guest observe it there, is the whole
    of the check. That line used to be the literal `device=virtio-gpu` on both
    sides -- matched by this module, printed by the guest unconditionally -- so
    the two agreed by construction and the milestone could not fail on any
    machine, including one where it was simply false.

    The pixel verdict is listed *before* the renderer line because that is the
    order the guest emits them in, and `classify_desktop` rejects a transcript
    whose milestones are out of order. The guest emits it first on purpose: the
    gate stops waiting as soon as the renderer line arrives, so anything after
    it risks being cut off at teardown and never reaching the transcript.
    """

    try:
        driver = _DRM_DRIVER_FOR_DEVICE[graphics_device]
    except KeyError as error:
        raise ValueError(
            f"no DRM driver is registered for the display device "
            f"{graphics_device!r}"
        ) from error

    milestones = (
        "DEBIAN_DESKTOP_DRM_UDEV state=active",
        "DEBIAN_DESKTOP_DRM_LOGIND state=active",
        "DEBIAN_DESKTOP_DRM_SESSION user=asterinas tty=tty1",
        "DEBIAN_DESKTOP_DRM_INPUT keyboard=evdev pointer=evdev",
        f"DEBIAN_DESKTOP_DRM_XORG driver=modesetting device={driver} "
        "drm=active display=:0",
        "DEBIAN_DESKTOP_DRM_CLIENTS window-manager=openbox file-manager=pcmanfm "
        "panel=lxpanel terminal=xterm",
        "DEBIAN_DESKTOP_DRM_READY user=asterinas display=:0",
    )
    if not virgl:
        return milestones
    return milestones + (DESKTOP_DRM_PIXEL_MILESTONE, DESKTOP_DRM_VIRGL_MILESTONE)


#: The scanout geometry each display device presents.
#:
#: A virtio-gpu's mode is synthesized by the kernel's driver, so it is the same
#: on every machine; a bochs display's is whatever QEMU programmed and U-Boot
#: wrote into the device tree, which is `BOCHS_XRGB8888`. Taken from that
#: contract rather than restated, so the two cannot disagree about how big the
#: frame is.
_SCREENSHOT_GEOMETRY = {
    "bochs-display": (BOCHS_XRGB8888.width, BOCHS_XRGB8888.height),
    "virtio-gpu-device": (DESKTOP_DRM_EXPECTED_WIDTH, DESKTOP_DRM_EXPECTED_HEIGHT),
    "virtio-gpu-gl-device": (DESKTOP_DRM_EXPECTED_WIDTH, DESKTOP_DRM_EXPECTED_HEIGHT),
}


def desktop_drm_screenshot_geometry(graphics_device: str) -> tuple[int, int]:
    """The frame size a screendump of `graphics_device` must have.

    The capture rejects any other size, so a run whose display is a different
    shape fails with `unexpected PPM geometry`. That is what the firmware path
    did: it was handed the virtio mode's 1280x800 while the bochs framebuffer
    U-Boot described is 1280x1024.
    """

    try:
        return _SCREENSHOT_GEOMETRY[graphics_device]
    except KeyError as error:
        raise ValueError(
            f"no screenshot geometry is registered for the display device "
            f"{graphics_device!r}"
        ) from error


#: The virtio-gpu expectations, kept as named constants because most callers
#: want the ordinary desktop and should not have to name a device to get it.
DESKTOP_DRM_MILESTONES = desktop_drm_milestones("virtio-gpu-device")
DESKTOP_DRM_VIRGL_MILESTONES = desktop_drm_milestones(
    "virtio-gpu-gl-device", virgl=True
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


def _firmware_framebuffer_commands(graphics_device: str) -> tuple[str, ...]:
    """Write the firmware framebuffer node for a display that has no GPU.

    With `virtio-gpu` the driver presents through the device and never consults
    the device tree. A `bochs-display` is the opposite case: there is no GPU at
    all, so the only display the kernel can find is the one the bootloader
    describes, and `ostd`'s `simple_framebuffer` parser reads it from here. No
    node, no framebuffer, no DRM node -- and the run would fail for a reason
    that has nothing to do with the driver.

    The constants are `BOCHS_XRGB8888`, the same contract the U-Boot-profile
    gates use. Imported rather than restated: a second copy of a base address
    is exactly how two of them drift apart.
    """

    if graphics_device != "bochs-display":
        return ()

    framebuffer = BOCHS_XRGB8888
    node = f"/framebuffer@{framebuffer.address:x}"
    return (
        # Confirms the display landed where the contract says before anything
        # is written about it, so a moved BAR fails here rather than as pixels
        # in the wrong place much later.
        "pci display 0.1.0",
        f"fdt mknode / {node[1:]}",
        f'fdt set {node} compatible "simple-framebuffer"',
        f"fdt set {node} reg <0x0 {framebuffer.address:#x} "
        f"0x0 {framebuffer.size:#x}>",
        f"fdt set {node} width <{framebuffer.width:#x}>",
        f"fdt set {node} height <{framebuffer.height:#x}>",
        f"fdt set {node} stride <{framebuffer.stride:#x}>",
        f'fdt set {node} format "{framebuffer.pixel_format}"',
        f'fdt set {node} status "okay"',
    )


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
    transcript: bytes,
    *,
    expected_debian_release: str,
    milestones: tuple[str, ...] = DESKTOP_DRM_MILESTONES,
) -> GateResult:
    """Grade a 2D run against the markers its own display device should produce.

    `milestones` defaults to the virtio-gpu expectations for callers that mean
    the ordinary desktop, but a run on a `bochs-display` has to pass its own:
    the Xorg line names the driver, and this device's driver is not that one.
    The orchestrator passes `operations.MILESTONES`; without that, a firmware
    run was graded against a virtio milestone it could never produce and failed
    with a reason naming a string the guest had no business emitting.
    """

    return classify_desktop(
        transcript,
        expected_debian_release=expected_debian_release,
        milestones=milestones,
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
    transcript: bytes,
    *,
    expected_debian_release: str,
    milestones: tuple[str, ...] = DESKTOP_DRM_VIRGL_MILESTONES,
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
        milestones=milestones,
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


#: The band a kernel image that can boot this gate falls in.
#:
#: `cargo osdk test` rewrites `target/osdk/aster-kernel-osdk-bin.Image` **in
#: place** with what the ktests need -- a dev-profile build of an Sv48 kernel --
#: and that is the same path `DEBIAN_DRM_KERNEL` defaults to. Two artifacts,
#: one filename, and nothing announces the swap.
#:
#: Both ways of getting it wrong end identically: U-Boot prints
#: "Starting kernel ...", the CPU faults on the first page-table walk, and
#: **nothing is printed again, ever**. The gate sees a silent serial line and
#: waits out its entire boot timeout, which for twenty-five minutes looks
#: exactly like a run that is still working.
#:
#: Size is a proxy and is offered as one: it cannot tell a correct kernel from
#: a merely small one. What it can do is turn a silent stall into one sentence,
#: which is the whole of the claim. The margins are wide deliberately -- a
#: release build is about 6 MB against about 14 MB for the test build and about
#: 4 MB for an Sv48 release build -- so ordinary growth will not trip it. The
#: semantic version of this check, if anyone wants it, is to require *some*
#: kernel output within a bounded window after the handoff, which would catch
#: any cause of a silent hang rather than these two.
DESKTOP_DRM_KERNEL_MIN_BYTES = 5 * 1024 * 1024
DESKTOP_DRM_KERNEL_MAX_BYTES = 10 * 1024 * 1024


def reject_a_kernel_that_cannot_boot(kernel: Path) -> None:
    """Refuse a kernel image that would hang without saying anything.

    See `DESKTOP_DRM_KERNEL_MIN_BYTES` for what this is, and what it is not.
    """
    size = kernel.stat().st_size
    if DESKTOP_DRM_KERNEL_MIN_BYTES <= size <= DESKTOP_DRM_KERNEL_MAX_BYTES:
        return
    raise GateFailure(
        f"kernel image is {size} bytes, outside the "
        f"{DESKTOP_DRM_KERNEL_MIN_BYTES}..{DESKTOP_DRM_KERNEL_MAX_BYTES} band "
        f"a bootable one occupies, so it would hang at 'Starting kernel ...' "
        f"with no output at all and the run would look like it was working. "
        f"Rebuild it with: make kernel RELEASE=1 FEATURES=riscv_sv39_mode "
        f"TARGET_ARCH=riscv64 CARGO_OSDK=$PWD/.osdk-bin/bin/cargo-osdk. "
        f"A preceding `cargo osdk test` is the usual reason it is wrong."
    )


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
        # Before anything is launched: this failure is silent on the serial
        # line, so it has to be caught here or it is not caught at all.
        reject_a_kernel_that_cannot_boot(config.kernel)
        # Derived from the device, not from `config.display`: the display is
        # chosen inside `desktop_drm_qemu_argv`, after this config is built, so
        # reading it here would still see the default and try to capture.
        self._capture_screenshot = config.graphics_device != "virtio-gpu-gl-device"
        # Asking for the GL device is asking for the 3D path, so the run has to
        # prove it got there: a desktop that came up on llvmpipe satisfies
        # every other milestone identically, and would otherwise be reported as
        # a passing virgl run.
        self._requires_virgl = config.graphics_device == "virtio-gpu-gl-device"
        self._milestones = desktop_drm_milestones(
            config.graphics_device, virgl=self._requires_virgl
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
        commands = [
            "virtio scan",
            "ext4load virtio 0:0 0x80200000 /asterinas.booti",
            "ext4load virtio 0:0 0x90000000 /qemu-virt.dtb",
            "fdt addr 0x90000000",
            "fdt resize 0x1000",
        ]
        commands.extend(_firmware_framebuffer_commands(config.graphics_device))
        commands.extend(
            [
                "ext4load virtio 0:0 0x83000000 /stage1-initramfs.cpio",
                "setenv initrd_size ${filesize}",
                f'setenv bootargs "{self._bootargs(guest_deadline)}"',
            ]
        )
        return tuple(commands)

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

        # From the display device, not from a constant: the bochs framebuffer
        # U-Boot describes is a different shape from the mode the virtio-gpu
        # driver synthesizes, and one of the two would be rejected.
        expected_width, expected_height = desktop_drm_screenshot_geometry(
            config.graphics_device
        )
        screenshot = session["directory"] / f"{self.ARTIFACT_PREFIX}.ppm"
        self._screenshot, self._screenshot_metadata = capture_rendered_ppm(
            session["monitor"],
            screenshot,
            time.monotonic() + config.command_timeout,
            expected_width=expected_width,
            expected_height=expected_height,
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
    #
    # The milestones are bound here rather than left to the classifiers'
    # defaults, because which ones apply depends on the display device this run
    # launched: a `bochs-display` run has to be graded against the driver it
    # actually presents through. Using the defaults graded a firmware run
    # against the virtio expectations and reported a milestone missing that the
    # guest had no business emitting.
    if classifier is None:
        milestones = operations.MILESTONES
        classifier = partial(
            classify_desktop_drm_virgl if operations.REQUIRES_VIRGL
            else classify_desktop_drm,
            milestones=milestones,
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
