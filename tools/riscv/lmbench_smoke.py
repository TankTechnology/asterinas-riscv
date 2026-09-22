#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run a bounded LMBench compatibility sample inside an already booted guest.

This is a smoke test, not a statistically meaningful performance measurement.
The repository's pinned LMBench executables and their runtime libraries must
already be installed. Only Python's standard library is required by this runner.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time


@dataclass(frozen=True)
class Case:
    name: str
    args: tuple[str, ...]
    measurement: str
    unit: str


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    bindir = args.bin_dir.resolve()
    start = time.monotonic()
    report = {
        'schema_version': 1,
        'purpose': 'compatibility-smoke',
        'uname': list(os.uname()),
        'bin_dir': str(bindir),
        'parameters': {'ENOUGH': '10000', 'parallelism': 1, 'warmup': 0,
                       'repetitions': 1, 'case_timeout_seconds': 10},
        'cases': [],
        'passed': False,
    }
    created_hello = False
    hello = Path('/tmp/hello')
    try:
        # lat_proc hard-codes /tmp/hello and ignores its child's exit status.
        # Verify the executable independently and never overwrite another file.
        expected = (bindir / 'hello').read_bytes()
        try:
            with hello.open('xb') as output:
                created_hello = True
                output.write(expected)
            hello.chmod(0o755)
        except FileExistsError:
            if hello.is_symlink() or hello.read_bytes() != expected:
                raise RuntimeError('/tmp/hello exists with different content')
        for argv in ([str(hello)], ['/bin/sh', '-c', str(hello)]):
            check = subprocess.run(argv, env={}, capture_output=True,
                                   text=True, timeout=3)
            if check.returncode != 0 or check.stdout != 'Hello world\n':
                raise RuntimeError(f'exec preflight failed: {check!r}')
        report['exec_preflight'] = 'passed'
        with tempfile.TemporaryDirectory(prefix='lmbench-smoke-') as work:
            fixture = Path(work) / 'file'
            fixture.write_bytes(b'x' * (1024 * 1024))
            cases = basic_cases(str(fixture))
            report['planned_cases'] = len(cases)
            binaries = {case.args[0] for case in cases} | {'hello'}
            report['binary_sha256'] = {
                name: hashlib.sha256((bindir / name).read_bytes()).hexdigest()
                for name in sorted(binaries)
            }
            env = os.environ.copy()
            # Retain actual loop/timer overhead calibration, but bound the
            # minimum sample interval instead of searching for one under TCG.
            for name in ('LOOP_O', 'TIMING_O', 'LMBENCH_SCHED'):
                env.pop(name, None)
            env.update(ENOUGH='10000', LC_ALL='C')
            for case in cases:
                print(json.dumps({'event': 'start', 'name': case.name}), flush=True)
                executable = str(bindir / case.args[0])
                invocation = Case(case.name, case.args[1:], case.measurement, case.unit)
                result = run_case(invocation, executable, env, 10)
                report['cases'].append(result)
                print(json.dumps({'event': 'result', **result}), flush=True)
                if not result['passed']:
                    break
            report['passed'] = (len(report['cases']) == len(cases)
                                and all(row['passed'] for row in report['cases']))
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        report['error'] = str(error)
    finally:
        if created_hello:
            hello.unlink(missing_ok=True)
        report['elapsed'] = round(time.monotonic() - start, 3)
        args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'event': 'complete', 'passed': report['passed'],
                      'elapsed': report['elapsed']}), flush=True)
    return 0 if report['passed'] else 1


def basic_cases(fixture: str) -> list[Case]:
    common = ('-P', '1', '-W', '0', '-N', '1')
    cases = []
    for mode, label in [('null', 'syscall'), ('read', 'read'), ('write', 'write'),
                        ('stat', 'stat'), ('fstat', 'fstat'), ('open', 'open/close')]:
        extra = (fixture,) if mode in ('stat', 'fstat', 'open') else ()
        name = 'getppid' if mode == 'null' else mode
        cases.append(Case('syscall-' + name, ('lat_syscall',) + common + (mode,) + extra,
                          'Simple ' + label, 'us'))
    for mode, label in [('fork', 'fork+exit'), ('exec', 'fork+execve'),
                        ('shell', 'fork+/bin/sh -c')]:
        cases.append(Case('process-' + mode, ('lat_proc',) + common + (mode,),
                          'Process ' + label, 'us'))
    cases += [Case('pipe', ('lat_pipe',) + common, 'Pipe latency', 'us'),
              Case('context-switch-2', ('lat_ctx',) + common + ('-s', '0', '2'),
                   'context', 'us')]
    for mode, label in [('install', 'installation'), ('catch', 'overhead')]:
        cases.append(Case('signal-' + mode, ('lat_sig',) + common + (mode,),
                          'Signal handler ' + label, 'us'))
    for mode in ('rd', 'wr', 'cp'):
        cases.append(Case('memory-' + mode, ('bw_mem',) + common + ('1m', mode),
                          'size', 'MB/s'))
    cases += [Case('mmap', ('lat_mmap',) + common + ('1m', fixture), 'size', 'us'),
              Case('pagefault', ('lat_pagefault',) + common + (fixture,),
                   'Pagefaults on ' + fixture, 'us')]
    return cases


def parse_measurement(kind: str, output: str) -> float | None:
    number = r'([0-9]+(?:\.[0-9]+)?)'
    if kind == 'size':
        match = re.fullmatch(number + r'\s+' + number, output.strip())
        # 1 MiB is printed as 1.05 decimal MB by bw_mem and 1.048576 by lat_mmap.
        if match is None or not math.isclose(float(match[1]), 1.048576, rel_tol=.01):
            return None
    elif kind == 'context':
        match = re.fullmatch(r'"size=0k ovr=' + number + r'\n2\s+' + number,
                             output.strip())
    else:
        match = re.fullmatch(re.escape(kind) + r': ' + number + r' microseconds',
                             output.strip())
    if match is None:
        return None
    value = float(match.groups()[-1])
    return value if math.isfinite(value) and value > 0 else None


def run_case(case: Case, executable: str, env: dict[str, str], timeout: float) -> dict:
    argv = [executable, *case.args]
    start = time.monotonic()
    result = {'name': case.name, 'argv': argv, 'timeout': False, 'passed': False,
              'unit': case.unit}
    try:
        process = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True,
                                   start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            result['timeout'] = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate(timeout=2)
        value = parse_measurement(case.measurement, stdout + stderr)
        result.update(status=process.returncode, stdout=stdout, stderr=stderr, value=value)
        result['passed'] = process.returncode == 0 and not result['timeout'] and value is not None
    except (OSError, subprocess.SubprocessError) as error:
        result['error'] = str(error)
    result['elapsed'] = round(time.monotonic() - start, 3)
    return result


if __name__ == '__main__':
    raise SystemExit(main())
