# Network Stack Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate the isolated IPv6 and IPv4-mapped dual-stack socket work into current `origin/main`, prove it on x86-64 and RISC-V QEMU with fail-closed tests, preserve the frozen desktop/Firefox baseline, and qualify the result on the Megrez board without redeploying or reimaging MMC.

**Architecture:** Keep address-family policy in the kernel socket layer and dual-stack packet/port behavior in `aster-bigtcp`. AF_INET6 UDP/TCP sockets carry their family and `IPV6_V6ONLY` state; IPv6 wildcard sockets optionally reserve and receive the corresponding IPv4 namespace, while IPv4 packets are mapped at ingress and demapped at egress. A bounded guest runner emits three independent success facts, and a host validator checks the entire QEMU transcript for exactly one of each fact and for fatal output. Physical qualification uses YMODEM for the new kernel/initramfs and leaves the known-good MMC boot files and Debian root image in place; ordinary runtime filesystem writes by the existing Debian gate are not an image deployment.

**Tech Stack:** Rust 2024, Asterinas kernel and `aster-bigtcp`, C regression programs, Python `unittest`, GNU Make, persistent Docker launcher, QEMU x86-64/RISC-V, Megrez RISC-V board.

---

## Fixed Inputs and Safety Boundaries

- Work only in `/home/ubuntu/.config/superpowers/worktrees/asterinas/network-stack-integration` on `codex/network-stack-integration`.
- The starting point is `origin/main@e4eaf7b2d3abd81f454ec2b8b6372df024fd80d1` plus design commit `3bfa2cc53`.
- Adapt the networking logic from `3ef0ef594`, `285b86443`, `94b099ca5`, `22a15cb99`, and `5b4ee17a9`; do not cherry-pick whole commits.
- Treat `a94151c6e` as a hypothesis only. Current main already prefaults socket buffers in syscall context, so add a UDP regression first and do not add a bounce buffer unless that regression fails for a kernel-lock page fault.
- Exclude `b42c3da3e`, `dd0a80d7a`, `6f1d6c550`, and `d88a412f2`.
- Do not modify `tools/riscv/debian/rootfs/browser_*`, the frozen Firefox/desktop fixture contract, partition 1, partition 2, or the DWMAC bring-up path.
- Use `tools/docker/run_dev_container.sh -- ...` for every build/test. Reuse the named container and persistent caches; do not remove an image/container/cache and do not install `cargo-osdk` interactively.
- Never force-push. Do not update remote `main` until every required gate below passes.

## File Map

### Host-side bounded gate

- Modify `Makefile` to add `AUTO_TEST=ipv6_dual_stack` and `AUTO_TEST=udp_user_buffer_prefault`, and to validate the complete QEMU transcript.
- Modify `tools/riscv/validate_run_kernel_log.py` to support the three-marker dual-stack contract.
- Modify `tools/riscv/tests/test_validate_run_kernel_log.py` to test acceptance, missing/duplicate facts, fatal-after-success rejection, and build/guest wiring.
- Add `test/initramfs/src/regression/network/run_dual_stack_test.sh` as the bounded guest runner.
- Add `test/initramfs/src/regression/scripts/run_ipv6_udp_test.sh` as the native-IPv6 single-test runner.
- Add `test/initramfs/src/regression/scripts/run_ipv6_dual_stack_udp_test.sh` as the mapped-UDP single-test runner.
- Add `test/initramfs/src/regression/scripts/run_udp_user_buffer_prefault_test.sh` as the single-regression runner.

### Socket API and options

- Modify `kernel/src/syscall/socket.rs` to allow AF_INET6 datagram sockets and pass `IpAddressFamily` into UDP construction.
- Modify `kernel/src/net/socket/ip/datagram/mod.rs` and `kernel/src/net/socket/ip/datagram/unbound.rs` to retain the family, enforce it, carry `IPV6_V6ONLY`, and pass dual-stack bind policy downward.
- Modify `kernel/src/net/socket/ip/stream/mod.rs`, `kernel/src/net/socket/ip/stream/init.rs`, and `kernel/src/net/socket/ip/stream/listen.rs` to retain/inherit family and IPv6 options and pass `v6only` to lower-layer operations.
- Modify `kernel/src/net/socket/ip/options.rs` to add the IPv6 option set with a dual-stack default.
- Add `kernel/src/util/net/options/ipv6.rs` and modify `kernel/src/util/net/options/mod.rs` for `SOL_IPV6`/`IPV6_V6ONLY` raw option conversion.

### UDP dual-stack data path

- Modify `kernel/src/net/socket/util/datagram_common.rs` and Netlink call sites under `kernel/src/net/socket/netlink/common/` for the extended bind-options flow.
- Modify `kernel/src/net/iface/init.rs` so all interfaces in one network namespace share one UDP registry and a new network namespace receives a fresh registry.
- Modify `kernel/libs/aster-bigtcp/src/socket_table.rs` to add the weak-reference UDP registry and cross-family port conflict checks.
- Modify `kernel/libs/aster-bigtcp/src/iface/common.rs`, `kernel/libs/aster-bigtcp/src/iface/phy/ip.rs`, and `kernel/libs/aster-bigtcp/src/iface/phy/ether.rs` to carry the per-namespace registry.
- Modify `kernel/libs/aster-bigtcp/src/socket/bound/udp.rs` to reserve/release the dual IPv4 port and record whether an IPv6 wildcard socket accepts IPv4.
- Modify `kernel/libs/aster-bigtcp/src/iface/poll.rs` for mapped IPv4 UDP ingress, egress demapping, and unit tests for source-address selection.

