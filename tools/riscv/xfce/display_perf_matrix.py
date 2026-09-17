#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Measure fbdev, software DRM, and virgl with repeatable Xfce boots."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import signal
import statistics
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence


REPO = Path(__file__).resolve().parents[3]
XFCE_DIR = Path(__file__).resolve().parent
BUILD_SCRIPT = XFCE_DIR / "build_xfce_drm.py"
BOOT_SCRIPT = XFCE_DIR / "boot_xfce_drm.py"
BUILD_ROOT_DISK = REPO / "target/xfce-drm/root.ext2"
DEFAULT_KERNEL = REPO / "target/osdk/aster-kernel-osdk-bin.Image"
DEFAULT_RUNTIME = REPO / "target/m19/rootfs"
EVIDENCE_ROOT = REPO / "target/xfce-display-perf"
BASELINE_DIR = EVIDENCE_ROOT / "baselines"
BASELINE_MANIFEST = BASELINE_DIR / "manifest.json"
DISPLAY_PATHS = ("virgl", "software-drm", "fbdev")
PATH_FLAGS = {
    "virgl": (),
    "software-drm": ("--software-display",),
    "fbdev": ("--fbdev-display",),
}
EXPECTED_RENDERERS = {
    "virgl": "virgl",
    "software-drm": "llvmpipe",
    "fbdev": "llvmpipe",
}
BENCHMARK_SOURCE_FILES = (
    BUILD_SCRIPT,
    BOOT_SCRIPT,
    XFCE_DIR / "gl_renderer_bench.c",
    XFCE_DIR / "xorg-drm.conf",
    XFCE_DIR / "xorg-drm-software.conf",
    XFCE_DIR / "xorg-fbdev.conf",
    XFCE_DIR / "units/gl-renderer-bench.service",
    XFCE_DIR / "units/graphical-drm.target",
    XFCE_DIR / "units/xorg-drm.service",
)
BENCHMARK_RE = re.compile(
    rb"^XFCE_GL_BENCH frames=(\d+) elapsed_ms=([0-9.]+) fps=([0-9.]+)$",
    re.MULTILINE,
)
FRAME_RE = re.compile(
    rb"^XFCE_GL_FRAME index=(\d+) elapsed_ms=([0-9.]+)$", re.MULTILINE
)
FRAME_TIMES_RE = re.compile(
    rb"^XFCE_GL_FRAME_TIMES frames=(\d+) mean_ms=([0-9.]+) "
    rb"p50_ms=([0-9.]+) p95_ms=([0-9.]+) p99_ms=([0-9.]+) "
    rb"max_ms=([0-9.]+) cpu_ms=([0-9.]+) "
    rb"cpu_ms_per_frame=([0-9.]+)$",
    re.MULTILINE,
)
RENDERER_RE = re.compile(rb"^XFCE_GL_RENDERER (.+)$", re.MULTILINE)
DIRECT_RE = re.compile(rb"^XFCE_GL_DIRECT (yes|no)$", re.MULTILINE)
PIXEL_RE = re.compile(
    rb"^XFCE_GL_PIXEL (\d+),(\d+),(\d+),(\d+)$", re.MULTILINE
)
BENCHMARK_PASS = b"XFCE_GL_BENCH_PASS"
METRIC_TOLERANCE_MS = 0.011
EXPECTED_PIXEL = (32, 128, 223, 255)
PIXEL_TOLERANCE = 2


@dataclass(frozen=True)
class BenchmarkSample:
    renderer: str
    is_direct: bool
    frames: int
    elapsed_ms: float
    fps: float
    mean_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    cpu_ms: float
    cpu_ms_per_frame: float


def _single_match(pattern: re.Pattern[bytes], transcript: bytes, label: str) -> re.Match[bytes]:
    matches = list(pattern.finditer(transcript))
    if len(matches) != 1:
        raise ValueError(f"expected one {label} record, found {len(matches)}")
    return matches[0]


def _nearest_rank(values: Sequence[float], percentile: int) -> float:
    rank = math.ceil(percentile * len(values) / 100)
    return sorted(values)[rank - 1]


def _require_close(label: str, actual: float, expected: float) -> None:
    if not math.isclose(actual, expected, abs_tol=METRIC_TOLERANCE_MS):
        raise ValueError(f"{label} mismatch: reported {actual}, calculated {expected}")


