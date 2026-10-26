# 冷邻居首包丢失：诊断、Linux 对照与两个修复方案（本轮只诊断，未改内核）

日期：2026-10-27
内核：`6d06f9ab2`（工作树干净；本轮的探针都不改内核）
任务书：`/tmp/tp/COLD_NEIGHBOR.md`

## 0. 结论

1. **现象成立且可量化**：邻居（ARP）未解析时，我们**丢弃数据包、改发 ARP 请求**，
   没有待发队列。冷启动后的第一个 UDP 查询 **sendto 返回成功、无任何 errno、无 ICMP**，
   `recvfrom` 3 s 后超时；ARP 在 **3.2 ms** 内解析完成，而被丢的首包在 4.9 ms 时仍无应答
   （第二包 3.4 ms 成功）——这正是"丢包而不是排队"的铁证。
2. **丢几个包**：丢的是 **ARP 解析完成之前发出的所有包**——冷态单发丢 1 个；
   冷态连发 5 个只收到 1 个（**丢 4 个**）；解析完成后立刻恢复（下一包毫秒级，实测 3.4 ms）。
3. **Linux 语义相反**：删掉网关邻居后连发 5 个 ping **0 丢**，单发 UDP 35 ms 内收到
   ICMP 端口不可达（说明包被**排队**到 ARP 完成后再发）。
4. **为什么以前没暴露**：UDP 无重传，丢一个就丢一次请求（dig 默认单次尝试就失败）；
   TCP 由 RTO 重传掩盖——冷态 `connect()` 实测 **0.744 s**（SYN 被丢，RTO 后重传成功），
   热态是毫秒级。这就是"第一下总是慢"的来源。
5. **实现定位**：`kernel/libs/aster-bigtcp/src/iface/phy/ether.rs:231-247` ——
   解析失败时 `return Err(Some(ArpRepr::Request))`，`Iface::dispatch`（`:137-146`）
   用**同一个包缓冲**改发 ARP 并返回 `None`，`poll_egress` 把 `None` 当作"这一轮结束"，
   数据包就此消失。全文注释（`:234-236`）明确写"drop the original packet … the upper
   layer should be responsible for retrying"。

## 1. 任务 A：精确刻画

### 1.1 最小复现与四件事（guest，冷态 = 开机后第一个网络动作）

探针：`/var/tmp/cn` 脚本（见 §5），基座 `target/app-matrix/daily.ext2`，
内核 `6d06f9ab2`。原始行：

```
CN py: arp_at_start []
CN py: single1_cold fail 3.003s errno=TimeoutError('timed out')
CN py: arp_after_single1 ['10.0.2.3']
CN py: single2_warm ok 0.001s bytes=12
```

精确时延探针（每 1 ms 采样 `/proc/net/arp`）：

```
CA py: arp_at_start []
CA py: cold_arp_resolved 0.0032s
CA py: first_packet_reply none errno=BlockingIOError(11, ...) after 0.0049s
CA py: second_packet_reply 0.0034s
```

| 问题 | 答案 |
|---|---|
| 丢几个包 | 冷态**单发就是 1 个**；ARP 完成后立刻恢复（下一包 3.4 ms 成功） |
| ARP 何时发出 | **丢包的同一次 dispatch 里立即发出**（不是等重传）；实测 `cold_arp_resolved 0.0032s`——发出后 **3.2 ms** 完成解析 |
| 应用看到的错误 | **没有错误**：`sendto` 返回成功（字节数正常），没有 `ENETUNREACH`/`EHOSTUNREACH`，没有 ICMP，只有 `recvfrom` 的**超时** |
| 邻居建立后是否立刻恢复 | 是，下一包 3.4 ms 成功（含一个 DNS 往返） |

### 1.2 突发场景（同一冷态下连发 5 个）

