# Debian 常用 CLI 兼容矩阵：30 个工具「装 + 版本 + 功能」实测

日期：2026-10-27
内核：`86cf3f5d0`（ELF sha256 前 32 位 `8c71177bd24c060db7eb4fc57443e241`）
镜像：`target/systemd-health/daily.ext2`（Debian 13 trixie，与 L4/L5/L6 同一基座）
方法：guest 内 root 串口执行，逐包 `apt-get install -y`（每个都包在 `timeout` 里），
再跑 `--version` 与一条真实功能命令；全部原始输出落在串口与
`evidence/2026-10-27-app-compat/matrix-raw.txt`。**内核代码零改动。**

## 0. 结论

| 指标 | 数值 |
|---|---|
| 应用数 | 30（8 个类别） |
| install ok | **30/30** |
| version ok | **30/30** |
| 功能命令 ok | **29/30**（唯一失败 `dnsutils`；`htop` 为测试口径问题，复测通过） |
| 三项全 ok | **29/30**（判据 ≥24/30 ✅） |
| 输出中出现 ENOSYS/EPERM/EINVAL | **0 次**（30 个应用的 version/功能输出） |

一句话：**装得上、跑得动**。唯一真实失败是 `dig`，根因已定位到内核的
**通配 `INADDR_ANY` bind 被固定在 loopback 接口**（`ip/common.rs:24-33,50-56`），
属语义缺口而非缺 syscall；另发现两处判据外缺口（`/proc/net/tcp6|udp6` 表头格式、
`PTRACE_ATTACH` 未实现）。

## 1. 矩阵表

| # | 类别 | 应用 | install | version | 功能命令 | 错误 | 备注 |
|---|---|---|---|---|---|---|---|
| 1 | 文件 | ripgrep | ok | `ripgrep 14.1.1` | `rg -n needle` → `1:needle` | 无 | |
| 2 | 文件 | fd-find | ok | `fdfind 10.2.0` | 找到 `needle.txt` | 无 | 二进制名 `fdfind` |
| 3 | 文件 | tree | ok | `tree v2.2.1` | `tree -L 1` 输出树 | 无 | |
| 4 | 文件 | rsync | ok | `3.4.1` | 目录同步后 `cat` 得 `hi` | 无 | |
| 5 | 文件 | unzip | ok | `UnZip 6.00` | 解压自建 zip → `hi` | 无 | |
| 6 | 文件 | xz-utils | ok | `5.8.1` | 压缩+解压 → `hello` | 无 | |
| 7 | 文本 | jq | ok | `jq-1.7` | `jq .a` → `1` | 无 | |
| 8 | 文本 | gawk | ok | `GNU Awk 5.2.1` | 求和 → `6` | 无 | |
| 9 | 文本 | vim | ok | `9.1` | `-es` 写文件 → `vimok` | 无 | |
| 10 | 文本 | less | ok | `less 668` | 非 tty 下读文件 → `l1 l2` | 无 | |
| 11 | 文本 | sed | ok | `4.9` | `s/a/b/` → `b` | 无 | |
| 12 | VCS | git | ok | `2.47.3` | init/add/commit/log → `17bf510 m` | 无 | |
| 13 | VCS | git-lfs | ok | `3.6.1` | `git lfs track` 写 `.gitattributes` | 无 | |
| 14 | 网络 | curl | ok | `8.14.1` | HTTP GET deb.debian.org → `200` | 无 | |
| 15 | 网络 | wget | ok | `1.25.0` | 下载 Release 头 40 字节 | 无 | |
| 16 | 网络 | dnsutils | ok | `DiG 9.20.26` | `dig +short deb.debian.org` | **超时** | **唯一真实失败**，见 §2.2 |
| 17 | 网络 | netcat-openbsd | ok | `1.229-1` | `nc -z deb.debian.org 80` → `open` | 无 | |
| 18 | 网络 | openssh-client | ok | `OpenSSH 10.0p2` | `ssh-keygen` 生成密钥 | 无 | |
| 19 | 进程 | procps | ok | `procps-ng 4.0.4` | `ps -e` → 21 行 | 无 | |
| 20 | 进程 | psmisc | ok | `23.7` | `pstree -p 1` | 无 | |
| 21 | 进程 | htop | ok | `htop 3.4.1` | 矩阵内无渲染 | — | **测试口径问题**，见 §2.1 |
| 22 | 进程 | lsof | ok | `lsof 4.99` | `lsof -p 1` 列出 fd | 无（warning 见 §2.3） | |
| 23 | 进程 | strace | ok | `6.13` | `strace -f -e openat /bin/true` | 无 | ptrace 基本可用 |
| 24 | 运行时 | python3 | ok | `3.13.5` | `sum(range(10))` → `45` | 无 | |
| 25 | 运行时 | nodejs | ok | `v20.19.2` | `console.log(1+1)` → `2` | 无 | |
| 26 | 运行时 | lua5.4 | ok | `5.4.7` | `print(1+1)` → `2` | 无 | |
| 27 | 构建 | make | ok | `4.4.1` | 构建一个 target | 无 | |
| 28 | 构建 | gcc | ok | `14.2.0` | 编译并运行 hello → `gccok` | 无 | |
| 29 | 构建 | pkg-config | ok | `1.8.1` | 打印 `pc_path` | 无 | |
| 30 | 杂项 | file | ok | `file-5.46` | `file /bin/ls` 识别 ELF | 无 | |

