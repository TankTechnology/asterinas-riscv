#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Run one bounded Firefox/dmesg correlation experiment; never acceptance."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.riscv.diagnostics.firefox_actor_records import ActorRecords
from tools.riscv.diagnostics.firefox_dmesg_records import DmesgFrames, correlate
from tools.riscv.diagnostics.firefox_transport_records import FirefoxTransportRecords


REPOSITORY = Path(__file__).resolve().parents[3]
DEFAULT_BASELINE = (
    REPOSITORY
    / "target/firefox-diagnostics-20260908/browser_checkpoint_experiment-wide.py"
)
BASELINE_SHA256 = "fc154e4d2deb72a9ca881320dce9da108cff8b2421b39d0dc112a6238ebec01a"
GUEST_HELPER = Path(__file__).with_name("firefox_dmesg_guest.py")
SERIAL_COMMAND_LIMIT = 4000
TRANSPORT_PREFIX = "A_WEB_MARIONETTE_TRANSPORT "
CHECKPOINT_PREFIX = "A_FF_CHECKPOINT "


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def replace_exact(source, before, after):
    if source.count(before) != 1:
        raise ValueError(f"source anchor must occur exactly once: {before[:80]!r}")
    return source.replace(before, after, 1)


def load_baseline(path=DEFAULT_BASELINE):
    path = Path(path)
    if not path.is_file() or sha256(path) != BASELINE_SHA256:
        raise ValueError("baseline checkpoint runner hash mismatch")
    spec = importlib.util.spec_from_file_location("firefox_dmesg_baseline", path)
    if spec is None or spec.loader is None:
        raise ValueError("baseline checkpoint runner cannot be loaded")
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    return baseline


def transform_worker_source(source):
    source = replace_exact(
        source,
        "import base64, hashlib, json, os, runpy, sys, time, zlib\n"
        "from pathlib import Path\n",
        "import base64, hashlib, json, os, runpy, sys, time, zlib\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, '/usr/lib/asterinas')\n"
        "from firefox_dmesg_guest import write_marker\n",
    )
    source = replace_exact(
        source,
        "phase, root_pid, client_pid, delay = sys.argv[1:]\n"
        "time.sleep(float(delay))\n"
        "started = time.monotonic_ns()",
        "phase, root_pid, client_pid, delay, request_id = sys.argv[1:]\n"
        "time.sleep(float(delay))\n"
        "start_marker = {'before':'snapshot_before_start',"
        "'during':'snapshot_during_start','after':'snapshot_after_start'}[phase]\n"
        "end_marker = {'before':'snapshot_before_end',"
        "'during':'snapshot_during_end','after':'snapshot_after_end'}[phase]\n"
        "write_marker(start_marker, request_id, root_pid, client_pid)\n"
        "started = time.monotonic_ns()",
    )
    source = replace_exact(
        source,
        "value = {'version':1, 'physical':False, 'phase':phase,\n"
        "    'started_monotonic_ns':started,'finished_monotonic_ns':time.monotonic_ns(),\n"
        "    'tree':tree,'client':client}\n"
        "for line in encode_snapshot(value):",
        "value = {'version':1, 'physical':False, 'phase':phase,\n"
        "    'started_monotonic_ns':started,'finished_monotonic_ns':time.monotonic_ns(),\n"
        "    'tree':tree,'client':client}\n"
        "write_marker(end_marker, request_id, root_pid, client_pid)\n"
        "for line in encode_snapshot(value):",
    )
    return source


