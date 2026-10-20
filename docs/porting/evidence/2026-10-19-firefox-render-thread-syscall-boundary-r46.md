# Firefox render-thread syscall boundary r46

This is a host-side analysis of the existing r44/r45 serial logs; it starts no
guest and changes no runtime artifact.

The display-order fix is confirmed by r44: `xdpyinfo` passes before glxtest,
and glxtest reports virgl on `renderD128`. The remaining Firefox boundary is
not a missing DRM node:

| Process | Dominant observed waits | Maximum elapsed jiffies | Interpretation |
|---|---|---:|---|
| Firefox parent (r44 PID 302) | futex(98), ppoll(73) | 32129 / 14337 | parent/render-thread synchronization |
| Socket Process (r44 PID 428) | ppoll(73), futex(98), epoll_pwait(22) | 54203 / 54196 / 53856 | socket-process wait while session startup remains pending |
| Web Content (r44 PID 479) | futex(98) | 4181 | content-process startup synchronization |

The Firefox log records `RenderThread::InitDeviceTask is slow: 3689`; r45
keeps the same boundary after extending the probe budget from 150 to 300
seconds. The formal E3 scene therefore remains blocked at Firefox userspace
startup, after virgl discovery and before a WebRender scene response.

The next kernel investigation should correlate these futex/ppoll waits with
the corresponding `asterinas_syscall` addresses and scheduler wake records;
there is no basis for another NewSession timeout increase.

The existing futex detail stream also shows long waits ending as `woken` or
`spurious`, rather than a queue with only missing wakeups. The largest sampled
waits are 15,898 and 13,021 jiffies with `outcome=woken`, while repeated
spurious waits occur on the content-side addresses. This points to scheduler
or userspace initialization latency after a wake, not a proven lost-wake bug.

## Sources

- r44 serial: `/mnt/mydisk/qemu-firefox-marionette-probe-r44-container/desktop-m4-drm.serial.log`
- r45 serial: `/mnt/mydisk/qemu-firefox-marionette-probe-r45-container/desktop-m4-drm.serial.log`
- [r44 runtime evidence](2026-10-19-qemu-firefox-marionette-probe-r44.md)
- [r45 runtime evidence](2026-10-19-qemu-firefox-marionette-probe-r45.md)
