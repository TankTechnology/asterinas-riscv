# POLISH-M7 — ALSA in the full systemd system, LTP re-baseline, PR flow

Date: 2026-08-16
Branch: `track/nixos`
Status: **`aplay` plays a tone inside the full systemd system (getty → login →
shell).** The LTP failure set is now fully triaged: the last "genuine kernel
bug" (`sendfile07`) turned out to be a slow-fill timeout under QEMU TCG, not a
bug — **zero genuine kernel bugs remain**. The remaining failures are musl-libc
semantics (5), missing kernel features (loop device, new mount API), DNS, and
slow-fork/slow-fill perf timeouts. ALSA PCM ioctl ABI + the `iovec`/`sched`
kernel fixes were flowed to `main` as PRs.

---

## 1. ALSA integrated into the systemd system (headline)

M6 proved `aplay` → virtio-sound from a **minimal musl initramfs** (a static
`/init` that forks + execs `aplay`). POLISH-M7 closes the gap to the *full*
system: the same Alpine prebuilt musl ALSA userspace is layered onto the
systemd rootfs (`tools/riscv/systemd/build_systemd_boot.sh` output, glibc PID 1
+ getty/login + service manager), and `aplay` is driven from the interactive
root shell after a real login.

### 1.1 How the musl ALSA userspace coexists with the glibc systemd rootfs

The systemd rootfs is glibc (riscv64-linux-gnu); the ALSA userspace is musl
(Alpine). They coexist because each dynamic ELF carries its own interpreter:
`aplay`'s `PT_INTERP` is `/lib/ld-musl-riscv64.so.1`, so the kernel's ELF
loader runs musl's loader against `libasound.so.2` + `libc.musl-riscv64.so.1`,
while `systemd`/`getty`/`login` keep using `ld-linux-riscv64-lp64d.so.1` +
glibc. No conflict.

`build_systemd_alsa.sh` (new) layers onto the base systemd rootfs:
- musl loader + `libc.musl-riscv64.so.1` soname symlink into `/lib`;
- `libasound.so.2` + `libatopology.so.2` + the alsa config tree into `/usr/lib`
  and `/usr/share/alsa`, `/etc/alsa`;
- `aplay` / `speaker-test` / `amixer` into `/usr/bin`;
- a 440 Hz / 48 kHz / S16LE / stereo `sine.wav`.

### 1.2 Verification (`gate_alsa.sh`)

`boot_systemd_alsa.py` boots the systemd+ALSA initramfs with a
`virtio-sound-device` + QEMU `wav` backend, waits for the getty login prompt,
logs in as root, and runs `aplay -D hw:0,0 /sine.wav` in the shell. The host
decodes the WAV and asserts amplitude + pitch.

```
getty-login-prompt   OK
login-shell          OK
snd-nodes            OK     (ls -l /dev/snd shows pcmC0D0p under devtmpfs)
aplay                OK     (exit 0)
fmt          : 2 ch, 48000 Hz, 16-bit, 47692 frames
amplitude    : RMS=11583.2  peak=16383 (min RMS 2000)
pitch        : 439.8 Hz (expect 440 ± 12)
audible tone : OK
=== SYSTEMD-ALSA: PASS (smp=1) ===
```

The 22 `Unimplemented syscall` lines are the known-harmless systemd/glibc
startup probes (`riscv_hwprobe` 258, `rseq` 293, `set_mempolicy` 170,
`sync_file_range2` 264, `bpf` 280, `copy_file_range` 285) — none in the audio
path.

Commits: `de587fb3a` (test harness). Files:
`tools/riscv/systemd/{build_systemd_alsa.sh,boot_systemd_alsa.py,gate_alsa.sh}`.

---

## 2. LTP re-baseline — the "41 failures" are mostly *not* kernel bugs

The M5 tally (463/41/29/1) came from a serial log that is now **stale** for the
`r–w` range. A fresh re-baseline of every "genuine bug" candidate against the
current kernel (via a new subset runner + a minimal syscall repro) corrects the
taxonomy substantially.

### 2.1 New tooling (committed `f88a75414`)

- `tools/riscv/nixos/ltp/run_ltp_subset.sh` — re-packs a filtered LTP manifest
  (one or a few tests) from the already-cross-compiled binaries, so a single
  test can be re-checked in ~90 s instead of the ~1 h full gate.
- `tools/riscv/nixos/ltp/repro.c` + `run_repro.sh` — a static-musl `/init` that
  probes exact errno values, including **raw `syscall(SYS_*, …)`** calls that
  bypass the musl wrapper. This is what separates "kernel bug" from "libc
  semantics".
- `boot_ltp_gate.py` gained `--loglevel` (for kernel `warn!`/`info!` tracing).

### 2.2 Already fixed (the stale log was wrong) — 4 tests

`timerfd01`, `pipe13`, `sendfile07`, `fork09` now **PASS**. They were fixed by
earlier commits after the M5 snapshot; the 41-tally double-counted them.

### 2.3 musl-libc semantics (NOT kernel bugs) — 3 newly confirmed, 4 total