def parse_benchmark(transcript: bytes) -> BenchmarkSample:
    """Parses and cross-checks one guest benchmark from a serial transcript."""

    transcript = transcript.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    benchmark = _single_match(BENCHMARK_RE, transcript, "benchmark")
    frame_times = _single_match(FRAME_TIMES_RE, transcript, "frame-times")
    renderer_match = _single_match(RENDERER_RE, transcript, "renderer")
    direct_match = _single_match(DIRECT_RE, transcript, "direct-rendering")
    pixel_match = _single_match(PIXEL_RE, transcript, "validation-pixel")
    if transcript.count(BENCHMARK_PASS) != 1:
        raise ValueError("expected one benchmark pass marker")

    pixel = tuple(int(pixel_match.group(index)) for index in range(1, 5))
    if any(
        abs(actual - expected) > PIXEL_TOLERANCE
        for actual, expected in zip(pixel, EXPECTED_PIXEL, strict=True)
    ):
        raise ValueError(f"validation pixel mismatch: {pixel}")

    frames = int(benchmark.group(1))
    reported_frame_count = int(frame_times.group(1))
    frame_records = [
        (int(match.group(1)), float(match.group(2)))
        for match in FRAME_RE.finditer(transcript)
    ]
    if frames <= 0 or reported_frame_count != frames:
        raise ValueError("benchmark frame counts disagree")
    if [index for index, _ in frame_records] != list(range(frames)):
        raise ValueError("frame timing records are missing, duplicated, or reordered")

    values = [elapsed_ms for _, elapsed_ms in frame_records]
    if not all(math.isfinite(value) and value > 0 for value in values):
        raise ValueError("frame timing must be finite and positive")

    reported_mean_ms = float(frame_times.group(2))
    reported_p50_ms = float(frame_times.group(3))
    reported_p95_ms = float(frame_times.group(4))
    reported_p99_ms = float(frame_times.group(5))
    reported_max_ms = float(frame_times.group(6))
    reported_cpu_ms = float(frame_times.group(7))
    reported_cpu_ms_per_frame = float(frame_times.group(8))
    _require_close("mean", reported_mean_ms, statistics.fmean(values))
    _require_close("p50", reported_p50_ms, _nearest_rank(values, 50))
    _require_close("p95", reported_p95_ms, _nearest_rank(values, 95))
    _require_close("p99", reported_p99_ms, _nearest_rank(values, 99))
    _require_close("max", reported_max_ms, max(values))
    _require_close(
        "CPU time per frame", reported_cpu_ms_per_frame, reported_cpu_ms / frames
    )

    elapsed_ms = float(benchmark.group(2))
    fps = float(benchmark.group(3))
    if not math.isfinite(elapsed_ms) or elapsed_ms <= 0:
        raise ValueError("benchmark elapsed time must be finite and positive")
    if not math.isclose(fps, frames * 1000.0 / elapsed_ms, rel_tol=0.001):
        raise ValueError("benchmark FPS disagrees with frames and elapsed time")

    return BenchmarkSample(
        renderer=renderer_match.group(1).decode("utf-8", "replace").strip(),
        is_direct=direct_match.group(1) == b"yes",
        frames=frames,
        elapsed_ms=elapsed_ms,
        fps=fps,
        mean_ms=reported_mean_ms,
        p50_ms=reported_p50_ms,
        p95_ms=reported_p95_ms,
        p99_ms=reported_p99_ms,
        max_ms=reported_max_ms,
        cpu_ms=reported_cpu_ms,
        cpu_ms_per_frame=reported_cpu_ms_per_frame,
    )


def rotated_paths(paths: Sequence[str], round_index: int) -> list[str]:
    """Rotates path order each round to balance host-temperature effects."""

    offset = round_index % len(paths)
    return list(paths[offset:]) + list(paths[:offset])


