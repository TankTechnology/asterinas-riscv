# Firefox Socket Process epoll probe r23

## Result

r23 filtered epoll details to pid 300+ and reached the Firefox Socket Process
before the NewSession timeout.  The serial trace contains:

```text
ASTERINAS_EPOLL_PROFILE call=9 pid=399 tid=401 epfd=10 max_events=32 timeout=None
ASTERINAS_EPOLL_PROFILE calls=9 phase=return pid=399 tid=401 events_len=1
ASTERINAS_EPOLL_PROFILE calls=16 phase=return pid=399 tid=401 events_len=1
```

The Socket Process is therefore making epoll progress and receiving events.
Together with r22's parent epoll evidence, this rules out a permanently stuck
parent or Socket Process epoll wait.  NewSession still times out after the
greeting, so the next boundary is Content/renderer process readiness or the
Marionette session state transition.

## Artifacts

| Artifact | SHA-256 |
|---|---|
| `desktop-m4-drm.serial.log` | `30f7913e53613e364b0cd052bc1bbd0ba51fff158943e4c4646bb70653d748cb` |
| `result.json` | `1cac8d9d63125d217c0bf8f7af4607bbfa7bdba2f814878c995a4f55decd18fa` |
| `lifecycle.json` | `4b9b2edc692324ea5b275fc6b14d1587f3fb6469ef19098526081add3beeb480` |

Output group: `/mnt/mydisk/qemu-firefox-marionette-probe-r23-headless-container/`
