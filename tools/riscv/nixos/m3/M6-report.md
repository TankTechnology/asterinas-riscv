# M6 Report: `nix build` realises a derivation into `/nix/store`

> 2026-08-13. Follow-up to `M5-report.md`. Goal: make `nix build` complete a real
> derivation on Asterinas RISC-V — the output must land in `/nix/store` and the
> product must run and print the expected value.
>
> Conclusion up front: **`nix build` now works end-to-end.** A trivial
> `builtins.derivation` (builder = `/bin/sh` writing to `$out`) is realised into
> `/nix/store` and reads back `hello-from-nix-store`, and a **hello derivation
> installs a real riscv64 binary into `/nix/store/<hash>-m6-hello/bin/hello`
> that prints `Hello, world!`.** Three kernel/configuration gaps were
> root-caused along the way: (1) nix's builder seccomp filter (bypassed with
> `filter-syscalls = false`), (2) the builder's empty `PATH`, and (3) a
> **kernel ELF-loader gap — non-PIE (`ET_EXEC`) dynamically-linked binaries do
> not execute** — which is why `gcc`/`cc1` cannot compile in the guest.

## TL;DR

| Check | Status |
|---|---|
| `nix build` trivial derivation → `/nix/store` | ✅ |
| trivial output reads back `hello-from-nix-store` | ✅ |
| `nix build` hello derivation → `/nix/store` | ✅ |
| `$out/bin/hello` prints `Hello, world!` | ✅ |
| hello compiled from source **in the guest** (path A, gcc) | ❌ blocked by `ET_EXEC` loader gap |

## Approach: minimal first, then hello

Following the plan, the work was split in two:

1. **Minimal validation** — a `builtins.derivation` whose builder is the system
   shell and whose only action is `echo -n hello-from-nix-store > $out`. No
   compiler, no nixpkgs, no sandbox. It exercises the full "realise a
   derivation" path: `.drv` instantiation, store-path hashing, forking the
   builder, and atomic rename of the output.
2. **hello** — a derivation whose output is a runnable riscv64 binary.

For hello, the plan offered two routes: **A** compile from source in the guest
(needs the Alpine riscv64 `gcc`/`binutils`/`make` in the rootfs) or **B**
cross-compile on the host and bundle the result. Route A is the recommended
self-contained one, so it was tried first — and hit a kernel gap (below). The
working milestone therefore uses route B: `build_m6.sh` cross-compiles `hello.c`
on the host with `riscv64-linux-musl-gcc` (producing a PIE musl binary) and the
derivation's builder copies it into `$out/bin/hello`.

## Deliverables (`tools/riscv/nixos/m6/`)

| File | Purpose |
|---|---|
| `trivial.nix` | minimal `builtins.derivation` (shell builder writes to `$out`) |
| `hello.nix` | hello derivation (path B: installs `hello-prebuilt`) |
| `hello-gcc.nix` | hello derivation (path A: `gcc` in the guest; blocked, kept for reference) |
| `hello.c` | the source |
| `build_m6.sh` | cross-compiles `hello-prebuilt`, assembles the initramfs, re-packs the boot disk |
| `init_m6.c` | `/init`: mounts, prepares `/nix`, runs the two builds behind markers |
| `boot_m6_smoke.py` | QEMU driver asserting the four check markers |

## Root cause analysis

### 1. nix's builder seccomp filter (blocker #1)

The first attempt failed with:

```
error: … while setting up the build environment
error: unable to load seccomp BPF program: Invalid argument
```

nix installs a seccomp filter on the builder by default — controlled by the
`filter-syscalls` config option (default `true`), independent of `sandbox`.
Asterinas does not implement seccomp BPF, so `seccomp(SECCOMP_SET_MODE_FILTER)`
fails with `EINVAL`. This is exactly the seccomp bypass the M-plan anticipated
("沙箱 (bubblewrap/seccomp) 评估 — 可先绕过"). Fix: `filter-syscalls = false` in
`/etc/nix/nix.conf` (added to `build_m3.sh` and `build_m6.sh`). The `.drv` was
already being instantiated correctly before this (`/nix/store/…-m6-trivial.drv`
appeared in the log), so store-path hashing and `.drv` writes worked from the
start.

### 2. The builder runs with an empty `PATH` (blocker #2)

After disabling seccomp, the hello build failed with:

```
builder failed with exit code 127
> sh: line 0: mkdir: not found
```

The trivial derivation had worked only because `echo` is a shell builtin. nix
sets an empty (or minimal) `PATH` for the builder, so `mkdir`/`gcc`/`as`/`ld`
are not found. Any non-reserved derivation attribute becomes an environment
variable for the builder, so `PATH = "/usr/bin:/bin:…"` in the derivation fixes
it (and `gcc` needs `as`/`ld` from `PATH` internally).

