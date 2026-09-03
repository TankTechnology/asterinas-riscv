# RISC-V desktop PR triage — 2026-09-03

This review classifies the six open pull requests in
`TankTechnology/asterinas-riscv` against the current schema-seven Debian
`browser-web` desktop line.  It does not use remote CI status as merge evidence;
the selected commits are rebuilt and tested locally.

## Integrate now

| PR | Decision | Desktop-line reason |
|---|---|---|
| [#99](https://github.com/TankTechnology/asterinas-riscv/pull/99) | Integrate exact commit `73514169d424` | Linux permits the `F_DUPFD`/`F_DUPFD_CLOEXEC` lower bound to equal the occupied source descriptor. Mesa GBM uses this form during EGL setup, so the existing `EINVAL` check can block the graphical stack. |
| [#100](https://github.com/TankTechnology/asterinas-riscv/pull/100) | Integrate exact commit `cde206963973` after hardware review | Replaces an invalid all-ones SBI RFENCE mask with masks derived from initialized hardware hart IDs and adds writer-side ordering. This is relevant to Firefox JIT/executable mappings and was exposed by the DRM desktop workload. |
| [#102](https://github.com/TankTechnology/asterinas-riscv/pull/102) | Integrate exact commit `6055d69fb682` | Keeps the RISC-V-only `riscv_flush_icache` module out of non-RISC-V builds. This is a narrow compilation fix around the same syscall line. |

Remote `main` commit `bcc018e27` (merged PR #95) is synchronized first because
PR #102 is based on it.  The three open PR commits remain independent commits
on the local integration branch so that each can be reverted or reviewed in
isolation.

## Do not merge wholesale

### PR #78 — old Debian VirtIO keyboard gate

[#78](https://github.com/TankTechnology/asterinas-riscv/pull/78) is an
18-commit, 3,837-line branch rooted hundreds of commits behind the current
line.  It adds a separate `tools/riscv/debian/input_gate.py` stack and currently
conflicts with `main`.

The maintained tree now has the later xHCI/input gate under
`tools/riscv/xhci/`, plus desktop-level keyboard and mouse evidence.  The board
has also already demonstrated interactive keyboard and mouse use.  Importing
the older branch would duplicate gate machinery without advancing the current
network/Firefox milestone.  Keep it as historical evidence; extract an
individual test only if a future VirtIO regression is not covered by the
current QEMU desktop gate.

### PRs #84 and #86 — schema-six browser stack

[#84](https://github.com/TankTechnology/asterinas-riscv/pull/84) and
[#86](https://github.com/TankTechnology/asterinas-riscv/pull/86) form a stacked
schema-six `browser-m5` rootfs builder.  Most of their commits are already
patch-equivalent to history on the current line, and the maintained tree still
contains the resulting `browser_m5.py`, signed-source verification, Firefox
fixture, builder support, and tests.

The active implementation is now the schema-seven `browser-web` profile.  It
adds dual proxy/direct networking, live Baidu and Bilibili evidence, browser
security checks, downloadable fixture validation, and a fast development
overlay.  Cherry-picking the remaining unique schema-six commits would modify
the same contract, profile, builder, and manifest files with an older identity
model.  Preserve these PRs as provenance, but do not merge either branch into
the current line.

## Local merge gate

The selected commits may reach `main` only after all of the following hold:

1. the Megrez install, GMAC, and Debian/rootfs unit suites pass;
2. RISC-V OSTD with `cfg(ktest)` checks successfully;
3. both RISC-V and x86-64 kernel builds succeed for the architecture gate;
4. the Asterinas persona review has no confirmed critical or major defect;
5. the schema-seven rootfs remains immutable and browser-only edits continue
   to use the derived overlay rather than a full rootfs rebuild.
