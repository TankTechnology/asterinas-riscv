#!/usr/bin/env python3
"""Generate, sanitize, and audit profile-matched QEMU RISC-V DTBs."""

from __future__ import annotations

import argparse
import json
import re
import stat
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Sequence

from megrez_contract import ArtifactIdentity, artifact_identity, load_contract
from qemu_uboot_profiles import (
    GENERIC_SV39,
    DtbProvider,
    Fidelity,
    QemuUbootProfile,
    profile_by_name,
    require_profile_launch_allowed,
    validate_registered_profile,
    validate_profile_policy,
)
from qemu_uboot_secure_io import PinnedOutputDirectory, PinnedRegularInput
from qemu_uboot_variants import (
    QemuUbootVariant,
    validate_registered_variant,
    variant_by_name,
)


@dataclass(frozen=True)
class GeneratedDtbAudit:
    """Machine-contract facts read back from one generated DTB."""

    machine: str
    cpu_ids: tuple[int, ...]
    mmu_types: tuple[str | None, ...]
    memory: range
    ad_extension: str
    rng_seed_present: bool
    sha256: str


@dataclass(frozen=True)
class RealDtbFacts:
    """Boot-relevant structure read from the real Megrez DTB."""

    model: str
    compatible: tuple[str, ...]
    enabled_cpu_ids: tuple[int, ...]
    mmu_types: tuple[str, ...]
    dram: range
    stdout_path: str
    rng_seed_present: bool
    fixed_no_map: tuple[range, ...]
    cma_size: int
    cma_alignment: int
    cma_alloc_range: range


@dataclass(frozen=True)
class PayloadDtbVariantAudit:
    """Identity and semantic proof for one derived payload-only DTB."""

    variant: str
    uart_node: str
    source_compatible: tuple[str, ...]
    payload_compatible: tuple[str, ...]
    changed_properties: tuple[tuple[str, str], ...]
    source_size: int
    source_sha256: str
    source_crc32: str
    payload_size: int
    payload_sha256: str
    payload_crc32: str


@dataclass(frozen=True)
class _SemanticDtbSnapshot:
    """All DTB nodes and raw property bytes, including empty values."""

    nodes: frozenset[str]
    properties: Mapping[tuple[str, str], bytes]


class DtbInspectionError(RuntimeError):
    """The external DTB tooling could not complete an inspection."""


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CRC32_RE = re.compile(r"[0-9a-f]{8}")
_DTB_NAME_RE = re.compile(r"[A-Za-z0-9,._+*#?@-]+")
_RAW_BYTES_RE = re.compile(r"(?:[0-9a-f]{1,2}(?: [0-9a-f]{1,2})*)?\n")
_SOURCE_UART_COMPATIBLE = ("ns16550a",)
_PAYLOAD_UART_COMPATIBLE = ("snps,dw-apb-uart",)
_AUDIT_RECORD_KEYS = {
    "machine",
    "cpu_ids",
    "mmu_types",
    "memory_start",
    "memory_end_exclusive",
    "ad_extension",
    "rng_seed_present",
    "size",
    "sha256",
    "crc32",
}
_VARIANT_AUDIT_RECORD_KEYS = {
    "variant",
    "uart_node",
    "source_compatible",
    "payload_compatible",
    "changed_properties",
    "source_size",
    "source_sha256",
    "source_crc32",
    "payload_size",
    "payload_sha256",
    "payload_crc32",
}


