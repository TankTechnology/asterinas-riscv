# E3 Firefox remote-trace probe r110b

## Scope

This was the single retry allowed after r110's Docker lifetime failure. It
enabled only the opt-in Firefox `remote.log.level=Trace` preference and
`RemoteAgent`/`Marionette` logging. The official Debian Firefox binary,
capability shape, kernel, DTB and QEMU GL device were unchanged. No full
scene or hardware was used.

## Result

The host gate entered `lifecycle.json` with `phase=launch,status=running`, but
the expected 420-second bound did not terminate the QEMU child. After the
process remained alive for more than six minutes with no serial log, no guest
marker and no `result.json`, the runner and QEMU were terminated explicitly.
The output is therefore a **host QEMU-runner timeout**, not a Firefox or
guest syscall result. The stale lifecycle is retained only as failure
provenance and must not be treated as a terminal guest receipt.

Input root image SHA-256:
`77b0f3dff3095573bb4d44214e23825d1163d9dd3ea802167502c8d319c68b19`.
Output group: `/mnt/mydisk/qemu-firefox-probe-trace-r110b`.

## Consequence

The trace code is statically verified (36 focused tests and Python syntax
checks pass), but no runtime `remote.log.level=Trace` evidence exists. The
next action must fix the host gate's child-timeout/cleanup path before another
Firefox launch; changing capability or Firefox settings again would not be
informative.

