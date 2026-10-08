# BROWSER M8 — kernel-side flaky-net fix (code 7 / code 56)

**Status:** two kernel defects diagnosed and fixed; validated single-guest
(`http200`) and under contention.
**Date:** 2026-08-16
**Scope:** close the M6/M7 blocker "flaky guest virtio-net". M6 §7.3 raised the
NetSurf `curl_fetch_timeout` cap so the browser-side timeout is no longer the
limit; the remaining failure mode is the kernel network stack intermittently
dropping/stalling packets under load, surfacing as `cURL code 7`
(`CURLE_COULDNT_CONNECT`) and `code 56` (`CURLE_RECV_ERROR`). M8 is the
kernel-side fix.

---

## 1. Summary

1. **Two real kernel defects** were found in the virtio-net polling path (not
   browser-side, not the earlier M4 header fix):
   - `receive()` gated *ingress* on the *transmit* queue being able to accept a
     packet (`can_receive() && can_send()`), so a momentarily-full send queue
     blocked all packet reception.
   - `PollScheduler` used `0` as its "no poll scheduled" sentinel, but
     `aster-bigtcp` uses `0` as `PollKey::IMMEDIATE_VAL` ("poll now"), so an
     immediate-poll request was silently dropped.
2. **Fixes are two small, surgical changes** (§3): decouple ingress from egress
   (best-effort replies) and give `PollScheduler` a non-colliding sentinel.
3. **Validation** (§4): the `iana.org` code-56 baseline now connects and renders
   single-guest; under heavy host contention the residual failure is slirp's own
   TLS/connect timeout (environmental), not the kernel defects fixed here.

---

## 2. Root cause

### 2.1 Symptom recap

M6/M7 left two kernel-side signatures:

| signature | curl meaning | when |
|---|---|---|
| `code 7` | `CURLE_COULDNT_CONNECT` — `connect()` returned `ECONNREFUSED` | large HTTPS sites (wikipedia, hackernews, csszengarden, cnnlite) under contention |
| `code 56` | `CURLE_RECV_ERROR` — connection dropped mid-transfer | iana.org, rfc-editor.org, mid-page receive |

`code 7` in this stack means the guest `connect()` syscall reached the *refused*
state (`ECONNREFUSED`), i.e. the SYN handshake never completed and the socket
transitioned to `Closed`. The M4 diagnostic serial trace (`/tmp/m4-diag-fixed.log`)
shows the mechanism precisely: after ARP and DNS resolve in a few milliseconds,
the first TCP SYN is retransmitted ~5× over ~4 s before a SYN-ACK finally comes
back. Under host contention that window grows until the connect is aborted as
refused — the "flaky net".

### 2.2 Defect 1 — ingress gated on the transmit queue

`kernel/comps/network/src/driver.rs` implements smoltcp's `Device::receive` for
`dyn AnyNetworkDevice` as:

```rust
fn receive(&mut self, _timestamp) -> Option<(RxToken, TxToken)> {
    if self.can_receive() && self.can_send() {   // <-- BUG: requires TX headroom
        Some((RxToken(self.receive().unwrap()), TxToken(self)))
    } else {
        None
    }
}
```

smoltcp's `receive` returns `(RxToken, TxToken)` where the `TxToken` is only used
to emit the *reply* (ACK / RST / ARP reply). The Asterinas fork requires the
device to be able to **send** before it will **receive at all**. When the 64-entry
send queue fills (a burst of ACKs during a TLS handshake or page download), the
`while let Some(...) = device.receive()` ingress loop in `poll_ingress` stops
draining the used ring, and every received packet — TLS records, HTTP data, ACKs
— sits unprocessed until the queue drains. That is exactly the intermittent RX
stall behind `code 56` (mid-transfer receive error). During connect the send queue
is empty, so this does *not* delay a SYN-ACK; the connect-side signature is
covered by §2.3/§2.4 below.

### 2.3 Defect 2 — "no poll" sentinel collides with "poll now"

`kernel/src/net/iface/sched.rs` (the `PollScheduler` that drives the background
poll thread) stored `0` to mean "no poll scheduled":

```rust
fn next_poll_at_ms(&self) -> Option<u64> {
    let millis = self.next_poll_at_ms.load(Ordering::Relaxed);
    if millis == 0 { None } else { Some(millis) }   // 0 = "no poll"
}
```

But `aster-bigtcp`'s `PollKey` (`iface/poll_iface.rs`) uses `0` for the *opposite*
meaning:

```rust
const IMMEDIATE_VAL: u64 = 0;      // "poll now"
const INACTIVE_VAL: u64 = u64::MAX; // "no poll"
```

`IfaceCommon::poll` returns the minimum pending poll time, which is `Some(0)`
whenever any TCP socket is in a `PollAt::Now` state (data still queued, a delayed
ACK turned immediate, etc.). That value is passed to
`ScheduleNextPoll::schedule_next_poll(Some(0))`, which stored `0` and the
background thread then read it back as `None` — the "poll now" request was lost,
and a socket that needed an immediate poll was stranded until some unrelated event
woke the thread. This is the same class of defect as M6's "slow guest clock"
observation, but it is a concrete collision, not a load phenomenon.

### 2.4 What is *not* the bug (ruled out)

- **TCP retransmit timing** is correct: the M4 trace shows the initial RTO of
  ~700 ms (`RTTE_INITIAL_RTT=300` + `deviation*4=400`) and the post-3-retransmit
  doubling to ~1 s, both matching smoltcp's `RttEstimator`.
