# IPv6 真实工具矩阵：12 组 v4/v6 成对对照

日期：2026-10-27
内核：`fe39cfe83`（IPv6 UDP 支持已在上一轮落地；本轮**只测不改**）
环境：guest Debian 13，`lo` 上只有 `::1/128`（`eth0` 无全局 IPv6），`/etc/hosts` 含 `::1 localhost`。
方法：所有能指向本机的工具都指向 `127.0.0.1` / `::1`，服务端用 python 自建
（UDP/TCP echo、DNS、HTTP、自签 TLS），避免外网变量。原始输出见
`evidence/2026-10-27-ipv6-tools/`。

## 0. 结论

- **8 组工具 v4/v6 完全一致通过**：`nc`(UDP)、`python3` UDP、`python3` TCP、`curl`、
  `ss`、`lsof`、`openssl s_client`、`socat`。
- **1 组 NA**：`ping`（镜像未安装 `iputils-ping`；内核也没有 `AF_INET SOCK_RAW`，
  两族都不可用，不是 v6 缺陷）。
- **3 组 v6-only 异常**：
  - `getent ahostsv6` —— 已证实是 **glibc `AI_ADDRCONFIG`**（本机只有 loopback IPv6）；
  - `iperf3 -6` —— 已定位是**内核缺口**：`setsockopt(IPPROTO_IPV6, IPV6_V6ONLY)`
    返回 `EOPNOTSUPP`，iperf3 的 v6 监听因此启动失败；
  - `dig @::1` —— **未完全定论**：两个候选（`AI_ADDRCONFIG` / `SOL_IPV6` sockopt），
    给了判别方法。

## 1. 12 行矩阵

| # | 工具 / 场景 | v4 | v6 | 差异/错误（原始） | 归因 |
|---|---|---|---|---|---|
| 1 | `nc` UDP 客户端 → echo 服务 | ok | ok | `echo:nc4` / `echo:nc6` | — |
| 2 | `python3` UDP 自环 | ok | ok | `echo:py4` / `echo:py6` | — |
| 3 | `python3` TCP 自环 | ok | ok | `echo:t4` / `echo:t6` | — |
| 4 | `dig @127.0.0.1 / @::1`（自建 DNS） | ok | **fail** | v6: `UDP setup with ::1#18222(::1) for test.local failed: address family not supported.`（rc=9） | 见 §2.1 |
| 5 | `curl http://127.0.0.1 / http://[::1]` | ok | ok | `http-ok` / `http-ok` | — |
| 6 | `getent ahostsv4 / ahostsv6 localhost` | ok | **fail** | v6 输出为空（rc=2） | **glibc `AI_ADDRCONFIG`**，见 §2.2 |
| 7 | `ss -tln / -uln` | ok | ok | `127.0.0.1:18211` / `[::1]:18212`；UDP `[::1]:18202` | — |
| 8 | `lsof -i` | ok | ok | v4：`UDP localhost:18301`、`TCP localhost:18303 (LISTEN)`；v6：`IPv6 UDP localhost:18302`、`TCP localhost:18304 (LISTEN)` | — |
| 9 | `ping -c1` | **NA** | **NA** | `ping: not found`；内核无 `AF_INET SOCK_RAW` | NA（两族一致） |
| 10 | `openssl s_client`（自签 TLS） | ok | ok | 自签证书校验告警符合预期 | — |
| 11 | `socat` UDP 客户端 | ok | ok | `echo:so4` / `echo:so6` | — |
| 12 | `iperf3 -c` | ok | **fail** | v6: 连接失败；v4 有 `getsockopt - Protocol not available` 告警 | 见 §2.3 |

首轮 `lsof` 的 v4 行用 `127.0.0.1:PORT` 过滤没命中，因为 lsof 把地址渲染成
`localhost:PORT`；这是测试口径，不是 v4 缺陷。最终探针用端口号过滤复核，v4/v6 两族
各有 UDP+TCP 两行，**完全对称**。

## 2. 失败归因

### 2.1 `dig @::1`：未定，两个候选

BIND 在 "UDP setup" 阶段返回 `EAFNOSUPPORT(97)`。我们逐项排除了 socket 层：

