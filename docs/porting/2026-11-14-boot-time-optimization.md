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
