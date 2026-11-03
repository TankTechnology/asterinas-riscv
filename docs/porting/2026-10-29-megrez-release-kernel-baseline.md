# Megrez：release 内核与桌面性能基线

日期：2026-10-29
基线：`727878c26`（本地 `main`）
硬件：Megrez（EIC7700，4×RV64 @1.6 GHz，16 GiB）+ 校园网有线口

## 1. 结论：卡顿的主因是 dev 构建，不是驱动

同一个 `kernel/core` 源码树、同一套 initramfs 和 rootfs，只把 profile 从 `dev`
换成 `release`（`cargo osdk build --release --target-arch riscv64 --scheme riscv
--features=riscv_sv39_mode`），桌面全链路快约 15–22 倍：

| 指标 | dev（17.2 MB Image） | release（6.6 MB Image） | 倍数 |
| --- | ---: | ---: | ---: |
| `getpid` | 119.02 µs | 5.88 µs | 20.2× |
| `getuid` | — | 5.34 µs | — |
| `clock_gettime` | — | 1.03 µs | — |
| `write(/dev/null)` | 184 µs | 11.81 µs | 15.6× |
| pipe 往返（同进程） | 405.7 µs | 27.3 µs | 14.9× |
| socketpair（同进程） | 1024.9 µs | 66.6 µs | 15.4× |
| socket 往返（跨进程） | 2527.9 µs | 152.7 µs | 16.6× |
| X `Sync time`（x11perf） | 7.94 ms | 0.363 ms | 21.9× |
| `scroll500` 重绘速率 | 1740/s | 2170/s | 1.25× |
| 桌面服务就绪（从 booti） | ~68 s | ~35 s | 1.9× |

空跑对照（`[len(()) for _ in range(n)]`，无系统调用）在 release 上是 0.46 µs，
所以上面的数字确实是系统调用/调度成本，不是 Python 循环本身。

要点：**图形吞吐从来不是瓶颈**（fb 写 ~3.6 GB/s、滚动重绘只快 1.25 倍），
瓶颈在每次系统调用 100 µs 量级的固定开销。dev 构建下 `bundle.toml` 的
`[config.build] profile = "dev"`，内核里成片的 `debug_assert!`、无内联、
未优化遍历正好落在这条最热的路径上。

## 2. 部署链路（本次打通）

### 2.1 板上引导事实

- p1 上**没有** `/extlinux/asterinas.conf`；`bootcmd_asterinas` 必然失败，
  实际使用 `bootcmd_rockos`（`/extlinux/extlinux.conf`，`prompt 2`、`timeout 50`）。
  其 `default` 是 `asterinas-stable-debian`。
- U-Boot 2024.01-gdbb5f9e3 **没有** `reboot`（是 `reset`）、**没有** `wget`。
  TFTP 可用但校园网未认证时单播被丢弃，所以 U-Boot 阶段传不了大文件。
- `ext4load mmc 1:2 0x80200000 /root/<img>` 必须写**绝对路径**。写成裸文件名
  只回 `Failed to load '<name>'`（不是 `file not found`），看起来像 p2 文件系统
  损坏，实际只是路径不对——这次在这个假象上浪费了一轮排查。
- 内核文件放在 p2 的 `/root/`，initramfs 与 DTB 在 p1 根目录
  （`stage1-daily.cpio`、`megrez-daily.dtb`）。

### 2.2 网络：先过门户，再走 HTTP

板子的有线口未认证时只有**广播**能过：U-Boot `dhcp` 能拿到
`58.206.232.122/24`，但 ARP/ICMP/TFTP 到网关和主机的单播全部无响应。
浏览器侧表现是 TCP 到任意地址返回门户拦截页
（`Authentication is required. → https://auth.zgclab.edu.cn/index_1.html`）。

