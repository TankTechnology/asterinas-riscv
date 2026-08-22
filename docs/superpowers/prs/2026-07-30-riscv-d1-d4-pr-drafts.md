# RISC-V D1-D4 Pull Request Drafts

The D1 and D3 drafts describe reviewed, uncommitted implementations. D2 and D4
describe design proposals that have not yet been approved or implemented.
Commit IDs, comparison ranges, final test counts, and fresh Megrez results will
be added only after the user reviews and approves each corresponding commit.

## D1: Flush RISC-V TLBs After Changing `satp`

### Title

```text
Flush RISC-V TLBs after changing satp
```

### Body

```markdown
## Problem

All current RISC-V address spaces use ASID 0. The RISC-V privileged
architecture does not require a `satp` write to invalidate address-translation
caches or order earlier page-table writes with later translations.

After switching page tables, Asterinas could therefore continue using stale
translations from the previous ASID-0 address space on implementations that do
not invalidate them eagerly.

## Fix

- Execute the existing `tlb_flush_all_including_global()` primitive immediately
  after writing `satp`.
- Keep the ordering in the architecture-specific page-table activation
  function so every RISC-V address-space switch receives the same guarantee.
- Do not add another activation API or change x86-64 or LoongArch behavior.

The zero-address, zero-ASID `SFENCE.VMA` is deliberately conservative: it also
invalidates global translations, which is required when every address space
currently reuses ASID 0.

## Scope

This is a six-line RISC-V hardware-contract fix in
`ostd/src/arch/riscv/mm/mod.rs`.

It does not add ASID allocation, change page-table ownership, or alter the
existing remote TLB shootdown mechanism.

## Testing

- `cargo fmt --all -- --check`
- `git diff --check`
- RISC-V OSTD kernel tests
- RISC-V Sv48 and Sv39 kernel builds
- final machine-code inspection confirming the `satp` write is followed by
  `sfence.vma`

QEMU invalidates translations eagerly on `satp` writes, so its two-address-space
behavior does not distinguish the unfixed and fixed kernels. The disassembly
check therefore provides the deterministic regression evidence; behavioral
confirmation remains a real-hardware gate.
```

### Submission Notes

- Base the PR on the newest `upstream/main`.
- Keep it independent of D2, D3, and D4.
- Record the final commit ID and exact test counts after commit approval.
- If a current Megrez integration build exercises D1, report it as integration
  evidence without claiming that it isolates stale translations.

## D3a: Call Console Devices Outside the Registry Lock

### Title

```text
Call console devices outside the registry lock
```

### Body

```markdown
## Problem

The logger currently holds the global console-device spinlock while invoking
every device's `send` callback. The lock disables local IRQs, so device I/O runs
in atomic context and cannot use a sleeping serialization primitive or a
device-specific nonblocking policy.

The public mutable registry guard also lets callers modify the device table
without preserving any derived read-side representation.

## Fix

- Store console registrations behind a sleeping mutex, which is used only by
  component and first-process initialization paths.
- Publish an allocation-free `Arc<[Arc<dyn AnyConsoleDevice>]>` snapshot after
  each registration.
- Clone only the outer `Arc` while holding the short snapshot spinlock, then
  invoke device callbacks after releasing it.
- Serialize normal log records with a sleeping mutex.
- Let atomic and IRQ callers skip that mutex and rely on each console device's
  existing nonblocking policy.
- Remove the mutable registry-lock API and expose the existing OSTD atomic-mode
  state as a read-only query.

The logging fast path performs no allocation and no device I/O while holding a
global spinlock.

## Scope

This is a console infrastructure prerequisite for a UART whose transmitter
must sleep under ordinary contention but refuse I/O from atomic context.

It does not select a console, change console registration order, introduce a
logging worker, or add a deferred log buffer.

## Testing

- `cargo fmt --all -- --check`
- `git diff --check`
- `make check`
- x86-64, RISC-V, and LoongArch kernel compile checks
- RISC-V four-HART QEMU boot through `Successfully booted.`
```

### Submission Notes

- Submit independently so the locking and API change can be reviewed without
  the DesignWare driver.
- The follow-up D3b PR depends on this change.
- Document that dedicated snapshot-replacement concurrency coverage is a
  nonblocking follow-up; all current registration callers are initialization
  paths.

