#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Cold-boot the signed Debian application desktop and capture visual evidence."""

from __future__ import annotations

import os
import sys
import re
import time
from typing import Any, Mapping

from tools.riscv.debian.rootfs.desktop_m3_gate import (
    DesktopM3Operations,
    _bounded_uboot_bootargs_commands,
    classify_desktop,
    desktop_m3_qemu_argv,
)
from tools.riscv.debian.rootfs.gate_protocol import GateResult, qemu_argv
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
from tools.riscv.drm.virgl_host_preflight import inspect_qemu, run_runtime_smoke


# Keep the desktop gate self-contained: the QEMU root disk is the second
# virtio block device in gate_protocol.qemu_argv, therefore it is /dev/vdb.
# The independent software deadline is a last-resort recovery boundary; it
# is intentionally also present in the ordinary boot so a hang cannot leave
# this QEMU invocation alive indefinitely.
DESKTOP_M4_BOOTARGS = (
    "console=ttyS0 console=tty0 loglevel=4 "
    "asterinas.first_process_diag=1 asterinas.first_process_diag_force=1 "
    "asterinas.reboot_after=300 "
    "asterinas.desktop_ordered=1 "
    "systemd.setenv=ASTERINAS_DESKTOP_INPUT_IDENTITY_MODE=ordered "
    "init=/init -- --root-init=systemd --root-device=/dev/vdb"
)

DESKTOP_M4_BOOT_PHASES = (
    b"ASTERINAS_BOOT_PHASE phase=first-kthread-enter",
    b"ASTERINAS_BOOT_PHASE phase=kthread-init-done",
    b"ASTERINAS_BOOT_PHASE phase=banner-begin",
    b"ASTERINAS_BOOT_PHASE phase=banner-done",
    b"ASTERINAS_BOOT_PHASE phase=init-spawned",
    b"ASTERINAS_FIRST_PROCESS_DIAG stage=process_components_ready",
    b"ASTERINAS_FIRST_PROCESS_DIAG stage=device_init_ready",
    b"ASTERINAS_FIRST_PROCESS_DIAG stage=stdio_init_ready",
    b"DEBIAN_STAGE1_PROGRESS step=start mode=systemd",
    b"DEBIAN_STAGE1_PROGRESS step=root-select source=explicit device=/dev/vdb",
    b"DEBIAN_STAGE1_PROGRESS step=probe-block device=/dev/vdb",
    b"DEBIAN_STAGE1_PROGRESS step=probe-complete result=match",
    b"DEBIAN_STAGE1_PROGRESS step=root-found device=/dev/vdb",
    b"DEBIAN_STAGE1_PROGRESS step=handoff-enter action=root-mount",
    b"DEBIAN_STAGE1_PROGRESS step=handoff-done action=root-mount",
)

# The guest probe service allows 300 seconds for Firefox's cold RISC-V
# startup.  Keep a separate host margin so a too-short CLI deadline cannot
# kill QEMU before the service publishes its terminal receipt (r97 did this
# with --boot-timeout=180).
FIREFOX_PROBE_GUEST_TIMEOUT_SECONDS = 300.0
FIREFOX_PROBE_HOST_MARGIN_SECONDS = 60.0


def firefox_probe_guest_timeout_seconds() -> float:
    """Return the bounded guest probe budget selected for this run."""

    raw = os.environ.get(
        "ASTERINAS_FIREFOX_PROBE_TIMEOUT",
        str(FIREFOX_PROBE_GUEST_TIMEOUT_SECONDS),
    ).strip()
    try:
        value = float(raw)
    except ValueError as error:
        raise GateFailure("firefox-probe-timeout: expected a number") from error
    if not 10.0 <= value <= 600.0:
        raise GateFailure("firefox-probe-timeout: expected 10..600 seconds")
    return value


def validate_firefox_probe_boot_timeout(timeout: float) -> float:
    """Reject a host deadline that can preempt the guest probe receipt."""

    required = firefox_probe_guest_timeout_seconds() + FIREFOX_PROBE_HOST_MARGIN_SECONDS
    if timeout < required:
        raise GateFailure(
            "firefox-probe-timeout-budget-too-small: "
            f"{timeout:.1f}s < required {required:.1f}s"
        )
    return timeout


