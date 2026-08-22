# D1 and D3 Pre-commit Review

This packet describes the exact uncommitted D1 and D3 work presented for user
approval. No file in either implementation branch is staged or committed.
The shared base `8a95431093609ed1ecca15b13e4568118513fe06` was reconfirmed as
the current `upstream/main` on 2026-07-30.

## D1

### Change

- Branch: `codex/riscv-satp-tlb-flush`
- Base: `8a95431093609ed1ecca15b13e4568118513fe06`
- Diff: one file, six added lines
- File: `ostd/src/arch/riscv/mm/mod.rs`

`activate_page_table` calls the existing
`tlb_flush_all_including_global()` immediately after writing `satp`. All current
RISC-V address spaces use ASID zero, and the architecture does not make a
`satp` write invalidate translations. The placement gives every existing
activation caller the required ordering without adding another API or changing
another architecture.

### Review result

No code defect remains. The implementation is the smallest change that
satisfies the RISC-V hardware contract.

The test limitation is explicit: QEMU eagerly invalidates translations after a
`satp` write, so a production two-address-space test also passes before the
fix. That test was tried and removed rather than upstreaming a regression test
that cannot detect the regression. Deterministic evidence comes from the final
instruction sequence; real hardware remains the behavioral gate.

### Evidence

- source diff check: pass
- Rust format check: pass
- RISC-V Sv48 OSTD tests: pass
- RISC-V Sv39 build: pass
- x86-64 and LoongArch builds: pass
- before/after disassembly: the new sequence reaches `sfence.vma` after the
  `satp` write

### Proposed commit

```text
Flush RISC-V TLBs after page table activation
```

## D3

### Change

- Branch: `codex/megrez-dw-apb-uart-tx`
- Base: `8a95431093609ed1ecca15b13e4568118513fe06`
- Diff: seven files, 1,057 additions and 33 deletions
- Production code is compact relative to that total; most of the new DW APB
  module is fake-MMIO and policy coverage.

The implementation has three separable layers:

1. publish an allocation-free console-device snapshot and call devices without
   the registry spinlock;
2. provide the RISC-V `fence iorw, iorw` boundary used when UART ownership is
   handed between tasks; and
3. select the firmware `stdout-path` and implement firmware-preserving
   DesignWare APB UART transmit.

The driver validates the selected node and shifted 32-bit register layout,
preserves firmware configuration, serializes task-context transmit with a
sleeping mutex, applies a bounded per-byte readiness deadline, and aborts after
a permanent runtime TX failure rather than silently losing the console.

### Review result

No unresolved defect remains in the D3 scope.

The integration review established these context rules:

- ordinary task-context logs and TTY output use direct DW APB MMIO;
- atomic, IRQ, and softirq callers never acquire the sleeping TX mutex or touch
  DW MMIO;
- the panic stack trace uses the RISC-V SBI early console directly and remains
  available even when the registered DW device rejects atomic-context output;
- the panic headline sent through the registered logger is best-effort in that
  situation; and
- D4 must deliver TTY input and echo from task context rather than adding an
  atomic SBI fallback to D3.

The proposed D4 input/echo ring buffers plus single input-thread design
satisfies the last rule without weakening D3's ownership contract or changing
generic TTY ordering.

### Evidence

- source diff check: pass
- Rust format check: pass
- full `make check`: pass
- RISC-V kernel build: pass
- LoongArch kernel build: pass
- 31 focused RISC-V UART kernel tests: pass
- RISC-V SMP=4 boot: four harts online, PID 1 reached, and
  `Successfully booted.` observed
- independent five-persona review: no unresolved finding

A current-source Megrez run is still required before D3b changes from draft to
ready. Historical board evidence proves the same shifted 32-bit TX hardware
contract but is not presented as fresh evidence for this diff.

### Proposed commits

```text
Call console devices outside the registry lock
Add a RISC-V device I/O fence
Support firmware-selected DW APB UART transmit
```

The first commit is an independently reviewable console prerequisite. The
second and third form the stacked DW APB UART PR.

## Approval boundary

Creating these commits requires an explicit user response. Approval of this
packet does not authorize D2 or D4 implementation; their revised design
proposal has a separate gate.
