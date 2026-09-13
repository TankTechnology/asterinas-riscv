# Megrez Fast Probe Design

## Goal

Make a lightweight kernel probe the default Megrez development loop.
One host command must boot the existing MMC deployment, run one or more probes,
retain a small result, and return the board to U-Boot.
The normal path should finish in about one minute when the selected probes are
short.

Firefox, systemd, the partition-2 Debian root, RockOS, deployment attestation,
three-cycle stability testing, HDMI, keyboard, and network success are not part
of this path.
They remain separate, explicitly selected workflows.

## User Interface

The only normal entry point is:

```bash
python3 -m tools.riscv.megrez_probe boot syscall213 syscall272
```

The command automatically uses the current deployment bundle and the known
serial device unless either is explicitly overridden.
Users do not supply artifact addresses, three MMC paths, evidence paths,
timeouts, or recovery inputs for each run.

An optional bounded shell is selected before boot:

```bash
python3 -m tools.riscv.megrez_probe ext2-writeback --shell --session-seconds 180
```

Without `--shell`, the total guest lifetime defaults to 90 seconds and the
guest reboots as soon as all probes finish.
With `--shell`, the first implementation accepts one fixed total guest lifetime
from 30 through 300 seconds.
Boot and probe execution consume part of that lifetime; the shell receives the
remainder.
It does not add a renewable lease protocol.

## Architecture

Use one small host orchestrator and reuse the existing serial, MMC loading,
debug-console, and recovery implementations.
Do not create a new board-session framework, transport, daemon, or per-probe
orchestrator.

The host orchestrator performs this sequence:

1. Read one `current.json` deployment bundle.
2. Open and exclusively lock the serial device.
3. Load the three versioned files from MMC and verify their existing size and
   CRC32 identities.
4. Boot the minimal Stage1 initramfs with the isolated root debug console and
   `asterinas.reboot_after=<limit>`.
5. Send one ordered batch containing the selected probes.
6. Read nonce-bound `START`, `PASS` or `FAIL`, and `DONE` records.
7. On success, retain only the structured result and a short serial summary.
8. On failure, collect one bounded dmesg tail and the failing probe's output.
9. Request immediate reboot, then require a fresh OpenSBI/U-Boot epoch.

The software reboot deadline is armed before probes run and is never disabled.
If a probe or shell blocks while timer interrupts continue, the deadline
recovers the board.
An entire-kernel hard lock remains outside this software guarantee.

## Deployment Bundle

`current.json` is the only default input.
It records the selected plan identity, kernel, Stage1 initramfs, DTB, load
addresses, sizes, CRC32 values, and serial device.
Changing a kernel or Stage1 artifact atomically replaces this bundle only after
the artifact has been placed on MMC.

The fast probe checks MMC size and CRC32 but does not boot RockOS or re-run the
SHA-256 attestation.
Release certification continues to validate the retained RockOS measurement
and SHA-256 receipt.

## Probe Contract

A probe is a named, bounded command supported by the Stage1 probe agent.
Each name maps to an argument vector and a maximum duration; it does not map to
an ad-hoc host script.
The initial registry contains only probes required by current kernel work:

- `boot`: minimal userspace and debug-console readiness;
- `syscall213`: focused syscall 213 behavior and caller evidence;
- `syscall272`: focused syscall 272 behavior and caller evidence;
- `ext2-writeback`: mount/writeback state and the two observed failed pages;
- `systemd-compat`: the kernel interfaces behind the current sysctl and random
  seed failures, without starting systemd.

Multiple names run in one boot.
Unknown, duplicate, or unbounded probes fail before the board is touched.
Adding a probe extends the single registry and probe agent; it does not add a
new lifecycle script.

## Output and Failure Handling

Every run writes one private directory containing:

- `result.json`: deployment identity, ordered probe outcomes, elapsed time,
  recovery state, and terminal reason;
- `serial-summary.log`: protocol records and at most a small surrounding
  context;
- `failure.dmesg.log`: present only after a probe or recovery failure;
- `sha256sums.txt`: hashes of the retained files.

`result.json` is published last.
The next run invalidates an earlier terminal result before opening the plan or
serial device.
There is no full dmesg, journal, process tree, graphics log, or three-cycle
evidence on a successful fast probe.

If the guest deadline expires, the host waits only for the bounded recovery
window.
If no new U-Boot epoch appears, it reports `manual-reset-required` and stops;
it does not send speculative commands to an unknown board state.

## Stability Boundary

The existing `asterinas.reboot_after` uses an Asterinas timer callback and SBI
cold reboot.
It cannot recover if the whole kernel stops receiving timer interrupts or an
SBI reset call hangs.
The EIC7700X WDT0 configuration has readback evidence but no reliable physical
reset proof, so it is not part of the default fast path.

A separate, one-time milestone may validate WDT0 or an externally controlled
reset line.
Only after that proof may the fast probe advertise unattended recovery from a
complete kernel hard lock.

## Validation

Implementation is accepted when:

1. Unit tests reject unsafe bundles, invalid probe names, protocol replay,
   missing terminal records, stale results, and unbounded session durations.
2. A QEMU run executes at least two probes in one boot and verifies both normal
   reboot and software-deadline recovery.
3. One physical Megrez run executes `boot` plus one focused kernel probe,
   publishes a hash-valid result, and returns to a fresh U-Boot prompt.
4. A successful run transfers no large serial payload, boots neither RockOS
   nor Firefox, writes neither partition 2 nor U-Boot environment, and collects
   no full diagnostic bundle.
5. The user-facing invocation requires no more than the probe names in the
   normal case.

## Deferred Work

Dynamic lease renewal, arbitrary remote shell upload, hardware watchdog
enablement, automatic artifact deployment, Firefox certification, network
testing, and removal of older specialized tools are deliberately deferred.
Older tools remain available for reproducing their historical evidence, but
the fast probe does not call them as separate preparation stages.