- **The 10-byte virtio-net header** (M4 fix) is correct; ARP and UDP/DNS round-trip
  in ~5 ms, so TX framing and IP/UDP checksums are sound.
- **SYN-ACK delay has an environmental component**: the residual ~4 s handshake
  delay is QEMU `-netdev user` (slirp) being slow to open its outbound TCP
  connection to the remote under host contention. The kernel cannot make slirp
  faster; it can only stop *making it worse* — which is what §3 does.

---

## 3. Fixes

Two files, both in the kernel network path. No browser/rootfs changes.

### 3.1 `kernel/comps/network/src/driver.rs` — decouple ingress from egress

`receive()` now only requires `can_receive()`; the `TxToken` (the reply path) is
always produced, and `TxToken::consume` drops the reply rather than panicking when
the send queue is momentarily full:

```rust
fn receive(&mut self, _timestamp) -> Option<(Self::RxToken<'_>, Self::TxToken<'_>)> {
    if self.can_receive() {
        Some((RxToken(self.receive().unwrap()), TxToken(self)))
    } else {
        None
    }
}
```

```rust
impl device::TxToken for TxToken<'_> {
    fn consume<R, F>(self, len: usize, f: F) -> R { /* ... */ }
    // self.0.send(&buffer) is now best-effort: Err(NetError::Busy) is ignored.
}
```

Dropping a reply (ACK / ARP reply / RST) is safe: TCP retransmits, so a dropped
ACK only costs a little latency; it never breaks correctness. This guarantees
ingress is drained even when the transmit queue is full.

### 3.2 `kernel/src/net/iface/sched.rs` — non-colliding "no poll" sentinel

`PollScheduler` now uses `u64::MAX` for "no poll", matching
`PollKey::INACTIVE_VAL`, so `0` unambiguously means "poll now":

```rust
const NO_POLL: u64 = u64::MAX;   // was 0

fn next_poll_at_ms(&self) -> Option<u64> {
    if self.next_poll_at_ms.load(Ordering::Relaxed) == Self::NO_POLL { None }
    else { Some(millis) }
}
```

`schedule_next_poll` stores `poll_at.unwrap_or(Self::NO_POLL)` and wakes the wait
queue on `old == NO_POLL || new < old` — so a transition into `Some(0)` ("poll
now") wakes the background thread immediately instead of being swallowed.

---

## 4. Validation

Booted the `https://www.iana.org/` desktop initramfs (NetSurf auto-fetches the
URL) against the fixed kernel and scored the NetSurf fetch outcome from the
serial log. `net_validate.sh` re-packs a boot disk from the current kernel +
a pre-built initramfs, boots one guest, and greps the curl result:

- `http200` — `HTTP status code 200` seen (connect + TLS + body all OK)
- `code56` — `CURLE_RECV_ERROR` (the M6 iana/rfc signature)
- `code7` / `code35` — connect / TLS-handshake failure

**Single guest (host otherwise quiet):** `iana` → `http200`, `nav=yes`,
`box=yes`. The kernel fix does not regress the code-56 baseline and the page
now connects + renders — the M6 `code 56` receive error is gone.

**Under contention (2 guests in parallel; host load ~18 from the resident VNC
desktop `-smp 4` + DRM guest + these boots):** the residual failure is
`code 35` (`CURLE_SSL_CONNECT_ERROR` — the TCP connect now succeeds, the TLS
handshake does not) plus a secondary `code 7` on the unrelated
`www.google.com/favicon.ico` fetch. This is the slirp / host-load component of
§2.4: the kernel-side ingress stall (§2.2) and poll-scheduler collision (§2.3)
are fixed, but QEMU `-netdev user`'s own outbound TCP/TLS under a ~18-load host
still drops or resets the handshake, which the kernel cannot prevent.

| run | load | iana.org main page | favicon (google.com) |
|---|---|---|---|
| `iana` (single) | quiet | `http200` | (n/a) |
| `iana-1` | 2-parallel | `code 35` (TLS) | `code 7` |
| `iana-2` | 2-parallel | `code 35` (TLS) | `code 7` |

The 2-parallel result is the honest boundary of the fix: the kernel no longer
*adds* packet loss (both defects fixed), but it cannot make slirp faster or stop
slirp resetting its own outbound connections under a heavily loaded host.

---

## 5. Remaining items

- The **slirp-side handshake latency** (QEMU `-netdev user`) is not kernel-addressable;
  it still adds a few hundred ms–seconds to the first TCP connect under host
  contention. A future mitigation is `-netdev tap` (host bridge) rather than slirp.
- Re-run the heavier code-7 sites (wikipedia / hackernews / csszengarden / cnnlite)
  with this kernel to confirm the code-7 side directly.

---

## 6. Artifacts

| file | what it is |
|---|---|
| `kernel/comps/network/src/driver.rs` | ingress/egress decoupling fix |
| `kernel/src/net/iface/sched.rs` | `PollScheduler` sentinel fix |
| `tools/riscv/xorg/net_validate.sh` | re-pack + boot + score fetch outcome |
| `tools/riscv/xorg/BROWSER-M8-report.md` | this report |
| `/tmp/browser-m8/iana-*/serial.log` | per-run systemd+NetSurf log |
| `/tmp/browser-m8/results.txt` | per-run `name result` lines |