def validate_boot_phase_contract(transcript: bytes) -> GateResult:
    """Require one ordered kernel-to-rootfs trace for a display-safe boot."""

    positions = [transcript.find(marker) for marker in DESKTOP_M4_BOOT_PHASES]
    missing = [
        marker.decode("ascii")
        for marker, position in zip(DESKTOP_M4_BOOT_PHASES, positions)
        if position < 0
    ]
    if missing:
        return GateResult(False, f"missing boot phase: {missing[0]}", None)
    if positions != sorted(positions):
        return GateResult(False, "boot phases out of order", None)
    return GateResult(True, "pass", None)


DESKTOP_M4_MILESTONES = (
    "DEBIAN_DESKTOP_M4_UDEV state=active",
    "DEBIAN_DESKTOP_M4_LOGIND state=active",
    "DEBIAN_DESKTOP_M4_SESSION user=asterinas tty=tty1",
    "DEBIAN_DESKTOP_M4_INPUT keyboard=evdev pointer=evdev",
    "DEBIAN_DESKTOP_M4_XORG framebuffer=fbdev display=:0",
    "DEBIAN_DESKTOP_M4_SHELL wallpaper=asterinas desktop=pcmanfm panel=lxpanel launchers=3",
    "DEBIAN_DESKTOP_M4_CLIENTS window-manager=openbox file-manager=pcmanfm browser=netsurf terminal=xterm",
    "DEBIAN_DESKTOP_M4_READY user=asterinas display=:0",
)

DESKTOP_M4_CORE_MILESTONES = (
    "DEBIAN_DESKTOP_M4_UDEV state=active",
    "DEBIAN_DESKTOP_M4_LOGIND state=active",
    "DEBIAN_DESKTOP_M4_SESSION user=asterinas tty=tty1",
    "DEBIAN_DESKTOP_M4_INPUT keyboard=evdev pointer=evdev",
    "DEBIAN_DESKTOP_M4_XORG framebuffer=fbdev display=:0",
    "DEBIAN_DESKTOP_M4_SHELL wallpaper=asterinas desktop=pcmanfm panel=lxpanel launchers=3",
    "DEBIAN_DESKTOP_M4_CORE_CLIENTS window-manager=openbox file-manager=pcmanfm panel=lxpanel terminal=xterm",
    "DEBIAN_DESKTOP_M4_CORE_READY user=asterinas display=:0",
)

DESKTOP_M4_DISPLAY_MILESTONES = (
    "DEBIAN_DESKTOP_M4_UDEV state=active",
    "DEBIAN_DESKTOP_M4_LOGIND state=active",
    "DEBIAN_DESKTOP_M4_SESSION user=asterinas tty=tty1",
    "DEBIAN_DESKTOP_M4_XORG framebuffer=fbdev display=:0",
    "DEBIAN_DESKTOP_M4_SHELL wallpaper=asterinas desktop=pcmanfm panel=lxpanel launchers=3",
    "DEBIAN_DESKTOP_M4_DISPLAY_CLIENTS window-manager=openbox file-manager=pcmanfm panel=lxpanel terminal=xterm input=disabled",
    "DEBIAN_DESKTOP_M4_DISPLAY_READY user=asterinas display=:0",
)

# The DRM provider has no firmware framebuffer handoff.  Keep its readiness
# contract separate so a DRM boot cannot satisfy the fbdev gate by emitting a
# stale simple-framebuffer record.  The classifier separately requires the
# bounded Xorg root-window pixel-hash marker emitted by the guest evidence
# script.
DESKTOP_M4_DRM_MILESTONES = (
    "DEBIAN_DESKTOP_M4_UDEV state=active",
    "DEBIAN_DESKTOP_M4_LOGIND state=active",
    "DEBIAN_DESKTOP_M4_SESSION user=asterinas tty=tty1",
    "DEBIAN_DESKTOP_M4_INPUT keyboard=evdev pointer=evdev",
    "DEBIAN_DESKTOP_M4_DRM_FBDEV dependency=none action=skipped",
    "DEBIAN_DESKTOP_M4_DRM_CONFIG driver=modesetting kmsdev=/dev/dri/card0 fbdev=none",
    "DEBIAN_DESKTOP_M4_DRM_SYSFS card=226:0 render=226:128 driver=virtio_gpu",
    "DEBIAN_DESKTOP_M4_DRM_NODES card=/dev/dri/card0 render=/dev/dri/renderD128",
    "DEBIAN_DESKTOP_M4_XORG provider=drm display=:0 renderer=virgl",
    "DEBIAN_DESKTOP_M4_SHELL wallpaper=asterinas desktop=pcmanfm panel=lxpanel launchers=3",
    "DEBIAN_DESKTOP_M4_CLIENTS window-manager=openbox file-manager=pcmanfm browser=netsurf terminal=xterm",
    "DEBIAN_DESKTOP_M4_READY user=asterinas display=:0",
)