def _run(command: list[str], *, timeout: float = 10.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _fdt_output(dtb: Path, options: list[str], node: str, *properties: str) -> str:
    result = _run(["fdtget", *options, str(dtb), node, *properties])
    return result.stdout.strip()


def _subnodes(dtb: Path, node: str) -> tuple[str, ...]:
    output = _fdt_output(dtb, ["-l"], node)
    return tuple(line.strip() for line in output.splitlines() if line.strip())


def _properties(dtb: Path, node: str) -> frozenset[str]:
    output = _fdt_output(dtb, ["-p"], node)
    return frozenset(line.strip() for line in output.splitlines() if line.strip())


def _strict_fdt_listing(
    dtb: Path,
    *,
    node: str,
    option: str,
    kind: str,
) -> tuple[str, ...]:
    output = _run(["fdtget", option, str(dtb), node]).stdout
    if output == "":
        return ()
    lines = output.splitlines(keepends=True)
    names: list[str] = []
    for line in lines:
        if not line.endswith("\n") or line.endswith("\r\n"):
            raise ValueError(f"malformed {kind} listing for DTB node {node}")
        name = line[:-1]
        if _DTB_NAME_RE.fullmatch(name) is None or name in {".", ".."} or "/" in name:
            raise ValueError(f"invalid {kind} name for DTB node {node}")
        names.append(name)
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate {kind} name for DTB node {node}")
    return tuple(names)


def _raw_property_bytes(dtb: Path, node: str, property_name: str) -> bytes:
    output = _run(["fdtget", "-t", "bx", str(dtb), node, property_name]).stdout
    if _RAW_BYTES_RE.fullmatch(output) is None:
        raise ValueError(f"malformed raw property output for {node}/{property_name}")
    tokens = output[:-1].split()
    return bytes(int(token, 16) for token in tokens)


def _semantic_dtb_snapshot(dtb: Path) -> _SemanticDtbSnapshot:
    """Read every node and property without normalizing its raw value."""

    pending = ["/"]
    nodes: set[str] = set()
    properties: dict[tuple[str, str], bytes] = {}
    while pending:
        node = pending.pop()
        if node in nodes:
            raise ValueError(f"duplicate DTB node path: {node}")
        nodes.add(node)

        property_names = _strict_fdt_listing(
            dtb,
            node=node,
            option="-p",
            kind="property",
        )
        for property_name in property_names:
            key = (node, property_name)
            if key in properties:
                raise ValueError(f"duplicate DTB property path: {key}")
            properties[key] = _raw_property_bytes(dtb, node, property_name)

        child_names = _strict_fdt_listing(
            dtb,
            node=node,
            option="-l",
            kind="subnode",
        )
        for child_name in reversed(child_names):
            child_path = f"/{child_name}" if node == "/" else f"{node}/{child_name}"
            pending.append(child_path)

    return _SemanticDtbSnapshot(
        nodes=frozenset(nodes),
        properties=MappingProxyType(properties),
    )


def _string_list_from_raw(value: bytes, *, label: str) -> tuple[str, ...]:
    if not value or not value.endswith(b"\0"):
        raise ValueError(f"{label} must be a non-empty NUL-terminated string list")
    encoded = value[:-1].split(b"\0")
    if any(not item for item in encoded):
        raise ValueError(f"{label} contains an empty string")
    try:
        return tuple(item.decode("ascii") for item in encoded)
    except UnicodeDecodeError as error:
        raise ValueError(f"{label} contains non-ASCII text") from error


def _single_string_property(
    snapshot: _SemanticDtbSnapshot,
    node: str,
    property_name: str,
) -> str:
    values = _string_list_property(snapshot, node, property_name)
    if len(values) != 1:
        raise ValueError(f"{node}/{property_name} must contain one string")
    return values[0]


def _string_list_property(
    snapshot: _SemanticDtbSnapshot,
    node: str,
    property_name: str,
) -> tuple[str, ...]:
    try:
        raw = snapshot.properties[(node, property_name)]
    except KeyError as error:
        raise ValueError(f"missing DTB property {node}/{property_name}") from error
    return _string_list_from_raw(raw, label=f"{node}/{property_name}")


def _require_absolute_node_path(
    snapshot: _SemanticDtbSnapshot,
    path: str,
    *,
    label: str,
) -> str:
    if (
        not path.startswith("/")
        or path == "/"
        or path.endswith("/")
        or "//" in path
        or any(component in {"", ".", ".."} for component in path.split("/")[1:])
        or path not in snapshot.nodes
    ):
        raise ValueError(f"{label} does not resolve to an existing absolute node")
    return path


def _resolve_payload_uart(snapshot: _SemanticDtbSnapshot) -> str:
    serial0 = _require_absolute_node_path(
        snapshot,
        _single_string_property(snapshot, "/aliases", "serial0"),
        label="/aliases/serial0",
    )
    stdout_path = _single_string_property(snapshot, "/chosen", "stdout-path")
    stdout_reference = stdout_path.partition(":")[0]
    if stdout_reference.startswith("/"):
        stdout_node = _require_absolute_node_path(
            snapshot,
            stdout_reference,
            label="/chosen/stdout-path",
        )
    else:
        stdout_node = _require_absolute_node_path(
            snapshot,
            _single_string_property(snapshot, "/aliases", stdout_reference),
            label="/chosen/stdout-path alias",
        )
    if stdout_node != serial0:
        raise ValueError("serial0 and stdout-path resolve to different DTB nodes")
    return serial0


def _string_property(
    dtb: Path,
    node: str,
    property_name: str,
    *,
    default: str | None = None,
) -> str:
    options = ["-t", "s"]
    if default is not None:
        options.extend(("-d", default))
    return _fdt_output(dtb, options, node, property_name)


def _integer_property(dtb: Path, node: str, property_name: str) -> int:
    output = _fdt_output(dtb, ["-t", "u"], node, property_name)
    tokens = output.split()
    if len(tokens) != 1:
        raise ValueError(f"{node}/{property_name} must contain one integer")
    try:
        return int(tokens[0], 10)
    except ValueError as error:
        raise ValueError(f"{node}/{property_name} is not an integer") from error


def _hex_cells(dtb: Path, node: str, property_name: str) -> tuple[int, ...]:
    output = _fdt_output(dtb, ["-t", "x"], node, property_name)
    try:
        cells = tuple(int(token, 16) for token in output.split())
    except ValueError as error:
        raise ValueError(f"{node}/{property_name} has a non-hex cell") from error
    if not cells or any(cell < 0 or cell > 0xFFFF_FFFF for cell in cells):
        raise ValueError(f"{node}/{property_name} has invalid cells")
    return cells


def _cells_to_integer(cells: tuple[int, ...]) -> int:
    value = 0
    for cell in cells:
        value = (value << 32) | cell
    return value


def _ranges_from_property(
    dtb: Path,
    node: str,
    property_name: str,
    *,
    address_cells: int,
    size_cells: int,
) -> tuple[range, ...]:
    stride = address_cells + size_cells
    cells = _hex_cells(dtb, node, property_name)
    if address_cells <= 0 or size_cells <= 0 or len(cells) % stride:
        raise ValueError(f"{node}/{property_name} has an invalid range encoding")
    ranges = []
    for offset in range(0, len(cells), stride):
        start = _cells_to_integer(cells[offset : offset + address_cells])
        size = _cells_to_integer(cells[offset + address_cells : offset + stride])
        if size == 0:
            raise ValueError(f"{node}/{property_name} contains an empty range")
        ranges.append(range(start, start + size))
    return tuple(ranges)


def _sized_integer_property(
    dtb: Path,
    node: str,
    property_name: str,
    *,
    size_cells: int,
) -> int:
    cells = _hex_cells(dtb, node, property_name)
    if size_cells <= 0 or len(cells) != size_cells:
        raise ValueError(f"{node}/{property_name} has the wrong cell count")
    return _cells_to_integer(cells)


def _contract_range(value: Mapping[str, object]) -> range:
    start = int(str(value["start"]), 16)
    end = int(str(value["end_exclusive"]), 16)
    return range(start, end)


def _profile_cpu_selectors(profile: QemuUbootProfile) -> dict[str, bool]:
    if profile.machine.cpu is None:
        return {}
    fields = profile.machine.cpu.split(",")
    if not fields or fields[0] != "rv64":
        raise ValueError(f"profile {profile.name} does not use an rv64 CPU")
    selectors: dict[str, bool] = {}
    for field in fields[1:]:
        name, separator, value = field.partition("=")
        if not separator or value not in {"true", "false"} or name in selectors:
            raise ValueError(f"profile {profile.name} has invalid CPU selectors")
        selectors[name] = value == "true"
    return selectors


def _validate_registered_profile(profile: QemuUbootProfile) -> None:
    validate_registered_profile(profile)
    if profile.fidelity is Fidelity.CONTRACT_APPROXIMATION:
        validate_profile_policy(load_contract(), profile)


def _validate_registered_variant_selection(
    profile: QemuUbootProfile,
    variant: QemuUbootVariant,
) -> None:
    _validate_registered_profile(profile)
    validate_registered_variant(variant)
    if profile.name != variant.base_profile_name:
        raise ValueError(
            f"variant {variant.name} requires profile {variant.base_profile_name}"
        )


def remove_rng_seed_if_present(dtb: Path) -> bool:
    """Remove ``/chosen/rng-seed`` when present and prove the deletion."""

    if "rng-seed" not in _properties(dtb, "/chosen"):
        return False
    _run(["fdtput", "-d", str(dtb), "/chosen", "rng-seed"])
    if "rng-seed" in _properties(dtb, "/chosen"):
        raise ValueError("failed to remove /chosen/rng-seed")
    return True


def _inspect_real_dtb(dtb: Path) -> RealDtbFacts:
    contract = load_contract()
    identity_claim = contract.raw["claims"]["identity"]
    expected_model = identity_claim["model"]
    expected_compatible = tuple(identity_claim["compatible"])

    model = _string_property(dtb, "/", "model")
    if model != expected_model:
        raise ValueError("real DTB model does not match the Megrez contract")
    compatible = tuple(_string_property(dtb, "/", "compatible").split())
    if compatible != expected_compatible:
        raise ValueError("real DTB compatible list does not match the Megrez contract")

    cpu_address_cells = _integer_property(dtb, "/cpus", "#address-cells")
    if cpu_address_cells <= 0:
        raise ValueError("real DTB has invalid CPU address cells")
    cpu_records: list[tuple[int, str]] = []
    for name in _subnodes(dtb, "/cpus"):
        node = f"/cpus/{name}"
        if _string_property(dtb, node, "device_type", default="missing") != "cpu":
            continue
        status = _string_property(dtb, node, "status", default="okay")
        if status == "disabled":
            continue
        if status not in {"ok", "okay"}:
            raise ValueError(f"real DTB has invalid CPU status: {status}")
        reg = _hex_cells(dtb, node, "reg")
        if len(reg) != cpu_address_cells:
            raise ValueError("real DTB CPU reg has the wrong cell count")
        cpu_records.append(
            (_cells_to_integer(reg), _string_property(dtb, node, "mmu-type"))
        )
    cpu_records.sort()
    enabled_cpu_ids = tuple(cpu_id for cpu_id, _mmu_type in cpu_records)
    if enabled_cpu_ids != contract.enabled_harts:
        raise ValueError("real DTB enabled CPU IDs do not match the Megrez contract")
    mmu_types = tuple(mmu_type for _cpu_id, mmu_type in cpu_records)
    if mmu_types != (contract.mmu_mode,) * len(contract.enabled_harts):
        raise ValueError("real DTB MMU types do not all declare riscv,sv48")

    address_cells = _integer_property(dtb, "/", "#address-cells")
    size_cells = _integer_property(dtb, "/", "#size-cells")
    memory_ranges: list[range] = []
    for name in _subnodes(dtb, "/"):
        node = f"/{name}"
        if _string_property(dtb, node, "device_type", default="missing") != "memory":
            continue
        memory_ranges.extend(
            _ranges_from_property(
                dtb,
                node,
                "reg",
                address_cells=address_cells,
                size_cells=size_cells,
            )
        )
    dram = next((item for item in memory_ranges if item == contract.dram), None)
    if dram is None:
        raise ValueError("real DTB does not contain the contracted 16 GiB DRAM range")

    stdout_path = _string_property(dtb, "/chosen", "stdout-path", default="")
    if not stdout_path:
        raise ValueError("real DTB /chosen/stdout-path must be non-empty")
    rng_seed_present = "rng-seed" in _properties(dtb, "/chosen")
    if rng_seed_present:
        raise ValueError("real DTB unexpectedly contains /chosen/rng-seed")

    reserved = "/reserved-memory"
    reserved_address_cells = _integer_property(dtb, reserved, "#address-cells")
    reserved_size_cells = _integer_property(dtb, reserved, "#size-cells")
    fixed_no_map: list[range] = []
    cma_nodes: list[str] = []
    for name in _subnodes(dtb, reserved):
        node = f"{reserved}/{name}"
        properties = _properties(dtb, node)
        if "no-map" in properties and "reg" in properties:
            fixed_no_map.extend(
                _ranges_from_property(
                    dtb,
                    node,
                    "reg",
                    address_cells=reserved_address_cells,
                    size_cells=reserved_size_cells,
                )
            )
        if "linux,cma-default" in properties:
            cma_nodes.append(node)

    expected_fixed = {
        _contract_range(item) for item in contract.raw["memory"]["fixed_no_map"]
    }
    if not expected_fixed.issubset(set(fixed_no_map)):
        raise ValueError("real DTB is missing a contracted fixed no-map range")
    if len(cma_nodes) != 1:
        raise ValueError("real DTB must contain exactly one default CMA node")

    cma_node = cma_nodes[0]
    cma_size = _sized_integer_property(
        dtb,
        cma_node,
        "size",
        size_cells=reserved_size_cells,
    )
    cma_alignment = _sized_integer_property(
        dtb,
        cma_node,
        "alignment",
        size_cells=reserved_size_cells,
    )
    cma_alloc_ranges = _ranges_from_property(
        dtb,
        cma_node,
        "alloc-ranges",
        address_cells=reserved_address_cells,
        size_cells=reserved_size_cells,
    )
    cma_contract = contract.raw["memory"]["dynamic_cma"]
    expected_cma_size = int(cma_contract["size"], 16)
    expected_cma_alignment = int(cma_contract["alignment"], 16)
    expected_cma_alloc = range(
        int(cma_contract["alloc_start"], 16),
        int(cma_contract["alloc_end_exclusive"], 16),
    )
    if cma_size != expected_cma_size:
        raise ValueError("real DTB CMA size does not match the Megrez contract")
    if cma_alignment != expected_cma_alignment:
        raise ValueError("real DTB CMA alignment does not match the Megrez contract")
    if cma_alloc_ranges != (expected_cma_alloc,):
        raise ValueError("real DTB CMA alloc-ranges do not match the Megrez contract")

    return RealDtbFacts(
        model=model,
        compatible=compatible,
        enabled_cpu_ids=enabled_cpu_ids,
        mmu_types=mmu_types,
        dram=dram,
        stdout_path=stdout_path,
        rng_seed_present=rng_seed_present,
        fixed_no_map=tuple(
            sorted(fixed_no_map, key=lambda item: (item.start, item.stop))
        ),
        cma_size=cma_size,
        cma_alignment=cma_alignment,
        cma_alloc_range=cma_alloc_ranges[0],
    )


def inspect_real_dtb(dtb: Path) -> RealDtbFacts:
    """Read and validate the real Megrez DTB structure without mutating it."""

    try:
        metadata = dtb.stat()
    except OSError as error:
        raise DtbInspectionError(f"cannot stat real DTB {dtb}: {error}") from error
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size == 0:
        raise ValueError(f"missing or empty real DTB: {dtb}")
    try:
        return _inspect_real_dtb(dtb)
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() if error.stderr else "fdtget failed"
        raise DtbInspectionError(
            f"cannot inspect real DTB structure: {detail}"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise DtbInspectionError("timed out while inspecting the real DTB") from error
    except OSError as error:
        raise DtbInspectionError(f"cannot run DTB inspection tools: {error}") from error


def audit_generated_dtb(
    *,
    profile: QemuUbootProfile,
    dtb: Path,
) -> GeneratedDtbAudit:
    """Read a generated DTB back and reject profile-contract drift."""

    _validate_registered_profile(profile)
    if not dtb.is_file() or dtb.stat().st_size == 0:
        raise ValueError(f"missing or empty generated DTB: {dtb}")

    cpu_records: list[tuple[int, str, frozenset[str]]] = []
    for name in _subnodes(dtb, "/cpus"):
        node = f"/cpus/{name}"
        if _string_property(dtb, node, "device_type", default="missing") != "cpu":
            continue
        status = _string_property(dtb, node, "status", default="okay")
        if status == "disabled":
            continue
        if status not in {"ok", "okay"}:
            raise ValueError(f"generated DTB has invalid CPU status: {status}")
        cpu_id = _cells_to_integer(_hex_cells(dtb, node, "reg"))
        raw_mmu_type = _string_property(dtb, node, "mmu-type", default="none")
        mmu_type = None if raw_mmu_type == "none" else raw_mmu_type
        extensions = frozenset(
            _string_property(
                dtb,
                node,
                "riscv,isa-extensions",
                default="",
            ).split()
        )
        cpu_records.append((cpu_id, mmu_type, extensions))
    cpu_records.sort(key=lambda record: record[0])
    cpu_ids = tuple(record[0] for record in cpu_records)
    if len(set(cpu_ids)) != len(cpu_ids):
        raise ValueError("generated DTB has duplicate CPU IDs")
    if cpu_ids != tuple(range(profile.hart_count)):
        raise ValueError(
            f"generated DTB CPU IDs {cpu_ids} do not match profile {profile.name}"
        )

    mmu_types = tuple(record[1] for record in cpu_records)
    if mmu_types != profile.machine.mmu_types:
        raise ValueError("generated DTB MMU types do not match the selected profile")

    selectors = _profile_cpu_selectors(profile)
    for _cpu_id, _mmu_type, extensions in cpu_records:
        for extension, enabled in selectors.items():
            if (extension in extensions) != enabled:
                raise ValueError(
                    f"generated DTB extension {extension} does not match profile"
                )

    address_cells = _integer_property(dtb, "/", "#address-cells")
    size_cells = _integer_property(dtb, "/", "#size-cells")
    if address_cells <= 0 or size_cells <= 0:
        raise ValueError("generated DTB has invalid root cell widths")
    memory_nodes = tuple(
        name for name in _subnodes(dtb, "/") if name.startswith("memory@")
    )
    if len(memory_nodes) != 1:
        raise ValueError("generated DTB must contain exactly one memory node")
    memory_node = f"/{memory_nodes[0]}"
    if _string_property(dtb, memory_node, "device_type", default="missing") != "memory":
        raise ValueError("generated DTB memory node has the wrong device_type")
    cells = _hex_cells(dtb, memory_node, "reg")
    if len(cells) != address_cells + size_cells:
        raise ValueError("generated DTB memory reg has the wrong cell count")
    start = _cells_to_integer(cells[:address_cells])
    size = _cells_to_integer(cells[address_cells:])
    memory = range(start, start + size)
    expected_memory = range(0x8000_0000, 0x8000_0000 + profile.memory_bytes)
    if memory != expected_memory:
        raise ValueError("generated DTB memory does not match the selected profile")

    rng_seed_present = "rng-seed" in _properties(dtb, "/chosen")
    if profile.remove_rng_seed and rng_seed_present:
        raise ValueError("generated DTB still contains /chosen/rng-seed")
    return GeneratedDtbAudit(
        machine=profile.machine.name,
        cpu_ids=cpu_ids,
        mmu_types=mmu_types,
        memory=memory,
        ad_extension=profile.ad_extension,
        rng_seed_present=rng_seed_present,
        sha256=artifact_identity(dtb).sha256,
    )


def _payload_dtb_variant_audit_from_snapshots(
    *,
    profile: QemuUbootProfile,
    variant: QemuUbootVariant,
    source_snapshot: _SemanticDtbSnapshot,
    payload_snapshot: _SemanticDtbSnapshot,
    source_identity: ArtifactIdentity,
    payload_identity: ArtifactIdentity,
) -> PayloadDtbVariantAudit:
    """Prove the one registered semantic difference between fixed DTB bytes."""

    _validate_registered_variant_selection(profile, variant)
    if variant.payload_uart_compatible != _PAYLOAD_UART_COMPATIBLE[0]:
        raise ValueError("registered variant has an invalid payload UART compatible")

    uart_node = _resolve_payload_uart(source_snapshot)
    payload_uart_node = _resolve_payload_uart(payload_snapshot)
    if payload_uart_node != uart_node:
        raise ValueError("payload UART node changed during derivation")
    source_compatible = _string_list_property(
        source_snapshot,
        uart_node,
        "compatible",
    )
    if source_compatible != _SOURCE_UART_COMPATIBLE:
        raise ValueError("source UART compatible must be exactly ns16550a")
    payload_compatible = _string_list_property(
        payload_snapshot,
        uart_node,
        "compatible",
    )
    if payload_compatible != _PAYLOAD_UART_COMPATIBLE:
        raise ValueError("payload UART compatible must be exactly snps,dw-apb-uart")

    if payload_snapshot.nodes != source_snapshot.nodes:
        raise ValueError("payload DTB has an unexpected semantic change in nodes")
    property_keys = set(source_snapshot.properties) | set(payload_snapshot.properties)
    changed_properties = tuple(
        sorted(
            key
            for key in property_keys
            if source_snapshot.properties.get(key)
            != payload_snapshot.properties.get(key)
        )
    )
    if changed_properties != ((uart_node, "compatible"),):
        raise ValueError("payload DTB has an unexpected semantic change set")

    return PayloadDtbVariantAudit(
        variant=variant.name,
        uart_node=uart_node,
        source_compatible=source_compatible,
        payload_compatible=payload_compatible,
        changed_properties=changed_properties,
        source_size=source_identity.size,
        source_sha256=source_identity.sha256,
        source_crc32=source_identity.crc32,
        payload_size=payload_identity.size,
        payload_sha256=payload_identity.sha256,
        payload_crc32=payload_identity.crc32,
    )


def _audit_payload_dtb_variant_pair(
    *,
    profile: QemuUbootProfile,
    variant: QemuUbootVariant,
    source_dtb: Path,
    payload_dtb: Path,
) -> PayloadDtbVariantAudit:
    """Audit one private, fixed source/payload DTB pair."""

    source_identity = artifact_identity(source_dtb)
    payload_identity = artifact_identity(payload_dtb)
    source_snapshot = _semantic_dtb_snapshot(source_dtb)
    payload_snapshot = _semantic_dtb_snapshot(payload_dtb)
    return _payload_dtb_variant_audit_from_snapshots(
        profile=profile,
        variant=variant,
        source_snapshot=source_snapshot,
        payload_snapshot=payload_snapshot,
        source_identity=source_identity,
        payload_identity=payload_identity,
    )


def derive_payload_dtb_variant(
    *,
    profile: QemuUbootProfile,
    variant: QemuUbootVariant,
    source_dtb: Path,
    payload_dtb: Path,
) -> PayloadDtbVariantAudit:
    """Derive and prove the registered payload-only UART-compatible change."""

    _validate_registered_variant_selection(profile, variant)
    if variant.payload_uart_compatible != _PAYLOAD_UART_COMPATIBLE[0]:
        raise ValueError("registered variant has an invalid payload UART compatible")

    with (
        PinnedOutputDirectory.open(payload_dtb.parent) as output_directory,
        PinnedRegularInput.open(source_dtb, label="source DTB") as pinned_source,
        tempfile.TemporaryDirectory(prefix="asterinas-qemu-payload-dtb-") as tmp,
    ):
        published_path = output_directory.path / payload_dtb.name
        if published_path == pinned_source.path:
            raise ValueError("payload DTB must not replace the source DTB")

        temporary = Path(tmp)
        source_snapshot_path = temporary / "source-before.dtb"
        staged_payload_path = temporary / "payload.dtb"
        source_after_path = temporary / "source-after.dtb"
        pinned_source.copy_to(source_snapshot_path)

        source_snapshot = _semantic_dtb_snapshot(source_snapshot_path)
        uart_node = _resolve_payload_uart(source_snapshot)
        source_compatible = _string_list_property(
            source_snapshot,
            uart_node,
            "compatible",
        )
        if source_compatible != _SOURCE_UART_COMPATIBLE:
            raise ValueError("source UART compatible must be exactly ns16550a")

        pinned_source.copy_to(staged_payload_path)
        _run(
            [
                "fdtput",
                "-t",
                "s",
                str(staged_payload_path),
                uart_node,
                "compatible",
                _PAYLOAD_UART_COMPATIBLE[0],
            ]
        )

        variant_audit = _audit_payload_dtb_variant_pair(
            profile=profile,
            variant=variant,
            source_dtb=source_snapshot_path,
            payload_dtb=staged_payload_path,
        )
        audit_generated_dtb(profile=profile, dtb=staged_payload_path)
        payload_identity = artifact_identity(staged_payload_path)
        expected_payload_identity = ArtifactIdentity(
            size=variant_audit.payload_size,
            sha256=variant_audit.payload_sha256,
            crc32=variant_audit.payload_crc32,
        )
        if payload_identity != expected_payload_identity:
            raise RuntimeError("staged payload DTB changed after its semantic audit")
        payload_bytes = staged_payload_path.read_bytes()
        if artifact_identity(staged_payload_path) != payload_identity:
            raise RuntimeError("staged payload DTB changed after it was audited")

        pinned_source.copy_to(source_after_path)
        expected_source_identity = ArtifactIdentity(
            size=variant_audit.source_size,
            sha256=variant_audit.source_sha256,
            crc32=variant_audit.source_crc32,
        )
        if artifact_identity(source_after_path) != expected_source_identity:
            raise RuntimeError("source DTB changed during payload derivation")

        output_directory.verify_current()
        with output_directory.atomic_write(
            payload_dtb.name,
            payload_bytes,
        ) as publication:
            try:
                output_directory.verify_current()
                output_directory.verify_entry(payload_dtb.name, publication.identity)
            except (OSError, RuntimeError):
                output_directory.verify_entry(
                    payload_dtb.name,
                    publication.identity,
                )
                raise

    return variant_audit


def payload_dtb_variant_audit_record(
    audit: PayloadDtbVariantAudit,
) -> dict[str, object]:
    """Serialize the exact identity and semantic proof for a payload variant."""

    return {
        "variant": audit.variant,
        "uart_node": audit.uart_node,
        "source_compatible": list(audit.source_compatible),
        "payload_compatible": list(audit.payload_compatible),
        "changed_properties": [list(item) for item in audit.changed_properties],
        "source_size": audit.source_size,
        "source_sha256": audit.source_sha256,
        "source_crc32": audit.source_crc32,
        "payload_size": audit.payload_size,
        "payload_sha256": audit.payload_sha256,
        "payload_crc32": audit.payload_crc32,
    }


def _variant_audit_identity(
    value: Mapping[str, object],
    *,
    prefix: str,
) -> tuple[int, str, str]:
    size = value[f"{prefix}_size"]
    sha256 = value[f"{prefix}_sha256"]
    crc32 = value[f"{prefix}_crc32"]
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or size <= 0
        or not isinstance(sha256, str)
        or _SHA256_RE.fullmatch(sha256) is None
        or not isinstance(crc32, str)
        or _CRC32_RE.fullmatch(crc32) is None
    ):
        raise ValueError(f"payload DTB variant audit {prefix} identity is invalid")
    return size, sha256, crc32


def _artifact_identity_tuple(path: Path, *, label: str) -> tuple[int, str, str]:
    try:
        identity = artifact_identity(path)
    except OSError as error:
        raise ValueError(f"{label} must be an existing regular file") from error
    return identity.size, identity.sha256, identity.crc32


def load_payload_dtb_variant_audit(
    path: Path,
    *,
    profile: QemuUbootProfile,
    variant: QemuUbootVariant,
    source_dtb: Path,
    payload_dtb: Path,
) -> PayloadDtbVariantAudit:
    """Load a strict variant audit and bind it to both DTB byte identities."""

    _validate_registered_variant_selection(profile, variant)
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(
            f"cannot load payload DTB variant audit {path}: {error}"
        ) from error
    if not isinstance(value, Mapping) or set(value) != _VARIANT_AUDIT_RECORD_KEYS:
        raise ValueError("payload DTB variant audit has invalid fields")
    if value["variant"] != variant.name:
        raise ValueError("payload DTB variant audit variant does not match the run")

    uart_node = value["uart_node"]
    if (
        not isinstance(uart_node, str)
        or not uart_node.startswith("/")
        or uart_node == "/"
        or uart_node.endswith("/")
        or "//" in uart_node
        or any(
            component in {"", ".", ".."} or _DTB_NAME_RE.fullmatch(component) is None
            for component in uart_node.split("/")[1:]
        )
    ):
        raise ValueError("payload DTB variant audit UART node is invalid")

    source_values = value["source_compatible"]
    payload_values = value["payload_compatible"]
    if not isinstance(source_values, list) or any(
        not isinstance(item, str) for item in source_values
    ):
        raise ValueError("payload DTB variant audit source compatible is invalid")
    if not isinstance(payload_values, list) or any(
        not isinstance(item, str) for item in payload_values
    ):
        raise ValueError("payload DTB variant audit payload compatible is invalid")
    source_compatible = tuple(source_values)
    payload_compatible = tuple(payload_values)
    if source_compatible != _SOURCE_UART_COMPATIBLE:
        raise ValueError("payload DTB variant audit source compatible is invalid")
    if payload_compatible != _PAYLOAD_UART_COMPATIBLE or payload_compatible != (
        variant.payload_uart_compatible,
    ):
        raise ValueError("payload DTB variant audit payload compatible is invalid")

    changed_values = value["changed_properties"]
    if not isinstance(changed_values, list) or any(
        not isinstance(item, list)
        or len(item) != 2
        or any(not isinstance(field, str) for field in item)
        for item in changed_values
    ):
        raise ValueError("payload DTB variant audit change set is invalid")
    changed_properties = tuple((item[0], item[1]) for item in changed_values)
    if changed_properties != ((uart_node, "compatible"),):
        raise ValueError("payload DTB variant audit change set is invalid")

    source_identity = _variant_audit_identity(value, prefix="source")
    payload_identity = _variant_audit_identity(value, prefix="payload")
    recorded_audit = PayloadDtbVariantAudit(
        variant=variant.name,
        uart_node=uart_node,
        source_compatible=source_compatible,
        payload_compatible=payload_compatible,
        changed_properties=changed_properties,
        source_size=source_identity[0],
        source_sha256=source_identity[1],
        source_crc32=source_identity[2],
        payload_size=payload_identity[0],
        payload_sha256=payload_identity[1],
        payload_crc32=payload_identity[2],
    )
    with (
        tempfile.TemporaryDirectory(prefix="asterinas-qemu-dtb-pair-") as tmp,
        PinnedRegularInput.open(source_dtb, label="source DTB") as pinned_source,
        PinnedRegularInput.open(payload_dtb, label="payload DTB") as pinned_payload,
    ):
        temporary = Path(tmp)
        private_source = temporary / "source.dtb"
        private_payload = temporary / "payload.dtb"
        pinned_source.copy_to(private_source)
        pinned_payload.copy_to(private_payload)

        if (
            _artifact_identity_tuple(private_source, label="source DTB")
            != source_identity
        ):
            raise ValueError("payload DTB variant audit source DTB identity changed")
        if (
            _artifact_identity_tuple(private_payload, label="payload DTB")
            != payload_identity
        ):
            raise ValueError("payload DTB variant audit payload DTB identity changed")
        actual_audit = _audit_payload_dtb_variant_pair(
            profile=profile,
            variant=variant,
            source_dtb=private_source,
            payload_dtb=private_payload,
        )

    if recorded_audit != actual_audit:
        raise ValueError(
            "payload DTB variant audit semantic proof does not match the actual pair"
        )
    return actual_audit


def generated_dtb_audit_record(
    audit: GeneratedDtbAudit,
    dtb: Path,
) -> dict[str, object]:
    """Serialize an audit together with the identity of its exact DTB bytes."""

    identity = artifact_identity(dtb)
    if identity.sha256 != audit.sha256:
        raise ValueError("generated DTB changed after it was audited")
    return {
        "machine": audit.machine,
        "cpu_ids": list(audit.cpu_ids),
        "mmu_types": list(audit.mmu_types),
        "memory_start": audit.memory.start,
        "memory_end_exclusive": audit.memory.stop,
        "ad_extension": audit.ad_extension,
        "rng_seed_present": audit.rng_seed_present,
        "size": identity.size,
        "sha256": identity.sha256,
        "crc32": identity.crc32,
    }


def load_generated_dtb_audit(
    path: Path,
    *,
    profile: QemuUbootProfile,
    expected_size: int,
    expected_crc32: str,
) -> GeneratedDtbAudit:
    """Load a prepared audit and bind it to the selected run and manifest."""

    _validate_registered_profile(profile)
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load generated DTB audit {path}: {error}") from error
    if not isinstance(value, Mapping) or set(value) != _AUDIT_RECORD_KEYS:
        raise ValueError("generated DTB audit has invalid fields")
    if value["machine"] != profile.machine.name:
        raise ValueError("generated DTB audit machine does not match the run")
    if (
        isinstance(value["size"], bool)
        or not isinstance(value["size"], int)
        or value["size"] != expected_size
        or not isinstance(value["crc32"], str)
        or _CRC32_RE.fullmatch(value["crc32"]) is None
        or value["crc32"] != expected_crc32
    ):
        raise ValueError("generated DTB audit identity does not match the manifest")
    sha256 = value["sha256"]
    if not isinstance(sha256, str) or _SHA256_RE.fullmatch(sha256) is None:
        raise ValueError("generated DTB audit SHA-256 is invalid")

    cpu_values = value["cpu_ids"]
    mmu_values = value["mmu_types"]
    if (
        not isinstance(cpu_values, list)
        or any(isinstance(cpu, bool) or not isinstance(cpu, int) for cpu in cpu_values)
        or not isinstance(mmu_values, list)
        or any(mmu is not None and not isinstance(mmu, str) for mmu in mmu_values)
    ):
        raise ValueError("generated DTB audit CPU facts are invalid")
    cpu_ids = tuple(cpu_values)
    mmu_types = tuple(mmu_values)
    if cpu_ids != tuple(range(profile.hart_count)):
        raise ValueError("generated DTB audit CPU IDs do not match the profile")
    if mmu_types != profile.machine.mmu_types:
        raise ValueError("generated DTB audit MMU types do not match the profile")

    start = value["memory_start"]
    end = value["memory_end_exclusive"]
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
    ):
        raise ValueError("generated DTB audit memory facts are invalid")
    memory = range(start, end)
    if memory != range(0x8000_0000, 0x8000_0000 + profile.memory_bytes):
        raise ValueError("generated DTB audit memory does not match the profile")
    if value["ad_extension"] != profile.ad_extension:
        raise ValueError("generated DTB audit A/D mode does not match the profile")
    rng_seed_present = value["rng_seed_present"]
    if not isinstance(rng_seed_present, bool):
        raise ValueError("generated DTB audit rng-seed fact is invalid")
    if profile.remove_rng_seed and rng_seed_present:
        raise ValueError("generated DTB audit still reports /chosen/rng-seed")
    return GeneratedDtbAudit(
        machine=profile.machine.name,
        cpu_ids=cpu_ids,
        mmu_types=mmu_types,
        memory=memory,
        ad_extension=profile.ad_extension,
        rng_seed_present=rng_seed_present,
        sha256=sha256,
    )


