# Megrez RJ45 GMAC M5 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a native EIC7700 DWMAC Ethernet interface to Asterinas, automatically select the linked Megrez RJ45 GMAC at boot, and pass a signed Debian static-IPv4 physical ping gate without regressing QEMU VirtIO networking or the existing desktop.

**Architecture:** A new safe-Rust `aster-dwmac` component separates the generic DWMAC4/5 descriptor, queue, and MDIO core from EIC7700 DT/platform setup. The selected physical controller registers as the stable logical device `eic7700-rj45`; generic network initialization enumerates registered devices and applies a strict device-keyed boot profile. A signed `desktop-m5-network` rootfs adds only the observation tools and bounded gate required to prove ARP and ICMP in both directions.

**Tech Stack:** Rust 2024 `no_std`, OSTD `IoMem`/IRQ/DMA APIs, `aster-network`, `aster-bigtcp`, Python 3 `unittest`, shell, Debian Trixie riscv64 signed-rootfs tooling, QEMU VirtIO regression, Milk-V Megrez physical serial/HDMI gate.

---

## File map

- `tools/riscv/megrez_gmac_contract.py`: strict host-side extraction and validation of the exact Megrez GMAC DT contract.
- `tools/riscv/megrez_gmac_contract.v1.json`: frozen identity and normalized values observed from the physical M4 DTB.
- `tools/riscv/tests/test_megrez_gmac_contract.py`: contract parser, identity, and failure-boundary tests.
- `kernel/comps/dwmac/src/regs.rs`: DWMAC4/5 register offsets and checked bit-field helpers.
- `kernel/comps/dwmac/src/descriptor.rs`: queue-0 descriptor ownership state machine.
- `kernel/comps/dwmac/src/phy.rs`: bounded Clause 22 MDIO and link decoding.
- `kernel/comps/dwmac/src/select.rs`: deterministic two-port boot selection.
- `kernel/comps/dwmac/src/queue.rs`: RX/TX DMA rings and bounded reclaim/poll.
- `kernel/comps/dwmac/src/device.rs`: `AnyNetworkDevice` adapter and IRQ-to-softirq flow.
- `kernel/comps/dwmac/src/arch/riscv.rs`: exact EIC7700 DT/resources and platform setup.
- `kernel/comps/dwmac/src/arch/other.rs`: no-device implementation for other targets.
- `kernel/comps/dwmac/src/lib.rs`: component initialization and fail-closed registration.
- `kernel/comps/network/src/lib.rs`: device metadata and deterministic enumeration.
- `kernel/src/net/iface/init.rs`: generic Ethernet creation and device-keyed boot profile.
- `kernel/libs/aster-bigtcp/src/iface/phy/ether.rs`: optional default IPv4 route.
- `tools/riscv/debian/rootfs/profiles.py`: immutable `desktop-m5-network` package/filesystem identity.
- `tools/riscv/debian/rootfs/desktop_m5_network_evidence.sh`: guest link and ping evidence.
- `tools/riscv/debian/rootfs/desktop_m5_network_gate.py`: bounded QEMU/physical transcript classifier.
- `tools/riscv/tests/test_debian_rootfs.py`: profile, filesystem, script, and gate tests.
- `tools/riscv/tests/test_megrez_gmac_gate.py`: physical command, marker, address-conflict, and transcript tests.
- `tools/riscv/megrez_gmac_gate.py`: safe physical boot and host-side ICMP gate.
- `docs/porting/evidence/2026-08-26-megrez-rj45-gmac-m5.md`: final identities and physical evidence for this dated milestone.

### Task 1: Freeze the exact Megrez GMAC DT contract

**Files:**
- Create: `tools/riscv/megrez_gmac_contract.py`
- Create: `tools/riscv/megrez_gmac_contract.v1.json`
- Create: `tools/riscv/tests/test_megrez_gmac_contract.py`
- Modify: `Makefile`

- [ ] **Step 1: Write strict contract tests before the parser exists**

