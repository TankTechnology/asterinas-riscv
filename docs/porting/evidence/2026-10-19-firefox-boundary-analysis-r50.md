# Firefox NewSession boundary analysis r50

This is a read-only analysis of the existing r49 serial log. It starts no
guest process and does not rerun Firefox.

Input:

- Serial log: `/mnt/mydisk/qemu-firefox-marionette-probe-r49-container/desktop-m4-drm.serial.log`
- Serial SHA-256: `bc53ae2b0f0464372d896136d56e5fd3abf0174f7769413ce3220b9e77af653f`
- Existing result: `/mnt/mydisk/qemu-firefox-marionette-probe-r49-container/result.json`

The parser is [analyze_firefox_boundary.py](/mnt/shared/xaj/Program/asterinas/tools/riscv/debian/rootfs/analyze_firefox_boundary.py).

## Extracted boundary

| Observation | Result |
|---|---|
| NewSession request sent completely | yes |
| NewSession response header | absent; bounded timeout |
| Firefox parent final state | sleeping, 57 threads, 264396 KiB RSS |
| Parent slow syscall evidence | futex waits are present in the parent PID |
| Socket Process | sleeping in `ppoll(73)` |
| WebExtensions/content/RDD children | still present; child `ppoll(73)` and running states observed |
| Futex detail outcomes | `woken=16`, `spurious=237`, `timeout_or_error=3` |
| Proven lost wakeup | no |

The parent is waiting while child IPC processes continue to run or poll. The
log therefore identifies the unresolved boundary as Firefox's protocol-side
startup coordination after the Marionette greeting, rather than proving that
the kernel's futex waiter queue dropped a wake. The minimal RISC-V H1/H2/H3
probe already passed in r9, so no `pause_timeout` semantic patch is justified
by this evidence.

## Verification

```text
python3 -m py_compile tools/riscv/debian/rootfs/analyze_firefox_boundary.py
git diff --check -- tools/riscv/debian/rootfs/analyze_firefox_boundary.py
```