已认证状态下一切正常，`curl` 6.6 MiB 内核到 `/root` 只需数秒，
sha256 与主机产物一致（`03536979…a65d`）。反向也通：板子起
`python3 -m http.server`，主机能把 6.2 MiB 壁纸拉回来。

认证方式仍是深澜 srun（见 `megrez-portal-srun-login` 的算法），脚本在板上以
`python3 /tmp/srun.py <user> <pass>` 运行，凭据只在运行时作为参数传入，
不落盘、不进仓库。

> 注意：`/extlinux/extlinux.conf` 里 `asterinas-daily` /
> `asterinas-stable-debian` 两个条目的 `append` 带着**旧实验室网络**的
> `asterinas.net=eic7700-rj45,10.100.19.200/21,10.100.16.1` 和一组
> `ASTERINAS_DESKTOP_PROXY_*`。在本机这套网络下按默认条目启动，eth0 会拿着
> 10.100.x 的静态地址，看起来「网线插着却完全不通」。内核**不支持运行时
> 改地址**（`ip addr add` → EOPNOTSUPP），地址只能在 bootargs 里给。

### 2.3 串口控制台的坑

- 115200 无流控。键入必须逐字符（20 ms/字节），文件推送 0.15 ms/字节
  （≈11 KiB/s）；0.09 ms/字节会把内容打坏。
- 打完整行后的**回车偶尔被吞**：命令回显了却不执行。解决办法是随后再发一行
  空行（桥接器把它变成额外的 CR）。两个 helper 都这么做。
- 长命令（带管道、引号）更容易掉字符，所以基准测试改成「推一个脚本再跑」，
  而不是在命令行里塞 `python3 -c "..."`。

## 3. 桌面精简（同步进仓库）

- lxpanel `transparent=0`：fbdev 没有合成器，半透明面板每次重绘都要整条读改写。
- pcmanfm `show_mounts=0`：没有热插拔设备时挂载探测是纯开销。
- Xorg 以 `-extension GLX` 起，无合成器。
- xterm 用点阵字体（`-fn fixed`），避免每帧放大字形。

## 4. 复现

```sh
# 主机：常驻桥接
python3 tools/riscv/board/megrez_console.py bridge /dev/ttyUSB0 \
    --log /tmp/board.log --fifo /tmp/board.cmd &

# U-Boot 里加载并启动指定内核（p2 路径）
python3 tools/riscv/board/megrez_console.py boot --kernel /root/kernel-rel.img \
    --append 'console=ttyS0 loglevel=warn init=/init asterinas.mmc_write_partition2 \
asterinas.net=eic7700-rj45,58.206.232.122/24,58.206.232.1 -- --root-init=systemd \
--debug-console=isolated-root'

# 桌面就绪后测一遍
python3 tools/riscv/megrez_desktop_baseline.py --label release --compare
```

基准脚本自己计时（`tools/riscv/diagnostics/syscall_ipc_bench_guest.py`、
`x11_bench_guest.sh`），串口只负责把结果带回来，所以一条慢串口不会污染数字。

## 5. 性能优化第一轮（2026-10-29 晚）

### 5.1 现状：一次系统调用约 3.6–4.5 µs

板上的 C 微基准（`tools/riscv/diagnostics/syscall_bench.c`，静态交叉编译后推到板上）：

| 调用 | 时间 | 周期（rdcycle） |
| --- | ---: | ---: |
| 空循环 | 0.7 ns | 1 |
| `getpid`（含 `syscall()` 直调） | 3.6–4.5 µs | 5400–7200 |
| 未实现系统调用（ENOSYS） | 4.4 µs | — |
| `clock_gettime`（**vDSO，不进内核**） | 0.39 µs | 550–730 |
| pipe 往返 | 10.4–11.1 µs | — |
| socketpair 往返 | 32–37 µs | — |

