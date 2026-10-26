# dig `@::1` 的 strace 定位：致命项是 `IPV6_RECVERR`

日期：2026-10-27
内核：`84cc1ea86`（已含 `SOL_IPV6 IPV6_V6ONLY`；本轮只测、不改内核）
任务书：`/tmp/tp/DIG_STRACE.md`

## 0. 结论

- **致命调用**：`setsockopt(fd, SOL_IPV6, IPV6_RECVERR, [1], 4) = -1 ENOPROTOOPT`。
  这是 dig 退出前**最后一个网络调用**，随后进程以 9 退出（dig 报
  `UDP setup ... failed: invalid protocol`）。
- v4 的**对应调用成功**：`setsockopt(fd, SOL_IP, IP_RECVERR, [1], 4) = 0`，
  之后 `bind` → `connect` → `sendmsg` 全部成功、查询返回 `127.0.0.1`。
- 判定：**sockopt 缺口续集**——`SOL_IPV6` 还缺 `IPV6_RECVERR`（BIND 视为必需）。
  `IPV6_DONTFRAG`、`IPV6_MTU` 也返回 `ENOPROTOOPT`，但 BIND 继续执行，不是致命项。
- **是否值得修**：值得。成本小（与 `IPV6_V6ONLY` 同一模式，只需读写一个布尔标志），
  修好后 `dig`/`host`/`nslookup` 的 v6 路径可用。**本轮不修**（任务书要求不改内核）。

## 1. 方法前置：`-f` 不可用，不带 `-f` 够用

任务书给的 `strace -f ... dig` 在我们内核上会杀进程：

```
S v6_f rc=134 trace_lines=5
S v6_f out=thread.c:140:isc_thread_create(): fatal error: pthread_create(): Operation not supported (95)|Aborted|
```

原因是 **clone-family ptrace 事件未支持**：
`kernel/core/src/process/clone.rs:243-249` 对"被 trace 的线程做 clone"直接返回
`EOPNOTSUPP`，而 `strace -f` 会设 `PTRACE_O_TRACECLONE`。v4 的 `-f` 同样失败（rc=134）。

**不带 `-f` 的 strace 可用**，而且 dig 的 UDP setup 恰好发生在**主线程**上，
所以单线程 trace 已经能看到全部相关调用。下面所有片段都来自不带 `-f` 的运行。

## 2. strace 片段（去噪后，完整 11/14 行见证据）

v6（`@::1 -p 18222`，失败，rc=9）：

```
socket(AF_INET6, SOCK_DGRAM, IPPROTO_IP) = 9
setsockopt(9, SOL_IPV6, IPV6_DONTFRAG, [0], 4) = -1 ENOPROTOOPT (Protocol not available)
setsockopt(9, SOL_IPV6, IPV6_MTU, [1280], 4) = -1 ENOPROTOOPT (Protocol not available)
getpeername(9, 0x7ffffffe6660, [128]) = -1 ENOTCONN (Transport endpoint is not connected)
setsockopt(9, SOL_SOCKET, SO_REUSEADDR, [0], 4) = 0
setsockopt(9, SOL_IPV6, IPV6_RECVERR, [1], 4) = -1 ENOPROTOOPT (Protocol not available)
+++ exited with 9 +++
```

v4（`@127.0.0.1 -p 18221`，成功，rc=0，输出 `127.0.0.1`）：

```
socket(AF_INET, SOCK_DGRAM, IPPROTO_IP) = 9
setsockopt(9, SOL_IP, IP_MTU_DISCOVER, [5], 4) = -1 ENOPROTOOPT (Protocol not available)
setsockopt(9, SOL_SOCKET, SO_REUSEADDR, [0], 4) = 0
setsockopt(9, SOL_IP, IP_RECVERR, [1], 4) = 0
bind(9, {sa_family=AF_INET, sin_port=htons(0), sin_addr=inet_addr("0.0.0.0")}, 16) = 0
connect(9, {sa_family=AF_INET, sin_port=htons(18221), sin_addr=inet_addr("127.0.0.1")}, 16) = 0
sendmsg(9, ..., 51) = 51
recvmsg(9, ..., 44) = 44
```

