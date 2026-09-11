# Megrez browser evidence hardening design

## Goal

Harden the already passing physical Firefox workflow before importing any
network-stack branch.  A successful result must be internally consistent,
owned host resources must be deterministically released, and the clock API
must describe its actual serial-based implementation.

## Scope

This change is limited to the host-side Megrez proxy and Firefox orchestration
plus their tests.  It does not change the kernel, Debian root image, Stage1,
guest browser gate, MMC contents, boot arguments, or network stack.  It also
does not split `megrez_desktop.py`; that would enlarge the merge surface without
improving the evidence contract.

## Proxy lifecycle contract

`ProxyBridge` is a one-shot owner of one `socat` process group.  It records two
different facts internally:

- whether the listener has ever passed its bounded startup probe;
- whether the owned process is currently running.

The public `running` property continues to report current liveness.  The
published `ready` field reports the latched startup fact, so a normal summary
taken after shutdown can prove that the bridge was usable during the
transaction.  A bridge that never passed startup must always publish
`ready=false`.

After `close`, another `start` is rejected.  Normal shutdown sends `SIGTERM`,
waits with the configured deadline, and escalates to `SIGKILL` only when
needed.  If the process group disappears between `poll` and `killpg`, shutdown
treats `ProcessLookupError` as an exit race and still reaps the child.  Other
signal errors remain failures.

The bridge captures bounded stderr before closing an internally-created spool.
Caller-owned stderr objects remain open.  Repeated `close` calls are safe, and
`summary` after close uses the stored stderr bytes without touching the closed
spool.

## Firefox result and clock contracts

The Firefox orchestration calls `synchronize_clock(timeout)` without a browser
PID because setting the system clock is global and uses the existing root
serial shell, not Firefox's network namespace.  The host epoch must fall in the
inclusive 2024-01-01 through 2100-12-31 UTC range before it is sent.  The guest
must echo the exact requested host epoch and a guest epoch no more than five
seconds later.

A `FirefoxBrowseResult` with `passed=true` is valid only when its proxy summary
has `ready=true`.  Failed results may retain `ready=false`, allowing startup
failures to be published without forging success.  Normal proxy termination
after a completed run may still have a nonzero signal-derived exit status; the
latched readiness and owned shutdown evidence distinguish that from startup
failure.

## Testing

Tests are written before implementation and must demonstrate the missing
behavior first.  Coverage includes:

- ready remains latched in a summary after normal close while `running` is
  false;
- a never-ready bridge remains false;
- restart after close is rejected;
- `ProcessLookupError` during TERM/KILL is bounded and the child is reaped;
- internally-owned stderr is closed while caller-owned stderr remains open;
- summaries after close retain captured stderr;
- the clock API takes no browser PID and rejects an out-of-range host clock;
- a passing Firefox result rejects absent or false proxy readiness;
- failed Firefox results remain publishable with false readiness.

The final verification runs the proxy, Firefox browse, desktop, GMAC, boot
stability, physical graphics, browser guest-contract, and Debian rootfs test
modules through the persistent Docker container.  Python bytecode, Bash syntax,
and diff checks follow.  One unchanged unattended physical Firefox transaction
then verifies the host-only cleanup against the existing attested MMC artifacts
and must recover to a fresh U-Boot prompt.

## Integration

The cleanup is committed in small logical commits.  After tests, normal review,
and the physical transaction pass, the branch is fetched against
`asterinas-riscv/main` and pushed only as a non-force fast-forward.  Network
branch commits remain outside this change.
