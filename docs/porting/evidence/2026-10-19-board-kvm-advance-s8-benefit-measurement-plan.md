# 开发板 KVM 收益测量方案（S8 / P3）

**状态**：方案（尚未执行测量；S6 已给出第一个受控数据点）
**目标**：量化"板载 KVM 相对 TCG / x86 TCG"对**整机场景**的收益，并给出可辩护的边界。

---

## 1. 已确立的基线（可直接引用）

| 实验 | 配置 | 结果 |
| --- | --- | --- |
| 纯 CPU 依赖链（S5 §5） | 同板同 QEMU，100M/400M 次 ALU 循环 | native 0.445 s；KVM 0.45 s；TCG 2.05 s → **KVM ≈ native；KVM/TCG ≈ 4.5×** |
| 内核启动到 init（S6） | 厂商 Linux 6.6.87 + 最小 initramfs，3 次中位 | KVM wall 0.876 s / TCG 5.450 s → **6.2×**；guest 内核自身时间戳 0.283 vs 3.687 s → **13.0×** |

结论方向：KVM 抹掉的是**指令模拟**成本；**设备模拟（virtio/MMIO/中断/定时器）与 G-stage 翻译仍在 QEMU 里**，所以整机倍率一定低于纯 CPU 倍率。

---

## 2. 测量矩阵（按场景，全部同输入/同 QEMU/同板）

### 2.1 启动时间（boot-to-marker）

| 场景 | marker | 变体 |
| --- | --- | --- |
| 最小 Linux initramfs → init | `BASELINE_INIT_REACHED` | KVM / TCG（已完成，S6） |
| Asterinas → 内核进入 + 首个用户态进程 | 内核打印首行 + Stage1 用户态标记 | KVM / TCG（待 Asterinas-under-KVM 修复后） |
| Asterinas → Xorg 起栈（虚拟显示） | Xorg 日志 ready + `asterinas.desktop` 就绪 | KVM / TCG |
| Firefox 冷启动 → 首帧/可交互 | 既有 `browser` 证据协议的 ready marker | KVM / TCG / x86 TCG |

**方法**：每场景 ≥3 次，中位 + 极差；同一 initramfs/rootfs/内核/DTB；同一 QEMU 9.2.0；只切 `-accel`；
计时用宿主 `time.monotonic()`（板上时钟不可用）；同时记录 guest 内核 printk 时间戳做交叉校验。

### 2.2 CPU/内存/IO 微基准（guest 内）

| 项 | 工具/方法 | 目的 |
| --- | --- | --- |
| 指令吞吐/依赖链 | 固定 ALU 循环（已做） | 上界倍率（≈native） |
| 系统调用热路径 | futex/syscall 基准（复用仓库 `futex_lost_wakeup`、ktest 中的计时项） | 反映 trap/exits 成本 |
| 内存带宽/延迟 | 指针追逐 / memcpy 流 | G-stage 翻译开销 |
| 块 IO | virtio-blk 顺序/随机读 | 判断 IO 是否成为瓶颈（KVM 不加速设备模拟） |
| 软中断/定时器 | 定时器抖动/中断风暴 | 判断 idle/定时器路径 |

### 2.3 并发与正确性

- 4 vCPU 的 futex lost-wakeup、调度器压力（复用既有 ktest/futex 调试输入）在 KVM 下的表现；
- 记录：是否更易复现、是否出现 KVM 特有挂死；**不作为正确性证明**（时序变化是调试变量）。

---

## 3. 对照与判据

| 对照 | 用途 | 注意 |
| --- | --- | --- |
| 板 TCG（同板同 QEMU） | **主对照**，消除硬件/ISA 差异 | TCG 含 OpenSBI 固定开销，报告时单列 |
| 板 KVM（同板同 QEMU） | 目标 | 需 `/dev/kvm`（S5 路径，非持久化） |
| x86 TCG（pku4090 容器 QEMU） | 既有工作流对照 | **不得**用 9/22 的 18.6× 之类数字；只能同输入重新测 |
| native（无虚拟化，板上直接跑） | 上界参照 | 仅对可原生跑的负载（如纯计算程序） |

**判据**：
1. 功能：同一场景 marker 必须在 KVM/TCG 两侧都出现，否则该场景不计入倍率；
2. 性能：倍率 = TCG 中位 / KVM 中位；同时给 guest 内部时间戳口径；
3. 误差控制：≥3 次、报告极差、固定 CPU governor（performance）、固定 `-smp/-m`、关闭无关设备（`-nic none`）、避免与其它负载并发；
4. **边界声明**：所有倍率标注"不含/包含 device emulation 的哪几段"，禁止外推到物理板（物理 MMC/以太网/USB/HDMI 只能裸机验证）。

---

## 4. 预期结论形态（避免过度声明）

- 纯 CPU 段：KVM ≈ native，相对同板 TCG 约 **4–13×**（视负载依赖链/内存访问比例）；
- 整机启动（含设备探测、virtio、用户态）：倍率显著低于纯 CPU 段，预计 **2–5×**（需实测，不得预先写死）；
- 浏览器整机场景：设备与渲染仍是 QEMU 模拟，倍率可能进一步降低；KVM 的主要价值是**同 ISA + 接近原生**，而不是"绝对快过 x86"。

---

## 5. 执行前置

1. `/dev/kvm` 可用（S5 一条 `insmod`，回退 `rmmod`；不持久化）；
2. `-dev/kvm` 权限对 `debian` 可读写（devtmpfs 下临时 `chmod 666`，重启即失效）；
3. Asterinas-under-KVM 修复（P2）后才可做 §2.1 后三行与 §2.3；
4. 所有 guest 输入（内核/Stage1/rootfs/DTB）先固定 sha256 并双向校验。

---

## 6. 交付物

- `docs/porting/evidence/2026-10-19-board-kvm-advance-*-benefit-results.md`：矩阵结果表 + 原始 log + 结论与边界；
- 每场景一份 `result.json` + `serial.log`（含非ce、时间戳、exit code）；
- 板子状态收据（boot_id 前后一致、无残留进程）。
