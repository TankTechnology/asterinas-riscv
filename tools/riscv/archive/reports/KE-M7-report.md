# KE-M7 Report — Second track/nixos → main Convergence Merge

Track B (kernel evolution), card KE-M7. Merge commit on `main`:

- `d27d12826` Merge remote-tracking branch 'origin/track/nixos'
- `de670b42f` test(riscv): drain NLMSG_DONE in netns test lo_index helper

Merge base: `d4a2f54e7` (KE-M4). Brought in from `origin/track/nixos`:

- N1 netlink completion (`ed41a4875`, `9ca81532e`): netlink socket
  options, RTM_SETLINK / RTM_NEWADDR, pktinfo control message, classic BPF
  socket filters, `seccomp` refactor into `util/bpf.rs`.
- N3/N4 Nix guest gates and fixes (`af9f64242` ARP-miss egress queueing,
  UDP/DNS fixes, substituter plumbing, gate assets).
- NIXOS-N1/N3/N4 reports and preflight notes.

## Conflict list and resolution strategy

Exactly one textual conflict:

| File | Both sides changed | Resolution |
|------|--------------------|------------|
| `kernel/src/net/socket/netlink/route/message/segment/link.rs` | KE-M6 added `change: InterfaceFlags` (with doc comment) to `LinkSegmentBody`; N1 added the same field | Kept the field once, with the doc comment |

Two semantic overlaps found and unified after the merge (included in the
merge commit):

1. **Duplicate `NEWLINK` parse arm** in `message/segment/mod.rs`: both
   sides added `RTM_NEWLINK` parsing (KE-M6 for flag changes, N1 for
   setlink); the auto-merge kept both arms, leaving an unreachable
   duplicate — removed.
2. **N1 `do_set_link` vs KE-M6 `do_new_link`**: N1's handler was written
   when interfaces were read-only and only accepted no-op flag changes
   (returning EOPNOTSUPP otherwise); KE-M6's handler actually applies flag
   changes via `set_flags` and is network-namespace-filtered. `do_set_link`
   now delegates to `do_new_link`, so `RTM_SETLINK` and `RTM_NEWLINK` share
   one ns-aware implementation.
3. **N1 `do_new_addr`** looked up interfaces in the global table; it is now
   scoped to the caller's network namespace like the other handlers.

## Verification on the merged kernel

- Build: `make kernel TARGET_ARCH=riscv64 FEATURES=riscv_sv39_mode` passes
  (only pre-existing warnings).
- KE-M6 netns guest test initially FAILED at "lo is up after RTM_NEWLINK";
  investigation showed the kernel is correct (the dump reports UP) — N1's
  completion handling now emits a proper `NLMSG_DONE` after dumps, and the
  test's `lo_index` helper returned without draining it, corrupting the
  next request/response pair. Test-side fix in `de670b42f`;
  `NETNS_TEST_PASS` afterwards.
- Smoke tests on the merged kernel: `PIDNS_TEST_PASS`,
  `SETNS_TEST_PASS`, `LO_INIT_NS_PASS`, `NETNS_TEST_PASS`.
- N1's own guest gate (`tools/riscv/nixos/n1/`): 9/9 checks OK
  (uevent socket, RTM_GETLINK/GETADDR/NEWADDR, probe, busybox `ip link`
  incl. eth0, `ip addr` incl. lo) — the N1 additions coexist with the
  KE-M6 namespace filtering.
- Xfce desktop chain: boots to `graphical.target`, all session milestones
  OK, no kernel panic.
- LTP subsets (SMP=1): process (78): `pass=68 fail=4 conf=6` and network
  (44): `pass=30 fail=14 conf=0 timeout=1` — both verdict sets are
  byte-identical to the pre-merge KE-M6 runs (only documented known
  failures). Verdict logs: `tools/riscv/pidns/ltp-kem7-proc-verdicts.log`
  and `ltp-kem7-net-verdicts.log`.

## Resulting capability set on main

Full Linux namespace matrix for the Nix sandbox: user, pid, mount, ipc,
uts, cgroup (all with clone/unshare + setns), and loopback-only network
namespaces — plus the A-track netlink/Nix user-space progress (netlink
route dumps and mutations, BPF filters, ARP-miss egress queueing, N3/N4
Nix guest gates).
