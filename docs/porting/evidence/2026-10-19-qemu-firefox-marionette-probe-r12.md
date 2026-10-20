# Firefox Marionette short probe r12

r12 enabled the bounded syscall, futex and aggregate syscall profilers only for the Firefox Probe boot. It reached the same NewSession boundary; no scene, screenshot, or recovery was attempted.

## Runtime evidence

```text
FIREFOX_DRM_PROBE_BOUNDARY name=greeting status=pass
FIREFOX_DRM_PROBE_BOUNDARY name=new-session status=fail type=TimeoutError
FIREFOX_DRM_PROBE_SYSCALL pid=127 sequence=33330,number=98,entered_jiffies=282634
FIREFOX_DRM_PROBE_THREAD tid=158 state=S syscall=sequence=4,number=73,entered_jiffies=76661
FIREFOX_DRM_PROBE_CHILD ... syscall=sequence=...,number=73,...
```

RISC-V generic syscall 98 is `futex`; 73 is `ppoll`. The serial log also records long calls such as Firefox futex waits around 9,900–30,000 jiffies and a child futex around 103,592 jiffies. `FUTEX_WAIT_DETAIL` records repeated `outcome=spurious` events and one timed futex timeout for the browser process. These observations establish a futex/ppoll wait chain during renderer initialization, but do not prove a lost wake: the spurious events can be signal-driven and the current instrumentation does not pair a waiter with its waker.

## Interpretation boundary

The next work item is a bounded futex/ppoll microtest or paired wake trace, not another Firefox run. A kernel change must not be made solely from this receipt. E3 remains blocked because `NewSession` did not return a session id.

## Artifacts

| Artifact | SHA-256 |
|---|---|
| `desktop-m4-drm.serial.log` | `22a212dac41d77b9ba5b221ed2bd662ab06f58f43884e25aa701ba436b2c7c4e` |
| `result.json` | `4c3e59ae91bbed0cf1dd7aa50148fab51d3c61904347802b4ce6d28a163107b0` |
| `lifecycle.json` | `7beb4fba9a863203babc2a4ab2162e829afacd8027b5026cfd5498a70f8a50fa` |
| guest probe receipt | `4bb048a88bfe7ce613ca979e37e7a06fd8e70602add88201ce296e3b2151c2b3` |
| rootfs | `ffa0e4f685b26c22fff5ff683d924ec89450cccfb9298c3fdd5e8102b28eaa3a` |

Output group: `/mnt/mydisk/qemu-firefox-marionette-probe-r12-container/`
