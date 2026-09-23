#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Package and supervise the pinned native LMBench ALL workflow (8 MiB).

Run `package --output FILE` inside the development container, extract the archive
at / in a disposable Debian guest, source /opt/lmbench/activate, then run
`cd /opt/lmbench/src && make results`. Python 3 and ordinary Debian tools are
required in the guest. This is compatibility qualification, not a TCG score.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time

REVISION = 'afb47eddaf10a411c1ea3cb64965461f1308a6ea'
PLATFORM = 'riscv64-unknown-linux-gnu'


def native_command(make: str, platform: str) -> list[str]:
    return [make, '--no-print-directory', '-f', 'Makefile', '-o', 'lmbench',
            'OS=' + platform, 'results']


def make_entry() -> str:
    return ('# SPDX-License-Identifier: MPL-2.0\n'
            '.PHONY: results result\n'
            'results:\n\tpython3 ../asterinas-native.py run\n'
            'result: results\n')


def measurements() -> dict[str, str]:
    labels = {'syscall-' + name: 'Simple ' + label for name, label in
              [('null', 'syscall'), ('read', 'read'), ('write', 'write'),
               ('stat', 'stat'), ('fstat', 'fstat'), ('open', 'open/close')]}
    labels.update({'select-' + str(n): f"Select on {n} fd's" for n in (10, 100, 250, 500)})
    labels.update({'select-tcp-' + str(n): f"Select on {n} tcp fd's" for n in (10, 100, 250, 500)})
    labels.update({'signal-install': 'Signal handler installation',
                   'signal-catch': 'Signal handler overhead',
                   'signal-prot': 'Protection fault', 'pipe-lat': 'Pipe latency',
                   'unix-lat': 'AF_UNIX sock stream latency',
                   'unix-bw': 'AF_UNIX sock stream bandwidth', 'pipe-bw': 'Pipe bandwidth',
                   'udp-lat': 'UDP latency using localhost',
                   'tcp-lat': 'TCP latency using localhost',
                   'rpc-udp': 'RPC/udp latency using localhost',
                   'rpc-tcp': 'RPC/tcp latency using localhost',
                   'tcp-connect': 'TCP/IP connection cost to localhost'})
    labels.update({'process-' + mode: 'Process ' + label for mode, label in
                   [('fork', 'fork+exit'), ('exec', 'fork+execve'), ('shell', 'fork+/bin/sh -c')]})
    for kind in ('integer', 'int64', 'float', 'double'):
        for op in (('bit', 'add', 'mul', 'div', 'mod') if kind in ('integer', 'int64')
                   else ('add', 'mul', 'div')):
            label = kind + ' ' + op
            labels[label] = label
            labels[label + '-parallelism'] = label + ' parallelism'
    for kind in ('float', 'double'):
        labels[kind + '-bogomflops'] = kind + ' bogomflops'
    for version, ops in [('STREAM', ('copy', 'scale', 'add', 'triad')),
                         ('STREAM2', ('fill', 'copy', 'daxpy', 'sum'))]:
        for op in ops:
            for metric in ('latency', 'bandwidth'):
                labels[f'{version.lower()}-{op}-{metric}'] = f'{version} {op} {metric}'
    return labels


def finite_number(value: str) -> bool:
    try:
        return math.isfinite(float(value)) and float(value) >= 0
    except ValueError:
        return False


def table_rows(text: str, heading: str) -> list[list[str]]:
    match = re.search(r'^' + re.escape(heading) + r'\n([^\n]*(?:\n[^\n]+)*)', text, re.M)
    return [row.split() for row in match[1].splitlines()] if match else []


def numeric_table(rows: list[list[str]], sizes: list[float], *, tolerance: float,
                  small_tolerance: float | None = None) -> bool:
    return (len(rows) == len(sizes)
            and all(len(row) == 2 and all(finite_number(v) for v in row)
                    and abs(float(row[0]) - size) <= (small_tolerance if size < 1 and small_tolerance is not None else tolerance)
                    for row, size in zip(rows, sizes)))


@contextmanager
def owned_hello(path: Path, content: bytes):
    # Reserve the hard-coded helper atomically, including dangling symlinks.
    with path.open('xb') as file:
        file.write(content)
        identity = os.fstat(file.fileno())
    path.chmod(0o755)
    try:
        yield path
    finally:
        try:
            current = path.lstat()
            if ((current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino)
                    and not path.is_symlink() and path.read_bytes() == content):
                path.unlink()
        except FileNotFoundError:
            pass


def termination_requested(signum, frame):
    raise RuntimeError(f'terminated by signal {signum}')