关键点：**空调用（ENOSYS）和 getpid 一样贵**，说明成本全在通用路径上，与具体
处理器无关；vDSO 的 0.39 µs 说明「一次完整 trap」之外还有约 3 µs 花在通用的
系统调用前后处理里。板上的 run-to-run 抖动很大（±25%），单次对比不可信，
所以改成 `rdcycle` 周期计数（与频率无关）+ 多次取最小值。

### 5.2 第一轮改动：待处理信号快路径

`handle_pending_signal()` 每个系统调用都会跑一次，而它内部
`dequeue_pending_signal()` 要依次拿 `sig_dispositions`（两把）、
`signal_job_control`、`selected_stop` 和两个队列锁，绝大多数时候队列是空的。

两个队列本来就有原子计数（`SigQueues::count` + `is_empty()`），所以加了快路径：

```rust
ctx.process.notify_group_stop();
if ctx.posix_thread.sig_queues().is_empty() && ctx.process.sig_queues().is_empty() {
    return;
}
```

竞态语义不变：检查之后才入队的信号，和「晚一个系统调用再入队」是同一个结果。

QEMU 里已验证新内核能正常引导到 `graphical.target`、桌面证据齐全（
`DEBIAN_DESKTOP_M4_READY`），说明信号投递没被破坏；**板上的 A/B 还没跑完**
（板子网络在测完之前掉线，内核推不上去），下一轮补上：
`syscall_bench 100000` × 5 取最小周期，对比 0353 与 1860 两个内核。

### 5.3 试过但放弃：`-C target-feature=+zba,+zbb`

EIC7700 报 `rv64imafdc_zba_zbb`，试过在 `[scheme."riscv"]` 加
`build.rustflags = "-C target-feature=+zba,+zbb"` 重新构建：QEMU 用
`-cpu rv64,…,zba=true,zbb=true` 能正常引导，但板上周期数落在噪声里
（5367 vs 6932 vs 7219 的不同次测量），**没有可复现收益**，已从 `OSDK.toml` 撤掉。

### 5.4 QEMU 上的 A/B 口径（可复现）

板子控制台被串口二进制流挂死之后（见
`2026-10-29-megrez-desktop-network-bringup.md` 第 5 节），把 A/B 搬到 QEMU：

- 镜像里放 `/root/syscall_bench_ns`（`-DNO_CYCLE` 编译，QEMU 里 `rdcycle` 不可用）+
  一个 `bench.service`（`After=basic.target`、`WantedBy=multi-user.target`），
  开机跑 20 轮（每轮 1 万次）写 `/root/bench.out`，最后一轮后 `sync`；
- 用同一套 U-Boot 链路分别引导两个内核（基线 `0353…`、信号快路径 `1860…`），
  **每个内核引导两次**，取两次之间的较小值，两次之间的散布就是这套口径的噪声底。

踩过的四个坑（都不是内核问题）：
1. **`/home/asterinas` 被 stage1 挂成 tmpfs**，写进去的测量结果在 QEMU 退出后消失；
   要写 root 上的持久路径。
2. `WantedBy=graphical.target` + `After=graphical.target` 形成 **ordering cycle**，
   systemd 直接 skip 该 unit，串口里只有一行 `Ordering cycle found, skipping`。
3. 硬杀 QEMU 前必须 `sync`，否则 ext2 的页缓存没回写，文件根本不会出现在镜像里。
   现在 `bench.sh` 最后写 `/root/bench.done` 再 `sync` 一次，宿主机只认这个哨兵，
   看到它就说明 `bench.out` 已经真正落盘。
4. `debugfs` 不加 `-w` 时是只读的，`rm` 会失败而且**错误信息走 stderr**，于是
   「清掉上一轮结果」看似成功、其实没删，宿主机会读到上一次的 `bench.done` 立刻
   判完成并杀掉 QEMU —— 两次「结果逐字节相同」就是这么来的。清理时必须 `-w`
   且把 stderr 一起判。

结果（每个数取两次引导、每轮 20 次采样里的最小值，ns）：

