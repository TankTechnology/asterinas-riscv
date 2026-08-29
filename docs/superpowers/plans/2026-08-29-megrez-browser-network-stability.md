# Megrez Browser Network Stability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Keep review bounded to the focused tests plus one relevant regression gate per task.

**Goal:** Establish a deterministic, desktop-independent definition of “network good enough for browser use” on QEMU and the Milk-V Megrez board, then use one bounded physical boot to verify the complete path before returning to NetSurf compatibility work.

**Architecture:** Split the existing physical browser gate into a `network` target and the backward-compatible `browser` target. The network target stops after ordered M5 evidence and cannot be invalidated by Xorg, xdotool, or NetSurf readiness. Add a host-owned deterministic HTTP fixture serving fixed bytes with a fixed SHA-256; the guest downloads and verifies it repeatedly before retaining the existing Baidu HTTPS and PNG checks as an external canary. Run all host tests and three cached QEMU SMP=4 trials before one physical run with host packet capture and automatic reboot.

**Tech Stack:** Python 3 standard library and `unittest`, Bash, curl/sha256sum, QEMU RISC-V SMP=4, Asterinas Debian rootfs, U-Boot serial control, arping/tcpdump/tshark, existing Megrez MMC boot workflow.

---

## Acceptance contract

- `--target browser` remains the default and preserves the existing complete M4/M5/M6 classification.
- `--target network` requires exactly one selected-GMAC marker followed by every Megrez M5 marker in order, then drains and scans the complete transcript for fatal markers.
- The deterministic fixture serves exactly 65,536 fixed bytes and records a bounded request summary. A conflicting listener or unsafe bind address fails before the serial device is opened.
- The guest performs 20 successful GETs. Every body must have the exact size and SHA-256 before it emits one `DEBIAN_NETWORK_M5_STRESS` marker.
- The existing Baidu HTTPS status and PNG asset checks remain required external canaries; deterministic success alone is insufficient.
- QEMU uses `-smp 4` and passes the complete network gate three consecutive times from cached build artifacts.
- Physical acceptance uses one boot, an automatic reboot deadline, duplicate-address preflight, packet capture, and a stable `/dev/serial/by-id/...` path. It must not require desktop readiness.
- Physical evidence must contain the stress marker, the M5 ready marker, no ARP pending-queue-full warnings, no fatal transcript marker, and no nonzero MTL/DMA receive-drop evidence.

## File map

- Modify `tools/riscv/megrez_gmac_gate.py`: target-specific markers, classification, wait condition, result identity, CLI, and fixture lifecycle integration.
- Modify `tools/riscv/tests/test_megrez_gmac_gate.py`: network-target contract, classifier ordering, lifecycle, failure, and backward-compatibility tests.
- Create `tools/riscv/megrez_network_fixture.py`: bounded deterministic HTTP fixture and canonical request summary.
- Create `tools/riscv/tests/test_megrez_network_fixture.py`: real-loopback HTTP, allowlist, cap, shutdown, and CLI tests.
- Modify `tools/riscv/debian/rootfs/desktop_m5_network_evidence.sh`: 20-request exact payload verification and stress marker.
- Modify `tools/riscv/tests/test_debian_m5_network.py`: fake-curl stress RED/GREEN cases and exact call ordering.
- Modify `tools/riscv/debian/rootfs/desktop_m5_network_gate.py`: include the new stress milestone in the Megrez ordered contract.
- Modify `tools/riscv/debian/rootfs/desktop_m5_qemu_gate.py`: pass the deterministic fixture endpoint to the guest QEMU gate.
- Modify `Makefile`: include the fixture unit test and expose fixed fixture endpoint variables to the QEMU gate.
- Modify `tools/riscv/README.md`: document the network-only command, QEMU-before-board funnel, required host tools, and evidence interpretation.
- Write ignored runtime evidence only under `target/megrez-debug/browser-network-stability/`.

### Task 1: Split the physical network gate from desktop/browser readiness

**Files:**
- Modify: `tools/riscv/tests/test_megrez_gmac_gate.py`
- Modify: `tools/riscv/megrez_gmac_gate.py`

- [ ] **Step 1: Write focused failing target tests**

Freeze a `GateTarget` enum with `NETWORK` and `BROWSER`, and add tests requiring:

```python
config = GateConfig(target=GateTarget.NETWORK)
result = run_gate(config, FakeOperations(chunks=(complete_network_evidence(),)))
self.assertTrue(result["passed"])
self.assertEqual(result["target"], "network")
```

The transcript intentionally omits M4/M6. Add cases for split markers, missing,
duplicate, reversed, fatal-after-ready during drain, and a browser-default
regression. Require `_parse_args([...])` to accept only `network|browser`.