- `socket(AF_INET6, SOCK_DGRAM)`、`socket(..., IPPROTO_UDP)` → **成功**；
- `bind(("::", 0))`、`bind(("::1", 0))`、`connect/sendto/recvfrom` 到 `::1`
  → **成功**（上一轮的 round-trip 已验证）；
- `ss`/`lsof`/`curl`/`socat`/`nc` 的 v6 路径全部成功。

剩余两个候选：

1. **glibc `AI_ADDRCONFIG`**（与 `getent` 同源）：我们实测
   `getaddrinfo("::1", ..., AI_ADDRCONFIG)` 返回 `gaierror(-9, 'Address family for
   hostname not supported')`，去掉该 flag 即成功。若 BIND 解析服务器地址时带
   `AI_ADDRCONFIG`，就会得到这个错误。
2. **IPv6 sockopt 缺口**：`setsockopt(IPPROTO_IPV6, IPV6_V6ONLY)` 在我们内核返回
   `EOPNOTSUPP(95)`——`kernel/core/src/util/net/options/mod.rs:169-175` 的
   `new_raw_socket_option` **没有 `SOL_IPV6` 分支**。若 BIND 把它当致命错误并映射成
   "address family not supported"，也会是这个现象。

**补充实验**：`dig -6 @::1`（强制 v6）与默认调用得到**完全相同**的错误，说明不是
BIND 选错 socket 族。

**未验证**：没有在不改内核的前提下把两者分开（见 §3 的 netlink 实验失败）。
修好 `SOL_IPV6`（§4 缺口 1）后重跑 `dig @::1` 即可一次性判别：若变好 ⇒ 候选 2；
若仍旧 ⇒ 候选 1（`AI_ADDRCONFIG`）。

### 2.2 `getent ahostsv6`：glibc `AI_ADDRCONFIG`（非内核）

python 用同一 flag 复现：

```
localhost_v6              -> [('::1', ...)]                 # 无 flag，成功
localhost_v6_ADDRCONFIG   -> ERR gaierror(-2, 'Name or service not known')
literal_v6_ADDRCONFIG     -> ERR gaierror(-9, 'Address family for hostname not supported')
localhost_v4_ADDRCONFIG   -> [('127.0.0.1', ...)]          # v4 正常
```

glibc 的 `AI_ADDRCONFIG` 只把**非 loopback** 地址算作"该族已配置"；本机只有 `::1`
（`eth0` 无 IPv6），所以 v6 被判定为不可用。**这是环境 + glibc 语义，不是内核缺陷**，
在有全局 IPv6 的机器上不会出现。

### 2.3 `iperf3 -6`：**已定位——`SOL_IPV6` sockopt 缺口**

服务端日志给出了决定性证据：

```
I srv4: iperf3: getsockopt - Protocol not available ... Server listening on 5201 (test #1)
I srv6: iperf3: error - unable to start listener for connections: Operation not supported
        iperf3: exiting
I v6:   error - unable to connect to server ... Connection refused
```

- v6 **监听**在启动阶段就失败，错误是 `Operation not supported`（`EOPNOTSUPP`），
  不是地址族问题；客户端随后只是"没人监听"（Connection refused）。
- 我们实测 `setsockopt(IPPROTO_IPV6, IPV6_V6ONLY, 1)` 返回 `EOPNOTSUPP(95)`：
  `kernel/core/src/util/net/options/mod.rs:169-175` 的 `new_raw_socket_option`
  **没有 `SOL_IPV6` 分支**。iperf3 在 v6 监听前会设置 `IPV6_V6ONLY`，失败即放弃。
- v4 侧的 `getsockopt - Protocol not available` 是同一缺口在 v4 路径上的告警
  （不致命），可解释为 iperf3 在探测某个不受支持的选项。

**结论**：`iperf3 -6` 的失败是**内核缺口**（SOL_IPV6 选项），不是 AI_ADDRCONFIG。

## 3. 定位实验（含负结果）

- **给 lo 加一个非 loopback 的 v6 地址**（用来让 `AI_ADDRCONFIG` 翻盘）：
  `ip -6 addr add fd00::1/64 dev lo` **挂死**——有界复跑给出
  `F add_rc=124 out=`（5 s 超时被杀），lo 上仍然只有 `::1`。与之前 `ip neigh del`
  一样：netlink 变更请求等不到应答。这是**另一个内核缺口**（见 §4），也导致这条
  判别路径走不通。