### TCP dual-stack data path

- Modify `kernel/libs/aster-bigtcp/src/socket/bound/tcp_listen.rs` to reserve/release the IPv4 wildcard port for a dual IPv6 listener.
- Modify `kernel/libs/aster-bigtcp/src/socket_table.rs` for mapped connection-key normalization and dual wildcard listener lookup.
- Modify `kernel/libs/aster-bigtcp/src/iface/common.rs` for wildcard cross-family port conflicts.
- Modify `kernel/libs/aster-bigtcp/src/iface/poll.rs` for mapped TCP ingress and IPv4 reply demapping.

### Guest regressions

- Add `test/initramfs/src/regression/network/ipv6_udp.c`.
- Add `test/initramfs/src/regression/network/ipv6_dual_stack_udp.c`.
- Add `test/initramfs/src/regression/network/ipv6_dual_stack.c`.
- Add `test/initramfs/src/regression/network/udp_user_buffer_prefault.c`.
- Modify `test/initramfs/src/regression/network/run_test.sh` by appending the selected tests without replacing current-main regressions.

## Task 1: Install a Fail-Closed Dual-Stack Gate

**Files:**

- Modify: `tools/riscv/tests/test_validate_run_kernel_log.py`
- Modify: `tools/riscv/validate_run_kernel_log.py`
- Modify: `Makefile`
- Add: `test/initramfs/src/regression/network/run_dual_stack_test.sh`

- [x] **Step 1: Add failing validator tests**

Add tests that call `validate_transcript(..., mode="ipv6-dual-stack")` with these logical facts:

```text
ASTERINAS_IPV6_DUAL_STACK_TCP_OK peer=::ffff:127.0.0.1
ipv6_udp: PASS
ASTERINAS_IPV6_DUAL_STACK_UDP_OK peer=::ffff:127.0.0.1
```

Cover all of these cases:

1. exactly one of each fact is accepted;
2. each fact missing in turn is rejected;
3. each fact duplicated in turn is rejected;
4. a fatal line before or after all success facts is rejected;
5. the Makefile selects `/test/network/run_dual_stack_test.sh` and invokes `--mode "ipv6-dual-stack"`;
6. the guest runner invokes `ipv6_dual_stack`, `ipv6_udp`, and `ipv6_dual_stack_udp` exactly once.

- [x] **Step 2: Run the test and confirm it fails for the missing mode**

Run:

```bash
tools/docker/run_dev_container.sh -- python3 -W error::ResourceWarning -m unittest tools.riscv.tests.test_validate_run_kernel_log -v
```

Expected: the new tests fail because `ipv6-dual-stack` is not an accepted validator mode and the Makefile/runner are not wired.

- [x] **Step 3: Implement multi-fact validation**

In `tools/riscv/validate_run_kernel_log.py`, add:

```python
MULTI_FACT_MARKERS = {
    "ipv6-dual-stack": (
        "ASTERINAS_IPV6_DUAL_STACK_TCP_OK",
        "ipv6_udp: PASS",
        "ASTERINAS_IPV6_DUAL_STACK_UDP_OK",
    ),
}


def _is_logical_marker(line: str, marker: str) -> bool:
    return line == marker or line.startswith(f"{marker} ")
```

For a multi-fact mode, require exactly one matching line for every marker. Keep the existing fatal-pattern scan over the complete transcript and keep the SMP4 contract restricted to regression mode. Set argparse choices to the union of `SUCCESS_MARKERS` and `MULTI_FACT_MARKERS`.

- [x] **Step 4: Add the bounded guest and Makefile entry**

Create an executable `run_dual_stack_test.sh` with `set -e`, `cd "$(dirname "$0")"`, and these commands in order:

```sh
./ipv6_dual_stack
./ipv6_udp
./ipv6_dual_stack_udp
```

In the option-selection section of `Makefile`, set `ENABLE_REGRESSION_TEST := true` and `--init-args="/test/network/run_dual_stack_test.sh"` for `AUTO_TEST=ipv6_dual_stack`. In the `run_kernel` validation section, invoke the full-transcript validator with `--mode "ipv6-dual-stack"`.

- [x] **Step 5: Run the validator tests and static checks**

Run:

```bash
chmod +x test/initramfs/src/regression/network/run_dual_stack_test.sh
tools/docker/run_dev_container.sh -- python3 -W error::ResourceWarning -m unittest tools.riscv.tests.test_validate_run_kernel_log -v
git diff --check
```

Expected: validator tests pass; `git diff --check` prints nothing.

- [x] **Step 6: Commit the gate separately**