def audit(text: str, stderr: str, status: int | None, timed_out: bool) -> dict:
    """Check native ALL result coverage; never infer success from make's status."""
    missing = []
    observed = []
    for name, label in measurements().items():
        match = re.search(r'^' + re.escape(label) + r': ([\d.]+)(?: |$)', text, re.M)
        if match and finite_number(match[1]):
            observed.append(name)
        else:
            missing.append(name)
    # Native 8 MiB ALL uses 15 sizes, HALF uses 14; mmap latency starts at
    # 512 KiB because upstream lat_mmap intentionally ignores smaller mappings.
    tables = {
        'mmap-lat': ('"mappings', 5), 'filesystem': ('"File system latency', 4),
        'tcp-bw': ('Socket bandwidth using localhost', 8),
        'file-read': ('"read bandwidth', 15),
        'file-open-read': ('"read open2close bandwidth', 15),
        'mmap-read': ('"Mmap read bandwidth', 15),
        'mmap-open-read': ('"Mmap read open2close bandwidth', 15),
        'bcopy': ('"libc bcopy unaligned', 14),
        'bcopy-aligned': ('"libc bcopy aligned', 14),
        'bzero': ('Memory bzero bandwidth', 15),
        'fcp': ('"unrolled bcopy unaligned', 14),
        'cp': ('"unrolled partial bcopy unaligned', 14),
        'frd': ('Memory read bandwidth', 15),
        'rd': ('Memory partial read bandwidth', 15),
        'fwr': ('Memory write bandwidth', 15),
        'wr': ('Memory partial write bandwidth', 15),
        'rdwr': ('Memory partial read/write bandwidth', 15),
    }
    for name, (heading, count) in tables.items():
        rows = table_rows(text, heading)
        sizes = [512 * 2**n for n in range(count)]
        if name == 'tcp-bw':
            rows = [r[:2] if len(r) == 3 and r[2] == 'MB/sec' else [] for r in rows]
        if name == 'mmap-lat':
            sizes = [512 * 1024 * 2**n for n in range(5)]
        elif name == 'tcp-bw':
            sizes = [1, 64, 128, 256, 512, 1024, 1437, 10 * 1024**2]
        if name == 'filesystem':
            valid = (len(rows) == 4 and [r[0] for r in rows] == ['0k', '1k', '4k', '10k']
                     and all(len(r) == 4 and all(finite_number(v) for v in r[1:]) for r in rows))
        else:
            # lib_timing prints six decimal places below 1 MB and two above it.
            valid = numeric_table(rows, [v / 1e6 for v in sizes], tolerance=.005,
                                  small_tolerance=.00000051)
        (observed if valid else missing).append(name)
    for size in (0, 4, 8, 16, 32, 64):
        match = re.search(r'^"size=' + str(size) + r'k ovr=[^\n]+\n((?:[\d.]+ [\d.]+\n)+)', text, re.M)
        rows = [r.split() for r in match[1].splitlines()] if match else []
        valid = numeric_table(rows, [2, 4, 8, 16, 24, 32, 64, 96], tolerance=0)
        (observed if valid else missing).append('ctx-' + str(size))
    for name, pattern in {
        'file-write': r'^File .+ write bandwidth: ([\d.]+) KB/sec$',
        'pagefault': r'^Pagefaults on .+: ([\d.]+) microseconds$',
        'http': r'^Avg xfer: ([\d.]+)KB, ([\d.]+)KB in ([\d.]+) millisecs, ([\d.]+) (?:M|K)B/sec$',
        'tlb': r'^tlb: ([0-9]+) pages(?: ([\d.]+) nanoseconds)?$',
    }.items():
        match = re.search(pattern, text, re.M)
        valid = match and all(finite_number(v) for v in match.groups() if v is not None)
        if valid and name == 'tlb':
            valid = int(match[1]) > 0
        if valid and name == 'http':
            valid = float(match[3]) > 0
        (observed if valid else missing).append(name)
    for name, heading, stride in [('memory-latency', 'Memory load latency', 128),
                                  ('random-latency', 'Random load latency', 16)]:
        sizes = []
        size = 512
        while size <= 8 * 1024**2:
            sizes.append(size / 1024**2)
            if size < 1024:
                size *= 2
            elif size < 4096:
                size += 1024
            else:
                step = 32 * 1024
                while step <= size:
                    step *= 2
                size += step // 16
        rows = table_rows(text, heading + '\n"stride=' + str(stride))
        (observed if numeric_table(rows, sizes, tolerance=.0000051) else missing).append(name)
    line = re.search(r'^\[LINE_SIZE: ([0-9]+)\]$', text, re.M)
    sizes = []
    if line:
        size = 16 * int(line[1])
        while 0 < size <= 8 * 1024**2:
            sizes.append(size / 1e6)
            size *= 2
    valid = bool(sizes) and numeric_table(table_rows(text, 'Memory load parallelism'),
                                         sizes, tolerance=.00000051)
    (observed if valid else missing).append('memory-parallelism')
    errors = [line for line in (text + '\n' + stderr).splitlines()
              if re.search(r'Usage:|not found|cannot |could not |[Cc]onnection refused|'
                           r'[Tt]imed out|error in|RPC:|unable to|Segmentation fault|'
                           r'Bus error|Killed', line)
              and not line.startswith('Warning: cannot open /proc/net/dev')]
    warnings = [line for line in text.splitlines()
                if 'Inappropriate ioctl' in line or line.startswith('Warning:')]
    complete = status == 0 and not timed_out and not missing and not errors
    return {'passed': complete, 'native_exit_status': status, 'timed_out': timed_out,
            'observed': observed, 'missing': missing, 'errors': errors,
            'metadata_warnings': list(dict.fromkeys(warnings)),
            'scope': 'native ALL, 8 MiB, one copy, FASTMEM; no raw disks or remote hosts'}