# The QEMU DRM smoke profile deliberately disables the browser so the display
# contract can be tested without network or browser variability. Its session
# emits the core-client pair instead of the browser-client pair; keep that
# variant just as strict and ordered as the full desktop profile.
DESKTOP_M4_DRM_CORE_MILESTONES = (
    "DEBIAN_DESKTOP_M4_UDEV state=active",
    "DEBIAN_DESKTOP_M4_LOGIND state=active",
    "DEBIAN_DESKTOP_M4_SESSION user=asterinas tty=tty1",
    "DEBIAN_DESKTOP_M4_INPUT keyboard=evdev pointer=evdev",
    "DEBIAN_DESKTOP_M4_DRM_FBDEV dependency=none action=skipped",
    "DEBIAN_DESKTOP_M4_DRM_CONFIG driver=modesetting kmsdev=/dev/dri/card0 fbdev=none",
    "DEBIAN_DESKTOP_M4_DRM_SYSFS card=226:0 render=226:128 driver=virtio_gpu",
    "DEBIAN_DESKTOP_M4_DRM_NODES card=/dev/dri/card0 render=/dev/dri/renderD128",
    "DEBIAN_DESKTOP_M4_XORG provider=drm display=:0 renderer=virgl",
    "DEBIAN_DESKTOP_M4_SHELL wallpaper=asterinas desktop=pcmanfm panel=lxpanel launchers=3",
    "DEBIAN_DESKTOP_M4_CORE_CLIENTS window-manager=openbox file-manager=pcmanfm panel=lxpanel terminal=xterm",
    "DEBIAN_DESKTOP_M4_CORE_READY user=asterinas display=:0",
)

DESKTOP_M4_DRM_BOOTARGS = DESKTOP_M4_BOOTARGS.replace(
    " -- --root-init=systemd --root-device=/dev/vdb",
    " asterinas.display_provider=drm -- --root-init=systemd --root-device=/dev/vdb",
)
DESKTOP_M4_DRM_X11_SCREENSHOT_PATTERN = re.compile(
    rb"DEBIAN_DESKTOP_M4_DRM_X11_SCREENSHOT width=1280 height=800 "
    rb"bpp=[1-9][0-9]* stride=[1-9][0-9]* bytes=[1-9][0-9]* "
    rb"sha256=[0-9a-f]{64}"
)
DESKTOP_M4_DRM_EGL_PIXEL_PATTERN = re.compile(
    rb"PIXEL_PROBE ok=yes .*renderer=[^\r\n]*virgl[^\r\n]*"
)
DRM_SOFTWARE_RENDERER_MARKERS = (b"llvmpipe", b"softpipe", b"swrast", b"swgl")