Add tests which inject a fake `fdtget` runner and require exactly two enabled
`eswin,win2030-qos-eth` nodes, unique MMIO/IRQ resources, `dma-noncoherent`,
`rgmii-txid`, one PHY address, valid unicast MACs, and complete ESWIN clock,
reset, HSP, and delay properties. Include rejection cases for missing,
duplicated, overlapping, disabled, or fallback-substituted fields.

```python
class MegrezGmacContractTests(unittest.TestCase):
    def test_accepts_two_complete_link_candidates(self):
        contract = validate_contract(valid_contract())
        self.assertEqual([port.mmio_start for port in contract.ports], [
            0x5040_0000,
            0x5041_0000,
        ])

    def test_rejects_missing_resource_instead_of_using_reference_default(self):
        raw = valid_contract()
        del raw["ports"][0]["interrupt"]
        with self.assertRaisesRegex(ContractError, "ports.0.interrupt"):
            validate_contract(raw)
```

- [ ] **Step 2: Run the focused RED test**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_megrez_gmac_contract -v
```

Expected: import failure because `tools.riscv.megrez_gmac_contract` does not
exist.

- [ ] **Step 3: Implement the strict JSON and DTB extractor**

Implement immutable records and a CLI which computes the DTB SHA-256, size,
and CRC32 from one open file, queries normalized properties with `fdtget`, and
validates rather than fills fields.

```python
@dataclass(frozen=True)
class GmacPort:
    alias_index: int
    node_path: str
    mmio_start: int
    mmio_size: int
    interrupt_parent: int
    interrupt: int
    phy_mode: str
    phy_address: int
    mac_address: str
    syscon_offsets: tuple[int, ...]
    delay_values: tuple[int, ...]

@dataclass(frozen=True)
class GmacContract:
    dtb_sha256: str
    dtb_size: int
    dtb_crc32: str
    ports: tuple[GmacPort, GmacPort]

def inspect_dtb(path: Path, run: Runner = subprocess.run) -> GmacContract:
    identity = hash_one_open_regular_file(path)
    ports = tuple(read_port(path, index, run) for index in (0, 1))
    return validate_contract(to_raw(identity, ports))
```

The `freeze` subcommand writes canonical JSON only after the supplied DTB
matches the already observed size 154800 and CRC32 `4afcb20e`. The generated
JSON records the newly measured SHA-256 and all actual Megrez property values;
no binary DTB is committed.

- [ ] **Step 4: Add and run the host unit target**

Add:

```make
.PHONY: test_riscv_megrez_gmac_unit
test_riscv_megrez_gmac_unit:
	@python3 -W error::ResourceWarning -m unittest \
		tools.riscv.tests.test_megrez_gmac_contract -v
```

Run the focused test and `git diff --check`. Expected: all contract tests pass.

- [ ] **Step 5: Extract and freeze the physical M4 DTB**

Use the existing board boot partition only as a source of the already used
DTB. Copy it without modifying U-Boot environment or booting Linux as the
runtime kernel, then run:

```bash
python3 -m tools.riscv.megrez_gmac_contract freeze \
  --dtb target/megrez-gmac-m5/eic7700-milkv-megrez.dtb \
  --output tools/riscv/megrez_gmac_contract.v1.json
python3 -m tools.riscv.megrez_gmac_contract verify \
  --dtb target/megrez-gmac-m5/eic7700-milkv-megrez.dtb \
  --contract tools/riscv/megrez_gmac_contract.v1.json
```

Expected: size 154800, CRC32 `4afcb20e`, exact two-port normalized contract,
and exit 0.

- [ ] **Step 6: Commit the DT contract**

```bash
git add Makefile tools/riscv/megrez_gmac_contract.py \
  tools/riscv/megrez_gmac_contract.v1.json \
  tools/riscv/tests/test_megrez_gmac_contract.py
