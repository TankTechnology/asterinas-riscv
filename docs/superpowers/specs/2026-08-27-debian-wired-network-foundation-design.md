# Debian Browser-Ready Wired Network Design

Date: 2026-08-27

## Goal

Provide enough wired IPv4 networking for the Debian desktop on Asterinas to
browse ordinary websites on both QEMU and the Milk-V Megrez. The accepted
path must resolve public domain names, validate normal HTTPS certificates,
download page assets, and let NetSurf open an external site.

This is deliberately not a complete Linux network-management milestone. DHCP,
dynamic rtnetlink address and route changes, cable replug recovery, live GMAC
failover, NetworkManager, Wi-Fi, and IPv6 are deferred. The existing static
boot profile is sufficient for the first useful desktop experience.

## Current state

Most of the required data path already exists:

- QEMU VirtIO-Net uses the fixed `10.0.2.15/24` address,
  `10.0.2.2` gateway, and slirp DNS at `10.0.2.3`;
- the QEMU M5 gate already proves DNS and certificate-validated HTTPS to
  `www.baidu.com` through Asterinas;
- the QEMU M6 gate foregrounds a Baidu-hosted PNG in NetSurf and separately
  classifies the packaged browser's limited JavaScript engine;
- Megrez selects a linked on-board GMAC and can exchange packets with the
  development host;
- the physical boot gate supplies the static board profile
  `10.100.19.200/21` with gateway `10.100.16.1`.

The remaining practical gap is that the Debian M5 evidence service assumes
the QEMU topology. It always rewrites `/etc/resolv.conf` to `10.0.2.3` and
requires the local address to be exactly `10.0.2.15`. That makes the same
signed desktop profile fail on Megrez even though the kernel has already
installed the board address and default route.

## Considered approaches

### Reuse strict static profiles (selected)

Keep the existing kernel configuration and make the Debian evidence path
recognize exactly two reviewed environments: QEMU slirp and the current
Megrez LAN. Each environment has a fixed local address, gateway, and resolver
contract. This is the smallest path to useful browsing and preserves the
already verified QEMU and physical drivers.

### Add partial userspace address and route mutation

Implementing `RTM_NEWADDR`, `RTM_NEWROUTE`, and DHCP would make Debian more
general, but it expands the work into netlink parsing, mutable smoltcp state,
lease handling, notifications, and regressions. It is useful future kernel
work but not required for browser access on the current two environments.

### Add a kernel DHCP client

A kernel DHCP client could avoid some rtnetlink work, but it would put lease
and DNS policy in the wrong layer and still would not make ordinary Debian
network tools work. It is rejected.

## Configuration contract

The QEMU path remains unchanged:

| Field | QEMU value |
|---|---|
| device key | `Virtio-Net` |
| interface | `eth0` |
| local address | `10.0.2.15/24` |
| gateway | `10.0.2.2` |
| resolver | `10.0.2.3` |

The physical path remains explicit and bounded:

| Field | Megrez value |
|---|---|
| device key | `eic7700-rj45` |
| interface | the enumerated selected GMAC interface |
| local address | `10.100.19.200/21` |
| gateway | `10.100.16.1` |
| primary resolver | `10.2.0.5` |
| fallback resolver | `10.2.0.6` |

The host gate keeps its existing duplicate-address probe before assigning
`10.100.19.200`. No persistent U-Boot environment, host network setting, or
rootfs profile schema, label, or package set is changed. Rebuilding the
modified evidence script necessarily produces a new signed image and manifest
content hash; the gate must use that new frozen identity.

The two Megrez resolver addresses are the current DNS servers learned by the
development host on the same wired LAN. A directed query to `10.2.0.5` for
`www.baidu.com` and traffic from the host Ethernet interface to that resolver
were verified while writing this design. They are environment inputs rather
than general Asterinas defaults.

The guest evidence script determines the environment from the observed
interface address after the kernel has configured it. It accepts only the two
rows above. For Megrez it also requires the exact static profile in
`/proc/cmdline`; the host gate owns that boot argument and rejects any drift.
An unknown address, duplicate accepted interface, or mismatched Megrez boot
profile fails before DNS or browser evidence is emitted. This avoids silently
treating an arbitrary LAN as a reviewed setup.

## Guest data flow

The successful path is:

1. Asterinas registers VirtIO-Net or the selected Megrez GMAC.
2. The existing kernel boot profile installs the local IPv4 CIDR and default
   route.
3. The Debian evidence service reads the address through the existing
   `RTM_GETADDR` support and, on Megrez, validates the exact static profile in
   `/proc/cmdline`. It does not claim `RTM_GETROUTE`, which Asterinas does not
   currently implement.
4. It atomically writes the environment's exact resolver to
   `/etc/resolv.conf`.
5. `getent ahostsv4 www.baidu.com` proves DNS through libc.
6. `curl` requests `https://www.baidu.com/` with the Debian CA bundle and
   requires a successful HTTP status.
7. `curl` downloads the existing Baidu PNG fixture to prove an ordinary page
   asset rather than only an empty response.
8. NetSurf opens that external resource in the existing desktop session and
   the browser evidence service verifies the expected window and process.

