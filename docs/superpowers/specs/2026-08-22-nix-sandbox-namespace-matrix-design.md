# Nix Build Sandbox Namespace Matrix Design

## Purpose

A track-B (kernel evolution) design document and the design input for the N2
implementation card.

The Nix build sandbox isolates each derivation build with Linux namespaces.
NixOS cannot do reproducible local builds on Asterinas until the kernel
satisfies the sandbox's namespace contract. This document audits the current
`clone`/`unshare`/`setns` support for every `CLONE_NEW*` flag, defines the
minimal namespace set the Nix sandbox needs, and proposes an implementation
order with risk call-outs.

Scope: design only. No implementation is committed under this document.

## What the Nix sandbox actually requests

From Nix `libstore` (`src/libstore/unix/build/linux-derivation-builder.cc`,
Nix 2.x):

- The builder child is created with `clone(CLONE_NEWNS | CLONE_NEWPID |
  CLONE_NEWIPC | CLONE_NEWUTS | CLONE_NEWUSER | CLONE_NEWNET | SIGCHLD)`.
- `CLONE_NEWUSER` is used whenever unprivileged user namespaces are
  available; the parent then writes `uid_map`/`gid_map`/`setgroups` under
  `/proc/[pid]/` mapping the build uid to root inside the sandbox.
- `CLONE_NEWNET` is part of the default sandbox: the builder gets a private
  network namespace with only loopback, which is how "no network access
  during builds" is enforced. Fixed-output derivations are exempted.
- Inside the sandbox Nix re-mounts `/` private (`MS_PRIVATE | MS_REC`),
  bind-mounts the store and declared inputs read-only, mounts a fresh
  `/dev`, `tmpfs`es, and `pivot_root`/`chroot`s into the build root.
- The sandbox relies on pid 1 semantics inside the PID namespace: the
  builder is init of its namespace; when it exits the kernel SIGKILLs all
  namespaced descendants.

The minimal set for a basic sandboxed build is therefore
**user + pid + mount + ipc + uts**. `net` is required by the *default*
sandbox configuration (private network, loopback only); without it every
sandboxed `clone` fails unless Nix is configured to degrade. `cgroup` and
`time` namespaces are not used by the Nix sandbox.

## Current-state matrix (main @ 927da3d65)

| Namespace  | Flag             | clone/unshare creation                    | setns entry                     | Depth of isolation |
|------------|------------------|-------------------------------------------|----------------------------------|--------------------|
| mount      | `CLONE_NEWNS`    | supported (`NsProxy::new_clone` → `MountNamespace::new_clone`, `kernel/src/fs/vfs/path/mount_namespace.rs`) | supported (`kernel/src/syscall/setns.rs`) | real mount-tree copy per ns |
| uts        | `CLONE_NEWUTS`   | supported (`kernel/src/net/uts_ns.rs`)    | supported                        | real per-ns hostname/domainname |
| ipc        | `CLONE_NEWIPC`   | supported (`kernel/src/ipc/ipc_ns.rs`)    | supported                        | real per-ns SysV sets; permission checks still TODO |
| cgroup     | `CLONE_NEWCGROUP`| supported (`kernel/src/fs/fs_impls/cgroupfs/cgroup_ns.rs`) | supported         | real (rooted cgroup view) |
| user       | `CLONE_NEWUSER`  | **rejected EINVAL** (`clone_user_ns`, `kernel/src/process/clone.rs:819`; `namespace/unshare.rs:43`) | rejected EINVAL | singleton only; no uid_map, no per-ns capabilities (`kernel/src/process/namespace/user_ns.rs` is a 65-line stub) |
| pid        | `CLONE_NEWPID`   | **rejected EINVAL** (`check_unsupported_ns_flags`, `kernel/src/process/namespace/nsproxy.rs:196`) | rejected | no `PidNamespace` type; documented TODO in `NsProxy` |
| net        | `CLONE_NEWNET`   | **rejected EINVAL** (same path)           | rejected                         | no network-namespace type; single global stack |
| time       | `CLONE_NEWTIME`  | **rejected EINVAL** (same path)           | rejected                         | not needed by Nix |

`/proc/[pid]/ns` entries and nsfs-backed `setns` plumbing already exist for
the four supported namespaces (`kernel/src/fs/fs_impls/procfs/pid/task/ns.rs`,
`kernel/src/fs/fs_impls/pseudofs/nsfs.rs`), so new namespace types must also
implement `NsCommonOps` and register procfs entries to stay consistent.

## Gap analysis per required namespace

### user (blocks everything else)

Today `UserNamespace` is a singleton with `owner_uid()` hard-wired to root
and `is_same_or_ancestor_of` reduced to pointer equality. The Nix sandbox's
entire privilege model — unprivileged builds mapping to in-sandbox root —
needs:

- real `UserNamespace` creation with a parent pointer and owner uid;
- `/proc/[pid]/{uid_map,gid_map,setgroups}` write handlers (procfs files do
  not exist yet);
- capability checks evaluated against the *current* user namespace
  (`capable()` must walk the namespace ancestry, per the existing FIXME in
  `is_same_or_ancestor_of`);
- uid/gid translation helpers (`make_kuid`/`from_kuid` equivalents) so file
  ownership and `setuid` behave inside the sandbox.

### pid

Largest structural change. Asterinas keeps a single global pid space
(`kernel/src/process/pid_table.rs`). A PID namespace requires:

- a `PidNamespace` type holding a per-namespace pid allocator and a parent
  chain (a process is visible in its own ns and all ancestors);
- `Process` gains a pid-per-namespace view: `getpid`/`getppid`, `kill`,
  `wait4`, `/proc/[pid]` lookup must resolve in the caller's namespace;
- namespace-init semantics: the first child is pid 1 in its ns; its exit
  must SIGKILL the whole namespace; `fork` from a non-init ns member keeps
  the child in the same ns;
- `/proc` must show only the caller-namespace's processes (procfs currently
  iterates the global table).

### mount / ipc / uts (present, need sandbox-grade hardening)

- mount: creation works. Sandbox use additionally exercises `MS_PRIVATE |
  MS_REC` propagation changes, bind-mount remount read-only, and
  `pivot_root` inside a fresh userns+mountns — mount *propagation* and
  remount-ro paths deserve focused tests even though `pivot_root` exists.
- ipc: per-namespace SysV sets work, but permission checks are TODO
  (`kernel/src/ipc/ipc_ns.rs:195,304`) — acceptable for a build sandbox,
  not for multi-tenant isolation.
- uts: complete enough as-is.

### net (default-sandbox requirement)

No `NetNamespace` exists. A minimal loopback-only namespace (fresh interface
table with `lo` down until configured, no external devices) is enough for
Nix's "no network" guarantee — the sandbox never asks for connectivity,
only for isolation. Interim alternative: boot NixOS with a Nix
configuration that tolerates `CLONE_NEWNET` failing, accepting weaker
isolation until this lands.

## Recommended implementation order

1. **user namespace** (prerequisite for unprivileged sandboxing; unblocks
   running Nix builds as a non-root daemon). Deliverable: userns creation +
   uid/gid maps + capability scoping; `clone(CLONE_NEWUSER)` stops
   returning EINVAL.
2. **pid namespace** (largest blast radius; every pid consumer must become
   namespace-aware). Deliverable: pid 1 semantics + namespaced procfs.
3. **mount/ipc/uts hardening** targeted by observed Nix sandbox failures
   (mount propagation, remount-ro, pivot_root-in-userns), each with a
   guest-side regression test.
4. **net namespace (minimal)** to restore the default private-network
   sandbox; can be deferred behind a documented Nix configuration fallback.

Rationale: user+pid are the hard blockers — every sandboxed build hits them
in a single `clone` call, so there is no partial credit. mount/ipc/uts
already exist and only need fixes driven by real sandbox traces. net is
isolation-only from Nix's perspective and has a configuration escape hatch.

## Risks and open questions

- **Global pid table coupling**: `kill`, `wait4`, process groups, sessions,
  and procfs all read the global table today. Expect the pid namespace work
  to touch signal and wait paths; stage it behind incremental guest tests,
  not one big-bang commit.
- **Capability audit surface**: making `capable()` namespace-aware
  incorrectly (granting caps in the init userns) is a security regression,
  not just a bug. Every current capability check site must be re-audited.
- **setns for pid namespaces** has Linux-mandated deferred semantics (only
  affects children). Design the `PidNamespace` representation with that in
  mind from the start — retrofitting it is painful.
- **Fork/exit invariants**: namespace-init death must reap the namespace
  reliably; Asterinas's exit path currently assumes a global init.
- Open question: whether cgroup namespace root confinement interacts with
  the systemd-managed cgroup tree once builds run as unprivileged users —
  to be measured once user namespaces land.

## Verification strategy (for N2)

Each step lands with a guest-side test in the existing riscv64 headless
harness:

1. userns: unprivileged `unshare(CLONE_NEWUSER)` + uid_map round trip.
2. pid ns: nested `unshare(CLONE_NEWPID|CLONE_NEWUSER)`; child observes
   `getpid() == 1`; killing ns-init reaps the tree.
3. Nix end-to-end: a trivial derivation (`stdenv` `hello`) builds with
   `sandbox = true` in the NixOS guest.
