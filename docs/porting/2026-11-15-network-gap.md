# 网络功能缺口盘点（2026-11-15）

在 Megrez 上逐条实测，不是读代码猜的。每一条都给出"板上现象 → 缺在哪一层 → 工作量"。

## 1. 现状：能用的部分

| 能力 | 状态 | 证据 |
| --- | --- | --- |
| 有线以太网（dwmac） | ✅ | `eth0` UP，MAC `00:48:54:71:00:47`，门户认证后双向可达 |
| IPv4 TCP/UDP/ICMP | ✅ | `curl`/`ping`/`ss`/iperf3 全通 |
| netlink route（RTM_GETLINK/GETADDR/GETROUTE） | ✅ | `ip link show`、`ip addr`、`ip route` 正常 |
| `ip link set <dev> up/down` | ✅ | `do_new_link` 支持 `ifi_flags`/`ifi_change` |
| unix socket / SCM_RIGHTS / pty | ✅ | OpenSSH、dropbear、X11 都跑在上面 |
| cgroup v2 | ✅ | `/sys/fs/cgroup` 挂载、`systemd` 使用 |

## 2. 缺口（板上实测）

| 功能 | 板上现象 | 缺在哪 | 工作量 |
| --- | --- | --- | --- |
| **`ip addr add` / `ip addr del`** | 以前是 `EOPNOTSUPP`（接口只能有启动配置给的地址） | 已支持；地址表从 2 扩到 8（见 §2.3） | 已修 |
| **`ip link set mtu/name`** | 以前返回成功但改不动（见 §2.1，已修） | — | 已修 |
| **`ip route add/del`、`ip neigh add`** | 以前**命令挂住不返回**（10 s 超时）；修复后立即返回 `Operation not supported` | 见 §2.2 | 已修 |
| **veth pair** | `ip link add veth0 type veth peer name veth1` → `RTNETLINK answers: No such device` | `do_new_link` 只按 index 找已有接口，**不解析 LINKINFO kind，也没有"创建接口"的路径**；内核里没有任何虚拟以太网设备 | 大：需要虚拟设备模型 + 双向队列表 + rtnl 创建/删除 + 命名 |
| **bridge** | `ip link add br0 type bridge` → 同上 | 同上，且没有转发表 | 大 |
| **netfilter / iptables / nft** | 内核里没有 netfilter 钩子；`iptables` 也没装（Debian 包可装，但内核不支持会直接报 `can't initialize iptables table`） | 完全没有：需要 `NF_HOOK` 挂点、table/chain/rule 数据结构、`setsockopt(SO_ATTACH_FILTER)` 之外的匹配动作、以及 netlink nfnetlink 协议 | 很大 |
| **tun/tap** | `ls /dev/net/tun` → 不存在 | 没有 tun 驱动，也没有 `/dev/net` | 中：字符设备 + 队列接 Iface |
| **IPv6** | `ip -6 addr` 只显示 `::1/128`（来自 netlink dump 的合成），`socket(AF_INET6)` 不可用 | `kernel/src/net/socket/ip` 只有 IPv4 的 tcp/udp/icmp；iface 层没有 IPv6 邻居/地址状态 | 很大 |
| **`/dev/bus/usb`、`/dev/snd`** | 不存在 | 与本篇无关，记录在 `2026-11-14-system-readiness.md` | — |

## 2.1 已修的：`ip link set` 的三种静默失败

板上实测（修复前）：

```
$ ip link set dev eth0 mtu 1400     # rc=0，但 MTU 仍是 1500
$ ip link show eth0
2: eth0: <BROADCAST,MULTICAST,LOWER_UP> mtu 1500    # 注意：UP 没了
```

`do_new_link` 只处理 `ifi_flags`/`ifi_change`，属性一律忽略；而"只带属性"的请求
`ifi_flags == ifi_change == 0`，落到"把 flags 当作新的知名位状态"那个分支，把 `IFF_UP`
清掉了。改一条 MTU 就等于在核心里把网卡下线。

修复：
- 请求既没有 `ifi_flags` 也没有 `ifi_change` 时**不动 flags**；
- `IFLA_MTU`、`IFLA_IFNAME` 与当前值不同时返回 `EOPNOTSUPP`（而不是假装成功）。

板上复验：`ip link set dev eth0 mtu 1400` → `Operation not supported`，MTU 不变、
`UP` 保持；`ip link set lo down/up` 仍然有效；默认路由与网关 ping 不受影响。

## 2.2 已修的：`ip route add` 挂起不返回

```
$ timeout 5 ip route add 10.98.0.0/24 dev lo ; echo $?
124          # 修复前：永远不返回
```

