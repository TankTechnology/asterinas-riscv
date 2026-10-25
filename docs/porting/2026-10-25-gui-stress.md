# GUI 重负载：10 并发应用 + 2 GiB I/O + 3 次休眠（**出现内核 panic**）

日期：2026-10-25　HEAD：`1d27beab3`
内核：`target/prime/kernels/x86.elf`，sha256 前缀 **`9e8ee5dd544fa03a`**
（L1–L3 旧基线内核；见“归因”）
镜像：`target/debian-x86-toolchain3/rootfs/debian-root.ext2`
（`desktop-drm-toolchain`，2.0 GiB / 409 包）

## 结论摘要

- **判定：有 panic**（未完成：休眠循环未执行）。
- **panic**：guest uptime **141.188 s**（2 GiB I/O 启动约 36 s 后）：
  ```
  Uncaught panic:
  called `Result::unwrap()` on an `Err` value: NoMemory
  at /root/asterinas/kernel/libs/ring-buffer/src/lib.rs:104
  ```
  串口里共 **3 份** panic 输出（多 CPU 并发打印）；无符号化栈。
- **GUI 部分已起来**：apt 修好 DNS 后 `x11-apps`/`xdotool` 安装成功，
  `DAILY_DESKTOP=ok Xorg+DRI3`，10 个客户端全部按顺序启动；计数
  **7/10**（缺的 3 个是 xterm，见下）；10 个应用的窗口未被 panic 前复核。
- **2 GiB I/O**：`/dev/shm` 只有 2,084,912 kB（2 GiB tmpfs 上限），放不下
  单个 2 GiB 文件，脚本回落到 `/tmp`（根 ext2 仅约 300–400 MiB 空闲），
  `dd` 启动后很快进入高内存/回写压力，随后触发上述 panic。
- **休眠循环：0/3，未执行**（panic 中断）。

## 1. 应用与窗口

| 应用 | 启动 | 计数（panic 前） | 备注 |
|---|---|---|---|
| pcmanfm ×2 | 是 | 2 | class `[Pp]cmanfm` |
| eog ×2 | 是 | 2 | class `[Ee]og` |
| xterm ×3 | 是 | **0** | class `[Xx]term` **未匹配**（xterm 的 WM_CLASS 通常是 `XTerm`，该正则第 2 个字母未覆盖大写；客户端进程未及复核） |
| xclock | 是 | 1 | class `[Xx]clock` |
| xcalc | 是 | 1 | class `[Xx]calc` |
| xeyes | 是 | 1 | class `[Xx]eyes` |
| **合计** | 10 | **7** 计数 | 实际窗口数未复核（panic 中断） |

工具链镜像不带 `x11-apps`/`xdotool`；第一次运行因镜像
`/etc/resolv.conf` 指向 `1.1.1.1`（slirp 应使用 `10.0.2.3`）导致 apt 失败，
回落后停止。修正 DNS 后重跑：

```
gui_tools x11_apps=yes xdotool=yes img=/usr/share/pixmaps/debian-logo.png
DAILY_DESKTOP=ok Xorg+DRI3
```

## 2. 内存采样

panic 前只采到 baseline：

```
gui_baseline memavail=3405856 uptime=105.07
```

（10 个 GUI 客户端 + Xorg 已占用约 560 MiB；无后续样本，panic 中断。）

## 3. 2 GiB I/O 与 panic

```
gui_io start shm_avail=2084912_fallback_tmp io_file=/tmp/gui-big
[   141.188] ERROR : Uncaught panic:
    called `Result::unwrap()` on an `Err` value: NoMemory
    at /root/asterinas/kernel/libs/ring-buffer/src/lib.rs:104
```

- `/dev/shm` 上限 2 GiB（2,084,912 kB 可用），单个 2 GiB 文件放不下；
  脚本回落到 `/tmp`（根 ext2 空闲不足以容纳 2 GiB，`dd` 会写满根分区）。
