# M7 Report: nix-daemon + multi-user store operations

> 2026-08-14. Follow-up to `M6-report.md`. Goal (issue #23): run **nix-daemon**
> in QEMU and complete a **multi-user** `nix build` through it.
>
> Conclusion up front: **both milestones are done and require no kernel change.**
> (1) A minimal repro proves the kernel already implements the two primitives
> nix-daemon depends on — `AF_UNIX` `SCM_RIGHTS` file-descriptor passing and
> `SO_PEERCRED` credential lookup — on RISC-V. (2) nix-daemon starts in the
> guest, a client connects over `NIX_REMOTE=daemon`, and three derivations are
> realised through the daemon with the builder running as a **non-root build
> user (`nixbld1`)** — the defining behaviour of multi-user mode. The only
> blocker found was a *userspace* configuration detail (nix 2.31 requires the
> build-users group to list its members in `/etc/group`), not a kernel gap.

## TL;DR

| Check | Status |
|---|---|
| `socketpair(AF_UNIX)` | ✅ |
| `SO_PEERCRED` returns peer (pid, uid, gid) | ✅ |
| `sendmsg(SCM_RIGHTS)` / `recvmsg` fd pass, fd usable on receiver | ✅ |
| nix-daemon starts and binds `/nix/var/nix/daemon-socket/socket` | ✅ |
| `nix build` (trivial) through the daemon | ✅ |
| builder runs as a non-root build user (`whoami_result=[nixbld1]`) | ✅ |
| `nix build` (hello) through the daemon, product runs | ✅ |

## Step 1 — minimal SCM_RIGHTS + SO_PEERCRED repro

The M-plan's known risk was that nix-daemon needs `AF_UNIX` + `SCM_RIGHTS` +
a socket credential model, and "the kernel may not implement SCM_RIGHTS". The
first deliverable is therefore a standalone repro (`scm_repro.c`) that forks
two processes over a `socketpair`, passes a file descriptor with
`sendmsg(SCM_RIGHTS)`, receives it with `recvmsg`, and reads a payload back
through the received fd to prove it is live. It also checks `SO_PEERCRED`.

Result — **all three checks pass on the first boot** (no kernel change):

```
=== M7 smoke results ===
  socketpair AF_UNIX:   OK
  SO_PEERCRED:          OK      # pid=1 uid=0 gid=0
  SCM_RIGHTS fd pass:   OK      # read_back=[scm-rights-ok]
```

The kernel had the whole mechanism already: `kernel/src/net/socket/unix/`
implements `SCM_RIGHTS` (`ctrl_msg.rs::FileMessage`), `SCM_CREDENTIALS`
(`CredMessage`), and `SO_PEERCRED` (`cred.rs` + the `PeerCred` socket option),
all architecture-independent. This code path is shared with x86_64 and works
unchanged on RISC-V. **Step 2 (patch the kernel for SCM_RIGHTS) is therefore a
no-op** — the `AuxiliaryData`/`ControlMessage` plumbing that carries fds and
credentials across the socket is present and correct.

## Step 3 — nix-daemon + multi-user build

`init_m7_daemon.c` drives the scenario: mount the pseudo filesystems, fork +
exec `/usr/sbin/nix-daemon` as root, poll for the daemon socket, then fork a
client that drops to **uid 1000 (`alice`)** with `setgid`/`setgroups`/`setuid`
and runs three builds over `NIX_REMOTE=daemon`. `build_m7_daemon.sh` layers the
multi-user pieces onto the M6 rootfs (build users `nixbld1`/`nixbld2`, client
user `alice`, `build-users-group = nixbld`).

Three derivations exercise the path end-to-end:

- `trivial.nix` — shell builder writes a fixed string to `$out` (store write
  path through the daemon);
- `whoami.nix` — builder writes `id -un` to `$out`, proving which uid actually
  ran the build;
- `hello.nix` — installs the prebuilt riscv64 hello (path B from M6) and runs it.

```
=== M7 daemon smoke results ===
  daemon socket ready:            OK
  trivial build via daemon:       OK    trivial_result=[hello-from-daemon]
  whoami build runs as nixbld:    OK    whoami_result=[nixbld1]
  hello build via daemon:         OK    hello_result=[Hello, world!]
```

`whoami_result=[nixbld1]` is the load-bearing result: a single-user (local)
build would have run the builder as `root` (or `alice`); seeing `nixbld1` proves
the daemon performed the build and dropped the builder's privileges to a member
of the `nixbld` group — multi-user mode working as intended.

### The one blocker found — and it is userspace, not kernel

The first daemon boot failed every build with:

```
error: the build users group 'nixbld' has no members
```

The initial rootfs had a *matching primary gid* in `/etc/passwd`
(`nixbld1:x:30001:30000:...`) but an **empty member list** in `/etc/group`
(`nixbld:x:30000:`). nix 2.31 resolves the build-users group's members from the
group's member list (`gr_mem`), so the empty list made it conclude there were
no members. The fix is the standard NixOS layout — list the members:

```
nixbld:x:30000:nixbld1,nixbld2
```

This is a configuration detail of how nix reads the group database (musl reads
`/etc/group` directly; no NSS modules are involved), not an Asterinas gap.

## Step 4 — daemon dependencies audit

The anticipated "heavy" daemon dependencies were checked off individually:

| Dependency | Status |
|---|---|
| `AF_UNIX` stream socket (bind/listen/accept) | ✅ present, verified by the repro + daemon bind |
| `SCM_RIGHTS` fd passing | ✅ `ctrl_msg.rs`, verified by the repro |
| `SO_PEERCRED` / peer credential lookup | ✅ `cred.rs`, verified by the repro |
| `setuid`/`setgid`/`setgroups`/`setresuid`/`setresgid` | ✅ present; exercised by the builder drop to `nixbld1` |
| `chown`/`fchown`/`fchownat` (store path ownership) | ✅ present; exercised by the daemon chowning outputs |
| `getgrnam`/`getpwnam` (group/user lookup) | ✅ userspace (musl reads `/etc/group`, `/etc/passwd`) |

No additional kernel gap was hit during the daemon run: the serial log shows
**zero** "Unimplemented syscall" lines, and only the inherited, harmless
warnings (`CLONE_DETACHED` / `CLONE_SYSVSEM` ignored, `personality(ADDR_NO_RANDOMIZE)`
accepted without disabling ASLR).

## Remaining gaps (all inherited from M3–M6, none block M7)

| Gap | Impact |
|---|---|
| seccomp BPF unimplemented (`SECCOMP_SET_MODE_FILTER` → EINVAL) | still bypassed with `filter-syscalls = false`; needed for real sandboxing |
| `ET_EXEC` + `PT_INTERP` ELF loader bug | blocks non-PIE dynamic binaries (gcc/cc1); hello path A still blocked |
| virtio-blk SMP race | `-smp 4` hangs ~2/3 of boots at the boot-sector read; `-smp 1` used for reliability |
| `landlock`(444)/`membarrier`(283)/`riscv_hwprobe`(258)/`rseq`(293) ENOSYS | startup probes; harmless (inherited) |
| `personality(ADDR_NO_RANDOMIZE)` accepted, ASLR not disabled | nix reproducibility request; ignored, harmless |

Note on the ACCELERATION.md `-smp 4` lever: it was deliberately **not** applied
to the daemon smoke because of the virtio-blk SMP race documented in M6 — the
daemon test is a serial daemon+client sequence that would only become flaky
under SMP, not faster. The other levers were adopted where they apply (the
daemon rootfs reuses the cached M6 tree, so the "build" is a cheap copy+pack
rather than a full re-download).

## Deliverables (`tools/riscv/nixos/m7/`)

| File | Purpose |
|---|---|
| `scm_repro.c` | static `/init`: socketpair + SO_PEERCRED + SCM_RIGHTS fd-pass repro |
| `build_m7.sh` | build the repro initramfs + repack the boot disk |
| `boot_m7_smoke.py` | QEMU driver asserting the three repro markers |
| `trivial.nix` / `whoami.nix` / `hello.nix` | derivations realised through the daemon |
| `init_m7_daemon.c` | static `/init`: start nix-daemon, drive the client build as uid 1000 |
| `build_m7_daemon.sh` | assemble the multi-user rootfs (build users + `nixbld` group + daemon init) |
| `boot_m7_daemon_smoke.py` | QEMU driver asserting the four daemon markers |

## Reproduction

```bash
# 1. Minimal SCM_RIGHTS + SO_PEERCRED repro.
tools/riscv/nixos/m7/build_m7.sh
python3 tools/riscv/nixos/m7/boot_m7_smoke.py

# 2. nix-daemon + multi-user build (reuses the M6 rootfs; cross-compiles hello).
tools/riscv/nixos/m7/build_m7_daemon.sh
python3 tools/riscv/nixos/m7/boot_m7_daemon_smoke.py
```

## Files changed

- `tools/riscv/nixos/m7/` — repro (`scm_repro.c`, `build_m7.sh`,
  `boot_m7_smoke.py`) and daemon deliverables (`trivial.nix`, `whoami.nix`,
  `hello.nix`, `init_m7_daemon.c`, `build_m7_daemon.sh`,
  `boot_m7_daemon_smoke.py`). No kernel source changes.
