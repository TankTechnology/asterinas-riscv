# 12 个工具在 syscall 层的真实失败清单（`strace -f`，看到具体 errno）

日期：2026-10-27
内核基线：`1936d59c9`（`strace -f` 可用于多线程程序之后）
任务书：`/tmp/tp/STRACE_MATRIX.md`

## 0. 结论

- 12 个工具（jq 按任务书跑了一次无效 JSON，另加一次有效 JSON 作对照；聚合表只计前者）
  共出现 **20 类「errno + syscall」组合**。按调用数看绝大部分是
  `ENOENT` 族的文件探测（`openat` 214 次、`newfstatat` 72 次、`access` 26 次 …），
  **这些在 Linux 上同样发生，不是缺陷**（§3.C 逐条给出判断依据与本机 Linux 对照）。
- 真正的内核缺口只有 **6 条**：
  - **未实现（ENOSYS）4 条**：`rseq`、`membarrier`、`io_uring_setup`、`landlock_create_ruleset`；
  - **sockopt 缺失（ENOPROTOOPT）2 条**：`IP_MTU_DISCOVER`（SOL_IP）、`NETLINK_EXT_ACK`（SOL_NETLINK）。
- **`rseq` 是 12/12 工具都会触发的**，但内核里是**有意返回 `ENOSYS` 的桩**
  （`kernel/core/src/syscall/rseq.rs:5-25` 注释写明这是模拟 Linux 的 feature probe），
  glibc 收到后禁用 rseq、走回退路径，**没有用户可见失败**。频率最高 ≠ 最该修（§4）。
- 两条 `ENOPROTOOPT` 是**唯一「Linux 支持、我们不支持、且修复不需要新抽象」**的项，
  列为 top2（§4）。

## 1. 方法

- 门禁：`make test_x86_daily_use` + 自定义 checklist；基座 `target/app-matrix/daily.ext2`
  （30 工具矩阵留下的镜像，12 个工具全部已装，`missing=[]`，没有为此跑 apt）。
- 每个工具：`timeout 20 strace -f -o /var/tmp/sm/<tool>.trace <cmd>`（**不带 `-c`、不带
  `-e trace=` 过滤**，避免漏掉 `rseq`/`membarrier` 这类不在 `%file,%network,%desc` 里的调用）。
- 跑完**立即**抽取 `grep ' = -1 '` 行，按 `(errno, syscall)` 聚合并打印到 console，
  然后删除原始 trace（只保留错误行信息，原始 trace 不留档）。
- 第一轮的两个「例子」总是 `ld.so.preload` 和 `rseq`，无法用于定性；因此**第二轮**同一批命令
  重跑一次，只打印**非 ENOENT** 的唯一调用签名（地址归一化为 `0xADDR`），用于 §3 的判断依据。
- 命令与偏差（如实记录）：
  - `jq . /etc/hostname` 按任务书执行（rc=5：内容不是 JSON，预期），另跑一份有效 JSON
    做对照；两者 syscall 错误集合一致，聚合表只计入 `/etc/hostname` 那次。
  - `dig +time=1 +tries=1 @127.0.0.1 localhost`：加 `+time/+tries` 只是防止重试占满 20 s 窗口。
  - `git status` 在 `/var/tmp/sm/repo`（`git init` + 一个未跟踪文件）。
- **所有计数都是 strace 下的形态，不做时间结论。** 本轮不改内核、不跑 7 域矩阵、不 push。

## 2. 汇总表（12 工具去重）

