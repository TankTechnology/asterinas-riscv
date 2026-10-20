# Firefox NewSession r24 offline IPC analysis

This is a read-only analysis of the r24 serial log; no guest was restarted.

The log contains 255 bounded futex-wait detail records and no
`ASTERINAS_FUTEX_WAKE_DETAIL` records.  That absence is not by itself proof
of a lost wake because the current diagnostic intentionally suppresses
zero-result wakes and only prints positive wake counts.  Some Firefox waits do
return `outcome=woken`, so the kernel does perform successful futex wakeups.

The stronger boundary is:

- Firefox parent PID 131 remains alive with 50 threads;
- its sample shows a parent thread in `ppoll(73)` and worker threads in
  `futex(98)`;
- Socket Process PID 399 remains asleep in `ppoll(73)` with a stable syscall
  snapshot;
- Marionette is listening on TCP port 2828 and the greeting succeeds;
- the single NewSession request is fully written, but no response header is
  returned before the bounded deadline.

Therefore the current evidence identifies a Firefox parent/Socket Process IPC
initialization stall after the transport greeting.  It does not yet prove a
specific futex wake bug.  The next diagnostic change should log bounded
zero-result futex wakes for the selected PID range, then use one more short
probe to distinguish an unmatched wake from a Firefox-side protocol wait.

## Artifacts

- Input serial log: `/mnt/mydisk/qemu-firefox-marionette-probe-r24-container/desktop-m4-drm.serial.log`
- Input SHA-256: `0e4d5e62de409259d7cf37cda88af84acdc97429d1c5a7a6dc541a36dbce7cee`
- Probe receipt: `docs/porting/receipts/2026-10-19-firefox-marionette-probe-r24.json`