## D3b: Support Firmware-Selected DW APB UART Transmit

### Title

```text
Support firmware-selected DW APB UART transmit
```

### Body

```markdown
## Problem

The RISC-V UART component currently initializes only an NS16550A-compatible
device. Platforms such as Milk-V Megrez expose a DesignWare APB UART selected
by `/chosen/stdout-path`, with shifted 32-bit register accesses.

Without recognizing the firmware-selected device, Asterinas can reach user
mode while losing its runtime serial console.

## Fix

- Resolve `/chosen/stdout-path` as either a device-tree alias or an absolute
  path and ignore serial options after `:`.
- Fail closed when a present selector is malformed, unresolved, disabled, or
  unsupported.
- Preserve the legacy first-enabled-NS16550A fallback only when
  `stdout-path` is absent.
- Prefer the exact `snps,dw-apb-uart` compatible when a node also advertises
  `ns16550a`.
- Validate the first non-overflowing `reg` range, `reg-shift = 2`,
  `reg-io-width = 4`, and the MMIO span required by THR and LSR.
- Use shifted 32-bit THR/LSR accesses and preserve firmware baud rate, line
  control, FIFO, clock, reset, and interrupt configuration.
- Add a one-second per-character readiness deadline, mark the transmitter
  failed after an MMIO or timeout error, and abort instead of continuing with
  an invalid runtime console.
- Keep panic debuggability: an MMIO invariant failure is reported through the
  SBI-backed early console with a stack trace before aborting.
- Refuse transmitter access in atomic or IRQ context before acquiring a
  sleeping mutex or touching MMIO.
- Add a RISC-V `fence iorw, iorw` helper around transmitter ownership handoff.

## Scope

This PR implements firmware-preserving transmit support only. It does not
enable UART interrupts, reprogram hardware configuration, implement receive,
or add board-specific addresses or host-side scripts.

Receive and PLIC integration remain the stacked D4 follow-up.

## Dependency

Depends on the console snapshot/locking prerequisite. Rebase after #3426 if
that PR changes the shared UART interface before submission.

## Testing

- `cargo fmt --all -- --check`
- `git diff --check`
- `make check`
- RISC-V and LoongArch kernel builds; x86-64 checked by the default workspace
  lint/build path
- 31 focused RISC-V UART kernel tests covering:
  - `stdout-path` parsing, fallback, status, and compatible selection
  - register layout and MMIO-range validation
  - shifted 32-bit reads and writes
  - CRLF transmission
  - timeout and MMIO failures
  - ownership, atomic-context rejection, and failure publication
- RISC-V four-HART QEMU boot through `Successfully booted.`, retaining the
  existing NS16550A fallback

The historical Megrez prototype proved the DW APB UART TX contract on the
board. A fresh run of this reviewed implementation is still required before
claiming current-source Megrez evidence.
```

### Submission Notes

- Keep the RISC-V I/O-fence helper as one preparatory commit and the UART
  implementation as the following commit in this PR.
- Add the final comparison range after the prerequisite PR branch is pushed.
- Attach one fresh Megrez transcript showing a single registration message and
  an exact userspace marker before changing the PR from draft to ready.

## D2: Synchronize RISC-V Instruction Fetches at VM Publication

> Proposal only: the user has not yet approved this design.

### Title

```text
Synchronize RISC-V instruction fetches at VM publication
```

### Body

```markdown
## Problem

RISC-V `FENCE.I` affects only the executing hart. A local fence, or a deferred
flag consumed by one task, cannot guarantee that another hart stops executing
stale instructions after `execve`, an executable lazy page fault, or
`mprotect`.

The publication operation must not return while another hart can still observe
the old instruction stream.

## Fix

- Add one VM-layer instruction-cache synchronization policy.
- Implement the RISC-V mechanism with a local `fence.i`, `fence w, o` before
  the remote interrupt, and SBI RFENCE over all harts.
- Treat an SBI RFENCE error as fatal instead of warning and continuing.
- Invoke the policy after:
  - a new `execve` image is installed;
  - executable page contents are ready but before the page-fault path installs
    the executable PTE; and
  - `mprotect` determines that it will change an executable mapping but before
    the first affected PTE is updated.
- Preserve the `mprotect` side-effect contract: if earlier mappings became
  executable before a later hole causes `ENOMEM`, the synchronization already
  ordered those publications before the error is returned.
- Synchronize any changed mapping whose new permissions include `EXEC`,
  including `RWX -> RX`, while skipping exact no-op permission requests.

Other architectures use the same policy entry point as a no-op. The existing
RISC-V `flush_icache` syscall remains the explicit userspace JIT interface.

## Scope

This PR does not add task-local generations, a migration protocol, a new IPI
mechanism, or a host-side script.

## Testing

- Focused OSTD tests for SBI success and fatal failure
- Existing C regression framework under `#if __riscv`
  - anonymous RW mapping, write a small RISC-V function, `mprotect` RX, execute
  - file-backed lazy executable mapping, fault, execute
  - no compiler cache-clear builtin in either regression
