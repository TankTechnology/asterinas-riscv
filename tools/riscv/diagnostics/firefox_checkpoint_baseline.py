#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""One diagnostic command, three bounded checkpoints; never acceptance."""

import base64
import hashlib
import inspect
import json
from pathlib import Path
import re
import shlex
import sys
import time
import zlib


def observe_command(command, capture, launch, stop):
    capture("before")
    worker = launch()
    try:
        return command()
    finally:
        stop(worker)
        capture("after")


def encode_snapshot(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("snapshot exceeds decoded byte budget")
    packed = base64.b64encode(zlib.compress(raw)).decode()
    parts = [packed[i : i + 600] for i in range(0, len(packed), 600)]
    digest = hashlib.sha256(raw).hexdigest()
    return [
        f"A_FF_SNAPSHOT phase={value['phase']} part={i}/{len(parts)} sha256={digest} data={part}"
        for i, part in enumerate(parts)
    ]


class SnapshotReceiver:
    def __init__(self):
        self.phase = None
        self.parts = []
        self.completed = set()

    def accept(self, line):
        if not line.startswith("A_FF_SNAPSHOT "):
            return None
        match = re.fullmatch(
            r"A_FF_SNAPSHOT phase=(before|during|after) part=(\d+)/(\d+) "
            r"sha256=([0-9a-f]{64}) data=([A-Za-z0-9+/=]+)",
            line,
        )
        if match is None:
            raise ValueError("malformed snapshot frame")
        phase, index, total, digest, data = match.groups()
        index, total = int(index), int(total)
        if phase in self.completed or not 0 <= index < total <= 4096:
            raise ValueError("duplicate or oversized snapshot")
        if index == 0:
            if self.phase is not None:
                raise ValueError("incomplete preceding snapshot")
            self.phase, self.total, self.digest = phase, total, digest
        if (
            phase != self.phase
            or total != self.total
            or digest != self.digest
            or index != len(self.parts)
        ):
            raise ValueError("snapshot sequence mismatch")
        self.parts.append(data)
        if len(self.parts) != total:
            return None
        packed = base64.b64decode("".join(self.parts), validate=True)
        decoder = zlib.decompressobj()
        raw = decoder.decompress(packed, 2 * 1024 * 1024 + 1)
        if (
            len(raw) > 2 * 1024 * 1024
            or not decoder.eof
            or decoder.unused_data
            or hashlib.sha256(raw).hexdigest() != digest
        ):
            raise ValueError("snapshot integrity mismatch")
        value = json.loads(raw)
        if value.get("phase") != phase:
            raise ValueError("snapshot phase mismatch")
        self.completed.add(phase)
        self.phase, self.parts = None, []
        return value


def collection_limits():
    return dict(max_seconds=15, max_threads=384, max_total_bytes=1048576, max_fds=256)


def worker_source():
    return (
        """import base64, hashlib, json, os, runpy, sys, time, zlib
from pathlib import Path
"""
        + inspect.getsource(encode_snapshot)
        + inspect.getsource(collection_limits)
        + """
phase, root_pid, client_pid, delay = sys.argv[1:]
time.sleep(float(delay))
started = time.monotonic_ns()
print('A_FF_CHECKPOINT ' + json.dumps({'event':'capture_start','phase':phase,
    'monotonic_ns':started,'collector_pid':os.getpid()}), flush=True)
module = runpy.run_path('/usr/lib/asterinas/firefox-diagnostic-snapshot')
tree = module['collect_snapshot'](int(root_pid), limits=module['Limits'](**collection_limits()))
reader = module['Reader'](module['Limits'](max_total_bytes=32768, max_seconds=1))
path = Path('/proc') / client_pid
identity = module['_identity'](reader, path)
client = {'pid':int(client_pid), 'identity':identity,
    'status':module['_status'](reader,path), 'syscall':module['_syscall'](reader,path)}
after = module['_identity'](reader,path)
client['identity_verified_after'] = identity is not None and identity == after
if not client['identity_verified_after']:
    client = {'pid':int(client_pid),'status':'identity_unverified'}
value = {'version':1, 'physical':False, 'phase':phase,
    'started_monotonic_ns':started,'finished_monotonic_ns':time.monotonic_ns(),
    'tree':tree,'client':client}
for line in encode_snapshot(value):
    print(line, flush=True)
print('A_FF_CHECKPOINT ' + json.dumps({'event':'capture_end','phase':phase,
    'monotonic_ns':time.monotonic_ns(),'complete':tree['complete'],
    'limitations':tree['limitations'],'processes':len(tree['processes'])}), flush=True)
"""
    )


def guest_source():
    return (
        """import os, runpy, signal, subprocess, sys, time, json
sys.path.insert(0, '/usr/lib/asterinas')
module = runpy.run_path('/usr/lib/asterinas/physical-graphics-gate')
import browser_m5_marionette_gate as transport
os.environ['ASTERINAS_MARIONETTE_DIAGNOSTICS'] = '1'
os.environ.pop('ASTERINAS_MARIONETTE_DEBUG_ERRORS', None)
"""
        + inspect.getsource(observe_command)
        + f"WORKER = {worker_source()!r}\n"
        + """
root_pid = sys.argv[sys.argv.index('--firefox-pid') + 1]
client_pid = str(os.getpid())
observed = False
original = transport.Marionette.command

def report(**values):
    print('A_FF_CHECKPOINT ' + json.dumps(dict(values,monotonic_ns=time.monotonic_ns())),flush=True)

def spawn(phase, delay):
    return subprocess.Popen(['timeout','-k','2',str(float(delay)+22),sys.executable,
        '-c',WORKER,phase,root_pid,client_pid,str(delay)],start_new_session=True)

def stop(worker):
    try:
        if worker.poll() is None:
            os.killpg(worker.pid,signal.SIGTERM)
            try:
                worker.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(worker.pid,signal.SIGKILL)
                worker.wait(timeout=2)
            report(event='during_cancelled_or_interrupted',returncode=worker.returncode)
        else:
            report(event='during_worker_exit',returncode=worker.returncode)
    except (OSError,subprocess.TimeoutExpired) as error:
        report(event='cleanup_error',error_type=type(error).__name__)

def capture(phase):
    worker = None
    try:
        worker = spawn(phase,0)
        report(event='worker_exit',phase=phase,returncode=worker.wait(timeout=27))
    except (OSError,subprocess.TimeoutExpired) as error:
        report(event='capture_error',phase=phase,error_type=type(error).__name__)
        if worker is not None:
            stop(worker)

def command(client,name,parameters=None):
    global observed
    if observed or name != 'WebDriver:ExecuteScript':
        return original(client,name,parameters)
    observed = True
    report(event='command_selected',client_pid=os.getpid(),root_pid=int(root_pid),
        request_id=client._next_id,command=name)
    def invoke():
        report(event='command_enter',request_id=client._next_id)
        try:
            result = original(client,name,parameters)
        except BaseException as error:
            report(event='command_error',error_type=type(error).__name__)
            raise
        report(event='command_return',result_type=type(result).__name__)
        return result
    observe_command(invoke,capture,lambda:spawn('during',30),stop)
    raise module['GateError']('checkpoint-diagnostic-complete; not browser acceptance')

transport.Marionette.command = command
raise SystemExit(module['main']())
"""
    )


def guest_loader():
    packed = base64.b64encode(zlib.compress(guest_source().encode(), 9)).decode()
    return f"import base64,zlib;exec(zlib.decompress(base64.b64decode({packed!r})))"


def diagnostic_bootargs(bootargs):
    if bootargs.count("loglevel=off") != 1 or bootargs.count(" -- ") != 1:
        raise RuntimeError("unexpected boot argument contract")
    return bootargs.replace(" -- ", " asterinas.syscall_diag=1 -- ", 1)


def main():
    from tools.riscv import physical_graphics_qemu_gate as gate

    config = gate.parse_gate_args(sys.argv[1:])
    started = time.monotonic()
    receiver = SnapshotReceiver()
    checkpoints, framing_errors = [], []

    def report(message):
        print(
            f"FF_CHECKPOINT_HOST elapsed={time.monotonic() - started:.3f} {message}",
            flush=True,
        )

    original_argv = gate.physical_graphics_qemu_argv
    original_command = gate.physical_cycle_command
    original_next = gate._next_line
    original_cycle = gate.PhysicalGraphicsQemuOperations._run_interaction_cycle

    def argv(**arguments):
        values = list(original_argv(**arguments))
        roots = [i for i, value in enumerate(values) if ",id=rootdisk," in value]
        if len(roots) != 1 or not values[roots[0]].endswith(",cache=directsync"):
            raise RuntimeError("unexpected root run-copy cache contract")
        values[roots[0]] = (
            values[roots[0]].removesuffix(",cache=directsync") + ",cache=writeback"
        )
        return tuple(values)

    def command(*args, **kwargs):
        value = original_command(*args, **kwargs)
        executable = "/usr/lib/asterinas/physical-graphics-gate "
        if value.count(executable) != 1:
            raise RuntimeError("unexpected guest command contract")
        value = value.replace(
            executable, "python3 -c " + shlex.quote(guest_loader()) + " ", 1
        )
        if len(value.encode()) > 4000:
            raise RuntimeError("guest command exceeds serial line budget")
        report(f"command_bytes={len(value.encode())}")
        return value

    def next_line(*args, **kwargs):
        line, cursor = original_next(*args, **kwargs)
        if line.startswith("A_FF_SNAPSHOT "):
            try:
                value = receiver.accept(line)
                if value is not None:
                    value["host_received_monotonic_ns"] = time.monotonic_ns()
                    (
                        config.output_directory
                        / ("snapshot-" + value["phase"] + ".json")
                    ).write_text(json.dumps(value, indent=2) + "\n")
                    checkpoints.append(value["phase"])
                    report(
                        f"snapshot_saved={value['phase']} processes={len(value['tree']['processes'])} limitations={value['tree']['limitations']}"
                    )
            except ValueError as error:
                framing_errors.append(str(error))
                report(f"framing_error={error}")
        elif line.startswith(
            (
                "A_FF_CHECKPOINT ",
                "A_WEB_MARIONETTE_TRANSPORT ",
                "ASTERINAS_PHYSICAL_",
                "__ASTERINAS_PHYSICAL_COMMAND_STATUS__",
            )
        ):
            report(line)
        return line, cursor

    def cycle(self, session, config, **kwargs):
        try:
            return original_cycle(self, session, config, **kwargs)
        finally:
            try:
                path = config.output_directory / "diagnostic-failure.ppm"
                session["monitor"].command(
                    "screendump " + json.dumps(str(path)), time.monotonic() + 10
                )
            except Exception as error:
                report(f"framebuffer_error={type(error).__name__}")

    bootargs = gate.PhysicalGraphicsQemuOperations.BOOTARGS
    gate.PhysicalGraphicsQemuOperations.BOOTARGS = diagnostic_bootargs(bootargs)
    gate.physical_graphics_qemu_argv = argv
    gate.physical_cycle_command = command
    gate._next_line = next_line
    gate.PhysicalGraphicsQemuOperations._run_interaction_cycle = cycle
    result = gate.main(sys.argv[1:])
    record = {
        "experimental": True,
        "physical": False,
        "browser_acceptance": False,
        "exit_status": result,
        "checkpoints": checkpoints,
        "framing_errors": framing_errors,
        "incomplete_frame_phase": receiver.phase,
        "elapsed_seconds": time.monotonic() - started,
        "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "guest_sha256": hashlib.sha256(guest_source().encode()).hexdigest(),
        "bootargs": gate.PhysicalGraphicsQemuOperations.BOOTARGS,
    }
    (config.output_directory / "checkpoint-experiment.json").write_text(
        json.dumps(record, indent=2) + "\n"
    )
    report(json.dumps(record))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
