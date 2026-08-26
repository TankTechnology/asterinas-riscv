# Megrez RJ45 GMAC M5 Design

Date: 2026-08-26

## Goal

Add a native, bounded, and testable on-board Ethernet path for the Milk-V
Megrez running Asterinas. M5 must discover both EIC7700 GMAC instances from
the real board DTB, select the RJ45 controller whose PHY reports link at boot,
exchange Ethernet frames through the existing Asterinas network stack, and
pass a physical static-IPv4 ping gate against the development host. The gate
runs from a new signed Debian profile derived from Desktop M4, so the existing
desktop artifact remains an immutable rollback point.

This milestone does not add a USB Ethernet adapter, Wi-Fi, DHCP, DNS, TLS, or
browser networking. Those user-space and routing features form the following
M6 milestone after the native link is trustworthy.

## Current state and hardware authority

Asterinas currently registers Ethernet devices through `aster-network`, but
the initial network namespace constructs only a hard-coded VirtIO interface
named `eth0` with QEMU-specific address and gateway values. No native EIC7700
Ethernet component exists.

The hardware authority for M5 is:

- the exact Megrez DTB used by the already verified physical Desktop M4 boot;
- the ESWIN EIC7700 device-tree nodes compatible with
  `eswin,win2030-qos-eth`;
- ESWIN's `dwmac-win2030.c` platform implementation for clock, reset, HSP,
  RGMII-delay, MDIO, and PHY behavior;
- the Synopsys DWMAC4/5 descriptor and register contract selected by that
  platform driver.

The relevant ESWIN sources are:

- <https://github.com/eswincomputing/linux-stable/blob/linux-6.6.18-EIC7X/arch/riscv/boot/dts/eswin/eswin-win2030-die0-soc.dtsi>
- <https://github.com/eswincomputing/linux-stable/blob/linux-6.6.18-EIC7X/arch/riscv/boot/dts/eswin/eic7700-evb.dts>
- <https://github.com/eswincomputing/linux-stable/blob/linux-6.6.18-EIC7X/drivers/net/ethernet/stmicro/stmmac/dwmac-win2030.c>

The generic ESWIN DTS describes GMAC0 at `0x50400000` with interrupt 61 and
GMAC1 at `0x50410000` with interrupt 70. These values are reference facts, not
fallback discovery rules: production Asterinas accepts only complete,
internally consistent nodes in the frozen Megrez DTB.

## Chosen approach

Create a focused safe-Rust `aster-dwmac` component with two boundaries:

1. a reusable minimal DWMAC4/5 queue, descriptor, MAC, and MDIO core;
2. an EIC7700 platform adapter that owns DT parsing, MMIO resources, PLIC
   interrupt mapping, clocks, resets, HSP controls, RGMII delays, and PHY link
   configuration.

The component implements queue 0 only, with one RX ring and one TX ring. It
does not implement multi-queue operation, checksum offload, TSO, PTP, WOL, or
energy-management features. This is smaller and easier to validate than a
Linux `stmmac` feature-parity port while still owning all hardware state needed
for reliable native operation.

Two alternatives are rejected:

- inheriting U-Boot's configured MAC and descriptor state would couple kernel
  correctness to undocumented bootloader residue and would fail across link
  changes or a different U-Boot build;
- porting the full Linux `stmmac` feature set would add several weeks of
  unused offload, queueing, PTP, and power-management work before the first
  packet could be trusted.

## Component boundaries

`kernel/comps/dwmac` will remain entirely safe Rust and `#![deny(unsafe_code)]`.
It will use existing OSTD interfaces for MMIO, IRQs, DMA addressing, and
non-coherent cache synchronization.

The component is divided by responsibility:

- register definitions contain typed offsets, masks, and checked field
  construction, with no resource discovery;
- descriptor code owns ring entry state transitions and DMA ownership bits;
- the queue owns 64 RX and 64 TX descriptors, 2 KiB packet buffers, producer
  and consumer indices, and bounded reclaim;
- MDIO code performs bounded Clause 22 transactions and reads the PHY status
  register twice where the link bit is latched low;
- the EIC7700 adapter parses and validates both DT nodes and performs the
  board-specific clock/reset/HSP/RGMII sequence;
- the network adapter implements `AnyNetworkDevice` and translates queue
  buffers into existing `RxBuffer` and `TxBuffer` values.

No new `unsafe` block is permitted in `kernel/`. MMIO access uses `IoMem`, DMA
storage uses OSTD DMA types, and interrupt handling follows the existing
top-half-to-network-softirq pattern.