| errno | syscall | 出现在几个工具 | 调用数 | 工具 |
|---|---|---|---|---|
| ENOENT | access | 12 | 26 | 全部 |
| ENOSYS | rseq | 12 | 12 | 全部 |
| ENOENT | openat | 10 | 214 | curl,dig,git,jq,lsof,py,tar,top,vim,xz |
| ENOTTY | ioctl | 9 | 80 | curl,dig,git,jq,py,ss,top,vim,xz |
| ENOENT | newfstatat | 6 | 72 | curl,git,jq,py,top,vim |
| ENOENT | statfs | 4 | 8 | lsof,ss,tar,vim |
| ENOENT | readlink | 3 | 5 | dig,git,jq |
| EINVAL | prctl | 2 | 8 | ss,top |
| EINVAL | readlink | 2 | 4 | jq,py |
| ENOENT | connect | 2 | 4 | curl,top |
| ENOENT | open | 2 | 3 | dig,py |
| ENOPROTOOPT | setsockopt | 2 | 2 | dig,ss |
| EOPNOTSUPP | getxattr | 1 | 65 | lsof |
| EINPROGRESS | connect | 1 | 1 | curl |
| ENOTCONN | getpeername | 1 | 1 | dig |
| ENOSYS | membarrier | 1 | 1 | dig |
| ENOSYS | io_uring_setup | 1 | 1 | dig |
| ESPIPE | lseek | 1 | 1 | py |
| ECONNREFUSED | connect | 1 | 1 | py |
| ENOSYS | landlock_create_ruleset | 1 | 1 | xz |

工具退出码（用于说明哪些失败是命令本身预期）：`git 0 / rg 0 / jq(/etc/hostname) 5 /
curl 7 / dig 9 / ss 0 / lsof 1 / python 1 / tar 2 / xz 0 / vim 0 / top 0`。

## 3. 三档分类（每条都给判断依据）

### A. 未实现（ENOSYS）

| 项 | 工具数 | 原始行 | 判断依据 |
|---|---|---|---|
| `rseq` | 12 | `rseq(0xADDR, 0xADDR, 0, 0xADDR) = -1 ENOSYS` | `kernel/core/src/syscall/rseq.rs:12-25` 是**显式桩**，注释说明「Debian libc 启动时探测 rseq，这里返回 Linux 的 feature-detection 错误」。**更正（2026-10-27 晚）**：x86_64 的 `arch/x86.rs` 没有登记 rseq(334)，实际走未知 syscall 路径（同样返回 ENOSYS，另打一条 WARN）；桩只对 generic 架构（RISC-V 的 293）生效，详见 `2026-10-27-sockopt-gaps.md` §4。glibc 收到 ENOSYS 后永久禁用 rseq（上一轮 Firefox 观测到只有前 3 个线程尝试、之后不再调用），**无用户可见失败** |
| `membarrier` | 1 | `membarrier(MEMBARRIER_CMD_QUERY, 0) = -1 ENOSYS` | 内核无 membarrier。调用方（dig 进程树，未进一步归因）探测失败后走回退路径——**本轮只确认没有观察到因此失败**，未验证具体回退实现 |
| `io_uring_setup` | 1 | `io_uring_setup(256, {flags=0, ...}) = -1 ENOSYS` | 内核无 io_uring（`anon_inodefs.rs:72` 注释也把它列在「仅部分成员」里）。调用方是 dig 进程树中的某个组件（未归因）；dig 最终 rc=9 的原因是 `127.0.0.1` 没有 DNS 服务（`no servers could be reached`），不是 io_uring 失败 |
| `landlock_create_ruleset` | 1 | `landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION) = -1 ENOSYS` | xz 用 `LANDLOCK_CREATE_RULESET_VERSION` 探测沙箱能力（参数即证据）；ENOSYS 后 xz 仍正常输出 `--version`（rc=0），说明该探针失败不影响功能 |

### B. 已实现但缺 option / 语义不符（需要对齐 Linux）

| 项 | 工具数 | 原始行 | 判断依据 |
|---|---|---|---|
| `IP_MTU_DISCOVER`（SOL_IP=0, opt=10） | 1（dig） | `setsockopt(9, SOL_IP, IP_MTU_DISCOVER, [5], 4) = -1 ENOPROTOOPT` | Linux 支持该 option（5 = `IP_PMTUDISC_OMIT`）。我们的 `util/net/options/ip.rs:33` **只有枚举名**，`new_ip_option()` 的 match 只有 `TOS/TTL/HDRINCL/RECVERR`，其余落到 `ip.rs:77` 的 `_ => ENOPROTOOPT`。**是 option 级缺失，不是 SOL_IP 整层不支持** |
| `NETLINK_EXT_ACK`（SOL_NETLINK=270, opt=11） | 1（ss） | `setsockopt(3, SOL_NETLINK, NETLINK_EXT_ACK, [1], 4) = -1 ENOPROTOOPT` | Linux 4.6+ 支持。`util/net/options/netlink.rs:14-31` 的 `CNetlinkOptionName` 只有 `ADD_MEMBERSHIP/DROP_MEMBERSHIP/PKTINFO`，opt 11 在 `try_from` 就失败 → ENOPROTOOPT。ss 不因此失败（rc=0），只是拿不到扩展 ACK 文本 |

