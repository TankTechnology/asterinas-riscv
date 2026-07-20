# Megrez Sv48 真机与 bootargs 证据

## Claim scope

本页冻结两条彼此独立的证据线：

- 2026-07-16 的 Megrez 真机运行，源码为
  `6df0f28fdb4b901fe7726ad4bd9d435b42e64102`；
- 2026-07-17 的最终 clean QEMU 候选，源码为
  `593d5bb19d5da8520b0c81c71743575840419a78`，记录为
  `tracked_dirty=false`。

二者不是同一份 Image。真机 Image 的 CRC32 是 `40b88c04`，QEMU 候选
Image 的 CRC32 是 `45701d7b`；契约也明确
`baseline_match_required=false`。因此，真机证据证明了默认 Sv48 的板级
可达边界，但不能证明 `593d5bb19` 的字节已经在 EIC7700 上运行。

本地原始串口和 RockOS 记录位于被忽略的 `target/`/历史工作目录中，不随
fresh clone 分发；本页只提供哈希锚点。不得把“tracked 文档含哈希”表述为
“tracked 仓库包含全部原始证据”。

## 2026-07-16 physical baseline

真机原始串口记录大小为 374132 字节，SHA-256 为
`8f8851bd58fb51e36fcc855f29bf17cd6685a53d9552e1ed1e08dcc1755be476`。
用于提炼 DTB/保留内存事实的 RockOS transcript SHA-256 为
`7a85517c9b3a2b4b0642397409479729776cf84ccc263ed5f0ea529415c02a55`。

该轮可观察到的顺序是：

```text
Starting kernel ...
Enter riscv_boot
INFO: Booting 3 processors
OSTD initialized. Preparing components.
use randomness based on the timestamp, which is insecure
[kernel] rootfs is ready
Failed to run the init process: ... ENOENT
```

日志还证明 bootstrap hart 为 2，hart 0、1、3 均启动。由此可排除“默认
Sv48 在最初 `satp`/high-half 跳转处必然卡死”这一旧假设。时间戳 fallback
证明缺少 Zkr/DTB seed 不再阻塞启动，但日志明确警告它不安全，所以这不是
随机源质量验证。

冻结的真机产物身份如下：

| 产物 | 大小 | SHA-256 | CRC32 |
|---|---:|---|---|
| booti Image | 11318656 | `15d411579945c52071996280827e409aa1c89b85dd9656cb4cb119b815f895ca` | `40b88c04` |
| initramfs | 3411 | `792246eccce7eab3e20401bd163f64dde0a790847998ec9ffd816071bcbeed2a` | `153879f1` |
| Megrez DTB | 154800 | 未取得 | `4afcb20e` |

精确 DTB 文件当前不可用，因此它只能以 size+CRC 身份和提炼后的结构契约
出现，不能声称已经对当前文件重新做过 SHA-256 或结构解析。契约明确标记
`runnable_under_qemu=false`；真实 Megrez DTB 也不得交给 QEMU `virt` 启动。

## Bootargs root cause and corrected contract

真机轮次在 `booti` 前只修改了 DTB `/chosen/bootargs`。U-Boot 随后会用
RAM 环境中的 `bootargs` 再写 DTB；旧环境丢失 PID 1 参数，Asterinas 因而
在 rootfs 就绪后得到 init ENOENT。2026-07-17 的受控 U-Boot/QEMU 负向
测试精确复现为 `EXPECTED_INIT_ENOENT`，从而把该机制与板上症状闭环。

修正后的契约要求 RAM 环境和 DTB 同时使用：

```text
cpu_no_boost_1_6ghz loglevel=info init=/init
```

它只允许在本次启动的 RAM 中生效。禁止执行持久环境写入；尤其不得运行
`saveenv`。QEMU 测试 init 自行打开 `/dev/ttyS0` 并在打印唯一 marker 后
保持运行，所以无需给板级 bootargs 添加模拟器专用 console 参数。

## 2026-07-17 final QEMU matrix

本节冻结纠正后契约在 `593d5bb19` 上的最终干净重跑。此前 `87e33235`
的 pre-review 运行使用了误含 `milkv,megrez` 的旧 compatible 契约，已被
本次基于冻结 RockOS transcript 的真实四项顺序所取代。

候选清单大小为 577 字节，SHA-256 为
`d8c0fdd66b96f67d819fe9633ba37938cc3846985ebbbdfd140bf8bd093eb7b7`。
最终 `fast-result.json` SHA-256 为
`52ac303606517fa1790a931b75cec2cbcbd23efb9ab7f175c1f7ae7d05ea723e`。
板级契约大小为 7373 字节，SHA-256 为
`a9820dd9dfaa4ae489bb0f7ac1efa5949a25a2bee1c9f83d8fa0af52447d102f`。

| 候选产物 | 大小 | SHA-256 | CRC32 |
|---|---:|---|---|
| ELF | 140256672 | `8c0aafdd5984b9ce472155760910f542bec23e945998bbd69ce493a94e85de08` | `5bbcedf4` |
| booti Image | 11319776 | `3533d6f53c7a29a4c3073ef2b1a53377fef1955d4ad335e07504091a0ba49f92` | `45701d7b` |
| initramfs | 570 | `b65549ef94936fd3d42e12fe89bde8d1d7af54e20d158b7daaddd26071610174` | `88443cc5` |

