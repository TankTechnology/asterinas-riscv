# 补两个 sockopt 缺口：`IP_MTU_DISCOVER` 与 `NETLINK_EXT_ACK`

日期：2026-10-27
基线：`e1f2c865f`（strace 扫描定证两个 `ENOPROTOOPT`）
任务书：`/tmp/tp/SOCKOPTS2.md`

## 0. 结论

- `SOL_IP/IP_MTU_DISCOVER`（opt 10）与 `SOL_NETLINK/NETLINK_EXT_ACK`（opt 11）
  现在 **set/get 都可用**，默认值与 Linux 一致（`MTU_DISCOVER` 默认 1，`EXT_ACK` 默认 0）。
- 两者都**只存值，不驱动任何行为**：
  - `IP_MTU_DISCOVER`：不做 PMTU 探测、不在 egress 设置 DF 位；
  - `NETLINK_EXT_ACK`：不生成扩展 ACK 属性，netlink 回复文本不变。
- 取值范围按 **Linux 实测**：`IP_MTU_DISCOVER` 接受 **0..=5**（`4` 也接受，Linux 只做
  `DONT..OMIT` 范围检查），`6`/`-1` 返回 **`EINVAL`**；`NETLINK_EXT_ACK` 是**布尔**
  （任意非 0 读回 1），不返回 `EINVAL`。
- **与任务书的两处偏差（以本机 Linux 实测为准，见 §1.1）**：
  1. 任务书要求「`IP_MTU_DISCOVER` 给 4+ ⇒ `EINVAL`」——Linux 实测 4 和 5 都被接受，
     只有 `>=6` / `<0` 才 `EINVAL`；dig 设的正是 **5**（`IP_PMTUDISC_OMIT`），
     若拒绝 5，C2 里 dig 的 `ENOPROTOOPT` 只会变成 `EINVAL`，不算修好。
  2. 任务书要求「v6 上设 `SOL_IP` 选项 ⇒ `ENOPROTOOPT`」——Linux 实测**接受**
     （v6 UDP/TCP 的 `IP_MTU_DISCOVER`、`IP_TTL` 都成功并读回），我们的实现同样接受；
     按任务书拒绝会把当前能用的 `IP_TOS`/`IP_TTL` on v6 变成回归。
- 改前/改后：`tools/x86/net/sockopt_gap_check.sh` **FAIL(15) → PASS**；
  12 工具重扫中 dig/ss 的 `ENOPROTOOPT setsockopt` **消失**，`rseq`/`membarrier`/
  `io_uring_setup` 等既有缺口**仍在**；7 域矩阵完整通过（`INPUT_METHOD` 为既有域外缺口），
  `panic=0`、`allocation failed=0`。
- 同一份 C 用例在**本机 Linux** 上全 PASS（`evidence/.../linux-control.txt`），
  说明测试编码的就是 Linux 语义，不是为我们的实现量身定做。

## 1. 任务 A：失败用例（改前）

脚本：`tools/x86/net/sockopt_gap_check.sh`（复用 `ipv6_options_check.sh` 骨架），
C 源：`tools/x86/net/sockopt_gap_check.c`。改前原始输出（`evidence/.../c1-before.txt`）：

```
SG mtu_discover_default FAIL errno=92 default=-1 (linux default 1)
SG mtu_discover_0 FAIL set_errno=92 get_errno=92 got=-1
...
SG mtu_discover_reject_6 FAIL set_errno=92 (want EINVAL=22)
SG v6_sol_ip_accepted_linux_parity FAIL set_errno=92 get_errno=92 got=-1
SG ext_ack_default FAIL errno=92 default=-1 (linux default 0)
SG ext_ack_set_1 FAIL set_errno=92 get_errno=92 got=-1
SG RESULT all FAIL failures=15
```

`92 = ENOPROTOOPT`：改前两个选项都未被识别（与 `e1f2c865f` 的扫描一致）。

### 1.1 与任务书假设不符的两点（本机 Linux 7.2.6 实测）

```
v4 UDP default get IP_MTU_DISCOVER -> 1
v4 set 0/1/2/3/4/5 -> ok（逐值读回相同）;  set 6 -> EINVAL;  set -1 -> EINVAL
v6 UDP set SOL_IP IP_MTU_DISCOVER=1 -> ok, get=1
v6 TCP set SOL_IP IP_MTU_DISCOVER=1 -> ok, get=1
v6 UDP set SOL_IP IP_TTL=16         -> ok, get=16
netlink default get NETLINK_EXT_ACK -> 0
netlink set 1 -> get 1;  set 0 -> get 0;  set 2 -> get 1;  set -1 -> get 1
```