```bash
git add Makefile tools/riscv/validate_run_kernel_log.py tools/riscv/tests/test_validate_run_kernel_log.py test/initramfs/src/regression/network/run_dual_stack_test.sh
git commit -m "test(net): add bounded dual-stack QEMU gate" -m "Adapted from source commit 5b4ee17a9 with current-main full-transcript validation."
```

## Task 2: Add AF_INET6 UDP and Linux-Compatible `IPV6_V6ONLY`

**Files:**

- Test: `test/initramfs/src/regression/network/ipv6_udp.c`
- Modify: `test/initramfs/src/regression/network/run_test.sh`
- Modify: `kernel/src/syscall/socket.rs`
- Modify: `kernel/src/net/socket/ip/datagram/mod.rs`
- Modify: `kernel/src/net/socket/ip/stream/mod.rs`
- Modify: `kernel/src/net/socket/ip/options.rs`
- Add: `kernel/src/util/net/options/ipv6.rs`
- Modify: `kernel/src/util/net/options/mod.rs`
- Modify: `tools/riscv/validate_run_kernel_log.py`
- Modify: `tools/riscv/tests/test_validate_run_kernel_log.py`
- Modify: `Makefile`
- Add: `test/initramfs/src/regression/scripts/run_ipv6_udp_test.sh`

- [x] **Step 1: Add the native IPv6 regression first**

Adapt `ipv6_udp.c` from `3ef0ef594`, with these assertions tightened for the approved contract:

```c
int value = -1;
socklen_t value_len = sizeof(value);
CHECK(getsockopt(fd, IPPROTO_IPV6, IPV6_V6ONLY, &value, &value_len) == 0);
CHECK(value == 0);
value = 1;
CHECK(setsockopt(fd, IPPROTO_IPV6, IPV6_V6ONLY, &value, sizeof(value)) == 0);
value = -1;
CHECK(getsockopt(fd, IPPROTO_IPV6, IPV6_V6ONLY, &value, &value_len) == 0);
CHECK(value == 1);
value = 0;
CHECK(setsockopt(fd, IPPROTO_IPV6, IPV6_V6ONLY, &value, sizeof(value)) == 0);
```

Then bind receiver and sender to `::1`, send one datagram, verify payload and IPv6 peer address, and print exactly `ipv6_udp: PASS`. Append `./ipv6_udp` to the existing general network runner without deleting its current tests.

Add `"ipv6-udp": "ipv6_udp: PASS"` to `SUCCESS_MARKERS`, cover acceptance/missing/duplicate/fatal-after behavior in the validator unit tests, and add `AUTO_TEST=ipv6_udp` using `/test/run_ipv6_udp_test.sh` plus full-transcript validation mode `ipv6-udp`.

- [x] **Step 2: Run the bounded gate and record the expected API failure**

Run:

```bash
mkdir -p target/network-stack-integration/qemu-x86_64
tools/docker/run_dev_container.sh -- bash -lc 'ASTERINAS_QEMU_LOG_DIR="$PWD/target/network-stack-integration/qemu-x86_64" timeout --foreground 600s make run_kernel AUTO_TEST=ipv6_udp TARGET_ARCH=x86_64 SMP=4'
```

Expected: the guest fails before `ipv6_udp: PASS` because AF_INET6/SOCK_DGRAM or `IPV6_V6ONLY` is unsupported. Preserve `qemu.log` as the red-test evidence.

- [x] **Step 3: Wire the raw IPv6 option**

Add `V6Only(bool)` in `kernel/src/util/net/options/ipv6.rs`. Dispatch level `SOL_IPV6` in the raw option layer and return `ENOPROTOOPT` for unknown IPv6 options. Add `IpV6OptionSet { v6only: bool }` to UDP and TCP option sets, defaulting `v6only` to `false`, and route get/set operations through it.

- [x] **Step 4: Carry the address family in both socket types**

Make `DatagramSocket::new` take `IpAddressFamily`, store it, and construct AF_INET6 UDP sockets in `sys_socket`. Store `IpAddressFamily` directly in `StreamSocket` as well so accepted sockets inherit the listener family rather than reconstructing it from an endpoint. Reject native address-family mismatches with `EAFNOSUPPORT`; when `v6only` is true, reject mapped IPv4 endpoints before bind/connect/send.

- [x] **Step 5: Run native IPv6 on x86-64 and RISC-V**

Run the dedicated native-IPv6 gate and both architecture builds:

```bash
tools/docker/run_dev_container.sh -- bash -lc 'ASTERINAS_QEMU_LOG_DIR="$PWD/target/network-stack-integration/qemu-x86_64" timeout --foreground 600s make run_kernel AUTO_TEST=ipv6_udp TARGET_ARCH=x86_64 SMP=4'
tools/docker/run_dev_container.sh -- bash -lc 'mkdir -p target/network-stack-integration/native-ipv6-riscv64 && ASTERINAS_QEMU_LOG_DIR="$PWD/target/network-stack-integration/native-ipv6-riscv64" timeout --foreground 600s make run_kernel AUTO_TEST=ipv6_udp TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode'
tools/docker/run_dev_container.sh -- make kernel TARGET_ARCH=x86_64 SMP=4
tools/docker/run_dev_container.sh -- make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
git diff --check
```

