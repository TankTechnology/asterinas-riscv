# Firefox NewSession TCP trace coverage r117

- gate: `e3-firefox-tcp-trace-coverage`
- claim: E3 diagnostic boundary
- status: `pass` for coverage classification; this does not claim E3 success
- input: existing r116 serial log only; no QEMU, Firefox, serial, network, or board operation
- source: `/mnt/mydisk/qemu-firefox-e3-r116/desktop-m4-drm.serial.log`

## Finding

The r116 serial log contains the complete Marionette client-side boundary:

```text
A_WEB_MARIONETTE_TRANSPORT ... command="WebDriver:NewSession" ... stage="send" ... send_complete=true
```

The last TCP diagnostic records before that boundary are the greeting exchange:

```text
ASTERINAS_TCP_TRACE seq=9  stage=send-buffered      local_port=2828 remote_port=33449 value0=53
ASTERINAS_TCP_TRACE seq=12 stage=peer-process        local_port=33449 remote_port=2828  value0=53 value2=53
ASTERINAS_TCP_TRACE seq=15 stage=send-buffered      local_port=33449 remote_port=2828  value0=66
ASTERINAS_TCP_TRACE seq=18 stage=peer-process        local_port=2828 remote_port=33449  value0=66 value2=66
ASTERINAS_TCP_TRACE seq=20 stage=pollee-notify       local_port=2828 remote_port=33449  value0=3 value1=66
```

There are **zero** `ASTERINAS_TCP_TRACE` records after the NewSession send
marker. This is an instrumentation-coverage result, not proof that the
request was dropped: the trace has no receive/write marker tied to the second
request. The only defensible next code boundary is to expose a request/response
sequence identifier at the Marionette socket read/write path, or to extend the
existing trace with a per-connection request counter. Repeating the same guest
run without that instrumentation would not distinguish transport loss from a
Firefox IPC wait.

## Evidence interpretation

- `virgl` and `/dev/dri/renderD128` succeeded in the same log.
- greeting completed; NewSession send completed.
- no NewSession response marker appeared before the bounded r116 termination.
- generic futex/ppoll r105 and capability A/B r101 remain independent controls.

This closes the question “does the current log prove TCP delivery after
NewSession?” with **no** and records the missing observation explicitly.

