# Megrez DWMAC High-Information Board Experiment Design

Date: 2026-08-28

## Goal

Identify where the Megrez wired-network receive path stops after the first TCP
burst without using repeated physical resets or speculative driver fixes. One
bounded Asterinas boot must distinguish host-to-board receive loss, guest ACK
generation failure, DWMAC transmit-completion failure, and userspace/socket
wakeup failure, then return the board to U-Boot through the already proven
software-reboot path.

The experiment is diagnostic. A passing 16 KiB stage may continue through
64 KiB, 1 MiB, and 16 MiB in the same boot. The first failed stage terminates
the guest probe and enters recovery observation; it never triggers another
automatic board boot.

## Evidence and corrected hypothesis

The failed 2026-08-28 board image already contained the generic network fixes
for ingress/TX decoupling and the `PollScheduler` immediate-poll sentinel. It
selected GMAC1 at 1 Gbit/s, failed while receiving the first 16 KiB response,
and failed identically with both USB host controllers disabled. The serial log
then showed a new firmware/OpenSBI/U-Boot cycle, so `asterinas.reboot_after=180`
did recover this failure mode.

The later 32-packet DWMAC RX budget fixes a real starvation lasso found by the
host model, but the observed first-burst failure does not prove that lasso was
reached. About one initial TCP congestion window was delivered before progress
stopped. The leading hypothesis is therefore missing ACK progress after the
first burst, caused by one of:

1. RX descriptor/tail/status/PLIC progress stops before the TCP stack sees the
   remaining frames;
2. the TCP stack generates ACKs but DWMAC TX submission or completion stalls;
3. RX and TX continue but the socket waiter is not awakened.

The experiment gathers evidence at each boundary before selecting a fix.

## Safety contract

No `booti` is permitted until all host, model, QEMU/generic-network, RISC-V
compile, artifact-identity, and recovery-contract checks pass.

The board transaction has these hard rules:

- start only from an observed U-Boot prompt while holding `TIOCEXCL` and a
  cooperative lock on `/dev/ttyUSB0`;
- use only volatile `setenv` commands; never execute `saveenv`, flash erase,
  partition writes, or a persistent boot-command change;
- boot an initramfs-only probe with USB disabled and no Debian root or desktop;
- arm `asterinas.reboot_after=60` before starting userspace;
- stop the network probe at 45 seconds, leaving a deterministic 15-second
  window to flush its terminal evidence before the recovery timer fires;
- test ordered payload sizes in one boot and stop on the first failure;
- after PASS or FAIL, keep the init process alive so the kernel timer owns
  recovery;
- require a fresh firmware/OpenSBI/U-Boot prompt after the terminal marker;
- impose a 90-second recovery deadline and a 120-second total board deadline;
- if recovery does not occur, close the serial descriptor, publish a
  `recovery-not-observed` result, and do not issue another boot/reset command.

This contract strongly protects the known failure mode. It cannot guarantee
recovery from global interrupt disablement, SBI failure, page-table corruption,
or a pre-arm boot hang. Reaching unattended recovery for those failures needs
an independent EIC7700 hardware watchdog or an out-of-band reset/power device;
software alone cannot honestly claim a universal 99% reset guarantee.

## Host-side TCP evidence

The development user lacks `CAP_NET_RAW`, so `tcpdump` is not a required gate.
The existing bounded Python probe server will collect Linux `TCP_INFO` directly
from each accepted socket at a bounded cadence while the response is being
sent. Each sample records monotonic time and the stable kernel fields needed
for the discriminator:

- connection state, retransmits, RTO, sender MSS;
- unacknowledged/lost/retransmitted segment counts;
- sender congestion window and advertised peer window;
- bytes sent, bytes acknowledged, bytes retransmitted;
- data segments sent and segments received.

The trace is capped by sample count and encoded as deterministic JSON. Socket
errors and the number of application bytes accepted by `send` are recorded.
The board output publication binds this trace to the exact plan hash alongside
serial and transport evidence. A loopback test with a deliberately stalled
client proves the trace observes unacknowledged data without needing a board.

## Guest and driver evidence

The guest probe retains the exact streamed payload validator and emits its
terminal FAIL marker once, including current-stage body bytes rather than only
the sum of completed stages. This makes a first-stage timeout quantitative.

The DWMAC driver emits logarithmically bounded progress snapshots at cumulative
RX milestones 1, 2, 4, 8, 16, and so on. A snapshot includes:

- RX packets consumed, RX budget exhaustions, reschedules, and PLIC rearms;
- TX descriptors submitted, completed/reclaimed, and currently outstanding;
- RX head and last advertised RX tail position;
- the last DMA channel status observed before write-one-to-clear.

Power-of-two sampling bounds log volume to `O(log packets)` and avoids a line
per packet. The counters are observational only and do not change descriptor,
MMIO, interrupt, or queue-control decisions.

## Static and simulated analysis

Before implementation changes, the DWMAC TX and RX sequence is compared line
by line with the already pinned ESWIN Linux 6.6 `stmmac` sources. The audit
covers descriptor publication barriers, TX tail semantics, completion reclaim,
RX refill/tail order, write-one-to-clear status, and interrupt reenablement.
Unverified MMIO or cache assumptions remain explicit.

Host tests exercise:

- the existing exhaustive reduced RX/PLIC/softirq state model;
- pure TX ring capacity, wrap, bounded reclaim, and progress counters;
- power-of-two diagnostic cadence and counter invariants;
- split serial FAIL/PASS/recovery markers and stale U-Boot prompt rejection;
- recovery timeout without a second `booti`;
- bounded TCP_INFO trace collection, truncation, and canonical publication;
- signal cleanup and no lingering socket/server/serial process.

QEMU cannot emulate EIC7700 DWMAC. It is used only for generic TCP/socket
regression and to prove the exact `reboot_after=60` kernel/initramfs returns to
its firmware/monitor boundary. It is not reported as a hardware pass.

## One-run classification

The final evidence is interpreted as follows:

| Host TCP_INFO | DWMAC RX | DWMAC TX | Meaning |
|---|---|---|---|
| bytes-acked stalls | RX stalls | no new ACK submit | RX/tail/IRQ path |
| bytes-acked stalls | RX advances | TX submit advances, completion stalls | TX DMA/MMIO/cache path |
| bytes-acked stalls | RX advances | TX completion advances | ACK content or host observation mismatch |
| bytes-acked advances | RX/TX advance | guest read stalls | socket wakeup/poll path |
| all advance | stage completes | stage completes | proceed to next ordered size |

If the run cannot uniquely select a row, it must end with the remaining two
hypotheses and the exact missing observation. It does not authorize another
physical run or a production fix by guesswork.

## Deliverables

- corrected board terminal/recovery state machine;
- bounded TCP_INFO probe trace and output publication;
- quantitative guest failure marker;
- low-rate DWMAC RX/TX/status progress snapshots;
- refreshed host/model/QEMU/RISC-V preboard evidence;
- one result bound to exact kernel, initramfs, DTB, plan, serial, and TCP trace.
