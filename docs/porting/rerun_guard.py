#!/usr/bin/env python3
"""Fail-closed guard for assertions frozen by GATES-REFACTOR.

This command is deliberately read-only. Gate launchers can call it before
starting QEMU, Firefox, or a board session. A blocked assertion requires an
external input change and cannot be retried by accident.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--claim", required=True)
    parser.add_argument("--assertion", required=True)
    parser.add_argument("--receipts", type=Path, default=ROOT / "receipts")
    args = parser.parse_args()

    blocked = []
    for path in sorted(args.receipts.glob("*.json")):
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if receipt.get("claim") != args.claim or receipt.get("status") != "blocked":
            continue
        assertions = receipt.get("assertions") or []
        if len(assertions) == 1 and assertions[0].get("name") == args.assertion:
            blocked.append(path)

    if blocked:
        print(
            f"RERUN_FORBIDDEN claim={args.claim} assertion={args.assertion} "
            f"reason=blocked-receipt evidence={','.join(str(p) for p in blocked)}"
        )
        return 2
    print(f"RERUN_ALLOWED claim={args.claim} assertion={args.assertion}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