- `getent ahostsv6 ::1`（数字地址）同样为空，与名字无关，进一步支持
  `AI_ADDRCONFIG` 的解释。
- **iperf3 服务端日志**（决定 §2.3 的归因）：

  ```
  I srv4: iperf3: getsockopt - Protocol not available ... Server listening on 5201 (test #1)
  I srv6: iperf3: error - unable to start listener for connections: Operation not supported
          iperf3: exiting
  I v4:   0.00-1.00 sec 1.66 GBytes 14.2 Gbits/sec sender
  I v6:   error - unable to connect to server ... Connection refused
  ```
- **lsof 端口过滤复核**（同一探针里同时持有 v4/v6 的 UDP+TCP socket）：

  ```
  F lsof_v4: python3 92 root 3u IPv4 197 0t0 UDP localhost:18301 |
             python3 92 root 5u IPv4 199 0t0 TCP localhost:18303 (LISTEN)
  F lsof_v6: python3 92 root 4u IPv6 198 0t0 UDP localhost:18302 |
             python3 92 root 6u IPv6 200 0t0 TCP localhost:18304 (LISTEN)
  ```
- **dig 强制 v6**：`dig -6 @::1` 与默认调用错误完全相同（§2.1）。

## 4. 新发现的内核缺口（只记录，本轮不修）

| # | 缺口 | 位置 | 影响 |
|---|---|---|---|
| 1 | 所有 `SOL_IPV6` sockopt 返回 `EOPNOTSUPP` | `kernel/core/src/util/net/options/mod.rs:169-175`（`new_raw_socket_option` 无 `SOL_IPV6` 分支） | `IPV6_V6ONLY`、`IPV6_MTU`、`IPV6_MULTICAST_*` 等全部失败；**iperf3 的 v6 监听已因此失败（实测）**；dig 也可能受影响 |
| 2 | `ip addr add` / `ip neigh del` 等 netlink 变更请求**无应答时永久阻塞** | 待定位（netlink route socket 的 ACK 路径） | 任何用 netlink 改地址/邻居的工具会挂死（本轮两次踩到） |
| 3 | 无 `AF_INET SOCK_RAW` | `kernel/core/src/syscall/socket.rs:31-102`（match 无 RAW 分支） | `ping`、`traceroute` 等 ICMP 工具两族都不可用 |
| 4 | 以太网 IPv6 出站被丢弃 | `kernel/libs/aster-bigtcp/src/iface/phy/ether.rs:219-224` | IPv6 只能跑 loopback；本轮所有 v6 用例因此限定在 `::1` |

## 5. 最值得修的前 3 项

1. **`SOL_IPV6` sockopt 支持**（缺口 1）——已经实测挡住 `iperf3 -6` 的监听；修好后
   既能救回 iperf3，也能对 `dig` 的两个候选做一次性判别（§2.1）。
2. **netlink 变更请求的 ACK/超时**（缺口 2）——`ip addr`/`ip neigh` 是网络排障的基本工具，
   现在会挂死整个进程。
3. **`AF_INET SOCK_RAW` + ICMP echo**（缺口 3）——`ping` 两族都不可用，是可用性硬缺口。

`getent`/`dig` 的 `AI_ADDRCONFIG` 问题不属于以上三项：前者是 glibc + 环境，
后者的最终判定依赖第 1 项修好后的复测。

## 6. 证据

- `evidence/2026-10-27-ipv6-tools/matrix-raw.txt`（12 组原始行）
- `evidence/2026-10-27-ipv6-tools/diag.txt`（getaddrinfo / sockopt 诊断）
- `evidence/2026-10-27-ipv6-tools/final.txt`（有界 netlink、getent、lsof 复核）
- `evidence/2026-10-27-ipv6-tools/iperf3.txt`（v4/v6 客户端 + 服务端日志）
- `evidence/2026-10-27-ipv6-tools/dig-v6.txt`（默认 vs `-6`）
- `evidence/2026-10-27-ipv6-tools/netlink-hang.txt`（`ip -6 addr add` 卡死 >120 s）

## 7. 边界

- 本轮**不改内核代码**；以上缺口只记录 + 给 `文件:行号`。
- 所有 v6 用例都指向 `::1`；以太网 IPv6 不可用是已知限制（缺口 4）。
- `ping` 记 NA，没有用别的工具凑数。