## 2. 失败与异常归因

### 2.1 `htop`：pty 尺寸为 0 的测试口径问题（非内核）

矩阵里的功能命令是 `script -qec "timeout 3 htop"`；`script` 从串口接管时
pty 窗口是 **0×0**，htop 渲染不出任何行，`timeout` 再以 124 结束，于是被判 fail。

复测（同一镜像，补 `stty rows 40 cols 120`）：

```
AT2 htop_rc=124 bytes=1447
AT2 htop: ^[[?1049h...  ^[[36mCPU^[[39m[^[[30 ...
```

htop 正常进入备用屏并画出 `CPU` 行（rc=124 只是被 timeout 杀掉，符合预期）。
**判定：测试口径，不是应用/内核失败；计入通过。**

### 2.2 `dnsutils` / `dig`：`INADDR_ANY` bind 被固定在 loopback（语义不符）

**现象**：`dig +short deb.debian.org`（无论 `+time=3`、`+tries=3`、`+time=10`）都报
`communications error to 10.0.2.3#53: timed out`；而同一条命令所在会话里
`getent hosts deb.debian.org`、`curl`、`wget`、`nc -z` 全部正常。

**取证链**：

1. dig 的套接字确实建好了：dig 等待期间 `/proc/net/udp` 有一行
   `0: 00000000:8005 0302000A:0035 01 …` —— 本地 `0.0.0.0:32773`、对端 `10.0.2.3:53`、
   状态 `01`（已连接），**rx_queue 为 0**（应答没有进到这个 socket）。
2. Python 对照（probe 3/4/5），**`local` 地址就是分水岭**：

   | 用例 | 本地端点 | 结果 |
   |---|---|---|
   | 隐式 bind（`connect()` 触发，按路由选 eth0） | `('10.0.2.15', 32769)` | **reply=12，0.00 s** |
   | 显式 `bind(('0.0.0.0', 0))` + connect | `('0.0.0.0', 32769)` | 3 s 超时 |
   | 显式 `bind(('0.0.0.0', 0))` + sendto | `('0.0.0.0', 32768/32770)` | 3 s 超时 |
   | 显式 `bind(('0.0.0.0', 40555))` | `('0.0.0.0', 40555)` | 3 s 超时（与端口无关） |
   | epoll 水平/边沿、`poll(2)` + 显式 bind | `0.0.0.0` | 4 s 无事件（排除等待原语） |

   （probe 5 里唯一例外是**开机后第一个 UDP 包**——隐式 sendto——也超时；probe 1/2 各出现过一次
   同样的首包超时、随后成功。首发包/ARP 抖动，单独记录，不作为本次归因依据。）