## DT and resource contract

Before driver implementation, the exact Desktop M4 Megrez DTB is extracted,
decompiled, hashed, and represented by a host-side contract fixture. Each
accepted GMAC must provide all of the following:

- enabled status and exact `eswin,win2030-qos-eth` compatibility;
- one unique, page-aligned 64 KiB MMIO range;
- a nonzero PLIC parent and one unique interrupt source;
- `dma-noncoherent` and an EIC7700-compatible DMA window;
- supported `phy-mode`, initially `rgmii-txid`;
- an MDIO child with one valid PHY address;
- the platform clock, reset, HSP, and RGMII-delay properties required by the
  ESWIN driver;
- a valid unicast MAC address from the DT or the board's existing firmware
  identity.

Missing, duplicated, overlapping, out-of-range, or contradictory resources
fail closed. Reference addresses are never used to silently replace absent DT
properties.

## Boot-time port selection

Both valid GMAC platform blocks are reset and prepared for MDIO inspection,
but DMA starts only on the selected controller. The selector samples each PHY
for a bounded three-second boot window:

1. exactly one link-up PHY selects that GMAC;
2. if both PHYs report link, the lowest DT alias (`ethernet0` before
   `ethernet1`) wins deterministically and the log records both links;
3. if neither PHY reports link before the deadline, no physical interface is
   registered, the desktop continues offline, and the log states that a reboot
   with the cable connected is required for M5.

Hot cable insertion and live failover between GMACs are explicitly deferred.
This keeps the first implementation independent of dynamic interface creation
and MAC-address replacement, neither of which the current network stack
supports cleanly.

After selection, the driver sets the MAC speed from the negotiated PHY result:
125 MHz for 1 Gbit/s, 25 MHz for 100 Mbit/s, or 2.5 MHz for 10 Mbit/s, matching
the ESWIN platform contract.

## Packet and interrupt flow

At initialization, the selected GMAC receives fresh descriptor rings and
buffers; no U-Boot descriptor or DMA pointer is reused. RX descriptors are
owned by hardware only after their buffers and descriptors have been synced to
the device. Completed RX buffers are synced from the device before packet
bytes are read. TX descriptors are published only after packet bytes and the
descriptor are synced to the device.

The IRQ top half acknowledges only known DWMAC status bits, masks work already
scheduled, and raises the existing network RX/TX softirqs. Ring walking,
packet delivery, TX reclaim, and descriptor refill happen in the bounded poll
path. Each poll has a fixed work budget so sustained traffic cannot starve
other kernel work. Unknown fatal DMA status stops that queue and produces a
specific diagnostic instead of looping or handing corrupt data to the stack.

## Network-stack integration

`aster-network` will expose deterministic enumeration of registered devices
rather than forcing consumers to know the VirtIO device name. Initial-network-
namespace setup will create loopback first and then one Ethernet interface per
registered device, registering its send and receive callbacks by the actual
device name.

The selected controller registers under the stable logical device key
`eic7700-rj45`; the physical GMAC index remains diagnostic metadata. A strict
repeatable kernel parameter supplies a device-keyed boot profile, for example
`asterinas.net=eic7700-rj45,10.100.19.200/21`. Invalid, duplicated, or
unknown-device profiles are rejected rather than partially applied. The
Ethernet-interface constructor is changed to accept an optional IPv4 gateway:
Megrez M5 has no default route, while the existing QEMU VirtIO compatibility
profile retains `10.0.2.2`.

M5 retains a boot-time static IPv4 profile because route mutation is not yet
complete in Asterinas netlink. On Megrez the selected physical interface uses
the already observed board address `10.100.19.200/21`; the direct physical gate
targets the development host at `10.100.19.216`. QEMU VirtIO keeps its existing
`10.0.2.15/24` and `10.0.2.2` profile. Address selection is keyed by the device
kind and cannot accidentally apply the Megrez address to QEMU.

Before physical launch, the host gate sends an ARP probe for `10.100.19.200`
and refuses to boot the candidate if a different MAC already owns that
address. The address is not written to U-Boot or any persistent host network
configuration.

M5 creates a signed `desktop-m5-network` Debian profile derived from M4. It
adds `iproute2` and `iputils-ping`, a bounded evidence service, and no other
application packages. The service waits for the selected interface and link,
records `ip link` and `ip address` evidence, then pings the development host.
It emits `DEBIAN_NETWORK_M5_READY` only after the guest-originated ping gate
passes. Package locks, checksums, manifest identity, ext2 label/UUID, and
publication remain governed by the existing signed-rootfs contract.
The frozen filesystem identity uses schema version 5, label
`ASTER_DEBIANM5`, and UUID `182e1ea4-296d-5383-8bcb-ea67e40db074`.

