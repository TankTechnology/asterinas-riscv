# 开发板 KVM 推进（S6）：Linux guest 端到端基线 — 板载 KVM 可用性决定性证据

**执行日期**：2026-10-19（宿主 pku4090 时钟）
**执行者**：board-kvm-advancer（P1）
**前置状态**：S5 已加载 vermagic 微调版 `kvm` 模块，`/dev/kvm` 存在（见 `2026-10-19-board-kvm-advance-s5-kvm-enable-and-smoke.md`）

---

## 0. 结论

**板载 KVM 端到端可用已被决定性证明**：同一个 Linux 6.6.87 内核 + 同一个最小 initramfs，在同一块板、同一 QEMU 9.2.0 上：

| 指标（3 次中位） | QEMU KVM | QEMU TCG | 倍率 |
| --- | ---: | ---: | ---: |
| **端到端 wall（含 ssh+QEMU 启动）** | **0.876 s** | **5.450 s** | **6.2×** |
| 启动到 init 标记（含启动） | 0.836 s | 5.379 s | **6.4×** |
| **guest 内核自身时间戳**（`Run /init as init process`） | **0.283 s** | **3.687 s** | **13.0×** |

三次重复均稳定（KVM wall 0.874–0.878 s；TCG 5.409–5.570 s），且每次 init 都执行、`reboot: Power down` 正常、QEMU 退出码 0。

---

## 1. 方法（受控条件）

| 项 | 值 |
| --- | --- |
| 宿主 | EIC7700 / Milk-V Megrez / RockOS（`6.6.87`，4×P550 1.8 GHz，9.7 GiB RAM），**未重启** |
| QEMU | 板载 `qemu-system-riscv64 9.2.0`（Debian 1:9.2.0+ds-5rockos1），TCG/KVM 同一二进制 |
| 内核 | `/boot/vmlinuz-6.6.87-win2030-origin` 解压后的原始 `Image`：`Linux 6.6.87-win2030 (builder@rockos-riscv-builder) #2025.04.20.18.43+bb0c69aea SMP`，26,572,288 B，sha256 `790380493872e387781a96ff99b64737daaf6f4c05b5894ba500a6aa39273b81` |
| initramfs | 板上构造的最小 cpio.gz：静态 `init`（打印标记、挂 proc/sys、`reboot(RB_POWER_OFF)`），sha256 `3057ff5e6d154d22213142cbd516867d64894a4567603dc7257092ad9a9e6b86` |
| 内存/CPU | `-m 2G -smp 2`，`-nic none -no-reboot` |
| KVM 启动方式 | `-bios none -kernel stub2.bin -device loader,file=Image.raw,addr=0x80200000,force-raw=on`（stub 只做 `li t0,0x80200000; jr t0`，保留 a0/a1；原因见 S5 §3.3：KVM 直载入口是 0x80000000，而 RISC-V Image 链接在 0x80200000） |
| TCG 启动方式 | `-bios default -kernel Image.raw`（OpenSBI 把内核装到 0x80200000 并交接） |
| 计时 | pku4090 宿主 `time.monotonic()` 包裹 SSH+QEMU；另取 guest 内核 printk 时间戳做"内核自身"对照 |
| 重复 | KVM 3 次、TCG 3 次，取中位 |
| 驱动脚本 | `/tmp/board-kvm-advance/bootbench.py`（pku4090，ssh 驱动板侧 QEMU，有界超时） |

命令（KVM）：

```bash
qemu-system-riscv64 -machine virt -accel kvm -cpu host -m 2G -smp 2 -nographic -nic none -no-reboot \
  -bios none -kernel /tmp/out/stub2.bin \
  -device loader,file=/tmp/p1-guest/Image.raw,addr=0x80200000,force-raw=on \
  -initrd /tmp/p1-guest/initramfs.cpio.gz \
  -append "console=ttyS0,115200 rdinit=/init loglevel=7"
```

（`CONFIG_CMDLINE_EXTEND=y`，厂商内建 cmdline 会追加 `earlycon=sbi console=ttyS0,115200n8 clk_ignore_unused …`，无碍。）

---

## 2. 原始证据

### 2.1 KVM 启动日志（节选，第 1 次）