def patch_scripts(root: Path) -> None:
    old = 'for server in $SERVERS; do $server -s; done'
    new = ('for server in $SERVERS; do\n'
           '    case "$server" in\n'
           '        lat_rpc) "$server" -s ;;\n'
           '        *) "$server" -s 127.0.0.1 ;;\n'
           '    esac\ndone')
    for script in (root / 'scripts/lmbench', root / 'bin' / PLATFORM / 'lmbench'):
        content = script.read_text()
        if content.count(old) != 1:
            raise ValueError('upstream server startup changed; review the adaptation')
        script.write_text(content.replace(old, new))
    version = root / 'scripts/version'
    old = "egrep 'MAJOR|MINOR'"
    content = version.read_text()
    if content.count(old) != 1:
        raise ValueError('upstream version probe changed; review the adaptation')
    version.write_text(content.replace(old, "grep -E 'MAJOR|MINOR'"))


def package(output: Path) -> None:
    repo = Path(__file__).resolve().parents[2]
    expression = repo / 'test/initramfs/nix/default.nix'
    packages = {}
    for name in ('lmbench', 'make', 'rpcbind', 'nettools', 'source'):
        packages[name] = subprocess.check_output(
            ['nix-build', str(expression), '--argstr', 'target', 'riscv64',
             '-A', 'lmbenchNative.' + name, '--no-out-link'], text=True).strip().splitlines()[-1]
    closure = subprocess.check_output(['nix-store', '-qR', *[v for k, v in packages.items()
                                                          if k != 'source']], text=True).splitlines()
    with tempfile.TemporaryDirectory(prefix='lmbench-native-') as tmp:
        root = Path(tmp) / 'lmbench'
        shutil.copytree(packages['source'], root)
        for p in [root, *root.rglob('*')]:
            if not p.is_symlink():
                p.chmod(p.stat().st_mode | 0o200)
        hashes = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in (root / 'scripts').iterdir() if p.is_file()}
        hashes['src/Makefile'] = hashlib.sha256((root / 'src/Makefile').read_bytes()).hexdigest()
        bindir = root / 'bin' / PLATFORM
        shutil.copytree(Path(packages['lmbench']) / 'bin', bindir)
        # Preserve the built driver's <version> substitution from bk.ver.
        patch_scripts(root)
        shutil.copy2(__file__, root / 'asterinas-native.py')
        (root / 'src/GNUmakefile').write_text(make_entry())
        (root / 'activate').write_text('export PATH=' + packages['make'] + '/bin:"$PATH"\n')
        metadata = {'revision': REVISION, 'platform': PLATFORM, 'packages': packages,
                    'closure': sorted(set(closure)), 'upstream_script_sha256': hashes,
                    'adaptations': ['explicit IPv4 loopback server arguments',
                                    'grep -E instead of the egrep shell wrapper'],
                    'benchmark_binaries_modified': False,
                    'binary_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                      for p in bindir.iterdir()
                                      if p.is_file() and p.read_bytes().startswith(b'\x7fELF')}}
        (root / 'asterinas-runtime.json').write_text(json.dumps(metadata, indent=2) + '\n')
        output.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(output, 'w:gz') as tar:
            for path in sorted(set(closure)):
                tar.add(path, arcname=path.lstrip('/'))
            tar.add(root, arcname='opt/lmbench')
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + '.sha256').write_text(digest + '  ' + output.name + '\n')
    print(json.dumps({'archive': str(output), 'sha256': digest}))


