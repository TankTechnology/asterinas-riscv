#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Summarize the bounded browser run from its unmodified guest archive."""

import argparse
import collections
import json
import re
import subprocess
import tarfile
from pathlib import Path


PREFIX = "asterinas-wave3/"
WARNINGS = {
    "scheduler_switch_contention": "Switching to a task already running in the foreground",
    "scm_rights_resource_warning": "UNIX sockets in SCM_RIGHTS messages can leak kernel resource",
    "unsupported_wall_wait": "unsupported wait options are found: WALL",
    "journal_missed_kernel_messages": "Missed ",
    "kmsg_overrun": "/dev/kmsg buffer overrun",
}


def read_archive(archive: tarfile.TarFile, name: str) -> str:
    member = archive.getmember(PREFIX + name)
    stream = archive.extractfile(member)
    if stream is None:
        raise ValueError(f"missing regular file: {name}")
    return stream.read().decode("utf-8", errors="replace")


def phase_for(timestamp: float, events: list[dict]) -> str:
    for start, end in zip(events[::2], events[1::2]):
        if start["uptime"] <= timestamp < end["uptime"]:
            return start["name"]
    return "outside_workload"


def summarize_phase(phase: dict) -> dict:
    measurement = phase["measurement"]
    ticks = measurement["ticks_per_second"]
    processes = measurement["processes"]
    threads = measurement["threads"]

    def process_cpu(predicate):
        selected = [item for item in processes if predicate(item)]
        return {
            "user_seconds": round(sum(item["user"] for item in selected) / ticks, 2),
            "system_seconds": round(sum(item["system"] for item in selected) / ticks, 2),
        }

    def thread_cpu(names):
        selected = [item for item in threads if item["comm"] in names]
        return round(sum(item["user"] + item["system"] for item in selected) / ticks, 2)

    output = phase["output"]
    return {
        "name": phase["name"],
        "wall_seconds": measurement["wall_seconds"],
        "browser_cpu": process_cpu(lambda item: item["role"].startswith("firefox")),
        "xorg_cpu": process_cpu(lambda item: item["role"] == "xorg"),
        "renderer_and_swcomposite_cpu_seconds": thread_cpu({"Renderer", "SwComposite"}),
        "rdd_cpu": process_cpu(lambda item: item["comm"] == "RDD Process"),
        "score": output.get("score") if output else None,
        "dropped_frames": output.get("droppedFrames") if output else None,
    }


def symbolize_samples(rows: list[dict], maps: str, debug_file: Path | None) -> dict:
    mappings = []
    for line in maps.splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) < 6:
            continue
        start, end = (int(value, 16) for value in fields[0].split("-"))
        mappings.append((start, end, int(fields[2], 16), fields[5]))

    locations = collections.Counter()
    libxul_addresses = []
    for row in rows:
        pc = row["pc"]
        mapping = next((item for item in mappings if item[0] <= pc < item[1]), None)
        if mapping is None:
            locations["unmapped"] += 1
            continue
        start, _, offset, library = mapping
        if library.endswith("/libxul.so"):
            libxul_addresses.append(hex(pc - start + offset))
        else:
            locations[library] += 1

    if debug_file is None:
        locations["libxul.so (unsymbolized)"] += len(libxul_addresses)
    elif libxul_addresses:
        result = subprocess.run(
            ["riscv64-linux-gnu-addr2line", "-f", "-C", "-e", str(debug_file), *libxul_addresses],
            check=True, capture_output=True, text=True,
        )
        lines = result.stdout.splitlines()
        for index in range(0, len(lines), 2):
            locations[lines[index].split("(")[0].strip()] += 1
    return {"total": len(rows), "locations": dict(locations.most_common())}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--libxul-debug", type=Path)
    args = parser.parse_args()
    with tarfile.open(args.archive, "r:gz") as archive:
        result = json.loads(read_archive(archive, "result.json"))
        events = [json.loads(line) for line in read_archive(archive, "phase-events.jsonl").splitlines()]
        journal = read_archive(archive, "journal.log")
        rows = [json.loads(line) for line in read_archive(archive, "active-pcs.jsonl").splitlines()]
        sample_rows = [row for row in rows if row["kind"] == "sample"]
        maps = read_archive(archive, "active-pcs.maps")
        exits = {name: read_archive(archive, name + ".exit").strip()
                 for name in ("workload", "journal", "dmesg", "units")}

    warnings = {name: collections.Counter() for name in WARNINGS}
    for line in journal.splitlines():
        match = re.match(r"\[\s*([0-9.]+)\]", line)
        if match is None:
            continue
        phase = phase_for(float(match.group(1)), events)
        for name, text in WARNINGS.items():
            if text in line:
                warnings[name][phase] += 1

    summary = {
        "boot_id": result["boot_id"],
        "firefox_version": result["firefox_version"],
        "workload_error": result["error"],
        "collector_exit_codes": exits,
        "phases": [summarize_phase(phase) for phase in result["phases"]],
        "journal_warning_counts_by_phase": {name: dict(counts) for name, counts in warnings.items()},
        "pc_samples": symbolize_samples(sample_rows, maps, args.libxul_debug),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
