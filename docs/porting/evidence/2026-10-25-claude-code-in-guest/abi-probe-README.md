# abi-probe

A small, dependency-free command-line tool that probes which Linux ABI
features a running kernel actually implements — and whether they behave
correctly, not merely whether the syscall exists.

It was written for this machine, where the kernel is **Asterinas** (a
non-Linux OS exposing a Linux ABI) with a Debian userland. On such a
kernel, features can be missing (`ENOSYS`), silently wrong, or able to
hang a naive test. `abi-probe` isolates every check in a forked child with
a hard timeout, so a broken syscall cannot take down the run.

## Quick start

```sh
make                 # builds ./abi-probe (needs a C compiler + glibc headers)
./abi-probe          # human-readable table
./abi-probe --json   # machine-readable report
make test            # 373 black-box checks of the tool itself
make report          # writes results/report.{txt,json}
```

No root privileges are required. Probes only create files under
`$TMPDIR` (default `/tmp`) and remove them afterwards; network probes use
loopback endpoints only.

## Status semantics

| Status    | Meaning |
|-----------|---------|
| `PASS`    | The feature works and the observed behaviour matched the assertion. |
| `FAIL`    | The call exists but returned wrong data/behaviour, or a required primitive failed. |
| `SKIP`    | The feature is not implemented (`ENOSYS`, or `EINVAL` for `PR_GET_SECCOMP`). |
| `TIMEOUT` | The probe did not finish within `--timeout` ms and was killed. |
| `CRASH`   | The probe child died from a signal or exited without reporting. |

`SKIP` does not affect the exit code. The exit code is `0` when no probe
failed, `1` when any probe failed/timed out/crashed, and `2` for usage
errors. Use `--exit-zero` for report-only runs.

## Options

```
-l, --list             list probes (respects --filter) and exit
-f, --filter TEXT      only probes whose name or category contains TEXT
                       (case-insensitive; may be repeated)
-t, --timeout MS       per-probe timeout in milliseconds (default 3000)
-j, --json             emit a JSON report on stdout
-v, --verbose          include per-probe timing
-q, --quiet            print only the summary line
    --no-color         disable ANSI colors
    --no-fork          run probes in-process (no isolation, no timeout)
    --debug            let probe children write to stdout/stderr
    --selftest         exercise the fail/timeout/crash reporting paths
-x, --exit-zero        always exit 0
-V, --version          print version and exit
-h, --help             print help and exit
```

Examples:

```sh
./abi-probe -f epoll -f fs          # only epoll and filesystem probes
./abi-probe -t 500 -v               # tighter timeout, show per-probe ms
./abi-probe -j | jq '.summary'      # summary for CI
```

## Probe catalog (38)

| Category | Probes |
|----------|--------|
| `core` | `uname`, `ids`, `fork_wait`, `getrandom` |
| `fs` | `file_rw`, `openat2`, `dir_iter`, `symlink_link`, `statfs`, `statx`, `fallocate`, `fcntl_dup`, `close_range` |
| `io` | `pipe_io`, `splice`, `sendfile`, `epoll`, `poll`, `eventfd`, `timerfd`, `signalfd`, `inotify` |
| `ipc` | `socketpair`, `scm_rights`, `unix_bind`, `unix_abstract` |
| `net` | `tcp_loopback` |
| `mm` | `mmap_fork`, `mprotect` |
| `proc` | `procfs`, `prctl_name`, `sched`, `pidfd` |
| `time` | `clock` |
| `threads` | `pthreads` |
| `security` | `memfd_seal`, `seccomp`, `io_uring` |

Run `./abi-probe --list` for one-line descriptions. The checks go beyond
"does the syscall return 0": e.g. `scm_rights` verifies the received fd
refers to the same inode, `memfd_seal` verifies writes get `EPERM`,
`mmap_fork` verifies shared/private fork semantics, `statx` cross-checks
size/mode/ino/nlink against `fstat` and `AT_EMPTY_PATH`, and `mprotect`
verifies the permission change is visible in `/proc/self/maps`.

## JSON report

```json
{
  "tool": "abi-probe",
  "version": "1.1.0",
  "generated_at": "2026-10-25T11:02:30Z",
  "system": {"sysname": "Linux", "release": "5.13.0", "machine": "x86_64",
             "nodename": "thinkpad", "cpus": 4, "page_size": 4096,
             "libc": "glibc 2.41"},
  "timeout_ms": 3000,
  "results": [
    {"name": "epoll", "category": "io",
     "description": "epoll ADD/MOD/DEL, EPOLLIN/EPOLLOUT",
     "status": "pass", "detail": "...", "ms": 4}
  ],
  "summary": {"total": 38, "passed": 34, "failed": 0, "skipped": 4,
              "timeout": 0, "crash": 0, "duration_ms": 197}
}
```

## Design notes

- **Isolation.** The parent forks a child per probe, redirects the child's
  stdout/stderr to `/dev/null` (unless `--debug`), and enforces the
  deadline itself by polling `waitpid(WNOHANG)` and sending `SIGKILL`.
  Relying on the parent's clock means the timeout also works when `alarm`
  or signal delivery is broken — a real possibility in a partial ABI.
- **Cleanup.** Each probe registers its scratch directory globally; the
  child removes it on every exit path, including failures. The parent
  verifies nothing is left behind (see the test script).
- **Self-test.** `--selftest` runs three hidden probes (always-fail,
  hang, self-kill) through the real runner and checks the `FAIL` /
  `TIMEOUT` / `CRASH` paths. The Python suite additionally covers the CLI
  contract, JSON schema, filters, exit codes and temp-file cleanup.
- **Limits.** A probe is a point check, not a conformance suite. `SKIP`
  hides differences between a clean `ENOSYS` and an `EINVAL` "unknown
  option"; the detail text preserves the errno so the distinction is
  visible. `--no-fork` is intended for debugging only.

## Result on this machine

`2026-10-25`, Asterinas kernel reporting `Linux 5.13.0 x86_64`, Debian 13
userland, glibc 2.41: **34 passed, 0 failed, 4 skipped** in ~0.2 s.

Not implemented on this kernel:

| Probe | Observation |
|-------|-------------|
| `openat2` | `ENOSYS` (Function not implemented) |
| `splice` | `ENOSYS` (Function not implemented) |
| `io_uring` | `ENOSYS` (Function not implemented) |
| `seccomp` | `PR_GET_SECCOMP` returns `EINVAL` instead of a mode |

Everything else — including `pidfd`, `memfd` sealing, `close_range`,
`inotify`, `signalfd`, abstract Unix sockets and `SCM_RIGHTS` fd passing,
TCP loopback, `epoll`, `timerfd`, `eventfd`, `fallocate` punch-hole,
`sendfile`, `statx` (including `AT_EMPTY_PATH`), `/proc` parsing and
mprotect/maps consistency — worked as on mainline Linux.

The current raw output is in `results/report.txt` and
`results/report.json` (regenerate with `make report`).