> 说明：`ENOPROTOOPT` 不在任务书 B 档的 errno 列表里（那里列的是 EPERM/EINVAL/ENOTTY/EOPNOTSUPP），
> 但这两条的实质是「层级已实现、选项未实现」，归入 B 并注明 errno 差异。
> `EOPNOTSUPP` 出现在 `getxattr` 上，但**不是**语义不符 —— 归入 C（见下）。

### C. 正常错误（不算缺陷，逐条依据）

| 项 | 工具数 | 原始行（例） | 判断依据 |
|---|---|---|---|
| ENOENT `openat`/`newfstatat`/`access`/`readlink`/`statfs`/`open`/`connect` | 2–12 | `access("/etc/ld.so.preload", R_OK) = -1 ENOENT`；`connect(3, {sa_family=AF_UNIX, sun_path="…"}, 110) = -1 ENOENT`（具体路径未保留，原始 trace 已删） | 动态加载器/程序探测可选文件与可选 unix socket，Linux 完全相同。**`/etc/ld.so.preload` 每个工具都会试一次** |
| ENOTTY `ioctl` | 9（80 次） | `ioctl(1, TCGETS, 0xADDR) = -1 ENOTTY`；`ioctl(1, TIOCGWINSZ, ...) = -1 ENOTTY` | `isatty()`/终端尺寸探测；本机 Linux 实测 `ioctl(pipe, TCGETS)` → **ENOTTY(25)**，与 guest 同值。ioctl 的 ENOTTY 是「该 fd 不支持此 ioctl」的标准答复 |
| EINVAL `readlink` | 2 | `readlink("/tmp", ...) = -1 EINVAL`；`readlink("/usr/bin/python3.13", ...) = -1 EINVAL` | 对**非符号链接**调用 readlink 就是 EINVAL；本机 Linux 实测 `readlink("/tmp")` → **EINVAL(22)**。guest 里 `/usr/bin/python3.13` 是普通文件 |
| EINVAL `prctl(PR_CAPBSET_READ)` | 2（8 次） | `prctl(PR_CAPBSET_READ, ... /* CAP_??? */) = -1 EINVAL` | ss/top 用「枚举 cap 直到 EINVAL」的方式探测 bounding set。我们的 `prctl.rs:76` 对合法 cap 返回 0/1，`parse_capability`→`capabilities.rs:76-88` 只对 **cap > 40**（`CAP_CHECKPOINT_RESTORE`，与 Linux 相同）返回 EINVAL。本机 Linux 实测 cap 0/40 → **1**，cap 41 → **EINVAL(22)**，行为一致 |
| EOPNOTSUPP `getxattr("system.sockprotoname")` | 1（65 次） | `getxattr("/proc/60/fd/9", "system.sockprotoname", ...) = -1 EOPNOTSUPP` | procfs 的 fd 项没有 xattr handler，Linux 对同一路径同样返回 **ENOTSUP(95)**（本机 Linux 实测 `/proc/self/fd/0` → 95，`EOPNOTSUPP` 与 `ENOTSUP` 同值）。lsof 用它取 socket 协议名，失败即回退 |
| ENOTCONN `getpeername` | 1 | `getpeername(9, ..., [128]) = -1 ENOTCONN` | dig 对未连接 socket 取对端地址；Linux 同 |
| EINPROGRESS `connect` | 1 | `connect(4, {...127.0.0.1:1...}) = -1 EINPROGRESS` | 非阻塞 connect 的中间态，curl 随即等可写事件；Linux 同 |
| ECONNREFUSED `connect` | 1 | `connect(3, {127.0.0.1:1}) = -1 ECONNREFUSED` | 命令本身就是连一个没人监听的端口（python 例子），预期失败 |
| ESPIPE `lseek` | 1 | `lseek(0, 0, SEEK_CUR) = -1 ESPIPE` | 对管道/终端 seek；Linux 同 |
| ENOENT `connect` | 2 | `connect(... AF_UNIX ...) = -1 ENOENT` | 可选 unix socket 不存在（nscd 等），Linux 同 |