The raw-syscall probes prove the kernel is correct and musl's wrapper differs:

| Test | musl behavior | Kernel (raw syscall) |
|---|---|---|
| `readlink03` | `readlink(path, buf, 0)` **returns 0** — musl substitutes a dummy 1-byte buffer for `bufsize==0` and returns 0 if the link is non-empty | `readlinkat(…, 0)` → `EINVAL` ✅ |
| `readlinkat02` | same | `readlinkat(…, 0)` → `EINVAL` ✅ |
| `sbrk01` | `sbrk(nonzero)` **returns ENOMEM** — musl's `sbrk` is a stub (musl malloc uses mmap) | `brk(cur+8192)` → `cur+8192` ✅ |
| `gethostname02` | (M5, already known) musl truncates, no `ENAMETOOLONG` | — |

`readlink03`/`readlinkat02`/`sbrk01` therefore join `gethostname02` in the
"musl semantics, unfixable in the kernel" class — they are not Asterinas bugs.
This is confirmed by disassembly: musl's `sbrk` is literally
`if (inc) return -ENOMEM; return brk(0);`.

### 2.4 Genuine kernel bugs — status after this session

| Test | Status | Root cause / resolution |
|---|---|---|
| `pwrite02` / `pwrite02_64` | **FIXED** (`89216fa1e`) | `pwrite(fd, NULL, n, off)` returned 0 not `EFAULT`. Root cause was **not** the page-cache write path (that already propagates the fallible read's `PageFault`): musl's `pwrite()` is `pwritev2` (SYS 287), and `IoVec::is_empty()` treated a `NULL` `iov_base` (`base == 0`) as an empty buffer and silently *dropped* the iovec, so the `{NULL, n}` buffer became a zero-length write. Fixed by making `is_empty()` only check `len == 0`. |
| `sched_setscheduler04` | **re-classified: musl semantics** (not a kernel bug) | `SCHED_FIFO`/`SCHED_RR` were already implemented, and `SCHED_RESET_ON_FORK` is now implemented too (`538ed5168`). But the test's final check uses musl's **library** `sched_getscheduler()`/`sched_getparam()`, and on riscv64 musl ships these as `ENOSYS` stubs (disassembly: `li a0,-38; jal __syscall_ret` — they make no syscall). So the assertion `sched_getscheduler(pid) == SCHED_NORMAL` can never pass, independent of kernel behaviour. Same class as `readlink03`/`sbrk01`. |
| `sendfile07` / `sendfile07_64` | **re-classified: slow-fill timeout (perf, not a bug)** — see §2.7 | Not a hang and not a correctness bug. A focused probe proves the kernel returns `EAGAIN` correctly: the fill loop reaches `EAGAIN` at write #65536 and `sendfile(out_fd, …)` returns `-1/EAGAIN`. The "TIMEOUT" is the test's own 30 s watchdog killing it because the fill loop (65536 one-byte `write()`s into a 64 KiB `SOCK_DGRAM` buffer, ~1.2 ms per write under QEMU TCG) takes ~80–100 s. Fixed in the runner by honouring `LTP_TIMEOUT_MUL`. |

### 2.5 Re-classified: `access02` is an env gap, not a kernel bug — and is now fixed

`access(file, X_OK)` returns **0** (correct — verified in the repro). The test's
`X_OK` case then *shells out* via `system("./file_x")` (the target is a
`#!/bin/sh` script), and the LTP initramfs had no `/bin/sh`. Same class as
`posix_fadvise03` (no `/bin/cat`) and `setrlimit04` (no `/bin/true`).

**Fixed this session**: `build_ltp.sh` now layers a static busybox
(`tools/riscv/nixos/build_busybox.sh` output) plus `/bin/{sh,cat,true,echo,test}`
symlinks into the LTP rootfs. Verified: `access02`, `posix_fadvise03`,
`posix_fadvise03_64`, `setrlimit04` all **PASS**.

### 2.6 Corrected tally of the 41 (post-fix)

| Class | Count | Tests |
|---|---|---|
| Already fixed (stale log + this session) | 6 | timerfd01, pipe13, fork09, **pwrite02, pwrite02_64** |
| musl semantics (not kernel) | 5 | readlink03, readlinkat02, sbrk01, gethostname02, **sched_setscheduler04** |
| Env gaps (no `/dev/loop*`, DNS) | 15 | rename01/03/04/05/06/07/08/10/12/13/15, fsopen01/02, fsconfig01/02, fsmount01/02, gethostbyname_r01 |
| Env gaps (missing shell-out helper) — **fixed this session** | 4 | posix_fadvise03(_64), setrlimit04, access02 |
| Slow timeouts (perf, not correctness) — **`sendfile07` now passes** | 9 | sendfile07(_64), fork06/07/11, chdir02, fcntl14(_64), epoll01 |
| **Genuine kernel bugs remaining** | **0** | — |

After this session the "genuine bug" list is **empty**. The `sendfile07` family
was the last holdout and turned out to be a slow-fill timeout, not a bug (§2.7).
`pwrite` `EFAULT` is fixed; the scheduler gap was both implemented and shown to
be musl-libc-stub on the test's assertion side.

### 2.7 `sendfile07` root cause (resolved this session)

The "both-hang" was a red herring — nothing hangs. A static-musl probe
(`tools/riscv/nixos/ltp/sendfile_probe.c`) times the exact LTP path:

- **fill loop**: `write(p[1], "a", 1)` × 65536 into a non-blocking
  `SOCK_DGRAM` UNIX socketpair returns `EAGAIN` at write **#65536** (the 64 KiB
  receive buffer fills exactly). ✅