Expected: both gates exit zero, each transcript contains `ipv6_udp: PASS` exactly once with no fatal signature, and both kernels build.

- [x] **Step 6: Commit the API slice**

```bash
git add Makefile tools/riscv/validate_run_kernel_log.py tools/riscv/tests/test_validate_run_kernel_log.py kernel/src/syscall/socket.rs kernel/src/net/socket/ip/datagram/mod.rs kernel/src/net/socket/ip/stream/mod.rs kernel/src/net/socket/ip/options.rs kernel/src/util/net/options/ipv6.rs kernel/src/util/net/options/mod.rs test/initramfs/src/regression/network/ipv6_udp.c test/initramfs/src/regression/network/run_test.sh test/initramfs/src/regression/scripts/run_ipv6_udp_test.sh
git commit -m "feat(net): add AF_INET6 UDP sockets and v6only" -m "Adapted from source commit 3ef0ef594; IPV6_V6ONLY defaults to Linux-compatible dual-stack behavior."
```

## Task 3: Prove UDP User Buffers Are Prefaulted Before Network Locks

**Files:**

- Add: `test/initramfs/src/regression/network/udp_user_buffer_prefault.c`
- Add: `test/initramfs/src/regression/scripts/run_udp_user_buffer_prefault_test.sh`
- Modify: `test/initramfs/src/regression/network/run_test.sh`
- Modify: `Makefile`
- Modify: `tools/riscv/validate_run_kernel_log.py`
- Modify: `tools/riscv/tests/test_validate_run_kernel_log.py`

- [x] **Step 1: Add a page-fault-sensitive UDP regression**

Create a loopback UDP receiver and sender. Allocate a `4 * 4096 + 137` byte anonymous mapping for send and receive buffers and do not touch either mapping before the syscall. Send the zero-filled mapping with `sendto`, receive it with `recvfrom`, verify the complete byte count and zero contents, enforce a ten-second alarm, and print exactly:

```text
UDP user buffer prefault regression passed.
```

This exercises both read-prefault and write-prefault paths while the actual copy still occurs below the syscall layer.

- [x] **Step 2: Add a single-test runner and Makefile gate**

The runner executes only `/test/network/udp_user_buffer_prefault`. Add `"udp-user-buffer-prefault": "UDP user buffer prefault regression passed."` to `SUCCESS_MARKERS`, add validator unit tests for acceptance/missing/duplicate/fatal-after behavior, and add `AUTO_TEST=udp_user_buffer_prefault` using full-transcript validation mode `udp-user-buffer-prefault`. Also append the binary to the general network runner.

- [x] **Step 3: Run the focused test before importing bounce-buffer code**

Run on both architectures:

```bash
mkdir -p target/network-stack-integration/prefault-x86_64 target/network-stack-integration/prefault-riscv64
tools/docker/run_dev_container.sh -- bash -lc 'ASTERINAS_QEMU_LOG_DIR="$PWD/target/network-stack-integration/prefault-x86_64" timeout --foreground 600s make run_kernel AUTO_TEST=udp_user_buffer_prefault TARGET_ARCH=x86_64 SMP=4'
tools/docker/run_dev_container.sh -- bash -lc 'ASTERINAS_QEMU_LOG_DIR="$PWD/target/network-stack-integration/prefault-riscv64" timeout --foreground 600s make run_kernel AUTO_TEST=udp_user_buffer_prefault TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode'
```

Expected: both commands exit zero and the full-line marker appears once. If they pass, explicitly keep `a94151c6e` out of the branch. If either fails with a page fault or atomic-context panic, stop this task, capture the full transcript, and make the smallest prefault correction at the syscall boundary before reconsidering any copy buffer.

- [x] **Step 4: Commit the characterization test**

```bash
git add Makefile tools/riscv/validate_run_kernel_log.py tools/riscv/tests/test_validate_run_kernel_log.py test/initramfs/src/regression/network/udp_user_buffer_prefault.c test/initramfs/src/regression/network/run_test.sh test/initramfs/src/regression/scripts/run_udp_user_buffer_prefault_test.sh
git commit -m "test(net): cover UDP user buffer prefault"
```

## Task 4: Add IPv4-Mapped UDP Receive, Reply, and Port Ownership

**Files:**

- Test: `test/initramfs/src/regression/network/ipv6_dual_stack_udp.c`
- Modify: `kernel/libs/aster-bigtcp/src/socket_table.rs`
- Modify: `kernel/libs/aster-bigtcp/src/iface/common.rs`
- Modify: `kernel/libs/aster-bigtcp/src/iface/phy/ip.rs`
- Modify: `kernel/libs/aster-bigtcp/src/iface/phy/ether.rs`
- Modify: `kernel/libs/aster-bigtcp/src/iface/poll.rs`
- Modify: `kernel/libs/aster-bigtcp/src/socket/bound/udp.rs`
- Modify: `kernel/src/net/iface/init.rs`
- Modify: `kernel/src/net/socket/ip/datagram/mod.rs`
- Modify: `kernel/src/net/socket/ip/datagram/unbound.rs`
- Modify: `kernel/src/net/socket/util/datagram_common.rs`
- Modify: `kernel/src/net/socket/netlink/common/mod.rs`
- Modify: `kernel/src/net/socket/netlink/common/unbound.rs`
- Modify: `tools/riscv/validate_run_kernel_log.py`
- Modify: `tools/riscv/tests/test_validate_run_kernel_log.py`
- Modify: `Makefile`
- Add: `test/initramfs/src/regression/scripts/run_ipv6_dual_stack_udp_test.sh`

