# Firefox Marionette independent probe r27

r27 ran once with the rebuilt kernel and the independent probe service.  It
reached greeting, sent one `WebDriver:NewSession`, and timed out at the
response header.  The new wake instrumentation produced exactly 64 bounded
zero-result wake records and 128 positive wake records.

This proves the futex wake path is active for the Firefox process.  Parent
PID 130/132 has successful wakes on several addresses, while one repeated
wait address (`0x2aaae83c84`) continues to return spurious waits.  The final
Firefox parent snapshot is in syscall 212 (`recvmsg`); the Socket Process
remains in `ppoll(73)`.  The browser log still reports Marionette listening
on port 2828, but no NewSession response arrives.

E3 therefore remains blocked at Firefox IPC/protocol initialization.  No
scene, screenshot, frame-rate, or recovery phase was run.

## Artifacts

| Artifact | SHA-256 |
|---|---|
| serial log | `49efcdd9dd441afab6396aaf4558495942fce618cefc9f658646dcfa0678d8e3` |
| result | `064d3bb4f855b985f82e9c948bd1601f0c5a06d7c3499c4e071c858463ae2e5b` |
| lifecycle | `82ec13a9dd5656b1a224bc740cf30658e5857897034438f8b4bbe1df837e35f7` |
| kernel | `c7d6c654f2437b6e8c7da67815ca9f4002012333ef42dfb93812b7ffd368fd17` |

Output group: `/mnt/mydisk/qemu-firefox-marionette-probe-r27-container/`

