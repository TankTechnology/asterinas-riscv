# 通配 bind 修复：失败用例已复现；内核修复需要新抽象，停在设计阶段等批准

日期：2026-10-27
内核基线：`e74d914e8`（**通配 bind 部分内核零改动**；同轮的可选任务 D 单独改了
`/proc/net/tcp6|udp6` 表头，见 `2026-10-27-procnet-header.md`）
任务书：`/tmp/tp/WILDCARD_BIND.md`
**一句话：任务 A（失败用例 + 可复跑脚本）完成并已复现；任务 B 按任务书红线停手——
正确修复必须给 aster-bigtcp 增加「一个 socket 绑定多个 iface」的新抽象，先出设计等批准。**

> 路径更正：任务书写的是 `kernel/src/net/ip/common.rs`，那是**死树**；
> 活树是 `kernel/core/src/net/socket/ip/common.rs`（本文所有行号都指活树）。

## 1. 任务 A：失败用例（改前）

### 1.1 交付物

- `tools/x86/net/wildcard_bind_check.c`：最小 C 用例，两种模式
  - `bind`：`socket(AF_INET,SOCK_DGRAM)` → `bind(0.0.0.0:0)` → `sendto(10.0.2.3:53, DNS A 查询)` → `recvfrom`（3 s 超时，最多 3 次）
  - `nobind`：同样的查询，但不 bind（隐式绑定按路由选源地址）
- `tools/x86/net/wildcard_bind_check.sh`：可复跑入口，参数
  `--kernel/--initramfs/--base-root/--accel/--workspace/--timeout`；
  内部用 `daily_use_gate.sh` 起 guest，注入并编译用例，最后按
  `RESULT <case> PASS` 打印判定，**两个用例都 PASS 才退出 0**。

### 1.2 改前结果（原始行）

```
WB compile=ok
WB bind: CASE bind|local=0.0.0.0:32768|attempt=1 recvfrom errno=11 (Resource temporarily unavailable)|attempt=2 ...|attempt=3 ...|RESULT bind FAIL|
WB nobind: CASE nobind|attempt=1 recvfrom errno=11 (Resource temporarily unavailable)|attempt=2 reply=86 bytes id=0x1234|RESULT nobind PASS|
```

判定：`bind=FAIL nobind=PASS`，脚本 `check_rc=1` —— **判据 C1 的"改前 FAIL"部分成立**
（改后 PASS 无法在本轮给出，因为内核没改，见 §2）。

### 1.3 用例设计中发现并单列的现象：冷邻居时首包丢失

第一版用例每个 case 只发 1 次，结果**两个 case 都 FAIL**；加"最多 3 次发送"后，
`nobind` 变成第 1 次超时、**第 2 次成功**。同样的现象在前几轮探针里重复出现
（probe 1 首个 UDP 成功、probe 2/5 首个失败后续成功）。

初步解释：eth0 的邻居表（10.0.2.2 网关）在开机后尚未解析，
`Iface::poll_egress` 里 `phy.dispatch(...)` 返回 `None` 时**该 UDP 包已经被出队、
直接丢弃**（`iface/poll.rs:481-490`，`phy.dispatch` 的返回类型见
`iface/common.rs:72-76`），Linux 则会排队等邻居解析。

这是**独立于通配 bind 的第二个缺口**，本轮不修、也不混入归因；为了避免它污染
失败用例，C 用例采用重试。它值得单独立项（下文 §5 列为候选）。

## 2. 为什么本轮没有改内核

任务书红线："若必须 unsafe 或新抽象 ⇒ 停手写设计说明等批准"。
复核代码后确认，**Linux 语义无法在现有抽象内实现**，理由如下（全部为活树行号）：

1. **一个 socket 只有一个 bound port（单 iface）**
   `kernel/libs/aster-bigtcp/src/socket/bound/common.rs:45-49`
   ```rust
   pub struct SocketBg<T: Inner<E>, E: Ext> {
       pub(super) bound: T::BoundPort,   // ← 单个 iface
   ```
   `Socket::iface()`（`:100-102`）直接返回它；销毁时也只从这一个 iface 注销
   （`socket/bound/udp.rs:37-42`）。
2. **端口表与 socket 表都是 per-iface**
   `iface/common.rs:47-48`（`used_ports: SpinLock<PortTable>`、`sockets: SpinLock<SocketTable>`），
   注册点 `:234-243`，注销点 `:245-250`。绑在 lo 的 socket 在 eth0 的表里根本不存在，
   eth0 的轮询不会把包交给它。
3. **发送/出队由"轮询它的那个 iface"决定**
   `iface/poll.rs:594+ dispatch_udp` 遍历本 iface 表里的 socket 并调用
   `socket.dispatch(cx, ...)`，`cx` 就是本 iface 的 Context；`emit_udp`（`:438-458`）
   生成帧后交 `phy.dispatch`（`iface/common.rs:72-76`），邻居/路由不可达时返回 `None`，
   包即丢。**所以"同一 socket 注册到多个 iface"而不加出方向选择，会让先轮询到的
   iface 把包发丢。**
4. 现状的"钉 lo"逻辑在 `kernel/core/src/net/socket/ip/common.rs:24-33`，
   `default_bind_iface()` 在 `:50-56`，FIXME 在 `:47-49`——它自己就写明需要
   "register one socket in several socket tables"，也就是新抽象。

