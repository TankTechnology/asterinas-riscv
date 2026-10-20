# SPDX-License-Identifier: MPL-2.0

"""Fail-closed contracts for one read-only Megrez hardware probe.

The kernel emits the markers checked here.  Keeping the device registry and
the serial evidence contract in one small host module makes it possible to
review a probe without trusting a broad driver log.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from tools.riscv.eic7700_platform_contract import EIC7700


class ProbeContractError(ValueError):
    """The serial transcript does not prove a safe, recovered probe."""


@dataclass(frozen=True)
class ProbeSpec:
    device: str
    base: int
    size: int
    irq: int
    whitelist: tuple[int, ...]
    prerequisites: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProbeAccessAudit:
    reads: int
    writes: int
    gmac_reads: int
    gmac_writes: int
    mdio: int
    dma: int
    tx: int
    rx: int
    irq: int


# These are deliberately fixed values from the EIC7700 device tree and the
# companion QTest model.  Keep prerequisites here because they describe the
# probe sequence; all MMIO identity facts come from the shared registry.
_PROBE_PREREQUISITES = {
    "dwc3-0": (),
    "dwc3-1": (),
    "gmac-0": ("dwc3-0", "dwc3-1"),
    "gmac-1": ("dwc3-0", "dwc3-1"),
}
PROBE_SPECS = {
    name: ProbeSpec(
        name,
        EIC7700.peripheral(name).base,
        EIC7700.peripheral(name).size,
        EIC7700.peripheral(name).irq,
        EIC7700.peripheral(name).probe_offsets,
        _PROBE_PREREQUISITES[name],
    )
    for name in ("dwc3-0", "dwc3-1", "gmac-0", "gmac-1")
}


_DEVICE = r"(?:dwc3-[01]|gmac-[01])"
_FORBIDDEN = re.compile(
    r"(?:\b(?:mw\.[blw]|md|mm|nm|fdt\s+(?:set|resize|rm))\b|"
    r"starting the controller|xhci|usb device(?:s)? found|"
    r"(?:dma|mdio|phy|reset|clock).*(?:write|enable|start)|"
    r"(?:write|enable|start).*(?:dma|mdio|phy|reset|clock))",
    re.IGNORECASE,
)
_BEGIN = re.compile(r"^ASTERINAS_PROBE_BEGIN device=(\S+)$", re.MULTILINE)
_READ = re.compile(
    r"^ASTERINAS_PROBE_READ device=(\S+) offset=0x([0-9a-fA-F]+) value=0x([0-9a-fA-F]+)$",
    re.MULTILINE,
)
_ATTEMPT = re.compile(
    r"^ASTERINAS_PROBE_ATTEMPT device=(\S+) offset=0x([0-9a-fA-F]+)$",
    re.MULTILINE,
)
_PHASE = re.compile(
    r"^ASTERINAS_PROBE_PHASE device=(\S+) phase=(mmio-map|platform-init|mmio-read)$",
    re.MULTILINE,
)
_FAIL = re.compile(
    r"^ASTERINAS_PROBE_FAIL device=(\S+) reason=([a-z0-9][a-z0-9_-]*)$",
    re.MULTILINE,
)
_PASS = re.compile(r"^ASTERINAS_PROBE_PASS device=(\S+)$", re.MULTILINE)
_ACCESS = re.compile(
    r"^ASTERINAS_PROBE_ACCESS device=(\S+) reads=(\d+) writes=(\d+) "
    r"gmac_reads=(\d+) gmac_writes=(\d+) mdio=(\d+) dma=(\d+) "
    r"tx=(\d+) rx=(\d+) irq=(\d+)$",
    re.MULTILINE,
)
_DEVICE_MARKER = re.compile(
    r"^ASTERINAS_PROBE_(?:RESOURCE|DT|PHASE|ATTEMPT|READ|ACCESS|FAIL|PASS|DONE) device=(\S+)",
    re.MULTILINE,
)


def _validate_register_value(device: str, offset: int, value: int) -> None:
    if value > 0xFFFFFFFF or value == 0xFFFFFFFF:
        raise ProbeContractError(f"register value at 0x{offset:x} is unavailable")
    if device.startswith("gmac-") and offset == 0x0110 and value & 0xFF != 0x52:
        raise ProbeContractError(f"register value at 0x{offset:x} is not a DWMAC 5.20 identity")
    if device.startswith("dwc3-") and offset == 0xC120:
        if value & 0xFFFFFFF0 != 0x55333300 or value & 0xF not in (0xA, 0xB):
            raise ProbeContractError(f"register value at 0x{offset:x} is not a DWC3 identity")


@dataclass(frozen=True)
class ProbeResult:
    device: str
    status: str
    recovered: bool
    read_offsets: tuple[int, ...]
    attempt_offsets: tuple[int, ...] = ()
    access_audit: ProbeAccessAudit | None = None


def probe_menu_args(device: str) -> str:
    """Return the bounded Stage1 command line for exactly one probe."""

    try:
        PROBE_SPECS[device]
    except KeyError as error:
        raise ProbeContractError(f"unknown probe device: {device}") from error
    return (
        "console=ttyS0 loglevel=info asterinas.klog_capture=info "
        f"asterinas.reboot_after=60 asterinas.hardware_probe={device} "
        "init=/init -- --root-init=probe-auto"
    )


def validate_probe_evidence(
    text: str, device: str, run_id: str, *, require_access_audit: bool = False
) -> ProbeResult:
    """Validate one transcript before accepting it as a physical result."""

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if device not in PROBE_SPECS:
        raise ProbeContractError(f"unknown probe device: {device}")
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise ProbeContractError("invalid run ID")
    begins = _BEGIN.findall(text)
    if begins != [device]:
        if any(item != device for item in begins):
            raise ProbeContractError("unexpected device marker")
        raise ProbeContractError("probe must have exactly one begin marker")
    if any(marker_device != device for marker_device in _DEVICE_MARKER.findall(text)):
        raise ProbeContractError("unexpected device marker")
    begin_at = text.index(f"ASTERINAS_PROBE_BEGIN device={device}")
    done_matches = list(
        re.finditer(
            rf"^ASTERINAS_PROBE_DONE device={re.escape(device)} status=(pass|fail)$",
            text[begin_at:],
            re.MULTILINE,
        )
    )
    if len(done_matches) != 1:
        raise ProbeContractError("probe must have exactly one completion marker")
    done_at = begin_at + done_matches[0].start()
    done_end = begin_at + done_matches[0].end()
    for marker in _DEVICE_MARKER.finditer(text):
        if marker.start() < begin_at or marker.start() >= done_end:
            raise ProbeContractError("probe marker is outside probe interval")
    reboot_at = text.find("ASTERINAS_PROBE_AUTO_REBOOT", done_end)
    probe_text = text[begin_at:done_end]
    # U-Boot itself may print controller-start and USB enumeration messages
    # before the candidate entry.  Only the bounded Asterinas interval is
    # evidence for side effects in the diagnostic implementation.
    # The audit marker contains words such as ``writes`` and ``dma`` by
    # design.  Remove that structured line before applying the free-form
    # side-effect filter; its numeric fields are checked below instead.
    if _FORBIDDEN.search(_ACCESS.sub("", probe_text)):
        raise ProbeContractError("forbidden side effect in probe transcript")
    spec = PROBE_SPECS[device]
    resource_matches = list(re.finditer(
        rf"^ASTERINAS_PROBE_RESOURCE device={re.escape(device)} "
        rf"base=0x([0-9a-fA-F]+) size=0x([0-9a-fA-F]+) irq=([0-9]+)$",
        text,
        re.MULTILINE,
    ))
    if any(match.start() < begin_at or match.start() >= done_at for match in resource_matches):
        raise ProbeContractError("resource marker is outside probe interval")
    resource = [match.groups() for match in resource_matches]
    if resource != [(f"{spec.base:x}", f"{spec.size:x}", str(spec.irq))]:
        raise ProbeContractError("resource marker does not match fixed device contract")
    dt_matches = list(
        re.finditer(
            rf"^ASTERINAS_PROBE_DT device={re.escape(device)} clocks=\[[^\n]+\] resets=\[[^\n]+\]$",
            text,
            re.MULTILINE,
        )
    )
    if device.startswith("gmac-"):
        if len(dt_matches) != 1:
            raise ProbeContractError("GMAC device-tree clock/reset marker is missing")
        if dt_matches[0].start() < begin_at or dt_matches[0].start() >= done_at:
            raise ProbeContractError("device-tree marker is outside probe interval")

    all_reads = list(_READ.finditer(text))
    if any(match.start() < begin_at or match.start() >= done_at for match in all_reads):
        raise ProbeContractError("register read is outside probe interval")
    reads = [match.groups() for match in all_reads]
    if any(read_device != device for read_device, _, _ in reads):
        raise ProbeContractError("unexpected device marker")
    offsets = tuple(int(offset, 16) for _, offset, _ in reads)
    attempts = tuple(
        int(match.group(2), 16)
        for match in _ATTEMPT.finditer(text)
        if begin_at <= match.start() < done_at
    )
    if any(
        match.start() < begin_at or match.start() >= done_at
        for match in _ATTEMPT.finditer(text)
    ):
        raise ProbeContractError("probe attempt marker is outside probe interval")
    if any(match.group(1) != device for match in _ATTEMPT.finditer(text)):
        raise ProbeContractError("unexpected device marker")
    if attempts and attempts != offsets:
        raise ProbeContractError("probe attempts do not match register reads")
    failure_matches = list(_FAIL.finditer(text))
    if any(match.start() < begin_at or match.start() >= done_at for match in failure_matches):
        raise ProbeContractError("failure marker is outside probe interval")
    failure = [match.groups() for match in failure_matches]
    if failure and any(read_device != device for read_device, _ in failure):
        raise ProbeContractError("unexpected device marker")
    if len(failure) > 1:
        raise ProbeContractError("probe must have at most one failure marker")

    pass_matches = list(_PASS.finditer(text))
    if any(match.start() < begin_at or match.start() >= done_at for match in pass_matches):
        raise ProbeContractError("pass marker is outside probe interval")
    if any(match.group(1) != device for match in pass_matches):
        raise ProbeContractError("unexpected device marker")

    access_matches = list(_ACCESS.finditer(text))
    if any(match.start() < begin_at or match.start() >= done_end for match in access_matches):
        raise ProbeContractError("access audit is outside probe interval")
    if len(access_matches) > 1:
        raise ProbeContractError("probe must have at most one access audit")
    access_audit = None
    if access_matches:
        access_match = access_matches[0]
        if access_match.group(1) != device:
            raise ProbeContractError("unexpected device marker")
        values = tuple(int(value) for value in access_match.groups()[1:])
        access_audit = ProbeAccessAudit(*values)
        if access_audit.gmac_reads != len(reads):
            raise ProbeContractError("access audit does not match register reads")
        if any(
            value
            for value in (
                access_audit.gmac_writes,
                access_audit.mdio,
                access_audit.dma,
                access_audit.tx,
                access_audit.rx,
                access_audit.irq,
            )
        ):
            raise ProbeContractError("probe access audit reports a datapath side effect")
    elif require_access_audit:
        raise ProbeContractError("probe access audit is missing")

    for index, (read_device, offset, raw_value) in enumerate(reads):
        value = int(raw_value, 16)
        # The kernel logs the read before validating it. Preserve the final
        # rejected value as failure evidence, never as a successful snapshot.
        try:
            _validate_register_value(read_device, int(offset, 16), value)
        except ProbeContractError:
            if not (
                failure == [(device, "readback-mismatch")]
                and index == len(reads) - 1
                and value <= 0xFFFFFFFF
            ):
                raise

    status_pass = len(pass_matches)
    status_done_pass = text.count(f"ASTERINAS_PROBE_DONE device={device} status=pass")
    status_done_fail = text.count(f"ASTERINAS_PROBE_DONE device={device} status=fail")
    if failure:
        if status_pass or status_done_pass:
            raise ProbeContractError("failed probe must not report pass")
        if status_done_fail != 1:
            raise ProbeContractError("failed probe completion marker is missing")
        if offsets != spec.whitelist[: len(offsets)]:
            raise ProbeContractError("failed probe reads are not a whitelist prefix")
        result_status = "fail"
    else:
        if status_done_fail:
            raise ProbeContractError("failed probe failure reason is missing")
        if offsets != spec.whitelist:
            missing = next(
                (offset for offset in spec.whitelist if offset not in offsets), None
            )
            if missing is not None:
                raise ProbeContractError(f"missing register read 0x{missing:x}")
            raise ProbeContractError("register reads are not the fixed whitelist")
        if status_pass != 1 or status_done_pass != 1:
            raise ProbeContractError("probe did not report pass")
        if status_done_fail:
            raise ProbeContractError("probe has an unexpected failure completion")
        result_status = "pass"

    if reboot_at < 0:
        raise ProbeContractError("recovery epoch is missing")
    opensbi_at = text.find("OpenSBI", reboot_at)
    uboot_at = text.find("U-Boot ", opensbi_at + 1) if opensbi_at >= 0 else -1
    if opensbi_at < 0 or uboot_at < 0:
        raise ProbeContractError("recovery epoch is missing")
    return ProbeResult(device, result_status, True, offsets, attempts, access_audit)


def validate_probe_timeout_evidence(
    text: str, device: str, run_id: str
) -> ProbeResult:
    """Validate a probe that stopped before completion and was recovered.

    A stalled MMIO read cannot emit ``FAIL`` or ``DONE`` from the guest.  The
    host therefore accepts only the bounded prefix (resource/DT and optional
    whitelist reads) followed by a fresh firmware epoch.  The result is
    always ``timeout`` and can never satisfy a successful-device prerequisite.
    """

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if device not in PROBE_SPECS:
        raise ProbeContractError(f"unknown probe device: {device}")
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise ProbeContractError("invalid run ID")
    begins = _BEGIN.findall(text)
    if begins != [device]:
        if any(item != device for item in begins):
            raise ProbeContractError("unexpected device marker")
        raise ProbeContractError("probe must have exactly one begin marker")
    begin_at = text.index(f"ASTERINAS_PROBE_BEGIN device={device}")
    opensbi_at = text.find("OpenSBI", begin_at)
    uboot_at = text.find("U-Boot ", opensbi_at + 1) if opensbi_at >= 0 else -1
    if opensbi_at < 0 or uboot_at < 0:
        raise ProbeContractError("recovery epoch is missing")
    for marker in _DEVICE_MARKER.finditer(text):
        if marker.group(1) != device:
            raise ProbeContractError("unexpected device marker")
        if marker.start() < begin_at or marker.start() >= opensbi_at:
            raise ProbeContractError("probe marker is outside probe interval")
    if re.search(
        rf"^ASTERINAS_PROBE_(?:DONE|PASS|FAIL) device={re.escape(device)}",
        text[begin_at:opensbi_at],
        re.MULTILINE,
    ):
        raise ProbeContractError("timeout evidence contains a completion marker")
    if _FORBIDDEN.search(text[begin_at:opensbi_at]):
        raise ProbeContractError("forbidden side effect in probe transcript")
    spec = PROBE_SPECS[device]
    resource_matches = list(
        re.finditer(
            rf"^ASTERINAS_PROBE_RESOURCE device={re.escape(device)} "
            rf"base=0x([0-9a-fA-F]+) size=0x([0-9a-fA-F]+) irq=([0-9]+)$",
            text,
            re.MULTILINE,
        )
    )
    if resource_matches != [] and any(
        match.start() < begin_at or match.start() >= opensbi_at
        for match in resource_matches
    ):
        raise ProbeContractError("resource marker is outside probe interval")
    resource = [match.groups() for match in resource_matches]
    if resource != [(f"{spec.base:x}", f"{spec.size:x}", str(spec.irq))]:
        raise ProbeContractError("resource marker does not match fixed device contract")
    dt_matches = list(
        re.finditer(
            rf"^ASTERINAS_PROBE_DT device={re.escape(device)} clocks=\[[^\n]+\] resets=\[[^\n]+\]$",
            text,
            re.MULTILINE,
        )
    )
    if device.startswith("gmac-"):
        if len(dt_matches) != 1:
            raise ProbeContractError("GMAC device-tree clock/reset marker is missing")
        if dt_matches[0].start() < begin_at or dt_matches[0].start() >= opensbi_at:
            raise ProbeContractError("device-tree marker is outside probe interval")
    all_reads = list(_READ.finditer(text))
    if any(match.start() < begin_at or match.start() >= opensbi_at for match in all_reads):
        raise ProbeContractError("register read is outside probe interval")
    reads = [match.groups() for match in all_reads]
    if any(read_device != device for read_device, _, _ in reads):
        raise ProbeContractError("unexpected device marker")
    offsets = tuple(int(offset, 16) for _, offset, _ in reads)
    if offsets != spec.whitelist[: len(offsets)]:
        raise ProbeContractError("timeout reads are not a whitelist prefix")
    attempt_matches = list(_ATTEMPT.finditer(text))
    attempts = tuple(int(match.group(2), 16) for match in attempt_matches)
    if any(
        match.start() < begin_at or match.start() >= opensbi_at
        for match in _ATTEMPT.finditer(text)
    ):
        raise ProbeContractError("probe attempt marker is outside probe interval")
    if any(match.group(1) != device for match in _ATTEMPT.finditer(text)):
        raise ProbeContractError("unexpected device marker")
    if attempts[: len(offsets)] != offsets:
        raise ProbeContractError("timeout attempts do not match register reads")
    if len(attempts) > len(offsets) + 1:
        raise ProbeContractError("timeout has multiple pending register attempts")
    if attempts != spec.whitelist[: len(attempts)]:
        raise ProbeContractError("timeout attempts are not a whitelist prefix")
    # A blocked load cannot emit another attempt. Require each completed read
    # to follow its attempt before the next register can be touched.
    for index, read in enumerate(all_reads):
        if attempt_matches[index].start() >= read.start():
            raise ProbeContractError("timeout attempt/read order is invalid")
        if index + 1 < len(attempt_matches):
            if read.start() >= attempt_matches[index + 1].start():
                raise ProbeContractError("timeout attempt/read order is invalid")
    return ProbeResult(device, "timeout", True, offsets, attempts)


def classify_probe_timeout(text: str, device: str, run_id: str) -> str:
    """Classify the last bounded phase reached before a watchdog timeout."""

    result = validate_probe_timeout_evidence(text, device, run_id)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    phases = [
        match.group(2)
        for match in _PHASE.finditer(normalized)
        if match.group(1) == device
    ]
    if not result.attempt_offsets:
        if result.read_offsets:
            return f"unknown-after-read:0x{result.read_offsets[-1]:04x}"
        if phases and phases[-1] == "mmio-map":
            return "mmio-map"
        if phases and phases[-1] == "mmio-read":
            return "mmio-read-before-attempt"
        return "unknown-before-first-read"
    if len(result.attempt_offsets) > len(result.read_offsets):
        offset = result.attempt_offsets[len(result.read_offsets)]
        return f"mmio-read:0x{offset:04x}"
    offset = result.read_offsets[-1] if result.read_offsets else result.attempt_offsets[-1]
    return f"unknown-after-read:0x{offset:04x}"


def validate_prerequisites(device: str, completed: set[str]) -> None:
    """Enforce the low-risk order: both USB probes precede either GMAC probe."""

    try:
        spec = PROBE_SPECS[device]
    except KeyError as error:
        raise ProbeContractError(f"unknown probe device: {device}") from error
    missing = [name for name in spec.prerequisites if name not in completed]
    if missing:
        raise ProbeContractError(f"missing prerequisite probes: {', '.join(missing)}")
