# Firefox composite workload qualification

## Scope

This milestone adds a deterministic, phase-labelled Firefox workload for
finding repeated kernel cost classes. It is measurement infrastructure, not a
claim that page faults, SMP synchronization, software rendering, or Firefox
performance are fully optimized.

The workload uses one Firefox session and one exact local fixture origin. Its
ordered phases are `warmup`, `interaction-layout`, `canvas-image`,
`concurrent-resources`, `navigation-history`, `multi-context`, and `cooldown`.
The resource phase distinguishes a cold `no-store` pass from a cache-eligible
fill and repeated read of the same URLs. Navigation uses exact local pages and
actual history back/forward operations. Multi-context work is capped at three
iframes and verifies cleanup before the terminal snapshot.

## Evidence contract

Three fixed modes are available:

| Mode | Scale | Deadline | Intended use |
| --- | ---: | ---: | --- |
| `smoke` | 1 | 30 s | host/QEMU regression |
| `profile` | 4 | 120 s | repeated physical attribution |
| `stress` | 12 | 300 s | bounded tail/stability investigation |

The capture validates an append-only phase protocol and writes an atomic
checkpoint after phase progress. In parallel it samples Firefox and Xorg CPU
ticks, minor/major faults, RSS, optional `MemAvailable`, system CPU/load, and
Firefox per-thread `schedstat`. Firefox and Xorg PID start times must remain
stable. The collector closes only its transport socket; it does not send
`WebDriver:DeleteSession`, restart Firefox, reboot the guest, or rewrite the
root partition.

Interpret the evidence by clock domain:

- phase durations and rAF samples are browser-monotonic synthetic evidence;
- CPU, fault, memory, runnable-wait and dispatch intervals are guest-monotonic
  procfs evidence;
- USB input through physical HDMI scanout is not measured by either clock.

Missing compatible procfs fields are unsupported, not zero. The known Firefox
negative `fetchStart` issue is retained as invalid navigation evidence and is
not clamped. Public websites are final acceptance inputs, not attribution
workloads.

## Host qualification

The implementation was developed on `codex/firefox-performance-m2` in these
reviewable commits:

- `3b0829a7c` defines the strict workload snapshot protocol;
- `d49747469` adds the deterministic local fixture workload;
- `681dd4ea2` adds process fault/RSS and memory evidence;
- `84d6eb5c0` adds one-session capture and atomic checkpoints;
- `c36dc4628` packages the tools into Stage1 and the development overlay;
- `bc4db3a3f` makes cache reuse and history operations explicit;
- `bd77ff592` packages the input-identity helper required by older frozen roots.
- `ee99b42ef` rejects a document identity change during capture;
- `26d5921c9` gives page setup and the bounded workload separate deadlines.
- `31dfb0e53` explicitly selects the verified Firefox window before navigation.
- `a6b23ecbf` makes history completion observable and retains failure detail;
- `2a5a2362b` creates deterministic same-document history entries.

The final `tools/riscv/firefox_fast_check.sh` run passed 252 tests and emitted
`FIREFOX_FAST_CHECK_PASS`. The focused protocol, fixture, and
development-overlay run also passed 45 tests; it is a subset and is not added
to the 252-test total.

The final deterministic build products are:

| Artifact | SHA-256 |
| --- | --- |
| Stage1 initramfs | `7469602695184417dd6a62bd4ea29e97f750f759899a1cddfca0efd814e5ec2b` |
| browser-web development ext2 | `b1c0da81c34a44866112fa225490845d3ac8abcafc5d00502c5baee588fb27ad` |
| Sv39/SMP=4 kernel used by QEMU | `5444c9eb40e10d26278affb00f69bb8c212ce94091204cf94a6209899d2f588c` |
| four-hart QEMU DTB | `fbf606c939b855f06a11d66f3206be46552ba7ba0fe58240231d3b50a891aef8` |
| QEMU U-Boot | `ff9abcc4d04ecae76645ddd8f42a61d314938a66f98e91878faedb2633d76b62` |

Both workload Python tools and `/usr/lib/asterinas/desktop-input-identity` are
present in the resulting artifacts with executable mode. The development
overlay path performs no package download or debootstrap rebuild.

## QEMU qualification history

The first four-hart browser-web attempt used Stage1
`b0a18932a6132992e203a7404fe467e66a05434fe6b2c448eb0c844f1f5ec51d`
and overlay
`a0fe4b4c05cdf4ab7982238d7d5717171d4a592e031dc028b7e3b6bebb18a803`.
All ten network layers passed, but Firefox never reached readiness and the
bounded gate ended after 900 seconds with `browser-timeout:firefox-launch`.
The serial log repeatedly showed that the frozen root lacked
`/usr/lib/asterinas/desktop-input-identity`; therefore this was a deployment
overlay defect before the composite workload ran, not a workload or kernel
performance result. Commit `bd77ff592` fixes that exact omission.

