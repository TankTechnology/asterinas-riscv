# Firefox Marionette independent probe r24

## Result

r24 used the probe overlay that replaces `firefox-drm-scene.service` with
`firefox_marionette_probe.service`.  The detached host runner kept QEMU alive
through the RISC-V TCG cold start and the guest emitted the complete bounded
probe receipt.

The probe reached the Marionette greeting, sent one minimal
`WebDriver:NewSession`, and timed out waiting for its response header.  The
receipt contains two timeline samples (`sample_count=2`), including:

- Firefox parent PID 131, 47–51 threads, alive;
- Socket Process PID 399 sleeping in `ppoll(73)`;
- parent threads in `ppoll(73)` and `futex(98)`, with scalar arguments;
- browser log saying Marionette is listening on port 2828;
- empty MOZ renderer log because no session was created.

This closes the independent-probe evidence requirement, but E3 remains
blocked at Firefox userspace NewSession/IPC initialization.  No scene,
screenshot, frame-rate, or recovery gate was run.

## Key serial markers

```text
FIREFOX_DRM_PROBE_BOUNDARY name=greeting status=pass
FIREFOX_DRM_PROBE_SAMPLE sample_index=0 ...
FIREFOX_DRM_PROBE_SAMPLE_CHILD ... syscall=sequence=1011,number=73,...
FIREFOX_DRM_PROBE_SAMPLE_THREAD ... number=98,arg0=274387579768,arg1=393
FIREFOX_DRM_PROBE_BOUNDARY name=new-session status=fail type=TimeoutError
FIREFOX_DRM_PROBE_RECEIPT ... phase=new-session status=fail sample_count=2
FIREFOX_DRM_PROBE_DONE status=fail phase=new-session
```

## Artifacts

| Artifact | SHA-256 |
|---|---|
| serial log | `0e4d5e62de409259d7cf37cda88af84acdc97429d1c5a7a6dc541a36dbce7cee` |
| result | `aa064a96645e1cbe4b25dc130a769550fd5af93a217b41b7f9d02483764c4bef` |
| lifecycle | `34d9ed117d85e07fec3d8044f31af62f2531450e307cf9708df002805934dd1a` |
| root image | `1b9cc72eba3c15559af630b43720c470326220a6da90bc6e5d75aed5811fa9a0` |
| root manifest | `092bf54b121d8decef90c2e1798b652ba20bf59e174ce97f7271cfdf8859aef2` |

Output group: `/mnt/mydisk/qemu-firefox-marionette-probe-r24-container/`