The M5 network evidence service is ordered before the M4 desktop service, so
the serial milestones for a fresh physical boot are M5 network READY, M4
desktop READY, and then M6 remote-browser evidence. The physical gate must use
that actual systemd order rather than accepting markers from an older boot.

No host proxy variable is copied into the guest. QEMU may use host slirp as
its packet transport, while Megrez sends packets through its physical RJ45
controller and LAN gateway.

## Components

### Environment-aware M5 evidence

Refactor the existing M5 shell evidence into a small, strict profile
selection step followed by shared DNS and HTTPS checks. Stable markers include
the environment, interface, address, gateway, resolver, host, and HTTP result.
They must not contain an unbounded command dump or credentials.

The QEMU marker contract remains compatible with existing results where
possible. Physical markers use a distinct `MEGREZ` identity so a QEMU result
cannot satisfy the board gate.

### Physical gate

Update the Megrez gate to require, in order:

1. selected GMAC identity and negotiated link;
2. Megrez address and host-gate boot-profile evidence, including the expected
   gateway;
3. DNS resolution of `www.baidu.com` through the reviewed LAN resolver;
4. certificate-validated HTTPS success;
5. successful Baidu PNG retrieval;
6. the existing Debian desktop READY marker;
7. NetSurf process/window evidence for the remote resource.

The gate drains and scans the complete serial transcript for panic, oops,
fatal GMAC/DMA failures, DNS failure, TLS failure, or browser failure. Missing,
duplicate, or out-of-order milestones fail.

### QEMU regression

The existing M5 and M6 QEMU gates remain the fast regression path. They prove
that environment selection has not replaced `10.0.2.3`, changed the expected
slirp address, or weakened HTTPS and browser evidence.

## Error handling

- The guest accepts only the exact QEMU and Megrez network identities.
- Resolver publication uses a temporary file and atomic rename; failure
  leaves no partially written `resolv.conf`.
- DNS, TCP, TLS, HTTP, asset download, and browser-window checks have separate
  bounded timeouts and distinct failure reasons.
- HTTPS always uses the Debian CA bundle; `curl -k` is forbidden.
- A DNS or external-network failure leaves the local desktop, terminal, file
  manager, keyboard, and mouse usable offline.
- The physical gate never invokes Linux as a substitute runtime kernel and
  never calls `saveenv`.

## Validation

### Host tests

- exact selection of the QEMU and Megrez profiles;
- rejection of unknown address/gateway/resolver combinations;
- preservation of existing QEMU milestones;
- atomic resolver replacement and failure preservation;
- strict command timeouts and stable layer-specific failure markers;
- physical transcript ordering and fatal-marker scanning.

### QEMU acceptance

One current Asterinas SMP=4 boot must retain the existing M5/M6 evidence:

- address `10.0.2.15/24`, gateway `10.0.2.2`, DNS `10.0.2.3`;
- successful certificate-validated HTTPS to `www.baidu.com`;
- successful Baidu PNG transfer;
- NetSurf foreground/window evidence;
- no panic, oops, network failure, or browser failure marker.

### Megrez acceptance

One current Asterinas boot with the RJ45 cable already connected must prove:

- selected GMAC, stable link, and no fatal DMA/PLIC error;
- address `10.100.19.200/21` and the exact boot profile containing gateway
  `10.100.16.1`;
- DNS resolution through primary resolver `10.2.0.5`, with `10.2.0.6` as the
  configured fallback;
- certificate-validated HTTPS to `www.baidu.com`;
- successful Baidu PNG transfer;
- NetSurf remains running and displays the external resource while HDMI,
  keyboard, mouse, terminal, and file manager stay usable.

Cable unplug/replug is not part of acceptance. If the cable is absent during
the boot-time GMAC selection window, the operator may reconnect it and reboot.

## Non-goals

- DHCP, `systemd-networkd`, NetworkManager, dynamic address or route mutation.
- Carrier notifications, cable replug recovery, or live GMAC failover.
- General LAN discovery or support for arbitrary board subnets.
- IPv6, Wi-Fi, USB Ethernet, VPNs, firewalling, NAT, or containers.
- Firefox admission, Chromium compatibility, or modern JavaScript parity.
- GNOME, audio, video, GPU acceleration, or unrelated desktop polish.

## Follow-up desktop milestone

After QEMU and Megrez pass this browser-ready network gate, desktop work moves
to a separate design. Its target is an Ubuntu-like usage experience on a
lightweight stack: graphical login, persistent normal-user session,
XFCE-class panel and application menu, file manager, terminal, settings,
network status, fonts, clipboard, and browser launcher. Full GNOME and GPU
acceleration remain later work.

## Estimate

The expected effort is one to three effective development days:

- half to one day for strict QEMU/Megrez profile selection and host tests;
- half to one day for QEMU M5/M6 regression and evidence cleanup;
- one to two days for physical DNS, TLS, asset, and NetSurf validation,
  including focused kernel debugging only if runtime evidence exposes a real
  packet-path defect.

No rtnetlink, DHCP, or reconnect work is included in this estimate.