```
CB py: arp_at_start []
CB py: burst sent=5 replies=1 lost=4 elapsed=3.004s
CB py: arp_after_burst ['10.0.2.3']
CB py: after_burst_single ok 0.001s
```

**冷态突发 5 包丢 4 包**：只收到 1 个应答，其余 4 个落在"邻居还未解析"的窗口里被丢弃；
ARP 解析完成后立刻恢复（下一包 1 ms 成功）。
所以"丢几个包"的准确说法是：**丢的是 ARP 解析完成之前发出的所有包**，
单发时表现为 1 个，突发时可以是 4/5。

### 1.3 TCP 是否被重传掩盖（冷态 TCP）

```
CT py: arp_at_start []
CT py: tcp 10.0.2.2 err 0.744s ConnectionRefusedError(111, 'Connection refused')
CT py: arp_after_10.0.2.2 ['10.0.2.2']
CT py: tcp 10.0.2.3 err 8.001s TimeoutError('timed out')
```

冷态下 `connect(10.0.2.2:80)` 用了 **0.744 s** 才拿到 RST：SYN 因邻居未解析被丢，
TCP 在 **RTO≈0.75 s** 后重传，此时 ARP 已解析，这次拿到 RST。
热态下同一连接是毫秒级——这就是"第一下总是慢"的来源（约 +0.7 s），
而且它被 TCP 重传掩盖成了"慢"而不是"错"。

（`10.0.2.3:80` 的 8 s 超时与邻居无关：slirp 不服务该地址的 TCP 80。）

### 1.4 Linux 对照（host，删网关邻居后立即发）

```
$ sudo ip neigh del 10.100.79.1 dev wlp0s20f3
$ ping -c 5 -i 0.01 -W 2 10.100.79.1
5 packets transmitted, 5 received, 0% packet loss, time 40ms
$ ip neigh show 10.100.79.1
10.100.79.1 dev wlp0s20f3 lladdr 4c:d6:29:18:93:46 REACHABLE

# 单发 UDP（连到网关的关闭端口）：35 ms 收到 ICMP 端口不可达
udp result ConnectionRefusedError(111, 'Connection refused') 0.035
```

判读：Linux 在邻居未解析时把包放进**该邻居的待发队列**（`arp_queue`，有长度上限），
同时发 ARP；解析完成后队列里的包照常发出。**0 丢包**。
我们的实现是"丢包 + 发 ARP"，语义不同。

### 1.5 顺带发现：`ip neigh flush` 在 guest 内是空操作

```
CN py: flush2_rc 0 arp_after_flush2 ['10.0.2.3']
CN py: flush3_rc 0 arp_now ['10.0.2.3']
```

`ip neigh flush dev eth0` 返回 0，但 ARP 表条目仍在——netlink 的 del/flush 没有真正生效
（第一次 `ip neigh del` 还直接把探针挂死，需要 `timeout` 才能收住）。**这不是本缺陷**，
但意味着"清 ARP 复现冷态"这条路在 guest 内不可用，冷态只能取**开机后的第一个动作**。
（建议另立小项：RTM_DELNEIGH 语义或至少返回错误而不是成功。）

## 2. 任务 B：实现链路（只读，文件:行号）

```
sendto(UDP)
 → kernel/core/src/net/socket/ip/datagram/mod.rs:94-125   DatagramSocket::try_send
 → kernel/core/src/net/socket/ip/datagram/bound.rs:86       BoundDatagram::try_send
     · bound_socket.send(...)  只把数据排进 smoltcp UDP 发送队列
     · iface_to_poll.poll()    触发该 iface 的轮询
 → kernel/libs/aster-bigtcp/src/iface/poll.rs:478-496       poll_egress
 → kernel/libs/aster-bigtcp/src/iface/poll.rs:594+          dispatch_udp → socket.dispatch
 → kernel/libs/aster-bigtcp/src/iface/poll.rs:438-458       emit_udp 生成 IP/UDP 帧
 → kernel/libs/aster-bigtcp/src/iface/phy/ether.rs:137-146  EtherIface::dispatch
     · resolve_ether_or_generate_arp(packet.dst_addr, cx)
 → kernel/libs/aster-bigtcp/src/iface/phy/ether.rs:211-254  resolve_ether_or_generate_arp
     · :217 iface_cx.route(dst) → next hop IP
     · :231 arp_table.get(next_hop)           命中 → 正常发
     · :237-246 未命中 → return Err(Some(ARP request))
     · :144 dispatch 收到 Err(Some(arp)) → 用**同一个缓冲**发 ARP，返回 None
 → poll_egress(:484-490)：packet 为 None 时什么也不发，数据包已随 reset_to_fresh() 丢弃
```

