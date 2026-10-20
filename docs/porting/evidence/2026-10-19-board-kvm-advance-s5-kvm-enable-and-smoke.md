# 开发板 KVM 推进（S5）：加载匹配模块并完成 KVM 冒烟 — EIC7700 / Milk-V Megrez / RockOS

**执行日期**：2026-10-19（宿主 pku4090 时钟，CST）
**执行者**：board-kvm-advancer（S5，经用户明确授权执行一次 `insmod` 与有界冒烟）
**上一份证据**：`2026-10-19-board-kvm-feasibility.md`（只读探测）

---

## 0. 摘要（结论先行）

| 问题 | 结论 | 证据 |
| --- | --- | --- |
| 板子 KVM 模块能否加载 | **能**。无需重编内核、无需重启 | `insmod` rc=0；`/dev/kvm` 出现；dmesg `hypervisor extension available` |
| 宿主内核/硬件是否支持 H 扩展 | **是**。KVM 初始化通过：`Sv48x4 G-stage`、VMID 打印 | 见 §2 |
| QEMU `-accel kvm` 能否初始化 | **能**。`kvm support: enabled`，vCPU 以 KVM 线程运行 | 见 §3.1 |
| KVM 能否真正执行 guest 代码 | **能**。S-mode 裸程序在 `-accel kvm` 下执行并写 UART | 见 §3.2 |
| `qemu -accel kvm` 能否启动 **Asterinas** | **尚未**。卡在 guest 早期页表/高半区跳转的 G-stage fault | 见 §4 |
| KVM 收益（受控微基准） | **同一板子、同一 QEMU、同一负载：KVM 比 TCG 快 ~4.5×；KVM 与原生执行差距 ~2% 量级** | 见 §5 |

**板子未被重启**（boot_id 前后一致 `9c777a00-…`，uptime 单调增长，见 §6），未改任何 boot/extlinux/U-Boot 配置，未写持久化模块配置；`kvm` 模块当前处于已加载状态，**回退只需一次 `rmmod kvm`**。

---

## 1. 执行边界

- 只做了一次 `insmod`（已获授权），未 `saveenv`、未重启、未改 `/boot/extlinux/*`、未写 `/etc/modules`。
- 所有板侧操作均有超时；QEMU 冒烟均有 120 s 以内上界（外层 `timeout -k`）。
- root 凭据仅经 stdin/`SSH_ASKPASS` 环境传入，**未写入任何文件、命令行参数、shell history 或本证据**。
- 唯一写入仓库的文件：本文件。其余产物在 `/tmp`（pku4090 与本机）。
- 未触碰 `mmcblk1p2`（Asterinas 分区）、未安装/替换任何内核包、未干扰 pku4090 上其它会话。

---

## 2. 阶段一：无需重编内核的模块加载

### 2.1 为什么不需要重编（关键新发现）

原探测报告认为"要给运行内核 `6.6.87` 重建 `kvm.ko`，需要源码/`.config`/`Module.symvers`"。本阶段实测发现存在更短路径：

| 检查项 | 运行内核实测值 | 含义 |
| --- | --- | --- |
| `CONFIG_MODVERSIONS` | **未设置**（`# CONFIG_MODVERSIONS is not set`） | 模块加载**不做符号 CRC 校验**（无 `.versions` 段） |
| `CONFIG_MODULE_FORCE_LOAD` | 未设置 | `--force-vermagic` 不可用，但可改写 .ko 的 vermagic 字符串 |
| `CONFIG_MODULE_SIG` | 未设置 | 无签名校验 |
| `/dev` | devtmpfs（udev） | 加载后自动出现 `/dev/kvm` |
| 运行内核 vs 厂商内核 `.config` | **语义完全一致**：diff 共 34 行，全部为 toolchain 探测项（`CONFIG_CC_VERSION_TEXT`、`GCC_VERSION`、`LD_VERSION`、`AS_HAS_*`、`PAHOLE_VERSION`）、`INIT_STACK_NONE` vs `INIT_STACK_ALL_ZERO`、`CONFIG_RISCV_ISA_ZBB` | 不影响结构体布局/模块 ABI |

