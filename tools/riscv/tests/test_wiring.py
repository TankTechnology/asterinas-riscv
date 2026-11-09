#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Validate the wiring between the Makefile, `rt`, and the test suite."""

from __future__ import annotations

import importlib
import importlib.util
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MAKEFILE = REPO_ROOT / "Makefile"
RT_PATH = REPO_ROOT / "tools/riscv/rt"
RT_COMMAND = re.compile(r"tools/riscv/rt ([a-z0-9-]+)")
RT_GATE = re.compile(r"tools/riscv/rt gate ([a-z0-9-]+)")
REFERENCE = re.compile(r"\btools\.riscv\.tests\.[A-Za-z_][A-Za-z0-9_.]*")


def referenced_tests(makefile: str) -> set[tuple[str, str | None]]:
    """Return (module, optional class) pairs referenced by a Makefile."""

    references: set[tuple[str, str | None]] = set()
    for raw in REFERENCE.findall(makefile):
        suffix = raw.split(".")[3:]
        if not suffix:
            continue
        if suffix[-1][:1].isupper():
            references.add((".".join(raw.split(".")[:-1]), suffix[-1]))
        else:
            references.add((raw, None))
    return references


class MakefileTestTargetTests(unittest.TestCase):
    def test_extraction_distinguishes_modules_and_classes(self) -> None:
        text = """
        tools.riscv.tests.test_alpha
        tools.riscv.tests.test_beta.BetaTests
        tools.riscv.tests.sub.test_gamma.GammaTests
        """

        self.assertEqual(
            referenced_tests(text),
            {
                ("tools.riscv.tests.test_alpha", None),
                ("tools.riscv.tests.test_beta", "BetaTests"),
                ("tools.riscv.tests.sub.test_gamma", "GammaTests"),
            },
        )

    def test_every_makefile_test_reference_resolves(self) -> None:
        references = referenced_tests(MAKEFILE.read_text(encoding="utf-8"))
        self.assertGreater(len(references), 20)

        problems: list[str] = []
        for module, class_name in sorted(
            references, key=lambda reference: (reference[0], reference[1] or "")
        ):
            try:
                imported = importlib.import_module(module)
            except BaseException as error:  # noqa: BLE001 - report every bad ref
                problems.append(f"{module}: {type(error).__name__}: {error}")
                continue
            if class_name is None:
                continue
            value = getattr(imported, class_name, None)
            if not (
                isinstance(value, type)
                and issubclass(value, unittest.TestCase)
            ):
                problems.append(f"{module}.{class_name}: not a TestCase")

        self.assertEqual(problems, [])


def load_rt():
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader("riscv_rt", str(RT_PATH))
    spec = importlib.util.spec_from_loader("riscv_rt", loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WiringTests(unittest.TestCase):
    def test_rt_commands_in_makefile_exist(self) -> None:
        rt = load_rt()
        text = MAKEFILE.read_text(encoding="utf-8")
        problems: list[str] = []
        for command in sorted(set(RT_COMMAND.findall(text))):
            if command == "gate":
                continue
            if command not in rt.COMMANDS:
                problems.append(f"unknown rt command: {command}")
        for gate in sorted(set(RT_GATE.findall(text))):
            if gate not in rt.GATES:
                problems.append(f"unknown rt gate: {gate}")
        self.assertEqual(problems, [])

    def test_rt_targets_resolve(self) -> None:
        rt = load_rt()
        problems: list[str] = []
        for command, (kind, target) in sorted(rt.COMMANDS.items()):
            if kind == "module":
                try:
                    if importlib.util.find_spec(target) is None:
                        problems.append(f"{command}: no module {target}")
                except (ImportError, ModuleNotFoundError):
                    problems.append(f"{command}: no module {target}")
            else:
                if not (RT_PATH.parent / target).is_file():
                    problems.append(f"{command}: no script {target}")
        for gate, module in sorted(rt.GATES.items()):
            try:
                if importlib.util.find_spec(module) is None:
                    problems.append(f"gate {gate}: no module {module}")
            except (ImportError, ModuleNotFoundError):
                problems.append(f"gate {gate}: no module {module}")
        self.assertEqual(problems, [])

    # Debt that is not allowed to grow: these modules still assert on raw
    # Makefile text. New tests must assert behaviour or use this module.
    MAKEFILE_READERS = {
        "tools.riscv.tests.test_debian_browser_web",
        "tools.riscv.tests.test_debian_dev_overlay",
        "tools.riscv.tests.test_debian_m5_network",
        "tools.riscv.tests.test_debian_m6_browser",
        "tools.riscv.tests.test_debian_m7_baidu",
        "tools.riscv.tests.test_ltp_gate",
        "tools.riscv.tests.test_megrez_boot_stability",
        "tools.riscv.tests.test_megrez_desktop_boot",
        "tools.riscv.tests.test_megrez_firefox_daily_use",
        "tools.riscv.tests.test_qemu_uboot_booti",
        "tools.riscv.tests.test_riscv_rootfs_builder_image",
        "tools.riscv.tests.test_validate_run_kernel_log",
    }

    def test_makefile_readers_do_not_grow(self) -> None:
        pattern = re.compile(
            r'MAKEFILE\.read_text|Makefile"\)\.read_text|Makefile\'\)\.read_text'
        )
        readers: set[str] = set()
        for path in (REPO_ROOT / "tools/riscv/tests").glob("test_*.py"):
            if pattern.search(path.read_text(encoding="utf-8")):
                readers.add(f"tools.riscv.tests.{path.stem}")
        readers.discard("tools.riscv.tests.test_wiring")
        self.assertLessEqual(readers, self.MAKEFILE_READERS)