3. 根因在内核源码：
   - `kernel/core/src/net/socket/ip/common.rs:24-33`：目的地址为 unspecified 时走
     `default_bind_iface()`；
   - `common.rs:50-56`：`default_bind_iface()` **固定选 loopback 接口**；
   - `common.rs:36-49` 的注释与 FIXME 已经把这一限制写死：
     "A wildcard bind is therefore *not* reachable through the configured network
     interface; a server that must be reachable from the network has to bind that
     interface's address explicitly."
   - 对比 `common.rs:71-97` 的 `get_ephemeral_endpoint()`：隐式绑定按**路由**选接口
     （所以 `connect()/sendto()` 路径正常）。

**因果**：dig 显式 `bind(INADDR_ANY)` → socket 注册在 lo；发包时路由按目的地址走 eth0
（所以包发出去了），但 **eth0 上回来的应答不会投递给挂在 lo 的 socket** → 超时。

**归因**：**语义不符**（Linux 的 `INADDR_ANY` 跨接口语义缺失），不是 ENOSYS/EPERM/EINVAL，
工具输出里也没有任何 errno 字符串。

**影响面**：所有"显式 bind 通配地址、再从 eth0 收发"的 UDP 程序——`dig`/`host`/`nslookup`、
`nc -u -l`、DHCP 客户端、以及大量守护进程的 UDP 监听。

**修复建议（本轮不执行）**：实现 `common.rs:47-49` 的 FIXME——把通配绑定注册到所有接口
（至少默认路由接口）；这需要 `aster-bigtcp` 支持一个 socket 注册到多张 socket 表。
在这个限制修掉之前，客户端可以改用"不显式 bind"的写法（glibc resolver 就是如此）。

### 2.3 `lsof` 的 `/proc/net/tcp6|udp6` warning：procfs 格式偏差

`lsof -p 1` 功能通过，但每次运行都打印：

```
lsof: WARNING: unsupported format: /proc/net/tcp6
lsof: WARNING: unsupported format: /proc/net/udp6
```

实测两文件都存在，首行为：

```
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
```

Linux 同文件首行是 `  sl  local_address remote_address ...`（**`remote_address` 全拼**），
且列宽不同。lsof 按列位置校验，遇到我们的表头即判"unsupported format"。
来源：`kernel/core/src/fs/fs_impls/procfs/pid/task/net.rs:320`（表头字符串）
与 `:330-345`（行格式）。**影响 `lsof -i` 的套接字列表**，不影响 `lsof -p`。
归因：**proc/sysfs 格式不符**；修复建议：表头改为 `remote_address` 并对齐列宽，
属独立小改动，不在本轮执行。

### 2.4 附加发现：`strace -p`（`PTRACE_ATTACH`）不可用

矩阵里 `strace -f -e openat /bin/true` 通过（**启动时** trace 可用）；但归因探针里
`strace -p <pid>` 产出 **0 行**。源码依据：
`kernel/core/src/syscall/ptrace.rs:229` 只有一行注释 `// PTRACE_ATTACH = 16,`，
请求枚举里没有 16，因此 attach 不被支持；只有 `PTRACE_TRACEME`（`:36`）这条路径。
影响：`strace -p`、`gdb attach`、`perf` 之类"附加到运行中进程"的调试工作流。
归因：**缺 syscall 子功能**（ptrace request 未实现）。不在这 30 项的判据里（strace 本身算通过）。

## 3. 内核日志观察：未实现 syscall 全部被工具回退

矩阵运行期间控制台出现的 `Unimplemented syscall number`：

