# NIXOS-N4 — UDP/DNS fix + substituter completion + profile/GC smoke

Date: 2026-08-23
Branch: `track/nixos`
Commits:
- `af9f64242` fix(net): queue egress packets during ARP resolution instead of dropping them
- (N3 gate extension + this report)
Status: **Complete — UDP/DNS fixed at the root; real substitution from cache.nixos.org verified end-to-end; profile generations/rollback/GC verified. All 11 gate checks green.**

---

## 1. UDP inbound failure — root cause and fix

**Symptom (from N3):** a guest UDP `sendto` to the slirp DNS resolver
(10.0.2.3:53) left fine (33 bytes sent), but the reply never arrived
(`recv` timed out with EAGAIN). TCP worked in both directions.

**Diagnosis.** The receive path (virtio IRQ → iface poll → UDP socket
dispatch) was never broken. The failure was on **egress**: in
`kernel/libs/aster-bigtcp/src/iface/phy/ether.rs`, when the next-hop MAC was
not yet in the ARP table, `resolve_ether_or_generate_arp` **dropped the
packet** and emitted an ARP request instead, with a comment telling the upper
layer to retransmit. TCP survives this (retransmission timer), but a one-shot
UDP exchange — a DNS query — loses its only packet and times out.

**Proof before the fix:** inserting a sacrificial datagram (ARP warm-up)
before the DNS query made the query succeed immediately
(`recv=96, rcode=0, ancount=2`).

**Fix (`af9f64242`).** On an ARP miss the IPv4 packet is now serialized
(checksums included) into a bounded per-interface pending queue
(`MAX_PENDING_TX = 64`) next to the ARP request; the queue is flushed at the
end of the next interface poll, after the ingress phase has processed the
ARP reply. Unroutable packets are dropped; a still-unresolved front packet
re-emits the ARP request and waits.

**Verification (no warm-up, no /etc/hosts hack):**

```
__NETPROBE__ dns sendto=33 errno=0
__NETPROBE__ dns recv=96 errno=0 (ok)        # first attempt, cold ARP
__NETPROBE__ dns answer rcode=0 ancount=2    # real cache.nixos.org A records
```

## 2. Real substitution from cache.nixos.org (N3-5 complete)

With DNS working, `nix copy --from https://cache.nixos.org --to daemon` of a
path that is **not** in the local store (editline from the nix 2.28.5 riscv64
closure; narinfo confirmed 200 host-side) downloads its whole little closure:

```
copying 3 paths...
copying path '...-libgcc-riscv64-unknown-linux-gnu-14.3.0' from 'https://cache.nixos.org'...
copying path '...-glibc-riscv64-unknown-linux-gnu-2.40-66' from 'https://cache.nixos.org'...
copying path '...-editline-riscv64-unknown-linux-gnu-1.17.1' from 'https://cache.nixos.org'...
__N3_SUBST_RC__=0
libeditline.so.1.0.2  libeditline.so  libeditline.so.1  libeditline.la
__N3_SUBST_RESTORED__
```

Notes:

- `nix-store --realise <path>` did **not** attempt substitution for an
  unknown path ("don't know how to build these paths"); `nix copy --from`
  is the reliable driver.
- Deleting a path from the running daemon's own closure is impossible by
  design: the daemon's `/proc/<pid>/maps` entries are GC roots, so every
  mapped library is alive. (Verified via `nix-store --gc --print-roots`,
  which also confirms our procfs exposes `/proc/<pid>/{environ,exe,maps}`
  correctly enough for nix's root scanner.)
- `nix.conf`: `substituters = https://cache.nixos.org`, `require-sigs = false`
  (no trusted keys provisioned in the guest).

## 3. Profile generations / rollback / GC smoke (N4 task 3)

All working:

- `nix profile add` via the daemon creates generation links
  (`default-1-link`, `default-2-link`).
- `nix profile rollback` switches back (`switching profile from version 2
  to 1`, rc=0).
- `nix store gc` collects unrooted paths (`n3-hello` build output + drv +
  the unreferenced `nss-cacert`: `3 store paths deleted, 0.72 MiB freed`).

(An intermediate run appeared to show the second `nix profile add` not
creating a generation; that turned out to be a bug in this session's own
test script — an over-eager block edit had silently dropped the install
section — not a nix or kernel issue.)

## 4. Final gate state (11/11)

```
nix-version, nix-version-exit, daemon-ping, profile-install,
profile-binary-runs, drv-build, drv-output,
dns-single-shot, https-subst-ping, substitution, gc
```

## 5. Open items

- `CLONE_NEWNET` for the default build sandbox (B-track; N3-4).
- IPv6 egress over Ethernet is still dropped (no neighbor discovery) —
  unchanged, noted in ether.rs.
- The pending-tx queue is per-interface and unbounded per next-hop; a hostile
  peer pattern could fill it (bounded at 64 packets total, then drops).
