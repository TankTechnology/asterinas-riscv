# Ext2 page-cache batched-read results, 2026-09-13

## Result

The RISC-V Firefox cold-start investigation found a concrete kernel I/O
bottleneck and repaired it. Sequential ext2 buffered reads previously entered
the page-cache backend one 4-KiB page at a time. On Megrez, each page became a
separate synchronous MMC command. Direct ext2 I/O already grouped contiguous
blocks, which explained why direct reading was more than twice as fast as the
buffered path.

The repaired path collects at most 32 cache pages, locks missing pages before
publishing them, and asks the filesystem backend to submit the group. Ext2
splits it at logical gaps, physical gaps, and sparse holes. Each physically
contiguous run uses one BIO and scatters completion data into the original
locked pages. Other filesystems retain the single-page default.

## Correctness evidence

Focused RISC-V QEMU KTests passed for:

- four adjacent ext2 blocks producing one four-block read BIO with exact data;
- sparse holes producing zeros and splitting the two mapped runs;
- a failed combined read returning `EIO`, followed by one successful combined
  retry with exact data;
- one generic page-cache batch call for four cold pages and no empty resident
  batch;
- delayed completions, concurrent page faults, and persistent backend errors;
- logical/physical contiguity and arithmetic-overflow run boundaries.
- block-layer scatter copy, short-source rejection, and direction rejection.

The completion callback does not allocate or take a blocking lock. It reads
from the already allocated extent segment, copies into the already allocated
page segments, invokes each page completion once, and releases the page locks.

The bounded TLA+ model under
`tools/verification/page_cache_batch_publish/` compares the implemented
lock-before-publish ordering with a publish-before-lock negative control. TLC
found the expected unsubmitted wait cycle in the negative model after 35
generated states (26 distinct). The corrected model exhausted 69 generated
states (41 distinct) without an invariant error. This proves only the finite
model; runtime tests separately cover data, error, and retry behavior.

An attempt to run all 230 KTests did not terminate in the unrelated
`tty_echo_runs_without_the_line_discipline_lock` test. Its QEMU process group
was terminated without deleting the persistent container. The exact tests
above, rather than that incomplete broad run, are the regression evidence.

Formatting, whitespace checks, and the offline RISC-V release build completed.
The build reused the persistent Docker container and cached `cargo-osdk`; no
container, image, Cargo cache, or toolchain was deleted or downloaded.

## Physical I/O result

The physical run used the same Firefox file, 128-MiB byte count, Debian
partition, stage-1 initramfs, DTB, kernel arguments, and direct-then-buffered
ordering as the baseline. Only the release kernel changed.

| Kernel | Path | Seconds | MB/s |
| --- | --- | ---: | ---: |
| Previous | Direct | 13.85 | 9.69 |
| Batched page cache | Direct | 13.71 | 9.79 |
| Previous | Buffered | 34.63 | 3.88 |
| Batched page cache | Buffered | 15.10 | 8.89 |

Buffered latency fell 56.4%, and throughput increased 2.29 times. Direct I/O
changed by only about 1%, serving as a same-device control. Buffered I/O now
reaches about 91% of the direct throughput, which supports the specific causal
claim that per-page submission was the dominant buffered-path gap. It does not
claim that the MMC PIO driver has reached RockOS throughput.

The run reached the debug console 7.82 seconds after menu selection, both `dd`
commands returned zero, and the board recovered to RockOS. The persistent
candidate and vendor selector hashes remained
`02280720efe7a1ad0ac084cdc20429406631e12d2e16f05638544bab0883fb26`
and
`eb5f39a6e2db71ccc93ae005c488fd9f7ef353e75426e5a51e8923a9cbf2ebc5`.

## Firefox result

The established runner measures from selecting the temporary Asterinas menu
entry until `xdotool` finds a visible Firefox window. It also requires X11 and
`/dev/fb0`, samples the main process from `/proc`, records thread state, and
recovers the board to RockOS.

| Run | Menu to console (s) | Menu to visible Firefox (s) | Service restarts | Recovery |
| --- | ---: | ---: | ---: | --- |
| Previous comparable run | 9.41 | 61.81 | — | RockOS |
| Batched page cache 1 | 7.74 | 40.33 | 0 | RockOS |
| Batched page cache 2 | 7.25 | 46.21 | 0 | RockOS |

The two new cold starts average 43.27 seconds, 30.0% below the 61.81-second
comparable baseline. Both final samples had a real visible window; the browser
service was `active/running` with `NRestarts=0`. The 5.88-second difference
between the new runs remains real variability and indicates that storage
batching is not the only remaining Firefox cost.

At the final sample the Firefox main thread had respectively 1,690/1,822 and
1,684/1,944 user/system ticks. The still-high system time, the approximately
9.8-MB/s direct-read ceiling, software rendering, and startup variance are the
next performance leads. This change makes no DRM or MMC-DMA claim.

## Deployment and provenance

The release image was 5,967,784 bytes with SHA-256
`ae4695acf747ecd77bdcfa5e9ac2b5961f8f0d52a31d1c242a732d9c8332fd9b`.
It was copied over the existing RockOS network into the unique path
`/var/tmp/asterinas-fair-yield.PeOiTdcd/pagecache-batch-ae4695acf747.Image`.
No partition image, Debian root filesystem, Firefox package, existing kernel,
or persistent selector was rewritten. Remote and local image hashes matched
before boot and after final recovery.

Raw local evidence is intentionally ignored under
`target/firefox-current-performance/`:

- baseline: `io-path-2/result.json`, `run-1/result.json`;
- new I/O: `pagecache-batch-io-1/{result.json,serial.log}`;
- new Firefox: `pagecache-batch-firefox-{1,2}/` with `result.json`,
  `serial.log`, and `details.log`;
- exact temporary configs: `desktop-pagecache-batch.conf` and
  `desktop-pagecache-batch-io.conf`.

The corresponding result SHA-256 values are:

| Result | SHA-256 |
| --- | --- |
| Previous I/O | `262d8fbce9242be9ce6777aa9c8db2ebc79c6e5ad3822d922740c5778529d93c` |
| Previous Firefox | `45c31dc09b720005d841aeb081c5c5d9f2bd16030b115402dd1878fcf4abe4ca` |
| Batched I/O | `b4e7c343d70204282326c8479436c124ab2c6207d1033886c3655597c6271d8e` |
| Batched Firefox 1 | `b4bd5469179286fac02db592fb5b97f9251f6329a655c0186586d399028126ab` |
| Batched Firefox 2 | `e41ae34aa00d225b48bb8fd321f7bc77be2a0506a5fe84bc485d60bfad4f21f9` |

The board was finally confirmed running RockOS 6.6.87 with boot ID
`c0341b4e-0612-4b81-b868-1759c6464ac8`.