# These records contain per-boot hashes and therefore cannot be represented as
# literal milestones.  The physical gate validates their exact grammar and
# places them between the shell and display-client records.
DESKTOP_M4_DISPLAY_FRAMEBUFFER_PATTERNS = (
    r"DEBIAN_DESKTOP_M4_FB_IOCTL width=1920 height=1080 stride=7680 bpp=32 format=BgrReserved",
    r"DEBIAN_DESKTOP_M4_FB_SAMPLE bytes=4096 sha256=[0-9a-f]{64}",
    r"DEBIAN_DESKTOP_M4_FB_SCREENSHOT path=/run/asterinas-desktop-m4-framebuffer\.png width=1920 height=1080 bytes=[1-9][0-9]* sha256=[0-9a-f]{64}",
)
DESKTOP_M4_QEMU_FRAMEBUFFER_PATTERN = re.compile(
    rb"DEBIAN_DESKTOP_M4_FB_IOCTL width=1280 height=1024 stride=5120 "
    rb"bpp=32 format=BgrReserved\r*\n"
    rb"DEBIAN_DESKTOP_M4_FB_SAMPLE bytes=4096 sha256=[0-9a-f]{64}\r*\n"
    rb"DEBIAN_DESKTOP_M4_FB_COLORS distinct=[2-9][0-9]* "
    rb"non_background=[1-9][0-9]*\r*\n"
)
DESKTOP_M4_FRAMEBUFFER_HANDOFF_PATTERN = re.compile(
    rb"Registered firmware framebuffer: base=0x40000000, size=0x1000000, "
    rb"resolution=1280x1024, stride=5120, format=BgrReserved"
)
DESKTOP_M4_UBOOT_FRAMEBUFFER_COMMANDS = (
    b"pci display 0.1.0",
    b"fdt mknode / framebuffer@40000000",
    b"fdt set /framebuffer@40000000 compatible \"simple-framebuffer\"",
    b"fdt set /framebuffer@40000000 reg <0x0 0x40000000 0x0 0x1000000>",
    b"fdt set /framebuffer@40000000 width <0x500>",
    b"fdt set /framebuffer@40000000 height <0x400>",
    b"fdt set /framebuffer@40000000 stride <0x1400>",
    b"fdt set /framebuffer@40000000 format \"x8r8g8b8\"",
    b"fdt set /framebuffer@40000000 status \"okay\"",
)


def validate_framebuffer_handoff(transcript: bytes) -> GateResult:
    """Require one simple-framebuffer owner registration for one boot."""

    registrations = DESKTOP_M4_FRAMEBUFFER_HANDOFF_PATTERN.findall(transcript)
    if len(registrations) > 1:
        return GateResult(False, "duplicate framebuffer owner registration", None)
    command_positions = [transcript.find(command) for command in DESKTOP_M4_UBOOT_FRAMEBUFFER_COMMANDS]
    has_uboot_handoff = all(position >= 0 for position in command_positions)
    if has_uboot_handoff and command_positions != sorted(command_positions):
        return GateResult(False, "simple-framebuffer metadata out of order", None)
    if not registrations and not has_uboot_handoff:
        return GateResult(False, "missing simple-framebuffer metadata", None)
    if command_positions[0] >= 0 and transcript.find(
        DESKTOP_M4_UBOOT_FRAMEBUFFER_COMMANDS[0], command_positions[0] + 1
    ) >= 0:
        return GateResult(False, "duplicate framebuffer owner registration", None)
    return GateResult(True, "pass", None)


def desktop_m4_qemu_argv(**arguments: Any) -> tuple[str, ...]:
    """Return the same frozen graphical QEMU device contract used by M3."""

    return desktop_m3_qemu_argv(**arguments)


def desktop_m4_drm_qemu_argv(**arguments: Any) -> tuple[str, ...]:
    """Return the GL virtio-gpu desktop contract without a firmware fb0."""

    base = list(qemu_argv(**arguments, graphical=False))
    display_index = base.index("-display")
    rendernode = os.environ.get("ASTERINAS_QEMU_RENDER_NODE", "/dev/dri/renderD129")
    base[display_index + 1] = f"egl-headless,rendernode={rendernode}"
    boot_device_index = base.index("virtio-blk-device,drive=bootdisk")
    # Insert before the existing ``-device`` flag, not between that flag and
    # its value.  Inserting at ``boot_device_index`` emitted ``-device
    # -device virtio-gpu-gl-device`` and made the full desktop gate fail in
    # QEMU argument parsing before the guest could provide any evidence.
    device_flag_index = boot_device_index - 1
    if base[device_flag_index] != "-device":
        raise ValueError("boot disk device contract is malformed")
    base[device_flag_index:device_flag_index] = [
        "-device",
        "virtio-gpu-gl-device",
        "-device",
        "virtio-keyboard-device",
        "-device",
        "virtio-tablet-device",
    ]
    return tuple(base)


