#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Validate that Makefile test references resolve to real host tests."""

from __future__ import annotations

import importlib
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MAKEFILE = REPO_ROOT / "Makefile"
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
