# Firefox futex pairing probe r19

## Runtime boundary

r19 used the opt-in `asterinas.futex_detail_min_pid=100` filter and a 1024
detail-event cap.  Firefox still reached Marionette greeting but
`NewSession` timed out; no scene or screenshot was attempted.

The trace now permits a real wait/wake comparison:

| Firefox parent observation | Value |
|---|---:|
| wait records | 267 |
| wake records from the same pid | 86 |
| waits at `0x2aaaf2ce40` | 46 (43 spurious, 3 woken) |
| waits at `0x2aaaf2ce44` | 220 (218 spurious, 2 woken) |
| wakes at `0x2aaaf2ce40` | 3, all `woken=1` |
| wakes at `0x2aaaf2ce44` | 2, all `woken=1` |

The matching addresses and nonzero wake results show that the kernel can pair
Firefox's futex waits with its wake calls.  The remaining spurious waits are
real observations, but this run does not support the hypothesis that all
Firefox wakeups are lost.  The NewSession block should now be investigated at
Firefox's IPC/renderer state transition, with futex handling retained as a
measured dependency rather than changed speculatively.

## Artifacts

| Artifact | SHA-256 |
|---|---|
| `desktop-m4-drm.serial.log` | `60115a1bc1ed4cac6e8adabab378e62e8842492894c515f62883736d6640ab30` |
| `result.json` | `22a58327bae40752a594b791194e42e570f2faec9e7783546f2ea8932de3c706` |
| `lifecycle.json` | `d5faaca2909fd5ed1ebd21a76324de9c6e708f17f33f4dce00dcc3b1afcc630b` |
| kernel | `67643184cee924744fe6b890a42d7ac349d47c0aa5788cfac275da2a6d8ab378` |

Output group: `/mnt/mydisk/qemu-firefox-marionette-probe-r19-container/`