- RISC-V SMP=4 kernel tests and userspace regression tests
- BusyBox `execve` integration
- `cargo fmt --all -- --check`
- `git diff --check`
- `make check`
- RISC-V, x86-64, and LoongArch compile checks
```

### Submission Notes

- Keep D2 independent of D1, D3, and D4.
- Cite the RISC-V Zifencei and SBI RFENCE specifications in the final PR.
- Replace this proposed test list with exact commands and results before asking
  the user to approve its commit.

## D4: Support Bounded DW APB UART Receive

> Proposal only: the user has not yet approved this design.

### Title

```text
Support bounded DW APB UART receive on RISC-V
```

### Body

```markdown
## Problem

The firmware-selected DesignWare APB UART transmit path cannot receive serial
input. Megrez therefore cannot deliver console input through the PLIC to
`ttyS0`, and an interactive shell is unavailable.

The existing PLIC source table also interprets `riscv,ndev = N` as N array
slots even though valid source IDs are `1..=N`, leaving source N out of bounds.

## Fix

- Allocate the PLIC source table with a reserved source-zero slot and accept
  exactly source IDs `1..=N`.
- Return an error for an unknown interrupt parent, source zero, or a source
  above N instead of panicking or indexing out of bounds.
- Map the selected DW APB UART interrupt and enable only receive-data
  interrupts after the callback and PLIC mapping are ready.
- Decode receive-data, character-timeout, receiver-line-status, and DesignWare
  busy causes.
- Read USR to clear busy detect and perform the required dummy RBR read for an
  empty character-timeout cause.
- Drain at most four 16-byte batches per interrupt invocation and rely on the
  level-sensitive interrupt to retrigger for remaining FIFO data.
- Best-effort mask UART RX and always mask its mapped PLIC source after an MMIO
  failure or unsupported interrupt cause, preventing a storm even when the
  UART IER write itself fails.
- Copy received bytes into a preallocated ring buffer from the console
  callback and wake a dedicated kernel thread.
- Run TTY line discipline and signals from that task context.
- Buffer serial echo under the line-discipline lock, then let the same input
  thread drain it after `push_input` returns, where the D3 transmitter may use
  its sleeping serialization safely and input/echo order remains stable.

Initialization failures leave the reviewed D3 transmit path available.

## Scope

This PR is stacked on D3. It changes approximately five existing files and
adds no Python helper, softirq dependency, board address, or UART
reconfiguration.

## Testing

- PLIC source 0, 1, N, N+1, and unknown-parent tests
- Fake-MMIO tests for exact shifted 32-bit IIR/IER/RBR/LSR/USR accesses
- Receive-data, timeout, line-status, busy, MMIO-failure, unsupported-cause,
  and 64-byte budget tests
- Preallocated input/echo ring-buffer capacity, overflow, wakeup, ordering, and
  task-context TTY delivery tests
- `cargo fmt --all -- --check`
- `git diff --check`
- `make check`
- RISC-V UART kernel tests
- RISC-V, x86-64, and LoongArch compile checks
- RISC-V SMP=4 boot
- Fresh Megrez token/ACK, blocking-read, echo, and exact interactive command
```

### Submission Notes

- Keep the PLIC boundary correction as the first commit and the DW RX plus
  task-context delivery as the second commit.
- Base the branch on the final D3b commit and state that dependency explicitly.
- Use the existing board reset/login procedure and historical exact marker
  protocol; do not add another host-side harness.
- Keep the PR draft until the current-source Megrez transcript is attached.
