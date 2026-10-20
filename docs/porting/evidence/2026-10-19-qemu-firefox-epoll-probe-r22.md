# Firefox epoll probe r22

## Result

r22 enabled the opt-in epoll profile and filtered detail records to pid 100+
so Firefox's calls were visible.  In headless mode, Marionette greeting still
passed and NewSession still timed out, but the Firefox parent epoll calls were
actively returning events:

```text
ASTERINAS_EPOLL_PROFILE call=1 pid=131 tid=251 epfd=20 max_events=256 timeout=None
ASTERINAS_EPOLL_PROFILE calls=1 phase=return ... events_len=1
ASTERINAS_EPOLL_PROFILE call=3 pid=131 tid=131 epfd=3 max_events=256 timeout=None
ASTERINAS_EPOLL_PROFILE calls=3 phase=return ... events_len=2
ASTERINAS_EPOLL_PROFILE calls=9 phase=etime ...
```

The profile recorded 16 Firefox-parent calls in the bounded early window;
most returned one or two events, with a few normal zero-timeout `ETIME`
results.  This rules out a permanently blocked `epoll_pwait` in the parent as
the direct NewSession cause.  The remaining boundary is the IPC/Content child
startup or a later Firefox state transition.

## Artifacts

| Artifact | SHA-256 |
|---|---|
| `desktop-m4-drm.serial.log` | `d46a6d826cef462c3d6c8726f9034b05d59d4f305e304c8d4478fabac7037fff` |
| `result.json` | `650380c67b0173eb7a433719985472766fae41d5d5ca9d30d4dfe66c08ac8699` |
| `lifecycle.json` | `fe1f8b7522e650d6547759401711487e7fa32546dbf9b66ab687506eddd13d03` |

Output group: `/mnt/mydisk/qemu-firefox-marionette-probe-r22-headless-container/`