- [ ] **Step 1: Add the failing mapped-UDP regression**

Adapt the final version from `94b099ca5`. The AF_INET6 receiver must bind `[::]:0` with `IPV6_V6ONLY=0`; an AF_INET sender must send from `127.0.0.1`; the receiver must observe peer `::ffff:127.0.0.1`, reply to that mapped address, and the IPv4 sender must receive and verify the reply. Add a second case where `IPV6_V6ONLY=1` prevents IPv4 delivery and allows a separate IPv4 wildcard bind to the same port. Emit one success line beginning:

```text
ASTERINAS_IPV6_DUAL_STACK_UDP_OK peer=
```

Add validator mode `ipv6-dual-stack-udp` for the logical marker `ASTERINAS_IPV6_DUAL_STACK_UDP_OK`, cover its single/missing/duplicate/fatal cases, and add `AUTO_TEST=ipv6_dual_stack_udp` with a runner that executes only this binary.

- [ ] **Step 2: Run the x86 mapped-UDP gate and confirm it is red**

Run:

```bash
mkdir -p target/network-stack-integration/mapped-udp-x86_64
tools/docker/run_dev_container.sh -- bash -lc 'ASTERINAS_QEMU_LOG_DIR="$PWD/target/network-stack-integration/mapped-udp-x86_64" timeout --foreground 600s make run_kernel AUTO_TEST=ipv6_dual_stack_udp TARGET_ARCH=x86_64 SMP=4'
```

Expected: the mapped UDP fact is absent or the guest reports its assertion failure.

- [ ] **Step 3: Add a network-namespace-scoped UDP registry**

Implement `UdpSocketRegistry<E>` in `aster-bigtcp` with weak references and a dual-stack port set. Every interface inside one `NetNamespace` must receive the same `Arc<UdpSocketRegistry<_>>`; `new_ns_loopback()` must receive a fresh registry. Clean dead weak references during lookup. Do not use a process-global registry.

- [ ] **Step 4: Reserve the matching IPv4 namespace for dual wildcard binds**

Extend UDP bind options with `v6only`. For an AF_INET6 wildcard bind to `::` with `v6only=false`, set `accepts_ipv4`, reserve the IPv4 wildcard port in the registry, and release it in `Drop`. Include registry conflicts in ephemeral selection and explicit bind. Keep IPv6-only sockets independent from IPv4 ownership.

- [ ] **Step 5: Map ingress and demap egress**

At IPv4 UDP ingress, try the normal IPv4 table first, then query the namespace UDP registry and present the peer to a dual IPv6 socket as an IPv4-mapped IPv6 address. At egress, recognize mapped IPv4 destinations, construct an IPv4 UDP/IP representation, choose an IPv4 source from the socket's bound endpoint or the selected interface, and preserve checksums/ports. Add lower-layer unit tests for:

- mapped destination with an explicit mapped source;
- mapped destination with IPv6 wildcard source, selecting the interface IPv4 address;
- mapped loopback destination, selecting `127.0.0.1`;
- non-mapped IPv6 destination, leaving the representation unchanged.

- [ ] **Step 6: Run focused lower-layer and QEMU tests**

Run:

```bash
tools/docker/run_dev_container.sh -- cargo test -p aster-bigtcp
tools/docker/run_dev_container.sh -- bash -lc 'ASTERINAS_QEMU_LOG_DIR="$PWD/target/network-stack-integration/mapped-udp-x86_64" timeout --foreground 600s make run_kernel AUTO_TEST=ipv6_dual_stack_udp TARGET_ARCH=x86_64 SMP=4'
tools/docker/run_dev_container.sh -- bash -lc 'mkdir -p target/network-stack-integration/mapped-udp-riscv64 && ASTERINAS_QEMU_LOG_DIR="$PWD/target/network-stack-integration/mapped-udp-riscv64" timeout --foreground 600s make run_kernel AUTO_TEST=ipv6_dual_stack_udp TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode'
```

Expected: lower-layer tests pass and both mapped-UDP gates exit zero with exactly one mapped UDP fact and no fatal signature.

- [ ] **Step 7: Commit UDP dual-stack support**

```bash
git add Makefile tools/riscv/validate_run_kernel_log.py tools/riscv/tests/test_validate_run_kernel_log.py kernel/libs/aster-bigtcp kernel/src/net/iface/init.rs kernel/src/net/socket/ip/datagram kernel/src/net/socket/util/datagram_common.rs kernel/src/net/socket/netlink/common test/initramfs/src/regression/network/ipv6_dual_stack_udp.c test/initramfs/src/regression/scripts/run_ipv6_dual_stack_udp_test.sh
git commit -m "feat(net): support IPv4-mapped UDP sockets" -m "Adapted from source commits 285b86443 and 94b099ca5 for current network namespaces."
```