| 指标 | 基线 `0353…` | 信号快路径 `1860…` | 变化 |
| --- | ---: | ---: | ---: |
| `getpid`（libc） | 6022.7 | 4072.5 | −32.4% |
| `syscall(SYS_getpid)` | 5578.9 | 4088.9 | −26.7% |
| `getppid` | 5872.0 | 4410.1 | −24.9% |
| `close(-1)` | 5887.6 | 4548.0 | −22.8% |
| 未实现调用（ENOSYS） | 5568.1 | 4357.9 | −21.7% |
| `getuid` | 5838.6 | 4699.3 | −19.5% |
| pipe 往返 | 14391.0 | 12515.0 | −13.0% |
| socketpair 往返 | 103442.6 | 99545.7 | −3.8% |
| **对照**：vDSO `clock_gettime`（不进内核） | 160.8 | 159.3 | −0.9% |

同一个内核两次引导之间的散布是 1–11%，所以 19–32% 是真实的差异；对照组
（vDSO，完全走用户态）几乎不动，说明变化确实在系统调用路径上。

### 5.5 板上 `rdcycle` 复测（2026-10-29 晚，真机）

条件：同一个 rootfs、同一个桌面镜像，用保存的 U-Boot `bootcmd` 引导（参数完全一致），
把候选内核 `cp` 到 `/root/kernel-rel.img` 再 `sync; reboot`，每轮开机后等桌面稳定
100 秒再跑 `/root/syscall_bench 20000`×3，取最小值。`bench-rel` 与
`bench-base-restored` 是两次独立的基线测量，用来估计口径噪声。

| 内核 | `getpid` 周期 | `getpid` ns | pipe 往返 ns | socketpair 往返 ns | vDSO `clock` ns |
| --- | ---: | ---: | ---: | ---: | ---: |
| 基线 `0353…` | 5309 | 3553 | 10059 | 32942 | 382 |
| 基线（另一次开机） | 6004 | 4279 | 10868 | 33164 | 375 |
| 第一轮 信号快路径 `1860…` | **3071** | **2183** | **7475** | **26249** | 375 |
| 第二轮 原子读快路径 `2522…` | **3031** | **2172** | 8136 | 26788 | 393 |

相对基线的变化（取各内核最小值）：

| 内核 | `getpid` | pipe | socketpair | vDSO 对照 |
| --- | ---: | ---: | ---: | ---: |
| 第一轮 `1860…` | **−42.2%** | −25.7% | −20.3% | −1.8% |
| 第二轮 `2522…` | **−42.9%** | −19.1% | −18.7% | +2.8% |

结论：

- **第一轮（待处理信号快路径）在真机上真的值钱：每次系统调用少约 2200 个周期（−42%）**，
  pipe/socketpair 往返也降了 20–26%。vDSO 对照几乎不动，说明变化确实在系统调用路径上。
- **第二轮（`WaitQueue::is_empty` / `SyncSubject::notify_observers` 的退化 RMW 改
  acquire load）在真机上落在噪声里**：`getpid` 与第一轮持平，pipe/socketpair 略差
  但差值小于两次基线测量之间的差别（5309 vs 6004）。它在 QEMU 上的收益主要来自
  TCG 对原子指令的放大，**不能当作板上收益**。保留它的理由是那条快路径本来就不该
  做读-改-写，不是为了性能数字。
- 两次基线之间差 13%，所以上表的百分比要按 ±13% 的噪声来读；−42% 远在这个范围之外。

QEMU 这个百分比**不能当成板上的预期收益**：TCG 把原子指令和锁放大得比真机厉害得多
（一次 `amoadd` 在 TCG 里要经宿主 helper），所以删掉每系统调用若干次加锁在 QEMU 里
显得特别值钱。板上实测见 §5.5：第一轮确实是 −42%，但第二轮在 QEMU 里那 30% 在板上
完全落在噪声里——**这正好说明 QEMU 只适合判断方向，绝对值必须回板上测**。

