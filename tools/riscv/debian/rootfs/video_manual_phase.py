#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Bracket one preloaded VP8 playback with Firefox and Xorg CPU counters.

Run inside Firefox's network namespace against video_filter_probe.py. The
probe server verifies the pinned clip hash; this script arms the manual-start
page before taking its first counter snapshot, then triggers exactly one play.
"""

import argparse
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlencode

sys.path.insert(0, "/usr/lib/asterinas")
from browser_m5_marionette_gate import Marionette


RUN_ID = re.compile(r"[a-z0-9]{1,20}")


def thread_stats(pid: int) -> dict[int, tuple[str, int, int]]:
    result = {}
    for task in Path(f"/proc/{pid}/task").iterdir():
        try:
            name = (task / "comm").read_text().strip()
            run_ns, wait_ns, _ = map(int, (task / "schedstat").read_text().split())
            result[int(task.name)] = (name, run_ns, wait_ns)
        except (OSError, ValueError):
            continue
    return result


def thread_deltas(
    before: dict[int, tuple[str, int, int]],
    after: dict[int, tuple[str, int, int]],
) -> list[dict[str, int | str]]:
    return sorted(
        (
            {
                "tid": tid,
                "name": entry[0],
                "run_ns": entry[1] - before[tid][1],
                "wait_ns": entry[2] - before[tid][2],
            }
            for tid, entry in after.items()
            if tid in before and entry[0] == before[tid][0]
        ),
        key=lambda entry: entry["run_ns"],
        reverse=True,
    )


def script(client: Marionette, source: str) -> object:
    result = client.command(
        "WebDriver:ExecuteScript",
        {
            "script": source,
            "args": [],
            "newSandbox": True,
            "sandbox": "asterinas-video-manual-phase",
            "line": 1,
            "filename": "asterinas-video-manual-phase",
        },
    )
    return result.get("value") if isinstance(result, dict) else result


def run(args: argparse.Namespace) -> dict[str, object]:
    if RUN_ID.fullmatch(args.run_id) is None:
        raise ValueError("invalid run ID")
    if not re.fullmatch(r"[0-9a-f]{64}", args.clip_sha256):
        raise ValueError("invalid clip SHA-256")
    if not args.base_url.startswith("http://"):
        raise ValueError("video probe URL must use HTTP")
    page = args.base_url.rstrip("/") + "/probe?" + urlencode(
        {"run": args.run_id, "size": "large", "filter": "auto", "start": "manual"}
    )

    client = Marionette("127.0.0.1", 2828, 20)
    created = False
    try:
        client.set_timeout(20)
        session = client.command("WebDriver:NewSession", {"strictFileInteractability": True})
        if not isinstance(session, dict) or not isinstance(session.get("sessionId"), str):
            raise RuntimeError("Marionette session did not start")
        created = True
        client.set_timeout(30)
        client.command("WebDriver:Navigate", {"url": page})

        deadline = time.monotonic() + 15
        last_ready = None
        while time.monotonic() < deadline:
            client.set_timeout(8)
            last_ready = json.loads(script(
                client,
                "const v=document.querySelector('#clip');"
                "return JSON.stringify({armed:!!document.querySelector('#start-probe'),"
                "readyState:v.readyState,networkState:v.networkState,"
                "error:v.error?.code??null,status:document.querySelector('#status').textContent});",
            ))
            if last_ready["armed"] and last_ready["readyState"] >= 2:
                break
            time.sleep(0.2)
        else:
            raise TimeoutError(f"video did not become ready before playback: {last_ready}")

        before_browser = thread_stats(args.browser_pid)
        before_xorg = thread_stats(args.xorg_pid)
        before_ns = time.monotonic_ns()
        if script(
            client,
            "const b=document.querySelector('#start-probe');"
            "b.click();return b.dataset.started==='1';",
        ) is not True:
            raise RuntimeError("manual video playback did not start")

        deadline = time.monotonic() + 25
        metric = None
        while time.monotonic() < deadline:
            client.set_timeout(8)
            status = script(client, "return document.querySelector('#status').textContent;")
            if isinstance(status, str) and status.startswith("{"):
                metric = json.loads(status)
                break
            time.sleep(0.2)
        after_ns = time.monotonic_ns()
        after_browser = thread_stats(args.browser_pid)
        after_xorg = thread_stats(args.xorg_pid)
        if metric is None or metric.get("state") != "ended" or metric.get("run") != args.run_id:
            raise RuntimeError(f"video did not finish cleanly: {metric}")

        record = {
            "run": args.run_id,
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "clip_sha256": args.clip_sha256,
            "page": page,
            "window_ns": after_ns - before_ns,
            "metric": metric,
            "firefox_threads": thread_deltas(before_browser, after_browser),
            "xorg_threads": thread_deltas(before_xorg, after_xorg),
        }
        with args.output.open("x") as stream:
            json.dump(record, stream, sort_keys=True)
            stream.write("\n")
        return record
    finally:
        if created:
            try:
                client.set_timeout(10)
                client.command("WebDriver:Navigate", {"url": "about:blank"})
                client.command("WebDriver:DeleteSession")
            except Exception as error:
                print(f"VIDEO_MANUAL_PHASE cleanup_error={error!r}", flush=True)
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-pid", type=int, required=True)
    parser.add_argument("--xorg-pid", type=int, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--clip-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    record = run(args)
    firefox_cpu_ns = sum(
        row["run_ns"]
        for row in record["firefox_threads"]
        if row["name"] in ("Renderer", "SwComposite")
    )
    print(
        "VIDEO_MANUAL_PHASE "
        + json.dumps(
            {
                "run": record["run"],
                "window_ms": round(record["window_ns"] / 1_000_000, 3),
                "frames": record["metric"]["totalVideoFrames"],
                "dropped": record["metric"]["droppedVideoFrames"],
                "firefox_render_cpu_ns": firefox_cpu_ns,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
