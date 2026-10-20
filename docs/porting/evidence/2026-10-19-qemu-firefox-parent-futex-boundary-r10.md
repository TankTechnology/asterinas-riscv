# Firefox parent futex boundary r10

This is an offline analysis of the already completed r9 short probe; no new
guest was started. The terminal receipt establishes the following ordered
boundary:

```text
A_WEB_MARIONETTE_TRANSPORT command=WebDriver:NewSession send_complete=true
A_WEB_MARIONETTE_TRANSPORT stage=response_header error_type=TimeoutError
FIREFOX_DRM_PROBE_SYSCALL pid=124 sequence=20438 number=98 entered_jiffies=217130
FIREFOX_DRM_PROBE_CHILD pid=393 name=Socket_Process state=S syscall=ppoll(73)
FIREFOX_DRM_PROBE_CHILD pid=410 name=Web_Content state=R threads=2
```

The parent is therefore sleeping in futex(98) when the response header times
out, while Web Content is alive and Socket Process is waiting in ppoll. This
closes the narrower diagnostic claim that the NewSession failure is still at
TCP greeting, GLX initialization, or generic mmap construction. It does not
prove a futex kernel defect: the next check must correlate the parent futex
address/sequence with its cross-process wake or with the IPC reply writer.

Source artifact: `/mnt/mydisk/qemu-firefox-log-channel-r9/desktop-m4-drm.serial.log`
(`8fe407819ea690e72c6079ba0437b5f1d7cfa3c0c5a656dfd61fb0b5da66a1d9`).
