#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run the developer-workflow acceptance checks against an Asterinas guest.

Each check is one short command; the driver prints one line per check and a
summary, and exits non-zero when a check reports FAIL. Network checks report
SKIP when the guest cannot reach the package source.

    # board over the network (key already authorized in ~asterinas/.ssh)
    python3 -m tools.riscv.legacy.compat.run_workflow_checks \\
        --host 192.0.2.10 --key ~/.ssh/asterinas-gate

`192.0.2.10` is an RFC 5737 documentation address. The board's real
address is site configuration: set `BOARD_IP` (and `BOARD_GW` /
`PORTAL_IP` where the script uses them) in the local environment; never
commit it.

    # one check at a time while debugging
    python3 -m tools.riscv.legacy.compat.run_workflow_checks --host ... --only gcc

The guest-side checks live in guest_workflow_checks.sh next to this file; the
host-side ones (ssh, pty, scp, rsync) run from here because they need the
host's ssh client and a key the guest has authorized.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import http.server
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
GUEST_SCRIPT = HERE / "guest_workflow_checks.sh"
GUEST_CHECKS = ("python3", "gcc", "git", "tar_xz", "pip", "firefox")
HOST_CHECKS = ("ssh", "pty", "scp", "rsync")
ALL_CHECKS = HOST_CHECKS + GUEST_CHECKS


class Check:
    def __init__(self, name: str, status: str, detail: str) -> None:
        self.name = name
        self.status = status
        self.detail = detail

    def render(self) -> str:
        return f"CHECK {self.name} {self.status} {self.detail}"


def ssh_base(args: argparse.Namespace, *, force_pty: bool = False) -> list[str]:
    argv = [
        "ssh",
        "-p",
        str(args.port),
        "-i",
        str(Path(args.key).expanduser()),
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "LogLevel=ERROR",
    ]
    if force_pty:
        # `-tt` forces a remote pty even though this driver has no terminal.
        argv.append("-tt")
    return argv


def remote(
    args: argparse.Namespace, command: str, *, force_pty: bool = False
) -> list[str]:
    return [*ssh_base(args, force_pty=force_pty), f"{args.user}@{args.host}", command]


def scp_base(args: argparse.Namespace) -> list[str]:
    return [
        "scp",
        "-P",
        str(args.port),
        "-i",
        str(Path(args.key).expanduser()),
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "BatchMode=yes",
        "-o",
        "LogLevel=ERROR",
    ]


def rsync_remote_shell(args: argparse.Namespace) -> str:
    return " ".join(
        [
            "ssh",
            "-p",
            str(args.port),
            "-i",
            str(Path(args.key).expanduser()),
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "BatchMode=yes",
            "-o",
            "LogLevel=ERROR",
        ]
    )


def run(argv: list[str], timeout: float) -> tuple[int, str]:
    try:
        finished = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f}s"
    output = (finished.stdout + finished.stderr).strip()
    return finished.returncode, output


def check_ssh(args: argparse.Namespace) -> Check:
    rc, out = run(
        remote(args, "printf 'wf-ssh %s\\n' \"$(id -u)\""), args.timeout
    )
    if rc == 0 and "wf-ssh" in out:
        return Check("ssh", "PASS", out.splitlines()[-1])
    return Check("ssh", "FAIL", out.splitlines()[-1] if out else f"rc={rc}")


def check_pty(args: argparse.Namespace) -> Check:
    rc, out = run(
        remote(args, "printf 'wf-pty %s\\n' \"$(tty)\"", force_pty=True),
        args.timeout,
    )
    if rc == 0 and "/dev/pts/" in out:
        return Check("pty", "PASS", out.splitlines()[-1])
    return Check("pty", "FAIL", out.splitlines()[-1] if out else f"rc={rc}")


def check_scp(args: argparse.Namespace) -> Check:
    payload = os.urandom(256 * 1024)
    digest = hashlib.sha256(payload).hexdigest()
    target = f"{args.user}@{args.host}:/tmp/wf-scp.bin"
    with tempfile.TemporaryDirectory(prefix="wf-scp-") as directory:
        source = Path(directory) / "payload.bin"
        source.write_bytes(payload)
        rc, out = run([*scp_base(args), str(source), target], args.timeout)
        if rc != 0:
            return Check("scp", "FAIL", f"upload: {out.splitlines()[-1] if out else rc}")
        back = Path(directory) / "back.bin"
        rc, out = run(
            [*scp_base(args), f"{args.user}@{args.host}:/tmp/wf-scp.bin", str(back)],
            args.timeout,
        )
        if rc != 0:
            return Check("scp", "FAIL", f"download: {out.splitlines()[-1] if out else rc}")
        actual = hashlib.sha256(back.read_bytes()).hexdigest()
    run(remote(args, "rm -f /tmp/wf-scp.bin"), args.timeout)
    if actual == digest:
        return Check("scp", "PASS", f"roundtrip {digest[:16]}")
    return Check("scp", "FAIL", f"sha256 {actual[:16]} != {digest[:16]}")


