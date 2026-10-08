# SYSTEMD M2 — Clean minimal cross-build: drop the M1 linker workaround

Date: 2026-08-14
Status: **Clean cross-build SUCCEEDS** (878/878 ninja targets, 0 failures) with
**no** `-Wl,--allow-multiple-definition`. The only M1 workaround (the util-linux
`parse_size`/`parse_range` collision) is now fixed at the source archive level,
so the build is semantically clean. Output is still **dynamically-linked PIE** —
that is now the *only* remaining blocker, and it is hardwired into systemd.

## Objective

M1 proved systemd *builds* for riscv64 but only with the risky
`-Wl,--allow-multiple-definition` flag ("first definition wins" across two
non-identical `parse_size` implementations). M2 removes that workaround and
re-verifies a clean compile of the pid1 + journald trim. Runtime on QEMU is
still out of scope (kernel-side security triad is not ready); the goal is a
*semantically clean* proof that riscv64 glibc systemd compiles.

## 1. The clean fix (replaces `--allow-multiple-definition`)

M1's one linker blocker: `src/basic/parse-util.c` (systemd) and
`lib/strutils.c` (util-linux) both define GLOBAL `parse_size` / `parse_range`.
The signatures differ (systemd: `(const char*, uint64_t, uint64_t*)` and
`(const char*, unsigned*, unsigned*)`; util-linux: `(const char*, uintmax_t*, int*)`
and `(const char*, int*, int*, int)`), so "first definition wins" is genuinely
unsafe.

Because util-linux is built `--disable-shared`, `libcommon`'s `strutils.o` is
archived into `libmount.a` / `libblkid.a` / `libsmartcols.a` with those two
symbols still `T` (global), and systemd whole-archives its own `libbasic.a` —
collision.

**Fix** (`build_util_linux_libs.sh`, post-`make install` step): demote the two
symbols to LOCAL in the archives systemd actually links:

```sh
for lib in mount blkid smartcols; do
  "$HOST-objcopy" --localize-symbol=parse_size --localize-symbol=parse_range \
    "$PREFIX/lib/lib$lib.a" "$PREFIX/lib/lib$lib.a.tmp" && mv ...
done
```

This is safe because **neither libmount nor libblkid nor libsmartcols calls
`parse_size`/`parse_range` across objects** (verified by grep over their `src/`);
the symbols are passengers carried by `strutils.o`. `libfdisk.a` is deliberately
excluded: its `script.o`/`gpt.o` *do* call `parse_size` across objects
(`U parse_size`), so localizing there would break that archive — and systemd
never links libfdisk.

Verified: `riscv64-linux-gnu-nm` now shows lowercase `t parse_size`/`t parse_range`
(local) in all three archives.

## 2. Build result

`build_systemd_minimal.sh` regenerates a clean cross file (no
`--allow-multiple-definition`) and builds into a fresh `build-riscv/`:

- **meson setup: SUCCEEDS** (all hard deps resolve via the `--static` pkg-config
  wrapper, as in M1).
- **ninja: 878/878, 0 failures** — the link of `libsystemd-shared-257.so` (where
  the collision used to fire) now links cleanly with no flags.
- Log: `/tmp/systemd_m2_build.log` (ends `[878/878] … systemd minimal cross-build
  SUCCEEDED`).

Produced binaries: 67 top-level executables (`systemd`, `systemd-journald`,
`systemctl`, `journalctl`, `systemd-nspawn`, `udevadm`, all generators, udev
helpers, …) plus `libsystemd-core-257.so` / `libsystemd-shared-257.so` /
`libsystemd.so.0.40.0` / `libudev.so.1.7.10`.

```text
$ file build-riscv/systemd
ELF 64-bit LSB pie executable, UCB RISC-V, RVC, double-float ABI,
dynamically linked, interpreter /lib/ld-linux-riscv64-lp64d.so.1

$ readelf -d build-riscv/systemd | grep NEEDED
  NEEDED  libsystemd-core-257.so
  NEEDED  libsystemd-shared-257.so
  NEEDED  libc.so.6
```

## 3. Remaining dependencies

**None new.** Every hard dependency is now present and cleanly linked into the
static archives:

| dependency | provider | status |
|---|---|---|
| libcap / libpsx | libcap 2.75 | present |
| libmount / libblkid / libuuid | util-linux 2.40.4 (localized) | present |
| libcrypt | libxcrypt 4.4.38 | present |
| libz | glibc sysroot | present |

No additional source tarballs, headers, or `.pc` files are needed for the
compile-proof milestone.

## 4. Remaining blockers (in order)

1. **Dynamic linking is hardwired into systemd.** `libsystemd-core-257.so` and
   `libsystemd-shared-257.so` are declared `shared_library()` unconditionally in
   `src/core/meson.build:129` and `src/shared/meson.build:352` — there is **no
   meson option** to build them static. The `systemd`/`journald` binaries NEED
   these two `.so` files, so a fully-static pid1 is not reachable by a flag:
   under `-static` the linker ignores the `.so` and the internal symbols become
   undefined (`undefined reference to 'access_fd'` in a probe). `-Dstatic-libsystemd=true`
   only adds the *public* `libsystemd.a`, not a static core/shared. A static pid1
   would require patching those two `shared_library()` calls to `static_library()`
   and full `-static` glibc linking — which upstream does not support (pid1 uses
   dlopen for NSS/PAM/etc.), and which reintroduces the static-glibc NSS/dlopen
   caveats this project already hit elsewhere.

2. **Runtime syscall surface is unproven.** Compiling is not running: pid1 needs
   mount namespaces, cgroups, `prctl`/`capset`/`setns`/`mknod`, and the glib
   programs this project has already run hit rseq/signal gaps on the kernel.
   Out of M2 scope (kernel-side security triad not yet landed).

## 5. Trim status

The meson feature flags (`-Dnetworkd=false -Dhomed=false -Dlogind=false …`,
~70 `-D…=false` in the script) cut whole subsystems. The remaining 67 binaries
are the core that systemd's build graph emits unconditionally: pid1, journald,
the management CLI (`systemctl`/`journalctl`), and the generators + udev helpers.
A further *target-level* trim (`ninja systemd systemd-journald systemctl
journalctl`) is possible but unnecessary for the compile-proof goal and would
break the `878/878` whole-graph check that gives confidence the tree compiles.

## 6. Conclusion

M2 closes the last build-time blemish from M1. **riscv64 glibc systemd compiles
cleanly** — full tree, zero linker flags, zero new dependencies. The single
remaining obstacle to *using* it is dynamic linking, which is a systemd
architectural choice (shared internal libs) rather than a cross-compile gap.
Revisit only once a working dynamic linker (or a deliberate static-link patching
effort) lands on Asterinas riscv64.

## Reproduce

```sh
cd tools/riscv/systemd
./build_libcap.sh               # libcap/libpsx (idempotent)
./build_util_linux_libs.sh      # libmount/libblkid/libuuid + localize fix
./build_libxcrypt.sh            # libcrypt (idempotent)
./build_systemd_minimal.sh      # clean build, 878/878, no --allow-multiple-definition
```