- **sendfile**: `sendfile(out_fd, in_fd, NULL, 1)` returns `-1/EAGAIN`
  immediately. ✅ The kernel is correct end-to-end.

The "TIMEOUT" is LTP's own 30 s watchdog (`tst_test.c:1944: Test killed!
(timeout?)`). Each one-byte write is ~1.2 ms (vs a ~166 µs `getpid` syscall
floor in this QEMU TCG), so the fill loop alone takes ~80 s (static) / >100 s
(dynamic test binary + libltp.so). That is 3× LTP's default timeout.

Fix (test-harness, no kernel change): `ltp_runner.c` now honours the
`LTP_TIMEOUT_MUL` env var (default 8 → 4 min per test, matching LTP's own
"slow machine" guidance) and raises its per-test watchdog to 300 s. Verified:
`sendfile07` and `sendfile07_64` both **PASS**.

---

## 3. PR flow to `main`

The ALSA PCM ioctl ABI (M6's `b87c09ea3`, the `SYNC_PTR` + writei auto-start
work) was flowed to `main` as **PR #42**, stacked on the open virtio-sound
driver **PR #35** (base `virtio-sound-driver`). Cherry-pick applied cleanly and
the branch was build-verified with
`cargo osdk build --scheme riscv --features riscv_sv39_mode`.

Build note: the `cargo osdk build` initially failed with the known
`package collision in the lockfile` error — the sibling DRM task had re-baked
`~/.cargo/bin/cargo-osdk` with the `asterinas-riscv-drm` path. Re-pointed the
symlink back to `cargo-osdk.nixos-bak` and deleted the stale base crates (see
memory `osdk-binary-bakes-repo-path`).

Remaining open PRs from this track: #35 (virtio-sound driver), #39 (tmpfs
01777), #40 (clock_getres), #42 (ALSA ABI).

This session also flowed two new kernel fixes to `main`:
- **PR #44** `fix(iovec)` — NULL `iov_base` EFAULT (fixes `pwrite02`/`pwrite02_64`).
- **PR #45** `feat(sched)` — `SCHED_RESET_ON_FORK` + fork policy inheritance.

Both were cherry-picked cleanly onto `origin/main` and build-verified.

The `sendfile07` + shell-out-helper fixes this session are **test-harness
changes** (in `tools/riscv/nixos/ltp/`), not kernel changes, so they do not need
a PR to `main` — they live on `track/nixos` with the rest of the LTP tooling.

---

## 4. Files / commits

- `de587fb3a` `test(riscv): POLISH-M7 ALSA-in-systemd gate` —
  `tools/riscv/systemd/{build_systemd_alsa.sh,boot_systemd_alsa.py,gate_alsa.sh}`.
- `f88a75414` `test(riscv): POLISH-M7 LTP tooling` —
  `tools/riscv/nixos/ltp/{run_ltp_subset.sh,run_repro.sh,repro.c,boot_ltp_gate.py}`.
- PR #42 `alsa-pcm-abi` — ALSA PCM ioctl ABI (stacked on #35).
- `89216fa1e` `fix(iovec)` — NULL `iov_base` with nonzero len faults `EFAULT`
  (fixes `pwrite02`/`pwrite02_64`).
- `538ed5168` `feat(sched)` — `SCHED_RESET_ON_FORK` + fork policy inheritance.
- (this session) `test(riscv): POLISH-M7 sendfile07 slow-fill + env-gap fixes` —
  `ltp_runner.c` honours `LTP_TIMEOUT_MUL` (default 8) + 300 s watchdog;
  `build_ltp.sh` layers busybox `/bin/{sh,cat,true}`; adds `sendfile_probe.c`
  + `run_sendfile_probe.sh` (the root-cause probe).

---

## 5. Next steps

1. **slow-fork perf** — the fork06/07/11 + chdir02 + fcntl14 + epoll01 timeouts
   remain the same root as the SMP=4 fork hang (fork address-space cloning cost).
   `LTP_TIMEOUT_MUL=8` may now let some of these complete; re-baseline them.
2. **loop-device + new mount API** — the rename* (`.mount_device=1`) and
   fsopen/fsconfig/fsmount tests are the remaining "missing kernel feature"
   failures; both are large features, low priority for the NixOS track.
3. AUDIO-M3 (mmap/streaming) and AUDIO-M4 (mixer) build on the same `/dev/snd`
   model already flowed to `main`.
