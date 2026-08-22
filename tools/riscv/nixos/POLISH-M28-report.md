# POLISH-M28 — INADDR_ANY bind fix verification: 2 PASS, 7 new failure modes, zero kernel bugs

Date: 2026-08-18
Branch: `track/nixos`
Commits: `8b28de0ef` (INADDR_ANY bind support) + `97d1bfb17` (ownership fix)
Status: **Complete — INADDR_ANY bind fix verified on 18-test network subset; 2 new PASS, 7 remaining FAILs reclassified to new root causes; zero new kernel bugs.**

M27 identified 12 network tests failing with `EADDRNOTAVAIL` because the TCP/UDP
stack could not bind to 0.0.0.0 (bucket A). Commits `8b28de0ef` and `97d1bfb17`
implement INADDR_ANY bind support. This report verifies the fix against a
18-test network subset gate at SMP=1.

---

## 1. Scorecard

### 1.1 Network subset (18 tests, SMP=1)

```
[summary] total=18 pass=8 fail=9 conf=1 crash=0 timeout=0
```

| Verdict | M27 (baseline) | M28 (after fix) | Δ |
|---|---|---|---|
| PASS | 6 | **8** | +2 |
| FAIL | 9 | **9** | 0 |
| CONF | 1 | **1** | 0 |
| CRASH | 3 | **0** | -3 (reclassified) |

**Net effect:** 2 tests moved from FAIL→PASS; 3 CRASH false positives eliminated;
7 FAILs remain but with *new* failure reasons (not EADDRNOTAVAIL). Zero regressions.

### 1.2 Per-test comparison

| Test | M27 | M28 | Δ | M28 failure reason |
|---|---|---|---|---|
| `accept01` | FAIL (EADDRNOTAVAIL) | **PASS** | ✅ | — |
| `bind01` | FAIL (EADDRNOTAVAIL) | **PASS** | ✅ | — |
| `bind03` | PASS | **PASS** | — | — |
| `bind04` | CONF | **CONF** | — | AF_INET6 not supported |
| `connect01` | FAIL/CRASH (EADDRNOTAVAIL) | **FAIL** | ⚠️ | ECONNREFUSED — connect to 0.0.0.0 unsupported |
| `getsockopt01` | FAIL (EADDRNOTAVAIL) | **FAIL** | ⚠️ | ENOPROTOOPT instead of EFAULT (NULL optval) |
| `listen01` | PASS | **PASS** | — | — |
| `recvfrom01` | FAIL/CRASH (EADDRNOTAVAIL) | **FAIL** | ⚠️ | ECONNREFUSED — connect to 0.0.0.0 unsupported |
| `recvmsg01` | FAIL (EADDRNOTAVAIL) | **FAIL** | ⚠️ | ECONNREFUSED — connect to 0.0.0.0 unsupported |
| `sendmsg01` | FAIL (EADDRNOTAVAIL) | **FAIL** | ⚠️ | `ip/ifconfig` missing in busybox |
| `sendto01` | FAIL/CRASH (EADDRNOTAVAIL) | **FAIL** | ⚠️ | ECONNREFUSED — connect to 0.0.0.0 unsupported |
| `setsockopt01` | FAIL (EADDRNOTAVAIL) | **FAIL** | ⚠️ | ENOPROTOOPT instead of EFAULT (NULL optval) |
| `setsockopt03` | PASS | **PASS** | — | — |
| `shutdown01` | PASS | **PASS** | — | — |
| `socket01` | FAIL (EPROTONOSUPPORT) | **FAIL** | — | EAFNOSUPPORT instead of EPROTONOSUPPORT |
| `socket02` | PASS | **PASS** | — | — |
| `socketpair01` | FAIL (EPROTONOSUPPORT) | **FAIL** | — | EAFNOSUPPORT instead of EPROTONOSUPPORT |
| `socketpair02` | PASS | **PASS** | — | — |

---

## 2. Classification of remaining 9 FAILs

### 2.1 Bucket α — connect to 0.0.0.0 → ECONNREFUSED (4 tests)

`connect01`, `recvfrom01`, `recvmsg01`, `sendto01`

