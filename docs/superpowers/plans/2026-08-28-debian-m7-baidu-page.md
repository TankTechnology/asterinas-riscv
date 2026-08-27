# Debian M7 Baidu Page Evidence Implementation Plan

> Execute this plan in the current `codex/debian-network-main` worktree. Use
> focused TDD, one concise self-review, and no physical-board reset.

**Goal:** Prove basic real-page browsing and search in NetSurf on the Asterinas
Debian RISC-V desktop while preserving the M6 baseline.

**Architecture:** Move only run-private QEMU state to a short `/tmp` directory;
keep durable artifacts descriptor-pinned in the requested output directory.
Extend the existing guest browser evidence sequence and add an M7 host adapter
that captures homepage and search-result frames.

**Tech stack:** Python 3, Bash/systemd, xdotool, NetSurf GTK, QEMU HMP, Debian
Trixie riscv64, Asterinas generic-Sv39 SMP=4.

## Task 1: Bound QEMU runtime paths

**Files:**

- Modify: `tools/riscv/debian/rootfs/rootfs_gate_backend.py`
- Modify: `tools/riscv/tests/test_debian_rootfs.py`

- [ ] Add a regression using a repository-style output path long enough to
  make the old monitor socket exceed the AF_UNIX limit.
- [ ] Assert the session directory is private, under `/tmp`, and independent
  of the output path while boot/root hard links still reference the pinned
  output files.
- [ ] Assert drain and launch failures remove the runtime directory.
- [ ] Run the focused backend session tests and record RED.
- [ ] Implement the minimal short-runtime-directory change.
- [ ] Run focused GREEN plus Python/Ruff/diff checks.
- [ ] Commit as `fix(riscv): bound Debian QEMU monitor paths`.

## Task 2: Define M7 guest and classifier contracts

**Files:**

- Modify: `tools/riscv/debian/rootfs/desktop_m6_browser_evidence.sh`
- Create: `tools/riscv/debian/rootfs/desktop_m7_baidu_gate.py`
- Modify: `tools/riscv/tests/test_debian_rootfs.py`
- Modify: `Makefile`

- [ ] Add classifier RED cases for missing, duplicate, reordered, and
  failure-marked homepage/search evidence.
- [ ] Add guest-script RED cases for exact homepage navigation, bounded title
  waits, frozen search interaction, and stable failures.
- [ ] Preserve every M6 marker and add exact M7 homepage, search, and ready
  markers.
- [ ] Add an M7 operation that waits for each capture point, captures two
  bounded PPMs, and publishes their metadata and bytes.
- [ ] Add a focused Makefile unit target and run GREEN/static checks.
- [ ] Commit as `test(riscv): define Debian Baidu page gate`.

## Task 3: Rebuild the signed desktop rootfs

**Files:**

- Modify generated ignored artifacts only under
  `target/debian-riscv/desktop-m7-baidu/`.

- [ ] Reuse the proven signed Debian/TUNA/cache workflow; do not change the
  mirror, package lock policy, or kernel.
- [ ] Build one independent `desktop-m5-network` root image containing the M7
  evidence script.
- [ ] Run the public rootfs `contract verify` command.
- [ ] Record image, manifest, lock, InRelease, checksum, stage1, DTB, U-Boot,
  and kernel SHA-256 identities.

## Task 4: Run M6 regression and M7 real-page gate

- [ ] Run M6 once at the documented long output path and require pass.
- [ ] Run M7 once with the same generic-Sv39 SMP=4 QEMU contract.
- [ ] Require DNS/HTTPS, M4 desktop, M6 remote/JavaScript, M7 homepage/search,
  and final ready evidence in the fully drained transcript.
- [ ] Inspect both M7 screenshots visually; record what is visible without
  claiming unsupported modern-JavaScript behavior.
- [ ] If M7 fails, classify it using the design failure boundaries and add only
  a focused reproducer before changing kernel code.
- [ ] Clean named containers and temporary runtime directories.

## Task 5: Document the operator result

**Files:**

- Modify: `tools/riscv/debian/rootfs/README.md`
- Modify: `docs/superpowers/plans/2026-08-28-megrez-debug-workflow-m3-m4.md`

- [ ] Document the M7 command, artifacts, success boundary, and known NetSurf
  limitations.
- [ ] Mark the preserved M6 and actual-homepage checklist items accurately.
- [ ] Run focused host tests, shell syntax checks, Python/Ruff checks, and
  `git diff --check` once.
- [ ] Commit the documentation and report the exact QEMU evidence.