strace 显示内核确实回了 `NLMSG_ERROR error=-EOPNOTSUPP`（seq 对得上），但 iproute2
又阻塞在 `recvmsg`。用最小 netlink 客户端（`/tmp/nlprobe.c`）复现后对比发现：
**解析阶段失败的请求，错误回复里的 `nlmsg_pid` 是客户端发来的 0**，而解析成功、
进入分发阶段后才被规范化成请求端的端口号（Linux 的 `netlink_ack` 就是这么写的）。
iproute2 6.15 按 `nlmsg_pid` 过滤回复，pid=0 的回复被当成外来消息丢弃，于是它一直在等
一个它永远不会认的消息。

修复：`bound.rs` 里 `SkippedErr` 分支在回错误前把 `header.pid` 设成 `local_port`，
与正常路径一致。板上复验：`ip route add/del`、`ip neigh add` 全部立即返回
`Operation not supported`（rc=2），默认路由与网关不受影响。

## 2.3 已加的：`ip addr add` / `ip addr del`

以前 `RTM_NEWADDR` 直接返回 `EOPNOTSUPP`，一个接口只能有启动 profile 给它的地址。
现在：

- `aster-bigtcp` 的 `Iface` 暴露 `add_ip_addr` / `del_ip_addr` / `ip_addrs`（走 smoltcp 的地址表）；
- `RTM_NEWADDR` 真正加地址（重复 → `EEXIST`，表满 → `ENOSPC`）；
- 新增 `RTM_DELADDR` 的解析与处理（不存在 → `EADDRNOTAVAIL`）；
- 地址 dump 遍历整张表，而不是每个协议族只报第一个。

**顺带发现地址表太小**：smoltcp 默认 `IFACE_MAX_ADDR_COUNT = 2`，而 loopback 自己就占了
两个（127.0.0.1/8 和 ::1/128），所以第一次 `ip addr add` 必然 `ENOSPC`。工作区依赖
改成 `iface-max-addr-count-8`。

板上复验：

```
$ ip addr add 10.99.99.1/32 dev lo      # rc=0
$ ip -4 addr show lo
    inet 127.0.0.1/8 scope host lo
    inet 10.99.99.1/32 scope global lo
$ ip addr del 10.99.99.1/32 dev lo      # rc=0，回到单地址
```

eth0 的地址与默认路由不受影响，网关可达。

## 3. rtnl 的现状（决定 veth 怎么做）

`kernel/src/net/socket/netlink/route/kernel/link.rs`：

- `RTM_GETLINK`：dump 或按 index/name 查已有的 `Iface`，正常。
- `RTM_NEWLINK` / `RTM_SETLINK`：**只处理 `ifi_flags`/`ifi_change`**，靠 `body.index` 找接口；
  没有 `body.index` 时直接返回 `ENODEV`（这就是 `ip link add` 看到的 "No such device"）。
- 没有 `LINKINFO`（`IFLA_LINKINFO` / `IFLA_INFO_KIND` / `IFLA_INFO_DATA`）的解析。

要做 veth，至少需要：

1. `LinkSegment` 解析 `IFLA_LINKINFO` → `kind = "veth"` 与 `peer` 名字；
2. `NetNamespace` 提供"注册一个新 `Iface`"的入口（现在接口表由驱动在初始化时填）；
3. 一个 `veth` 设备：两个 `Iface` 共享一对队列，A 的 TX 直接进 B 的 RX（不经过硬件）；
4. `RTM_DELLINK`（删除）与 `RTM_SETLINK` 的 name/MTU 变更。

## 4. 建议顺序（按性价比）

1. **veth**（本篇评估后决定是否动手）：容器网络、用户态测试（`netns` 里跑两个进程）都靠它；
   不需要硬件，风险局限在 netlink 与设备注册，**不动 eth0 和默认路由**。
2. **tun/tap**：能让用户态 VPN/隧道跑起来，但需要字符设备与队列接线，工作量约是 veth 的一半，
   价值取决于是否要做 WireGuard。
3. **netfilter 最小集**：只做 `filter` 表的 INPUT/OUTPUT 与 `-j ACCEPT/DROP`，可以让
   `iptables -L` 有输出、规则真的生效。**风险最高**（写错一条规则会把自己锁在外面），
   而且需要 nfnetlink 或 `setsockopt(IPT_SO_SET_REPLACE)` 的老接口，工作量比 veth 大。
4. **IPv6**：影响面最大（地址、邻居发现、路由、socket 层全要补），不建议近期做。

## 5. 验收方式（每项）

- QEMU 先验：`ip link add ... type veth` → `ip link show` 看到两个口 → 两端配置地址 →
  `ping` 互通 → `ip link del`。
- 板上复验同一组命令，**不碰 eth0/默认路由**，失败立即回滚内核。
- 每项都要有 `cargo osdk test` 432/0 + preflight 记录。