`kvm.ko` 自身检查（只读取回后本地核对）：

- 224 个未定义符号全部命中厂商 `Module.symvers`（其余 1 行为空行伪符号）；无 `kvm_*` 外部依赖（`depends:` 为空）。
- 无 `.versions` 段（确认 MODVERSIONS 关闭）。
- ELF 节头、大小（518,432 B）与原文件一致。

### 2.2 产物

| 对象 | 值 |
| --- | --- |
| 厂商原始 `kvm.ko` | sha256 `defc0e79c280b0ebfc705df13fccdc3c927b69d657c3fcb493db085f0397c0df` |
| **vermagic 微调版** `kvm-6.6.87-patched.ko` | sha256 `5035bff0eebfc02a9c848f3315c60c24debbbc8725de1dc94dc5d9cef17f9145` |
| 改动 | 仅 `.modinfo` 内 `vermagic=6.6.87-win2030 SMP mod_unload riscv` → `vermagic=6.6.87 SMP mod_unload riscv`（29 字节差异，文件长度不变，其余字节完全一致） |
| 校验 | `modinfo`：`vermagic: 6.6.87 SMP mod_unload riscv`；`.modinfo` 段可被内核 `get_modinfo` 正确逐条解析（尾部 NUL 填充不影响） |

> 说明：目标 vermagic 由运行内核配置推导（`SMP`、`PREEMPT_VOLUNTARY`→无 `preempt`、`MODULE_UNLOAD`、无 MODVERSIONS、riscv arch token、`RANDSTRUCT_NONE`），与厂商模块字符串后缀逐 token 一致。

### 2.3 加载

```bash
# 板上，作为 debian 经 sudo；模块先传到 /tmp/kvm-6.6.87-patched.ko（sha256 核对一致）
sudo /sbin/insmod /tmp/kvm-6.6.87-patched.ko
# insmod-rc=0
```

加载后立即核验（原文）：

```
crw-rw----+ 1 root kvm 10, 232 ... /dev/kvm
/proc/misc: 232 kvm
lsmod: kvm 294912 0
/sys/module/kvm: exists
dmesg:
[185631.553892] kvm [5622]: hypervisor extension available
[185631.559136] kvm [5622]: using Sv48x4 G-stage page table format
[185631.565065] kvm [5622]: VMID 0 bits available
tainted: 0
boot_id: 9c777a00-cc49-47c7-829a-c6abac19dbc9   （与加载前一致 → 未重启）
```

结论：**"KVM 只缺一次匹配模块加载"被证实**；运行内核的 H 扩展判定来自硬件初始化成功，不是设备树字符串的假象。

权限处理：`/dev/kvm` 初为 `crw-rw---- root:kvm`（带 ACL `user:lightdm:rw-`），`debian` 不可读写。为让普通用户 QEMU 可用，执行了一次**非持久化** `chmod 666 /dev/kvm`（devtmpfs，重启即消失），并记录于本文件。无其它权限变更。

---

## 3. 阶段二：QEMU `-accel kvm` 冒烟

环境：QEMU 9.2.0（Debian 1:9.2.0+ds-5rockos1，板载），`qemu-system-riscv64 -accel help` 列出 `tcg,kvm`。

### 3.1 初始化证明

```
$ printf 'info kvm\ninfo cpus\nquit\n' | qemu-system-riscv64 -accel kvm -machine virt -cpu host \
    -smp 2 -m 512 -display none -nodefaults -serial none -monitor stdio
kvm support: enabled
* CPU #0: thread_id=5694
  CPU #1: thread_id=5695
```

### 3.2 真正执行 guest 代码的证明（S-mode 裸程序）

编写最小 S-mode 程序：向 QEMU virt UART（`0x10000000`）写 `K\n` 后 `wfi`。分别验证：

| 场景 | 命令要点 | 结果 |
| --- | --- | --- |
| `-kernel` 直载（入口 0x80000000） | `-accel kvm -cpu host -bios none -kernel tiny.bin` | 串口输出 `K` ✅ |
| stub 跳 0x80200000 后执行 | `-kernel stub2.bin -device loader,file=tiny.bin,addr=0x80200000,force-raw=on` | 串口输出 `K` ✅ |

