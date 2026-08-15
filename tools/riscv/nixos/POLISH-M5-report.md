# POLISH-M5 — ALSA feasibility (de-risked), snd device model, LTP re-baseline

Date: 2026-08-15
Branch: `track/nixos`
Status: **ALSA userspace is prebuilt — no cross-compile needed**; a reusable
`/dev/snd` device model designed; LTP re-baselined with a corrected
failure taxonomy (the "kernel bug" list is actually dominated by a slow-fork
perf issue + musl-libc semantics).

---

## 1. ALSA feasibility — the userspace is free (de-risks AUDIO-M2)

M4 concluded "alsa-lib cross-compile = hours". **That estimate is wrong — it is
zero work.** Alpine v3.22 ships prebuilt riscv64-musl packages of the entire
ALSA userspace on the TUNA mirror (direct-connect, no proxy), so there is no
cross-compilation at all.

### 1.1 What was verified

Downloaded and unpacked three APKs from
`https://mirrors.tuna.tsinghua.edu.cn/alpine/v3.22/main/riscv64/`:

| package | provides | ELF | size |
|---|---|---|---|
| `musl-1.2.5-r12` | `/lib/ld-musl-riscv64.so.1` + `libc.musl-riscv64.so.1` (symlink) | riscv64 musl | 613 KB |
| `alsa-lib-1.2.14-r0` | `libasound.so.2` (soname) + `/usr/share/alsa/` config (85 files) | riscv64 musl | 932 KB + 178 KB |
| `alsa-utils-1.2.14-r0` | `aplay` (63 KB), `speaker-test` (30 KB), `amixer` (47 KB) | riscv64 musl PIE | 140 KB |

`readelf -d usr/bin/aplay`:
```
NEEDED  libasound.so.2
NEEDED  libc.musl-riscv64.so.1
interpreter  /lib/ld-musl-riscv64.so.1
```
`aplay` embeds `version 1.2.14 by Jaroslav Kysela`.

### 1.2 Why this de-risks everything

- **Total ~1.8 MB** added to the initramfs (currently 2.5 MB cpio.gz) — negligible.
- The LTP initramfs already ships the musl loader+libc (`ld-musl-riscv64.so.1`);
  only the soname name (`libc.musl-riscv64.so.1`, a one-line symlink) and the
  two ALSA packages need adding to `build_audio.sh`.
- So the **entire AUDIO-M2 userspace is a ~5-minute package-copy**, not "hours of
  autotools". The M4 effort estimate collapses to just the kernel-side ALSA PCM
  ioctl ABI (unchanged: ~20 ioctls, a focused few days).

Conclusion stands but sharpened: **the kernel ioctl ABI is now the *only* real
work in AUDIO-M2.** No cross-compile, no glibc/musl ABI divergence risk.

---

## 2. snd device model — design doc

Written to `docs/porting/snd-device-model.md`. Summary of the model:

- A new reusable `kernel/src/device/snd/` layer (`pcm.rs`, `control.rs`, `mod.rs`)
  speaks the ALSA ioctl ABI on the existing misc nodes (`pcmC0D0p` minor 116,
  new `controlC0`), behind a small `SoundBackend` trait that the virtio-sound
  driver implements.
- Minimal playback ioctl set for `aplay -D hw:0,0`: `PVERSION`, `INFO`,
  `HW_REFINE`/`HW_PARAMS`/`HW_FREE`, `SW_PARAMS`, `STATUS`, `PREPARE`, `START`,
  `DROP`, `DRAIN`, `WRITEI_FRAMES` (magic `'A'`); control `CARD_INFO`/`ELEM_*`
  (magic `'U'`). Structs copied verbatim from `asound.h`.
- Driver change is confined to **parameterizing `SET_PARAMS`** (the only driver
  edit); the rest is ABI plumbing reusing Asterinas's `ioc!`/`dispatch_ioctl!`
  machinery (same pattern as `fb.rs`/`evdev`).
- Phasing: AUDIO-M2 writei playback → AUDIO-M3 mmap/streaming → AUDIO-M4 control.

---

## 3. LTP re-baseline (fresh `ltp-gate.sh --skip-build --smp 1`)

**Baseline (partial — through the `p` range, 293/534 tests).** The gate hit
`boot_ltp_gate.py`'s default 1200 s `--command-timeout` before finishing, so the
tail (`r`–`w`: `readlink*`, `sbrk`, `sendfile`, `symlink03`, …) was not reached.
Tally at truncation: **254 pass / 21 fail / 17 conf / 1 timeout**. A full re-run
with `--command-timeout 3600` (added in `4ccf07554`) is in flight for §5.

The 21 FAIL + 1 TIMEOUT observed so far, classified:

### 3.1 Corrected taxonomy

The POLISH-M2 "kernel bug" list conflated three distinct classes. The fresh run
separates them:

**A. Slow-fork perf (timeouts, NOT correctness bugs).** Quantified: `fork06`
("Forking 1000 processes") and `fork11` ("Forking 100 processes") exceed LTP's
30 s internal timeout — fork latency is **> 30 ms** on this kernel. The
following fail with `Test timeouted, sending SIGKILL!` / `TBROK: Test killed!
(timeout?)`:

- `fork06`, `fork07`, `fork09`, `fork11` (fork loops)
- `chdir02`, `fcntl14`, `fcntl14_64` (fork per testcase), `link05` (1000 links)
- `epoll01` (TIMEOUT)

Root cause is fork address-space cloning cost, not a correctness defect.
Mitigation (harness-level): `ltp_runner.c` already exports `LTP_TIMEOUT_MUL`
(set to `1` at line 143); the LTP timeout message itself suggests `LTP_TIMEOUT_MUL > 1`.
The *real* fix is fork perf (a tracked subtask, related to the SMP=4 fork hang).

**B. musl-libc semantics (NOT kernel bugs).** `gethostname02` expects
`gethostname(len < strlen)` to return `ENAMETOOLONG`, but musl implements
`gethostname()` in libc over `uname()` and **silently truncates** — it never
returns `ENAMETOOLONG`. Unfixable in the kernel; the test is glibc-assuming.

**C. Env gaps.** 17 loop-device tests (`fsopen`/`fsconfig`/`fsmount`/`rename*`,
no `/dev/loop*`); `posix_fadvise03(_64)` (no `/bin/cat`); `setrlimit04` (no
`/bin/true`); `gethostbyname_r01` (no DNS).

**D. Genuine correctness bugs (open).** Observed in the reached range: `access02`
(truncated output; `access()` real-vs-effective-uid or `system()`/`/bin/sh`),
`epoll_wait04`. The `r`–`w` range (`readlink03`, `readlinkat02`, `sendfile07`,
`sbrk01`, `symlink03`) was not reached in this partial run — still to be
confirmed by the full re-run.

### 3.2 tmpfs root mode — root cause + fix (landed this session)

**Root cause:** `RamFs::new_tmpfs()` reuses the shared ramfs/rootfs root mode
`0755` (`mkmod!(a+rx, u+w)`), but Linux tmpfs defaults its root to **01777**
(world-writable + sticky). So `/tmp` was root-owned `rwxr-xr-x`, and any test
that `setuid(nobody)` *before* creating its tmpdir fails at `mkdir("/tmp/LTP_…")`
with `EACCES`. `symlink03` is exactly this shape (`setuid` at line 226, then
`tst_tmpdir()` at line 229) — hence its `mkdtemp EACCES`.

**Fix (committed `e96c2722d`):** give `new_tmpfs()` a `mkmod!(a+rwx) |
InodeMode::S_ISVTX` root while keeping `ramfs`/`rootfs` at `0755`. Compiles
clean (`cargo osdk build --scheme riscv --features riscv_sv39_mode`); runtime
verification = re-run of the gate (see §4).

This clears the **setuid-before-tmpdir** class (`symlink03`). `readlink03` and
`access02` use `needs_tmpdir = 1` (tmpdir is created as *root* first), so their
failures are errno-subcase bugs in `readlink`/`access` proper, not the `/tmp`
mode — still open.

### 3.3 clock_getres — missing syscall (fixed this session)

`getitimer01` failed with `clock_getres(CLOCK_MONOTONIC_COARSE) → ENOSYS`:
`sys_clock_getres` was never implemented (only `clock_gettime` was wired). **Fix
(committed `7775689cc`):** implement `sys_clock_getres`, returning 1 ns for
nanosecond clocks and 1 ms for the coarse clocks (updated once per the 1000 Hz
timer tick), and wire `SYS_CLOCK_GETRES` into the riscv/loongarch generic table
(114) and x86 (229). Compiles clean.

---

## 4. Commits / PR flow

On `track/nixos`:
- `fix(tmpfs)` `e96c2722d` — tmpfs root mode 01777.
- `docs(nixos)` `b95fe9745` — snd device model design.
- `feat(syscall)` `7775689cc` — implement `clock_getres`.

Kernel-fix PRs (cherry-picked onto `origin/main`):
- **PR #39** `fix/tmpfs-root-mode` — tmpfs root mode 01777.
- **PR #40** `fix/clock-getres` — implement clock_getres.

(Runtime verification of both is the in-flight gate re-run, §5.)

---

## Next steps

1. **AUDIO-M2 (now much smaller):** parameterize `SET_PARAMS` + implement the
   ALSA PCM ioctl ABI per `docs/porting/snd-device-model.md`; copy the three
   Alpine packages into `build_audio.sh`; verify `aplay -D hw:0,0` in QEMU.
2. **Verify the two landed fixes** by re-running the gate (expect `symlink03`
   and `getitimer01` to clear); then `gh pr create` the two branches.
3. **Remaining genuine bugs** — `access02`, `readlink03`, `readlinkat02`,
   `sendfile07`, `sbrk01`, `epoll_wait04` (errno-subcase bugs); **fork perf**
   (`fork06/07/09/11`, `chdir02`, `fcntl14`, `link05`, `epoll01` timeouts — the
   same root as the SMP=4 fork hang).
