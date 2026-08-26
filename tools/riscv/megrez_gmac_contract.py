#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Strict physical identity and resource contract for the Megrez GMACs."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from types import MappingProxyType
from typing import Any, Protocol
import zlib


_TOP_LEVEL_KEYS = {"schema_version", "contract_id", "dtb_identity", "ports"}
_IDENTITY_KEYS = {"sha256", "size", "crc32"}
_PORT_KEYS = {
    "alias_index",
    "node_path",
    "controller_id",
    "status",
    "compatible",
    "mmio_start",
    "mmio_size",
    "interrupt_parent",
    "interrupt",
    "dma_noncoherent",
    "clock_names",
    "clock_cells",
    "reset_name",
    "reset_cells",
    "phy_mode",
    "phy_address",
    "phy_address_source",
    "mac_address",
    "mac_address_source",
    "hsp_sp_csr",
    "syscrg_csr",
    "delay_registers",
    "delay_1000m",
    "delay_100m",
    "delay_10m",
    "rgmii_select",
    "reset_gpio",
    "axi_blen",
    "axi_rd_osr_lmt",
    "axi_wr_osr_lmt",
    "axi_lpi_en",
}
_CLOCK_NAMES = ("app", "stmmaceth", "tx")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CRC32 = re.compile(r"[0-9a-f]{8}")
_MAC_ADDRESS = re.compile(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}")
_READ_CHUNK_SIZE = 1024 * 1024
_EXPECTED_DTB_SIZE = 154800
_EXPECTED_DTB_CRC32 = "4afcb20e"
_HSP_NODE = "/soc/hsp_sp_top_csr@0x50440000"
_SYSCRG_NODE = "/soc/sys-crg@51828000"

_REFERENCE = {
    0: {
        "node_path": "/soc/ethernet@50400000",
        "controller_id": 0,
        "mmio_start": 0x5040_0000,
        "mmio_size": 0x1_0000,
        "interrupt": 61,
        "hsp_sp_csr": (0x5044_0000, 0x2000, 0x1030, 0x100, 0x108),
        "syscrg_csr": (0x5182_8000, 0x8_0000, 0x148, 0x14C),
        "delay_registers": (0x114, 0x118, 0x11C),
    },
    1: {
        "node_path": "/soc/ethernet@50410000",
        "controller_id": 1,
        "mmio_start": 0x5041_0000,
        "mmio_size": 0x1_0000,
        "interrupt": 70,
        "hsp_sp_csr": (0x5044_0000, 0x2000, 0x1034, 0x200, 0x208),
        "syscrg_csr": (0x5182_8000, 0x8_0000, 0x148, 0x14C),
        "delay_registers": (0x214, 0x218, 0x21C),
    },
}


class ContractError(ValueError):
    """The GMAC contract is malformed or disagrees with Megrez hardware."""


class Runner(Protocol):
    def __call__(
        self, argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True)
class DtbIdentity:
    """Content identity read from one held regular-file descriptor."""

    sha256: str
    size: int
    crc32: str


@dataclass(frozen=True)
class GmacPort:
    """One complete boot-time GMAC candidate."""

    alias_index: int
    node_path: str
    controller_id: int
    status: str
    compatible: tuple[str, ...]
    mmio_start: int
    mmio_size: int
    interrupt_parent: int
    interrupt: int
    dma_noncoherent: bool
    clock_names: tuple[str, ...]
    clock_cells: tuple[int, ...]
    reset_name: str
    reset_cells: tuple[int, ...]
    phy_mode: str
    phy_address: int
    phy_address_source: str
    mac_address: str
    mac_address_source: str
    hsp_sp_csr: tuple[int, ...]
    syscrg_csr: tuple[int, ...]
    delay_registers: tuple[int, ...]
    delay_1000m: tuple[int, ...]
    delay_100m: tuple[int, ...]
    delay_10m: tuple[int, ...]
    rgmii_select: tuple[int, ...]
    reset_gpio: tuple[int, ...]
    axi_blen: tuple[int, ...]
    axi_rd_osr_lmt: int
    axi_wr_osr_lmt: int
    axi_lpi_en: int