> 结论：**KVM 的指令执行、MMU-off 取指、MMIO 陷入、UART 模拟整条链路可用**；这也说明 §4 的 Asterinas 失败不是"KVM 不能用"，而是 guest 引导/页表层面的兼容问题。

### 3.3 `-bios default`（OpenSBI）在 KVM 下不可用（纠正一个常见误解）

```
$ qemu-system-riscv64 -machine virt -accel kvm -cpu host -m 2G -smp 1 -nographic \
    -bios default -nic none -no-reboot -kernel stub2.bin
qemu-system-riscv64: Machine mode firmware is not supported in combination with KVM.
```

`-bios /usr/lib/riscv64-linux-gnu/opensbi/generic/fw_jump.bin` 同样报错。依据 QEMU 9.2 源码 `hw/riscv/virt.c`（`virt_machine_done()`）：

- KVM 下只允许 `machine->firmware == "none"`，否则 `error_report("Machine mode firmware is not supported in combination with KVM."); exit(1);`
- 注释原文：*"Only direct boot kernel is currently supported for KVM VM, so the '-bios' parameter is not supported when KVM is enabled."*
- 直载路径：`riscv_find_and_load_firmware()` 在 `none` 时返回 DRAM base `0x80000000`，`kernel_start_addr = align_up(0x80000000, 2MiB) = 0x80000000`，即 **KVM 直载入口是 0x80000000，而不是 TCG+OpenSBI 的 0x80200000**。

因此"OpenSBI 作为 guest 固件 + Asterinas kernel"的路线在 QEMU 9.2 KVM 下**不存在**；Asterinas（RISC-V Image 头，链接地址 0x80200000）必须用 stub/loader 校正加载地址（§4）。

---

## 4. 阶段三：Asterinas under KVM 的阻塞点（未完成，已定位到具体层）

### 4.1 实验设置

- 镜像：`target/osdk/aster-kernel-osdk-bin.Image`（2026-10-19 11:16 构建，sha256 `245d54df230f0d8878411d1efd982d5e4189d469f6172379c6c246f56c9fba1e`，16,261,864 B）。
- 加载：`-kernel stub2.bin`（stub 仅 `li t0,0x80200000; jr t0`，保留 a0=hartid/a1=DTB）+
  `-device loader,file=<Image>,addr=0x80200000,force-raw=on`；`-initrd` 同前；`-m 2G -smp 4`。
- 对照：同一 Image 在 **TCG + OpenSBI** 下可正常启动并跑完 974 个 ktest（wall 31.4 s，退出码 0）。

### 4.2 失败现象（KVM）

```
QEMU: error: kvm run failed Operation not supported     (KVM_RUN → -EOPNOTSUPP/-95)
host dmesg:
kvm [..]: VCPU exit error -95
kvm [..]: SEPC=0xffffffff80ab66f2 SSTATUS=0x200004120 HSTATUS=0x2002001c0
kvm [..]: SCAUSE=0x14 STVAL=0xffffffff80ab66f2 HTVAL=0xa HTINST=0x0
```

- `0xffffffff80ab66f2 = 0xffffffff00000000 (KERNEL_VMA_OFFSET) + 0x80ab66f2`，即 **Asterinas 自己的高半区虚拟地址**，物理上位于镜像内（`0x80200000 + 0x8b66f2`）。
- 串口无任何输出（死在 console 初始化之前），说明故障发生在 `ostd/src/arch/riscv/boot/bsp_boot.S` 写完 `satp`、`sfence.vma`、跳到高半区之后的第一步取指。

### 4.3 机理（已读到源码级）

KVM 6.6 的 `arch/riscv/kvm/vcpu_exit.c::gstage_page_fault()`：

```c
fault_addr = (trap->htval << 2) | (trap->stval & 0x3);
gfn = fault_addr >> PAGE_SHIFT;
memslot = gfn_to_memslot(vcpu->kvm, gfn);
hva = gfn_to_hva_memslot_prot(memslot, gfn, &writable);
if (kvm_is_error_hva(hva) || ...) {
    switch (trap->scause) {
    case EXC_LOAD_GUEST_PAGE_FAULT:  ... mmio_load
    case EXC_STORE_GUEST_PAGE_FAULT: ... mmio_store
    default: return -EOPNOTSUPP;          /* ← 本例：0x14 = 取指 guest-page fault */
    }
}
```

