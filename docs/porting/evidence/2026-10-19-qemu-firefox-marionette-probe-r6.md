# Firefox Marionette short probe r6

r6 repeated the short probe with syscall summary output. It confirms the same
boundary and adds scheduler evidence:

```text
FIREFOX_DRM_PROBE_BOUNDARY name=greeting status=pass
FIREFOX_DRM_PROBE_SAMPLE pid=127 state=S threads=58 rss=264500 kB schedstat=110147626100_14434965000_4609 syscall=present
FIREFOX_DRM_PROBE_BOUNDARY name=new-session status=fail type=TimeoutError
FIREFOX_DRM_PROBE_SYSCALL pid=127 none
```

The process enters a sleeping state and its `schedstat` values stop changing
while NewSession is pending. Asterinas syscall data is present as a procfs
file, but has no active `current` record at the final snapshot; `wchan` is not
available. Firefox's browser log again reports Marionette listening and
`NewRenderer::Run is slow`.

The evidence localizes the failure to Firefox renderer/session startup after the
Marionette greeting: the browser process becomes quiescent and Firefox logs
`NewRenderer::Run is slow`. It does not by itself prove a kernel scheduler defect
or identify which child thread owns the wait; the next diagnostic must inspect the
captured thread/process records before any runtime retry. No further full or short
QEMU retries are justified until a code or diagnostic change alters that boundary.

Artifacts: `/mnt/mydisk/qemu-firefox-marionette-probe-r6/`

| Artifact | SHA-256 |
|---|---|
| `desktop-m4-drm.serial.log` | `6be711217cd16f614615d7458dcc645d2123405b2d0e7a944e7b3d7b1be920e6` |
| `result.json` | `cec884f30caeffb8121cf2d7059a852233a5b4ff09df05f7b84e2b11be41cfaa` |
| `lifecycle.json` | `e2ec489b5cc321465d9643f404bf1d63ad4400b8b82e3f6020ae859c8846b995` |
