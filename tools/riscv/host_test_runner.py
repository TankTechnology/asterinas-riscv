#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Run the RISC-V host test suite in bounded, isolated module workers."""

from __future__ import annotations

import argparse
import concurrent.futures
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TESTS_DIR = REPO_ROOT / "tools" / "riscv" / "tests"
DEFAULT_BASELINE = DEFAULT_TESTS_DIR / "expected_host_tests.json"
DEFAULT_LOG_DIR = REPO_ROOT / "target" / "host-tests"
MODULE_PREFIX = "tools.riscv.tests."
DEFAULT_TIMEOUT_SECONDS = 300.0


def discover_modules(
    tests_dir: Path = DEFAULT_TESTS_DIR, *, root: Path = REPO_ROOT
) -> list[str]:
    """Return sorted dotted module names for every host test file."""

    tests_dir = tests_dir.resolve()
    root = root.resolve()
    modules: list[str] = []
    for path in sorted(tests_dir.rglob("test_*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            relative = path.relative_to(root).with_suffix("")
        except ValueError:
            continue
        module = ".".join(relative.parts)
        if module.startswith(MODULE_PREFIX):
            modules.append(module)
    return sorted(modules)


def shard_modules(
    modules: Sequence[str], shard_index: int, shard_count: int
) -> list[str]:
    """Select a deterministic, non-overlapping slice of the module list."""

    if shard_count < 1:
        raise ValueError("shard count must be positive")
    if not 0 <= shard_index < shard_count:
        raise ValueError("shard index must be within the shard count")
    return list(modules[shard_index::shard_count])


def iter_tests(suite: unittest.TestSuite) -> Iterable[unittest.TestCase]:
    """Flatten a unittest suite into leaf test cases."""

    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from iter_tests(test)
        else:
            yield test


def test_id(test: unittest.TestCase) -> str:
    """Return a stable identifier even for unittest error holders."""

    try:
        return test.id()
    except (AttributeError, TypeError):
        return str(test)


def duplicate_ids(ids: Sequence[str]) -> list[str]:
    """Return IDs collected more than once, in deterministic order."""

    return sorted({value for value in ids if ids.count(value) > 1})


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Write one finite JSON object atomically."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


class _JsonResult(unittest.TestResult):
    """Collect machine-readable outcomes without changing unittest behavior."""

    def __init__(self) -> None:
        super().__init__()
        self.failures_list: list[dict[str, str]] = []
        self.errors_list: list[dict[str, str]] = []
        self.skipped_list: list[dict[str, str]] = []
        self.expected_failures_list: list[dict[str, str]] = []
        self.unexpected_successes_list: list[dict[str, str]] = []

    def _record(
        self, target: list[dict[str, str]], test: unittest.TestCase, error=None
    ) -> None:
        value = {"test": test_id(test)}
        if error is not None:
            value["traceback"] = self._exc_info_to_string(error, test)
        target.append(value)

    def addFailure(self, test: unittest.TestCase, error) -> None:  # type: ignore[no-untyped-def]
        super().addFailure(test, error)
        self._record(self.failures_list, test, error)

    def addError(self, test: unittest.TestCase, error) -> None:  # type: ignore[no-untyped-def]
        super().addError(test, error)
        self._record(self.errors_list, test, error)

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:
        super().addSkip(test, reason)
        self.skipped_list.append({"test": test_id(test), "reason": reason})

    def addExpectedFailure(self, test: unittest.TestCase, error) -> None:  # type: ignore[no-untyped-def]
        super().addExpectedFailure(test, error)
        self._record(self.expected_failures_list, test, error)

    def addUnexpectedSuccess(self, test: unittest.TestCase) -> None:
        super().addUnexpectedSuccess(test)
        self._record(self.unexpected_successes_list, test)


def run_worker(module: str, result_path: Path, *, list_only: bool) -> int:
    """Run or list one module and write its result as JSON."""

    start = time.monotonic()
    try:
        importlib.import_module(module)
        suite = unittest.defaultTestLoader.loadTestsFromName(module)
        ids = [test_id(test) for test in iter_tests(suite)]
    except BaseException as error:  # noqa: BLE001 - report import failures as data
        write_json(
            result_path,
            {
                "module": module,
                "tests": [],
                "error": f"{type(error).__name__}: {error}",
                "elapsed_seconds": round(time.monotonic() - start, 3),
            },
        )
        return 1

    if list_only:
        write_json(
            result_path,
            {
                "module": module,
                "tests": ids,
                "elapsed_seconds": round(time.monotonic() - start, 3),
            },
        )
        return 0

    result = _JsonResult()
    try:
        suite.run(result)
    except BaseException as error:  # noqa: BLE001 - preserve a complete result file
        result.errors_list.append(
            {
                "test": module,
                "traceback": f"{type(error).__name__}: {error}",
            }
        )
    payload: dict[str, Any] = {
        "module": module,
        "tests": ids,
        "ran": result.testsRun,
        "failures": result.failures_list,
        "errors": result.errors_list,
        "skipped": result.skipped_list,
        "expected_failures": result.expected_failures_list,
        "unexpected_successes": result.unexpected_successes_list,
        "elapsed_seconds": round(time.monotonic() - start, 3),
    }
    write_json(result_path, payload)
    return 1 if (
        result.failures_list
        or result.errors_list
        or result.unexpected_successes_list
    ) else 0


def worker_command(module: str, result_path: Path, *, list_only: bool) -> list[str]:
    """Build the isolated worker command for one module."""

    command = [
        sys.executable,
        "-W",
        "error::ResourceWarning",
        str(Path(__file__).resolve()),
        "--worker",
        module,
        "--result",
        str(result_path),
    ]
    if list_only:
        command.append("--list")
    return command


def run_module(
    module: str,
    *,
    timeout: float,
    log_dir: Path,
    list_only: bool = False,
) -> dict[str, Any]:
    """Run one module in a subprocess and return its parsed result."""

    log_dir.mkdir(parents=True, exist_ok=True)
    result_path = log_dir / f"{module}.json"
    log_path = log_dir / f"{module}.log"
    result_path.unlink(missing_ok=True)
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(REPO_ROOT) if not existing else f"{REPO_ROOT}:{existing}"
    )
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    started = time.monotonic()
    timed_out = False
    with log_path.open("wb") as log:
        try:
            completed = subprocess.run(
                worker_command(module, result_path, list_only=list_only),
                cwd=REPO_ROOT,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=timeout,
            )
            returncode = completed.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            returncode = 124
    elapsed = round(time.monotonic() - started, 3)
    payload: dict[str, Any]
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        payload = {
            "module": module,
            "tests": [],
            "error": f"missing or invalid worker result: {type(error).__name__}",
        }
    if not isinstance(payload, dict):
        payload = {
            "module": module,
            "tests": [],
            "error": "worker result is not a JSON object",
        }
    payload.update(
        {
            "module": module,
            "returncode": returncode,
            "timed_out": timed_out,
            "elapsed_seconds": elapsed,
            "log": str(log_path),
        }
    )
    return payload


def _module_failed(payload: Mapping[str, Any]) -> bool:
    return bool(
        payload.get("timed_out")
        or payload.get("returncode") not in (0, None)
        or payload.get("error")
        or payload.get("failures")
        or payload.get("errors")
        or payload.get("unexpected_successes")
    )


def baseline_disagreements(
    expected: Mapping[str, Any],
    actual_ids: set[str],
    skip_reasons: Sequence[str],
    actual_modules: set[str] | None = None,
) -> list[str]:
    """Report test or module drift and skips outside the allowlist."""

    problems: list[str] = []
    expected_ids = set(expected.get("test_ids", []))
    missing = sorted(expected_ids - actual_ids)
    if missing:
        problems.append(
            f"{len(missing)} baseline test IDs are missing; first: {missing[0]}"
        )
    unexpected = sorted(actual_ids - expected_ids)
    if unexpected:
        problems.append(
            f"{len(unexpected)} test IDs are not in the baseline; first: {unexpected[0]}"
        )
    expected_count = expected.get("tests")
    if isinstance(expected_count, int) and len(actual_ids) < expected_count:
        problems.append(
            f"discovered {len(actual_ids)} tests, below baseline {expected_count}"
        )
    if actual_modules is not None:
        expected_modules = set(expected.get("module_names", []))
        added_modules = sorted(actual_modules - expected_modules)
        removed_modules = sorted(expected_modules - actual_modules)
        if added_modules or removed_modules:
            problems.append(
                "module baseline mismatch: "
                f"added={added_modules[:3]} removed={removed_modules[:3]}"
            )
    allowed = set(expected.get("skip_reasons", []))
    for reason in sorted(set(skip_reasons)):
        if reason not in allowed:
            problems.append(f"unexpected skip reason: {reason}")
    return problems


def _load_baseline(path: Path) -> Mapping[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _run_many(
    modules: Sequence[str], *, jobs: int, timeout: float, log_dir: Path, list_only: bool
) -> list[dict[str, Any]]:
    if jobs < 1:
        raise ValueError("jobs must be positive")
    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [
            pool.submit(
                run_module,
                module,
                timeout=timeout,
                log_dir=log_dir,
                list_only=list_only,
            )
            for module in modules
        ]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    return sorted(results, key=lambda value: str(value["module"]))


def _print_list(modules: Sequence[str], results: Sequence[dict[str, Any]]) -> int:
    ids: list[str] = []
    problems: list[str] = []
    for result in results:
        if result.get("error") or result.get("timed_out"):
            problems.append(f"{result['module']}: {result.get('error', 'timeout')}")
            continue
        ids.extend(result.get("tests", []))
    duplicates = duplicate_ids(ids)
    if duplicates:
        problems.append(f"duplicate test IDs: {duplicates[:3]}")
    ids = sorted(ids)
    print(
        json.dumps(
            {
                "modules": len(modules),
                "tests": len(ids),
                "test_ids": ids,
                "problems": problems,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 1 if problems else 0


def _print_run(results: Sequence[dict[str, Any]], baseline_path: Path, no_baseline: bool) -> int:
    problems: list[str] = []
    all_ids: list[str] = []
    skip_reasons: list[str] = []
    failures: list[dict[str, Any]] = []
    empty: list[str] = []
    for result in results:
        module = str(result["module"])
        if not result.get("tests"):
            empty.append(module)
        all_ids.extend(result.get("tests", []))
        if _module_failed(result):
            failures.append(result)
        for skipped in result.get("skipped", []):
            skip_reasons.append(str(skipped.get("reason", "")))
    if empty:
        print(f"warning: {len(empty)} modules contain no tests", file=sys.stderr)
    duplicates = duplicate_ids(all_ids)
    if duplicates:
        problems.append(f"duplicate test IDs: {duplicates[:3]}")
    if failures:
        problems.append(f"{len(failures)} modules failed")
    baseline = None if no_baseline else _load_baseline(baseline_path)
    if not no_baseline and baseline is None:
        problems.append(f"baseline unavailable or invalid: {baseline_path}")
    elif baseline is not None:
        actual_modules = {str(result["module"]) for result in results}
        problems.extend(
            baseline_disagreements(
                baseline,
                set(all_ids),
                skip_reasons,
                actual_modules,
            )
        )
    summary = {
        "modules": len(results),
        "tests": len(all_ids),
        "skipped": len(skip_reasons),
        "failures": [
            {
                "module": result["module"],
                "returncode": result.get("returncode"),
                "timed_out": result.get("timed_out", False),
                "details": [
                    *(result.get("failures") or []),
                    *(result.get("errors") or []),
                    *(result.get("unexpected_successes") or []),
                ],
            }
            for result in failures
        ],
        "problems": problems,
    }
    for result in results:
        status = "FAIL" if _module_failed(result) else "OK"
        print(
            f"{status:4} {result['module']:55} "
            f"tests={len(result.get('tests', [])):4} "
            f"ran={result.get('ran', 0):4} "
            f"elapsed={result.get('elapsed_seconds', 0):7.3f}s"
        )
    print(
        f"TOTAL modules={summary['modules']} tests={summary['tests']} "
        f"skipped={summary['skipped']} failed_modules={len(failures)}"
    )
    if problems:
        for problem in problems:
            print(f"ERROR {problem}", file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    parser.add_argument("--result", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--list", action="store_true", help="list test IDs only")
    parser.add_argument("--json", action="store_true", help="emit list JSON")
    parser.add_argument("--jobs", type=int, default=32)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--module", action="append", default=[])
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--no-baseline", action="store_true")
    values = parser.parse_args(argv)

    if values.worker:
        if values.result is None:
            parser.error("--worker requires --result")
        return run_worker(values.worker, values.result, list_only=values.list)

    modules = discover_modules()
    if values.module:
        requested = set(values.module)
        unknown = sorted(requested - set(modules))
        if unknown:
            parser.error(f"unknown modules: {unknown}")
        modules = [module for module in modules if module in requested]
    try:
        modules = shard_modules(modules, values.shard_index, values.shard_count)
    except ValueError as error:
        parser.error(str(error))
    results = _run_many(
        modules,
        jobs=values.jobs,
        timeout=values.timeout,
        log_dir=values.log_dir,
        list_only=values.list,
    )
    if values.list:
        if values.json:
            return _print_list(modules, results)
        ids = sorted(
            test_id_value
            for result in results
            for test_id_value in result.get("tests", [])
        )
        print(f"modules={len(modules)} tests={len(ids)}")
        return 1 if any(result.get("error") for result in results) else 0
    return _print_run(results, values.baseline, values.no_baseline)


if __name__ == "__main__":
    raise SystemExit(main())