**不能采用的折中**：把 `default_bind_iface()` 改成"默认路由接口"能救 `dig`，
但会把"bind 0.0.0.0 后从 127.0.0.1 访问"的服务端打断——与今天正好相反的回归，
不满足任务书的"接收任意本地地址命中"，故否决。

## 3. 设计方案（等批准）

### 方案 A（推荐）：多 iface 绑定 + 每包路由选 egress

- **aster-bigtcp**
  - 新增 `BoundUdpPortSet<E>`：持有 `primary: BoundUdpPort<E>`（出方向默认）与
    `extras: Vec<BoundUdpPort<E>>`（只收不发）；`BoundPort` 结构不变。
  - `UdpSocket::new_bind_wildcard(set, observer)`：把同一个
    `Arc<UdpSocketBg>` 逐个 `register_udp_socket` 到每个 iface 的表；`on_drop`
    从所有表注销并释放每个端口。
  - `dispatch_udp` 增加一道判断：只有 `socket.bound.primary_iface()` 与该 iface
    相同才出队（`socket.bound.iface()` 换成集合后需要新访问器）。
  - 端口冲突语义：跨 iface 统一（绑定期间在**每个** iface 的 `used_ports` 占位，
    Linux 也是这样：同一端口不能同时在 lo 和 eth0 上以互斥方式绑定）。
- **kernel/core**
  - `datagram/unbound.rs:bind()`：地址 unspecified 时走
    `wildcard_bind_all_ifaces()`，用 `iter_all_ifaces()`（`net/iface/init.rs`）
    逐个 `bind_udp`。
  - `datagram/bound.rs::try_send`：按目的地做一次路由查找（现有
    `route::lookup_iface`），把结果记到 socket 的 egress 选择上，并 poll 该 iface
    （现在只 poll `bound.iface()`）。
  - 显式地址 bind 完全走旧路径，不动。
- **语义**：接收任意 iface/任意本地地址命中；发送按路由选源与出口；
  127.0.0.1 与 10.0.2.15 都可达；TCP 通配 listen 本轮不动（行为保持现状）。
- **规模估计**：aster-bigtcp 约 150-200 行（新增类型 + 注册/注销 + dispatch 判断），
  kernel/core 约 60-100 行（wildcard 绑定、egress 选择）。
- **风险**：注销竞态（socket 在多张表里被并发轮询）、egress 提示与队列里多目的地
  包的关系、端口占位的跨 iface 锁顺序（lo 与 eth0 的 `used_ports` 是两把锁，
  需要固定顺序避免死锁）。**无 unsafe、不碰 ostd。**

### 方案 B：全局 UDP socket 表（改动更大）

把所有 UDP socket 移出 per-iface 表，放进网络命名空间级的表；接收时按目的地址
匹配，发送时按 socket 记录的 egress 选择。语义最正，但牵动 TCP 表结构，工作量与
回归面都更大，不建议本轮做。

### 方案 C（否决）：默认路由 iface 代替 lo

治标，且造成"127.0.0.1 访问 bind(0.0.0.0) 服务"的回归。

## 4. 获批后的验证计划（对应任务书 C1–C3）

| 层 | 步骤 |
|---|---|
| C1 | `tools/x86/net/wildcard_bind_check.sh` 改前 FAIL（已有）→ 改后 bind/nobind 双 PASS |
| C2 | guest 内 `dig @10.0.2.3 deb.debian.org` 有应答、`dig +tcp` 通、`getent hosts` 通 |
| C3 | 7 域矩阵全绿、panic 0；`nc -l`（TCP 通配 listen）与显式地址 bind（127.0.0.1、10.0.2.15）各跑一次对照；`allocation failed` 0 |

## 5. 顺带记录的独立缺口（不在本轮范围）

1. **冷邻居首包丢失**：`iface/poll.rs:481-490` 邻居未解析时 UDP 包被丢弃而不是排队重试
   （Linux 会缓存待发）。证据见 §1.3。
2. `lsof -i` 的 `/proc/net/tcp6|udp6` 表头（`net.rs:320`，`rem_address` vs Linux
   `remote_address`），见 `2026-10-27-app-compat-matrix.md` §2.3。
3. `PTRACE_ATTACH` 未实现（`ptrace.rs:229`）。

## 6. 证据

- `evidence/2026-10-27-wildcard-bind/before-fix.txt`（改前用例原始行与判定）
- 主机工作区：`target/wildcard-bind/`（`console.txt` 全量串口、`gate.log`）
- 用例源码：`tools/x86/net/wildcard_bind_check.c/.sh`

## 7. 现状与请求

- 内核代码**零改动**，无 commit 到内核；本轮只新增测试与文档。
- 同一轮的**可选任务 D** 已单独落地（`/proc/net/tcp6|udp6` 表头对齐 Linux，
  `lsof -i` 不再报 unsupported format，见 `2026-10-27-procnet-header.md`），
  与本文的通配 bind 设计无关，也未改变 §1 的失败用例结果。
- 请批准方案 A（或指定方案 B），下一轮按 §4 的 C1–C3 实施并提交
  `fix(net): wildcard bind no longer pinned to loopback`。
