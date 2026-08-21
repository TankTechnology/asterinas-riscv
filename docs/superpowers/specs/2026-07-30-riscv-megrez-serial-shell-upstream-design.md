# RISC-V Megrez Serial Shell Upstream Design

> Status: D1 and D3 describe reviewed, uncommitted implementations. The revised
> D2 and D4 sections are design proposals awaiting explicit user approval.

## Goal

Reach an interactive BusyBox shell on Milk-V Megrez while keeping every
architecture fix and device feature independently reviewable upstream.

Development uses two lines:

- a local integration branch that composes all accepted and candidate changes
  for immediate board validation;
- four board-neutral upstream branches, D1 through D4, whose commits retain the
  same boundaries as the integration branch.

No new Python helper or board-only kernel mode is part of this work.

## Scope

The required path is:

```text
U-Boot booti
  -> Asterinas Image
  -> rootfs and PID 1
  -> BusyBox exec
  -> DesignWare APB UART TX
  -> DesignWare APB UART RX
  -> interactive serial shell
```

The following work is intentionally outside this series:

- HDMI and `simple-framebuffer`;
- USB, PCI, IOMMU, DMA-coherency, and AIA support;
- persistent U-Boot environment changes;
- a board-specific machine abstraction;
- new host-side scripts.

## Development Lines

### Local integration line

`codex/megrez-serial-shell-integration` starts at the current
`upstream/main`. It composes the already reviewed Image and Svade changes with
D1 through D4. Each new logical change remains a separate commit so that board
evidence can be attributed to, and later reproduced from, the corresponding
upstream PR.

The integration line is not itself an upstream PR. It may temporarily contain
stacked changes while upstream dependencies are under review.

### Upstream PR line

Each PR is based on the newest `upstream/main` available when it is submitted.
D1 and D2 are independent architecture fixes. D3 and D4 are device-driver
changes, with D4 stacked on D3. D3 will be rebased after PR #3426 if that PR
changes the shared UART trait before D3 is submitted.

## D1: Flush RISC-V TLBs After `satp` Activation

All current RISC-V address spaces use ASID zero. The architecture does not
require a `satp` write to invalidate translations for that ASID. Therefore
`activate_page_table` must execute `sfence.vma` after changing `satp` and before
the new address space is used.

The implementation belongs in `ostd/src/arch/riscv/mm/mod.rs` and calls the
existing `tlb_flush_all_including_global` primitive. It does not add a second
page-table activation API or change other architectures.

QEMU eagerly invalidates translations on a `satp` write, so a production-path
two-`VmSpace` test passes even before this fix. D1 therefore verifies the
required ordering in the final RISC-V machine code, runs the existing OSTD
kernel suite, and reserves behavioral confirmation for real hardware.

## D2: Synchronize RISC-V Instruction Fetches

RISC-V `FENCE.I` synchronizes instruction fetches only on the executing hart.
A task-local deferred flag is insufficient because a task can migrate after
consuming the flag. A software generation is also weaker than the contract
needed at an executable-memory publication boundary: the publishing operation
must not return while another hart can still execute stale instructions.

D2 therefore has one VM-level policy entry point. On RISC-V it:

1. executes a local `fence.i`;
2. executes `fence w, o` so earlier instruction stores are ordered before the
   firmware's remote-interrupt writes;
3. invokes the SBI RFENCE extension for all harts; and
4. treats an SBI failure as fatal instead of warning and continuing with an
   invalid instruction-fetch state.

Other architectures implement the same policy entry point as a no-op unless
their hardware contract requires otherwise. This keeps the publication policy
in the VM layer and the hardware mechanism in RISC-V OSTD.

The VM layer requests synchronization at every currently implemented ordinary
executable-memory publication boundary:

- after `execve` installs a new executable image;
- in an executable lazy page fault after the page contents are ready but before
  the executable PTE is installed; and
- in `mprotect` before the first changed executable PTE is installed, while the
  VMAR write lock still preserves the publication order.

The page-fault placement is intentional. `Vmar::handle_page_fault` holds a
shared VMAR lock, so another hart can resolve the same mapping concurrently.
Synchronizing only after the first handler returns would allow the second
handler to observe the new PTE and return to user mode too early. The single
page and read-around paths therefore synchronize before their first executable
`cursor.map`.

`mprotect` also requires special care. It can update mappings before
discovering a later hole and returning `ENOMEM`. Synchronizing once before its
first changed executable mapping covers every earlier publication even when a
later hole produces the error. A changed mapping whose new permissions include
`EXEC` is synchronized even for transitions such as `RWX -> RX`; an exact
no-op permission request does not synchronize.

The existing RISC-V userspace `flush_icache` syscall remains a separate JIT
interface. D2 does not add a new host-side helper or a task-migration protocol.