关键事实：

| 嫌疑 | 结论 | 证据 |
|---|---|---|
| 丢包而非排队 | **丢包**。全树没有 ARP 待发队列（`grep arp_queue/unres_qlen/pending packet` 无命中） | `ether.rs:231-247` 注释 `:234-236` |
| 不触发 ARP | **会触发**，且是立即触发；丢的正是触发 ARP 的那一包 | `:237-246`、`:144` |
| 无重传 | UDP 无重传 → 必丢；TCP 由 smoltcp RTO 重传掩盖 | §1.3 |
| 错误码 | **无声**：`sendto` 成功，应用层看不到任何错误；内核也不生成 ICMP | §1.1；内核的 ICMP 不可达只对**收到**的包生成（`poll.rs:142,311`），本路径不涉及 |
| ARP 表 | `BTreeMap<Ipv4Address, EthernetAddress>`，**没有状态、没有过期**（`:172` TODO） | `ether.rs:32,173-175` |

补充：因为 ARP 表没有"待定/失败"状态，冷态下每个发出的包都会各发一次 ARP 请求
（`resolve_ether_or_generate_arp` 每次未命中都返回请求），突发时会看到多次 ARP 请求。

## 3. 任务 C：两个方案（本轮不实现）

### 方案 A（对齐 Linux）：待发包队列 + 触发 ARP

**做法**：邻居未解析时把 `TxPacketWithDst` 放进"该下一跳的待发队列"（有上限，Linux
默认 `unres_qlen=3` 个包 / 1 KB），同时发 ARP；`process_arp` 学到邻居后把队列里的包
按序重新交给 `phy.dispatch`。

**改动点**：
- `iface/phy/ether.rs`
  - 新增 `arp_pending: SpinLock<BTreeMap<Ipv4Address, VecDeque<TxPacketWithDst>>>`（`:32` 旁）；
  - `resolve_ether_or_generate_arp`（`:211-254`）未命中时返回"待定"信号而不是复用包缓冲，
    真正发 ARP 用**新的** `alloc_tx_buffer`（现在用的是数据包自己的缓冲 `:140`）；
  - `dispatch`（`:137-146`）在 "待定" 分支里把包入队，返回 `None`（本轮不发）；
  - `process_arp`（`:155-178`）学到映射后 drain 队列、逐包 `dispatch`；
  - 队列上限与丢弃策略（满了丢最老的，与 Linux 类似）；
  - iface 关闭时清空队列。
- 不需要改 socket/上层接口。

**影响面**：新增状态 + 内存上限；轮询路径多一次 map 查找；**缓冲所有权**要选清楚——
待发队列若直接持有 `TxPacket`，就是占着设备发送缓冲（受队列长度约束，和 Linux 用内核
内存 clone 的做法不同）；也可以把负载拷到内核内存、解析完成后再重建帧（更接近 Linux，
但要多一次拷贝和内存记账）。
**风险**：中等。新增抽象（队列），按之前的口径需要批准；并发上是 `SpinLock` + bottom-half
关闭环境，只在 poll 路径内操作，锁范围可控。