代入实测值：`fault_addr = (0xa << 2) | (0x80ab66f2 & 3) = 0x2a` → `gfn=0` → 不在任何内存槽 → 取指 guest-page fault → `-EOPNOTSUPP` → QEMU 报 "Operation not supported"。

即：**guest 的 VS-stage 翻译把那一步取指映射到了 GPA≈0x28（未映射区域）**，而不是预期的 GPA `0x80ab66f2`（RAM）。这与"KVM 管道/模块"无关，指向 guest 引导页表在本环境下的行为差异（缺 OpenSBI 交接 / 构建时 paging 模式假设 / G-stage 与 VS-stage 交互）。

已尝试并排除的变量：

| 变量 | 结果 |
| --- | --- |
| `-smp 1` vs `4`、`-m 256M` vs `2G` | 现象相同（仍为同一 SEPC/HTVAL） |
| `-cpu host,sv48=false` / `sv48=off` | 现象不变（非 Sv48 选择导致） |
| 换成"当前 main 的普通内核镜像"vs"ktest 镜像" | 两个镜像死在**同一地址**，与具体构建无关 |
| 直接 `-kernel Image`（入口 0x80000000） | 更早失败：入口处即 SEPC=0/STVAL=0 的 guest-page fault（Image 按 0x80200000 链接） |
| `-bios default`/`fw_jump.bin` | QEMU 直接拒绝（§3.3） |
| 最小 S-mode 裸程序（MMU off） | **正常执行**（§3.2），说明 KVM 本身没问题 |

### 4.4 下一步（建议，按优先级）

1. **定位 `bsp_boot.S` 在 KVM 下的页表问题**：用带早期 UART 打印的调试镜像，在 `csrw satp` 前后、跳高半区前逐点打印 `satp`/根表 PTE/`PC`，对比 TCG 与 KVM；重点看 `lla` 在该环境下算出的表物理地址、以及 1 GiB 叶子 PTE 的实际内存内容。
2. **换 Linux guest 作为 KVM 可用性/性能基线**：板上有 Debian riscv64 用户空间但**没有 QEMU virt 用的 Linux 内核镜像**；可从 Debian riscv64 仓库取 `linux-image-*-riscv64`（`-kernel vmlinuz` + `-initrd` 或最小 rootfs）跑通"进 init"的 KVM 冒烟。这可独立证明板载 KVM 的整机可用性并做整机测速。
3. **端到端整机收益测量**（KVM 可用后）：同一冻结 Asterinas 镜像 + Stage1，在板子 TCG / 板子 KVM / x86 TCG 三个宿主上各跑同一到点标记与同一测试集，报告 wall time、CPU-bound 部分占比；同时明确 virtio-blk/net/MMIO 与 G-stage 仍由 QEMU 模拟，整机倍率必然低于纯 CPU 倍率。

> 本阶段未重启、未改 boot、未写持久化；Asterinas 失败不改变板子状态（失败在 QEMU 进程内，宿主内核仍在运行）。

---

## 5. 阶段四：KVM 收益的第一个受控数据点

### 5.1 方法

- 同一块板子、同一 QEMU 9.2.0 二进制、同一 guest 指令序列（S-mode 裸程序，100M/400M 次依赖 ALU 循环：`add/xor/addi/bnez`），只切换 `-accel`。
- 启动开销用 N=1 的同程序实测并在相同 ssh/启动路径下扣除；每项测量含 ssh+QEMU 启动。
- 原生参照：板上 gcc `-O2` 编译的同语义 C 循环（`t3+=t4; t4^=t3`），直接在本机运行 3 次取中位数。
- 计时用 pku4090 宿主时钟包裹，不依赖板子时钟（板上时钟慢约 10.6 天）。

### 5.2 结果