```
[    0.000000] Linux version 6.6.87-win2030 (builder@rockos-riscv-builder) ... #2025.04.20.18.43+bb0c69aea SMP
[    0.000000] Machine model: riscv-virtio,qemu
[    0.000000] SBI specification v1.0 detected
[    0.000000] SBI implementation ID=0x3 Version=0x60657      ← 由 KVM 提供的 SBI 仿真（无 OpenSBI）
[    0.000000] SBI TIME/IPI/RFENCE/SRST/HSM extension detected
[    0.000000] riscv: base ISA extensions acdfim
[    0.000000] Kernel command line: console=ttyS0,115200 rdinit=/init loglevel=7 earlycon=sbi console=ttyS0,115200n8 ...
[    0.000000] riscv-intc: 64 local interrupts mapped
[    0.000000] plic: plic@c000000: mapped 95 interrupts with 2 handlers for 2 contexts.
...
[    0.280782] Freeing unused kernel image (initmem) memory: 2204K
[    0.282592] Run /init as init process
=== BASELINE_INIT_REACHED ===
[    0.284983] reboot: Power down
```

（KVM 下 QEMU 退出码 0；`/dev/kvm` 由 S5 的模块提供，宿主无重启。）

### 2.2 TCG 对照（第 1 次）

```
[    0.000000] Linux version 6.6.87-win2030 ...
[    0.000000] Machine model: riscv-virtio,qemu
...
[    3.681888] Freeing unused kernel image (initmem) memory: 2204K
[    3.686729] Run /init as init process
=== BASELINE_INIT_REACHED ===
[    3.899204] reboot: Power down
```

（TCG 走 OpenSBI：`Platform Name : riscv-virtio,qemu` / `OpenSBI v1.5`；KVM 无 OpenSBI。）

### 2.3 计时汇总

| 运行 | accel | wall (s) | to-marker (s) | 退出码 |
| --- | --- | ---: | ---: | ---: |
| k1 | kvm | 0.876 | 0.836 | 0 |
| k2 | kvm | 0.878 | 0.837 | 0 |
| k3 | kvm | 0.874 | 0.833 | 0 |
| t1 | tcg | 5.409 | 5.336 | 0 |
| t2 | tcg | 5.570 | 5.498 | 0 |
| t3 | tcg | 5.450 | 5.379 | 0 |
| **中位** | kvm | **0.876** | **0.836** | |
| **中位** | tcg | **5.450** | **5.379** | |

guest 内核时间戳：`Run /init` KVM 0.2826 s vs TCG 3.6867 s（13.0×）；`Power down` 0.2850 s vs 3.8992 s。

---

## 3. 解读与边界

- KVM 客人**没有 OpenSBI**：earlycon/SBI 控制台由 KVM 的 SBI 仿真提供（SBI spec v1.0，impl ID 0x3），Linux 正常识别并接管。
- 该结果**证明板载 KVM 可运行真实 Linux guest 到用户态 init**，不只是"能加载模块"。
- **倍率解释**：guest 内核自身启动 13× 是 KVM 的直接收益（指令原生执行）；端到端 6.2–6.4× 被 QEMU 进程启动、ssh、OpenSBI（仅 TCG）等固定开销摊薄。
- **不可外推**：这是"最小 initramfs 启动到 init"的端到端数据，不含 systemd/Xorg/浏览器；整机场景的倍率会显著低于 13×，必须以实际场景测量（见 P3 方案）。TCG 一侧含 OpenSBI 启动，KVM 一侧不含，故 wall 倍率略偏向 KVM；guest 内核时间戳对照更接近"同等启动阶段"。
- 用的是**厂商原版内核**（`-origin` 解压），非运行中的 zrp 重编内核；用它是因为它与板上 initrd/DTB 配对、且是干净的原版 Linux。这不影响"KVM 可用性"结论。

---

## 4. 板子状态（执行后）

| 项 | 值 |
| --- | --- |
| boot_id | `9c777a00-cc49-47c7-829a-c6abac19dbc9`（与 S5 前一致，**全程未重启**） |
| kvm 模块 | 仍加载（未 `rmmod`，按指示保留） |
| `/dev/kvm` | `crw-rw-rw-+ root kvm 10,232`（S5 临时 chmod，devtmpfs 非持久化） |
| QEMU 残留 | 无 |
| boot/extlinux | 未触碰 |

---

## 5. 产物

| 路径 | 说明 |
| --- | --- |
| pku4090:`/tmp/p1-guest/Image.raw` | 解压后的厂商 Linux Image（sha256 见 §1） |
| pku4090:`/tmp/p1-guest/initramfs.cpio.gz` | 最小 initramfs（静态 init） |
| pku4090:`/tmp/p1-guest/init.c` | init 源码 |
| pku4090:`/tmp/board-kvm-advance/bootbench.py` | 计时驱动 |
| pku4090:`/tmp/board-kvm-advance/receipts/p1boot/` | 6 组 `boot-*.log` + `boot-*.json` |
| 板上 `/tmp/p1-guest/*`、`/tmp/out/*` | 临时产物（非持久化配置） |