def classify_desktop_m4(
    transcript: bytes, *, expected_debian_release: str, display_provider: str = "fbdev"
) -> GateResult:
    """Require ordered application evidence across the fully drained transcript."""

    if display_provider not in {"fbdev", "drm"}:
        return GateResult(False, "unknown display provider", None)

    if display_provider == "drm":
        milestones = (
            DESKTOP_M4_DRM_CORE_MILESTONES
            if b"DEBIAN_DESKTOP_M4_CORE_CLIENTS" in transcript
            else DESKTOP_M4_DRM_MILESTONES
        )
    else:
        milestones = DESKTOP_M4_MILESTONES
    result = classify_desktop(
        transcript,
        expected_debian_release=expected_debian_release,
        milestones=milestones,
        failure_marker=b"DEBIAN_DESKTOP_M4_FAIL reason=",
    )
    if not result.passed:
        return result
    if display_provider == "drm":
        return result
    handoff = validate_framebuffer_handoff(transcript)
    if not handoff.passed:
        return handoff
    if not DESKTOP_M4_QEMU_FRAMEBUFFER_PATTERN.search(transcript):
        return GateResult(False, "missing guest framebuffer evidence", None)
    return result


class DesktopM4Operations(DesktopM3Operations):
    """Desktop M3 mechanics bound to the immutable M4 application profile."""

    SCHEMA_VERSION = 4
    PROFILE_NAME = "desktop-m4"
    ARTIFACT_PREFIX = "desktop-m4"
    MILESTONES = DESKTOP_M4_MILESTONES
    FAILURE_MARKER = b"DEBIAN_DESKTOP_M4_FAIL reason="
    # Keep a host-side QEMU screendump in this gate.  The guest fbdev sample
    # proves that reads see more than one value, while the HMP image proves
    # those pixels reached the emulated scanout rather than only a guest
    # mapping.
    CAPTURE_SCREENSHOT = True
    BOOTARGS = DESKTOP_M4_BOOTARGS

    @classmethod
    def _accepted_profile_identities(cls) -> tuple[tuple[int, str], ...]:
        # The Firefox gate deliberately uses the larger immutable profile.
        # Keep the ordinary M4 image valid for the display-only gate while
        # allowing the same lifecycle to validate the official Firefox image.
        return (
            (cls.SCHEMA_VERSION, "desktop-m4"),
            (cls.SCHEMA_VERSION, "desktop-m4-firefox"),
        )

    def _validate_protocol_transcript(self, session: dict[str, Any]) -> None:
        phase_result = validate_boot_phase_contract(session["serial"].transcript)
        if not phase_result.passed:
            raise GateFailure(phase_result.reason)

    @staticmethod
    def _qemu_argv(**arguments: Any) -> tuple[str, ...]:
        return desktop_m4_qemu_argv(**arguments)


def classify_desktop_m4_drm(
    transcript: bytes,
    *,
    expected_debian_release: str,
    require_x11_screenshot: bool = True,
) -> GateResult:
    """Classify the provider handoff before the separate pixel gate."""

    result = classify_desktop_m4(
        transcript,
        expected_debian_release=expected_debian_release,
        display_provider="drm",
    )
    if not result.passed:
        return result
    if (
        require_x11_screenshot
        and DESKTOP_M4_DRM_X11_SCREENSHOT_PATTERN.search(transcript) is None
    ):
        return GateResult(False, "missing DRM X11 pixel hash", None)
    egl_match = DESKTOP_M4_DRM_EGL_PIXEL_PATTERN.search(transcript)
    if egl_match is None:
        return GateResult(False, "missing DRM EGL virgl pixel probe", None)
    egl_line = transcript[egl_match.start() : transcript.find(b"\n", egl_match.start())]
    if any(marker in egl_line.lower() for marker in DRM_SOFTWARE_RENDERER_MARKERS):
        return GateResult(False, "DRM EGL pixel probe selected a software renderer", None)
    return result