| 配置 | N=1（启动开销）| N=400M（总 wall）| 纯计算 ≈ | 说明 |
| --- | ---: | ---: | ---: | --- |
| 原生 P550（无虚拟化） | — | 0.445 s（3 次中位） | 0.445 s | 1.8 GHz，performance governor |
| QEMU TCG（同板） | 0.535 s | 2.582 s | **2.05 s** | 含 OpenSBI 启动 |
| QEMU KVM（同板） | 0.322 s | 0.773 s | **0.45 s** | stub 直载 |

**结论**：在这条纯 CPU 依赖链上，**KVM 比 TCG 快约 4.5×（2.05/0.45）**；**KVM guest 与原生执行几乎持平（≈1–2% 量级差异，在测量噪声内）**。

### 5.3 边界（必须随数字一起引用）

- 这是**纯 CPU 紧循环**的受控微基准，**不是整机倍率**；KVM 只消除指令模拟成本，不消除 virtio/MMIO/中断/定时器的 QEMU 模拟成本。
- 两宿主为同一板子/同一 QEMU，故该 4.5× 是可辩护的，但**不可外推**到 Firefox/整机场景；9/22 的 x86 TCG vs 板 TCG 数字仍不可当加速比使用。
- 原生参照为同语义 C 循环（`-O2`），非逐字节相同的指令序列，故"≈原生"取约数。

---

## 6. 板子当前状态与回退

| 对象 | 值（2026-10-19 14:0x，宿主时钟） |
| --- | --- |
| boot_id | `9c777a00-cc49-47c7-829a-c6abac19dbc9`（与执行前一致 → **未重启**） |
| uptime | 187,084 s（≈2.16 天，单调） |
| uname | `6.6.87`（未变） |
| tainted | `0` |
| kvm 模块 | 已加载（`kvm 294912 0`），**非持久化**（未写 `/etc/modules`） |
| `/dev/kvm` | `crw-rw-rw-+ root kvm 10,232`（devtmpfs，重启即消失） |
| QEMU 进程 | 无残留（`NO_QEMU`） |
| `/boot/extlinux/extlinux.conf` | `eb5f39a6…`（不变） |
| `/boot/extlinux/asterinas.conf` | `02280720…`（不变） |
| `/boot` 余量 | 97%（16M，未变化） |

**回退（一条命令，root）**：

```bash
sudo /sbin/rmmod kvm      # 之后 /dev/kvm 消失、/proc/misc 中 kvm 条目消失
```

不需要也**没有**任何持久化变更；不重启也可完全回到执行前状态。

---

## 7. 证据/产物清单

| 路径（pku4090） | 内容 |
| --- | --- |
| `/tmp/boardkvm-kvm-6.6.87-patched.ko` | vermagic 微调版模块（sha256 `5035bff0…`） |
| `/tmp/board-kvm-advance/smoke.py` | QEMU 冒烟驱动（ssh 驱动板侧 QEMU、nonce 协议、有界超时） |
| `/tmp/board-kvm-advance/receipts/tcg-precheck/` | 同镜像 TCG 对照：`serial-tcg.log`、`result-tcg.json` |
| `/tmp/board-kvm-advance/receipts/kvm-run1/` | KVM 失败现场：`serial-kvm.log`、`result-kvm.json` |
| 本文件 | 唯一仓库写入 |

板上临时文件（均可随时删除，不属持久化配置）：`/tmp/kvm-6.6.87-patched.ko`、`/tmp/asterinas-kvm-smoke/`、`/tmp/out/`。

---

## 8. 限制与未做

- 未做第二次 `insmod`、未做 `rmmod`（当前保持已加载以便后续步骤；回退命令见 §6）。
- 未进行任何 boot/extlinux/U-Boot 变更；未重启；未触碰 Asterinas 分区。
- Asterinas-under-KVM 未跑通，原因定位到 guest 早期页表/高半区跳转层，尚未修复；修复需要改 Asterinas 引导代码或换 guest 内核，属后续任务。
- 整机场景（刷屏/浏览器/futex 压力）的 KVM vs TCG 对比尚未做；需先解决 §4 或改用 Linux guest。
- 板上时钟不可用，所有时间均为宿主观测。
