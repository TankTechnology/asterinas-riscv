# `IPV6_RECVERR` / `IPV6_DONTFRAG` / `IPV6_MTU`：让 dig 的 v6 路径走通

日期：2026-10-27
内核基线：`4b910c03e`（已含 `SOL_IPV6 IPV6_V6ONLY`）
任务书：`/tmp/tp/IPV6_RECVERR.md`

## 0. 结论

- 上一轮 strace 已定证 dig 死在 `setsockopt(SOL_IPV6, IPV6_RECVERR, 1) = ENOPROTOOPT`
  （见 `2026-10-27-dig-v6-strace.md`）。本轮补上三个 `SOL_IPV6` 选项的读写：
  `IPV6_RECVERR(25)`、`IPV6_DONTFRAG(62)`、`IPV6_MTU(24)`。
- **语义（诚实说明）**：三者目前**只保存标志、可回读，不驱动行为**：
  - `IPV6_RECVERR` —— **未实现错误上报**：我们不会把 ICMP 错误放进错误队列、
    也不会用 `MSG_ERRQUEUE` 递送（v4 的 `IP_RECVERR` 本来也只是存储）；
  - `IPV6_DONTFRAG` —— IPv6 出站路径不设置 DF 位；
  - `IPV6_MTU` —— **无 PMTU 强制**，值按写入原样保存。
  换句话说：这三个选项是用来让工具"能启动"的兼容读写，**不是**完整语义。
- v4 socket 上设这三个 `SOL_IPV6` 选项 → `ENOPROTOOPT`（不误接受）。
- 验证：改前三个选项全 `ENOPROTOOPT` → 改后读写正常；`dig @::1` 从 exit 9
  变为走完 `bind`/`connect`/`sendmsg`；7 域矩阵全绿，v4 `IP_RECVERR` 不变。

## 1. 任务 A：失败用例（改前）

脚本：`tools/x86/net/ipv6_options_check.sh` + `ipv6_options_check.c`
（复用 `ipv6_v6only_check.sh` 骨架）。

改前（内核 `4b910c03e` 构建的 ELF）：

```
V6O ipv6_recverr  set rc=-1 errno=92 (Protocol not available) | get rc=-1 errno=92 value=-1
V6O ipv6_dontfrag set rc=-1 errno=92 (Protocol not available) | get rc=-1 errno=92 value=-1
V6O ipv6_mtu      set rc=-1 errno=92 (Protocol not available) | get rc=-1 errno=92 value=-1
V6O v4_ip_recverr rc=0 errno=0 (Success)
V6O v4_sol6_recverr rc=-1 errno=92 (Protocol not available)
V6O RESULT all FAIL        check_rc=1
```

v4 的 `IP_RECVERR` 在改前就成功（对照），说明失败不是测试环境问题。

## 2. 任务 B：实现（最小集）

| 文件 | 位置 | 改动 |
|---|---|---|
| `kernel/core/src/util/net/options/ipv6.rs` | `CIpv6OptionName` | 新增 `MTU=24`、`RECVERR=25`、`DONTFRAG=62`；其余 IPv6 选项仍 `ENOPROTOOPT` |
| 同上 | `new_ipv6_option` | 三个分支返回对应选项对象；四个选项都实现 `RawSocketOption` |
| `kernel/core/src/net/socket/ip/options.rs` | `IpOptionSet` | 新字段 `ipv6_recverr: bool`、`ipv6_dontfrag: bool`、`ipv6_mtu: i32`（默认 false/false/0）；get/set 分支；`check_ipv6(family)` 统一把 v4 socket 挡成 `ENOPROTOOPT` |
| 同上 | 模块文档 | 明确写出"只保存标志、未实现错误上报/DF/PMTU" |

没有 `unsafe`、没有改 ostd、没有新抽象。

## 3. 任务 C：验证

### 3.1 C1：三个选项读写（改后）

```
V6O ipv6_recverr  set rc=0 | get rc=0 value=1
V6O ipv6_dontfrag set rc=0 | get rc=0 value=1
V6O ipv6_mtu      set rc=0 | get rc=0 value=1280
V6O v4_ip_recverr rc=0
V6O v4_sol6_recverr rc=-1 errno=92 (Protocol not available)
V6O RESULT all PASS      check_rc=0
```

改前三个 IPv6 选项都是 `ENOPROTOOPT`，改后读写与回读值正确；v4 对照与
"v4 上设 SOL_IPV6 → `ENOPROTOOPT`"都保持不变。

### 3.2 C2：`dig @::1` 前后对照

`dig +short @::1 -p 18222 test.local A`（本机 DNS responder）：

```
改前: ;; UDP setup with ::1#18222(::1) ... failed: invalid protocol.    rc=9（exit 9）
改后: 127.0.0.1                                                          rc=0
```

改后 strace 尾段（同一 `-e trace=network,socket,getsockopt,setsockopt`）：

```
setsockopt(9, SOL_IPV6, IPV6_RECVERR, [1], 4) = 0
setsockopt(9, SOL_IPV6, IPV6_V6ONLY, [1], 4) = 0
bind(9, {sa_family=AF_INET6, ... "::", ...}, 28) = 0
connect(9, {sa_family=AF_INET6, sin6_port=htons(18222), ... "::1", ...}, 28) = 0
sendmsg(9, ...) = ...
```

**不只走到了 `bind/connect/sendmsg`，还拿到了应答**（`127.0.0.1`）。

顺带解释了上一轮 trace 里为什么没有 `IPV6_V6ONLY`：BIND 的调用顺序是
`DONTFRAG → MTU → RECVERR → V6ONLY → bind → connect`，上一轮在 `RECVERR` 就退出了。
也就是说 **V6ONLY 与 RECVERR 两处都必须修**。

### 3.3 C3：7 域矩阵

同一内核、`daily_use_checklist_regress.sh`、基座 `target/systemd-health/daily.ext2`：

```
DESKTOP ok  APP ok  APT ok  CLIPBOARD ok  AUDIO ok  USB ok  SUSPEND ok
DAILY_DONE=ok   panic=0   allocation failed=0
```

（`INPUT_METHOD=fail` 是 7 域之外的既有缺口。）v4 的 `IP_RECVERR` 在 C1 的对照项里
保持成功；其他 v4 sockopt 行为未变。

## 4. 边界

- `IPV6_RECVERR` **不上报** ICMP 错误队列；想真正用 `MSG_ERRQUEUE` 的工具不会拿到错误。
- `IPV6_MTU` 不做校验（Linux 会把 <1280 抬到 1280），也不做路径 MTU 发现。
- 其他 `SOL_IPV6` 选项（`IPV6_CHECKSUM`、multicast 等）仍返回 `ENOPROTOOPT`。
- 未 push。
