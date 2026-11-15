# 开机优化：从 42 秒到 25.6 秒，桌面从 38 秒到 16.4 秒（2026-11-14）

## 1. 结果

| 指标 | 优化前 | 优化后 |
| --- | --- | --- |
| 内核启动 | 1.95 s | 2.2 s |
| userspace 到 systemd "finished" | 40.8 s | 23.4 s |
| **合计** | **42.7 s** | **25.6 s** |
| USB 键鼠枚举完成 | **29 s** | **2.46 s** |
| **桌面就绪（DISPLAY_READY）** | **38.2 s** | **16.4 s** |

## 2. 三个根因，按影响排序

### 2.1 USB 设备发现白等 30 秒（-27 s）

`ostd/src/bus/usb.rs` 的 `PollingUsbHidHost::open()` 里有一个发现循环：每个 xHCI
控制器都要探测到**键盘和鼠标都齐**（`discovery_decision`）才收工，否则一直重试到
`KEYBOARD_DISCOVERY_TIMEOUT`（30 秒）。

插桩后的实测（两个 worker 各打一行）：

```
round=909  devices=0 elapsed_ms=29910 keyboard=false mouse=true    ← host 0 只有鼠标
round=1029 devices=0 elapsed_ms=29909 keyboard=true  mouse=false   ← host 1 只有键盘
```

**这块板子的键盘和鼠标分属两个 xHCI 控制器**，所以两边都在等一个永远不会来的
"另一个设备"，各自空转到 30 秒上限。桌面又要等 `/dev/input/event*`，于是整条链被
拖到 38 秒。

修法：找到**第一个**设备后开一个 1 秒的 settle 窗口，窗口内没有第二个设备就认为
"这个控制器只有这一个设备"，立刻收工。QEMU 里键鼠在同一控制器、同一轮探测就都出现，
行为不变。

### 2.2 桌面会话挂在一个永不激活的 target 上（-17 s）

```
WantedBy=basic.target asterinas-browser-web-evidence.service
```

`basic.target` 在这个镜像里从不激活（旧问题），会话能起来纯属某个 evidence 单元在
33 秒时顺手拉了一把。已用 drop-in 改为 `WantedBy=asterinas-debug-console.target`，
并把 `DefaultDependencies` 关掉（它会隐式加 `After=basic.target`）。

> 注意：这一项单独做时**看不到收益**——省下的 17 秒正好被 USB 的 29 秒吃掉了。
> 两项都修完才显形。

### 2.3 三个必然失败的 `modprobe@*`（-0 s，但值得清）

`modprobe@configfs/efi_pstore/fuse` 各占 6 秒：本内核没有内核模块，`/sbin/modprobe`
根本不存在。屏蔽它们与 `var-lib-xkb.mount`（有 xkbcomp stub，不需要这个 tmpfs）
后 blame 里消失，但对总时长影响在噪声内——它们是并行跑的。

## 3. 排查过程中排除的假设

`dbus`（屏蔽后无变化）、`basic.target` 隐式依赖（去掉后无变化）、VT
`VT_ACTIVATE/VT_WAITACTIVE`（0.000 秒）、getty 冲突（无 getty 在跑）、
串口控制台带宽（整段启动只有 67 KB ≈ 6 秒串口时间，不是瓶颈）。

**有效手段**：`systemd-analyze blame/critical-chain`；对比 `systemctl show -p
InactiveExitTimestampMonotonic` 的绝对时刻（blame 只给时长，会误导）；把服务
disable 掉做对照实验（证明 USB 与桌面无关）；在发现循环里打点。

## 4. 落地方式

- 内核：`ostd/src/bus/usb.rs`（settle 窗口 + 决策函数单测）。
- 板级：`tools/riscv/debian/rootfs/asterinas-desktop-m5.service.d-zz-desktop-start.conf`
  （随 rootfs 重建一起进镜像）。
- 板上另做了四个 mask（`modprobe@configfs/efi_pstore/fuse`、`var-lib-xkb.mount`），
  属于部署动作，重建镜像时需要重新执行或写进构建脚本。

## 5. 还能再要的

- `run-lock.mount` 仍占 6.9 s：单独测 tmpfs 挂载只要 12 ms，说明时间花在等待上而
  不是挂载本身，未查清。
- `asterinas-desktop-ready.service` 19.8 s：它在等桌面就绪，是结果不是原因。

## 6. 第二轮：25.6 s → 14.4 s（2026-11-14 深夜）

