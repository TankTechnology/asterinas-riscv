#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Hold the kernel's pinned DRM ioctl table against the real uapi headers.

`kernel/src/device/dri.rs` pins, for every DRM ioctl the driver serves, the
command number and argument size the Linux uapi gives it. Those constants are
what `dri.rs`'s own ktest asserts its declarations against -- so if a pinned
constant were wrong, the ktest would happily agree with the wrong number, and
nothing would notice.

This test closes that loop by deriving the same table from the uapi itself:
`tools/riscv/perf/drm-uapi-contract.c` includes `<drm/drm.h>` and
`<drm/virtgpu_drm.h>` and evaluates the `DRM_IOCTL_*` and `sizeof()` expressions
those headers define. The two tables are then compared.

The reason this is worth a test rather than a comment: an ioctl command number
is `direction | size | type | number` packed into 32 bits, and `ioc!` never
reads its first argument, so a declaration whose label says one ioctl and whose
number says another compiles cleanly and then answers `ENOTTY` -- which reads
as "not implemented". Four separate debugging cycles in this tree were spent on
that, most recently on `DRM_IOCTL_GEM_OPEN` declared as `DRM_IOCTL_RM_MAP`.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DRIVER = REPO_ROOT / "kernel/src/device/dri.rs"
PROBE = REPO_ROOT / "tools/riscv/perf/drm-uapi-contract.c"

# The pinned table, as `uapi_contract!` writes it:
#     GetVersion: 0xc0406400, DrmVersion, 64;
PINNED = re.compile(
    r"^\s*(?P<name>[A-Z]\w*):\s*(?P<number>0x[0-9a-fA-F]{8}),\s*"
    r"(?P<argument>\w+),\s*(?P<size>\d+);",
    re.MULTILINE,
)

# A declaration in `mod ioctl_defs`:
#     pub(super) type GetVersion = ioc!(DRM_IOCTL_VERSION, b'd', 0x00, ...);
DECLARED = re.compile(r"type\s+(?P<rust>\w+)\s*=\s*ioc!\(\s*(?P<linux>\w+)\s*,")

# One line of the probe's output:
#     GetVersion                   0xc0406400 64
PROBED = re.compile(
    r"^(?P<name>[A-Z]\w*)\s+(?P<number>0x[0-9a-fA-F]{8})\s+(?P<size>\d+)$",
    re.MULTILINE,
)


def uapi_disagreements(pinned: dict[str, tuple[int, int]],
                       probed: dict[str, tuple[int, int]]) -> list[str]:
    """Every way `pinned` disagrees with the uapi, as readable sentences.

    Kept a pure function so that it can be shown to *detect* a disagreement,
    rather than only ever being run against a table that agrees.
    """
    problems: list[str] = []

    for name in sorted(set(pinned) - set(probed)):
        problems.append(f"{name} is pinned but the uapi probe does not know it")

    for name in sorted(set(probed) - set(pinned)):
        problems.append(f"{name} is in the uapi probe but not pinned")

    for name in sorted(set(pinned) & set(probed)):
        pinned_number, pinned_size = pinned[name]
        probed_number, probed_size = probed[name]
        if pinned_number != probed_number:
            problems.append(
                f"{name} pins command number 0x{pinned_number:08x} "
                f"but the uapi defines 0x{probed_number:08x}"
            )
        if pinned_size != probed_size:
            problems.append(
                f"{name} pins argument size {pinned_size} "
                f"but the uapi declares {probed_size}"
            )

    return problems


