#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

log_result()
{
	message="tcp_event_handoff: $*"
	printf '%s\n' "$message"
	printf '<12>%s\n' "$message" > /dev/kmsg 2>/dev/null || true
}

log_result "begin"
if /test/network/tcp_event_handoff; then
	log_result "PASS"
else
	status=$?
	log_result "FAIL status=$status"
	exit "$status"
fi
