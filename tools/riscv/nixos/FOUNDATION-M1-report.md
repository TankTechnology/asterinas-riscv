# FOUNDATION-M1 report — shared kernel gaps

Milestone **FOUNDATION-M1** closes the shared (non-NixOS-track) kernel gaps that
the foundation track owns, in priority order. Each item is: reproduce → fix →
QEMU-verify → incremental commit, with the kernel-side changes intended to flow
back to `main` as a PR.

Status summary:

| # | Item | Status |
|---|------|--------|
| 1 | System V shm (`shmget`/`shmat`/`shmdt`/`shmctl`) | **Done** — implemented, verified, committed |
| 2 | `ET_EXEC` + `PT_INTERP` ELF loader bug (M6/M8) | **Already fixed** — verified, no code change needed |
| 3 | `mlock`/`munlock` (issue #11) | Not started (time) |

---

## 1. System V shared memory — `shmget`/`shmat`/`shmdt`/`shmctl`

Commit: `36915af88` (`feat(ipc): System V shared memory …`).

### What changed

System V shm is the 3-syscall shared-memory surface needed by Xorg's MIT-SHM
acceleration (issue #10 / #18). Asterinas already had the System V *semaphore*
family (`semget`/`semop`/`semctl`) but no shm. The new code mirrors the semaphore
structure:

- `kernel/src/ipc/shared_memory/{mod,shm,shm_set}.rs` — `ShmSet` (segment object
  holding an anonymous `Vmo`, permission, times, creator/last PIDs, attach
  count), the `shm_attach`/`shm_detach` helpers, and `ShmidDs` (Linux
  `shmid64_ds`, both the x86_64 and generic 64-bit layouts).
- `kernel/src/ipc/ipc_ns.rs` — per-namespace `shm_ids` table plus an attachment
  registry keyed by `(pid, address)` so `shmdt` can resolve `shmid` from the
  unmapped address.
- `kernel/src/ipc/mod.rs` — hoisted the shared `IpcPerm` (`ipc64_perm`) struct
  out of the semaphore module so both IPC families reuse it.
- `shmget`/`shmat`/`shmdt`/`shmctl` wired into both the x86_64 and generic
  (riscv64/loongarch64) syscall tables. riscv64 numbers 194/195/196/197,
  x86_64 numbers 29/30/31/67.

Semantics follow Linux `ipc/shm.c`: `IPC_PRIVATE` always allocates a fresh
segment; a keyed get with an oversized size returns `EINVAL`; `shmat` honours
`SHM_RDONLY`/`SHM_RND`/`SHM_REMAP`/`SHM_EXEC`; `shmctl` implements `IPC_STAT`
and `IPC_RMID`. Permission checks are stubbed with the same `TODO` as the
semaphore code.

### Verification

A two-process smoke test (parent attaches, forks, child attaches the same
`shmid` and writes through its own mapping, parent observes the write) passes on
riscv64 QEMU, along with `shm_segsz`/`shm_nattch` accounting and `IPC_RMID`:

```
OK: shmget(IPC_PRIVATE)
OK: shmctl(IPC_STAT)
OK: shm_segsz == 4096
OK: shm_nattch == 0 (before attach)
OK: shmat(NULL)
OK: fork()
OK: child shmat + write + shmdt
OK: parent observes child write (shared)
OK: shm_nattch == 1 (after child detach)
OK: shmdt(parent)
OK: shmctl(IPC_RMID)
__SYSV_SHM_OK__
```

Harness: `tools/riscv/nixos/shm/` (`build_shm.sh` + `boot_shm_smoke.py`),
committed as `df3db08b3`. Regression test:
`test/initramfs/src/regression/ipc/shm/sysv_shm.c` (added to `run_test.sh`).

---

## 2. `ET_EXEC` + `PT_INTERP` ELF loader bug (M6/M8)

**Conclusion: already fixed in the current tree — no code change required.**

M6/M8 diagnosed that non-PIE dynamically-linked binaries (`gcc`/`cc1`, the only
`ET_EXEC`+`PT_INTERP` binaries in the Alpine toolchain) "exit 0 with no output",
hypothesizing an ELF-loader defect. Reproducing against the *actual* Alpine
binaries today contradicts that diagnosis — the binaries run correctly.

### Reproduction matrix (all on riscv64 QEMU, current kernel)

| binary | ELF type | result |
|---|---|---|
| glibc `hello` (`-no-pie`) | `EXEC` + `PT_INTERP` | ✅ reaches main |
| musl `hello` (`-no-pie`) | `EXEC` + `PT_INTERP` | ✅ reaches main |
| musl `hello` with 32 MB `.bss` | `EXEC` + `PT_INTERP` | ✅ reaches main |
| Alpine `gcc` driver (2.6 MB, `--version`) | `EXEC` + `PT_INTERP` | ✅ prints `gcc (Alpine 15.2.0) 15.2.0` |
| Alpine `cc1` (55 MB, `-version`) | `EXEC` + `PT_INTERP` | ✅ prints `GNU C23 (Alpine 15.2.0) version 15.2.0` |

The Alpine `gcc` and `cc1` above are the exact binaries M6 cited as broken
(fetched from the Alpine edge `riscv64` repo, byte-for-byte the toolchain in
`build_m6.sh --with-gcc`). Both `gcc --version` and the 55 MB `cc1 -version`
reach `main` and print, so the loader correctly maps the fixed-address
`ET_EXEC` segments *and* runs the `PT_INTERP` dynamic linker — the exact
combination the reports said was broken. Direct-exec and fork+exec both work.

### Caveat — full in-guest compilation still blocked by a *different* gap

The full `gcc hello.c -o hello` (driver → cc1 → as → ld) does **not** yet work,
but the failure is **not** the ELF loader. In a large full-toolchain initramfs
(~79 MB unpacked), `execv("/usr/bin/gcc", …)` fails with `ENOEXEC`, whereas the
*same* `gcc` + *same* musl interpreter exec fine from a minimal initramfs:

| initramfs | `execv(gcc …)` |
|---|---|
| minimal (`gcc` + musl loader + libc only) | ✅ works |
| full toolchain (~79 MB, thousands of files) | ❌ `ENOEXEC` |

Since the `gcc` binary and interpreter are byte-identical across both, and only
the rootfs population differs, this points to an initramfs-unpacking / VFS issue
for large/many-file initramfs, not the `ET_EXEC` loader. That is a separate bug
and is out of scope for the `ET_EXEC`+`PT_INTERP` loader item.

### Reproduce

`tools/riscv/nixos/etexec/` (`build_etexec.sh` + `boot_etexec_smoke.py`) builds
the minimal repro and the full-toolchain repro (fetches the Alpine `gcc`/`musl`
APKs and extracts `cc1`/`gcc`).

---

## 3. `mlock`/`munlock` (issue #11)

Not started — lower priority than the two items above. Left for a follow-up.