class DrmUapiContractTests(unittest.TestCase):
    """The pinned table, the driver's declarations, and the uapi -- agreed."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._directory.cleanup)
        binary = Path(cls._directory.name) / "drm-uapi-contract"

        # -Werror because the probe is the *source* of the expected values: a
        # warning here could mean a struct silently resolved to something else.
        subprocess.run(
            ["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
             str(PROBE), "-o", str(binary)],
            check=True, capture_output=True,
        )
        completed = subprocess.run(
            [str(binary)], check=True, capture_output=True, text=True,
        )
        cls.probe_output = completed.stdout
        cls.probed = cls._parse(PROBED, cls.probe_output)

        cls.driver_source = DRIVER.read_text(encoding="utf-8")
        cls.pinned = cls._parse(PINNED, cls.driver_source)
        cls.declared = [match.group("rust")
                        for match in DECLARED.finditer(cls.driver_source)]

    @staticmethod
    def _parse(pattern: re.Pattern[str], text: str) -> dict[str, tuple[int, int]]:
        table: dict[str, tuple[int, int]] = {}
        for match in pattern.finditer(text):
            table[match.group("name")] = (
                int(match.group("number"), 16),
                int(match.group("size")),
            )
        return table

    def test_the_probe_derives_a_contract_from_the_uapi_headers(self) -> None:
        """A probe that emitted nothing would make every test below vacuous."""
        self.assertGreater(len(self.probed), 30)
        # Spelled out, because these two are the ones that get mis-encoded in
        # opposite directions and are the whole reason the table exists.
        self.assertEqual(self.probed["GetMagic"], (0x80046402, 4))
        self.assertEqual(self.probed["AuthMagic"], (0x40046411, 4))

    def test_the_pinned_table_matches_the_uapi(self) -> None:
        self.assertEqual(
            uapi_disagreements(self.pinned, self.probed), [],
            "the pinned ioctl table disagrees with the uapi headers",
        )

    def test_every_ioctl_the_driver_declares_is_pinned(self) -> None:
        """A new ioctl must arrive with a contract row, or it goes unchecked.

        This is the half that keeps the test from decaying: the comparison
        above can only check what somebody remembered to pin.
        """
        missing = sorted(set(self.declared) - set(self.pinned))
        self.assertEqual(
            missing, [],
            "these ioctls are declared in dri.rs but not pinned in the "
            "uapi contract table",
        )

    def test_every_declared_ioctl_is_known_to_the_probe(self) -> None:
        unknown = sorted(set(self.declared) - set(self.probed))
        self.assertEqual(
            unknown, [],
            "these ioctls are declared in dri.rs but the uapi probe does not "
            "cover them; add a row to tools/riscv/perf/drm-uapi-contract.c",
        )

    def test_the_pinned_arguments_are_the_ones_the_driver_uses(self) -> None:
        """A pinned size is only meaningful next to the struct it measures.

        Pinning `GetVersion` at 64 bytes says nothing if the declaration it
        refers to is the one carrying `DrmGetCap`. The data spec is the last
        element of the `ioc!` invocation, and it names that struct -- either
        bare (`NoData`) or as a type parameter (`InOutData<DrmVersion>`).
        """
        for match in PINNED.finditer(self.driver_source):
            name = match.group("name")
            argument = match.group("argument")

            declaration = re.search(
                rf"type\s+{name}\s*=\s*ioc!\((?P<body>[^;]*?)\);",
                self.driver_source,
            )
            self.assertIsNotNone(
                declaration, f"{name} is pinned but never declared"
            )
            data_spec = declaration.group("body").rsplit(",", 1)[-1].strip()
            self.assertIn(
                argument, data_spec,
                f"{name} pins {argument}, but its declaration carries "
                f"{data_spec}",
            )


class UapiDisagreementDetectionTests(unittest.TestCase):
    """The comparison has to be able to fail, or it is a rubber stamp."""

    def test_a_wrong_command_number_is_reported(self) -> None:
        problems = uapi_disagreements(
            {"GetMagic": (0xC0046402, 4)}, {"GetMagic": (0x80046402, 4)}
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("0xc0046402", problems[0])
        self.assertIn("0x80046402", problems[0])

    def test_a_wrong_argument_size_is_reported(self) -> None:
        problems = uapi_disagreements(
            {"PrimeHandleToFd": (0xC010642D, 16)},
            {"PrimeHandleToFd": (0xC00C642D, 12)},
        )
        self.assertEqual(len(problems), 2)

    def test_an_unpinned_or_unknown_ioctl_is_reported(self) -> None:
        self.assertEqual(len(uapi_disagreements({}, {"GetMagic": (0, 4)})), 1)
        self.assertEqual(len(uapi_disagreements({"GetMagic": (0, 4)}, {})), 1)

    def test_agreement_produces_nothing(self) -> None:
        table = {"GetMagic": (0x80046402, 4)}
        self.assertEqual(uapi_disagreements(table, dict(table)), [])


if __name__ == "__main__":
    unittest.main()
