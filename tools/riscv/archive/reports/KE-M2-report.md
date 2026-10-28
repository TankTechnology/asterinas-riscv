# KE-M2 Report — Boot Regression Triage, copy_file_range, bpf Assessment

Track B (kernel evolution), card KE-M2. Commits on `main`:

- `a1f9ee8ba` feat(syscall): register bpf as an ENOSYS stub (280/321)
- `0db995f4f` feat(syscall): implement copy_file_range (285/326)

## Task 1: Early-boot Load page fault on the merged kernel

### Finding: the regression is already fixed on current main

The reported crash (`Unhandled exception: Load page fault` around initramfs
unpack) does **not** reproduce at HEAD. Root-cause analysis of the history:

- `288848e7e` (codex line, Aug 9) made the riscv64 memory-BAR path
  **unconditionally** reassign every PCI BAR from a fresh linear
  `MmioAllocator` seeded at the PCIe 32-bit MMIO window base
  (`0x4000_0000` on QEMU virt) — even BARs that firmware had already
  programmed.
- The Xfce boot chain (`tools/riscv/xfce/boot_xfce_desktop.py`) has U-Boot
  run `pci display` (programming the bochs-display BAR at `0x4000_0000`) and
  bakes that address into a hand-built `simple-framebuffer` DTB node.
  Once the kernel reallocated BARs, the DTB framebuffer address and the
  actual device BAR diverged, and devices could be moved onto addresses
  other drivers/U-Boot state still referenced — hence an early kernel load
  page fault on this chain specifically.
- This was fixed upstream in our own main on Aug 21, before this card:
  - `c2d0f449c` "Harden RISC-V PCI BAR allocation" — on riscv64, keep the
    firmware-assigned BAR when it is non-zero; allocate only when the BAR
    is uninitialized, and restore the original value on failure;
  - `7e6e0ee4e` "Harden RISC-V PCI BAR assignment preflight".

The reported crash log predates those fixes (the on-disk log was later
overwritten by an older successful boot, so the exact faulting PC could not
be re-decoded; the mechanism above is confirmed by the fix commits and by
the boot-chain conflict they describe).

### Verification at HEAD

Headless (VNC-only, no GTK) boot of the current-main kernel on the same
bochs-display + simple-framebuffer chain:

- zero `Load page fault` / `kernel panic` / `Unhandled exception` in the
  serial transcript;
- systemd reached `Graphical Interface`; xfwm4, xfce4-panel, xfdesktop all
  running;
- QEMU monitor screendump shows rendered X11 windows (xterm + GTK3 smoke
  window) — the U-Boot-established framebuffer is intact, i.e. the kernel
  no longer steals the firmware BAR.

No code change was needed; the fix was the two hardening commits above.

## Task 2: copy_file_range (285)

Implemented `kernel/src/syscall/copy_file_range.rs`, modeled on
`sys_sendfile`:

- NULL offsets use and advance the file positions; explicit offsets are
  validated (negative / `off+len` overflow → EINVAL), used, written back,
  and leave the file positions untouched;
- upfront checks matching Linux's `generic_copy_file_checks`: EBADF on
  access mode, EISDIR for a directory input, EBADF for `O_APPEND` output,
  EINVAL for overlapping ranges within the same inode, EINVAL for
  non-zero `flags`;
- short copies past EOF return the number of bytes copied; works across
  filesystems and for pipes via the byte I/O path (no reflink/server-side
  copy attempted).

Registered on the asm-generic table (285, riscv64/loongarch64) and the
x86-64 table (326). Note: the x86-64 table entry was compile-reviewed but
not built (this track builds riscv64 only).

### Verification

New regression test `test/initramfs/src/regression/io/file_io/copy_file_range.c`
(hooked into `io/run_test.sh`):

- passes on host Linux (all assertions), proving the test encodes real
  Linux semantics;
- passes in the riscv64 guest (15/15 assertions) under a minimal
  initramfs; the run ends with the expected "init terminates with code 0"
  kernel panic.

LTP note: `copy_file_range01-03` remain commented out in
`test/initramfs/src/conformance/ltp/testcases/all.txt`; admitting them is a
natural follow-up once an LTP guest run can be scheduled.

## Task 3: bpf (280) assessment

Observed twice per boot in the Xfce desktop guest log. The caller is
systemd's early-boot probing (the calls land in the sysinit window; the
guest's systemd 257.5 is built with `-BPF_FRAMEWORK`, so nothing in the
desktop actually depends on eBPF).

- A real `bpf()` requires the eBPF subsystem — maps, the verifier, program
  attachment points, and a JIT or interpreter. That is a multi-month
  subsystem, not a card.
- Callers treat ENOSYS as "kernel built without CONFIG_BPF_SYSCALL" and
  degrade gracefully, so a stub is semantically correct.

Decision: registered an explicit ENOSYS stub (`kernel/src/syscall/bpf.rs`,
asm-generic 280, x86-64 321). This removes the warn-level "Unimplemented
syscall number: 280" noise and documents the decision. Revisit only when a
real consumer appears (systemd with BPF_FRAMEWORK, or a container runtime).

## Housekeeping

- No out-of-scope files touched; the pre-existing dirty files
  (`tools/riscv/xfce/boot_xfce_desktop.py`, M4 leftovers, `stash@{0}` from
  KE-M1) remain untouched.
- QEMU verification was headless in `/tmp/kem2/` (VNC 5919 / serial files);
  no other session's QEMU was disturbed.
