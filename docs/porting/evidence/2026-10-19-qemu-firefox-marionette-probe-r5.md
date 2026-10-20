# Firefox Marionette short probe r5

## Boundary

r5 is the first probe that sampled the process while `NewSession` was
blocked. It reached a valid greeting, sent the complete NewSession frame, and
timed out with no response.

Representative samples:

```text
FIREFOX_DRM_PROBE_SAMPLE pid=122 state=R threads=50 rss=210552 kB schedstat=71017876200_12846557000_4608 syscall=present
FIREFOX_DRM_PROBE_SAMPLE pid=122 state=S threads=59 rss=261192 kB schedstat=97645250500_14742247200_5906 syscall=present
FIREFOX_DRM_PROBE_BOUNDARY name=new-session status=fail type=TimeoutError
```

The final receipt also records 59 threads, stable RSS around 261 MiB, and
`asterinas_syscall` data available for the process and threads. `wchan` remains
unavailable in this guest procfs. Firefox's browser log says:

```text
Marionette INFO Listening on port 2828
Crash Annotation GraphicsCriticalError: NewRenderer::Run is slow: 2597
```

This localizes the stall to Firefox's renderer startup/NewSession handling,
after Marionette is listening and after the virgl path is available. It is not
a missing port, DRM node, or capability-shape problem.

## Artifacts

- Rootfs SHA-256: `a0d85d34ff5bf949a6dc7b0028b448f0a7abfce235b5ce80ec3d0516f5171c0c`
- Rootfs manifest SHA-256: `edf937a94f03a80815aabca03163d15efe15bc16948ce24d97dd640d77f87ce3`
- Serial SHA-256: `c1147f609d9249119e6cc167d606c10f4a463cb9e571560d54b871239291ce7b`
- Result SHA-256: `e4b9e15666c7fb33ac67e71f3802d19a5a4a2f5bef1b53c9bdb7e290e4ea03a5`
- Lifecycle SHA-256: `c3b6b506cdd8ead646fe61f9d58c2be8bcc656fd19f2f3123d74b277ef15d854`
- Output group: `/mnt/mydisk/qemu-firefox-marionette-probe-r5/`

No scene, screenshot, frame, or recovery step was attempted.