The bind fix works: the server now successfully binds to INADDR_ANY:0 and gets
an ephemeral port via `getsockname`. However, the client then calls
`connect(0.0.0.0:<ephemeral_port>)` and the kernel rejects it with
`ECONNREFUSED`. The comment in `kernel/src/net/socket/ip/stream/connecting.rs:47-53`
explains: connecting to an unspecified address is explicitly unsupported and
returns ECONNREFUSED. On Linux, connecting to 0.0.0.0:port resolves to localhost.
This is a separate feature gap from the bind fix.

**Root cause:** `connecting.rs` treats any connect to 0.0.0.0 as an error.
**Not a regression** — the test was previously failing at the bind step
(EADDRNOTAVAIL); now it fails at the connect step (ECONNREFUSED).

### 2.2 Bucket β — getsockopt/setsockopt NULL optval → ENOPROTOOPT (2 tests)

`getsockopt01`, `setsockopt01`

When `optval` is NULL, the kernel returns `ENOPROTOOPT` instead of `EFAULT`.
The kernel checks the option level/name before validating the pointer. Linux
returns `EFAULT` for a NULL buffer.

```
getsockopt01.c:70: TFAIL: invalid option buffer expected EFAULT: ENOPROTOOPT (92)
setsockopt01.c:104: TFAIL: setsockopt() returned unexpected error: ENOPROTOOPT (92)
```

**Root cause:** pointer validation is deferred until after option-name lookup.
**Not a regression** — the test was previously failing at the bind step
(EADDRNOTAVAIL); now it proceeds to the actual getsockopt/setsockopt testing.

### 2.3 Bucket γ — missing ip/ifconfig (1 test)

`sendmsg01`

The test shells out to `ip` or `ifconfig` to bring up the loopback device.
The initramfs busybox does not include these applets.

```
sendmsg01    1  TBROK  :  sendmsg01.c:544: ip/ifconfig failed to bring up loop back device
```

**Root cause:** environment gap (busybox lacks `ip`/`ifconfig`).
**Not a regression** — the test was previously failing at the bind step.

### 2.4 Bucket δ — socket errno mismatch (2 tests, unchanged from M27)

`socket01`, `socketpair01`

The kernel returns `EAFNOSUPPORT` (97) when the test expects `EPROTONOSUPPORT` (93)
for unknown protocol/domain combinations. This is a pre-existing errno mismatch
documented in M27 bucket F.

---

## 3. CRASH false-positive elimination

M27 reported 5 CRASH items: `connect01`, `recv01`, `recvfrom01`, `send01`,
`sendto01`. All were false positives from the test runner misclassifying
`TBROK`+signal as a crash. Of the 3 in the subset, all are now correctly
classified as FAIL (not CRASH) because the tests now fail normally with
`TBROK` (ECONNREFUSED) instead of the test framework killing the child on
EADDRNOTAVAIL.

---

## 4. Build fix

The `cargo-osdk` symlink was pointing at a binary built in the sibling
`asterinas-riscv` repo, causing a lockfile collision. Fixed by repointing
to `cargo-osdk.nixos-bak` (see `osdk-binary-bakes-repo-path` memory).

Commit `8b28de0ef` also had an ownership bug: `ListenStream::new` takes
`BoundTcpPort` by value, but the code passed `&BoundTcpPort` via `as_ref()`.
Fixed in `97d1bfb17` by changing `listen()` to `mut self` and using `take()`.

---

## 5. Conclusion

| Metric | Value |
|---|---|
| **Tests verified** | 18 (network subset) |
| **New PASS** | 2 (`accept01`, `bind01`) |
| **CRASH→FAIL reclassification** | 3 (runner artifact, not kernel crash) |
| **New kernel bugs** | **0** |
| **Regressions** | **0** |
| **Remaining feature gaps** | connect-to-0.0.0.0 (4 tests), errno mismatches (4 tests), missing ip/ifconfig (1 test) |

**Next steps:**
1. Run the full 767-test gate to verify the full M27→M28 diff (especially the 3
   bucket-A tests not in the subset: `epoll_wait05`, `recv01`, `send01`)
2. Fix `getsockopt`/`setsockopt` NULL-pointer-before-option-lookup ordering
3. Fix `socket`/`socketpair` EAFNOSUPPORT→EPROTONOSUPPORT errno
4. Support connect to 0.0.0.0 (resolve to localhost)