**验证**：本轮的冷态探针（单包/突发/TCP）应从 FAIL 变 PASS，并与 Linux 数字对齐
（单包成功、突发 0 丢、TCP 冷态 ~ms）；再跑 7 域矩阵与 30 应用矩阵回归。

### 方案 B（最小改动）：发送路径同步解析 + 短超时

**做法**：在 `BoundDatagram::try_send`（`kernel/core/src/net/socket/ip/datagram/bound.rs:86`）
把数据**排进 smoltcp 发送队列之前**，先确认下一跳邻居已解析；未解析时触发一次 ARP 并在
**有限时间**（例如 20–50 ms）内等待，成功后再入队发送，超时则返回错误
（`EAGAIN` 或 `ENETUNREACH`，需要定口径）。

注意"先检查、后入队"是必须的：一旦进了 smoltcp 的队列，`dispatch` 会把包消耗掉并丢弃，
socket 层没有副本可以重发。

**改动点**：
- aster-bigtcp 新增一个小接口（例如 `Iface::ensure_next_hop(dst, timeout) -> Result<()>`）：
  做 `iface_cx.route(dst)` → `arp_table.get()`；未命中时发出一次 ARP 请求（复用
  `resolve_ether_or_generate_arp` 的构造逻辑），然后在**非 poll 上下文**里等待
  （sleep + 重查，或邻居表上一个 event），超时返回错误；
- `datagram/bound.rs:86`（UDP）与 TCP 的 connect 路径（`socket/ip/stream`）在入队前调用它；
- 等待必须在用户线程上下文，绝不能放进 `ether.rs dispatch`（那里持有 bottom-half 关闭的
  自旋锁）。

**影响面**：发送路径引入最坏几十毫秒的阻塞；所有 UDP/TCP 首包都会走这条路径；
需要小心重入（发送 -> poll -> 邻居表）。
**风险**：阻塞语义与 Linux 不符（Linux 的 sendto 从不因邻居未解析而阻塞），
且只是"把丢包换成等待"，仍没有队列；高并发首包会串行变慢。
**适用**：如果只想快速消掉"第一下慢"的体感，且不想加队列状态。

**验证**：同一套冷态探针；另外必须验证 `sendto` 在超时场景下不再"假成功"，
以及 30 应用矩阵、7 域矩阵不回归。

### 推荐

**方案 A**。它是 Linux 的语义（队列 + 异步完成），对应用完全透明（`sendto` 仍立即成功），
且能覆盖突发与多目的地；方案 B 会引入阻塞，并且改不掉"突发时中间包仍被丢"的问题。
代价是新增一个有界队列状态，按纪律需要先批准。

## 4. 证据

- `evidence/2026-10-27-cold-neighbor/guest-cold-raw.txt`（guest 冷态原始行）
- `evidence/2026-10-27-cold-neighbor/arp-timing-raw.txt`（1 ms 采样的冷态 ARP 时延）
- `evidence/2026-10-27-cold-neighbor/linux-raw.txt`（host Linux 对照原始行）
- `evidence/2026-10-27-cold-neighbor/burst-raw.txt`、`tcp-raw.txt`（§1.2/§1.3）
- 复现脚本：`guest-probe.sh`、`burst-probe.sh`、`tcp-probe.sh`、`arp-timing-probe.sh`
- 主机工作区：`target/cold-timing/`（另有已清理的 `target/cold-burst`、`target/cold-tcp`）

## 5. 探针

guest 侧脚本在 `/tmp/appmatrix/`（不入库），核心逻辑：开机后**第一个网络动作**即
向 `10.0.2.3:53` 发一条 DNS 查询，`recvfrom` 3 s 超时；随后立刻读 `/proc/net/arp`
并再发一包对照。由于 `ip neigh flush` 不生效，冷态只能靠"开机首包"获得。

## 6. 状态

- 本轮**不改内核、不 commit、不 push**（本文与证据是唯一交付）。
- 若批准方案 A，下一轮改动点与验证计划见 §3。