def transform_guest_source(source, *, original_worker, transformed_worker):
    source = replace_exact(
        source,
        f"WORKER = {original_worker!r}\n",
        f"WORKER = {transformed_worker!r}\n",
    )
    source = replace_exact(
        source,
        "import browser_m5_marionette_gate as transport\n",
        "import browser_m5_marionette_gate as transport\n"
        "from firefox_dmesg_guest import BoundedDmesg\n",
    )
    source = replace_exact(
        source,
        "os.environ['ASTERINAS_MARIONETTE_DIAGNOSTICS'] = '1'\n",
        "os.environ['ASTERINAS_MARIONETTE_DIAGNOSTICS'] = '1'\n"
        "os.environ['ASTERINAS_FIREFOX_ACTOR_DIAGNOSTICS'] = '1'\n",
    )
    source = replace_exact(
        source,
        "client_pid = str(os.getpid())\nobserved = False",
        "client_pid = str(os.getpid())\n"
        "collector = BoundedDmesg(firefox_pid=int(root_pid), client_pid=int(client_pid))\n"
        "collector.start(['/usr/bin/dmesg','--follow-new','--raw'])\n"
        "selected_request_id = 0\n"
        "observed = False",
    )
    source = replace_exact(
        source,
        "def spawn(phase, delay):\n"
        "    return subprocess.Popen(['timeout','-k','2',str(float(delay)+22),sys.executable,\n"
        "        '-c',WORKER,phase,root_pid,client_pid,str(delay)],start_new_session=True)",
        "def spawn(phase, delay):\n"
        "    return subprocess.Popen(['timeout','-k','2',str(float(delay)+45),sys.executable,\n"
        "        '-c',WORKER,phase,root_pid,client_pid,str(delay),str(selected_request_id)],"
        "start_new_session=True)",
    )
    source = replace_exact(
        source,
        "        report(event='worker_exit',phase=phase,returncode=worker.wait(timeout=27))",
        "        report(event='worker_exit',phase=phase,returncode=worker.wait(timeout=50))",
    )
    source = replace_exact(
        source,
        "    global observed\n",
        "    global observed, selected_request_id\n",
    )
    source = replace_exact(
        source,
        "    report(event='command_selected',client_pid=os.getpid(),root_pid=int(root_pid),\n"
        "        request_id=client._next_id,command=name)\n"
        "    def invoke():",
        "    selected_request_id = client._next_id\n"
        "    collector.set_request(selected_request_id)\n"
        "    collector.mark('request_selected')\n"
        "    report(event='command_selected',client_pid=os.getpid(),root_pid=int(root_pid),\n"
        "        request_id=selected_request_id,command=name,transport_budget_seconds=300)\n"
        "    def invoke():",
    )
    source = replace_exact(
        source,
        "    def invoke():\n"
        "        report(event='command_enter',request_id=client._next_id)\n"
        "        try:\n"
        "            result = original(client,name,parameters)\n"
        "        except BaseException as error:\n"
        "            report(event='command_error',error_type=type(error).__name__)\n"
        "            raise\n"
        "        report(event='command_return',result_type=type(result).__name__)\n"
        "        return result",
        "    def invoke():\n"
        "        client.set_timeout(300.0)\n"
        "        collector.mark('request_enter')\n"
        "        report(event='command_enter',request_id=selected_request_id,"
        "transport_budget_seconds=300)\n"
        "        try:\n"
        "            result = original(client,name,parameters)\n"
        "        except BaseException as error:\n"
        "            collector.mark('request_error')\n"
        "            report(event='command_error',error_type=type(error).__name__)\n"
        "            raise\n"
        "        collector.mark('request_return')\n"
        "        report(event='command_return',result_type=type(result).__name__)\n"
        "        return result",
    )
    source = replace_exact(
        source,
        "observe_command(invoke,capture,lambda:spawn('during',30),stop)",
        "observe_command(invoke,capture,lambda:spawn('during',1),stop)",
    )
    source = replace_exact(
        source,
        "transport.Marionette.command = command\nraise SystemExit(module['main']())\n",
        "transport.Marionette.command = command\n"
        "collector_exported = False\n"
        "def export_collector():\n"
        "    global collector_exported\n"
        "    if collector_exported:\n"
        "        return\n"
        "    collector.mark('collector_stopping')\n"
        "    collector.stop_and_emit()\n"
        "    collector_exported = True\n"
        "original_print = print\n"
        "def terminal_print(*args, **kwargs):\n"
        "    line = args[0] if len(args) == 1 and isinstance(args[0], str) else ''\n"
        "    if line.startswith('ASTERINAS_PHYSICAL_GRAPHICS_FAIL '):\n"
        "        export_collector()\n"
        "    return original_print(*args, **kwargs)\n"
        "module['main'].__globals__['print'] = terminal_print\n"
        "exit_code = 1\n"
        "try:\n"
        "    exit_code = module['main']()\n"
        "finally:\n"
        "    export_collector()\n"
        "raise SystemExit(exit_code)\n",
    )
    return source


def build_guest_source(baseline):
    original_worker = baseline.worker_source()
    transformed_worker = transform_worker_source(original_worker)
    return transform_guest_source(
        baseline.guest_source(),
        original_worker=original_worker,
        transformed_worker=transformed_worker,
    )


def guest_loader(baseline):
    # The generated source is installed in the hash-audited diagnostic rootfs.
    # Keeping only this fixed loader on the serial command line leaves ample
    # room for the existing bounded physical-gate arguments.
    del baseline
    return (
        "import runpy;runpy.run_path("
        "'/usr/lib/asterinas/firefox-dmesg-driver.py',run_name='__main__')"
    )