@dataclass(frozen=True)
class GmacContract:
    """The exact two-port physical contract used by M5."""

    schema_version: int
    contract_id: str
    dtb_identity: DtbIdentity
    ports: tuple[GmacPort, GmacPort]


def _mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ContractError(f"{path} must be an object with string keys")
    return value


def _sequence(value: object, path: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractError(f"{path} must be an array")
    return value


def _exact_keys(value: Mapping[str, Any], keys: set[str], path: str) -> None:
    missing = keys - set(value)
    unknown = set(value) - keys
    if missing:
        raise ContractError(f"{path} missing fields: {sorted(missing)}")
    if unknown:
        raise ContractError(f"{path} has unknown fields: {sorted(unknown)}")


def _integer(value: object, path: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{path} must be an integer")
    if positive and value <= 0:
        raise ContractError(f"{path} must be positive")
    return value


def _string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{path} must be a non-empty string")
    return value


def _integer_tuple(value: object, path: str, length: int) -> tuple[int, ...]:
    sequence = _sequence(value, path)
    if len(sequence) != length:
        raise ContractError(f"{path} must contain exactly {length} integers")
    return tuple(_integer(item, f"{path} item") for item in sequence)


def _identity(value: object) -> DtbIdentity:
    raw = _mapping(value, "dtb_identity")
    _exact_keys(raw, _IDENTITY_KEYS, "dtb_identity")
    sha256 = _string(raw["sha256"], "dtb_identity.sha256")
    crc32 = _string(raw["crc32"], "dtb_identity.crc32")
    if _SHA256.fullmatch(sha256) is None:
        raise ContractError("dtb_identity.sha256 must be lowercase hexadecimal")
    if _CRC32.fullmatch(crc32) is None:
        raise ContractError("dtb_identity.crc32 must be lowercase hexadecimal")
    return DtbIdentity(
        sha256=sha256,
        size=_integer(raw["size"], "dtb_identity.size", positive=True),
        crc32=crc32,
    )


def _port(value: object, index: int) -> GmacPort:
    path = f"ports.{index}"
    raw = _mapping(value, path)
    _exact_keys(raw, _PORT_KEYS, path)

    alias_index = _integer(raw["alias_index"], f"{path}.alias_index")
    if alias_index not in _REFERENCE:
        raise ContractError(f"{path}.alias_index must be 0 or 1")

    compatible = tuple(
        _string(item, f"{path}.compatible item")
        for item in _sequence(raw["compatible"], f"{path}.compatible")
    )
    clock_names = tuple(
        _string(item, f"{path}.clock_names item")
        for item in _sequence(raw["clock_names"], f"{path}.clock_names")
    )
    clock_cells = _integer_tuple(raw["clock_cells"], f"{path}.clock_cells", 6)
    if any(cell <= 0 for cell in clock_cells[::2]):
        raise ContractError(f"{path}.clock_cells has an invalid provider")
    if len(set(clock_cells[::2])) != 1 or len(set(clock_cells[1::2])) != 3:
        raise ContractError(f"{path}.clock_cells must name three unique clocks")
    reset_cells = _integer_tuple(raw["reset_cells"], f"{path}.reset_cells", 3)
    if reset_cells[0] <= 0:
        raise ContractError(f"{path}.reset_cells has an invalid provider")
    hsp_sp_csr = _integer_tuple(raw["hsp_sp_csr"], f"{path}.hsp_sp_csr", 5)
    syscrg_csr = _integer_tuple(raw["syscrg_csr"], f"{path}.syscrg_csr", 4)
    delay_registers = _integer_tuple(
        raw["delay_registers"], f"{path}.delay_registers", 3
    )

    checked_reference = {
        "node_path": _string(raw["node_path"], f"{path}.node_path"),
        "controller_id": _integer(raw["controller_id"], f"{path}.controller_id"),
        "mmio_start": _integer(raw["mmio_start"], f"{path}.mmio_start"),
        "mmio_size": _integer(raw["mmio_size"], f"{path}.mmio_size", positive=True),
        "interrupt": _integer(raw["interrupt"], f"{path}.interrupt", positive=True),
        "hsp_sp_csr": hsp_sp_csr,
        "syscrg_csr": syscrg_csr,
        "delay_registers": delay_registers,
    }
    status = _string(raw["status"], f"{path}.status")
    if status != "okay":
        raise ContractError(f"{path}.status must be okay")
    if compatible != ("eswin,win2030-qos-eth",):
        raise ContractError(f"{path}.compatible is unsupported")
    if raw["dma_noncoherent"] is not True:
        raise ContractError(f"{path}.dma_noncoherent must be true")
    if clock_names != _CLOCK_NAMES:
        raise ContractError(f"{path}.clock_names is invalid")
    reset_name = _string(raw["reset_name"], f"{path}.reset_name")
    if reset_name != "ethrst":
        raise ContractError(f"{path}.reset_name must be ethrst")
    phy_mode = _string(raw["phy_mode"], f"{path}.phy_mode")
    if phy_mode != "rgmii-txid":
        raise ContractError(f"{path}.phy_mode must be rgmii-txid")
    phy_address = _integer(raw["phy_address"], f"{path}.phy_address")
    if not 0 <= phy_address <= 31:
        raise ContractError(f"{path}.phy_address must fit Clause 22")
    phy_address_source = _string(
        raw["phy_address_source"], f"{path}.phy_address_source"
    )
    if phy_address_source not in {"dt-property", "vendor-driver-fixed"}:
        raise ContractError(f"{path}.phy_address_source is unsupported")

    mac_address = _string(raw["mac_address"], f"{path}.mac_address")
    if _MAC_ADDRESS.fullmatch(mac_address) is None:
        raise ContractError(f"{path}.mac_address must be canonical")
    mac_bytes = bytes.fromhex(mac_address.replace(":", ""))
    if mac_bytes == b"\x00" * 6 or mac_bytes == b"\xff" * 6 or mac_bytes[0] & 1:
        raise ContractError(f"{path}.mac_address must be unicast")
    mac_address_source = _string(
        raw["mac_address_source"], f"{path}.mac_address_source"
    )
    if mac_address_source not in {
        "dt-local-mac-address",
        "dt-mac-address",
        "firmware-observed",
    }:
        raise ContractError(f"{path}.mac_address_source is unsupported")

    axi_blen = _integer_tuple(raw["axi_blen"], f"{path}.axi_blen", 7)
    if axi_blen != (0, 0, 0, 0, 16, 8, 4):
        raise ContractError(f"{path}.axi_blen is unsupported")
    axi_rd_osr_lmt = _integer(raw["axi_rd_osr_lmt"], f"{path}.axi_rd_osr_lmt")
    axi_wr_osr_lmt = _integer(raw["axi_wr_osr_lmt"], f"{path}.axi_wr_osr_lmt")
    axi_lpi_en = _integer(raw["axi_lpi_en"], f"{path}.axi_lpi_en")
    if (axi_rd_osr_lmt, axi_wr_osr_lmt, axi_lpi_en) != (2, 2, 0):
        raise ContractError(f"{path}.axi configuration is unsupported")

    return GmacPort(
        alias_index=alias_index,
        node_path=checked_reference["node_path"],
        controller_id=checked_reference["controller_id"],
        status=status,
        compatible=compatible,
        mmio_start=checked_reference["mmio_start"],
        mmio_size=checked_reference["mmio_size"],
        interrupt_parent=_integer(
            raw["interrupt_parent"], f"{path}.interrupt_parent", positive=True
        ),
        interrupt=checked_reference["interrupt"],
        dma_noncoherent=True,
        clock_names=clock_names,
        clock_cells=clock_cells,
        reset_name=reset_name,
        reset_cells=reset_cells,
        phy_mode=phy_mode,
        phy_address=phy_address,
        phy_address_source=phy_address_source,
        mac_address=mac_address,
        mac_address_source=mac_address_source,
        hsp_sp_csr=hsp_sp_csr,
        syscrg_csr=syscrg_csr,
        delay_registers=delay_registers,
        delay_1000m=_integer_tuple(raw["delay_1000m"], f"{path}.delay_1000m", 3),
        delay_100m=_integer_tuple(raw["delay_100m"], f"{path}.delay_100m", 3),
        delay_10m=_integer_tuple(raw["delay_10m"], f"{path}.delay_10m", 3),
        rgmii_select=_integer_tuple(raw["rgmii_select"], f"{path}.rgmii_select", 3),
        reset_gpio=_integer_tuple(raw["reset_gpio"], f"{path}.reset_gpio", 3),
        axi_blen=axi_blen,
        axi_rd_osr_lmt=axi_rd_osr_lmt,
        axi_wr_osr_lmt=axi_wr_osr_lmt,
        axi_lpi_en=axi_lpi_en,
    )


def validate_contract(value: object) -> GmacContract:
    """Validate and freeze one exact version-1 contract."""

    raw = _mapping(value, "contract")
    _exact_keys(raw, _TOP_LEVEL_KEYS, "contract")
    schema_version = _integer(raw["schema_version"], "schema_version")
    if schema_version != 1:
        raise ContractError("schema_version must be 1")
    contract_id = _string(raw["contract_id"], "contract_id")
    if contract_id != "megrez-gmac-m5":
        raise ContractError("contract_id must be megrez-gmac-m5")

    port_values = _sequence(raw["ports"], "ports")
    if len(port_values) != 2:
        raise ContractError("ports must contain exactly two candidates")
    parsed = tuple(_port(value, index) for index, value in enumerate(port_values))
    ports = tuple(sorted(parsed, key=lambda port: port.alias_index))
    if tuple(port.alias_index for port in ports) != (0, 1):
        raise ContractError("port aliases must be unique")
    if len({port.controller_id for port in ports}) != 2:
        raise ContractError("controller IDs must be unique")
    if len({port.interrupt for port in ports}) != 2:
        raise ContractError("interrupts must be unique")
    if len({port.node_path for port in ports}) != 2:
        raise ContractError("node paths must be unique")
    if len({port.mac_address for port in ports}) != 2:
        raise ContractError("MAC addresses must be unique")
    left, right = ports
    if left.mmio_start < right.mmio_start + right.mmio_size and right.mmio_start < (
        left.mmio_start + left.mmio_size
    ):
        raise ContractError("ports MMIO ranges overlap")
    for port in ports:
        reference = _REFERENCE[port.alias_index]
        for field in (
            "node_path",
            "controller_id",
            "mmio_start",
            "mmio_size",
            "interrupt",
            "hsp_sp_csr",
            "syscrg_csr",
            "delay_registers",
        ):
            if getattr(port, field) != reference[field]:
                raise ContractError(
                    f"ports.{port.alias_index}.{field} disagrees with Megrez"
                )

    return GmacContract(
        schema_version=schema_version,
        contract_id=contract_id,
        dtb_identity=_identity(raw["dtb_identity"]),
        ports=(ports[0], ports[1]),
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> Mapping[str, Any]:
    value: dict[str, Any] = {}
    for key, nested in pairs:
        if key in value:
            raise ContractError(f"duplicate JSON key: {key}")
        value[key] = nested
    return MappingProxyType(value)


def load_contract(path: Path) -> GmacContract:
    """Load canonical UTF-8 JSON and validate its complete schema."""

    try:
        text = path.read_text(encoding="utf-8")
        raw = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except UnicodeDecodeError as error:
        raise ContractError("contract must be UTF-8") from error
    except json.JSONDecodeError as error:
        raise ContractError(f"contract is not valid JSON: {error.msg}") from error
    return validate_contract(raw)


def _open_regular_dtb(path: Path) -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ContractError("DTB must be a regular file") from error
        raise
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise ContractError("DTB must be a regular file")
    return descriptor


def _identity_from_descriptor(descriptor: int) -> DtbIdentity:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    crc32 = 0
    size = 0
    while True:
        payload = os.read(descriptor, _READ_CHUNK_SIZE)
        if not payload:
            break
        digest.update(payload)
        crc32 = zlib.crc32(payload, crc32)
        size += len(payload)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return DtbIdentity(sha256=digest.hexdigest(), size=size, crc32=f"{crc32:08x}")


def read_dtb_identity(path: Path) -> DtbIdentity:
    """Hash one non-symlink regular DTB through a single open descriptor."""

    descriptor = _open_regular_dtb(path)
    try:
        return _identity_from_descriptor(descriptor)
    finally:
        os.close(descriptor)


def _run_fdtget(
    dtb_path: str,
    arguments: list[str],
    *,
    descriptor: int,
    run: Runner,
    context: str,
) -> str:
    if arguments[0] == "-p":
        argv = ["fdtget", "-p", dtb_path, *arguments[1:]]
    else:
        argv = ["fdtget", *arguments[:2], dtb_path, *arguments[2:]]
    try:
        result = run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            pass_fds=(descriptor,),
        )
    except OSError as error:
        raise ContractError(f"{context}: cannot execute fdtget: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or "property is missing"
        raise ContractError(f"{context}: {detail}")
    return result.stdout.strip()


def _property(
    dtb_path: str,
    node: str,
    name: str,
    kind: str,
    *,
    descriptor: int,
    run: Runner,
    context: str,
) -> str:
    return _run_fdtget(
        dtb_path,
        ["-t", kind, node, name],
        descriptor=descriptor,
        run=run,
        context=context,
    )


def _properties(
    dtb_path: str,
    node: str,
    *,
    descriptor: int,
    run: Runner,
    context: str,
) -> set[str]:
    output = _run_fdtget(
        dtb_path,
        ["-p", node],
        descriptor=descriptor,
        run=run,
        context=context,
    )
    return set(output.splitlines()) if output else set()


def _cells(output: str, base: int, context: str) -> tuple[int, ...]:
    try:
        return tuple(int(token, base) for token in output.split())
    except ValueError as error:
        raise ContractError(f"{context}: fdtget returned invalid cells") from error


def _hex_cells(output: str, context: str) -> tuple[int, ...]:
    return _cells(output, 16, context)


def _decimal_cells(output: str, context: str) -> tuple[int, ...]:
    return _cells(output, 10, context)


def _reg(cells: tuple[int, ...], context: str) -> tuple[int, int]:
    if len(cells) != 4:
        raise ContractError(f"{context}: reg must contain four cells")
    return ((cells[0] << 32) | cells[1], (cells[2] << 32) | cells[3])


def _one_cell(cells: tuple[int, ...], context: str) -> int:
    if len(cells) != 1:
        raise ContractError(f"{context} must contain one cell")
    return cells[0]


def _provider_resource(
    dtb_path: str,
    node: str,
    expected_phandle: int,
    *,
    descriptor: int,
    run: Runner,
    context: str,
) -> tuple[int, int]:
    phandle = _hex_cells(
        _property(
            dtb_path,
            node,
            "phandle",
            "x",
            descriptor=descriptor,
            run=run,
            context=f"{context}.phandle",
        ),
        f"{context}.phandle",
    )
    if phandle != (expected_phandle,):
        raise ContractError(f"{context}: provider phandle disagrees")
    return _reg(
        _hex_cells(
            _property(
                dtb_path,
                node,
                "reg",
                "x",
                descriptor=descriptor,
                run=run,
                context=f"{context}.reg",
            ),
            f"{context}.reg",
        ),
        f"{context}.reg",
    )


def _required_hex(
    dtb_path: str,
    node: str,
    name: str,
    *,
    descriptor: int,
    run: Runner,
    context: str,
) -> tuple[int, ...]:
    return _hex_cells(
        _property(
            dtb_path,
            node,
            name,
            "x",
            descriptor=descriptor,
            run=run,
            context=context,
        ),
        context,
    )


def _required_decimal(
    dtb_path: str,
    node: str,
    name: str,
    *,
    descriptor: int,
    run: Runner,
    context: str,
) -> tuple[int, ...]:
    return _decimal_cells(
        _property(
            dtb_path,
            node,
            name,
            "u",
            descriptor=descriptor,
            run=run,
            context=context,
        ),
        context,
    )


def _inspect_port(
    dtb_path: str,
    alias_index: int,
    firmware_mac: str | None,
    *,
    descriptor: int,
    run: Runner,
) -> dict[str, object]:
    prefix = f"ethernet{alias_index}"
    node = _property(
        dtb_path,
        "/aliases",
        prefix,
        "s",
        descriptor=descriptor,
        run=run,
        context=f"aliases.{prefix}",
    )
    properties = _properties(
        dtb_path,
        node,
        descriptor=descriptor,
        run=run,
        context=f"{prefix}.properties",
    )
    for name in (
        "status",
        "compatible",
        "reg",
        "interrupt-parent",
        "interrupts",
        "dma-noncoherent",
        "id",
        "clocks",
        "clock-names",
        "resets",
        "reset-names",
        "phy-mode",
        "eswin,hsp_sp_csr",
        "eswin,syscrg_csr",
        "eswin,dly_hsp_reg",
        "dly-param-1000m",
        "dly-param-100m",
        "dly-param-10m",
        "eswin,rgmiisel",
        "rst-gpios",
        "snps,axi-config",
    ):
        if name not in properties:
            raise ContractError(f"{prefix}.{name}: property is missing")

    reg = _reg(
        _required_hex(
            dtb_path,
            node,
            "reg",
            descriptor=descriptor,
            run=run,
            context=f"{prefix}.reg",
        ),
        f"{prefix}.reg",
    )
    hsp_raw = _required_hex(
        dtb_path,
        node,
        "eswin,hsp_sp_csr",
        descriptor=descriptor,
        run=run,
        context=f"{prefix}.eswin,hsp_sp_csr",
    )
    if len(hsp_raw) != 4:
        raise ContractError(f"{prefix}.eswin,hsp_sp_csr must contain four cells")
    hsp_reg = _provider_resource(
        dtb_path,
        _HSP_NODE,
        hsp_raw[0],
        descriptor=descriptor,
        run=run,
        context=f"{prefix}.hsp-provider",
    )
    syscrg_raw = _required_hex(
        dtb_path,
        node,
        "eswin,syscrg_csr",
        descriptor=descriptor,
        run=run,
        context=f"{prefix}.eswin,syscrg_csr",
    )
    if len(syscrg_raw) != 3:
        raise ContractError(f"{prefix}.eswin,syscrg_csr must contain three cells")
    syscrg_reg = _provider_resource(
        dtb_path,
        _SYSCRG_NODE,
        syscrg_raw[0],
        descriptor=descriptor,
        run=run,
        context=f"{prefix}.syscrg-provider",
    )

    axi_reference = _required_hex(
        dtb_path,
        node,
        "snps,axi-config",
        descriptor=descriptor,
        run=run,
        context=f"{prefix}.snps,axi-config",
    )
    if len(axi_reference) != 1:
        raise ContractError(f"{prefix}.snps,axi-config must contain one phandle")
    axi_node = f"{node}/stmmac-axi-config"
    axi_phandle = _required_hex(
        dtb_path,
        axi_node,
        "phandle",
        descriptor=descriptor,
        run=run,
        context=f"{prefix}.axi.phandle",
    )
    if axi_phandle != axi_reference:
        raise ContractError(f"{prefix}.axi provider phandle disagrees")

    if "eswin,phyaddr" in properties:
        phy_address_values = _required_decimal(
            dtb_path,
            node,
            "eswin,phyaddr",
            descriptor=descriptor,
            run=run,
            context=f"{prefix}.eswin,phyaddr",
        )
        if len(phy_address_values) != 1:
            raise ContractError(f"{prefix}.eswin,phyaddr must contain one cell")
        phy_address = phy_address_values[0]
        phy_address_source = "dt-property"
    else:
        phy_address = 0
        phy_address_source = "vendor-driver-fixed"

    mac_address: str | None = None
    mac_address_source: str | None = None
    for property_name, source in (
        ("local-mac-address", "dt-local-mac-address"),
        ("mac-address", "dt-mac-address"),
    ):
        if property_name not in properties:
            continue
        octets = _required_hex(
            dtb_path,
            node,
            property_name,
            descriptor=descriptor,
            run=run,
            context=f"{prefix}.{property_name}",
        )
        if len(octets) != 6 or any(octet > 0xFF for octet in octets):
            raise ContractError(f"{prefix}.{property_name} must contain six bytes")
        mac_address = ":".join(f"{octet:02x}" for octet in octets)
        mac_address_source = source
        break
    if mac_address is None:
        if firmware_mac is None:
            raise ContractError(f"{prefix} requires an explicit firmware MAC")
        mac_address = firmware_mac
        mac_address_source = "firmware-observed"

    def text_property(name: str) -> str:
        return _property(
            dtb_path,
            node,
            name,
            "s",
            descriptor=descriptor,
            run=run,
            context=f"{prefix}.{name}",
        )

    def decimal_property(name: str) -> int:
        values = _required_decimal(
            dtb_path,
            axi_node,
            name,
            descriptor=descriptor,
            run=run,
            context=f"{prefix}.axi.{name}",
        )
        return _one_cell(values, f"{prefix}.axi.{name}")

    return {
        "alias_index": alias_index,
        "node_path": node,
        "controller_id": _one_cell(
            _required_decimal(
                dtb_path,
                node,
                "id",
                descriptor=descriptor,
                run=run,
                context=f"{prefix}.id",
            ),
            f"{prefix}.id",
        ),
        "status": text_property("status"),
        "compatible": text_property("compatible").split(),
        "mmio_start": reg[0],
        "mmio_size": reg[1],
        "interrupt_parent": _one_cell(
            _required_hex(
                dtb_path,
                node,
                "interrupt-parent",
                descriptor=descriptor,
                run=run,
                context=f"{prefix}.interrupt-parent",
            ),
            f"{prefix}.interrupt-parent",
        ),
        "interrupt": _one_cell(
            _required_decimal(
                dtb_path,
                node,
                "interrupts",
                descriptor=descriptor,
                run=run,
                context=f"{prefix}.interrupts",
            ),
            f"{prefix}.interrupts",
        ),
        "dma_noncoherent": True,
        "clock_names": text_property("clock-names").split(),
        "clock_cells": list(
            _required_hex(
                dtb_path,
                node,
                "clocks",
                descriptor=descriptor,
                run=run,
                context=f"{prefix}.clocks",
            )
        ),
        "reset_name": text_property("reset-names"),
        "reset_cells": list(
            _required_hex(
                dtb_path,
                node,
                "resets",
                descriptor=descriptor,
                run=run,
                context=f"{prefix}.resets",
            )
        ),
        "phy_mode": text_property("phy-mode"),
        "phy_address": phy_address,
        "phy_address_source": phy_address_source,
        "mac_address": mac_address,
        "mac_address_source": mac_address_source,
        "hsp_sp_csr": [*hsp_reg, *hsp_raw[1:]],
        "syscrg_csr": [*syscrg_reg, *syscrg_raw[1:]],
        "delay_registers": list(
            _required_hex(
                dtb_path,
                node,
                "eswin,dly_hsp_reg",
                descriptor=descriptor,
                run=run,
                context=f"{prefix}.eswin,dly_hsp_reg",
            )
        ),
        "delay_1000m": list(
            _required_hex(
                dtb_path,
                node,
                "dly-param-1000m",
                descriptor=descriptor,
                run=run,
                context=f"{prefix}.dly-param-1000m",
            )
        ),
        "delay_100m": list(
            _required_hex(
                dtb_path,
                node,
                "dly-param-100m",
                descriptor=descriptor,
                run=run,
                context=f"{prefix}.dly-param-100m",
            )
        ),
        "delay_10m": list(
            _required_hex(
                dtb_path,
                node,
                "dly-param-10m",
                descriptor=descriptor,
                run=run,
                context=f"{prefix}.dly-param-10m",
            )
        ),
        "rgmii_select": list(
            _required_hex(
                dtb_path,
                node,
                "eswin,rgmiisel",
                descriptor=descriptor,
                run=run,
                context=f"{prefix}.eswin,rgmiisel",
            )
        ),
        "reset_gpio": list(
            _required_hex(
                dtb_path,
                node,
                "rst-gpios",
                descriptor=descriptor,
                run=run,
                context=f"{prefix}.rst-gpios",
            )
        ),
        "axi_blen": list(
            _required_hex(
                dtb_path,
                axi_node,
                "snps,blen",
                descriptor=descriptor,
                run=run,
                context=f"{prefix}.axi.snps,blen",
            )
        ),
        "axi_rd_osr_lmt": decimal_property("snps,rd_osr_lmt"),
        "axi_wr_osr_lmt": decimal_property("snps,wr_osr_lmt"),
        "axi_lpi_en": decimal_property("snps,lpi_en"),
    }


def inspect_dtb(
    path: Path,
    *,
    firmware_macs: tuple[str, str] | None = None,
    run: Runner = subprocess.run,
) -> GmacContract:
    """Inspect one held DTB descriptor without substituting missing properties."""

    descriptor = _open_regular_dtb(path)
    try:
        identity = _identity_from_descriptor(descriptor)
        dtb_path = f"/proc/self/fd/{descriptor}"
        macs: tuple[str | None, str | None]
        macs = firmware_macs if firmware_macs is not None else (None, None)
        raw = {
            "schema_version": 1,
            "contract_id": "megrez-gmac-m5",
            "dtb_identity": asdict(identity),
            "ports": [
                _inspect_port(
                    dtb_path,
                    alias_index,
                    macs[alias_index],
                    descriptor=descriptor,
                    run=run,
                )
                for alias_index in (0, 1)
            ],
        }
        return validate_contract(raw)
    finally:
        os.close(descriptor)


def _write_contract(path: Path, contract: GmacContract) -> None:
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ContractError("output parent must be a non-symlink directory")
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise ContractError("output must be a regular file")
    payload = (json.dumps(asdict(contract), indent=2, sort_keys=True) + "\n").encode()
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=parent, prefix=f".{path.name}.", delete=False
        ) as output:
            temporary = output.name
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
        temporary = None
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _add_identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dtb", type=Path, required=True)
    parser.add_argument("--firmware-mac0", required=True)
    parser.add_argument("--firmware-mac1", required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze")
    _add_identity_arguments(freeze)
    freeze.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify")
    _add_identity_arguments(verify)
    verify.add_argument("--contract", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    observed = inspect_dtb(
        arguments.dtb,
        firmware_macs=(arguments.firmware_mac0, arguments.firmware_mac1),
    )
    if arguments.command == "freeze":
        if (
            observed.dtb_identity.size != _EXPECTED_DTB_SIZE
            or observed.dtb_identity.crc32 != _EXPECTED_DTB_CRC32
        ):
            raise ContractError("DTB identity is not the observed Desktop M4 DTB")
        _write_contract(arguments.output, observed)
        return 0

    frozen = load_contract(arguments.contract)
    if observed.dtb_identity != frozen.dtb_identity:
        raise ContractError("DTB identity disagrees with frozen contract")
    if observed != frozen:
        raise ContractError("DTB resources disagree with frozen contract")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ContractError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