def check_rsync(args: argparse.Namespace) -> Check:
    payload = os.urandom(256 * 1024)
    digest = hashlib.sha256(payload).hexdigest()
    with tempfile.TemporaryDirectory(prefix="wf-rsync-") as directory:
        source = Path(directory) / "payload.bin"
        source.write_bytes(payload)
        rc, out = run(
            [
                "rsync",
                "-a",
                "-e",
                rsync_remote_shell(args),
                str(source),
                f"{args.user}@{args.host}:/tmp/wf-rsync.bin",
            ],
            args.timeout,
        )
        if rc != 0:
            return Check("rsync", "FAIL", f"upload: {out.splitlines()[-1] if out else rc}")
        back = Path(directory) / "back.bin"
        rc, out = run(
            [
                "rsync",
                "-a",
                "-e",
                rsync_remote_shell(args),
                f"{args.user}@{args.host}:/tmp/wf-rsync.bin",
                str(back),
            ],
            args.timeout,
        )
        if rc != 0:
            return Check("rsync", "FAIL", f"download: {out.splitlines()[-1] if out else rc}")
        actual = hashlib.sha256(back.read_bytes()).hexdigest()
    run(remote(args, "rm -f /tmp/wf-rsync.bin"), args.timeout)
    if actual == digest:
        return Check("rsync", "PASS", f"roundtrip {digest[:16]}")
    return Check("rsync", "FAIL", f"sha256 {actual[:16]} != {digest[:16]}")


def local_address_for(host: str) -> str:
    """The host address the guest can reach us on (no packets are sent)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((host, 9))
        return probe.getsockname()[0]
    finally:
        probe.close()


def ensure_wheel(cache: Path) -> bool:
    """Make `six` available as a wheel in `cache`, downloading it if needed."""
    cache.mkdir(parents=True, exist_ok=True)
    if any(cache.glob("six-*.whl")):
        return True
    venv = cache / ".pip-venv"
    if not (venv / "bin" / "pip").exists():
        result = subprocess.run(
            [sys.executable, "-m", "venv", str(venv)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return False
    result = subprocess.run(
        [
            str(venv / "bin" / "pip"),
            "download",
            "--disable-pip-version-check",
            "--no-deps",
            "-q",
            "-d",
            str(cache),
            "six==1.16.0",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and any(cache.glob("six-*.whl"))


def serve_wheels(cache: Path) -> tuple[http.server.ThreadingHTTPServer, str]:
    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

    handler = functools.partial(QuietHandler, directory=str(cache))
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://{{host}}:{server.server_address[1]}/"


def run_guest_checks(args: argparse.Namespace) -> list[Check]:
    remote_script = "/tmp/wf-guest-checks.sh"
    rc, out = run(
        [*scp_base(args), str(GUEST_SCRIPT), f"{args.user}@{args.host}:{remote_script}"],
        args.timeout,
    )
    if rc != 0:
        return [
            Check(name, "FAIL", f"cannot install guest checks: {out.splitlines()[-1] if out else rc}")
            for name in GUEST_CHECKS
        ]

    wheel_index = ""
    wheel_host = ""
    server = None
    if not args.no_wheel_index:
        # Serve one pinned wheel from the host so the pip check exercises the
        # installer without depending on the guest's DNS or the campus portal.
        cache = Path(args.wheel_cache).expanduser().resolve()
        if ensure_wheel(cache):
            server, template = serve_wheels(cache)
            host = args.wheel_index_host or local_address_for(args.host)
            wheel_index = template.format(host=host)
            wheel_host = f"{host}:{server.server_address[1]}"
        else:
            print(
                "note: no wheel index available; the pip check will use PyPI",
                file=sys.stderr,
            )

    prefix = (
        f"WF_WHEEL_INDEX={wheel_index} WF_WHEEL_HOST={wheel_host} "
        if wheel_index
        else ""
    )
    try:
        rc, out = run(
            remote(args, f"{prefix}sh {remote_script}"), max(args.timeout, 120.0)
        )
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
    run(remote(args, f"rm -f {remote_script}"), args.timeout)
    seen: dict[str, Check] = {}
    for line in out.splitlines():
        if not line.startswith("CHECK "):
            continue
        _, name, status, *detail = line.split(" ", 3)
        seen[name] = Check(name, status, detail[0] if detail else "")
    return [
        seen.get(name, Check(name, "FAIL", "no result line from the guest script"))
        for name in GUEST_CHECKS
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="asterinas")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--key", default="~/.ssh/asterinas-gate")
    parser.add_argument(
        "--only",
        action="append",
        choices=ALL_CHECKS,
        help="run only the named check (repeatable)",
    )
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--wheel-cache",
        default="target/compat/wheels",
        help="directory of wheels served to the guest for the pip check",
    )
    parser.add_argument(
        "--wheel-index-host",
        help="address the guest uses to reach the wheel index (default: this host's LAN address)",
    )
    parser.add_argument(
        "--no-wheel-index",
        action="store_true",
        help="let the pip check use PyPI instead of a host-served wheel",
    )
    args = parser.parse_args(argv)

    selected = tuple(args.only) if args.only else ALL_CHECKS
    missing = [tool for tool in ("ssh", "scp", "rsync") if shutil.which(tool) is None]
    if missing:
        print(f"error: missing host tools: {', '.join(missing)}", file=sys.stderr)
        return 2

    results: list[Check] = []
    if "ssh" in selected:
        results.append(check_ssh(args))
    if "pty" in selected:
        results.append(check_pty(args))
    if "scp" in selected:
        results.append(check_scp(args))
    if "rsync" in selected:
        results.append(check_rsync(args))
    if any(name in selected for name in GUEST_CHECKS):
        guest = run_guest_checks(args)
        results.extend(check for check in guest if check.name in selected)

    for check in results:
        print(check.render())

    failed = [check for check in results if check.status == "FAIL"]
    skipped = [check for check in results if check.status == "SKIP"]
    print(
        f"SUMMARY {len(results) - len(failed)}/{len(results)} passed, "
        f"{len(failed)} failed, {len(skipped)} skipped"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