def build_serial_command(original, baseline):
    executable = "/usr/lib/asterinas/physical-graphics-gate "
    if original.count(executable) != 1:
        raise RuntimeError("unexpected guest command contract")
    result = original.replace(
        executable, "python3 -c " + shlex.quote(guest_loader(baseline)) + " ", 1
    )
    if len(result.encode()) > SERIAL_COMMAND_LIMIT:
        raise RuntimeError("guest command exceeds serial line budget")
    return result


def diagnostic_bootargs(bootargs):
    additions = (
        "asterinas.klog_capture=info",
        "asterinas.syscall_diag=1",
        "asterinas.tcp_diagnostic_port=2828",
        "systemd.setenv=ASTERINAS_FIREFOX_ACTOR_DIAGNOSTICS=1",
    )
    if bootargs.count("loglevel=off") != 1 or bootargs.count(" -- ") != 1:
        raise RuntimeError("unexpected boot argument contract")
    if any(value in bootargs for value in additions):
        raise RuntimeError("diagnostic boot arguments already present")
    return bootargs.replace(" -- ", " " + " ".join(additions) + " -- ", 1)


def prepare_output(path):
    path = Path(path)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        if not path.is_dir():
            raise
    if any(path.iterdir()):
        raise FileExistsError(
            "Firefox dmesg experiment requires an empty output directory"
        )
    return path


def _transport_record(line):
    try:
        record = json.loads(line[len(TRANSPORT_PREFIX) :])
    except (ValueError, RecursionError) as error:
        raise ValueError("invalid Marionette transport JSON") from error
    required = {
        "version",
        "event",
        "pid",
        "monotonic_ns",
        "request_id",
        "command",
        "stage",
        "send_complete",
        "header_bytes",
        "body_expected",
        "body_received",
    }
    if (
        not isinstance(record, dict)
        or not required <= set(record)
        or set(record) - required - {"error_type", "errno"}
        or record.get("version") != 1
        or any(
            type(record.get(key)) is not int
            for key in (
                "pid",
                "monotonic_ns",
                "request_id",
                "header_bytes",
                "body_received",
            )
        )
        or type(record.get("send_complete")) is not bool
        or not isinstance(record.get("event"), str)
        or not isinstance(record.get("command"), str)
        or not isinstance(record.get("stage"), str)
        or not (
            record.get("body_expected") is None
            or type(record.get("body_expected")) is int
        )
    ):
        raise ValueError("invalid Marionette transport schema")
    return record


class SerialEvidence:
    def __init__(self, output, progress):
        self.cursor = 0
        self.dmesg = DmesgFrames()
        self.actors = ActorRecords()
        self.firefox_transport = FirefoxTransportRecords()
        self.transport = []
        self.checkpoints = []
        self.errors = []
        self.transcript = b""
        self.output = output
        self.progress = progress

    def consume(self, transcript, phase):
        self.transcript = bytes(transcript)
        for record in self.actors.consume(transcript):
            record["host_received_monotonic_ns"] = time.monotonic_ns()
            record["collection_phase"] = phase
        for record in self.firefox_transport.consume(transcript):
            record["host_received_monotonic_ns"] = time.monotonic_ns()
            record["collection_phase"] = phase
        while (end := transcript.find(b"\n", self.cursor)) != -1:
            start = self.cursor
            self.cursor = end + 1
            try:
                line = transcript[start:end].rstrip(b"\r").decode("utf-8")
            except UnicodeDecodeError:
                self.errors.append("serial evidence line is not UTF-8")
                continue
            try:
                if line.startswith(("A_FF_DMESG_META ", "A_FF_DMESG ")):
                    if self.dmesg.accept(line) is not None:
                        self.progress("dmesg_export_received")
                elif line.startswith(TRANSPORT_PREFIX):
                    record = _transport_record(line)
                    record["host_received_monotonic_ns"] = time.monotonic_ns()
                    self.transport.append(record)
                elif line.startswith(CHECKPOINT_PREFIX):
                    record = json.loads(line[len(CHECKPOINT_PREFIX) :])
                    if not isinstance(record, dict):
                        raise ValueError("checkpoint is not an object")
                    record["host_received_monotonic_ns"] = time.monotonic_ns()
                    self.checkpoints.append(record)
                    if record.get("event") == "capture_end":
                        self.progress("snapshot_" + str(record.get("phase")))
            except (ValueError, RecursionError) as error:
                if len(self.errors) < 32:
                    self.errors.append(str(error))


def _write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _write_jsonl(path, values):
    with Path(path).open("x", encoding="utf-8") as stream:
        for value in values:
            stream.write(
                json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
            )