## 4. 任务 C：最值得修的 top3

排序依据：**是否有真实功能损失** × **影响面** × **成本**。`rseq` 虽然频率最高，但**不进入推荐**。

| # | 项 | 影响工具数 | 修复成本 | 需要新抽象？ | 理由 |
|---|---|---|---|---|---|
| 1 | `IP_MTU_DISCOVER`（SOL_IP, opt 10） | 1/12（dig；所有设 PMTU 策略的网络程序都会碰） | **低**：沿用 `IpOptionSet` 加一个 int 字段并接受/返回，行为可先 store-only，后续再做 DF 位 | 否 | 唯一「Linux 支持 + 层级已实现 + 只需加一个 option」的正式缺口；失败时 dig 不等价于 Linux |
| 2 | `NETLINK_EXT_ACK`（SOL_NETLINK, opt 11） | 1/12（ss；iproute2 系都会设） | **低**：`CNetlinkOptionName` 加一个布尔项（store-only），与现有 netlink option 框架一致 | 否 | 缺它只影响 netlink 错误文本，但它是 ss 的标准探测路径；加一个枚举 + set-only 选项即可 |
| 3 | `membarrier(MEMBARRIER_CMD_QUERY)` | 1/12 直接（dig），但属 **glibc 全局** | **低**（只做 QUERY 返回 0/支持集）；真正的 membarrier 是 **高** | 否（仅 QUERY 层） | 让 glibc 的探测路径不再报错；**收益仅为消除探针噪声，无功能收益**，不承诺实现 barrier 语义，需在文档里写清「只支持 QUERY」 |

**明确不建议现在做**：

- **`rseq`（12/12 工具）**：完整实现需要每线程 rseq 区、抢占/信号下的重启语义
  （≈新抽象），成本**高**；而当前 ENOSYS 桩让 glibc 正确回退，用户无感。
  保持桩是最优解，不要因为「触发次数第一」就去实现。
- **`io_uring_setup` / `landlock_create_ruleset`**：都是大型功能（io_uring 是一整套异步 I/O；
  landlock 是一套 LSM），只有探测路径触发且回退完好，收益接近 0。

## 5. 边界与观察

- **计数是 strace 下的形态**，不代表无 trace 时的频率；本轮不做任何时间结论。
- `lsof -i` 退出码 1 且输出为空：lsof 在「没有匹配的网络文件」时就是 1；本轮未深究，
  它在 syscall 层没有异常项（只有正常的 procfs xattr 探测）。
- 第一轮 trace 已全部删除，只保留错误行聚合（`aggregate.tsv`、`agg-<tool>.txt`）
  与第二轮的唯一错误签名（`signatures-non-enoent.txt`）。
- 未改内核代码、未跑 7 域矩阵、未 push。

## 6. 证据

- `evidence/2026-10-27-strace-failures/aggregate.tsv`：`errno|syscall|工具数|调用数|工具列表`
- `evidence/2026-10-27-strace-failures/agg-<tool>.txt`：每个工具的 `count errno syscall`
- `evidence/2026-10-27-strace-failures/signatures-non-enoent.txt`：第二轮的唯一非 ENOENT 错误签名
- `evidence/2026-10-27-strace-failures/linux-baselines.txt`：本机 Linux 对照（EINVAL/ENOTSUP/ENOTTY）
- `evidence/2026-10-27-strace-failures/checklist-pass1.sh.txt`、`checklist-pass2.sh.txt`：可复跑脚本
- 主机工作区：`target/sm-strace/console.txt`、`target/sm-strace2/console.txt`