| 次数 | 编号 | syscall | 是否造成失败 |
|---|---|---|---|
| 1560 | 334 | `rseq` | 否（glibc 探测，自动回退） |
| 137 | 303 | `name_to_handle_at` | 否 |
| 108 | 312 | `kcmp` | 否 |
| 33 | 250 | `keyctl` | 否 |
| 9 | 442 | `mount_setattr` | 否 |
| 5 | 444 / 425 / 321 / 326 | `landlock_create_ruleset` / `io_uring_setup` / `bpf` / `copy_file_range` | 否 |

另有 8 次 `Unsupported signal flags: SA_NOCLDSTOP | SA_RESTORER`（来自 `script` 会话的
shell 派生），同样未影响结果。**30 个应用输出中 ENOSYS/EPERM/EINVAL 出现 0 次。**

（`dmesg` 在 guest 内返回空——`/dev/kmsg` 读取路径未返回内容；本轮的"内核报错"以串口
原始日志为准，见 `matrix-console.txt.gz`。）

## 4. 状态页

- **成功率：29/30 三项全 ok**（install 30/30、version 30/30、功能 29/30；`htop` 复测通过）。
- 30 项判据内的失败归因：**语义不符 1**（`dig`：`INADDR_ANY` 通配 bind 被固定在 loopback）；
  缺 syscall 0；proc/sysfs 0；网络/环境 0；打包问题 0。
- 判据外附加发现：`lsof -i` 的 `/proc/net/tcp6|udp6` 格式偏差（proc/sysfs 类，1 处）、
  `PTRACE_ATTACH` 未实现（缺 syscall 子功能，1 处）、`rseq` 等 7 个未实现 syscall（仅日志，
  全部被工具回退）。
- **最值得修的前 3 项**（按影响的工具数）：
  1. **通配 `INADDR_ANY` bind 的跨接口语义**（`common.rs:24-33,50-56`）——影响 `dig`/`host`/
     `nslookup`、`nc -u -l`、DHCP 客户端，以及任何绑定 0.0.0.0 的 UDP 服务端；这是本轮唯一
     让 30 项矩阵掉分的真实内核缺口。
  2. **`/proc/net/tcp6|udp6` 表头/列宽对齐 Linux**（`net.rs:320`）——修好后 `lsof -i` 可用，
     `ss`/监控脚本也更可能直接工作。
  3. **`PTRACE_ATTACH`**（`ptrace.rs:229`）——让 `strace -p`、`gdb attach` 这类调试工作流可用；
     （`rseq` 噪声 1560 条/轮，优先级更低，可先降噪。）

## 5. 证据

- `evidence/2026-10-27-app-compat/matrix-raw.txt`（94 行：每项的 install/version/功能原始行）
- `evidence/2026-10-27-app-compat/matrix-console.txt.gz`（整轮串口，含内核 warning 统计）
- `evidence/2026-10-27-app-compat/udp-bind-probes.txt`（probe 3/4/5 原始行：dig 重试、
  epoll/poll、bind 变体对照）
- `evidence/2026-10-27-app-compat/wildcard-bind-kernel.txt`（`ip/common.rs` 摘录 72 行）
- 主机工作区：`target/app-matrix/`（矩阵）、`target/app-attr3/`、`target/app-attr4/`、
  `target/app-attr5/`（归因探针）

## 6. 方法与局限

- 每个应用独立 `apt-get install -y`（`timeout 420`），因此 install 结论是逐包的；
  apt 索引先整体 `apt-get update`（rc=0）。
- 功能命令都是确定性的本地操作；网络类只访问 `deb.debian.org`（apt 本身可达）。
- 本轮未跑 7 域矩阵：没有内核改动。
- 矩阵与归因探针都替换了 `daily_use_checklist.sh`，所以 gate 自身以
  `FAIL (no result for: DESKTOP, APP, APT, …)` 退出 1——这是**替换清单的预期结果**，
  不是内核或应用失败；判定以串口里的 `AM`/`AT` 原始行为准。
