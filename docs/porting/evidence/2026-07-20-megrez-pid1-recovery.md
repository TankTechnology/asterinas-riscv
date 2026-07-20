# Megrez PID 1 and Recovery Evidence

This page records the latest controlled physical-board result. It separates
what the run proved from the display and input work that remains.

## Candidate identity

| Item | Frozen value |
|---|---|
| Run ID | `megrez-3ef99e6bd153-20260719T173554Z` |
| Source branch | `codex/megrez-porting-handoff` |
| Source commit | `3ef99e6bd15341578b32256c897050e873ca2547` |
| Image SHA-256 | `352f6e045264bd4d3888344ecd7c885d9bde9252ca7bbc743874c408cc551506` |
| initramfs SHA-256 | `b65549ef94936fd3d42e12fe89bde8d1d7af54e20d158b7daaddd26071610174` |
| Raw serial SHA-256 | `e96bfb1a1fb404617178c548dffac378fbac8ef9d4fe707c5aab44c31b74bd99` |
| Volatile bootargs | `cpu_no_boost_1_6ghz loglevel=info init=/init asterinas.first_process_diag=1 asterinas.reboot_after=400` |

The tracked worktree was clean when the candidate was built and after the run.
The Image, initramfs, and loaded RockOS DTB passed the board-side size, checksum,
address, and non-overlap gates. The run did not change the persistent U-Boot
environment or overwrite existing RockOS files. Candidate artifacts were
installed under new names.

## Observed boundaries

| Gate | Result | Observation |
|---|---|---|
| One real U-Boot `booti` | PASS | `Starting kernel ...` was followed by `Enter riscv_boot`. |
| OSTD, SMP, components, rootfs | PASS | Three secondary harts started; OSTD initialized; rootfs became ready. |
| PID 1 enters userspace | PASS | PID 1 entered U-mode, handled its first page fault, called `openat`, and completed `write(fd=1, requested=50)` with result 50. |
| Userspace hello reaches UART | NOT YET | The 50-byte hello was absent from the UART log even though `write` returned 50. |
| HDMI output path | NOT PRESENT | Asterinas reported no framebuffer, so the VT had no HDMI backend. |
| Unattended software recovery | OBSERVED | With no external reset during the controlled window, a fresh DDR/OpenSBI/U-Boot sequence appeared and reached the final `=>` prompt. |
| Safe end state | PASS | The board was left at the U-Boot prompt and the serial owner and transfer services were closed. |

The raw serial stream proves that the candidate epoch reached PID 1 and that a
later, complete firmware epoch occurred. Because the raw stream has no
wall-clock timestamps and the timer deliberately has no trigger marker,
attribution of that later epoch to the configured 400-second timer also relies
on the controlled-session observation. This is evidence of the tested software
recovery path, not a guarantee against a kernel state that stops both timer
delivery and SBI execution.

## First unresolved boundary

The first missing boundary is console routing, not `exec`, paging liveness, or
the first userspace syscall:

1. The live RockOS DTB describes `serial0` as `snps,dw-apb-uart` with
   `reg-shift = <2>` and `reg-io-width = <4>`.
2. The current RISC-V UART component matches only `ns16550a`, so the physical
   UART is not registered as a kernel console.
3. With no framebuffer passed into Asterinas, the `tty0` VT backend accepts and
   discards bytes. This accounts for a successful 50-byte write with no visible
   hello.

The DTB must not be relabelled as `ns16550a`: byte-wide accesses at unshifted
offsets would violate the hardware description and could leave polling code
waiting on the wrong register.

## Unresolved at the end of this run

As of this run, a visible and interactive console remained unproven. The UART
route was missing, Asterinas received no framebuffer, and the frozen diagnostic
initramfs was not an interactive BusyBox shell. The tree also did not provide a
Megrez RISC-V USB-host and HID-keyboard path.

QEMU evidence can bound software behavior such as DT parsing, memory
reservation, mapping, VT rendering, shell startup, serial input, and cleanup.
It cannot prove that U-Boot's HDMI scanout remains active or that a chosen
framebuffer format and cache policy are correct on EIC7700. Those are board-only
questions and were not answered by this run.

## Local evidence location

Raw logs and frozen binaries intentionally remain outside Git under the local
workspace. Their identities are anchored by the hashes above. This tracked page
contains no serial credentials and is the portable handoff record.