def summarize_results(
    results: Sequence[dict[str, object]],
    paths: Sequence[str],
    rounds: int,
    minimum_p95_speedup: float,
) -> dict[str, object]:
    """Builds the performance verdict from successful, renderer-verified runs."""

    path_summaries: dict[str, dict[str, object]] = {}
    for display_path in paths:
        samples = [
            result["sample"]
            for result in results
            if result["display_path"] == display_path and result["passed"]
        ]
        summary: dict[str, object] = {
            "successful_runs": len(samples),
            "required_runs": rounds,
        }
        if samples:
            for metric in (
                "fps",
                "mean_ms",
                "p50_ms",
                "p95_ms",
                "p99_ms",
                "max_ms",
                "cpu_ms_per_frame",
            ):
                summary[f"median_{metric}"] = statistics.median(
                    float(sample[metric]) for sample in samples
                )
        path_summaries[display_path] = summary

    complete = all(
        path_summaries[display_path]["successful_runs"] == rounds
        for display_path in paths
    )
    p95_speedup: float | None = None
    fps_speedup: float | None = None
    if "software-drm" in paths and "virgl" in paths:
        software = path_summaries["software-drm"]
        virgl = path_summaries["virgl"]
        if software["successful_runs"] and virgl["successful_runs"]:
            p95_speedup = float(software["median_p95_ms"]) / float(
                virgl["median_p95_ms"]
            )
            fps_speedup = float(virgl["median_fps"]) / float(
                software["median_fps"]
            )

    acceleration_required = "software-drm" in paths and "virgl" in paths
    acceleration_passed = (
        not acceleration_required
        or (
            p95_speedup is not None
            and p95_speedup >= minimum_p95_speedup
        )
    )
    return {
        "complete": complete,
        "passed": complete and acceleration_passed,
        "minimum_p95_speedup": minimum_p95_speedup,
        "p95_speedup_software_over_virgl": p95_speedup,
        "fps_speedup_virgl_over_software": fps_speedup,
        "paths": path_summaries,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _input_digest(path: Path) -> str:
    """Hashes file contents or a directory tree, including modes and links."""

    if path.is_file():
        return _sha256(path)
    if not path.is_dir():
        raise ValueError(f"unsupported benchmark input: {path}")

    digest = hashlib.sha256()
    digest.update(b"directory\0")
    children = sorted(
        path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()
    )
    for child in children:
        relative = child.relative_to(path).as_posix().encode("utf-8", "surrogateescape")
        metadata = child.lstat()
        digest.update(relative)
        digest.update(b"\0")
        digest.update((metadata.st_mode & 0o7777).to_bytes(2, "little"))
        if child.is_symlink():
            digest.update(b"link\0")
            digest.update(os.readlink(child).encode("utf-8", "surrogateescape"))
        elif child.is_dir():
            digest.update(b"directory\0")
        elif child.is_file():
            digest.update(b"file\0")
            with child.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
        else:
            digest.update(b"special\0")
            digest.update(metadata.st_rdev.to_bytes(8, "little"))
        digest.update(b"\0")
    return digest.hexdigest()


def _benchmark_source_digest() -> str:
    digest = hashlib.sha256()
    for path in BENCHMARK_SOURCE_FILES:
        digest.update(path.relative_to(REPO).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _stop_process_group(process: subprocess.Popen[str]) -> None:
    for termination_signal, timeout_seconds in (
        (signal.SIGINT, 30),
        (signal.SIGTERM, 10),
    ):
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, termination_signal)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=timeout_seconds)
            return
        except subprocess.TimeoutExpired:
            continue
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        process.wait(timeout=10)


def _run_streaming(command: Sequence[str], log_path: Path) -> int:
    print(f"[run] {' '.join(command)}", flush=True)
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        assert process.stdout is not None
        try:
            for line in process.stdout:
                print(line, end="", flush=True)
                log.write(line)
                log.flush()
        except BaseException:
            _stop_process_group(process)
            raise
        return process.wait()


def _copy_reflink(source: Path, destination: Path) -> None:
    subprocess.run(
        ["cp", "--reflink=auto", "--sparse=always", source, destination],
        check=True,
    )


