# Firefox Web Content mmap boundary r23

The r23 guest process receipt contains three direct Firefox children:

```text
pid=399 name=Socket Process state=S threads=5 syscall=ppoll(73)
pid=417 name=Web Content state=R threads=2 syscall=mmap(222)
pid=421 name=firefox-esr state=R threads=1 syscall=none
```

At the same time, the parent had completed the Marionette greeting but its
`NewSession` request timed out.  The Socket Process epoll probe was making
progress.  RISC-V syscall 222 is `mmap`, so the next concrete boundary is
Content-process memory mapping/startup rather than a parent or Socket Process
epoll wait.

This is a diagnostic localization only; it does not yet prove that mmap is
deadlocked or that the kernel mmap implementation is incorrect.

Artifacts are the r23 group:
`/mnt/mydisk/qemu-firefox-marionette-probe-r23-headless-container/`.