Tests use existing C regression infrastructure under `#if __riscv`: one case
writes a small RISC-V function into anonymous RW memory, changes it to RX, and
calls it; another lazily faults executable code from a file-backed mapping.
Neither test calls a compiler cache-clear builtin. Focused OSTD tests cover SBI
success and fatal failure around the shared synchronization helper. RISC-V
SMP=4 execution and BusyBox `execve` are the integration gates.

## D3: Firmware-Preserving DesignWare APB UART TX

D3 supports the UART selected by `/chosen/stdout-path`. The selector may be a
device-tree alias or an absolute path and may carry serial options after `:`.
If the property is present but invalid or unresolved, initialization fails
closed instead of silently selecting a different UART. The legacy NS16550A
fallback remains available only when `stdout-path` is absent.

For `snps,dw-apb-uart`, D3 validates:

- enabled status;
- a non-overflowing first `reg` range;
- `reg-shift = 2`;
- `reg-io-width = 4`;
- enough MMIO space for THR and LSR.

The driver performs shifted 32-bit THR/LSR access, preserves firmware baud,
line, FIFO, clock, and reset configuration, and uses one bounded poll budget
for each output buffer. It does not call `Ns16550aUart::init`, enable
interrupts, or implement RX.

Rust kernel tests use an in-memory access implementation to verify exact
register offsets, width-independent values, CRLF output, MMIO errors, ownership,
and timeout bounds. QEMU must retain the existing NS16550A console behavior,
and Megrez must emit an exact user-space marker.

## D4: Bounded DesignWare APB UART RX

D4 builds on D3 and maps the selected UART interrupt through the existing PLIC.
It enables only the receive-data interrupt after the callback and PLIC mapping
are ready. Initialization failure leaves TX available.

The PLIC prerequisite interprets `riscv,ndev = N` as source IDs `1..=N` and
allocates a reserved source-zero slot in addition to those N entries. An
unknown interrupt parent, source zero, or a source above N returns an error
instead of panicking or indexing out of bounds.

The bounded UART IRQ path:

- reads IIR and handles receive-data, receive-timeout, and DesignWare busy
  causes;
- reads USR to clear a busy cause;
- reads LSR for a receiver-line-status cause;
- performs the documented dummy RBR read when a character-timeout cause
  reports no available byte;
- drains at most four 16-byte batches per interrupt invocation;
- leaves additional FIFO data to retrigger the level-sensitive interrupt;
- best-effort masks the UART receive source and always masks its mapped PLIC
  source after an MMIO failure or unsupported cause, preventing a storm even
  when the UART IER write itself fails;
- invokes TTY callbacks outside the hardware-access lock;
- never performs allocation, sleeping work, or unbounded polling in interrupt
  context.

The serial callback does not run the TTY line discipline from the interrupt.
It copies bytes into a preallocated bounded ring buffer and wakes one dedicated
kernel thread. That thread drains the queue and calls `Tty::push_input` in task
context. `SerialDriver::echo_callback` only appends to a second preallocated
ring while the line-discipline spinlock is held; after `push_input` releases
that lock, the same input thread drains echo through D3's sleeping TX
serialization. The single owner preserves input/echo order without changing
the generic `Tty` implementation, using an SBI fallback, or adding a softirq
dependency.

The proposal changes approximately five existing files: two PLIC files, the DW
APB driver, the generic UART console callback path, and the serial TTY adapter.
It adds no Python script and no new Cargo dependency.

Tests cover PLIC source zero, one, N, N+1, and an unknown parent; exact shifted
32-bit IIR/IER/RBR/LSR/USR accesses; receive-data, timeout, line-status, busy,
MMIO-failure, and unsupported-cause behavior; the 64-byte IRQ budget; input and
echo ring-buffer overflow; and task-context TTY delivery. The board gate sends
a unique token through UART IRQ, the queue, TTY, and a blocking user read, then
executes an exact interactive shell command with ordered echo.

## Verification Matrix

| Layer | Required evidence |
| --- | --- |
| Source quality | `cargo fmt`, Clippy/check, `git diff --check`, Asterinas code review |
| Cross-architecture | RISC-V build plus x86-64 and LoongArch compile checks |
| D1 | Before/after instruction-order evidence; Sv48 ktests; Sv39 build; real-hardware activation |
| D2 | SBI success/failure ktests; RISC-V SMP=4 executable-page, `mprotect`, and `execve` gates |
| D3 | DT selection and fake-MMIO ktests; existing NS16550A QEMU boot; Megrez TX marker |
| D4 | Bounded IRQ/PLIC/ring-buffer/TTY ktests; Megrez RX token, echo, and interactive command |
| End to end | Three independent Megrez boots reaching BusyBox and printing `ASTERINAS_MEGREZ_OK` |
| Debuggability | One controlled panic with a complete symbolizable backtrace |

Every new commit is shown to the user with its diff and fresh test evidence
before it is created.