The rebuilt standard browser-web run then reached Firefox PID 159, established
Marionette, completed the Baidu-home probe, and entered the local fixture. It
failed the older broad capability gate because that Firefox build reported
`false-capability:wasm`. This proves the input-helper deployment repair but is
not a composite result: no workload resource request occurred. The dedicated
local smoke path therefore masks the external evidence service, starts the same
Firefox unit after Xorg readiness, and invokes the composite collector directly.

The first dedicated local attempt exposed a gate-control issue before Firefox:
the general physical-input helper tried to restart an already active desktop
unit and returned status 127. The second started Firefox directly and reached
Marionette, then exposed a capture-budget bug: the 30-second smoke budget was
being consumed by QEMU page setup while Firefox still reported
`about:interactive`. Commit `26d5921c9` preserves the 30-second workload bound
and adds a separate 120-second document-setup bound.

## Four-hart local QEMU smoke result

The final local run used the hashes above, manifest
`26e0a77f07d7627b3eaece213e6a706cdd2e64f411e74f77e4167760b6257959`,
and a four-hart Sv39 QEMU guest. It completed all seven ordered phases and
published both `ASTERINAS_BROWSER_COMPOSITE_PASS mode=smoke phases=7` and a
passing host result. The writable run-root hash was
`9ba0593ca20fad6a855eb2ea91333c718a6bfdb611cd5f105ad933541c1e38d6`.
Firefox PID 222 and Xorg PID 164 retained start-time identities 1060 and 724.

| Phase | Browser duration | Key bounded counts |
| --- | ---: | --- |
| warm-up | 123 ms | 128 operations, 1 request |
| interaction/layout | 676 ms | 528 operations, 3 of 8 rAF samples over 50 ms |
| canvas/image | 447 ms | 200 operations, 4 images |
| concurrent resources | 1179 ms | 24 fetch attempts |
| navigation/history | 1077 ms | 8 operations, 2 document requests |
| multi-context | 976 ms | 2 contexts, 2 resource requests |
| cooldown | 18 ms | zero remaining contexts |

The fixture observed 23 workload network requests: 17 binary resources, four
images, and two context resources. The browser attempted 24 fetches in the
concurrent phase, while the fixture saw only the eight cold and eight initial
warm binary requests; the repeated warm URLs were served from Firefox cache.
The request ledger was not truncated. QEMU serialized the observed requests
(`workload_max_active=1`), which is a follow-up hypothesis rather than a
physical-kernel conclusion.

The 15.125-second system sample recorded 14.97 s user and 3.77 s kernel CPU for
Firefox, 330 ms user and 70 ms kernel CPU for Xorg, average aggregate CPU busy
fraction 42.5%, average runnable count after each interval 2.53, and 25,606
context switches. Firefox RSS moved from 74,568 to 74,881 pages and peaked at
76,044 pages. `MemAvailable` moved from 1,457,968 to 1,443,724 KiB, with a
1,435,384 KiB minimum. Minor and major fault deltas were both zero in this
post-startup smoke window; that does not establish low fault cost during
Firefox startup or a larger workload.

The 17.285-second thread ledger recorded 20.872 s Firefox thread runtime,
20.000 s runnable wait, and 15,387 dispatches. The main thread contributed
5.052 s runtime, 6.293 s wait, and 1,950 dispatches. The ledger explicitly
reports `thread-churn` and `thread-affinity-unavailable`; physical HDMI scanout
is unsupported. These high waits are useful evidence that system-emulated QEMU
is not a physical performance baseline.

The retained artifacts and SHA-256 identities are:

| Artifact | SHA-256 |
| --- | --- |
| `browser-composite-capture.json` | `b49b251e7e9f7b77cd3a6a4f85ddf970d692f16a995ea47b84a4af69074520bb` |
| `browser-composite-checkpoint.json` | `238ffa64f3138ead2ee9784f7b170714833fcdf2340dad0249c056d5d82224b6` |
| `browser-system-time.json` | `f09e12f5c7774123e0a6fbc59e893112c78396c42aa0c765db42dd6ed6835b40` |
| `browser-thread-time.json` | `a943a11ecd0498cf0340e1a7cb808bacc041492e9815bb86775862bf7185417c` |

This qualifies the local `smoke` path. It is not a physical performance result
and does not select a kernel optimization by itself.

## Physical attribution procedure

Use one stable physical boot and run three `profile` captures into distinct
empty directories. Verify Firefox/Xorg start times and zero leaked extra
contexts between runs. For each phase compare duration, Firefox user/kernel
CPU, minor/major-fault growth, RSS, system memory, total/main-thread runnable
wait and dispatches, CPU busy fractions, and fixture concurrency. A candidate
bottleneck must recur in at least two of three runs and exceed the relevant
sampling resolution before a focused kernel A/B change is justified.

The earlier evidence remains the boundary: the fault-around correctness bug
and RISC-V SSIP acknowledgement race are fixed, but general page-fault and SMP
costs are not declared solved. Previous ten-second Firefox samples showed only
269--326 ms total runnable wait and a roughly 1.3% median global/local I-cache
flush difference with overlapping runs. Those results do not identify scheduler
starvation or RFENCE as the dominant bottleneck, and this workload intentionally
collects evidence before changing either subsystem.
