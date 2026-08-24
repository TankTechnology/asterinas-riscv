---
date: 2026-08-24
mode: diff
base: 4a138faa5
head: 54533d160
branch: codex/debian-rootfs-m1
title: "Debian RISC-V rootfs M1"
---

# Summary

This branch adds a signed Debian Trixie `riscv64` ext2 builder, immutable
artifact contract, minimal stage-1 root handoff, and a bounded two-boot QEMU
persistence gate. No Critical, Important, or Minor defect remains open in the
reviewed M1 scope.

The review consolidated the focused contract, security, lifecycle, and
publication reviews performed at each implementation milestone. The skill
resolved the complete 28-commit series (about 405 KiB of diff input) in
combined-persona mode; at the user's request, no additional five-persona
fan-out or external full-diff scan was launched. The final bounded pass instead
rechecked the high-risk boundaries against current code and current-run
evidence: signed Debian provenance, full package-lock binding, no-follow input
descriptors, immutable-base versus writable-root separation, four-hart DTB,
process-group teardown, signal deferral, transcript caps and fatal markers,
nonce redaction, and result-last evidence publication.

Confirmed findings discovered during implementation and real-gate execution
were closed with focused RED-to-GREEN regressions. The principal fixes were:

- full package provenance and descriptor-pinned cache/publication writes
  (`c84396425`, `0c9bb6f10`, `f6330ff24`);
- the stage-1 discovery deadline, console `FD_CLOEXEC`, and transactional
  init/archive publication (`08463b430`, `28f3491f5`, `63c6416de`);
- passing the pinned output-directory descriptor to the sparse root copy
  (`1a24decf7`);
- keeping QEMU session hardlinks on the output filesystem while checking its
  inode identity (`599a3628d`);
- normalizing manifest package order and real `\r\r\n` serial transport
  (`f948e5a76`, `d50b17aef`).

Final verification at `54533d160` passed 112/112 rootfs tests in 11.919
seconds, Python compilation, both shell syntax checks, Ruff lint/format, and
`git diff --check`. The preserved real run used QEMU 10.2.1 with Sv39, four
harts, 2 GiB, two VirtIO block disks, and `-nic none`. Both boots passed in
14.042 and 13.940 seconds. The immutable base remained
`060f613281f2e77fa2232f31322213a310f48b5b18df2991ade9eb2fca7bebae`;
the persisted writable root became
`f6300db673c17c038a8bdbca76f092e891d0874b6d74737e3bb766bbe7492262`.
Both serial logs are nonempty, contain shell-ready and zero-status command
evidence, and redact nonce plaintext. No target QEMU process or named container
remained.

Remaining limitations are explicit scope boundaries, not review findings:
the result proves generic QEMU `/bin/bash` handoff and two-boot ext2
persistence only. It does not prove systemd, guest networking, apt at runtime,
display, USB/xHCI input, desktop use, or physical Megrez behavior. The signed
stable-mirror build is auditable but is not claimed byte-reproducible across
future mirror updates, and multi-file evidence publication is fail-closed
with `result.json` last rather than power-loss atomic as a set.
