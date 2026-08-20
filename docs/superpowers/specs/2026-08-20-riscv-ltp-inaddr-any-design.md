# RISC-V LTP IPv4 `INADDR_ANY` Design

## Status

Approved in conversation on 2026-08-20. The user explicitly prioritized basic
system usability—NixOS integration, a graphical QEMU desktop, and browser use—
over broad network-stack completeness. This change therefore implements the
smallest coherent IPv4/TCP wildcard-listener capability and stops once its LTP
and regression gates pass.

## Context

The RISC-V LTP baseline reports five crashes in `connect01`, `recv01`,
`recvfrom01`, `send01`, and `sendto01`. Each test first creates an IPv4/TCP
server and calls `bind(INADDR_ANY)`. Asterinas rejects that bind with
`EADDRNOTAVAIL`; the legacy LTP cleanup then kills its process group.

Loopback is already initialized as `127.0.0.1/8`. The failure is in socket
ownership: `resolve_bind_iface_and_config` accepts only an exact interface
address, while each `aster-bigtcp` interface owns a separate port table and
socket table. Mapping `0.0.0.0` to loopback alone would pass only a narrow local
case and would leave wildcard listeners invisible to packets received on other
interfaces.

Linux defines `INADDR_ANY` as binding all local interfaces. Its listener lookup
tries the concrete destination address before the wildcard address. A listening
wildcard socket also conflicts with a socket bound to the same port on a
specific local address unless the separate `SO_REUSEPORT` semantics apply.

References:

- [Linux `ip(7)`](https://man7.org/linux/man-pages/man7/ip.7.html)
- [Linux IPv4 listener lookup](https://github.com/torvalds/linux/blob/master/net/ipv4/inet_hashtables.c)
- [Linux socket reuse semantics](https://man7.org/linux/man-pages/man7/socket.7.html)

## Goals

1. Allow IPv4/TCP sockets to bind and listen on `0.0.0.0`.
2. Make one wildcard listener receive traffic arriving on any currently
   registered IPv4 interface.
3. Preserve the concrete ingress interface and destination address on accepted
   connections.
4. Enforce wildcard-versus-specific port and listener conflicts atomically.
5. Eliminate the five RISC-V LTP crash outcomes without broadening this batch
   into a general network-stack rewrite.

## Non-goals

- UDP wildcard binding.
- IPv6 `::` wildcard and IPv4-mapped dual-stack behavior.
- `SO_REUSEPORT` listener groups or load distribution. Asterinas stores this
  option today but does not pass it into the TCP port allocator.
- Dynamic interface addition, multiple network namespaces, or routing-table
  redesign.
- Unrelated socket-option and TCP conformance failures.

These are separate future changes. Once this batch passes its gates, work moves
to two explicit workstreams: a RISC-V architecture-focused LTP milestone and
NixOS/QEMU graphical desktop readiness, including browser execution and only
the DRM/virtio-gpu work required by that path.

## Architecture

### Network-wide IPv4/TCP state

Introduce one `InetSocketSpace<E>` for the current kernel network environment.
The kernel creates it before constructing loopback and virtio-net interfaces
and passes the same `Arc` to each interface.

The shared space owns:

- the TCP/UDP port reservation table; and
- the TCP listener registry.

Established TCP connections and UDP sockets remain in per-interface socket
tables. This keeps packet dispatch and egress scheduling attached to the
correct device while making wildcard listener discovery network-wide.

This is deliberately a single-network-space abstraction, not a full network
namespace implementation. Its ownership boundary permits a later namespace
implementation without another listener-table redesign.

### Port groups and listener state

Reshape the port table around `(protocol, port)` groups. Each group contains
address-specific `PortState` records. A record retains the existing socket and
reuse counters and adds whether a TCP listener is active.

For IPv4/TCP, two addresses conflict when they are identical or either address
is unspecified. Binding and starting a listener both run under the same shared
space lock. This makes these operations atomic with respect to:

- wildcard versus specific address conflicts;
- sockets that were reuse-bound before either one called `listen()`; and
- a later bind racing with a newly active listener.

The existing Asterinas `SO_REUSEADDR` rules remain otherwise unchanged.
`SO_REUSEPORT` is not inferred from `SO_REUSEADDR` and is not implemented here.

Grouping by protocol and port bounds conflict checks to the addresses using one
port; wildcard ephemeral allocation does not scan every socket in the system.

### Listener registry

Split listener storage from the per-interface connection table. The shared
registry keeps concrete listeners hashed by address and port and wildcard
listeners hashed by address family and port.

Ingress lookup follows this order:

1. concrete packet destination address and destination port;
2. IPv4 wildcard address and destination port.

Lookup clones the selected listener `Arc` while holding the shared lock, then
releases the lock before processing the packet or locking the listener backlog.

### Ingress interface and accepted endpoint

Polling must provide the actual `Arc<dyn Iface<E>>` to `PollContext`. Change the
`Iface::poll` receiver to consume an `Arc<Self>` (callers poll a clone), so the
receive path can attach a newly accepted connection to the real ingress
interface without a self-referential weak pointer.

Separate two addresses in a bound port lease:

- `local_addr`: the address exposed by the socket and used by the established
  connection; and
- `reservation_addr`: the address whose shared port reservation is inherited
  and released.

For a wildcard listener, both addresses are `0.0.0.0`. When it accepts a SYN,
the child connection uses the packet's concrete destination as `local_addr`,
the listener's wildcard as `reservation_addr`, and the ingress interface as
its polling/egress interface. Consequently:

- the listener's `getsockname()` remains `0.0.0.0:port`;
- the accepted socket's `getsockname()` returns the actual local destination;
  and
- closing the child decrements the inherited wildcard reservation exactly
  once.

## Data flow

### Bind and listen

1. The syscall layer validates the family and privileged port.
2. An IPv4 unspecified address selects a family-capable control interface but
   reserves its address and port in the shared socket space.
3. `listen()` atomically verifies listener conflicts, marks the port state as
   listening, and registers the listener.
4. If listener construction or registration fails, ownership of the original
   bound port returns to `InitStream`; RAII retains the reservation and exposes
   the existing error to the caller.

The control interface exists only for the listener object's foreground API.
It does not decide which interface receives or owns accepted connections.

### SYN and accept

1. An interface poll checks its local established-connection table.
2. For a new SYN, it queries the shared listener registry using exact-then-any
   lookup.
3. It releases the registry lock and lets the listener process the SYN using
   the current interface context.
4. The listener creates a backlog port lease with the concrete destination,
   inherited wildcard reservation, and current ingress interface.
5. The new connection enters only that interface's established table.
6. `accept()` returns the connection with the concrete local endpoint.

### Close

Closing a listener atomically removes its registry entry and clears the
listening state before resetting backlog connections. The bound-port RAII
object releases the wildcard reservation when its final owner is dropped.
Each accepted connection remains attached to its ingress interface and releases
its inherited lease independently.

## Concurrency and lock ordering

The existing order remains `interface -> per-interface sockets`. The shared
socket-space lock is acquired after those locks only for a short lookup or port
lease update.

No packet processing, socket dispatch, device I/O, observer notification, or
backlog reset occurs while holding the shared lock. Listener creation and close
never retain the shared lock while acquiring a backlog or interface lock. This
avoids an inverse `socket space -> backlog -> interface` path.

The implementation must document this ordering beside the relevant locks and
keep check-and-update operations under one shared-space guard.

## Error handling and invariants

- Binding a nonlocal concrete IPv4 address continues to return
  `EADDRNOTAVAIL`.
- A conflicting wildcard or concrete port/listener returns `EADDRINUSE`.
- Ephemeral-port exhaustion retains the existing `EAGAIN` mapping.
- A backlog lease can be created only from a registered listener reservation.
  The type/API should encode this relationship so normal packet input cannot
  turn it into a recoverable allocation failure.
- Listener removal checks both its key and `Arc` identity before clearing
  state, preventing a stale close from deleting a replacement listener.
- No `unsafe` code is introduced in `kernel/` or `aster-bigtcp`.

## Testing

### Host/unit tests

Add focused tests for the shared port/listener state:

- wildcard then concrete non-reuse bind conflict and the reverse order;
- reuse-bound wildcard and concrete sockets before `listen()`;
- first listener wins and the second receives `EADDRINUSE`;
- an active listener blocks a later conflicting bind;
- TCP and UDP port groups remain isolated;
- close and drop clear listening and reservation state exactly once; and
- listener lookup tries the exact key before the wildcard key.

### Kernel regression test

Add an IPv4/TCP regression under
`test/initramfs/src/regression/network/` that verifies:

- wildcard ephemeral bind and listener `getsockname()`;
- loopback connect, accept, and bidirectional data;
- the accepted socket reports `127.0.0.1`, not `0.0.0.0`;
- active wildcard/specific conflicts in both bind orders; and
- when virtio-net is present, a local connection to its configured IPv4 address
  is accepted by the same wildcard listener.

### RISC-V LTP gate

Run the manifest containing:

- `connect01`
- `recv01`
- `recvfrom01`
- `send01`
- `sendto01`

The gate must report infrastructure PASS, exact manifest/verdict ordering, no
panic, no `CRASH`, and `5/5 PASS`. During QEMU runs, inspect gate status,
`progress.log`, current verdict counts, and serial output rather than waiting
without observing progress.

After the focused gate passes, run one SMP=4 767-test regression. It must retain
infrastructure PASS, introduce no new failures outside the five-test group, and
replace the five historical crash outcomes with passes. SMP=1 is not a routine
paired gate; add it only when the focused evidence exposes a CPU-count or SMP
dependency that needs a control run.

## Required next milestone: RISC-V architecture LTP

The general 767-test syscall baseline is not sufficient evidence for the
RISC-V architecture boundary. The reviewed architecture-sensitive subset of
the current runtime manifest contains 99 tests covering `brk`, clone/context
creation, `getcpu`, futexes, membarrier, memory mappings and protection,
`prctl`, ptrace, scheduler and affinity operations, robust lists, signals,
time/vDSO calls, and `uname`.

The published results for this subset are:

- SMP=1: 63 PASS, 18 FAIL, and 18 CONF;
- SMP=4: 62 PASS, 19 FAIL, and 18 CONF.

The only SMP differential is `getcpu01`, which matches the known failure to
migrate a task after its affinity mask excludes its current CPU.

The same LTP build already contains 39 additional runnable tests from these
architecture-sensitive syscall groups that the current 767-test policy file
does not enable. One further entry, `rt_sigtimedwait01`, still lacks a binary.
The next milestone must therefore:

1. Publish a reviewed `arch-riscv64` manifest containing all 138 currently
   built architecture-sensitive syscall entries, with the missing entry
   recorded rather than silently omitted.
2. Generalize the gate's evidence names away from the hard-coded
   `selected-syscalls` label so named suites retain exact manifest order,
   verdicts, run IDs, and run-owned artifact hashes.
3. Run the architecture manifest on SMP=4 by default. Do not require a paired
   SMP=1 run for every milestone. Add SMP=1 only as a targeted control for
   CPU-count-sensitive failures such as affinity, CPU ID, scheduler migration,
   or an observed SMP differential.
4. Expand the cross-build in a second layer to selected `sched`, `nptl`, and
   `mm` tests. CPU-hotplug tests remain explicitly classified until Asterinas
   exposes the required CPU online/offline interface.
5. Use an Asterinas x86-64 run of the same focused test as a discriminator when
   a failure may be generic rather than RISC-V-specific.
6. Record failure ownership by architecture boundary: syscall ABI, trap and
   signal context, context switch and affinity migration, atomic/futex
   behavior, page-table and fault handling, or vDSO/time behavior.

This architecture milestone receives its own design, plan, immutable baseline
report, and focused fixes. It proceeds alongside the NixOS graphical/browser
readiness work; neither workstream is allowed to erase the other from the
overall RISC-V objective.

## Delivery sequence

1. Add a failing kernel regression for observable wildcard-listener behavior.
2. Refactor shared port/listener ownership while keeping existing tests green;
   add focused unit tests alongside each new internal state transition.
3. Enable IPv4/TCP wildcard bind and exact-then-any lookup.
4. Preserve ingress iface and concrete accepted endpoint.
5. Run host/unit, kernel regression, focused RISC-V LTP, and SMP=4 full gates.
6. Update the RISC-V LTP report with immutable run IDs and artifact hashes.
7. Stop network feature expansion and begin the RISC-V architecture LTP gate
   plus the NixOS graphical/browser readiness audit as explicit workstreams.

## Completion criteria

This batch is complete only when all of the following are true:

- the unit and network regression tests pass;
- the five focused RISC-V LTP cases are `5/5 PASS` with infrastructure PASS;
- the SMP=4 full manifest completes without new regressions;
- run-owned kernel, initramfs, DTB, and boot-disk hashes verify;
- the baseline report records the new evidence; and
- no UDP, IPv6, `SO_REUSEPORT`, or unrelated network feature has been folded
  into the change.
