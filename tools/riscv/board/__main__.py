# SPDX-License-Identifier: MPL-2.0

"""One entry point for the board tools.

Run as `python3 tools/riscv/board <tool> [args...]`. Each tool keeps its own
command line; this dispatcher only fixes `sys.argv[0]` so the tool's help
text still names its real script.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

TOOLS = {
    "console": "megrez_console.py",
    "flash": "megrez_flash_rootfs.py",
    "portal": "megrez_portal_login.py",
    "prepare": "prepare_board_image.py",
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] in {"-h", "--help"}:
        print("board tools: " + ", ".join(sorted(TOOLS)))
        print(f"usage: {Path(sys.argv[0]).name} <{'|'.join(TOOLS)}> [args...]")
        return 0 if argv else 2
    name = argv[0]
    if name not in TOOLS:
        print(f"unknown tool: {name}", file=sys.stderr)
        print("available: " + ", ".join(sorted(TOOLS)), file=sys.stderr)
        return 2

    script = Path(__file__).resolve().parent / TOOLS[name]
    sys.argv = [str(script), *argv[1:]]
    runpy.run_path(str(script), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
