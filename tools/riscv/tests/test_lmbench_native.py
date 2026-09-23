# SPDX-License-Identifier: MPL-2.0

import subprocess
import os
import signal
import sys
import time
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import json

from tools.riscv.lmbench_native import audit, make_entry, native_command, owned_hello, run_locked, PLATFORM, patch_scripts


class NativeLmbenchTests(unittest.TestCase):
    def test_native_target_skips_only_compilation(self):
        self.assertEqual(native_command('/bin/make', 'riscv64-unknown-linux-gnu'),
                         ['/bin/make', '--no-print-directory', '-f', 'Makefile',
                          '-o', 'lmbench', 'OS=riscv64-unknown-linux-gnu', 'results'])

    def test_zero_exit_with_empty_results_is_incomplete(self):
        report = audit('', '', 0, False)
        self.assertFalse(report['passed'])
        self.assertIn('syscall-null', report['missing'])

    def test_measurement_does_not_hide_network_error(self):
        report = audit('Simple syscall: 4 microseconds\nUsage: lat_tcp -s serverhost\n', '', 0, False)
        self.assertNotIn('syscall-null', report['missing'])
        self.assertTrue(report['errors'])
        self.assertFalse(report['passed'])

    def test_usage_or_nan_is_not_a_measurement(self):
        for value in ('nan', 'inf', '-1'):
            report = audit(f'Simple syscall: {value} microseconds\n', '', 0, False)
            self.assertIn('syscall-null', report['missing'])

    def test_truncated_table_is_incomplete(self):
        report = audit('"read bandwidth\n0.000512 100\n', '', 0, False)
        self.assertIn('file-read', report['missing'])

    def test_titles_do_not_replace_memory_curves(self):
        report = audit('Memory load latency\nRandom load latency\n', '', 0, False)
        self.assertIn('memory-latency', report['missing'])
        self.assertIn('random-latency', report['missing'])

    def test_stream_requires_both_versions_and_all_operations(self):
        report = audit('STREAM copy latency: 1 nanoseconds\n', '', 0, False)
        self.assertIn('stream-triad-latency', report['missing'])
        self.assertIn('stream2-sum-bandwidth', report['missing'])

    def test_table_with_repeated_sizes_is_incomplete(self):
        report = audit('"read bandwidth\n' + '0.000512 100\n' * 15, '', 0, False)
        self.assertIn('file-read', report['missing'])

    def test_helper_cleanup_after_interruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / 'hello'
            with self.assertRaises(RuntimeError):
                with owned_hello(helper, b'fixture'):
                    self.assertEqual(helper.read_bytes(), b'fixture')
                    raise RuntimeError('interrupted')
            self.assertFalse(helper.exists())
            with owned_hello(helper, b'fixture'):
                pass

    def test_helper_does_not_overwrite_dangling_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / 'hello'
            helper.symlink_to(Path(tmp) / 'missing')
            with self.assertRaises(FileExistsError):
                with owned_hello(helper, b'fixture'):
                    pass
            self.assertTrue(helper.is_symlink())

    def test_helper_preserves_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / 'hello'
            with owned_hello(helper, b'fixture'):
                helper.unlink()
                helper.write_bytes(b'other')
            self.assertEqual(helper.read_bytes(), b'other')

    def test_summary_measurements_require_valid_numbers(self):
        report = audit('Avg xfer: 3.2KB, 41.8KB in 10 millisecs, inf MB/sec\n'
                       'tlb: \nFile x write bandwidth: 1.2.3 KB/sec\n'
                       'Pagefaults on x: 1.2.3 microseconds\n', '', 0, False)
        for name in ('http', 'tlb', 'file-write', 'pagefault'):
            self.assertIn(name, report['missing'])

    def test_http_accepts_native_kilobytes_per_second(self):
        report = audit('Avg xfer: 3.2KB, 41.8KB in 100 millisecs, 418.0 KB/sec\n', '', 0, False)
        self.assertNotIn('http', report['missing'])

    def test_tcp_bandwidth_accepts_native_units(self):
        sizes = [1, 64, 128, 256, 512, 1024, 1437, 10 * 1024**2]
        text = 'Socket bandwidth using localhost\n' + ''.join(
            f'{size / 1e6:.6f} 2.00 MB/sec\n' for size in sizes) + '\n'
        self.assertNotIn('tcp-bw', audit(text, '', 0, False)['missing'])

    def test_timeout_and_nonzero_are_preserved(self):
        report = audit('', '', -9, True)
        self.assertEqual(report['native_exit_status'], -9)
        self.assertTrue(report['timed_out'])

    def test_sigterm_reaps_child_and_removes_owned_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = """
import signal, subprocess, sys, time
from pathlib import Path
from tools.riscv.lmbench_native import owned_hello, stop_group, termination_requested
root = Path(sys.argv[1])
signal.signal(signal.SIGTERM, termination_requested)
try:
    with owned_hello(root / 'hello', b'fixture'):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
        try:
            (root / 'pid').write_text(str(child.pid))
            time.sleep(60)
        finally:
            stop_group(child)
except RuntimeError:
    pass
"""
            proc = subprocess.Popen([sys.executable, '-c', source, str(root)])
            try:
                deadline = time.monotonic() + 3
                while not (root / 'pid').exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                child_pid = int((root / 'pid').read_text())
                proc.send_signal(signal.SIGTERM)
                self.assertEqual(proc.wait(timeout=3), 0)
                self.assertFalse((root / 'hello').exists())
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()

    def test_artifact_failure_after_audit_cannot_report_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / 'bin' / PLATFORM / ('CONFIG.' + os.uname().nodename)
            config.parent.mkdir(parents=True)
            config.write_text('MAIL=no\n')
            rpc = Mock(pid=123, poll=Mock(return_value=None))
            make = Mock(pid=124, returncode=0)
            packages = {name: tmp for name in ('make', 'rpcbind', 'nettools')}
            with patch('tools.riscv.lmbench_native.pwd.getpwnam'), \
                    patch('tools.riscv.lmbench_native.subprocess.run',
                          return_value=subprocess.CompletedProcess([], 0, 'Hello world\n')), \
                    patch('tools.riscv.lmbench_native.subprocess.Popen', side_effect=[rpc, make]), \
                    patch('tools.riscv.lmbench_native.time.sleep'), \
                    patch('tools.riscv.lmbench_native.stop_group'), \
                    patch('tools.riscv.lmbench_native.audit', return_value={'passed': True}), \
                    patch('tools.riscv.lmbench_native.shutil.copy2', side_effect=OSError('disk full')), \
                    patch('builtins.print'):
                status = run_locked(root, packages, {'revision': 'fixture'})
            report = json.loads(next(root.rglob('report.json')).read_text())
            self.assertEqual(status, 1)
            self.assertFalse(report['passed'])
            self.assertEqual(report['error'], 'disk full')

    def test_adaptation_preserves_built_version_substitution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'scripts').mkdir()
            binary = root / 'bin' / PLATFORM
            binary.mkdir(parents=True)
            startup = 'for server in $SERVERS; do $server -s; done\n'
            (root / 'scripts/lmbench').write_text('echo <version>\n' + startup)
            (binary / 'lmbench').write_text('echo built-version\n' + startup)
            (root / 'scripts/version').write_text("egrep 'MAJOR|MINOR' version.h\n")
            patch_scripts(root)
            self.assertIn('echo built-version', (binary / 'lmbench').read_text())
            self.assertNotIn('<version>', (binary / 'lmbench').read_text())
            self.assertIn('-s 127.0.0.1', (binary / 'lmbench').read_text())

    def test_invalid_timeout_fails_before_starting_processes(self):
        for value in ('nan', 'inf', '0', '-1', 'invalid'):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with patch.dict(os.environ, {'LMBENCH_TIMEOUT': value}), \
                        patch('tools.riscv.lmbench_native.subprocess.Popen') as popen, \
                        patch('builtins.print'):
                    self.assertEqual(run_locked(root, {}, {'revision': 'fixture'}), 1)
                    popen.assert_not_called()
                self.assertFalse(json.loads(next(root.rglob('report.json')).read_text())['passed'])

    def test_make_entry_uses_supervisor_without_running_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'src').mkdir()
            (root / 'src/Makefile').write_text('lmbench:\n\tfalse\nresults: lmbench\n\t@echo NATIVE_RESULT\n')
            (root / 'asterinas-native.py').write_text(
                'import subprocess,sys\n'
                'assert sys.argv[1:] == ["run"]\n'
                'subprocess.run(["make","-f","Makefile","-o","lmbench","results"],check=True)\n')
            (root / 'src/GNUmakefile').write_text(make_entry())
            for target in ('results', 'result'):
                with self.subTest(target=target):
                    run = subprocess.run(['make', target], cwd=root / 'src',
                                         capture_output=True, text=True, timeout=3)
                    self.assertEqual(run.returncode, 0, run.stderr)
                    self.assertIn('NATIVE_RESULT', run.stdout)


if __name__ == '__main__':
    unittest.main()
