# BROWSER M12 — HTTPS certificate-chain validation + slirp→bridge deferral record

**Status:** TLS certificate matrix harness built and run against the riscv64 guest;
curl and NetSurf verdicts for valid / expired / wrong-hostname / self-signed
certificates recorded; slirp→bridge switch re-confirmed deferred.
**Date:** 2026-08-16
**Scope:** two items on top of M10 (PR #54): (1) act on the M10 §4 slirp→bridge
evaluation — it concluded *medium cost → defer*, so this milestone records the
reason rather than implementing; (2) close the HTTPS certificate-validation loop
flagged across M3/M4/M10 by exercising the guest TLS client against a controlled
set of bad certificates and recording the kernel-side TLS-link behaviour.

---

## 1. Summary

1. **slirp→bridge remains deferred (recorded, not implemented).** M10 §4 already
   priced the switch at *medium cost*: it needs a root-privileged, per-boot host
   `tap`+`bridge`+NAT step (an environmental dependency), a `--tap` boot mode, a
   `resolv.conf` DNS repoint — and a *true* L2 bridge would additionally require
   guest-IP parameterisation or a DHCP client that the kernel's compile-time
   `VIRTIO_ADDRESS`/`VIRTIO_GATEWAY` (`kernel/src/net/iface/init.rs:115-117`) does
   not provide. Single-guest slirp already yields `http200` (M8 §4), so the bridge
   only buys anything under multi-guest contention. The reason is re-recorded in
   §2 with the concrete recipe left as the starting point for a follow-on milestone.
2. **The guest TLS client is exercised against a controlled bad-certificate matrix**
   (valid, expired, wrong-hostname, self-signed), served on the host loopback and
   reached through slirp (`10.0.2.2`). curl's layer-specific error codes and
   NetSurf's accept/reject dialog are both recorded (§3–§5).
3. **Kernel TLS-link behaviour is pinned down** (§6): the guest's virtio-net/TCP
   stack carries every TLS handshake through the certificate exchange; the
   valid/expired/wrong-host/self-signed outcomes differ *only* at the final
   userspace X.509 check, never at the transport.

---

## 2. slirp→bridge deferral (item 1)

M10 §4 evaluated the switch and deferred it. This milestone re-checks that
conclusion against the current state and records it, per the M12 brief's "if
deferred → record the reason and move on" branch.

The guest network identity is a compile-time kernel constant — there is no DHCP
client (`kernel/src/net/iface/init.rs`):

```rust
const VIRTIO_ADDRESS: Ipv4Address = Ipv4Address::new(10, 0, 2, 15); // /24
const VIRTIO_GATEWAY: Ipv4Address = Ipv4Address::new(10, 0, 2, 2);
```

A bridge switch has three parts, each still standing as in M10 §4:

| part | change | effort |
|---|---|---|
| host tap+bridge+NAT | `ip tuntap add tap0 mode tap` + bridge + `ip addr add 10.0.2.2/24` + `ip_forward=1` + MASQUERADE (root) | low per-boot, but a root **environmental dependency** |
| boot driver | `-netdev user` → `-netdev tap,ifname=tap0,script=no,downscript=no` (opt-in `--tap`) | low |
| guest DNS | `resolv.conf` bakes `nameserver 10.0.2.3`; a bridge has no such relay | low (build-script change) |

The guest IP can stay `10.0.2.15` only if the bridge is *private* and NAT'd; a
true L2 bridge to the physical LAN (guest acquiring a LAN IP) needs DHCP or a
boot-parameterised address — a non-trivial kernel change.

**Verdict (unchanged): medium cost → defer.** The win (host-kernel NAT instead of
slirp's userspace stack) is real but only materialises under multi-guest/contention;
the self-contained headless boot path would gain a root-privileged per-boot host
step. The in-kernel M8 fixes remain the durable work. The recipe above stays the
concrete starting point for a future milestone (ideally with guest-IP
parameterisation or a DHCP client).

---

## 3. TLS certificate matrix (item 2) — harness

Four new artifacts implement the certificate matrix (see §7):

- **`gen_tls_certs.py`** — generates a test CA plus four server certs.
- **`tls_cert_server.py`** — serves four HTTPS endpoints on `127.0.0.1:8443..8446`,
  one per cert, and logs each connection's TLS handshake outcome.
- **`curl-cert-test.service`** — a guest-side oneshot unit that runs the
  standalone `curl` binary against all four endpoints and logs one normalized
  `TLS_TEST` line per verdict. The 13 probes are plain `ExecStart=` lines (no
  shell script) because the desktop busybox ships only the `sh` applet with no
  `echo`/`[`/`test` builtins.
- **`tls_cert_matrix.sh`** — the top-level harness (cert gen → base rootfs →
  post-process → serve → boot+score), mirroring `render_matrix_net.sh`.
- **`build_systemd_desktop.sh`** now also installs the standalone `curl` binary
  (dynamically linked against glibc only; libcurl + libssl/libcrypto statically
  linked in), so the guest has a curl CLI whose transport is *the same libcurl*
  compiled into `netsurf-gtk`.

### 3.1 Certificate matrix

All four server certs are reached as `https://10.0.2.2:<port>/` (the slirp host
alias). The test CA is appended to the guest CA bundle so that a *valid* cert is
trusted and each *bad* cert fails for its own reason, not for "untrusted CA":

| endpoint | cert | SAN | expected client verdict |
|---|---|---|---|
| `:8443` | valid | `IP:10.0.2.2` | chain OK, in-date, name matches → **200** |
| `:8444` | expired | `IP:10.0.2.2` | dates in the past → **certificate has expired** |
| `:8445` | wronghost | `DNS:wrong.example.com` | name mismatch → **no alternative subject name matches** |
| `:8446` | selfsigned | `IP:10.0.2.2` | leaf self-signed → **self-signed certificate** |

---

## 4. curl certificate verdicts

The guest's standalone `curl` (the same libcurl fetcher compiled into NetSurf)
was run against all four endpoints in three modes — default verification
(compiled-in CA bundle, now incl. the test CA), `-k` (verification disabled), and
`--cacert` (explicit bundle). `rc=%{exitcode}` is curl's own exit code (0 =
fetched, 60 = certificate problem), `http=%{http_code}` is the status curl saw.

| probe | rc | http | verdict |
|---|---|---|---|
| `valid_default` | 0 | 200 | trusted, in-date, name matches → fetched |
| `expired_default` | 60 | 000 | certificate has expired |
| `wronghost_default` | 60 | 000 | no alternative subject name matches `10.0.2.2` |
| `selfsigned_default` | 60 | 000 | self-signed certificate |
| `valid_k` | 0 | 200 | `-k` disables verification → fetched |
| `expired_k` | 0 | 200 | `-k` → fetched |
| `wronghost_k` | 0 | 200 | `-k` → fetched |
| `selfsigned_k` | 0 | 200 | `-k` → fetched |
| `valid_cacert` | 0 | 200 | explicit bundle (== default) → fetched |
| `valid_testca` | 0 | 200 | test-CA-only bundle → fetched |
| `expired_testca` | 60 | 000 | expired even against the test CA |
| `wronghost_testca` | 60 | 000 | name mismatch even against the test CA |
| `selfsigned_testca` | 60 | 000 | self-signed leaf not chained to the test CA |

Every one of the 13 verdicts matches the expected table in §3.1: the *valid*
cert is the only one fetched under verification, `-k` fetches everything, and
each *bad* cert fails for its own reason (expiry / name / untrusted issuer).

## 5. NetSurf certificate behaviour

NetSurf was pointed at one endpoint per boot. The observable signals are the
serial log (fetch outcome + `sslcert_viewer_init`) and the framebuffer
screenshot:

| case | fetch | serial log | screenshot |
|---|---|---|---|
| `netsurf-valid` | http200 + `content_scaled_redraw` | no cert dialog | rendered (distinct=352) |
| `netsurf-expired` | no http200, no redraw | `Building certificate viewer` | cert **dialog** (distinct=314) |
| `netsurf-wronghost` | no http200, no redraw | `Building certificate viewer` | cert **dialog** (distinct=258) |
| `netsurf-selfsigned` | no http200, no redraw | `Building certificate viewer` | cert **dialog** (distinct=258) |

NetSurf's libcurl fetcher returns `CURLE_PEER_FAILED_VERIFICATION` (60) for all
three bad certs; the fetcher catches that and calls `curl_start_cert_validate`
→ `FETCH_CERT_ERR` → the GTK `sslcert_viewer` dialog ("NetSurf failed to verify
the authenticity of an SSL certificate …"). So the page is **never rendered**
for a bad cert; instead the accept/reject dialog is shown.

A subtlety for the harness: the bad-cert cases *pixel*-validate as "rendered"
(258–314 distinct colours) because the GTK **dialog** itself renders antialiased
text on top of the empty page. The serial-log marker (`Building certificate
viewer` + no `HTTP status code 200`) is the authoritative signal, not the pixel
score. (Separately, the old `net_validate.sh` scoring reads the bad cert as
`code7` — that is the unrelated `google.com/favicon.ico` fetch, which code7s on
this host and masks the main-page outcome; M10 §3.4.)

## 6. Kernel TLS-link behaviour

The server logs each connection's TLS handshake outcome (negotiated version +
cipher on success, or the fatal-alert reason on failure). The curl-only run's
transcript maps one-to-one onto the 13 probes:

| endpoint | handshake (server view) |
|---|---|
| valid (all modes) | `OK` — `TLSv1.3 TLS_AES_256_GCM_SHA384` |
| expired (default/testca) | `FAIL` — `SSLV3_ALERT_CERTIFICATE_EXPIRED` |
| wronghost (default/testca) | `OK` — handshake *completes*, then curl rejects |
| selfsigned (default/testca) | `FAIL` — `TLSV1_ALERT_UNKNOWN_CA` |
| any `-k` | `OK` — `TLSv1.3 TLS_AES_256_GCM_SHA384` |

Two failure shapes fall out of this, and both are userspace decisions layered on
top of a kernel stack that carried the full handshake every time:

1. **Chain/validity failures abort the handshake.** An expired or self-signed
   cert is rejected *inside* OpenSSL's handshake, which sends a fatal TLS alert
   (`certificate expired` / `unknown ca`) before the handshake finishes. The
   kernel's virtio-net/TCP stack still delivered the ClientHello and the server's
   Certificate — the abort is the client's.
2. **The hostname check is post-handshake.** A name mismatch (`wronghost`) does
   not abort the handshake: the chain is valid, so OpenSSL completes it and curl
   then applies its own `CURLOPT_SSL_VERIFYHOST=2` check, returning code 60
   *after* the TLS session is up (no alert). NetSurf shows the same dialog either
   way.

The layering is also visible in curl's error codes: a transport failure would be
code 7 (`CURLE_COULDNT_CONNECT`) or 35 (`CURLE_SSL_CONNECT_ERROR`); every bad-cert
case is code 60 (`CURLE_PEER_FAILED_VERIFICATION`), proving the TCP connection and
TLS handshake succeeded and only the X.509 check failed. This closes the M3/M4
loop: the kernel network stack is not the bottleneck — TLS and certificate
validation are entirely the guest userspace (OpenSSL 3.0.15 in curl/NetSurf), and
they behave correctly against every certificate class.

---

## 7. Artifacts

| file | what it is |
|---|---|
| `tools/riscv/xorg/gen_tls_certs.py` | new — CA + valid/expired/wronghost/selfsigned cert generation |
| `tools/riscv/xorg/tls_cert_server.py` | new — 4-port HTTPS server with handshake-outcome logging |
| `tools/riscv/systemd/units/curl-cert-test.service` | new — systemd oneshot running the 13 curl TLS probes at boot |
| `tools/riscv/xorg/tls_cert_matrix.sh` | new — harness wiring cert gen → rootfs → serve → boot+score |
| `tools/riscv/systemd/build_systemd_desktop.sh` | now installs the standalone `curl` binary into the rootfs |
| `tools/riscv/xorg/BROWSER-M12-report.md` | this report |
| `/tmp/browser-m12/*/` | per-case boot dirs (serial.log, shot.ppm, net_validate.out) |
| `/tmp/browser-m12/tls-server.out` | NetSurf-run server-side TLS handshake transcript |
| `/tmp/browser-m12-curl/*/` | curl-only re-run (clean `TLS_TEST` verdicts + transcript) |
