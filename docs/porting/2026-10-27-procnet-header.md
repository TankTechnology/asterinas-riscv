# `/proc/net/tcp6|udp6` 表头对齐 Linux：`lsof -i` 不再报 unsupported format

日期：2026-10-27
基线内核：`e74d914e8`（改前）→ 本提交（改后）
改动文件：`kernel/core/src/fs/fs_impls/procfs/pid/task/net.rs`（`InetFileOps::read_at`）

## 1. 问题

2026-10-27 的 30 应用矩阵里，`lsof -p 1` 虽然可用，但每次运行都打印：

```
lsof: WARNING: unsupported format: /proc/net/tcp6
lsof: WARNING: unsupported format: /proc/net/udp6
```

（v4 的 `tcp`/`udp` 不报警。）

## 2. 根因

Linux 的 IPv4 与 IPv6 表头**不是同一个字符串**：

| 表 | Linux 表头 |
|---|---|
| `tcp`/`udp` | `  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode` |
| `tcp6`/`udp6` | `  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode` |

IPv6 的地址列宽 39，且远端列**拼写为 `remote_address`**。我们此前四个表共用
IPv4 的短表头（`net.rs:320` 旧代码），lsof 校验表头时把 v6 表判为 unsupported。
（`strings lsof` 里 `local_address`、`rem_address`、`remote_address` 三个字符串并存，
正对应 lsof 对两种格式的识别。）

## 3. 改动

`InetFileOps::read_at` 先取 `is_ipv6 = self.view.is_ipv6()`，再按视图选择表头：

```rust
let header = if is_ipv6 {
    "  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode"
} else {
    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode"
};
writeln!(printer, "{header}")?;
```

行格式不动（v6 地址本来就是 32 位十六进制，行内列宽与 Linux 一致）；
v4 表头逐字未变。

## 4. 验证

### 4.1 改前 / 改后（guest 内原始行）

改前（`evidence/2026-10-27-app-compat/matrix-raw.txt`）：

```
lsof: WARNING: unsupported format: /proc/net/tcp6
lsof: WARNING: unsupported format: /proc/net/udp6
```

改后（`evidence/2026-10-27-procnet-header/after.txt`）：

```
LH tcp6_header:   sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode$
LH tcp_header:   sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode$
LH udp6_header:   sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode$
LH lsof_i rc=1 lines=1 warns=0
```

`warns=0`（改前为 2）。`lsof -i` 在本 guest 里没有匹配的 inet 套接字，故只输出表头、
返回 1——这与 Linux 上"没有匹配文件"的行为一致，不是错误。

进一步用真实套接字验证**行解析**（`evidence/2026-10-27-procnet-header/after-rows.txt`）：
在 guest 内持有 `bind(0.0.0.0:9998)` 的 UDP socket 后

```
LH3 udp:   sl  local_address rem_address   st ...
           0: 00000000:270E 00000000:0000 07 ...
LH3 lsof_i rc=0 warns=0 rows=1
LH3 lsof_i: python3  75 root 3u  IPv4    191      0t0  UDP *:9998
```

`lsof -i` 正常解析并列出该行（`warns=0`）。
IPv6 侧本轮只验证到"表头被接受"：holder 的 `bind(("::",9999))` 没有在
`/proc/net/udp6` 里出现行（`udp6_rows=0`），原因未查（可能是我们的 v6 通配 bind
路径，见 `2026-10-27-wildcard-bind-fix.md`）。v6 行格式与 Linux 逐列一致，
但"能列出 v6 行"这一点**尚未实测**，如实标注。

### 4.2 7 域矩阵

同一内核、`daily_use_checklist_regress.sh`、基座 `target/systemd-health/daily.ext2`：

```
DESKTOP ok   APP ok   APT ok   CLIPBOARD ok   AUDIO ok   USB ok   SUSPEND ok
DAILY_DONE=ok   panic=0   allocation failed=0
```

（`INPUT_METHOD=fail` 是不在这 7 域内的既有缺口，与本次改动无关。）
`INPUT_METHOD` 之外无任何回归。

## 5. 状态

- 本轮**只改这一处**，独立 commit；与通配 bind 的修复无关
  （后者因需要新抽象停在设计阶段，见 `2026-10-27-wildcard-bind-fix.md`）。
- 未 push。

## 6. 证据

- `evidence/2026-10-27-procnet-header/before.txt`
- `evidence/2026-10-27-procnet-header/after.txt`
- 矩阵与 guest 串口：`target/matrix-procnet/`、`target/lsof-check/`