**注意**：两条 trace 里都**没有** `IPV6_V6ONLY` 调用。上一轮"补 V6ONLY 后 dig 错误从
EAFNOSUPPORT 变成 ENOPROTOOPT"的解释是：修好前整个 `SOL_IPV6` 层不存在，
**第一个** SOL_IPV6 选项（`DONTFRAG`）就返回 `EOPNOTSUPP(95)`，BIND 把它报成
"address family not supported"；现在该层存在，`DONTFRAG`/`MTU` 变成可容忍的
`ENOPROTOOPT`，进程继续执行，最后死在它认为必需的 `RECVERR` 上。
（这是由两次错误串 + 当前 trace 反推的解释，未在修好前的内核上做过 strace。）

## 3. v4 vs v6 差异表

| 调用 | v4 | v6 | 是否致命 |
|---|---|---|---|
| `socket(..., SOCK_DGRAM, IPPROTO_IP)` | 成功（fd 9） | **成功**（fd 9） | 否 |
| `setsockopt(IPV6_DONTFRAG, 0)` | — | `ENOPROTOOPT` | 否（BIND 继续） |
| `setsockopt(IPV6_MTU, 1280)` | — | `ENOPROTOOPT` | 否（BIND 继续） |
| `setsockopt(IP_RECVERR/IPV6_RECVERR, 1)` | **0** | **`ENOPROTOOPT`** | **是** |
| `bind` / `connect` / `sendmsg` | 全部成功 | **未到达** | — |

v4 自身也有一个 `IP_MTU_DISCOVER = ENOPROTOOPT` 却被容忍——说明 BIND 只对
`RECVERR` 这类"它认为必须成功"的选项致命。

## 4. 判定与成本

- **判定（任务 B 的四选一）**：`setsockopt(SOL_IPV6, …)` ⇒ **sockopt 缺口续集**。
  具体的缺口是 `IPV6_RECVERR`（dig 退出前最后一个网络调用）；`IPV6_MTU` /
  `IPV6_DONTFRAG` 也失败但 BIND 容忍，是噪声项。
- **是否值得修**：值得。DNS 工具（dig/host/nslookup）是网络排障的基础，修好 v6 查询
  在只有 loopback IPv6 的环境里也能直接验证。
- **成本估计**：小。照 `84cc1ea86` 的模式在
  `kernel/core/src/util/net/options/ipv6.rs` 里新增 `RECVERR = 11`（Linux 的
  `IPV6_RECVERR` 编号）；`IpOptionSet` 存一个布尔标志，get/set 对称即可——
  我们本来就不投递错误队列（v4 的 `IP_RECVERR` 也只是存储），所以**不需要
  实现错误队列语义**。若顺手，再把 `IPV6_MTU`（`=2`）与 `IPV6_DONTFRAG`（`=10`）
  的读写补上，可消除噪声，但不是必需。
- **验证方式**：复用 `tools/x86/net/ipv6_v6only_check.sh` 的骨架做失败用例；
  C2 用 `dig @::1`（本机 DNS responder）；C3 跑 7 域矩阵。

## 5. 证据

- `evidence/2026-10-27-dig-strace/`：v4/v6 完整 trace、`-f` 失败的原始行
- 主机工作区：`target/dig-strace/`（第一次，含 `-f` 对照）、`target/dig-strace2/`（完整 trace）

## 6. 边界

- 本轮**不改内核代码**；`IPV6_RECVERR` 只作为下一轮的修复项。
  （**2026-10-27 晚已实现**：`IPV6_RECVERR`/`IPV6_DONTFRAG`/`IPV6_MTU` 落地后
  `dig @::1` 返回 `127.0.0.1`、rc=0，见 `2026-10-27-ipv6-recv-options.md`。）
- `strace -f` 在本内核不可用（clone-ptrace 缺口），这是独立缺口，已在
  `2026-10-27-ptrace-attach.md` 的实现边界里记录过。
- 所有验证都指向本机 responder（`127.0.0.1:18221` / `::1:18222`），不依赖外网。