### 3. Non-PIE (`ET_EXEC`) dynamic binaries do not run (blocker #3 — kernel gap)

Route A (compile in the guest) failed even though the toolchain extracted
cleanly: `gcc` exited 0 but produced **no output at all**:

```
gcc --version           -> exit 0, empty output
gcc --bogus-flag        -> exit 0, no error message (should be exit 1)
gcc hello.c -o hello    -> exit 0, no output file produced
```

The isolation test showed the filesystem is fine (`echo hi > /m6/sub/f` works)
but `gcc` itself is broken. Comparing ELF types across the rootfs pinpoints the
cause:

| binary | ELF type | runs on Asterinas? |
|---|---|---|
| `nix`, `busybox`, `as`, `ld`, `make` | `DYN` (PIE) | ✅ |
| `/init` (compiled `-static -no-pie`) | `EXEC`, no interp | ✅ |
| **`gcc`, `cc1`** | **`EXEC` + `PT_INTERP`** | ❌ exits 0, no output |

`gcc` and `cc1` are the **only** non-PIE (`ET_EXEC`) dynamically-linked binaries
in the Alpine toolchain (Alpine builds gcc non-PIE); every binary that works is
PIE (`ET_DYN`) or static (`ET_EXEC` without an interpreter). Under qemu-user on
the host, the same `gcc` prints `gcc (Alpine 15.2.0) 15.2.0` correctly, so the
binary is valid — the defect is Asterinas's ELF loader failing on the
`ET_EXEC` + `PT_INTERP` combination (fixed-address mapping plus a dynamic
linker). The loader in `kernel/src/process/program_loader/elf/load_elf.rs` maps
`ET_EXEC` "as-is" and sets the entry point to the interpreter's entry, but the
process nonetheless exits cleanly without ever reaching `main`. This is a
kernel-side bug for the sibling `asterinas-riscv` tree; it blocks any non-PIE
dynamically-linked binary (gcc, and thus in-guest compilation) and is the one
real blocker preventing path A.

## Verification

```
=== M6 smoke results ===
  nix build trivial -> /nix/store: OK
  nix build trivial exited: OK
  nix build hello -> /nix/store: OK
  nix build hello exited: OK
```

The serial markers confirm the exact outputs:

```
out_path=[/nix/store/wsbwscgipdvr51bmqkd4w5s66kjkkfxs-m6-trivial]
trivial_result=[hello-from-nix-store]
hello_out_path=[/nix/store/…-m6-hello]
hello_result=[Hello, world!]
```

`/nix/store` after the run contains both `.drv` files and both outputs
(`…-m6-trivial`, `…-m6-hello`). The trivial output is a file; the hello output
is a directory with a runnable `bin/hello`.

## Remaining gaps

| Gap | Impact |
|---|---|
| seccomp BPF unimplemented (`SECCOMP_SET_MODE_FILTER` → EINVAL) | bypassed with `filter-syscalls = false`; needed for real sandboxing |
| `ET_EXEC` + `PT_INTERP` ELF loader bug | blocks non-PIE dynamic binaries (gcc/cc1); blocks hello path A; kernel-side fix |
| virtio-blk SMP race | `-smp 4` hangs ~2/3 of boots at the boot-sector read during `aster-block` init; `-smp 1` is reliable |
| `landlock` (syscall 444) ENOSYS | nix probes it and falls back; harmless |
| `personality(ADDR_NO_RANDOMIZE)` accepted, ASLR not disabled | nix asks to disable ASLR for reproducibility; ignored, harmless here |
| `membarrier`(283)/`riscv_hwprobe`(258)/`rseq`(293) ENOSYS | startup probes; harmless (inherited from M3-M5) |

No gap blocks the M6 goal itself: `nix build` realises both derivations and the
products run correctly.

## Reproduction

```bash
# Build the M6 initramfs (cross-compiles hello-prebuilt on the host) and
# re-pack the boot disk.
tools/riscv/nixos/m6/build_m6.sh

# QEMU smoke (boots, runs both nix builds, asserts the four markers).
python3 tools/riscv/nixos/m6/boot_m6_smoke.py

# Optional: stage the Alpine gcc/binutils/make toolchain for hello path A
# (blocked by the ET_EXEC loader gap — see hello-gcc.nix).
tools/riscv/nixos/m6/build_m6.sh --with-gcc
```

## Files changed

- `tools/riscv/nixos/m6/` — `trivial.nix`, `hello.nix`, `hello-gcc.nix`,
  `hello.c`, `build_m6.sh`, `init_m6.c`, `boot_m6_smoke.py`.
- `tools/riscv/nixos/m3/build_m3.sh` — add `filter-syscalls = false` to the
  canonical `nix.conf`.