def _git_metadata() -> tuple[str, list[str]]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty_paths = subprocess.run(
        ["git", "status", "--short"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    return head, dirty_paths


def _qemu_process_count() -> int:
    count = 0
    for process_dir in Path("/proc").glob("[0-9]*"):
        try:
            command = (process_dir / "comm").read_text(encoding="utf-8").strip()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if command.startswith("qemu-system-"):
            count += 1
    return count


def _prepare_baselines(
    paths: Sequence[str], base: Path, runtime: Path, smp: int
) -> dict[str, object]:
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    baseline_records: dict[str, dict[str, object]] = {}
    for display_path in paths:
        command = [
            sys.executable,
            os.fspath(BUILD_SCRIPT),
            "--base",
            os.fspath(base),
            "--runtime",
            os.fspath(runtime),
            "--smp",
            str(smp),
            *PATH_FLAGS[display_path],
        ]
        subprocess.run(command, cwd=REPO, check=True)
        baseline = BASELINE_DIR / f"{display_path}.ext2"
        temporary = baseline.with_suffix(".ext2.tmp")
        temporary.unlink(missing_ok=True)
        _copy_reflink(BUILD_ROOT_DISK, temporary)
        temporary.replace(baseline)
        baseline_records[display_path] = {
            "path": os.fspath(baseline.relative_to(REPO)),
            "size_bytes": baseline.stat().st_size,
            "sha256": _sha256(baseline),
        }

    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "source_digest": _benchmark_source_digest(),
        "base": os.fspath(base),
        "base_digest": _input_digest(base),
        "runtime": os.fspath(runtime),
        "runtime_digest": _input_digest(runtime),
        "smp": smp,
        "baselines": baseline_records,
    }
    _write_json(BASELINE_MANIFEST, manifest)
    return manifest


def _load_baselines(paths: Sequence[str], base: Path, runtime: Path, smp: int) -> dict[str, object]:
    try:
        manifest = json.loads(BASELINE_MANIFEST.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise SystemExit("missing or invalid baseline manifest; omit --reuse-baselines") from error
    expected = {
        "source_digest": _benchmark_source_digest(),
        "base": os.fspath(base),
        "base_digest": _input_digest(base),
        "runtime": os.fspath(runtime),
        "runtime_digest": _input_digest(runtime),
        "smp": smp,
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise SystemExit(f"baseline {key} is stale; omit --reuse-baselines")
    for display_path in paths:
        record = manifest.get("baselines", {}).get(display_path)
        if not isinstance(record, dict):
            raise SystemExit(f"missing {display_path} baseline")
        path = REPO / str(record["path"])
        if (
            not path.exists()
            or path.stat().st_size != record["size_bytes"]
            or _sha256(path) != record["sha256"]
        ):
            raise SystemExit(f"invalid {display_path} baseline artifact")
    return manifest


def _render_summary(summary: dict[str, object]) -> str:
    lines = [
        "# Xfce display performance summary",
        "",
        f"Result: **{'PASS' if summary['passed'] else 'FAIL'}**",
        "",
        "| Path | Successful runs | Median FPS | Median p95 frame | Median guest CPU/frame |",
        "|---|---:|---:|---:|---:|",
    ]
    for display_path, path_summary in summary["paths"].items():
        successful = path_summary["successful_runs"]
        required = path_summary["required_runs"]
        if successful:
            fps = f"{path_summary['median_fps']:.3f}"
            p95 = f"{path_summary['median_p95_ms']:.3f} ms"
            cpu = f"{path_summary['median_cpu_ms_per_frame']:.3f} ms"
        else:
            fps = p95 = cpu = "—"
        lines.append(
            f"| {display_path} | {successful}/{required} | {fps} | {p95} | {cpu} |"
        )
    lines.extend(
        [
            "",
            "The acceleration gate compares software DRM with virgl,",
            "so DRM infrastructure overhead is held constant.",
        ]
    )
    speedup = summary["p95_speedup_software_over_virgl"]
    if speedup is not None:
        lines.extend(
            [
                f"Virgl p95 frame-latency speedup: **{speedup:.3f}×**.",
                f"Required speedup: **{summary['minimum_p95_speedup']:.3f}×**.",
            ]
        )
    return "\n".join(lines) + "\n"


def _parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument(
        "--paths", nargs="+", choices=DISPLAY_PATHS, default=list(DISPLAY_PATHS)
    )
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--kernel-image", type=Path, default=DEFAULT_KERNEL)
    parser.add_argument("--smp", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--settle", type=float, default=3.0)
    parser.add_argument("--minimum-p95-speedup", type=float, default=2.0)
    parser.add_argument("--reuse-baselines", action="store_true")
    parser.add_argument("--allow-host-contention", action="store_true")
    parser.add_argument("--cpu-list", help="optional taskset CPU list for QEMU")
    parser.add_argument("--output", type=Path)
    parsed = parser.parse_args(arguments)
    if not 1 <= parsed.rounds <= 100:
        parser.error("--rounds must be in [1, 100]")
    if parsed.smp <= 0:
        parser.error("--smp must be positive")
    if not math.isfinite(parsed.minimum_p95_speedup) or parsed.minimum_p95_speedup <= 0:
        parser.error("--minimum-p95-speedup must be finite and positive")
    if len(set(parsed.paths)) != len(parsed.paths):
        parser.error("--paths must not contain duplicates")
    return parsed


def main(arguments: Sequence[str] | None = None) -> int:
    args = _parse_args(arguments)
    for path in (args.base, args.runtime, args.kernel_image):
        if not path.exists():
            raise SystemExit(f"missing input: {path}")
    if not args.allow_host_contention and _qemu_process_count() != 0:
        raise SystemExit("another QEMU process is running; stop it or use --allow-host-contention")

    manifest = (
        _load_baselines(args.paths, args.base, args.runtime, args.smp)
        if args.reuse_baselines
        else _prepare_baselines(args.paths, args.base, args.runtime, args.smp)
    )
    head, dirty_paths = _git_metadata()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output = args.output or EVIDENCE_ROOT / "runs" / f"{timestamp}-{head[:12]}"
    if output.exists():
        raise SystemExit(f"refusing to overwrite existing output: {output}")
    output.mkdir(parents=True)

    qemu_version = subprocess.run(
        ["qemu-system-riscv64", "--version"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()[0]
    evidence: dict[str, object] = {
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
        "git_head": head,
        "dirty_paths": dirty_paths,
        "qemu_version": qemu_version,
        "kernel_image": os.fspath(args.kernel_image),
        "kernel_sha256": _sha256(args.kernel_image),
        "baseline_manifest": manifest,
        "rounds": args.rounds,
        "paths": args.paths,
        "minimum_p95_speedup": args.minimum_p95_speedup,
        "smp": args.smp,
        "cpu_list": args.cpu_list,
        "results": [],
    }
    results = evidence["results"]
    assert isinstance(results, list)
    _write_json(output / "result.json", evidence)

    interrupted = False
    try:
        for round_index in range(args.rounds):
            for order_index, display_path in enumerate(
                rotated_paths(args.paths, round_index)
            ):
                if not args.allow_host_contention and _qemu_process_count() != 0:
                    raise RuntimeError("another QEMU process appeared during the matrix")
                run_name = f"round-{round_index + 1:02d}-{order_index + 1:02d}-{display_path}"
                serial_log = output / f"{run_name}.serial.log"
                driver_log = output / f"{run_name}.driver.log"
                baseline_record = manifest["baselines"][display_path]
                baseline = REPO / baseline_record["path"]
                with tempfile.TemporaryDirectory(
                    prefix=f"xfce-perf-{display_path}-", dir=output
                ) as temporary_directory:
                    root_disk = Path(temporary_directory) / "root.ext2"
                    _copy_reflink(baseline, root_disk)
                    command = [
                        sys.executable,
                        os.fspath(BOOT_SCRIPT),
                        "--timeout",
                        str(args.timeout),
                        "--settle",
                        str(args.settle),
                        "--smp",
                        str(args.smp),
                        "--kernel-image",
                        os.fspath(args.kernel_image),
                        "--root-disk",
                        os.fspath(root_disk),
                        "--serial-log",
                        os.fspath(serial_log),
                        *PATH_FLAGS[display_path],
                    ]
                    if args.cpu_list:
                        command = ["taskset", "--cpu-list", args.cpu_list, *command]
                    host_load_before = os.getloadavg()
                    return_code = _run_streaming(command, driver_log)
                    host_load_after = os.getloadavg()

                result: dict[str, object] = {
                    "round": round_index + 1,
                    "order": order_index + 1,
                    "display_path": display_path,
                    "return_code": return_code,
                    "serial_log": serial_log.name,
                    "driver_log": driver_log.name,
                    "host_load_before": host_load_before,
                    "host_load_after": host_load_after,
                    "passed": False,
                }
                try:
                    sample = parse_benchmark(serial_log.read_bytes())
                    renderer_matches = EXPECTED_RENDERERS[display_path] in sample.renderer.lower()
                    result["sample"] = asdict(sample)
                    result["renderer_matches"] = renderer_matches
                    result["passed"] = (
                        return_code == 0 and renderer_matches and sample.is_direct
                    )
                    if not result["passed"]:
                        result["reason"] = "boot, direct-rendering, or renderer gate failed"
                except (OSError, ValueError) as error:
                    result["reason"] = str(error)
                results.append(result)
                _write_json(output / "result.json", evidence)
    except KeyboardInterrupt:
        interrupted = True
    except Exception as error:  # Keep partial evidence for infrastructure failures.
        evidence["infrastructure_error"] = str(error)

    summary = summarize_results(
        results, args.paths, args.rounds, args.minimum_p95_speedup
    )
    evidence["status"] = "interrupted" if interrupted else "complete"
    evidence["finished_at"] = datetime.now(UTC).isoformat()
    evidence["summary"] = summary
    _write_json(output / "result.json", evidence)
    (output / "summary.md").write_text(_render_summary(summary), encoding="utf-8")
    print(f"[evidence] {output / 'result.json'}")
    print(_render_summary(summary), end="")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
