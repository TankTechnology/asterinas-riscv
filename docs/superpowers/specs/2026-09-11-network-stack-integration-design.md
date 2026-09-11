# Isolated network stack integration design

## Goal

Integrate the useful IPv6 and IPv4-mapped dual-stack socket work from pull
request 137 onto the tested `asterinas-riscv/main` baseline without importing
its unrelated browser or rootfs changes.  The result must preserve the current
Megrez boot, graphics, Firefox, evidence, and recovery contracts while adding a
bounded, reproducible dual-stack network validation path.

The integration starts from `main@e4eaf7b2d3abd81f454ec2b8b6372df024fd80d1`
on the isolated branch `codex/network-stack-integration`.  No change reaches
remote `main` until every software gate and the deliberately small physical
validation sequence passes.

## Frozen baseline

The current physical-browser baseline is an input to this work, not part of the
network merge surface.  In particular, the following remain unchanged unless a
network regression test proves that an interface adaptation is unavoidable:

- the Debian root image and package set;
- Stage1, U-Boot, MMC artifacts, and deployment attestations;
- the Firefox Marionette gate, proxy bridge, framebuffer evidence, and timeout
  contracts;
- the Megrez board session, recovery, and physical graphics orchestration.

The 427-test browser, desktop, GMAC, boot-stability, physical-graphics, and
rootfs matrix is the frozen host-side regression gate.  A clean worktree needs
the disposable
`target/current-main-physical-graphics/physical` parent directory before that
matrix runs; this is a pre-existing test-fixture precondition, not a network
change.  With the directory present, the baseline passes all 427 tests with
`ResourceWarning` promoted to an error.

## Source selection

The integration replays behavior in dependency order from these pull-request
commits:

1. `3ef0ef594` — AF_INET6 UDP sockets and `IPV6_V6ONLY`;
2. `285b86443` — IPv4-mapped UDP dual-stack routing and binding;
3. `94b099ca5` — completion and correction of the mapped UDP path;
4. `22a15cb99` — IPv4-mapped TCP dual-stack listeners;
5. `5b4ee17a9` — a bounded QEMU dual-stack gate.

These hashes identify the source behavior and attribution.  They are not a
promise to use literal conflict-blind cherry-picks.  Where current `main` has
evolved, the patch is adapted to the current interfaces and committed in a
reviewable unit.

Commit `a94151c6e` is review input, not an automatically selected patch.  It
copies a UDP receive payload into a kernel buffer while the socket lock is held
and writes to userspace after releasing the lock.  Current `main` already has
the broader `6f47cf1da` socket-user-buffer prefault mechanism.  The existing
prefault behavior is the default.  A UDP-specific bounce buffer is introduced
only if a focused fault-under-lock test demonstrates that prefaulting is
insufficient, and then only with an explicit explanation of the remaining race
or lock dependency.

The following pull-request commits are explicitly excluded:

- `b42c3da3e` — Bilibili browser playback gate;
- `dd0a80d7a` — rootfs transport mirror fallback;
- `6f1d6c550` — cached archive selection;
- `d88a412f2` — Marionette playback polling.

No content from the stale `codex/megrez-dwmac-board-current` branch is used.
The private-network tracking branch has no commits absent from current `main`,
so it contributes no source changes either.

## Semantic replay strategy

The work proceeds as four independently testable layers:

1. Add AF_INET6 datagram creation and the `IPV6_V6ONLY` option at the syscall
   and kernel-socket layer.
2. Add IPv4-mapped UDP address normalization, binding, lookup, ingress, and
   egress behavior across `aster-bigtcp` and the kernel socket adapter.
3. Add IPv4-mapped TCP listener binding and lookup behavior.
4. Add the bounded regression runner and its unambiguous completion markers.

For each layer, the source diff is compared with both its original parent and
current `main`.  Changes already present on `main` are dropped.  Changed APIs
are adapted semantically.  Broad `ours` or `theirs` conflict resolution is not
allowed.  In particular, `iface/poll.rs`, `socket_table.rs`, stream socket code,
and `run_test.sh` have changed on both sides and require line-by-line reasoning.

The implementation must preserve the following socket semantics:

- an AF_INET6 socket defaults to dual-stack behavior unless `IPV6_V6ONLY` is
  enabled;
- a v6-only socket does not accept or emit IPv4-mapped traffic;
- mapped addresses participate in bind-conflict and listener lookup rules
  consistently rather than bypassing the socket table;