- [ ] **Step 2: Run RED**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_gmac_gate.MegrezGmacGateTests -v
```

Expected: failures because `GateTarget`, `--target`, and the network-only
classifier/wait condition do not exist.

- [ ] **Step 3: Implement the minimum target seam**

Keep the existing browser classifier unchanged. Add a pure network classifier
that applies the same byte cap and fatal scan, requires the selected-GMAC plus
M5 tuple exactly once and in order, and returns the existing frozen
`GateResult`. Select ready markers and classifier from `GateConfig.target`.
Record `target` in both successful and failed published JSON.

- [ ] **Step 4: Run GREEN and one regression gate**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_gmac_gate -v
make test_riscv_megrez_gmac_unit
python3 -m py_compile tools/riscv/megrez_gmac_gate.py \
  tools/riscv/tests/test_megrez_gmac_gate.py
ruff check tools/riscv/megrez_gmac_gate.py \
  tools/riscv/tests/test_megrez_gmac_gate.py
ruff format --check tools/riscv/megrez_gmac_gate.py \
  tools/riscv/tests/test_megrez_gmac_gate.py
git diff --check
```

- [ ] **Step 5: Commit**

```bash
git add tools/riscv/megrez_gmac_gate.py \
  tools/riscv/tests/test_megrez_gmac_gate.py
git commit -m "test(riscv): split Megrez network gate"
```

### Task 2: Add a deterministic bounded HTTP fixture

**Files:**
- Create: `tools/riscv/megrez_network_fixture.py`
- Create: `tools/riscv/tests/test_megrez_network_fixture.py`
- Modify: `Makefile`

- [ ] **Step 1: Write real-loopback RED tests**

Freeze `FixtureConfig`, `FixtureServer`, `PAYLOAD_SIZE=65536`, and one canonical
payload hash. Tests use `127.0.0.1` and an ephemeral port and require exact body,
content length, content type, 404 for other paths, optional peer allowlist,
at-most-64 request records, strictly monotonic timestamps, idempotent cleanup,
and no listener/thread residue. CLI rejects non-IP binds and invalid ports.

- [ ] **Step 2: Run RED**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_network_fixture -v
```

Expected: import failure because the module does not exist.

- [ ] **Step 3: Implement the fixture**

Use only `http.server.ThreadingHTTPServer`, a precomputed immutable payload,
bounded request records, daemon request threads, and explicit `shutdown`,
`server_close`, and join. The fixture never proxies arbitrary URLs. It serves
only `/asterinas-network-probe.bin` and exposes the selected endpoint and a
canonical JSON summary.

- [ ] **Step 4: Run GREEN and commit**

Run the focused test twice, py_compile, Ruff, format check, diff-check, and the
Megrez GMAC unit target once. Add the new test module to that Make target. Commit:

```bash
git commit -m "test(riscv): serve deterministic network fixture"
```

### Task 3: Require repeated verified guest transfers

**Files:**
- Modify: `tools/riscv/tests/test_debian_m5_network.py`
- Modify: `tools/riscv/debian/rootfs/desktop_m5_network_evidence.sh`
- Modify: `tools/riscv/debian/rootfs/desktop_m5_network_gate.py`

- [ ] **Step 1: Write shell-contract RED tests**

Extend the fake curl so physical mode receives a deterministic fixture URL and
returns exact fixed bytes. Require 20 fixture GETs before the existing clock,
Baidu HTTPS, and PNG calls; one wrong byte, short body, curl error, invalid URL,
or non-20 request count must emit one stable failure and no ready marker.
Require exactly one marker:

```text
DEBIAN_NETWORK_M5_STRESS requests=20 bytes=1310720 sha256=<fixed-sha256> endpoint=10.100.19.216:17894
```

- [ ] **Step 2: Run RED**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_debian_m5_network.DebianDesktopM5EvidenceTests -v
```

Expected: call-count and missing-marker failures.

- [ ] **Step 3: Implement bounded verification**

Read exact fixture URL/size/hash/count from systemd environment, validate every
field before network access, download each body to a private temporary file,
check `wc -c` and `sha256sum`, and delete it on every path. Each curl retains
the existing per-command deadline; the whole script retains its total deadline.
Emit the stress marker only after all 20 checks. Add that marker immediately
before the external HTTPS marker in `DESKTOP_M5_MEGREZ_MILESTONES`.

- [ ] **Step 4: Run GREEN and commit**

Run the focused evidence class, the full M5 network module, bash syntax, Ruff,
and `make test_riscv_debian_rootfs_unit` once. Commit:

```bash
git commit -m "test(riscv): stress Megrez browser transfers"
```

### Task 4: Own fixture lifecycle in QEMU and physical gates

