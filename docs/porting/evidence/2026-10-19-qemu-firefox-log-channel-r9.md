# Firefox diagnostic log channel r9

## Runtime result

The bounded headless DRM probe used the reduced diagnostic stream and kept the
Firefox-only loglevel at 4. The serial artifact is 635 KiB, versus roughly
8 MiB in r7. The GLX preflight completed with `FIREFOX_DRM_GLXTEST rc=0`.

Diagnostic records remained observable:

- mmap detail: 255 records (the bounded stream reached its cap);
- epoll profile: 31 records;
- futex wait detail: 254 records;
- futex wake detail: 1 record, because zero-wakeup calls are now omitted.

Firefox still did not complete the application gate. It reached the
`probe-new-session` phase and timed out after 146.3 seconds; no fixed scene was
started. The transport receipt shows the NewSession frame was fully sent, but
no response header arrived. At the terminal sample the Socket Process was in
`ppoll(73)` and Web Content pid 410 was running, so the remaining boundary is
Content/renderer-to-parent IPC or the response generation path after GLX, not
host setup, mmap construction, or serial volume.

```text
A_WEB_MARIONETTE_TRANSPORT command=WebDriver:NewSession send_complete=true
A_WEB_MARIONETTE_TRANSPORT stage=response_header error_type=TimeoutError
FIREFOX_DRM_PROBE_CHILD pid=393 name=Socket_Process syscall=ppoll(73)
FIREFOX_DRM_PROBE_CHILD pid=410 name=Web_Content state=R
```

## Artifacts

| Artifact | SHA-256 |
|---|---|
| `desktop-m4-drm.serial.log` | `8fe407819ea690e72c6079ba0437b5f1d7cfa3c0c5a656dfd61fb0b5da66a1d9` |
| `result.json` | `3e834c4e40cacac8dba4763ff9239845ff3107fd65cceaae6d54d855277bcfd3` |
| `lifecycle.json` | `1d448a58c4fb1c02a55956d7af2418f7218abb28035753f678269b31218f8222` |

Output group: `/mnt/mydisk/qemu-firefox-log-channel-r9/`.
