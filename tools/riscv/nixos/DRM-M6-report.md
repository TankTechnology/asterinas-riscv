# DRM-M6 — keyctl fix upstream rollup + unified acceptance protocol

Date: 2026-08-15
Branch: `track/drm`
Status: **DONE** — the integration-exposed kernel fix (`keyctl
SETPERM`/`LINK`/`UNLINK`) has been extracted into its own PR against `main`
(**PR #43**), and the M5 full-system integration harness is now documented as a
unified, reproducible acceptance protocol (`tools/riscv/drm/ACCEPTANCE.md`). A
pass over the remaining integration warnings turned up two non-blocking
follow-ups, recorded in §3.

---

## 1. Upstream rollup — keyctl fix → PR #43

DRM-M5's one real kernel change was commit `905491522`, which makes
`KEYCTL_SETPERM`/`LINK`/`UNLINK` no-op successes so that systemd 257 can spawn
desktop services (see `tools/riscv/nixos/DRM-M5-report.md` §5). That fix was
made directly on the integration branch `track/drm`, interleaved with two merge
commits, so it was not independently reviewable.

Following the DRM-M4 rollup pattern (PR #41), the fix was extracted onto a clean
single-commit branch off `origin/main`:

```
git checkout -b fix/keyctl-setperm-link origin/main
git cherry-pick 905491522
```

The cherry-pick was **conflict-free and byte-identical** to the original: the
pre-fix state of `kernel/src/syscall/keyctl.rs` on `track/drm` is exactly
`origin/main`'s version, so the replayed diff (9 insertions, 1 file) matches the
original commit's diff exactly (verified with `diff`; only the commit hash
differs because the parent changed).

- Branch: `fix/keyctl-setperm-link`
- PR: **https://github.com/TankTechnology/asterinas-riscv/pull/43**
- Base: `main`, head: `fix/keyctl-setperm-link`, one commit.

The PR body reproduces the symptom → root-cause → fix → verification chain from
the M5 report so reviewers don't need the integration context.

## 2. Unified acceptance protocol

`tools/riscv/drm/ACCEPTANCE.md` now documents, in one place, how to reproduce the
M5 acceptance — **one QEMU boot that proves DRM/KMS, ALSA, and NetSurf coexist**:

- **Milestone map** (M1–M5) linking each harness to its report, so the DRM
  workstream's acceptance criteria are traceable end-to-end.
- **Prerequisites** — the three source trees the integration guest is assembled
  from (the sibling `asterinas-riscv` desktop rootfs + `riscv-cross` driver, the
  sibling `asterinas-riscv-nixos` Alpine ALSA cache, and the `/tmp/drm-m4` U-Boot
  + DTB seed), plus the toolchain and kernel-build prerequisites.
- **Build** — the kernel build (`OSDK_TARGET_ARCH=riscv64 cargo osdk build
  --scheme riscv --features riscv_sv39_mode` + the local `cargo-osdk` symlink
  trick) and `build_m5.sh` (initramfs assembly + `/tmp/drm-m5/boot.ext4` re-pack).
- **Run** — `boot_m5.py` with its flags (`--net`, `--settle-seconds`, `--smp`, …).
- **Acceptance criteria** — the three pass conditions: (1) `graphical.target` +
  Xorg `modesetting` on `/dev/dri/card0`, (2) `__ALSA_PASS__` **and** the
  host-decoded WAV (RMS ≥ 2000, pitch 440 ± 12 Hz), (3) a screendump histogram
  containing the NetSurf home page's cream `#f4e8d0` + blue `#1a4f8b` colours.
- **Known failure modes** — the seven gotchas that have actually bitten this
  workstream (keyctl `237/KEYRING`, the missing journal socket vs
  `StandardOutput=file:/dev/ttyS0`, the 120 s NetSurf settle, raw-cpio vs gzip,
  the `0x9000_0000` DTB relocation, the plane-resources fallback, the
  missing-framebuffer warning), each with its cause and fix.

The doc is written so a fresh checkout (with the sibling trees in place) can go
from zero to `=== DRM-M5: PASS ===` in two commands after the kernel build.

## 3. Additional integration-exposed issues (follow-ups)

A pass over the M5 serial transcript and the DRM ioctl surface found two
non-blocking items worth recording. Neither affects the M5 acceptance.

### 3.1 `DRM_IOCTL_MODE_GETPLANERESOURCES` (0xb5) unimplemented, but plane caps advertised

Xorg's modesetting driver logs `failed to get plane resources: Inappropriate
ioctl for device` and falls back to ShadowFB (the path the harness forces
anyway, so this is invisible to the acceptance result). The cause is that
`kernel/src/device/dri.rs` implements the KMS ioctl surface through
`MODE_OBJ_GETPROPERTIES` (0xb9) but **not** `MODE_GETPLANERESOURCES` (0xb5), so
the call returns `ENOTTY`.

The slight inconsistency: `SET_CLIENT_CAP` *accepts* `DRM_CLIENT_CAP_UNIVERSAL_PLANES`
and `DRM_CLIENT_CAP_ATOMIC` (returns success, with the comment "the
corresponding features are simply absent"). Because the cap is acknowledged, the
modesetting driver proceeds to enumerate planes and then hits the `ENOTTY`.
Harmless today, but the cap acknowledgement is mildly dishonest — either
implement `MODE_GETPLANERESOURCES` (and `MODE_GETPLANE`/`MODE_SETPLANE` for a
real universal-planes surface) or stop acknowledging those caps. Follow-up, not
blocking.

### 3.2 Six riscv64 syscalls unimplemented

The boot logs `Unimplemented syscall number: 170/258/264/280/285/293`. Cross-
referenced against the riscv64 (`asm-generic`) syscall table and the kernel's own
`kernel/src/syscall/arch/generic.rs` dispatch table:

| num | syscall | why the guest calls it |
|---|---|---|
| 170 | `settimeofday` | time-of-day setup |
| 264 | `name_to_handle_at` | systemd path/handle operations |
| 280 | `bpf` | systemd 257 cgroup filtering |
| 285 | `copy_file_range` | file copy fast path |
| 293 | `rseq` | glibc restartable sequences (per-thread fast path) |
| 258 | *(not a standard asm-generic number — reserved gap)* | — |

All return `ENOSYS` and the guest degrades gracefully (glibc falls back from
`rseq` to TLS, systemd falls back from BPF cgroup to cgroupfs). These are
**pre-existing** — not introduced by the DRM/ALSA merge — and match the
"non-fatal gaps" already listed in DRM-M5 §6.

## 4. Result

| deliverable | status |
|---|---|
| keyctl fix upstreamed | PR **#43** (`fix/keyctl-setperm-link` → `main`), byte-identical cherry-pick |
| unified acceptance protocol | `tools/riscv/drm/ACCEPTANCE.md` |
| additional issues | §3 (two non-blocking follow-ups; no new kernel work required for M6) |

Next steps: merge PR #43 into `main`, then (optionally) implement
`MODE_GETPLANERESOURCES`/universal-planes or the `rseq`/`bpf` syscalls as
independent follow-ups. Neither is on the DRM critical path.