def main(arguments=None):
    from tools.riscv import physical_graphics_qemu_gate as gate

    values = list(sys.argv[1:] if arguments is None else arguments)
    config = gate.parse_gate_args(values)
    output = prepare_output(config.output_directory)
    progress_value = {"version": 1, "physical": False, "stage": "prepared"}

    def progress(stage):
        progress_value.update(stage=stage, monotonic_ns=time.monotonic_ns())
        temporary = output / ".experiment-progress.json.tmp"
        _write_json(temporary, progress_value)
        os.replace(temporary, output / "experiment-progress.json")

    progress("packaging_validated")
    baseline = load_baseline()
    generated_source = build_guest_source(baseline)
    generated_loader = guest_loader(baseline)
    evidence = SerialEvidence(output, progress)
    original_next = gate._next_line
    original_drain = gate.PhysicalGraphicsQemuOperations.drain_serial

    def next_line(serial, *args, **kwargs):
        try:
            return original_next(serial, *args, **kwargs)
        finally:
            evidence.consume(serial.transcript, "protocol_scan")

    def drain(self, session, inner_config):
        try:
            return original_drain(self, session, inner_config)
        finally:
            evidence.consume(session["serial"].transcript, "final_drain")

    gate._next_line = next_line
    gate.PhysicalGraphicsQemuOperations.drain_serial = drain
    baseline.guest_loader = lambda: generated_loader
    baseline.diagnostic_bootargs = diagnostic_bootargs
    progress("boot_started")
    base_status = baseline.main()
    progress("base_gate_finished")

    classification = None
    complete = False
    errors = [
        *evidence.errors,
        *evidence.actors.errors,
        *evidence.firefox_transport.errors,
    ]
    snapshots = []
    for phase in ("before", "during", "after"):
        path = output / f"snapshot-{phase}.json"
        if not path.is_file():
            errors.append(f"missing {path.name}")
            continue
        try:
            snapshots.append(json.loads(path.read_text()))
        except (OSError, ValueError, RecursionError):
            errors.append(f"invalid {path.name}")
    try:
        evidence.dmesg.finish()
    except ValueError as error:
        errors.append(str(error))
    budget_records = [
        record
        for record in evidence.checkpoints
        if record.get("event") in ("command_selected", "command_enter")
    ]
    if not budget_records or any(
        record.get("transport_budget_seconds") != 300 for record in budget_records
    ):
        errors.append("selected request did not receive the fixed 300-second budget")
    if not errors:
        try:
            classification = correlate(
                evidence.dmesg.payload,
                evidence.actors.records,
                snapshots,
                evidence.transport,
            )
            complete = True
        except ValueError as error:
            errors.append(str(error))
    if classification is None:
        classification = {
            "version": 1,
            "physical": False,
            "browser_acceptance": False,
            "evidence_complete": False,
            "outcome": "evidence_incomplete",
            "first_missing_boundary": "collector",
            "selected_subsystem": None,
            "errors": errors,
        }

    if evidence.dmesg.payload is not None:
        (output / "dmesg.raw").write_bytes(evidence.dmesg.payload)
    if evidence.dmesg.metadata is not None:
        _write_json(output / "dmesg-meta.json", evidence.dmesg.metadata)
    _write_jsonl(output / "actor-stages.jsonl", evidence.actors.records)
    _write_jsonl(
        output / "firefox-transport-stages.jsonl",
        evidence.firefox_transport.records,
    )
    _write_jsonl(output / "transport-records.jsonl", evidence.transport)
    _write_jsonl(output / "checkpoint-records.jsonl", evidence.checkpoints)
    _write_json(output / "timeline.json", classification.get("timeline", []))
    _write_json(output / "classification.json", classification)
    (output / "full-serial.log").write_bytes(evidence.transcript)
    manifest = {
        "version": 1,
        "baseline_sha256": BASELINE_SHA256,
        "runner_sha256": sha256(Path(__file__)),
        "guest_helper_sha256": sha256(GUEST_HELPER),
        "guest_source_sha256": hashlib.sha256(generated_source.encode()).hexdigest(),
        "input_sha256": {},
    }
    base_result_path = output / "result.json"
    if base_result_path.is_file():
        base_result = json.loads(base_result_path.read_text())
        manifest["input_sha256"] = base_result.get("input_sha256", {})
        os.replace(base_result_path, output / "base-gate-result.json")
    result = {
        "version": 1,
        "experimental": True,
        "physical": False,
        "browser_acceptance": False,
        "passed": complete,
        "base_gate_exit_status": base_status,
        "errors": errors,
        "classification": classification,
    }
    _write_json(output / "input-source-manifest.json", manifest)
    _write_json(base_result_path, result)
    progress("classification_complete" if complete else "evidence_incomplete")
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