## Task 5: Add IPv4-Mapped TCP Listener and Connection Handling

**Files:**

- Test: `test/initramfs/src/regression/network/ipv6_dual_stack.c`
- Modify: `kernel/libs/aster-bigtcp/src/iface/common.rs`
- Modify: `kernel/libs/aster-bigtcp/src/iface/poll.rs`
- Modify: `kernel/libs/aster-bigtcp/src/socket/bound/tcp_listen.rs`
- Modify: `kernel/libs/aster-bigtcp/src/socket_table.rs`
- Modify: `kernel/src/net/socket/ip/stream/init.rs`
- Modify: `kernel/src/net/socket/ip/stream/listen.rs`
- Modify: `kernel/src/net/socket/ip/stream/mod.rs`

- [ ] **Step 1: Add the failing mapped-TCP regression**

Adapt `ipv6_dual_stack.c` from `22a15cb99`. Bind an AF_INET6 listener to `[::]:0` with `IPV6_V6ONLY=0`, assert that an AF_INET wildcard bind to the same port fails with `EADDRINUSE`, connect via `127.0.0.1`, verify the accepted peer is `::ffff:127.0.0.1`, exchange data in both directions, and emit one success line beginning:

```text
ASTERINAS_IPV6_DUAL_STACK_TCP_OK peer=
```

Add a second case proving `IPV6_V6ONLY=1` allows the separate IPv4 bind and does not accept the IPv4 connection.

- [ ] **Step 2: Run the bounded x86 gate and confirm TCP is the remaining failure**

Run the x86 dual-stack command. Expected: the first TCP guest assertion fails and the full-transcript validator rejects the run because the TCP fact is absent; retain that transcript as the red test.

- [ ] **Step 3: Reserve ports and normalize connection keys**

For an IPv6 wildcard TCP listener with `v6only=false`, reserve the corresponding IPv4 wildcard port and release it with the listener. Normalize IPv4 TCP connection keys to IPv4-mapped IPv6 keys for dual sockets while preserving ordinary IPv4 socket behavior. Lookup order must be exact endpoint, same-family wildcard, then IPv6 dual wildcard fallback.

- [ ] **Step 4: Map TCP ingress and demap replies**

When an IPv4 SYN targets a dual IPv6 wildcard listener, present local and remote endpoints as mapped IPv6 inside the socket. When that connection transmits, demap both endpoints into an IPv4 representation before routing and packet emission. Carry `v6only` from `StreamSocket` through `InitStream::listen`; accepted sockets inherit family and IPv6 options from the listener.

- [ ] **Step 5: Pass the complete dual-stack gate on both architectures**

Run:

```bash
tools/docker/run_dev_container.sh -- cargo test -p aster-bigtcp
tools/docker/run_dev_container.sh -- bash -lc 'ASTERINAS_QEMU_LOG_DIR="$PWD/target/network-stack-integration/qemu-x86_64" timeout --foreground 600s make run_kernel AUTO_TEST=ipv6_dual_stack TARGET_ARCH=x86_64 SMP=4'
tools/docker/run_dev_container.sh -- bash -lc 'ASTERINAS_QEMU_LOG_DIR="$PWD/target/network-stack-integration/qemu-riscv64" timeout --foreground 600s make run_kernel AUTO_TEST=ipv6_dual_stack TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode'
```

Expected for each QEMU transcript: exactly one TCP mapped fact, one native IPv6 fact, one UDP mapped fact, no fatal pattern, and process exit status zero.

- [ ] **Step 6: Commit TCP dual-stack support**

```bash
git add kernel/libs/aster-bigtcp/src/iface/common.rs kernel/libs/aster-bigtcp/src/iface/poll.rs kernel/libs/aster-bigtcp/src/socket/bound/tcp_listen.rs kernel/libs/aster-bigtcp/src/socket_table.rs kernel/src/net/socket/ip/stream test/initramfs/src/regression/network/ipv6_dual_stack.c
git commit -m "feat(net): support IPv4-mapped TCP listeners" -m "Adapted from source commit 22a15cb99 for current socket tables and stream state."
```

## Task 6: Validate Architecture, Scope, and Existing Regressions

**Files:** all files changed by Tasks 1-5.

- [ ] **Step 1: Check the source boundary**

Run:

```bash
git diff --name-only origin/main...HEAD
git diff --stat origin/main...HEAD
```

Expected: only the design/plan, socket/network implementation, bounded test infrastructure, and selected guest regressions appear. No frozen rootfs/browser fixture, DWMAC, block/MMC, or deployment file appears.

- [ ] **Step 2: Run formatting and compile checks without broad mechanical rewrites**

Run checks first:

```bash
tools/docker/run_dev_container.sh -- cargo fmt --all -- --check
tools/docker/run_dev_container.sh -- cargo clippy -p aster-bigtcp --all-targets -- -D warnings
tools/docker/run_dev_container.sh -- clang-format --dry-run --Werror test/initramfs/src/regression/network/ipv6_udp.c test/initramfs/src/regression/network/ipv6_dual_stack_udp.c test/initramfs/src/regression/network/ipv6_dual_stack.c test/initramfs/src/regression/network/udp_user_buffer_prefault.c
tools/docker/run_dev_container.sh -- make kernel TARGET_ARCH=x86_64 SMP=4
tools/docker/run_dev_container.sh -- make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
git diff --check
```

If formatting fails, run the repository formatter only on the changed Rust/C files, inspect its diff, and rerun the checks. Do not accept unrelated formatting churn.

- [ ] **Step 3: Re-run every focused automated gate from clean log directories**

Remove only the task-specific QEMU log directories under `target/network-stack-integration`, recreate them, then run both architecture commands for native IPv6, UDP prefault, mapped UDP, and the complete dual-stack gate from Tasks 2-5. Never remove Cargo/Rustup/Nix/Docker caches.

Expected: all eight commands exit zero; each transcript satisfies its exact marker contract.

- [ ] **Step 4: Run the broader network regression where bounded**

Run x86-64 network/regression coverage through the existing project target. On RISC-V, retain the bounded dual-stack gate if the unrelated full regression is known to block on the existing SCM-rights limitation; do not weaken or skip any dual-stack assertion to make it pass.

- [ ] **Step 5: Commit any test-only correction**

If Step 1-4 required a scoped correction, stage only those files and commit it with a message describing the actual correction. If the tree is unchanged, do not create an empty commit.

## Task 7: Re-Prove the Frozen Desktop/Firefox Baseline

**Files:** no source modifications expected.

- [ ] **Step 1: Satisfy the known local fixture precondition**

```bash
mkdir -p target/current-main-physical-graphics/physical
```

This directory is ignored and disposable. Do not change tests to hide the precondition during the network integration.

- [ ] **Step 2: Run the exact 427-test baseline matrix**

```bash
tools/docker/run_dev_container.sh -- python3 -W error::ResourceWarning -m unittest \
 tools.riscv.tests.test_debian_browser_web.BrowserWebContractTests \
 tools.riscv.tests.test_megrez_proxy_bridge \
 tools.riscv.tests.test_megrez_firefox_browse \
 tools.riscv.tests.test_megrez_desktop \
 tools.riscv.tests.test_megrez_gmac_gate \
 tools.riscv.tests.test_megrez_boot_stability \
 tools.riscv.tests.test_megrez_physical_graphics \
 tools.riscv.tests.test_debian_rootfs -q
```

Expected: `Ran 427 tests` and `OK`, with ResourceWarning promoted to an error.

- [ ] **Step 3: Perform a normal code review**

Review `git diff origin/main...HEAD` directly against the repository's maintainability, development, security, hardware, and documentation guidelines. Verify lock ordering, weak-reference cleanup, namespace isolation, port release on every error/drop path, endpoint-family checks, and absence of `unsafe` in `kernel/`. Do not invoke the retired `aster-code-review` skill and do not generate its report artifact.

- [ ] **Step 4: Fix findings with regression coverage**

For each real defect, first add or tighten the smallest failing unit/guest test, then apply the fix, rerun the affected focused gate, and commit the fix separately. If there are no findings, leave the branch unchanged.

## Task 8: Qualify the New Kernel on Megrez Without MMC Redeployment

**Files:** no tracked source modifications expected; evidence goes under ignored `target/network-stack-integration/physical/`.

- [ ] **Step 1: Build and fingerprint physical artifacts**

Build the RISC-V kernel through the persistent launcher, then stage the new kernel beside the already-proven Stage1 initramfs. Use the following exact preparation; it copies no root image and does not access a block device:

```bash
tools/docker/run_dev_container.sh -- make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
PHYSICAL_DIR="$PWD/target/network-stack-integration/physical"
YMODEM_DIR="$PHYSICAL_DIR/ymodem"
FROZEN_INPUTS=/home/ubuntu/.config/superpowers/worktrees/asterinas/megrez-physical-graphics-current-main/target/current-main-physical-graphics/physical/inputs
mkdir -p "$YMODEM_DIR"
install -m 0644 target/osdk/kernel.Image "$YMODEM_DIR/asterinas-network-stack.booti"
xz --format=lzma -9 --force --keep "$YMODEM_DIR/asterinas-network-stack.booti"
install -m 0644 "$FROZEN_INPUTS/initramfs-isolated-resolved.cpio" "$YMODEM_DIR/asterinas-network-stack-stage1.cpio"
```

Set exact artifact facts with a read-only helper:

```bash
crc32_file() { python3 -c 'import pathlib,sys,zlib; print(f"{zlib.crc32(pathlib.Path(sys.argv[1]).read_bytes()) & 0xffffffff:08x}")' "$1"; }
KERNEL="$YMODEM_DIR/asterinas-network-stack.booti"
COMPRESSED_KERNEL="$KERNEL.lzma"
INITRD="$YMODEM_DIR/asterinas-network-stack-stage1.cpio"
KERNEL_CRC32="$(crc32_file "$KERNEL")"
COMPRESSED_CRC32="$(crc32_file "$COMPRESSED_KERNEL")"
INITRD_CRC32="$(crc32_file "$INITRD")"
KERNEL_SIZE="$(stat -c %s "$KERNEL")"
sha256sum "$KERNEL" "$COMPRESSED_KERNEL" "$INITRD" > "$PHYSICAL_DIR/artifacts.sha256"
```