五阶段结果全部符合预期：

| 阶段 | 期望 | 观察 | 耗时 | cleanup |
|---|---|---|---:|---|
| candidate | `PASS` | `PASS` | 0 s | true |
| direct Svade | `PASS` | `PASS` | 4.102 s | true |
| direct Svadu | `PASS` | `PASS` | 4.047 s | true |
| U-Boot stale bootargs | `EXPECTED_INIT_ENOENT` | `EXPECTED_INIT_ENOENT` | 62.280 s | true |
| U-Boot corrected bootargs | `PASS` | `PASS` | 6.817 s | true |

两个 direct stage 与 U-Boot 正向 stage 都命中唯一用户态 marker，随后由
runner 发出 SIGTERM，`killed=false`。stale stage 不应出现 marker；它命中
init ENOENT 后的等待超时属于负向测试设计，进程同样由 SIGTERM 清理。
所有阶段均记录 `cleanup_complete=true`。这是 runner 内部的清理证据，
不是对宿主机孤儿进程的独立第三方审计。

两个 fast profile 都使用 4 hart、Sv48、2 GiB、`zkr=false`、
`svpbmt=false`，并移除 `/chosen/rng-seed`。Svade 与 Svadu 两端均启动三个
AP、选择时间戳 fallback、到达 rootfs 和用户态 marker。

运行环境 provenance：

- container tag：`asterinas/asterinas:0.18.0-20260702`；
- container image ID：
  `sha256:c430e6be9ad8669703f33956d6ad57411af3c48ad62fcc9572b7e9b2527eb557`；
- QEMU `10.2.1`；OpenSBI `v1.7`，Runtime SBI `3.0`；
- U-Boot `2026.07`，commit
  `ece349ade2973e220f524ce59e59711cc919263f`；
- U-Boot binary 大小 9142960，SHA-256
  `f34a36531c40d3b14657792ea16faad1eebb59be7524b76c0cda750a1eb1bca6`；
- U-Boot compiler GCC `13.3.0`，Binutils `2.42`。

独立的 generic Sv39/U-Boot 回归也从当前源码重新构建并通过，其
`result.json` SHA-256 为
`fbfe9962392be549a26f6cb7c210872e1f0d612864b0c4236bb84e4816f276c5`。

现有 `fast-result.json` 本身没有嵌入 container ID/tag；上述绑定来自执行器
记录。固定镜像已经写入 CI job，后续运行仍应保留 workflow revision 与 CI
run identity，不能只复制本页的环境说明。

## Slow profile and real-DTB disposition

16 GiB slow profile 是 4-hart Sv48/Svade direct QEMU，仅在显式 opt-in、
`MemAvailable >= 20 GiB` 且 swap 已用比例不超过 25% 时获准启动。本机实际
结果是：

```text
SKIP_RESOURCE: MemAvailable is below 20 GiB
```

所以本轮没有执行 16 GiB QEMU，不能写成已验证真机完整 DRAM、no-map、
CMA 或 16 GiB 映射时序。执行器会先写 `slow-gate.json`；只有初次门禁通过
才构建候选，并在构建后重新读取 `/proc/meminfo`、写
`slow-gate-recheck.json`。初次 PASS 不是模拟成功，最终
`slow-result.json` 只会原子记录 skip/failure 或已完成的受限 QEMU 结果。

真实 DTB 契约记录 model `Milk-V Megrez`，以及按实际顺序观察到的 compatible
列表 `sifive,hifive-unmatched-a00`、`sifive,fu740-c000`、
`sifive,fu740`、`eswin,eic7700`；同时记录 4 个
CPU、Sv48、DRAM `[0x80000000, 0x480000000)`、无 `rng-seed`、大小 154800、
CRC32 `4afcb20e`。由于精确 artifact 缺失，当前分类只能是显式
`UNAVAILABLE_EXACT_ARTIFACT`，不是 PASS，也不会触发任何 QEMU 运行。

## QEMU fidelity boundaries

QEMU `virt` 没有验证：

- EIC7700 MMIO 与真实中断拓扑；
- 厂商 OpenSBI/U-Boot relocation、PMP、保留内存和 cache/coherency；
- MMC、UART、PMIC、watchdog、reset 和 power；
- 真机固定 boot hart 2、1 MHz timebase 与 16 GiB 时序；
- 真实 DTB 的 no-map/CMA 与设备节点运行；
- 真实硬件的 A/D 模式和时间戳 fallback 的安全熵质量。

QEMU fast DTB 的 timebase 是 10 MHz，真机契约是 1 MHz；QEMU U-Boot 的
DTB 地址是 `0x88000000`，真机地址是 `0xf0000000`。这些都属于明确的近似，
不能被“matrix PASS”掩盖。

## Next physical experiment

下一次真机实验应从当前干净 HEAD 重新冻结 Image/initramfs 身份，在主机、
板端文件和 U-Boot 内存中逐项核对大小与 CRC；使用默认 Sv48，只在 RAM 中
同步修正环境与 DTB bootargs，然后执行唯一一次 `booti`。成功判据是
`/init` 打印唯一 marker；失败时记录第一条缺失边界。实验必须由能够外部
复位或断电的操作员现场进行，本页不授权任何无人值守硬件操作。