- UDP send, receive, and ingress routing agree on address representation;
- TCP mapped listener selection preserves existing specific-address and
  wildcard precedence;
- existing AF_INET IPv4 and native IPv6 behavior remains unchanged.

If an imported patch requires modifying the frozen browser/rootfs files, that
is treated as evidence that the patch boundary is wrong.  The integration
stops and the network behavior is extracted more narrowly.

## Commit boundaries

The preferred history keeps behavior and evidence easy to bisect:

- AF_INET6 datagram and `IPV6_V6ONLY` support with focused regression coverage;
- mapped UDP support and its focused regression coverage;
- mapped TCP listener support and its focused regression coverage;
- bounded dual-stack QEMU orchestration;
- any current-main-specific correction proven necessary by a failing test.

When a source commit cannot be retained intact because of current-main API
changes, its replacement commit records the source hash in the body.  A test
must accompany or precede the behavior it protects.  The optional UDP locking
correction, if needed, remains a separate commit so its necessity and memory
cost are visible.

## Validation contract

Validation is staged so failures remain local and informative.  Each stage
must pass before the next begins.

### 1. Focused host and component checks

Run formatting and the smallest relevant Rust and regression checks for every
changed socket module.  Add focused tests for AF_INET6 creation,
`IPV6_V6ONLY`, mapped bind conflicts, UDP send/receive, TCP listener selection,
and native IPv4/IPv6 non-regression.  Any lock-safety change must have a test
that faults a user buffer at the relevant syscall boundary rather than relying
on code inspection alone.

All builds and tests run through `tools/docker/run_dev_container.sh` with the
existing persistent Cargo, Rustup, and Nix caches.  The workflow must not
delete the image, create a fresh uncached build environment, or reinstall
`cargo-osdk`.

### 2. Architecture-aware QEMU gates

Because the edited network stack is shared, run the relevant x86_64 network
regressions as well as a bounded RISC-V dual-stack gate.  The RISC-V result is
accepted only when the serial log contains all of these exact success facts:

- `ASTERINAS_IPV6_DUAL_STACK_TCP_OK`;
- `ipv6_udp: PASS`;
- `ASTERINAS_IPV6_DUAL_STACK_UDP_OK`.

Timeout, kernel panic, missing marker, duplicated stale marker, or an early
runner exit is a failure.  The gate records the command, architecture, kernel
revision, exit status, and bounded log path so a failed run can be diagnosed
without repeating it blindly.

### 3. Frozen host-side regression matrix

Run the existing 427-test browser, proxy, desktop, GMAC, boot-stability,
physical-graphics, browser-contract, and Debian-rootfs matrix with
`ResourceWarning` treated as an error.  Network integration must not weaken or
rewrite those assertions to make this stage pass.

### 4. Minimal physical validation

After software gates pass, use one lightweight Megrez boot and GMAC/IPv4 smoke
test against the already deployed artifacts.  It must verify kernel boot,
interface readiness, the existing IPv4 path, bounded serial evidence, and
recovery to a fresh U-Boot prompt.  It must not rewrite MMC or rebuild the
Debian/Firefox root image.

Only after that smoke test passes, run one complete Firefox Baidu transaction
to prove that the existing IPv4 proxy path, TLS navigation, framebuffer
evidence, and recovery contract have not regressed.  This is one final
cross-layer qualification run, not the primary network-debugging loop.

## Failure and publication policy

The integration fails closed.  At the first semantic conflict or regression,
the current layer is diagnosed with its focused test and captured logs before
later source commits are applied.  Unrelated cleanups and opportunistic browser
or deployment changes are deferred.

The branch stays separate from remote `main` during development.  After all
gates pass, the diff is reviewed normally for correctness, maintainability,
security, and hardware-facing behavior.  Publication then uses a non-force
push, with remote `main` fetched immediately beforehand.  If remote `main` has
moved, the integration is replayed or rebased and the affected gates are run
again; history is never forced over newer remote work.

## Completion criteria

The integration is complete only when:

- the final diff contains the selected network behavior and no excluded
  browser/rootfs work;
- the `a94151c6e` decision is recorded as either unnecessary due to proven
  prefault coverage or necessary due to a reproducing test;
- focused and architecture-aware QEMU tests pass with the required markers;
- the frozen 427-test matrix passes;
- one lightweight physical network boot and one final Firefox transaction pass
  without MMC rewriting, browser restarts, or recovery failure;
- the branch is clean, review findings are resolved, and the remote update can
  be performed as a non-force fast-forward.
