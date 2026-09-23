#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Summarize the last RPC call from an lmbench_rpc_trace.c output file."""

import argparse
import json
import re
from pathlib import Path

EVENT = re.compile(
    r"(?P<seq>\d+) (?P<ns>\d+) (?P<op>[SPR]) fd=(?P<fd>-?\d+) "
    r"rc=(?P<rc>-?\d+) arg=(?P<arg>-?\d+) xid=(?P<xid>\d+) "
    r"revents=(?P<revents>\d+) duration_ns=(?P<duration_ns>\d+)"
)


def summarize(path: Path) -> dict:
    events = [
        {key: value if key == "op" else int(value) for key, value in match.groupdict().items()}
        for line in path.read_text().splitlines()
        if (match := EVENT.fullmatch(line))
    ]
    sends = [(index, event) for index, event in enumerate(events) if event["op"] == "S"]
    if not sends:
        raise ValueError("trace contains no sendto call")

    xid = sends[-1][1]["xid"]
    first_index = next(index for index, event in sends if event["xid"] == xid)
    prior_send_xid = next(
        (event["xid"] for index, event in reversed(sends) if index < first_index), None
    )
    call = events[first_index:]
    timeout_deduction = 0
    stale_deduction = 0
    stale_replies = []
    poll_ready = []
    for index, event in enumerate(call):
        if event["op"] != "P":
            continue
        if event["rc"] == 0:
            timeout_deduction += event["arg"]
        elif event["rc"] == 1:
            poll_ready.append(event)
            next_event = call[index + 1] if index + 1 < len(call) else None
            if next_event and next_event["op"] == "R" and next_event["xid"] != xid:
                stale_deduction += event["arg"]
                stale_replies.append(next_event["xid"])

    return {
        "trace": str(path),
        "xid": xid,
        "prior_send_xid": prior_send_xid,
        "first_send_seq": call[0]["seq"],
        "last_event_seq": call[-1]["seq"],
        "wall_ms": round((call[-1]["ns"] - call[0]["ns"]) / 1_000_000, 4),
        "request_sends": sum(event["op"] == "S" for event in call),
        "poll_timeouts": sum(event["op"] == "P" and event["rc"] == 0 for event in call),
        "poll_ready": len(poll_ready),
        "stale_replies": len(stale_replies),
        "stale_reply_xids": sorted(set(stale_replies)),
        "timeout_deduction_ms": timeout_deduction,
        "stale_reply_deduction_ms": stale_deduction,
        "total_deduction_ms": timeout_deduction + stale_deduction,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.trace), indent=2))


if __name__ == "__main__":
    main()