git commit -m "test(riscv): freeze Megrez GMAC contract"
```

### Task 2: Add DWMAC register and descriptor state machines

**Files:**
- Create: `kernel/comps/dwmac/Cargo.toml`
- Create: `kernel/comps/dwmac/src/regs.rs`
- Create: `kernel/comps/dwmac/src/descriptor.rs`
- Create: `kernel/comps/dwmac/src/lib.rs`
- Modify: `Cargo.toml`
- Modify: `Cargo.lock`
- Modify: `kernel/Cargo.toml`

- [ ] **Step 1: Add failing kernel tests for checked registers and descriptors**

The tests must cover descriptor ownership publication, RX length extraction,
TX completion, ring-end flags, malformed frame lengths, and register field
overflow.

```rust
#[ktest]
fn descriptor_is_published_only_after_address_and_length() {
    let mut descriptor = Descriptor::zeroed();
    assert!(descriptor.publish_rx(Daddr::new(0x8000_0000), 2048).is_ok());
    assert!(descriptor.owned_by_dma());
    assert_eq!(descriptor.buffer_address(), 0x8000_0000);
}

#[ktest]
fn rejects_frame_larger_than_the_backing_buffer() {
    let completed = CompletedRx::decode([0, 0, 4096 << RX_FRAME_LEN_SHIFT, 0]);
    assert_eq!(completed.validate(2048), Err(DescriptorError::FrameTooLong));
}
```

- [ ] **Step 2: Run RED before adding the crate implementation**

Run the pinned-container command:

```bash
cargo osdk check --ktests -p aster-dwmac \
  --target riscv64imac-unknown-none-elf
```

Expected: package/module/type absence.

- [ ] **Step 3: Implement only the DWMAC4/5 queue-0 register surface**

Define checked helpers around the MAC configuration, MDIO address/data, DMA
mode/status/interrupt, channel control, descriptor-list, ring-length, and tail
pointer registers. Descriptor words remain private and are changed through
typed state transitions.

```rust
#[repr(C, align(16))]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct Descriptor([u32; 4]);

impl Descriptor {
    fn publish_rx(&mut self, address: Daddr, capacity: usize) -> Result<(), DescriptorError>;
    fn publish_tx(&mut self, address: Daddr, length: usize) -> Result<(), DescriptorError>;
    fn take_completed_rx(&mut self, capacity: usize) -> Result<Option<usize>, DescriptorError>;
    fn reclaim_tx(&mut self) -> Result<bool, DescriptorError>;
}
```

- [ ] **Step 4: Register the component crate without probing hardware**

Add `aster-dwmac` to workspace members/dependencies and to `aster-kernel`.
Keep component initialization returning success with no device until the
platform task is complete, so intermediate commits compile on all targets.

- [ ] **Step 5: Run GREEN and commit**

Run the same OSDK check plus:

```bash
RUSTFLAGS=-Dwarnings cargo clippy -p aster-dwmac \
  --target riscv64imac-unknown-none-elf --no-deps
```

Expected: descriptor/register ktests compile and Clippy is clean.

```bash
git add Cargo.toml Cargo.lock kernel/Cargo.toml kernel/comps/dwmac
git commit -m "feat(riscv): define DWMAC queue contract"
```

### Task 3: Implement bounded PHY discovery and deterministic port selection

**Files:**
- Create: `kernel/comps/dwmac/src/phy.rs`
- Create: `kernel/comps/dwmac/src/select.rs`
- Modify: `kernel/comps/dwmac/src/lib.rs`

- [ ] **Step 1: Add failing MDIO and selector tests**

Use an injected `MdioBus` and monotonic clock. Freeze double-read BMSR
semantics, 10/100/1000 and duplex decoding, transaction deadlines, one-link,
two-link, no-link, and exact-at-deadline behavior.

```rust
#[ktest]
fn lowest_alias_wins_when_both_links_are_up() {
    let selected = select_linked_port(
        [linked(1, 1000), linked(0, 100)],
        Duration::from_secs(3),
    ).unwrap();
    assert_eq!(selected.alias_index, 0);
}