因此实现取 Linux 语义：`MTU_DISCOVER ∈ 0..=5`，`EXT_ACK` 布尔，v6 上接受 `SOL_IP`。
测试里 `v6_sol_ip_accepted_linux_parity` 断言的是**接受并与 Linux 一致**，
而不是任务书写的 `ENOPROTOOPT`。

## 2. 任务 B：实现（只存值）

| 文件:行号 | 改动 |
|---|---|
| `kernel/core/src/net/socket/ip/options.rs:30` | `IpOptionSet` 新增 `ip_mtu_discover: i32`（注释写明 stored only） |
| 同上 `:54`、`:68` | TCP/UDP 构造默认值 **1**（`IP_PMTUDISC_WANT`，Linux 默认） |
| 同上 `:98-100` | `get_option`：读回存储值 |
| 同上 `:150-157` | `set_option`：**范围校验 0..=5，否则 `EINVAL`**，然后存值 |
| 同上 `:208` | `impl_socket_options!` 新增 `IpMtuDiscover(i32)` |
| `kernel/core/src/util/net/options/ip.rs:78` | `CIpOptionName::MTU_DISCOVER` → `IpMtuDiscover::new()` |
| 同上 `:118` | `impl_raw_ip_socket_option!(IpMtuDiscover)`（用户态 int 读写） |
| `kernel/core/src/net/socket/netlink/options.rs:10` | 新增 `ExtAck(bool)` |
| `kernel/core/src/util/net/options/netlink.rs:22,30,39` | `EXT_ACK = 11` 枚举 + 分支 + `impl_raw_socket_option!(ExtAck)`（`bool` 的 `ReadFromUser` 已按「非 0 即真」实现） |
| `kernel/core/src/net/socket/netlink/common/mod.rs:50,57` | 选项集新增 `ext_ack: bool`，默认 `false` |
| 同上 `:201-204` | `get_option`：读回存储值 |
| 同上 `:242-246` | `set_option`：存值（布尔归一化） |

语义诚实表：

| 选项 | 存了什么 | **没做什么** |
|---|---|---|
| `IP_MTU_DISCOVER` | int 0..=5，默认 1 | 不做 PMTU 探测、不设置 DF 位、不影响分片 |
| `NETLINK_EXT_ACK` | bool，默认 0 | 不生成扩展 ACK 属性、不改变 netlink 错误回复 |

没有 `unsafe`、没有改 ostd、没有引入新抽象（沿用 `IpOptionSet` / netlink `OptionSet` 与既有
raw option 框架）。`deny(unsafe_code)` 未触碰。

### 2.1 第一版的一个真实 bug（被字符串检查抓住）

第一版把范围校验写成 `impl ReadIpOption for IpMtuDiscover`。但
`impl_raw_ip_socket_option!` 展开为 `let input = ReadIpOption::read_ip_option(...)?; self.set(input);`，
而 `set()` 的参数类型是宏声明的 **`i32`** ⇒ 类型推断选中 `impl ReadIpOption for i32`
（无校验），新写的 `impl` 成了死代码。

证据链：ELF 里找不到 `"invalid IP_MTU_DISCOVER value"` 字符串；改后第一轮测试
`set 6` 返回 0 且读回 6。改法：把校验移到 `IpOptionSet::set_option`（唯一存值点），
重建后字符串进入 ELF、`set 6` 返回 `EINVAL`。

## 3. 任务 C：三层验证

### C1：失败用例 改前 FAIL → 改后 PASS

改后（`evidence/.../c1-after.txt`）：

```
SG mtu_discover_default PASS errno=0 default=1 (linux default 1)
SG mtu_discover_0..5 PASS  （逐值 set/get 相同）
SG mtu_discover_reject_6 PASS set_errno=22 (want EINVAL=22)
SG mtu_discover_reject_-1 PASS set_errno=22 (want EINVAL=22)
SG v6_sol_ip_accepted_linux_parity PASS set_errno=0 get_errno=0 got=1
SG ext_ack_default PASS errno=0 default=0 (linux default 0)
SG ext_ack_set_1/0/2/-1 PASS （读回 1/0/1/1，布尔语义）
SG RESULT all PASS
```