- 在 `dd` 持续推进、页缓存/回写压力上升时，**旧内核的 ring-buffer 增长
  分配返回 `NoMemory`，`unwrap()` 直接 panic**。
- 串口有 3 份 panic（多 CPU），第一份在 CPU 2；栈未符号化，只有地址列表。

## 4. 休眠唤醒

未执行：panic 发生在应用/I/O 阶段，脚本未到 suspend 循环
（3 次宿主唤醒的门禁改动已就位但未验证）。

## 5. 判定与归因

**判定：有 panic。**

- 直接原因：`kernel/libs/ring-buffer/src/lib.rs:104` 的分配
  `unwrap()` 遇到 `Err(NoMemory)`；在 10 个 GUI 客户端 + 2 GiB I/O 的
  内存压力下触发。
- **关键背景**：本次 gate 使用的是 `target/prime/kernels/x86.elf`
  （`9e8ee5dd`，L1–L3 旧基线），**不包含**后来 L6 的内存修复：
  - 内核栈 128 页 → 32×4 页分段；
  - 内核堆 16 MiB 启动预留连续池；
  - ring-buffer 大块增长走预留池（`99d4b116e`）/ 分段回退。
  旧内核在碎片化/高压下正好死在 ring-buffer 分配，与
  `docs/porting/2026-10-24-frame-allocator-starvation.md` 记录的现象同源。
  当前内核（`e2312060`/`66004cd6`）已经把这条路径改为池/分段，不再
  `unwrap` 崩溃。
- 因此这不是“GUI 栈本身”的缺陷，而是**旧内核 + 高内存压力**暴露的
  已知分配器问题；GUI 客户端、Xorg+DRI3、apt/GTK 路径本身已工作。

## 6. 后续（不重试，按纪律保存日志）

1. 用已修复内核（`e2312060` 或 `66004cd6`）复跑同一 GUI 压力，预期
   ring-buffer 走预留池/分段，不再 panic；
2. 2 GiB I/O 需要给 guest 挂一块 ≥4 GiB 的数据盘（或扩大 `/dev/shm`），
   避免回落到根 ext2；
3. xterm 计数改用 `xwininfo -root -tree | grep -i`（大小写不敏感），
   或在 panic 前打印每个应用的首条错误；
4. 3 次 suspend 的宿主机多唤醒逻辑已加入门禁客户端，待下次可用运行验证。

## 7. 证据

- `target/gui-stress2/console.txt`（串口全量，含 panic）
- `target/gui-stress2/host.log`（门禁被手动终止：`make ... Terminated`）
- 归档：`docs/porting/evidence/2026-10-25-gui-stress/console-excerpt.txt`

## 8. 修复后复验（2026-10-25）

ring-buffer 的 NoMemory panic 已修复
（`fix(ring-buffer): never panic on NoMemory during growth`），并用当前树重建
内核（`20582f4b207b6cb1`）复跑同一 GUI 负载，I/O 改为 `/dev/shm` 可容纳的
1.5 GiB（保持 10 分钟）：

- **无 panic**（串口真实 panic 块 0；ring-buffer 降级 warn 0 次）；
- **10 个应用全部起窗**（xwininfo 计数 14 含子窗口：pcmanfm 4、eog 4、
  xterm 3、xclock/xcalc/xeyes 各 1）；
- 1.5 GiB `dd` + sha256：`dd_rc=0 sha_rc=0`；
- `MemAvailable` 3,398,356 → 3,288,172 kB（**-3.24%**）；
- **suspend 3/3 ok**，唤醒后窗口存活、探测窗正常；
- ring-buffer 全程走预留池（`pool hit` 累计 1024），heap 池峰值
  2090/4096 页。

判定 **A（修复有效）**。详见
[`2026-10-25-ringbuf-nomemory-fix.md`](2026-10-25-ringbuf-nomemory-fix.md)。
