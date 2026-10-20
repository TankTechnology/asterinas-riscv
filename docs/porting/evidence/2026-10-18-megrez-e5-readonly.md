# Megrez E5 management-chain read-only observation — 2026-10-18

This is a read-only observation. It did not write the serial device, issue a
reboot, select a U-Boot entry, mount or modify a filesystem, start Asterinas,
or touch a board peripheral.

## Observed host control boundary

- Stable serial path was present:
  `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0`.
- `fuser -v` found no process owning that serial path during the observation.
- A passive owner scan found no `screen`, `picocom`, `minicom`, `socat`, or
  board-session process holding the serial path.

## RockOS network identity

The board answered one bounded ICMP probe at `10.100.19.200`. A single
non-interactive SSH command used an existing key and only read-only commands;
the host generated the nonce and included it in the response:

```text
MEGREZ_READONLY_NONCE=c4774f93566d3264f8238f12
1000
9c777a00-cc49-47c7-829a-c6abac19dbc9
Linux 6.6.87
02:42:14 up 1 day, 7:21, 4 users, load average: 0.16, 0.15, 0.10
```

The response proves a live RockOS SSH path and a fresh nonce/boot-id pair,
but the SSH identity is UID 1000. It is not an authenticated UID-0 receipt.

A second bounded, non-interactive SSH check used a new host nonce and tried
only `sudo -n id -u` (no password input and no state-changing command):

```text
MEGREZ_SSH_NONCE=526e4b50fbc15d5bf80386d7
1000
sudo: a password is required
9c777a00-cc49-47c7-829a-c6abac19dbc9
```

This confirms that the current `debian` management path does not provide a
passwordless UID-0 handoff. The board remained in the same RockOS boot epoch;
no reboot or serial write was attempted.

A single bounded authentication probe using the previously authorized local
credential path then received a fresh nonce but no password prompt or UID-0
response before the 8-second deadline:

```text
MEGREZ_AUTH_NONCE=6949d7a6ca44876148e492e1
AUTH_PROBE_TIMEOUT
```

The probe was stopped at the deadline and was not retried. No privileged
command was observed and no board state was changed.

## E5 classification

The read-only sub-assertions `rockos_ssh_identity` and `serial_owner_free`
pass. The complete E5 claim remains **pending** because this observation has
no UID-0 nonce response, no serial command response, and no new fallback or
recovery cycle. Those missing conditions must be obtained in a separately
authorized, recoverable operation; this receipt must not be promoted to a
physical Asterinas boot result.
