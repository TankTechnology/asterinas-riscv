# SPDX-License-Identifier: MPL-2.0

"""Host-side tests for the RISC-V tooling."""

import sys
from pathlib import Path

_TOOLS = Path(__file__).resolve().parents[1]
# Tests import tooling modules by their top-level name. Put every directory
# that owns such a module on the path, so a package import (the canonical
# runner and `python3 -m unittest tools.riscv.tests.X`) resolves them the same
# way the Makefile's PYTHONPATH does.
for _tooling_dir in (
    _TOOLS,
    _TOOLS / "qemu",
    _TOOLS / "megrez" / "board",
    _TOOLS / "megrez" / "desktop",
    _TOOLS / "megrez" / "image",
    _TOOLS / "megrez" / "network",
):
    if str(_tooling_dir) not in sys.path:
        sys.path.insert(0, str(_tooling_dir))
del _tooling_dir, _TOOLS