class DesktopM4DrmOperations(DesktopM4Operations):
    """Run the M4 lifecycle with virtio-gpu GL and no simple-framebuffer."""

    PROFILE_NAME = "desktop-m4"
    ARTIFACT_PREFIX = "desktop-m4-drm"
    DISPLAY_PROVIDER = "drm"
    MILESTONES = DESKTOP_M4_DRM_MILESTONES
    BOOTARGS = DESKTOP_M4_DRM_BOOTARGS
    # egl-headless intentionally exposes no QEMU UI surface, so HMP
    # ``screendump`` is not a valid observation for this provider. The guest
    # evidence script captures the Xorg root window with bounded XGetImage
    # tiles and records the pixel-area hash instead; the classifier above
    # requires that marker.
    CAPTURE_SCREENSHOT = False
    FIREFOX_BOOTARGS = DESKTOP_M4_DRM_BOOTARGS.replace(
        "asterinas.reboot_after=300", "asterinas.reboot_after=1200"
    )
    FIREFOX_PROBE_BOOTARGS = FIREFOX_BOOTARGS.replace(
        "loglevel=4",
        "loglevel=4",
    ).replace(
        "asterinas.reboot_after=1200",
        "asterinas.reboot_after=600 asterinas.syscall_diag=1 "
        "asterinas.futex_profile=1 asterinas.syscall_profile=1 "
        "asterinas.futex_detail_min_pid=100 asterinas.epoll_profile=1 "
        "asterinas.epoll_detail_min_pid=300 "
        "asterinas.ppoll_profile=1 asterinas.ppoll_detail_min_pid=100 "
        "asterinas.mmap_profile=1 asterinas.mmap_detail_min_pid=300 "
        "asterinas.tcp_diagnostic_port=2828 "
        "systemd.setenv=ASTERINAS_FIREFOX_PROBE_ONLY=1 "
        "systemd.setenv=ASTERINAS_FIREFOX_NEWSESSION_CAPS=minimal",
    )
    # The desktop service normally starts NetSurf in its X session.  A
    # Marionette capability probe needs only the Xorg/DRM owner; suppressing
    # the unrelated browser removes a competing cold-start workload.
    FIREFOX_PROBE_BOOTARGS = FIREFOX_PROBE_BOOTARGS.replace(
        "systemd.setenv=ASTERINAS_FIREFOX_PROBE_ONLY=1",
        "systemd.setenv=ASTERINAS_FIREFOX_PROBE_ONLY=1 "
        "systemd.setenv=ASTERINAS_DESKTOP_BROWSER_ENABLED=0",
        1,
    )
    FIREFOX_HEADLESS_PROBE_BOOTARGS = FIREFOX_PROBE_BOOTARGS.replace(
        "systemd.setenv=ASTERINAS_FIREFOX_PROBE_ONLY=1",
        "systemd.setenv=ASTERINAS_FIREFOX_PROBE_ONLY=1 "
        "systemd.setenv=ASTERINAS_FIREFOX_PROBE_HEADLESS=1",
    )

    def __init__(self, config: GateConfig) -> None:
        super().__init__(config)
        self._firefox_profile = False
        self._firefox_probe_only = False
        self._bootargs = self.BOOTARGS

    def _select_firefox_probe_capability_arm(self) -> None:
        """Bind the host-selected A/B arm into the guest environment."""

        mode = os.environ.get("ASTERINAS_FIREFOX_NEWSESSION_CAPS", "minimal").strip().lower()
        if mode not in {"minimal", "legacy", "envelope"}:
            raise GateFailure(f"firefox-newsession-capability-arm: unsupported value {mode!r}")
        self._bootargs = self._bootargs.replace(
            "systemd.setenv=ASTERINAS_FIREFOX_NEWSESSION_CAPS=minimal",
            f"systemd.setenv=ASTERINAS_FIREFOX_NEWSESSION_CAPS={mode}",
        )

    def _select_firefox_profile_dir(self) -> None:
        """Optionally keep probe profile writes on the guest runtime tmpfs."""

        raw = os.environ.get("ASTERINAS_FIREFOX_PROFILE_DIR", "").strip()
        if not raw:
            return
        if not raw.startswith("/") or not re.fullmatch(r"[A-Za-z0-9_./-]+", raw):
            raise GateFailure("firefox-profile-dir: expected an absolute safe path")
        marker = "systemd.setenv=ASTERINAS_FIREFOX_PROBE_ONLY=1"
        replacement = f"{marker} systemd.setenv=ASTERINAS_FIREFOX_PROFILE_DIR={raw}"
        self._bootargs = self._bootargs.replace(marker, replacement)

    def _select_dri_trace(self) -> None:
        """Append the opt-in DRM ioctl trace for a diagnostic probe.

        The probe command line carries a ``--`` separator before the init
        arguments, and a kernel parameter appended after it would be handed to
        init instead of the kernel.
        """

        if os.environ.get("ASTERINAS_DRI_TRACE") != "1":
            return
        separator = " -- "
        index = self._bootargs.find(separator)
        if index >= 0:
            self._bootargs = (
                f"{self._bootargs[:index]} asterinas.dri_trace=1"
                f"{self._bootargs[index:]}"
            )
        else:
            self._bootargs = f"{self._bootargs} asterinas.dri_trace=1"

    def _select_dri_trace_fail(self) -> None:
        """Append the opt-in refusal-only DRM trace for a diagnostic probe.

        The full trace is far too large for a WebRender scene, but the single
        refused ioctl is what explains an ``OutOfMemory`` renderer.
        """

        if os.environ.get("ASTERINAS_DRI_TRACE_FAIL") != "1":
            return
        separator = " -- "
        index = self._bootargs.find(separator)
        if index >= 0:
            self._bootargs = (
                f"{self._bootargs[:index]} asterinas.dri_trace_fail=1"
                f"{self._bootargs[index:]}"
            )
        else:
            self._bootargs = f"{self._bootargs} asterinas.dri_trace_fail=1"

    def _select_dri_pool_mb(self) -> None:
        """Pass a diagnostic GEM pool size to the kernel.

        Like the trace switch, the parameter belongs before the ``--``
        separator; after it the kernel never sees it.
        """

        raw = os.environ.get("ASTERINAS_DRI_POOL_MB", "").strip()
        if not raw:
            return
        try:
            value = int(raw)
        except ValueError as error:
            raise GateFailure("dri-pool-mb: expected an integer") from error
        if not 16 <= value <= 1024:
            raise GateFailure("dri-pool-mb: expected 16..1024")
        parameter = f" asterinas.dri_pool_mb={value}"
        separator = " -- "
        index = self._bootargs.find(separator)
        if index >= 0:
            self._bootargs = (
                f"{self._bootargs[:index]}{parameter}{self._bootargs[index:]}"
            )
        else:
            self._bootargs = f"{self._bootargs}{parameter}"

    def _select_firefox_probe_timeout(self) -> None:
        """Pass the bounded host-selected probe budget into the guest."""

        value = firefox_probe_guest_timeout_seconds()
        marker = "systemd.setenv=ASTERINAS_FIREFOX_PROBE_ONLY=1"
        replacement = f"{marker} systemd.setenv=ASTERINAS_FIREFOX_PROBE_TIMEOUT={value:g}"
        self._bootargs = self._bootargs.replace(marker, replacement, 1)
        # The boot watchdog must outlive the guest probe receipt.  The base
        # probe bootargs hard-code 600 seconds for the default 300-second
        # budget; a longer diagnostic budget needs the same margin.
        reboot_after = max(600, int(value) + 300)
        self._bootargs = self._bootargs.replace(
            "asterinas.reboot_after=600",
            f"asterinas.reboot_after={reboot_after}",
            1,
        )

    def validate_inputs(
        self, config: GateConfig, snapshots: Mapping[str, str]
    ) -> Mapping[str, object]:
        identity = super().validate_inputs(config, snapshots)
        self._firefox_profile = (
            identity.get("profile") == "desktop-m4-firefox"
            and os.environ.get("ASTERINAS_DESKTOP_SKIP_FIREFOX") != "1"
        )
        if self._firefox_profile:
            self._firefox_probe_only = os.environ.get("ASTERINAS_FIREFOX_PROBE_ONLY") == "1"
            headless_probe = os.environ.get("ASTERINAS_FIREFOX_HEADLESS_PROBE") == "1"
            if self._firefox_probe_only:
                validate_firefox_probe_boot_timeout(config.boot_timeout)
            self._bootargs = (
                self.FIREFOX_HEADLESS_PROBE_BOOTARGS
                if self._firefox_probe_only and headless_probe
                else self.FIREFOX_PROBE_BOOTARGS
                if self._firefox_probe_only
                else self.FIREFOX_BOOTARGS
            )
            if self._firefox_probe_only:
                self._select_firefox_probe_capability_arm()
                self._select_firefox_probe_timeout()
                if os.environ.get("ASTERINAS_FIREFOX_REMOTE_TRACE") == "1":
                    self._bootargs = self._bootargs.replace(
                        "systemd.setenv=ASTERINAS_FIREFOX_PROBE_ONLY=1",
                        "systemd.setenv=ASTERINAS_FIREFOX_PROBE_ONLY=1 "
                        "systemd.setenv=ASTERINAS_FIREFOX_REMOTE_TRACE=1",
                    )
                self._select_firefox_profile_dir()
                self._select_dri_trace()
            self._select_dri_trace_fail()
            self._select_dri_pool_mb()
        return identity

    def run_protocol(self, session: dict[str, Any], config: GateConfig) -> None:
        super().run_protocol(session, config)
        if self._firefox_profile:
            # The Firefox scene service owns its own bounded Marionette and
            # recovery phases. Do not let the desktop READY marker terminate
            # QEMU while that service is still in connect-state.
            if self._firefox_probe_only:
                completion = session["serial"].wait_for_any(
                    (
                        b"FIREFOX_DRM_PROBE_DONE status=pass",
                        b"FIREFOX_DRM_PROBE_DONE status=fail",
                        b"FIREFOX_DRM_FAIL_REASON",
                    ),
                    time.monotonic() + config.boot_timeout,
                )
                if completion.startswith(b"FIREFOX_DRM_PROBE_DONE status=fail"):
                    raise GateFailure("Firefox NewSession probe failed")
                if completion.startswith(b"FIREFOX_DRM_FAIL_REASON"):
                    raise GateFailure("Firefox NewSession probe crashed")
            else:
                session["serial"].wait_for_any(
                    (b"FIREFOX_DRM_RECOVERY restart=pass", b"FIREFOX_DRM_FAIL_REASON"),
                    time.monotonic() + config.boot_timeout,
                )

    @staticmethod
    def _validate_qemu_argv(argv: tuple[str, ...]) -> None:
        """Reject a DRM launch unless the selected QEMU really supports virgl."""

        if not argv:
            raise GateFailure("virgl host preflight failed: empty QEMU command")
        result = inspect_qemu(argv[0])
        if not result.passed:
            raise GateFailure(f"virgl host preflight failed: {result.reason}")
        smoke = run_runtime_smoke(argv[0])
        if not smoke.passed:
            raise GateFailure(f"virgl host runtime smoke failed: {smoke.reason}")

    @classmethod
    def _accepted_profile_identities(cls) -> tuple[tuple[int, str], ...]:
        return (
            (cls.SCHEMA_VERSION, "desktop-m4"),
            (cls.SCHEMA_VERSION, "desktop-m4-firefox"),
        )

    @staticmethod
    def _qemu_argv(**arguments: Any) -> tuple[str, ...]:
        return desktop_m4_drm_qemu_argv(**arguments)

    def _boot_commands(self, framebuffer_address: int) -> tuple[str, ...]:
        del framebuffer_address
        return (
            "virtio scan",
            "ext4load virtio 0:0 0x80200000 /asterinas.booti",
            "ext4load virtio 0:0 0x90000000 /qemu-virt.dtb",
            "fdt addr 0x90000000",
            "fdt resize 0x1000",
            "ext4load virtio 0:0 0x83000000 /stage1-initramfs.cpio",
            "setenv initrd_size ${filesize}",
            *_bounded_uboot_bootargs_commands(getattr(self, "_bootargs", self.BOOTARGS)),
        )


def orchestrate_desktop_m4_gate(
    config: GateConfig, operations: DesktopM4Operations
) -> dict[str, object]:
    """Reuse the bounded graphical lifecycle with the M4 classifier."""

    return orchestrate_systemd_m2_gate(
        config,
        operations,
        classifier=classify_desktop_m4,
    )


def main(arguments: list[str] | None = None) -> int:
    try:
        config = parse_gate_args(arguments)
        _safe_output(config.output_directory)
        with TerminationSignalState(), DesktopM4Operations(config) as operations:
            result = orchestrate_desktop_m4_gate(config, operations)
        return 0 if result["passed"] else 1
    except SystemExit as error:
        return int(error.code or 0)
    except GateTermination as error:
        print(
            f"debian-desktop-m4-gate: terminated by signal {error.signum}",
            file=sys.stderr,
        )
        return 128 + error.signum
    except BaseException as error:
        reason = error.reason if isinstance(error, GateFailure) else str(error)
        print(f"debian-desktop-m4-gate: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