After the signed profile passes its QEMU VirtIO regression, the existing
restart-safe Asterinas installer writes and reads back the M5 image in eMMC
partition 2. Linux may transfer already frozen artifacts to the board but may
not install the root or serve as the runtime kernel. The test boot uses only
RAM-local U-Boot commands and never calls `saveenv`.

M6 will replace the physical static profile with user-space configuration and
will separately implement the missing address and route mutation semantics
needed by `systemd-networkd`.

## Failure handling and safety

- Initialization errors identify the exact GMAC index and stage: DT, platform,
  MDIO, link selection, ring allocation, reset, MAC configuration, or IRQ.
- The unselected GMAC remains quiesced with DMA disabled and interrupts masked.
- Partial ring initialization revokes hardware ownership before DMA storage is
  released.
- Descriptor indexes and packet lengths are checked before buffer access;
  malformed or oversized frames are dropped and counted.
- MDIO, reset, DMA-stop, and descriptor waits all use monotonic deadlines.
- A driver failure never prevents the already verified storage, USB input, or
  HDMI desktop path from continuing offline.
- The physical test changes no persistent U-Boot environment and does not boot
  Linux as a substitute for Asterinas.

## Validation strategy

M5 uses one verification layer for each claim:

1. host unit tests freeze exact DT parsing, rejection cases, descriptor
   ownership transitions, ring wraparound, bounded polling, PHY link decoding,
   and deterministic two-port selection;
2. RISC-V OSDK compile and kernel tests validate the safe-Rust component and
   EIC7700-specific configuration paths;
3. the existing QEMU VirtIO network tests prove that generic interface
   enumeration does not regress QEMU networking; QEMU is not claimed to
   emulate the EIC7700 GMAC;
4. a physical Megrez gate records selected node, PHY address, link speed,
   duplex, MAC address, descriptor-ring startup, ARP exchange, and ICMP traffic
   in both directions;
5. the signed Desktop M5 profile must reach both the unchanged M4 desktop
   READY marker and `DEBIAN_NETWORK_M5_READY`, with keyboard and mouse usable
   after network initialization.

The serial gate requires these new markers in order:

1. `ASTERINAS_GMAC_SELECTED device=eic7700-rj45 controller=<0|1>
   phy=<0..31> speed=<10|100|1000> duplex=<half|full> mac=<address>`;
2. `DEBIAN_NETWORK_M5_LINK interface=eth0 address=10.100.19.200/21`;
3. `DEBIAN_NETWORK_M5_GUEST_PING target=10.100.19.216 transmitted=10
   received=10`;
4. `DEBIAN_NETWORK_M5_READY interface=eth0`.

Any corresponding `ASTERINAS_GMAC_FAIL` or `DEBIAN_NETWORK_M5_FAIL` marker,
kernel panic, oops, DMA fatal status, or missing ordered marker fails the gate.

Physical acceptance requires all of the following in one current boot:

- exactly one selected GMAC and no DMA/PLIC error;
- stable link for at least 60 seconds;
- successful ARP resolution between `10.100.19.200` and `10.100.19.216`;
- ten consecutive host-to-board pings and ten board-to-host pings with no loss;
- a bounded packet burst that wraps both RX and TX rings without descriptor
  leak, timeout, panic, or oops;
- the unchanged Desktop M4 READY marker after network initialization.

## Scope boundary and follow-up

M5 ends at a trustworthy native Ethernet link and static-IP packet exchange.
M6 will then add mutable link/address/default-route support, Debian
`systemd-networkd`, DHCP, DNS, clock synchronization, CA validation, HTTPS, and
the first online NetSurf page. USB Ethernet, Wi-Fi, IPv6 configuration,
firewalling, advanced DWMAC offloads, live link failover, and modern browser
JavaScript remain outside M5.

## Work estimate

The expected M5 effort is five to ten effective development days:

- one to two days for the exact DT/hardware contract and host tests;
- three to five days for the minimal DWMAC core, EIC7700 adapter, and generic
  interface integration;
- two to five days for physical PHY, DMA-cache, clock, reset, and interrupt
  debugging, with overlap between implementation and board work.

The first static-IP ping is expected after roughly three to seven effective
development days. M6 DHCP/DNS/HTTPS/browser networking is expected to require
another three to seven days after M5 is stable.