Expected: each file is non-empty, `KERNEL_SIZE` is positive, each CRC32 is eight lowercase hex characters, and the frozen initramfs SHA-256 remains `f4d9b349094bf2e4368de6edd5131c4e492cafe5905ffdd05a09c94b867e4401`.

- [ ] **Step 2: Run one small network smoke boot over YMODEM**

Run:

```bash
SERIAL=/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0
PYTHONPATH=. python3 -m tools.riscv.megrez_gmac_gate "$SERIAL" \
  --booti asterinas-network-stack.booti.lzma \
  --initrd asterinas-network-stack-stage1.cpio \
  --dtb dtbs/linux-image-6.6.87-win2030/eswin/eic7700-milkv-megrez.dtb \
  --expected-crc32 "booti=$KERNEL_CRC32,dtb=4afcb20e,initrd=$INITRD_CRC32" \
  --host-interface enp12s0 \
  --load-transport ymodem \
  --ymodem-directory "$YMODEM_DIR" \
  --booti-compressed-crc32 "$COMPRESSED_CRC32" \
  --booti-uncompressed-size "$KERNEL_SIZE" \
  --network-mode proxy --target network --reboot-after 300 \
  --boot-timeout 240 --drain-timeout 5 --recovery-timeout 360 \
  --output-directory "$PHYSICAL_DIR/network"
```

Expected: YMODEM supplies only the new volatile kernel/initramfs; U-Boot reads the DTB and Debian root image already on MMC; no MMC partition or boot artifact is rewritten; the existing guest may perform ordinary runtime writes inside its mounted root filesystem; boot reaches the network success contract with no fatal signature and returns to a fresh U-Boot prompt.

- [ ] **Step 3: Run one final Firefox transaction**

Run exactly one final browser boot:

```bash
PYTHONPATH=. python3 -m tools.riscv.megrez_gmac_gate "$SERIAL" \
  --booti asterinas-network-stack.booti.lzma \
  --initrd asterinas-network-stack-stage1.cpio \
  --dtb dtbs/linux-image-6.6.87-win2030/eswin/eic7700-milkv-megrez.dtb \
  --expected-crc32 "booti=$KERNEL_CRC32,dtb=4afcb20e,initrd=$INITRD_CRC32" \
  --host-interface enp12s0 \
  --load-transport ymodem \
  --ymodem-directory "$YMODEM_DIR" \
  --booti-compressed-crc32 "$COMPRESSED_CRC32" \
  --booti-uncompressed-size "$KERNEL_SIZE" \
  --network-mode proxy --target firefox --reboot-after 900 \
  --boot-timeout 780 --drain-timeout 5 --recovery-timeout 960 \
  --output-directory "$PHYSICAL_DIR/firefox"
```

Require all of the following from this one boot:

- Firefox starts on the existing Debian desktop fixture;
- the proxy path completes a request to `www.baidu.com`;
- the uploaded screenshot/evidence validator accepts the rendered result;
- no kernel panic, uncaught panic, physical-I/O stall, or unexpected reboot appears;
- the board is returned through the runner's normal recovery path.

Do not repeat Firefox as a diagnostic loop. Any failure must be classified from the gate transcript and artifacts before another physical run.

- [ ] **Step 4: Record the physical evidence checkpoint**

Record artifact hashes, gate result, transcript path, screenshot/evidence path, and whether recovery completed. Keep bulk logs/screenshots ignored; commit only a concise Markdown evidence record if the repository's current physical-test convention already tracks one.

## Task 9: Rebase Safely and Publish

**Files:** branch history only unless conflict resolution is required.

- [ ] **Step 1: Confirm a clean tree and fetch remote state**

```bash
git status --short
git fetch origin
git log --oneline --decorate --max-count=12 --graph HEAD origin/main
```

Expected: the worktree is clean. If `origin/main` moved, rebase the integration commits onto it, resolve only in-scope conflicts, and rerun the gates affected by those conflicts plus the 427-test matrix.

- [ ] **Step 2: Verify the publish relationship**

```bash
git merge-base --is-ancestor origin/main HEAD
git status --short --branch
```

Expected: the command exits zero; the branch is clean and only ahead of `origin/main`.

- [ ] **Step 3: Push without force only after every gate is green**

```bash
git push origin HEAD:main
```

Expected: a normal fast-forward update. If rejected because remote main moved, do not force; fetch, rebase, and repeat the affected verification.

- [ ] **Step 4: Report the result with evidence**

Report the pushed commit range, exact QEMU gate results for both architectures, UDP prefault result, 427-test result, physical network/Firefox evidence paths, and explicit exclusions. Distinguish test-proven behavior from any remaining network feature work.