### C2：12 工具重扫（同一 checklist、同一基座镜像）

| 工具 | 改前 | 改后 |
|---|---|---|
| dig | `1 ENOPROTOOPT setsockopt` | **消失**（其余不变：`rseq`/`membarrier`/`io_uring_setup` 仍在） |
| ss | `1 ENOPROTOOPT setsockopt` | **消失**（`EINVAL prctl`、`ENOSYS rseq` 仍在） |

改后 dig 多出 `1 EAGAIN futex`（一次 futex 唤醒竞争，属于正常瞬态，不是缺陷）。
其余 10 个工具的错误集合与 `e1f2c865f` 一致；`rseq`（12/12）、`membarrier`、
`io_uring_setup`、`landlock_create_ruleset` 均**仍然存在**（本轮未实现它们）。

### C3：7 域矩阵

第一次运行把门禁超时设为 1500 s，**被默认 checklist 的稳定性段（6 × 300 s）切断**：
6 个域当时已 `ok`、`DAILY_STAB_SUSPEND` 三轮 `ok`、`panic=0`、`allocation failed=0`，
但没有 `DAILY_DONE`，门禁报 `guest workload did not complete`。
这属于**计时配置错误**（不是内核失败）；该次运行的 console 在重跑前随工作区一起删除，
未留存原始文件（上述读数来自当时输出）。

放宽到 3600 s 重跑后**矩阵完整**（`evidence/.../c3-matrix.tsv`）：

```
DESKTOP       ok   Xorg+DRI3
APP           ok   xterm
APT           ok   xclip+xterm+alsa-utils-installed
CLIPBOARD     ok   xclip-round-trip
INPUT_METHOD  fail engine--no-text:      <- 7 域之外的既有缺口
AUDIO         ok   aplay-hw:0,0
USB           ok   mounted-/dev/sda-read-ASTERINAS-USB-OK
SUSPEND       ok   woke-up-disk-and-io-ok
DAILY_STAB_DONE uptime=2646.66
DAILY_DONE=ok
```

`panic=0`、`allocation failed=0`；不带 `-f` 的 strace、单线程工具行为不变
（12 工具重扫全部正常退出）。

## 4. 边界

- **只存值**：`IP_MTU_DISCOVER` 与 `NETLINK_EXT_ACK` 不改变任何数据包或 netlink 回复；
  不要据此认为 PMTU 探测或扩展 ACK 已实现。
- `NETLINK_EXT_ACK` 只在 netlink socket 上生效；其它 socket 类型走原有 `ENOPROTOOPT` 路径。
- 本轮未实现 `rseq` / `membarrier` / `io_uring` / `landlock`（见上一轮 `top3` 讨论）。
- **顺带观察（未改）**：矩阵 console 里出现 `Unimplemented syscall number: 334`。
  334 是 x86_64 的 `rseq` 编号，而 `SYS_RSEQ = 293` 只登记在
  `kernel/core/src/syscall/arch/generic.rs:409`（RISC-V 等架构），
  x86_64 的 `arch/x86.rs` 没有 rseq 项 ⇒ x86_64 上 rseq 走**未知 syscall 路径**
  （返回同样的 `ENOSYS`，但每次进程启动多一条 WARN）。
  上一轮文档说「rseq 是有意的 ENOSYS 桩」对 generic 架构成立，对 x86_64 不准确；
  两轮的结论（工具都能回退、不阻塞）不受影响。补一行 `SYS_RSEQ = 334` 可消除该 WARN，
  但不在本轮范围，记录待办。
- 未 push。

## 5. 证据

- `evidence/2026-10-27-sockopt-gaps/c1-before.txt`、`c1-after.txt`（同一 C 用例前后）
- `evidence/2026-10-27-sockopt-gaps/c2-after-aggregate.txt`（12 工具改后全量聚合）
- `evidence/2026-10-27-sockopt-gaps/c2-after-dig-ss.txt`（dig/ss 改后明细）
- `evidence/2026-10-27-sockopt-gaps/c3-matrix.txt`（7 域矩阵）
- 主机工作区：`target/sockopt-gap-before/`、`target/sockopt-gap-after2/`、
  `target/sm-strace-after/`、`target/matrix-sockopt/`