**Files:**
- Modify: `tools/riscv/tests/test_megrez_gmac_gate.py`
- Modify: `tools/riscv/tests/test_debian_m5_network.py`
- Modify: `tools/riscv/megrez_gmac_gate.py`
- Modify: `tools/riscv/debian/rootfs/desktop_m5_qemu_gate.py`
- Modify: `Makefile`

- [ ] **Step 1: Write lifecycle RED tests**

Require fixture bind/listen before duplicate-address/serial work, propagate its
endpoint through volatile bootargs only, and close it on prepare, open, boot,
read, drain, signal, and publication failures. A port conflict must fail before
opening serial. QEMU receives the host endpoint through `10.0.2.2` while the
physical board receives `10.100.19.216`.

- [ ] **Step 2: Run RED**

Run only new lifecycle tests. Expected: no fixture ownership or environment
bootargs exist.

- [ ] **Step 3: Implement one ownership boundary**

Create the fixture in each gate's outer context, pass only validated scalar
identity to the guest, and publish its request summary next to serial/result
evidence. Preserve the physical proxy on port 17893; use fixture port 17894.
Never stop or reconfigure the existing Clash/socat proxy.

- [ ] **Step 4: Run GREEN and commit**

Run both affected full unittest modules twice, the two Make unit targets once,
static checks, and a listener/thread residue check. Commit:

```bash
git commit -m "test(riscv): bind browser gates to network fixture"
```

### Task 5: Run the simulation funnel

**Files:**
- Runtime output only: `target/megrez-debug/browser-network-stability/qemu-*`

- [ ] **Step 1: Verify artifact identity**

Record HEAD and SHA-256/size for kernel, U-Boot, 4-hart generic-Sv39 DTB,
Stage1 initramfs, Debian ext2 image, manifest, lock, and checksums. Reject the
legacy one-hart or Sv48 DTB before QEMU.

- [ ] **Step 2: Run focused kernel tests at SMP=4**

Run the cached RISC-V OSDK checks and the focused ARP/network ktests. No network
download is permitted after the cache preflight. Expected: all selected tests
pass with QEMU `-smp 4` and no panic.

- [ ] **Step 3: Run the complete M5 QEMU gate three times**

Use the same frozen artifacts and a fresh output directory for each run. Each
must contain the exact 20-request stress marker, external HTTPS ready evidence,
and a fixture request count of 20. Reject a run if transcript/result identity or
hashes differ from the frozen input set.

- [ ] **Step 4: Record the checkpoint**

Write a concise evidence Markdown file under the ignored output directory with
the exact commands, durations, hashes, result paths, and pass/fail status. Do
not commit generated artifacts.

### Task 6: Run one bounded physical network-only experiment

**Files:**
- Runtime output only: `target/megrez-debug/browser-network-stability/physical`
- Modify after evidence: `tools/riscv/README.md`

- [ ] **Step 1: Preflight without touching the board**

Require the stable serial by-id path, host interface link via `ethtool`, no
duplicate `10.100.19.200` via the existing arping contract, fixture port 17894
free, proxy port 17893 listening, frozen artifact hashes, and sufficient output
space. Start `tcpdump` with board-only BPF and a bounded capture size.

- [ ] **Step 2: Execute exactly one network target boot**

Use MMC transport, `--target network`, a 180-second host boot deadline, and
`--reboot-after 240`. Do not wait for Xorg, NetSurf, M4, M6, or M7. Do not ask
for a manual reset unless the automatic reboot and serial recovery both fail.

- [ ] **Step 3: Classify serial and packet evidence**

Use `tshark` to report TCP handshakes, retransmissions, zero-window events, and
HTTP request/response count. Require 20 deterministic responses plus the Baidu
canary, no ARP queue-full warning, zero MTL RX FIFO overflow, zero DMA receive
buffer-unavailable count, no fatal bus error, and a passing network result.

- [ ] **Step 4: Document and commit**

Update `tools/riscv/README.md` with the exact QEMU-first and physical commands,
stable serial path, installed host tools, network/browser target distinction,
and how to interpret the evidence. Run link/source checks and commit:

```bash
git commit -m "docs(riscv): document Megrez network stability gate"
```

### Task 7: Decide the next user-visible compatibility slice

- [ ] **Step 1: If physical network passes**

Freeze the network milestone and stop changing DWMAC unless new packet/counter
evidence proves a regression. Start a separate NetSurf compatibility plan for
HTML/image/TLS rendering and desktop startup diagnostics; do not mix it into
the network gate.

- [ ] **Step 2: If physical network fails**

Classify only from evidence: host fixture/request absence => TX/ARP path; TCP
retransmit with zero hardware drops => TCP timer/ACK path; nonzero MTL/DMA loss
=> RX descriptor/interrupt path. Add one focused simulation reproducer before
any second board boot.

