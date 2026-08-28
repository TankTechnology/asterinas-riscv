"""Strict tests for the physical Megrez dual-GMAC device-tree contract."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import zlib

from tools.riscv import megrez_gmac_contract as gmac


def _port(alias_index: int) -> dict[str, object]:
    if alias_index == 0:
        return {
            "alias_index": 0,
            "node_path": "/soc/ethernet@50400000",
            "controller_id": 0,
            "status": "okay",
            "compatible": ["eswin,win2030-qos-eth"],
            "mmio_start": 0x5040_0000,
            "mmio_size": 0x1_0000,
            "interrupt_parent": 16,
            "interrupt": 61,
            "dma_noncoherent": True,
            "clock_names": ["app", "stmmaceth", "tx"],
            "clock_cells": [3, 0x226, 3, 0x227, 3, 0x228],
            "reset_name": "ethrst",
            "reset_cells": [0x14, 7, 0x0400_0000],
            "phy_mode": "rgmii-txid",
            "phy_address": 0,
            "phy_address_source": "vendor-driver-fixed",
            "mac_address": "00:48:54:71:00:47",
            "mac_address_source": "firmware-observed",
            "hsp_sp_csr": [0x5044_0000, 0x2000, 0x1030, 0x100, 0x108],
            "syscrg_csr": [0x5182_8000, 0x8_0000, 0x148, 0x14C],
            "delay_registers": [0x114, 0x118, 0x11C],
            "delay_1000m": [0x23232323, 0x800C8023, 0x0C0C0C0C],
            "delay_100m": [0x50505050, 0x803F8050, 0x3F3F3F3F],
            "delay_10m": [0, 0, 0],
            "rgmii_select": [0x1A, 0x290, 0x3],
            "reset_gpio": [0x19, 30, 1],
            "axi_blen": [0, 0, 0, 0, 16, 8, 4],
            "axi_rd_osr_lmt": 2,
            "axi_wr_osr_lmt": 2,
            "axi_lpi_en": 0,
        }
    return {
        "alias_index": 1,
        "node_path": "/soc/ethernet@50410000",
        "controller_id": 1,
        "status": "okay",
        "compatible": ["eswin,win2030-qos-eth"],
        "mmio_start": 0x5041_0000,
        "mmio_size": 0x1_0000,
        "interrupt_parent": 16,
        "interrupt": 70,
        "dma_noncoherent": True,
        "clock_names": ["app", "stmmaceth", "tx"],
        "clock_cells": [3, 0x226, 3, 0x227, 3, 0x229],
        "reset_name": "ethrst",
        "reset_cells": [0x14, 7, 0x0200_0000],
        "phy_mode": "rgmii-txid",
        "phy_address": 0,
        "phy_address_source": "vendor-driver-fixed",
        "mac_address": "00:48:54:71:00:48",
        "mac_address_source": "firmware-observed",
        "hsp_sp_csr": [0x5044_0000, 0x2000, 0x1034, 0x200, 0x208],
        "syscrg_csr": [0x5182_8000, 0x8_0000, 0x148, 0x14C],
        "delay_registers": [0x214, 0x218, 0x21C],
        "delay_1000m": [0x25252525, 0x80268025, 0x26262626],
        "delay_100m": [0x48484848, 0x80588048, 0x58585858],
        "delay_10m": [0, 0, 0],
        "rgmii_select": [0x1A, 0x294, 0x3],
        "reset_gpio": [0x1D, 16, 1],
        "axi_blen": [0, 0, 0, 0, 16, 8, 4],
        "axi_rd_osr_lmt": 2,
        "axi_wr_osr_lmt": 2,
        "axi_lpi_en": 0,
    }


def _contract() -> dict[str, object]:
    return {
        "schema_version": 1,
        "contract_id": "megrez-gmac-m5",
        "dtb_identity": {
            "sha256": "ab" * 32,
            "size": 154800,
            "crc32": "4afcb20e",
        },
        "ports": [_port(0), _port(1)],
    }


class _FakeFdtget:
    def __init__(
        self,
        *,
        omit: tuple[str, str] | None = None,
        overrides: dict[tuple[str, str, str], str] | None = None,
        extra_properties: dict[str, set[str]] | None = None,
    ):
        self.calls: list[tuple[str, ...]] = []
        self.omit = omit
        self.overrides = overrides or {}
        self.extra_properties = extra_properties or {}

    def __call__(self, argv: list[str], **kwargs: object):
        del kwargs
        command = tuple(argv)
        self.calls.append(command)
        if command[1] == "-p":
            node = command[3]
            properties = {
                "/soc/ethernet@50400000": _port_properties(0),
                "/soc/ethernet@50410000": _port_properties(1),
            }.get(node, set())
            properties = properties | self.extra_properties.get(node, set())
            return subprocess.CompletedProcess(
                command, 0, stdout="\n".join(sorted(properties)) + "\n", stderr=""
            )

        kind, node, prop = command[2], command[4], command[5]
        if self.omit == (node, prop):
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="missing")
        try:
            value = self.overrides.get(
                (kind, node, prop), _fdt_values()[(kind, node, prop)]
            )
        except KeyError:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="missing")
        return subprocess.CompletedProcess(command, 0, stdout=value + "\n", stderr="")


def _port_properties(alias_index: int) -> set[str]:
    properties = set(_port(alias_index)) - {
        "alias_index",
        "node_path",
        "mmio_start",
        "mmio_size",
        "dma_noncoherent",
        "clock_cells",
        "reset_name",
        "reset_cells",
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
    properties -= {"controller_id", "interrupt", "clock_names", "phy_mode"}
    properties |= {
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
    }
    return properties


def _fdt_values() -> dict[tuple[str, str, str], str]:
    values: dict[tuple[str, str, str], str] = {
        ("s", "/aliases", "ethernet0"): "/soc/ethernet@50400000",
        ("s", "/aliases", "ethernet1"): "/soc/ethernet@50410000",
        ("x", "/soc/hsp_sp_top_csr@0x50440000", "phandle"): "16",
        ("x", "/soc/hsp_sp_top_csr@0x50440000", "reg"): "0 50440000 0 2000",
        ("x", "/soc/sys-crg@51828000", "phandle"): "12",
        ("x", "/soc/sys-crg@51828000", "reg"): "0 51828000 0 80000",
    }
    for alias_index in (0, 1):
        port = _port(alias_index)
        node = str(port["node_path"])
        axi_node = f"{node}/stmmac-axi-config"
        axi_phandle = (0x17, 0x1B)[alias_index]
        hsp = list(port["hsp_sp_csr"])
        syscrg = list(port["syscrg_csr"])
        values.update(
            {
                ("s", node, "status"): "okay",
                ("s", node, "compatible"): "eswin,win2030-qos-eth",
                ("x", node, "reg"): (
                    f"0 {int(port['mmio_start']):x} 0 {int(port['mmio_size']):x}"
                ),
                ("x", node, "interrupt-parent"): "10",
                ("u", node, "interrupts"): str(port["interrupt"]),
                ("u", node, "id"): str(port["controller_id"]),
                ("x", node, "clocks"): " ".join(
                    f"{cell:x}" for cell in port["clock_cells"]
                ),
                ("s", node, "clock-names"): "app stmmaceth tx",
                ("x", node, "resets"): " ".join(
                    f"{cell:x}" for cell in port["reset_cells"]
                ),
                ("s", node, "reset-names"): "ethrst",
                ("s", node, "phy-mode"): "rgmii-txid",
                ("x", node, "eswin,hsp_sp_csr"): " ".join(
                    ["16", *(f"{cell:x}" for cell in hsp[2:])]
                ),
                ("x", node, "eswin,syscrg_csr"): " ".join(
                    ["12", *(f"{cell:x}" for cell in syscrg[2:])]
                ),
                ("x", node, "eswin,dly_hsp_reg"): " ".join(
                    f"{cell:x}" for cell in port["delay_registers"]
                ),
                ("x", node, "dly-param-1000m"): " ".join(
                    f"{cell:x}" for cell in port["delay_1000m"]
                ),
                ("x", node, "dly-param-100m"): " ".join(
                    f"{cell:x}" for cell in port["delay_100m"]
                ),
                ("x", node, "dly-param-10m"): "0 0 0",
                ("x", node, "eswin,rgmiisel"): " ".join(
                    f"{cell:x}" for cell in port["rgmii_select"]
                ),
                ("x", node, "rst-gpios"): " ".join(
                    f"{cell:x}" for cell in port["reset_gpio"]
                ),
                ("x", node, "snps,axi-config"): f"{axi_phandle:x}",
                ("x", axi_node, "phandle"): f"{axi_phandle:x}",
                ("x", axi_node, "snps,blen"): "0 0 0 0 10 8 4",
                ("u", axi_node, "snps,rd_osr_lmt"): "2",
                ("u", axi_node, "snps,wr_osr_lmt"): "2",
                ("u", axi_node, "snps,lpi_en"): "0",
            }
        )
    return values


class MegrezGmacContractTests(unittest.TestCase):
    def test_accepts_and_freezes_two_complete_ports(self):
        contract = gmac.validate_contract(_contract())

        self.assertEqual(contract.schema_version, 1)
        self.assertEqual(contract.contract_id, "megrez-gmac-m5")
        self.assertEqual(
            tuple(port.mmio_start for port in contract.ports),
            (0x5040_0000, 0x5041_0000),
        )
        self.assertEqual(tuple(port.interrupt for port in contract.ports), (61, 70))
        with self.assertRaises(FrozenInstanceError):
            contract.ports[0].interrupt = 70

    def test_rejects_missing_unknown_and_wrongly_typed_fields(self):
        cases: list[tuple[str, dict[str, object], str]] = []

        missing = _contract()
        del missing["ports"][0]["interrupt"]
        cases.append(("missing", missing, "ports.0.*interrupt"))

        unknown = _contract()
        unknown["ports"][0]["fallback_mmio"] = 0x5040_0000
        cases.append(("unknown", unknown, "ports.0.*unknown"))

        boolean_number = _contract()
        boolean_number["ports"][0]["interrupt"] = True
        cases.append(("boolean", boolean_number, "ports.0.interrupt.*integer"))

        for name, raw, pattern in cases:
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(gmac.ContractError, pattern),
            ):
                gmac.validate_contract(raw)

    def test_rejects_resource_aliases_and_nonunicast_mac_addresses(self):
        cases: list[tuple[str, dict[str, object], str]] = []

        overlap = _contract()
        overlap["ports"][1]["mmio_start"] = 0x5040_8000
        cases.append(("overlap", overlap, "ports.*MMIO"))

        duplicate_irq = _contract()
        duplicate_irq["ports"][1]["interrupt"] = 61
        cases.append(("irq", duplicate_irq, "interrupts.*unique"))

        multicast_mac = _contract()
        multicast_mac["ports"][1]["mac_address"] = "01:00:5e:00:00:01"
        cases.append(("mac", multicast_mac, "mac_address.*unicast"))

        for name, raw, pattern in cases:
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(gmac.ContractError, pattern),
            ):
                gmac.validate_contract(raw)

    def test_rejects_reference_resource_drift_without_fallbacks(self):
        mutations = {
            "mmio": ("mmio_start", 0xDEAD_0000),
            "interrupt": ("interrupt", 62),
            "phy-mode": ("phy_mode", "rgmii"),
            "hsp": ("hsp_sp_csr", [0x5044_0000, 0x2000, 0x1030, 0x104, 0x108]),
            "delay": ("delay_registers", [0x114, 0x118, 0x120]),
        }
        for name, (field, value) in mutations.items():
            raw = _contract()
            raw["ports"][0][field] = value
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(gmac.ContractError, f"ports.0.{field}"),
            ):
                gmac.validate_contract(raw)

    def test_rejects_incomplete_platform_clock_reset_and_axi_contracts(self):
        mutations = {
            "clocks": ("clock_cells", [3, 270]),
            "resets": ("reset_cells", [20, 7]),
            "rgmii": ("rgmii_select", [0x290, 3]),
            "axi-burst": ("axi_blen", [0, 0, 0, 16, 8, 4]),
            "axi-limit": ("axi_rd_osr_lmt", True),
        }
        for name, (field, value) in mutations.items():
            raw = _contract()
            raw["ports"][0][field] = value
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(gmac.ContractError, f"ports.0.{field}"),
            ):
                gmac.validate_contract(raw)

    def test_rejects_unrecorded_firmware_and_vendor_sources(self):
        mutations = {
            "phy": ("phy_address_source", "dt-fallback"),
            "mac": ("mac_address_source", "generated"),
        }
        for name, (field, value) in mutations.items():
            raw = _contract()
            raw["ports"][0][field] = value
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(gmac.ContractError, f"ports.0.{field}"),
            ):
                gmac.validate_contract(raw)

    def test_load_rejects_duplicate_json_keys_at_every_depth(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contract.json"
            path.write_text(
                '{"schema_version":1,"schema_version":1,"contract_id":"x",'
                '"dtb_identity":{},"ports":[]}'
            )
            with self.assertRaisesRegex(gmac.ContractError, "duplicate JSON key"):
                gmac.load_contract(path)

            raw = _contract()
            encoded = json.dumps(raw, separators=(",", ":"))
            encoded = encoded.replace('"size":154800', '"size":154800,"size":154800')
            path.write_text(encoded)
            with self.assertRaisesRegex(gmac.ContractError, "duplicate JSON key"):
                gmac.load_contract(path)

    def test_dtb_identity_uses_one_open_regular_file(self):
        payload = b"frozen-megrez-dtb"
        expected = gmac.DtbIdentity(
            sha256=hashlib.sha256(payload).hexdigest(),
            size=len(payload),
            crc32=f"{zlib.crc32(payload):08x}",
        )
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            path = directory_path / "board.dtb"
            path.write_bytes(payload)
            self.assertEqual(gmac.read_dtb_identity(path), expected)

            link = directory_path / "link.dtb"
            link.symlink_to(path.name)
            with self.assertRaisesRegex(gmac.ContractError, "regular file"):
                gmac.read_dtb_identity(link)

    def test_inspect_dtb_queries_and_normalizes_every_required_property(self):
        runner = _FakeFdtget()
        with tempfile.TemporaryDirectory() as directory:
            dtb = Path(directory) / "board.dtb"
            dtb.write_bytes(b"exact-dtb-fixture")
            contract = gmac.inspect_dtb(
                dtb,
                firmware_macs=("00:48:54:71:00:47", "00:48:54:71:00:48"),
                run=runner,
            )

        self.assertEqual(contract.ports[0].clock_cells, (3, 0x226, 3, 0x227, 3, 0x228))
        self.assertEqual(contract.ports[1].reset_cells, (0x14, 7, 0x0200_0000))
        self.assertEqual(contract.ports[0].hsp_sp_csr[:2], (0x50440000, 0x2000))
        self.assertEqual(contract.ports[1].axi_blen, (0, 0, 0, 0, 16, 8, 4))
        self.assertEqual(contract.ports[0].mac_address_source, "firmware-observed")
        self.assertTrue(
            any(
                command[:3] == ("fdtget", "-t", "x")
                and command[3].startswith("/proc/self/fd/")
                and command[4:] == ("/soc/ethernet@50400000", "clocks")
                for command in runner.calls
            )
        )

    def test_inspect_dtb_rejects_missing_property_without_reference_fallback(self):
        node = "/soc/ethernet@50400000"
        runner = _FakeFdtget(omit=(node, "clocks"))
        with tempfile.TemporaryDirectory() as directory:
            dtb = Path(directory) / "board.dtb"
            dtb.write_bytes(b"exact-dtb-fixture")
            with self.assertRaisesRegex(gmac.ContractError, "ethernet0.clocks"):
                gmac.inspect_dtb(
                    dtb,
                    firmware_macs=(
                        "00:48:54:71:00:47",
                        "00:48:54:71:00:48",
                    ),
                    run=runner,
                )

    def test_inspect_dtb_rejects_unsupported_dma_translation(self):
        for alias_index, property_name in (
            (0, "iommus"),
            (1, "dma-ranges"),
        ):
            node = f"/soc/ethernet@504{alias_index}0000"
            runner = _FakeFdtget(extra_properties={node: {property_name}})
            with self.subTest(alias_index=alias_index, property_name=property_name):
                with tempfile.TemporaryDirectory() as directory:
                    dtb = Path(directory) / "board.dtb"
                    dtb.write_bytes(b"exact-dtb-fixture")
                    with self.assertRaisesRegex(
                        gmac.ContractError,
                        rf"ethernet{alias_index}\.{property_name}: unsupported DMA translation",
                    ):
                        gmac.inspect_dtb(
                            dtb,
                            firmware_macs=(
                                "00:48:54:71:00:47",
                                "00:48:54:71:00:48",
                            ),
                            run=runner,
                        )

    def test_inspect_dtb_rejects_multi_cell_scalar_properties(self):
        node = "/soc/ethernet@50400000"
        runner = _FakeFdtget(overrides={("u", node, "id"): "0 1"})
        with tempfile.TemporaryDirectory() as directory:
            dtb = Path(directory) / "board.dtb"
            dtb.write_bytes(b"exact-dtb-fixture")
            with self.assertRaisesRegex(gmac.ContractError, "ethernet0.id.*one cell"):
                gmac.inspect_dtb(
                    dtb,
                    firmware_macs=(
                        "00:48:54:71:00:47",
                        "00:48:54:71:00:48",
                    ),
                    run=runner,
                )

    def test_inspect_dtb_requires_explicit_firmware_mac_when_dtb_has_none(self):
        with tempfile.TemporaryDirectory() as directory:
            dtb = Path(directory) / "board.dtb"
            dtb.write_bytes(b"exact-dtb-fixture")
            with self.assertRaisesRegex(gmac.ContractError, "firmware MAC"):
                gmac.inspect_dtb(dtb, run=_FakeFdtget())

    def test_freeze_and_verify_use_canonical_contract_and_exact_identity(self):
        contract = gmac.validate_contract(_contract())
        drifted = _contract()
        drifted["dtb_identity"]["sha256"] = "cd" * 32
        drifted_contract = gmac.validate_contract(drifted)
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            output = directory_path / "contract.json"
            dtb = directory_path / "board.dtb"
            dtb.write_bytes(b"not-read-because-inspection-is-patched")

            with mock.patch.object(gmac, "inspect_dtb", return_value=contract):
                self.assertEqual(
                    gmac.main(
                        [
                            "freeze",
                            "--dtb",
                            str(dtb),
                            "--output",
                            str(output),
                            "--firmware-mac0",
                            "00:48:54:71:00:47",
                            "--firmware-mac1",
                            "00:48:54:71:00:48",
                        ]
                    ),
                    0,
                )
            self.assertEqual(output.read_bytes()[-1:], b"\n")
            self.assertEqual(gmac.load_contract(output), contract)

            with mock.patch.object(gmac, "inspect_dtb", return_value=drifted_contract):
                with self.assertRaisesRegex(gmac.ContractError, "identity"):
                    gmac.main(
                        [
                            "verify",
                            "--dtb",
                            str(dtb),
                            "--contract",
                            str(output),
                            "--firmware-mac0",
                            "00:48:54:71:00:47",
                            "--firmware-mac1",
                            "00:48:54:71:00:48",
                        ]
                    )


if __name__ == "__main__":
    unittest.main()