#[ktest]
fn bmsr_link_is_read_twice_because_it_is_latched_low() {
    let mut bus = FakeMdio::with_reads([0, BMSR_LINK_STATUS]);
    assert!(read_link(&mut bus, 0, deadline()).unwrap());
    assert_eq!(bus.read_count(), 2);
}
```

- [ ] **Step 2: Run RED**

Run the `aster-dwmac` ktest compile. Expected: missing `phy` and `select`
modules.

- [ ] **Step 3: Implement bounded Clause 22 operations**

```rust
trait MdioBus {
    fn read(&mut self, phy: u8, reg: u8, deadline: Instant) -> Result<u16, MdioError>;
    fn write(&mut self, phy: u8, reg: u8, value: u16, deadline: Instant)
        -> Result<(), MdioError>;
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct LinkState {
    speed_mbps: u16,
    full_duplex: bool,
}
```

No MDIO/reset/link loop may depend on an unbounded iteration count.

- [ ] **Step 4: Implement boot selection**

Poll both complete candidates until the three-second deadline. Return one
selected port, prefer the lowest alias if both are up, and return `NoLink`
without registering a device if the deadline expires.

- [ ] **Step 5: Run GREEN and commit**

Run focused ktests and Clippy, then:

```bash
git add kernel/comps/dwmac/src
git commit -m "feat(riscv): select linked Megrez GMAC"
```

### Task 4: Add the EIC7700 platform adapter

**Files:**
- Create: `kernel/comps/dwmac/src/arch/riscv.rs`
- Create: `kernel/comps/dwmac/src/arch/other.rs`
- Modify: `kernel/comps/dwmac/src/lib.rs`
- Modify: `kernel/comps/dwmac/Cargo.toml`

- [ ] **Step 1: Add failing tests from the frozen contract**

Convert the normalized physical JSON fields into Rust test fixtures and
require exact validation of both MMIO ranges, PLIC sources, DMA incoherency,
PHY addresses, MAC addresses, syscon offsets, delays, and alias order.

```rust
#[ktest]
fn accepts_the_two_frozen_megrez_ports() {
    assert_eq!(validate_port(gmac0_fields()).unwrap().mmio_range,
               0x5040_0000..0x5041_0000);
    assert_eq!(validate_port(gmac1_fields()).unwrap().mmio_range,
               0x5041_0000..0x5042_0000);
}
```

- [ ] **Step 2: Run RED**

Expected: missing architecture module and configuration types.

- [ ] **Step 3: Parse resources without address fallbacks**

Implement `PlatformConfig::from_node` using `DEVICE_TREE`. Each reference
address remains an assertion in validation, never a substitute for an absent
property. Acquire MMIO with `IoMem`, resolve PLIC sources through
`InterruptSourceInFdt`, and preserve the non-coherent EIC7700 DMA window.

- [ ] **Step 4: Implement the official platform sequence**

Program only the HSP/CRG fields present in the frozen contract, assert reset,
set clock and RGMII delays, deassert reset, confirm DWMAC4/5 identity, and
perform MDIO inspection. Set 125/25/2.5 MHz TX clock for 1000/100/10 Mbit/s.
Every reset and ready transition uses a monotonic deadline and a read-back
check.

- [ ] **Step 5: Run GREEN and commit**

Run component ktests, RISC-V compile, and Clippy.

```bash
git add kernel/comps/dwmac
git commit -m "feat(riscv): prepare EIC7700 GMAC resources"
```

### Task 5: Implement RX/TX rings and IRQ-to-softirq delivery

**Files:**
- Create: `kernel/comps/dwmac/src/queue.rs`
- Create: `kernel/comps/dwmac/src/device.rs`
- Modify: `kernel/comps/dwmac/src/lib.rs`
- Modify: `kernel/comps/dwmac/Cargo.toml`

- [ ] **Step 1: Add failing queue tests**

Test 64-entry wraparound, a full TX ring returning `NetError::Busy`, RX frame
delivery, malformed-frame drop, a fixed poll budget, TX reclaim, cache sync
ordering through a fake DMA backend, and fatal status queue stop.

```rust
#[ktest]
fn bounded_poll_stops_at_budget_and_preserves_remaining_work() {
    let mut queue = FakeQueue::with_completed_rx(POLL_BUDGET + 1);
    assert_eq!(queue.poll_rx().processed, POLL_BUDGET);
    assert!(queue.has_completed_rx());
}
```

- [ ] **Step 2: Run RED**

Expected: missing queue/device types.

- [ ] **Step 3: Implement fresh non-coherent rings**

Allocate 64 RX and 64 TX descriptors and 2 KiB buffers through OSTD DMA
storage. Sync packet bytes before descriptor ownership on TX, and sync a
completed RX buffer from the device before reading it. Program fresh list,
length, and tail pointers; do not reuse U-Boot state.

- [ ] **Step 4: Implement `AnyNetworkDevice` and interrupt handling**

```rust
impl AnyNetworkDevice for DwmacDevice {
    fn mac_addr(&self) -> EthernetAddr;
    fn capabilities(&self) -> DeviceCapabilities;
    fn can_receive(&self) -> bool;
    fn can_send(&self) -> bool;
    fn receive(&mut self) -> Result<RxBuffer, NetError>;
    fn send(&mut self, packet: &[u8]) -> Result<(), NetError>;
    fn free_processed_tx_buffers(&mut self);
    fn notify_poll_end(&mut self);
}
```

The top half acknowledges known channel bits, masks scheduled work, and raises
the existing network softirq. Poll completion unmasks the relevant source.

- [ ] **Step 5: Register only the selected logical device**

Register `eic7700-rj45` after link and queue startup. Leave the unselected DMA
engine stopped and interrupts masked. Emit the exact
`ASTERINAS_GMAC_SELECTED` marker only after all read-back checks pass.

- [ ] **Step 6: Run GREEN and commit**

Run component ktests, full RISC-V OSDK ktest compile, and `-Dwarnings` Clippy.

```bash
git add kernel/comps/dwmac
git commit -m "feat(riscv): drive EIC7700 Ethernet packets"
```

### Task 6: Generalize interface construction and add static boot profiles

**Files:**
- Modify: `kernel/comps/network/src/lib.rs`
- Modify: `kernel/src/net/iface/init.rs`
- Modify: `kernel/libs/aster-bigtcp/src/iface/phy/ether.rs`
- Modify: `kernel/src/net/socket/netlink/route/kernel/link.rs`
- Modify: `kernel/src/net/socket/netlink/route/kernel/addr.rs`

- [ ] **Step 1: Add failing metadata and boot-profile tests**

Freeze deterministic registry order, duplicate registration rejection,
strict `asterinas.net=<key>,<address>/<prefix>` parsing, unknown keys, duplicate
profiles, VirtIO compatibility defaults, optional gateway, and accurate
`LOWER_UP` reporting.

```rust
#[ktest]
fn megrez_profile_has_no_fabricated_default_route() {
    let profile = BootNetworkProfile::parse(
        "eic7700-rj45,10.100.19.200/21"
    ).unwrap();
    assert_eq!(profile.gateway, None);
}
```

- [ ] **Step 2: Run RED**

Expected: generic enumeration and boot-profile types are missing.

- [ ] **Step 3: Add immutable registration metadata**

Change the registry value to carry the stable logical key, physical link state,
and device reference. Reject duplicate keys instead of silently replacing the
first device. Return a sorted snapshot from `all_devices()`.

- [ ] **Step 4: Build interfaces generically**

Create loopback first, enumerate every registered Ethernet device, assign
`eth0`, `eth1`, and later names deterministically, register callbacks by the
logical key, and apply only the matching strict boot profile. Preserve the
current VirtIO address/gateway when no explicit VirtIO profile exists.

- [ ] **Step 5: Make default routes optional**

Change `EtherIface::new(..., gateway: Option<Ipv4Address>, ...)`; add a default
route only for `Some(gateway)`. Update the VirtIO call to `Some(10.0.2.2)` and
Megrez to `None`.

- [ ] **Step 6: Run GREEN and commit**

Run network/dwmac/kernel ktest compile, Clippy, and the existing QEMU network
test with `-smp 4`.

```bash
git add kernel/comps/network kernel/src/net/iface \
  kernel/src/net/socket/netlink/route/kernel \
  kernel/libs/aster-bigtcp/src/iface/phy/ether.rs
git commit -m "feat(net): enumerate physical Ethernet devices"
```

### Task 7: Build the signed Desktop M5 network profile and gates

**Files:**
- Modify: `tools/riscv/debian/rootfs/profiles.py`
- Modify: `tools/riscv/debian/rootfs/build_rootfs.sh`
- Create: `tools/riscv/debian/rootfs/desktop_m5_network_evidence.sh`
- Create: `tools/riscv/debian/rootfs/desktop_m5_network_gate.py`
- Modify: `tools/riscv/tests/test_debian_rootfs.py`
- Create: `tools/riscv/tests/test_megrez_gmac_gate.py`
- Create: `tools/riscv/megrez_gmac_gate.py`
- Modify: `Makefile`

- [ ] **Step 1: Add profile and transcript RED tests**

Require schema 5, label `ASTER_DEBIANM5`, UUID
`182e1ea4-296d-5383-8bcb-ea67e40db074`, the exact M4 package set plus sorted
`iproute2` and `iputils-ping`, ordered markers, full-transcript panic/failure
scan, and exact interface/address/ping values.

- [ ] **Step 2: Add physical-gate RED tests**

Test ARP-conflict refusal before serial open, safe RAM-only U-Boot commands,
the required kernel argument, split serial markers, bounded deadlines, ten
host pings, no `saveenv`, result-first stale invalidation, atomic logs/result,
and cleanup on signal or process failure.

- [ ] **Step 3: Run RED**

```bash
make test_riscv_debian_rootfs_unit
make test_riscv_megrez_gmac_unit
```

Expected: missing profile, evidence, and gate implementation failures.

- [ ] **Step 4: Implement the immutable profile and evidence service**

The service waits with a monotonic deadline, verifies `eth0`,
`10.100.19.200/21`, and `LOWER_UP`, executes exactly ten guest-originated
pings to `10.100.19.216`, and emits either one stable failure marker or the
ordered `DEBIAN_NETWORK_M5_LINK`, `DEBIAN_NETWORK_M5_GUEST_PING`, and
`DEBIAN_NETWORK_M5_READY` markers. Any error emits exactly one
`DEBIAN_NETWORK_M5_FAIL reason=<stable-reason>` marker and exits nonzero.

- [ ] **Step 5: Implement the bounded host gate**

Reuse existing board-session and descriptor-pinned output patterns. Before
opening serial, perform duplicate-address detection. Start the Asterinas
candidate with `asterinas.net=eic7700-rj45,10.100.19.200/21`, collect all
kernel/guest markers, run ten host-originated pings, drain serial, and publish
evidence only if both directions pass and no fatal marker exists.

- [ ] **Step 6: Run GREEN and commit**

Extend `test_riscv_megrez_gmac_unit` to include
`tools.riscv.tests.test_megrez_gmac_gate`, then run both host unit targets,
shell syntax, Python compilation, Ruff, and diff checks.

```bash
git add Makefile tools/riscv/debian/rootfs tools/riscv/megrez_gmac_gate.py \
  tools/riscv/tests/test_debian_rootfs.py \
  tools/riscv/tests/test_megrez_gmac_gate.py
git commit -m "test(riscv): gate Megrez Ethernet desktop"
```

### Task 8: Run local compile and QEMU regression gates

**Files:**
- Modify only if a real gate exposes a source defect in the files from Tasks 2-7.

- [ ] **Step 1: Run the bounded host gates once**

```bash
make test_riscv_megrez_gmac_unit
make test_riscv_debian_rootfs_unit
```

Expected: all tests pass with no ResourceWarning.

- [ ] **Step 2: Run RISC-V compile and Clippy gates in the pinned container**

```bash
cargo osdk check --ktests -p aster-dwmac -p aster-network -p aster-kernel \
  --target riscv64imac-unknown-none-elf
RUSTFLAGS=-Dwarnings cargo clippy -p aster-dwmac -p aster-network \
  --target riscv64imac-unknown-none-elf --no-deps
make kernel TARGET_ARCH=riscv64 SMP=4
```

Expected: exit 0. Use the existing host rustup cache and local proxy rather
than waiting for slow duplicate toolchain downloads.

- [ ] **Step 3: Build the signed M5 root once**

Run the builder in the pinned Debian build container using the existing
content-addressed package cache:

```bash
./tools/riscv/debian/rootfs/build_rootfs.sh --profile desktop-m5-network
python3 -m tools.riscv.debian.rootfs.contract verify \
  --image target/debian-riscv/desktop-m5-network/rootfs/debian-root.ext2 \
  --manifest target/debian-riscv/desktop-m5-network/rootfs/manifest.json \
  --packages-lock target/debian-riscv/desktop-m5-network/rootfs/packages.lock
dumpe2fs -h target/debian-riscv/desktop-m5-network/rootfs/debian-root.ext2
debugfs -R 'stat /bin/bash' \
  target/debian-riscv/desktop-m5-network/rootfs/debian-root.ext2
```

Expected: the public contract exits 0; the ext2 label, UUID, block size, and
no-journal feature match schema 5; `/bin/bash` is present; the manifest and
checksums bind every locked package; no qemu-user binary is present.

- [ ] **Step 4: Run one QEMU cold-boot regression**

Use generic Sv39, SMP=4, 2 GiB, the existing VirtIO NIC, and the M5 root. The
QEMU gate proves the generic network-interface changes and full M4 desktop
remain valid; it does not claim EIC7700 hardware coverage.

- [ ] **Step 5: Commit only actual bug fixes**

If no source defect is found, create no empty commit. If a defect is found,
record its focused RED/GREEN evidence and use a narrow `fix(riscv): ...`
commit.

### Task 9: Pass the physical Megrez M5 gate and record evidence

**Files:**
- Create: `docs/porting/evidence/2026-08-26-megrez-rj45-gmac-m5.md`

- [ ] **Step 1: Preflight the live hardware without changing it**

Confirm `/dev/ttyUSB0` ownership, host `10.100.19.216/21`, link carrier, no
owner of `10.100.19.200` with a different MAC, exact candidate hashes, and
clean U-Boot environment policy. Do not wait silently; report serial state or
the exact manual reset needed within one minute.

- [ ] **Step 2: Install M5 through Asterinas**

Use the existing restart-safe installer to write only eMMC partition 2, read
back and hash the full image, sync, and cold reboot. Linux may transfer frozen
artifacts but does not install or run the candidate system.

- [ ] **Step 3: Run the bounded physical gate**

Require the exact selected-GMAC, link, guest-ping, host-ping, Desktop M4 READY,
and M5 READY evidence. Keep link for 60 seconds and send enough bounded ICMP
traffic to wrap both 64-entry rings. Reject any loss, DMA fatal bit, panic,
oops, marker reordering, or unexplained process exit.

- [ ] **Step 4: Leave a persistent desktop/network boot only after PASS**

Repeat the same frozen RAM-only boot without an automatic reboot timer. Release
the host serial reader after READY so HDMI, keyboard, mouse, and the RJ45 link
continue running while serial remains available for later debugging.

- [ ] **Step 5: Write and verify the evidence page**

Record source commit, kernel/DTB/stage1/root identities, exact selected GMAC,
PHY/link/MAC values, host and guest commands, packet counts, serial/result-log
hashes, QEMU limitation, and the M6 boundary. Run `git diff --check` and verify
every cited local file exists.

- [ ] **Step 6: Commit M5 evidence**

```bash
git add docs/porting/evidence/2026-08-26-megrez-rj45-gmac-m5.md
git commit -m "docs(riscv): record Megrez RJ45 GMAC M5"
```

## Completion gate

M5 is complete only when the branch is clean and all of the following are
freshly established: strict physical DT contract, host unit tests, RISC-V
ktest compile, DWMAC/network Clippy with warnings denied, one QEMU VirtIO M5
regression, one physical current-boot two-direction ping gate, a 60-second
stable link with ring wrap, unchanged desktop/input readiness, and committed
evidence. Remote CI monitoring is not part of this plan.
