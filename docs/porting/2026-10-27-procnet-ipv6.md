# `/proc/net/tcp6|udp6`：4×4 对照表与 IPv6 UDP 的补齐

日期：2026-10-27
内核基线：`bd0596fa7`；本轮改动后重新构建
任务书：`/tmp/tp/IPV6PROC.md`

## 1. 任务 A：先出 4×4 表（改前）

探针同时保持 4 个 socket 存活（`target/ipv6-proc`，`/var/tmp/v6/hold.py`）：

| socket | `/proc/net/tcp` | `tcp6` | `udp` | `udp6` |
|---|---|---|---|---|
| **TCP v4** `0.0.0.0:18081` + listen | **HIT** | MISS | MISS | MISS |
| **TCP v6** `::1:18082` + listen | MISS | **HIT** | MISS | MISS |
| **UDP v4** `0.0.0.0:18083` | MISS | MISS | **HIT** | MISS |
| **UDP v6** `::1:18084` | MISS | MISS | MISS | **MISS**（`bind` 失败 `EAFNOSUPPORT(97)`） |

原始行：

```
V6 disable_ipv6_all=cat: /proc/sys/net/ipv6/conf/all/disable_ipv6: No such file or directory
V6 ipv6_addrs=1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65535 qlen 1000|    inet6 ::1/128 scope host proto kernel_lo |
V6 hold: BIND tcp4 ok local=('0.0.0.0', 18081)
V6 hold: BIND tcp6 ok local=('::1', 18082, 0, 0)
V6 hold: BIND udp4 ok local=('0.0.0.0', 18083)
V6 hold: BIND udp6 FAILED OSError(97, 'Address family not supported by protocol')
V6 tcp_rows=1 tcp6_rows=1 udp_rows=1 udp6_rows=0
V6 tcp6_content:    0: 00000000000000000000000001000000:46A2 ... 0A ...
```

结论（这张表就是判据）：

1. **procfs 侧没有 bug**：TCP v6 socket 正确出现在 `tcp6`、**不出现在** `tcp`，
   地址格式与 Linux 一致（`::1` → `00000000000000000000000001000000`），
   `ss -tln`、`lsof -i` 也都能看到它。
2. **上一轮「v6 行不出现」是第三种情况**：既不是 procfs 过滤，也不是纯测试问题，
   而是 **`socket(AF_INET6, SOCK_DGRAM)` 根本创建不出来**——
   `kernel/core/src/syscall/socket.rs:59` 的 `SOCK_DGRAM` 分支只匹配 `AF_INET`，
   IPv6 UDP 落到 `:102` 的 `EAFNOSUPPORT`。没有 v6 UDP socket，`udp6` 自然为空。
3. `disable_ipv6` 路径在我们内核里不存在（`/proc/sys/net/ipv6/...` 未暴露），
   但 IPv6 在 lo 上是启用的（`::1/128`），所以不是环境限制。

## 2. 任务 B：改动（沿用既有模式，不是新抽象）

| 文件 | 改动 |
|---|---|
| `kernel/core/src/syscall/socket.rs:59-74` | `SOCK_DGRAM` 分支扩为 `AF_INET \| AF_INET6`，按 domain 传 `IpAddressFamily` |
| `kernel/core/src/net/socket/ip/datagram/mod.rs` | `DatagramSocket` 增加 `family: IpAddressFamily`；新增 `check_family()`；`bind`/`connect`/`sendmsg` 校验地址族（不匹配 → `EAFNOSUPPORT`）；未绑定时的 `getsockname` 返回该族的 unspecified 地址；IPv6 的 `ioctl` 返回 `ENOTTY` |

这是照抄 `StreamSocket` 已有的 family 处理（`stream/init.rs:96-101,128-136`），
没有引入新抽象、没有 `unsafe`、没有改 ostd。

## 3. 任务 C：三层验证

### 3.1 C1：改后 4×4 表

| socket | `tcp` | `tcp6` | `udp` | `udp6` |
|---|---|---|---|---|
| **TCP v4** `0.0.0.0:18081` | **HIT** | MISS | MISS | MISS |
| **TCP v6** `::1:18082` | MISS | **HIT** | MISS | MISS |
| **UDP v4** `0.0.0.0:18083` | MISS | MISS | **HIT** | MISS |
| **UDP v6** `::1:18084` | MISS | MISS | MISS | **HIT** |

```
V6 matrix_udp6  in_tcp=MISS in_tcp6=MISS in_udp=MISS in_udp6=HIT
V6 tcp_rows=1 tcp6_rows=1 udp_rows=1 udp6_rows=1
V6 udp6_content:    0: 00000000000000000000000001000000:46A4 00000000000000000000000000000000:0000 07 ...
```

v6 地址格式与 Linux 一致（`::1` → `00000000000000000000000001000000`），
未绑定 UDP 的状态码 `07`（`TCP_CLOSE`）也与 Linux 相同。

### 3.2 C2：工具能看到 v6 socket

```
V6 ss_tln:  ... LISTEN 0  0  0.0.0.0:18081 ... | LISTEN 0  0  [::1]:18082 ... 
V6 ss_uln:  ... UNCONN 0  0  0.0.0.0:18083 ... | UNCONN 0  0  [::1]:18084 ...
V6 lsof_i:  python3 81 root 4u IPv6 192 0t0 TCP localhost:18082 (LISTEN)
            python3 81 root 6u IPv6 194 0t0 UDP localhost:18084
```

`ss -tln` / `ss -uln` / `lsof -i` 都贴出了 `[::1]` 的行。

### 3.3 数据路径（补充）

::1 上两个 UDP v6 socket 的真实收发：

```
RT v6: srv ('::1', 18090, 0, 0) cli ('::1', 32768, 0, 0)
RT v6: server got b'hello-v6' from ('::1', 32768, 0, 0) in 0.004s
RT v6: client got b'pong-v6' from ('::1', 18090, 0, 0)
RT v6: v4 server got b'hello-v4' from ('127.0.0.1', 32769)
```

IPv6 UDP 在 loopback 上双向可用；同轮的 v4 对照也正常。

### 3.4 C3：7 域矩阵

同一内核、`daily_use_checklist_regress.sh`、基座 `target/systemd-health/daily.ext2`：

```
DESKTOP ok  APP ok  APT ok  CLIPBOARD ok  AUDIO ok  USB ok  SUSPEND ok
DAILY_DONE=ok   panic=0   allocation failed=0
```

（`INPUT_METHOD=fail` 是 7 域之外的既有缺口。）v4 行为在同轮的 4×4 表与
loopback 对照里均未变化。`ss` 走的是 `NETLINK_SOCK_DIAG`，同样能列出 v6 socket，
说明 procfs 与 sock_diag 两条视图一致。

## 4. 边界与未验证

- **已验证**：IPv6 UDP 在 **::1（loopback）** 上的 bind/列表/双向收发。
- **未验证**：**以太网 IPv6 出站**——`ether.rs:219-224` 仍明确丢弃 IPv6 出站包
  （缺邻居发现），所以 v6 只能跑 loopback，不能跑 eth0。
- **未实现**：`IPV6_V6ONLY`、IPv4-mapped IPv6（双栈）、IPv6 的 socket ioctl/选项。
- 本轮只保证：v6 UDP socket **能创建、能 bind、能被 procfs/ss/lsof 看到**。
- 未 push。