上一轮把 42.7 s 压到 25.6 s 之后，`run-lock.mount` 那 6.9 s 一直没查清。这一轮查清了
——**它不是挂载慢，是串口控制台慢**。

### 6.1 根因：每一条 systemd 日志都要走 115200 的串口

`systemd-journald.service` 起来之前，`LogTarget=journal-or-kmsg` 会把管理器的每一条
消息写进 `/dev/kmsg`，而内核控制台同步写到 `ttyS0`。实测一条消息被拆成 4–5 次写，
**平均一个事件 290 ms**：

```
[3.376] Listening on systemd-journald.socket
[3.980] Mounting run-lock.mount ...          ← 4.0 s 时"开始挂"
[11.15] (中间只有 systemd 自己的状态行)
[12.06] Mounted run-lock.mount               ← 真正完成
```

`run-lock.mount` 的 mount(2) 只用了 24 ms——那 7.7 s 是排队等串口。

修法：`/etc/systemd/system.conf.d/10-console-budget.conf`

```ini
[Manager]
LogTarget=journal
ShowStatus=no
```

内核自己的 `loglevel=warn` 消息、以及显式写了 `StandardOutput=journal+console` 的单元
（桌面会话为了 QEMU 串口契约保留）照旧上串口；其余全进 journal。
**单这一项：26.45 s → 18.18 s（-8.3 s）。**

### 6.2 时钟同步不再挡在桌面前面（-1~5 s）

`megrez-clock-sync` 原本是桌面单元的 `ExecStartPre`：板子没有 RTC，开机是 1970，
证书全都"尚未生效"，所以必须在起会话前对时。但开机时网络还没起，它按每个源的超时
逐个跳过——实测 16.2 s 开始、21.2 s 才 `CLOCK_SYNC_READY`，桌面等了 5 s。

改成独立单元 `megrez-clock-sync.service`（`Type=oneshot`，`WantedBy=sysinit.target`
但**不带 `Before=`**），与桌面并行。实测开机后 8.6 s 对时完成，桌面不再等它。

### 6.3 桌面不再挂在 sysinit.target 后面（-3 s）

`/data`（115 GB ext2）的挂载要 **2.1 s**，因为 ext2 在 mount 时把每个块组的
块位图和 inode 位图都读一遍：30,040,567 块 / 32768 块每组 = **917 组 × 2 = 1834 次
4 KiB 读**，每次约 1.1 ms。`p3-data.service` 是 `Before=sysinit.target`，于是整个
sysinit（以及排在它后面的桌面）都要等这 2.1 s。

两处改动：
- `p3-data.service` 去掉 `Before=sysinit.target`（只有 dsh-web 需要 /data，它自己
  已经 `Requires=p3-data.service`）；
- 桌面单元的 drop-in 把 `After=` 重置成 `local-fs.target systemd-journald.socket`。
  会话真正需要的是根文件系统、`/run`、设备节点和 journal socket，framebuffer 由
  `desktop-m5-device-access` 自己等。

> 坑：drop-in 里的 `After=` 是**追加**不是替换，要清空必须单独写一行空的 `After=`。
> 板上还残留了一个上一轮的 `zz-bootopt-start.conf`，两个 drop-in 的 `After=` 叠在一起，
> 第一次改完 `systemctl show -p After` 里 `sysinit.target` 还在，数字只降了 0.4 s。

### 6.4 结果

| 指标 | 第一轮 | 现在 |
| --- | ---: | ---: |
| kernel | 1.95 s | 2.0–2.2 s |
| userspace | 24.5 s | 12.3–13.4 s |
| **合计** | **26.45 s** | **14.4–15.6 s** |
| 桌面就绪 | 20.4 s | **10.5 s** |
| 时钟同步完成 | 21.2 s（挡路） | 8.6 s（并行） |

连续两次冷启动分别为 14.40 s 与 15.56 s；无 failed unit，桌面、Firefox、VNC 全部正常
（抓屏验证：百度首页 + lxpanel + 壁纸）。

### 6.5 还剩下的

1. ~~**ext2 挂载仍是 2.1 s**~~：已修，见 `2026-11-15-ext2-lazy-bitmaps.md`。
   位图改成按组惰性加载后，`mount /data` 从 **2120 ms 降到 21 ms**，校验推迟到该组
   首次被使用时进行，ktest 432/0。
2. `asterinas-desktop-m5.service` 自身仍花 4.1 s 起 ExecStartPre（`device-access`
   冷启动约 1 s，其余 2 s 还没定位，怀疑与 VT/TTY 初始化有关）。
3. Xorg 起来到 `x-socket-ready` 约 2.6 s。
