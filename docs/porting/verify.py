#!/usr/bin/env python3
"""Read-only verifier for the single-assertion porting gates.

Inputs are manifest.lock and receipts/*.json.  STATUS.md is generated
atomically; no guest, QEMU process, serial device, or network command is
started by this program.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent.parent
RECEIPTS = ROOT / "receipts"
REQUIRED = {
    "E1": ("render_nodes_present", "render_ioctl_contract"),
    "E2": ("renderer_not_llvmpipe", "xorg_renderer_virgl", "fixed_pixel_hash"),
    "E3": ("official_firefox_scene",),
    "E4": ("recovery_return",),
    "E5": (
        "rockos_ssh_identity",
        "serial_owner_free",
        "uid0_nonce",
        "fallback_recovery",
    ),
}


def load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except Exception as exc:  # keep a malformed receipt visible in STATUS
        return None, f"{path}: {exc}"


def manifest_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_artifact(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPO / path


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=ROOT / "manifest.lock")
    parser.add_argument("--receipts", type=Path, default=RECEIPTS)
    parser.add_argument("--output", type=Path, default=ROOT / "STATUS.md")
    args = parser.parse_args()

    # Normalize CLI paths before storing them in receipts.  The default paths
    # are absolute, but callers commonly pass ``docs/porting/receipts``
    # relative to the repository.  Keeping a single absolute representation
    # makes evidence rendering deterministic and avoids ``relative_to``
    # failures when generating STATUS.md.
    manifest_path = args.manifest.resolve()
    receipts_path = args.receipts.resolve()
    output_path = args.output.resolve()

    manifest, manifest_error = load(manifest_path)
    errors = [manifest_error] if manifest_error else []
    expected_manifest_sha = manifest_sha256(manifest_path) if manifest is not None else ""
    receipts: list[dict] = []
    if receipts_path.exists():
        for path in sorted(receipts_path.glob("*.json")):
            item, error = load(path)
            if error:
                errors.append(error)
                continue
            item["_path"] = path
            receipts.append(item)
            if item.get("schema") != 1:
                errors.append(f"{path}: schema must be 1")
            if item.get("manifest_sha256") != expected_manifest_sha:
                errors.append(f"{path}: manifest_sha256 does not match manifest.lock")
            assertions = item.get("assertions")
            if not isinstance(assertions, list) or len(assertions) != 1:
                errors.append(f"{path}: exactly one assertion is required")
            artifacts = item.get("artifacts", {})
            evidence = artifacts.get("evidence") if isinstance(artifacts, dict) else None
            if evidence:
                evidence_path = resolve_artifact(evidence)
                if not evidence_path.exists():
                    errors.append(f"{path}: missing evidence artifact {evidence}")
                else:
                    expected_evidence_sha = artifacts.get("evidence_sha256")
                    if expected_evidence_sha:
                        actual_evidence_sha = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
                        if actual_evidence_sha != expected_evidence_sha:
                            errors.append(f"{path}: evidence sha256 mismatch for {evidence}")
            if isinstance(artifacts, dict):
                for key, value in artifacts.items():
                    if not key.endswith("_evidence") or not isinstance(value, str):
                        continue
                    comparison_path = resolve_artifact(value)
                    if not comparison_path.exists():
                        errors.append(f"{path}: missing comparison evidence {value}")
                    comparison_sha = artifacts.get(f"{key}_sha256")
                    if comparison_sha and comparison_path.exists():
                        actual_comparison_sha = hashlib.sha256(comparison_path.read_bytes()).hexdigest()
                        if actual_comparison_sha != comparison_sha:
                            errors.append(f"{path}: comparison evidence sha256 mismatch for {value}")
            input_set = item.get("input_set")
            if input_set and (manifest is None or input_set not in manifest.get("input_sets", {})):
                errors.append(f"{path}: unknown input_set {input_set}")

    by_assertion: dict[str, list[dict]] = {}
    for receipt in receipts:
        assertions = receipt.get("assertions") or []
        if assertions:
            name = assertions[0].get("name")
            if name:
                by_assertion.setdefault(name, []).append(receipt)

    rows = []
    for claim, required in REQUIRED.items():
        missing = []
        failed = []
        passed = []
        for name in required:
            candidates = by_assertion.get(name, [])
            passing = [r for r in candidates if r.get("status") == "pass" and r["assertions"][0].get("ok") is True]
            if passing:
                passed.append(name)
            elif candidates:
                failed.append(name)
            else:
                missing.append(name)
        blocked = [name for name in required if any(r.get("status") == "blocked" for r in by_assertion.get(name, []))]
        failed = [name for name in failed if name not in blocked]
        if failed or blocked:
            status = "❌"
        elif not missing:
            status = "✅"
        else:
            status = "⏳"
        evidence = []
        for name in required:
            for receipt in by_assertion.get(name, []):
                evidence.append(str(receipt["_path"].relative_to(REPO)))
        details = missing + failed + [f"{name} (blocked)" for name in blocked]
        rows.append((claim, status, ", ".join(required), ", ".join(evidence) or "—", ", ".join(details) or "—"))

    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        "# Porting gate status",
        "",
        "Generated by `docs/porting/verify.py`; do not edit this file manually.",
        f"Generated UTC: `{generated}`",
        f"Manifest SHA-256: `{expected_manifest_sha or 'unavailable'}`",
        "",
        "| Claim | Status | Required assertions | Evidence receipts | Missing/failed |",
        "|---|---|---|---|---|",
    ]
    lines.extend(f"| {claim} | {status} | `{required}` | {evidence} | `{missing}` |" for claim, status, required, evidence, missing in rows)
    lines.extend(["", "## Registered Task 1–5 sources", "", "| Task | Status | Source | Note |", "|---|---|---|---|"])
    for task in (manifest or {}).get("task_receipts", []):
        note = task.get("note", "")
        extras = task.get("additional_sources", [])
        if extras:
            note += " Additional sources: " + ", ".join(f"`{item}`" for item in extras)
        lines.append(f"| {task.get('task','?')} | {task.get('status','?')} | `{task.get('source','?')}` | {note} |")
    supplemental = [
        receipt
        for receipt in receipts
        if receipt.get("claim") in REQUIRED
        and (receipt.get("assertions") or [{}])[0].get("name") not in REQUIRED[receipt.get("claim")]
    ]
    if supplemental:
        lines.extend(
            [
                "",
                "## Supplemental diagnostic receipts",
                "",
                "These receipts expose intermediate boundaries and never satisfy a required claim assertion.",
                "",
                "| Claim | Status | Assertion | Receipt |",
                "|---|---|---|---|",
            ]
        )
        for receipt in supplemental:
            assertion = (receipt.get("assertions") or [{}])[0].get("name", "?")
            lines.append(
                f"| {receipt.get('claim','?')} | {receipt.get('status','?')} | `{assertion}` | "
                f"`{receipt['_path'].relative_to(REPO)}` |"
            )
    lines.extend(["", "## Immediate blockers", "", "| ID | Status | Boundary | Evidence |", "|---|---|---|---|"])
    for blocker in (manifest or {}).get("immediate_blockers", []):
        lines.append(
            f"| {blocker.get('id','?')} | {blocker.get('status','?')} | "
            f"{blocker.get('description','')} | `{blocker.get('evidence','?')}` |"
        )
    lines.extend(["", "## Verification diagnostics", ""])
    if errors:
        lines.extend(f"- ❌ {error}" for error in errors)
    else:
        lines.append("- ✅ Receipt schema, one-assertion rule, manifest binding and evidence paths passed.")
    lines.extend(["", "Input hashes are provenance data; a null value is intentional when the historical file is no longer retained.", ""])
    atomic_write(output_path, "\n".join(lines))

    # Exit zero only when every claim is complete and there are no schema errors.
    return 0 if not errors and all(status == "✅" for _, status, *_ in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
