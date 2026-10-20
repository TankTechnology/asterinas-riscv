# Firefox Content mmap boundary r24

## Scope

This is a bounded, headless Firefox NewSession probe. It does not run the
15-minute scene gate and does not claim E3. The diagnostic assertion is the
latency of parseable Content-process `mmap(222)` VMA builds.

## Observation

The guest serial stream contains 128 `ASTERINAS_MMAP_DETAIL phase=enter`
records (events 1–128). 125 corresponding `phase=build-done` records are
parseable; the three remaining completion records are visibly interleaved
with Marionette output in the same serial lines (events 5, 38, and one
additional corrupted record), so line-oriented pairing is not sound. The
parseable completion records have `elapsed_jiffies` from 0 through 4
(mean 1.616). The bounded diagnostic assertion therefore passes for the 125
parseable records: none took more than four jiffies. This is evidence that the
mmap VMA-build path is completing for the observed calls, not evidence of a
general mmap deadlock. It does not assert that the three interleaved records
were paired.

The independent Firefox boundary remains failed:

```text
FIREFOX_DRM_PROBE_BOUNDARY name=new-session status=fail type=TimeoutError
FIREFOX_DRM_PROBE_SAMPLE pid=128 state=D (disk sleep) threads=50 rss=203128 kB
FIREFOX_DRM_PROBE_CHILD pid=400 name=Socket Process state=S (sleeping) threads=5
FIREFOX_DRM_PROBE_CHILD pid=418 name=firefox-esr state=R (running) threads=1
FIREFOX_DRM_PROBE_DONE status=fail phase=new-session reason=TimeoutError:_timed_out
```

The browser log still reports Marionette listening on port 2828. Therefore
r24 closes the specific hypothesis that ordinary, parseable Content-process
mmap builds are the direct NewSession blocker, while leaving the NewSession
assertion blocked.

## Artifacts

| Artifact | SHA-256 |
|---|---|
| `desktop-m4-drm.serial.log` | `9bbc4f9e9d0add69066afde36c30234210144a7c80678f2778f6ad60dbe813bd` |
| `result.json` | `e18a8815f9895136444d013a699b102415dc611215584125d18887f0bfa8ecf6` |
| `lifecycle.json` | `432ae0e9aab7bfe12a97c1ba277aa7476ba43c4d930b88918b2e683984dbb9f8` |

Output group: `/mnt/mydisk/qemu-firefox-marionette-probe-r24-headless-container/`.

Next diagnostic boundary: Firefox renderer/Content IPC or a page-fault/exec
handoff after the completed mmap calls. Do not repeat this mmap latency probe
without a new kernel hypothesis or a parser that preserves serial record
ownership.