def stop_group(process: subprocess.Popen) -> None:
    # Servers fork into the same process group; clean up even after make exits.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


def run() -> int:
    root = Path(__file__).resolve().parent
    metadata = json.loads((root / 'asterinas-runtime.json').read_text())
    packages = metadata['packages']
    with (root / '.asterinas-run.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        old_handler = signal.signal(signal.SIGTERM, termination_requested)
        try:
            with owned_hello(Path('/tmp/hello'), (root / 'bin' / PLATFORM / 'hello').read_bytes()):
                return run_locked(root, packages, metadata)
        finally:
            signal.signal(signal.SIGTERM, old_handler)


def run_locked(root: Path, packages: dict, metadata: dict) -> int:
    logs = root / 'asterinas-runs'
    logs.mkdir(exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix='run-', dir=logs))
    report = {'passed': False, 'revision': metadata['revision'], 'output': str(out),
              'uname': list(os.uname()), 'adaptations': metadata.get('adaptations', []),
              'binary_sha256': metadata.get('binary_sha256', {})}
    processes = []
    start = time.monotonic()
    try:
        timeout = float(os.environ.get('LMBENCH_TIMEOUT', '900'))
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('LMBENCH_TIMEOUT must be finite and positive')
        pwd.getpwnam('rpc')  # Provision once during guest installation, never a login account.
        # Native lat_proc ignores the child's exit status. Verify its executable.
        check = subprocess.run(['/tmp/hello'],
                               capture_output=True, text=True, timeout=5)
        if check.returncode or check.stdout != 'Hello world\n':
            raise RuntimeError('native hello executable preflight failed')
        fs = out / 'fs'
        fs.mkdir()
        env = os.environ.copy()
        for key in ('LOOP_O', 'TIMING_O', 'LMBENCH_SCHED', 'MAKEFLAGS', 'MFLAGS'):
            env.pop(key, None)
        env.update(ENOUGH='10000', LC_ALL='C', PATH=':'.join(
            packages[p] + '/bin' for p in ('make', 'rpcbind', 'nettools')) + ':' + env['PATH'])
        answers = '\n'.join(['1', '1', '8', 'all', 'yes', 'no', '', '', '',
                             str(fs), str(out / 'status.log'), 'no']) + '\n'
        (out / 'answers.txt').write_text(answers)
        results = root / 'results' / PLATFORM
        before = set(results.glob('*')) if results.exists() else set()
        with (out / 'rpcbind.log').open('w') as rpc_log, \
                (out / 'stdout').open('w') as stdout, (out / 'stderr').open('w') as stderr:
            rpc = subprocess.Popen([packages['rpcbind'] + '/bin/rpcbind', '-f'],
                                   stdout=rpc_log, stderr=subprocess.STDOUT, start_new_session=True)
            processes.append(rpc)
            time.sleep(.2)
            if rpc.poll() is not None:
                raise RuntimeError('rpcbind exited; inspect rpcbind.log and the system journal')
            argv = native_command(packages['make'] + '/bin/make', PLATFORM)
            report['argv'] = argv
            proc = subprocess.Popen(argv, cwd=root / 'src', env=env, stdin=subprocess.PIPE,
                                    stdout=stdout, stderr=stderr, text=True, start_new_session=True)
            processes.append(proc)
            timed_out = False
            try:
                proc.communicate(answers, timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                stop_group(proc)
            raw = sorted(set(results.glob('*')) - before)
            text = '\n'.join(p.read_text(errors='replace') for p in raw if p.is_file())
            (out / 'native-results.txt').write_text(text)
            report.update(audit(text, (out / 'stderr').read_text(), proc.returncode, timed_out))
            report['native_result_files'] = [str(p) for p in raw]
            config = root / 'bin' / PLATFORM / ('CONFIG.' + os.uname().nodename)
            if config.exists():
                shutil.copy2(config, out / 'CONFIG')
    except (OSError, KeyError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        report['error'] = str(error)
        report['passed'] = False
    finally:
        for process in reversed(processes):
            try:
                stop_group(process)
            except (OSError, subprocess.SubprocessError) as error:
                report.setdefault('cleanup_errors', []).append(str(error))
                report['passed'] = False
        report['elapsed_seconds'] = round(time.monotonic() - start, 3)
        (out / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2), flush=True)
    return 0 if report['passed'] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    pack = sub.add_parser('package')
    pack.add_argument('--output', type=Path, required=True)
    sub.add_parser('run')
    args = parser.parse_args()
    if args.command == 'package':
        package(args.output.resolve())
        return 0
    return run()


if __name__ == '__main__':
    raise SystemExit(main())