def _extract_boot_disk_dtb(
    boot_disk: Path,
    destination: Path,
    *,
    filename: str,
) -> None:
    """Extract the DTB that U-Boot will load from the prepared ext4 image."""

    subprocess.run(
        [
            "debugfs",
            "-R",
            f"dump /{filename} {destination}",
            str(boot_disk),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10.0,
    )
    if not destination.is_file() or destination.stat().st_size == 0:
        raise ValueError(f"prepared boot disk has no non-empty /{filename}")


def verify_prepared_dtb(
    *,
    boot_disk: Path,
    audit_path: Path,
    profile: QemuUbootProfile,
    expected_size: int,
    expected_crc32: str,
    variant: QemuUbootVariant | None = None,
    variant_audit_path: Path | None = None,
    source_dtb: Path | None = None,
) -> GeneratedDtbAudit:
    """Bind the serialized audit to the exact DTB in the prepared boot disk."""

    if variant is None:
        if variant_audit_path is not None or source_dtb is not None:
            raise ValueError("standard DTB verification rejects variant materials")
    elif variant_audit_path is None or source_dtb is None:
        raise ValueError(
            "diagnostic DTB verification requires variant, variant audit, and source DTB"
        )
    else:
        _validate_registered_variant_selection(profile, variant)

    expected = load_generated_dtb_audit(
        audit_path,
        profile=profile,
        expected_size=expected_size,
        expected_crc32=expected_crc32,
    )
    with tempfile.TemporaryDirectory(prefix="asterinas-qemu-dtb-") as tmp:
        extracted = Path(tmp) / "qemu-virt.dtb"
        _extract_boot_disk_dtb(
            boot_disk,
            extracted,
            filename=profile.machine.dtb_filename,
        )
        identity = artifact_identity(extracted)
        if identity.size != expected_size or identity.crc32 != expected_crc32:
            raise ValueError(
                "actual boot disk DTB identity does not match the manifest"
            )
        actual = audit_generated_dtb(profile=profile, dtb=extracted)
        if variant is not None:
            assert variant_audit_path is not None
            assert source_dtb is not None
            load_payload_dtb_variant_audit(
                variant_audit_path,
                profile=profile,
                variant=variant,
                source_dtb=source_dtb,
                payload_dtb=extracted,
            )
    if actual != expected:
        raise ValueError("generated DTB audit does not match the actual boot disk DTB")
    return actual


def generate_sanitized_dtb(
    *,
    profile: QemuUbootProfile,
    dtb: Path,
    dts: Path,
    slow_permit: object | None = None,
) -> GeneratedDtbAudit:
    """Generate the selected QEMU DTB, apply seed policy, and audit it."""

    _validate_registered_profile(profile)
    require_profile_launch_allowed(
        profile,
        slow_permit=slow_permit,
    )
    dtb.parent.mkdir(parents=True, exist_ok=True)
    dts.parent.mkdir(parents=True, exist_ok=True)
    dtb.unlink(missing_ok=True)
    dts.unlink(missing_ok=True)
    command = generated_dtb_qemu_argv(profile=profile, dtb=dtb)
    subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=30.0,
    )
    if not dtb.is_file() or dtb.stat().st_size == 0:
        raise ValueError("QEMU did not produce a non-empty DTB")
    if profile.remove_rng_seed:
        remove_rng_seed_if_present(dtb)
    subprocess.run(
        ["dtc", "-I", "dtb", "-O", "dts", "-o", str(dts), str(dtb)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10.0,
    )
    return audit_generated_dtb(profile=profile, dtb=dtb)


def generated_dtb_qemu_argv(
    *,
    profile: QemuUbootProfile,
    dtb: Path,
) -> list[str]:
    """Render a registered QEMU machine's side-effect-free DTB dump command."""

    _validate_registered_profile(profile)
    if profile.machine.dtb_provider is not DtbProvider.GENERATED_PAYLOAD:
        raise ValueError("registered machine has no generated payload DTB")
    if "," in str(dtb):
        raise ValueError("QEMU DTB output path must not contain a comma")
    command = [
        "qemu-system-riscv64",
        "-machine",
        f"{profile.machine.qemu_machine.value},dumpdtb={dtb}",
    ]
    if profile.machine.cpu is not None:
        command.extend(("-cpu", profile.machine.cpu))
    command.extend(("-m", profile.memory, "-smp", str(profile.hart_count)))
    return command


def _profile_argument(value: str) -> QemuUbootProfile:
    try:
        return profile_by_name(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _variant_argument(value: str) -> QemuUbootVariant:
    try:
        return variant_by_name(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _publish_json_record(path: Path, value: Mapping[str, object]) -> None:
    payload = (json.dumps(value, sort_keys=True) + "\n").encode()
    with PinnedOutputDirectory.open(path.parent) as output_directory:
        output_directory.verify_current()
        with output_directory.atomic_write(path.name, payload) as publication:
            try:
                output_directory.verify_current()
                output_directory.verify_entry(path.name, publication.identity)
            except (OSError, RuntimeError):
                output_directory.verify_entry(path.name, publication.identity)
                raise


def _require_distinct_audit_output(
    audit_output: Path,
    *dtb_inputs: Path,
) -> None:
    try:
        resolved_output = audit_output.resolve(strict=False)
        resolved_inputs = tuple(path.resolve(strict=False) for path in dtb_inputs)
    except (OSError, RuntimeError) as error:
        raise ValueError("cannot resolve DTB audit input and output paths") from error
    if resolved_output in resolved_inputs:
        raise ValueError("audit output must differ from DTB inputs")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate-profile")
    validate.add_argument("--profile", type=_profile_argument, required=True)
    selection = subparsers.add_parser("validate-selection")
    selection.add_argument("--profile", type=_profile_argument, required=True)
    selection.add_argument("--variant", type=_variant_argument, required=True)
    generate = subparsers.add_parser("generate")
    generate.add_argument("--profile", type=_profile_argument, required=True)
    generate.add_argument("--dtb", type=Path, required=True)
    generate.add_argument("--dts", type=Path, required=True)
    derive = subparsers.add_parser("derive-variant")
    derive.add_argument("--profile", type=_profile_argument, required=True)
    derive.add_argument("--variant", type=_variant_argument, required=True)
    derive.add_argument("--source-dtb", type=Path, required=True)
    derive.add_argument("--payload-dtb", type=Path, required=True)
    derive.add_argument("--audit-output", type=Path, required=True)
    audit = subparsers.add_parser("audit-existing")
    audit.add_argument("--profile", type=_profile_argument, required=True)
    audit.add_argument("--dtb", type=Path, required=True)
    audit.add_argument("--audit-output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "validate-profile":
        _validate_registered_profile(args.profile)
        print(f"profile={args.profile.name}")
        return 0
    if args.command == "validate-selection":
        _validate_registered_variant_selection(args.profile, args.variant)
        print(f"profile={args.profile.name} variant={args.variant.name}")
        return 0
    if args.command == "generate":
        audit = generate_sanitized_dtb(
            profile=args.profile,
            dtb=args.dtb,
            dts=args.dts,
        )
        print(json.dumps(generated_dtb_audit_record(audit, args.dtb), sort_keys=True))
        return 0
    if args.command == "derive-variant":
        _require_distinct_audit_output(
            args.audit_output,
            args.source_dtb,
            args.payload_dtb,
        )
        audit = derive_payload_dtb_variant(
            profile=args.profile,
            variant=args.variant,
            source_dtb=args.source_dtb,
            payload_dtb=args.payload_dtb,
        )
        _publish_json_record(
            args.audit_output,
            payload_dtb_variant_audit_record(audit),
        )
        load_payload_dtb_variant_audit(
            args.audit_output,
            profile=args.profile,
            variant=args.variant,
            source_dtb=args.source_dtb,
            payload_dtb=args.payload_dtb,
        )
        return 0
    if args.command == "audit-existing":
        _require_distinct_audit_output(args.audit_output, args.dtb)
        audit = audit_generated_dtb(profile=args.profile, dtb=args.dtb)
        identity = artifact_identity(args.dtb)
        _publish_json_record(
            args.audit_output,
            generated_dtb_audit_record(audit, args.dtb),
        )
        if artifact_identity(args.dtb) != identity:
            raise RuntimeError("generated DTB changed while its audit was published")
        return 0
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
