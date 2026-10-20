# 开发板 KVM 推进（S7）：Asterinas-under-KVM 进展与卡点（P2 状态报告）

**执行日期**：2026-10-19（宿主 pku4090 时钟）
**执行者**：board-kvm-advancer（P2；时间盒 4 h，本报告为阶段状态，不宣称完成）
**前置**：S5（KVM 模块加载）/ S6（Linux guest 端到端跑通，6.2–6.4×）

---

## 0. 一句话结论

**KVM 本身已证明可跑真实 Linux guest 到 init；Asterinas 仍未进入内核**：其 guest 在**早期引导切换到运行时页表之后**，对内核高半区 VA 的取指触发 G-stage fault（`SCAUSE=0x14, STVAL=0xffffffff80ab66f2, HTVAL=0xa`，KVM 返回 `-95/EOPNOTSUPP`）。用自研的最小 S-mode 载荷已证明 **KVM 的 Sv39 页表 + 高半区跳转本身工作正常**（见 §2），因此卡点定位在 **Asterinas 引导/运行时页表构造**，而不是板上 KVM 不可用。

---

## 1. 实验设置

| 项 | 值 |
| --- | --- |
| guest 镜像 | `target/osdk/aster-kernel-osdk-bin.Image`（2026-10-19 11:16 构建，16,261,864 B，sha256 `245d54df…`；另有 ktest 版 17,102,008 B / `e7e582bf…`，两者现象相同） |
| 加载 | `-bios none -kernel stub2.bin -device loader,file=<Image>,addr=0x80200000,force-raw=on`（stub：`li t0,0x80200000; jr t0`，保留 a0=hartid / a1=DTB） |
| QEMU | 板载 9.2.0；`-machine virt -accel kvm -cpu host -m 2G -smp 2/4 -nographic -nic none -no-reboot` |
| 对照 | 同一 Image 在 **TCG + OpenSBI**（`-bios default`）下可正常启动并跑完 974 个 ktest（wall 31.4 s，exit 0） |
| 板子状态 | 全程未重启、未改 boot、未持久化；`boot_id` 不变 |

---

## 2. 已排除的变量（都用实测收据）

| 变量 | 实测 | 结论 |
| --- | --- | --- |
| KVM 管道本身 | `info kvm` → `kvm support: enabled`；S-mode 裸程序写 UART 成功 | KVM 执行链路 OK（S5 §3.2） |
| 通用 Sv39 页表 + 高半区跳转 | 自研 154 B 载荷：`satp` 写入 Sv39（identity idx0/idx2 + high-half idx510），打印 `P`→`S`→`H` 后 poweroff | **KVM 的 VS-stage 页表与高半区取指正常**（关键对照） |
| `-bios default`/OpenSBI 作 guest 固件 | QEMU 直接拒绝：`Machine mode firmware is not supported in combination with KVM.` | 不可用（QEMU 设计限制，非板子问题） |
| 直载地址 | KVM 直载入口是 **0x80000000**（`riscv_calc_kernel_start_addr` 对齐 DRAM base），Asterinas Image 链接在 0x80200000 | 已用 stub/`-device loader` 修正到 0x80200000 |
| Image 是否被正确加载 | 用 QEMU monitor `xp` 逐段比对：0x80200040 / 0x802000A0 / 0x80203000（Sv39 根表）/ 0x80B7DF72 处的内存字**与镜像文件逐字节一致** | 加载无误 |
| Asterinas 静态引导页表 | 从镜像 dump：idx0=0xCF、idx1=0x1000_00CF、idx2=0x2000_00CF（覆盖 code/UART）、idx508/509/510=0x0/0x1000_00CF/0x2000_00CF（对应 VA 0xffffffff0/4/8000_0000） | 表项正确 |
| 分页模式 | 反汇编 boot code：`(SATP_MODE)=(−1<<63)=8<<60` → **Sv39 build**；`satp = (表PA>>12) | 8<<60`，写后回读校验通过 | 非 Sv48 假设 |
| `-smp`/`-m`/`-cpu host,sv48=false` | 现象不变（同一 SEPC/HTVAL） | 与 SMP/内存/Sv48 选择无关 |

---

## 3. 失败现象与已定位的层次

```
QEMU: error: kvm run failed Operation not supported
host dmesg:
kvm [..]: VCPU exit error -95
kvm [..]: SEPC=0xffffffff80ab66f2 SSTATUS=0x200004120 HSTATUS=0x2002001c0
kvm [..]: SCAUSE=0x14 STVAL=0xffffffff80ab66f2 HTVAL=0xa HTINST=0x0
```

寄存器现场（QEMU monitor `info registers`，同一失败）：
`pc = ra = 0xffffffff80ab66f2`；`sp = 0xffffffff80204030`；`gp = 0xffffffff81245bb0`；
`t2 = 0xffffffffd00dfeed`（FDT magic，符号扩展常量）；`a6 = 0xffffffc0bfe003d0`（DTB 的 direct-map 地址）。
→ 现场显示：已经在 Rust 引导代码里（GP 已设置、栈在 boot stack 深处、正在处理 FDT），
`pc==ra` 说明是**函数返回/间接跳转到内核高半区文本地址**时取指失败；`sp` 深处说明引导代码已执行了相当一段。

KVM 侧机理（`arch/riscv/kvm/vcpu_exit.c::gstage_page_fault`，v6.6.87）：

```c
fault_addr = (trap->htval << 2) | (trap->stval & 0x3);   /* = (0xa<<2)|2 = 0x2a */
gfn = fault_addr >> PAGE_SHIFT;                          /* = 0 */
memslot = gfn_to_memslot(...); hva = ...;                /* 无内存槽 */
if (kvm_is_error_hva(hva) || ...) {
    switch (trap->scause) {
    case EXC_LOAD_GUEST_PAGE_FAULT: ... mmio_load
    case EXC_STORE_GUEST_PAGE_FAULT: ... mmio_store
    default: return -EOPNOTSUPP;   /* 0x14 = 取指 guest-page fault */
    }
}
```

即：**guest 的 VS-stage 翻译把该取指映射到了 GPA≈0x28（不在任何 RAM 槽）**，而不是 RAM 内的
`0x80ab66f2`。结合 §2（静态引导页表正确、通用载荷通过），最合理的解释是：
**Asterinas 在 Rust 引导阶段安装的"运行时页表"在该 VA 上存在错误表项**（或其查询 ISA/平台信息所得的条件与 KVM guest 环境不符）。

尚未验证的假设（按优先级，下一步）：

1. **DT/ISA 差异导致运行时页表 flags 不同**：TCG 下 QEMU 生成 `riscv,isa` 含 `svpbmt/svade/svadu/sstc` 等；KVM 下 guest ISA 来自宿主 `/proc/cpuinfo` 的旧式字符串（`rv64imafdch_zicntr_…`，缺多字母扩展）。若 Asterinas 依据运行期能力在 PTE 中置 PBMT/A-D 相关位，而 KVM guest 的 CPU 模型未声明该能力，可能出现非法 PTE 行为。
2. 运行时页表的**根表地址/布局**在该环境算错（例如依赖 `satp` 读回或 `hgatp`），需要在 `activate_page_table` 前后打点。
3. `cbo.zero`/`zicboz` 等清零页表路径在 KVM guest 上不可用（非法指令或未生效），导致新页表内容为垃圾。

**下一步（建议，需约 1–2 h，且需要改 Asterinas 引导代码/重编镜像的授权）**：
- 用带早期 UART 打点的调试镜像，在 `bsp_boot_virt`、`riscv_boot` 设置运行时页表前后、`activate_page_table` 之后分别打印 `satp`、关键 PTE、PC/RA；对比 TCG 与 KVM 的轨迹；
- 对比 TCG 与 KVM 两个 guest 的 DTB（`/proc/device-tree` 不可用，可 dump 内存中的 FDT 比较 `riscv,isa`/`riscv,isa-extensions`）；
- 若确认是 ISA/DT 差异，可尝试 QEMU KVM 侧 `-cpu host` 的扩展覆盖（若 QEMU 支持），或让 Asterinas 以保守 flags 建表。

---

## 4. 时间盒说明

P2 进行中尚未进入内核；本报告如实记录卡点与已排除项。若继续，需要**授权修改 Asterinas 引导/构建**（当前授权仅限 `/tmp` 下 stub/loader 与引导构建脚本范围内），或接受"以 Linux guest 作为 KVM 可用性/性能基线，Asterinas under KVM 作为后续子任务"。

---

## 5. 板子状态

未重启（`boot_id 9c777a00-…` 不变）、无 QEMU 残留、kvm 模块仍加载、`/dev/kvm` 在、boot/extlinux 未改。

## 6. 产物

| 路径（pku4090） | 说明 |
| --- | --- |
| `/tmp/out/stub2.bin`、`/tmp/out/tbl3.bin` | 跳转 stub；通用 Sv39 页表验证载荷 |
| `/tmp/asterinas-kvm-smoke/` | guest 镜像/initramfs 暂存（板上） |
| `/tmp/board-kvm-advance/monitor.py`/`mon2.py` | QEMU monitor 内存/寄存器取证 |
| 本文件 | 证据 |

---

## 7. P2b（授权 A 后 90 分钟盒内）：ISA/DT 假设的验证与排除

### 7.1 两种 guest 实际拿到的 DTB 对比（`-machine virt,dumpdtb=`）

| 属性（cpu@0） | KVM guest（`-cpu host`） | TCG guest（对比） |
| --- | --- | --- |
| `riscv,isa` | `rv64imafdc_zicntr_zicsr_zifencei_zihpm_zba_zbb` | `rv64imafdch_zic64b_…_sstc_sstvala_sstvecd_svade_svpbmt_svvptc` |
| `riscv,isa-extensions` | `i m a f d c zicntr zicsr zifencei zihpm zba zbb` | 再加 `h zic64b zicbom zicbop zicboz … sstc svade svpbmt svvptc zfa zbc zbs zkr zmmul …` |
| cbo block size 属性 | 无 | 有 |
| mmu-type | 无 | 无 |

结论：KVM guest 的 ISA 集确实远小于 TCG guest（宿主旧式 ISA 串导致），**与 Lead 假设的"PBMT/ISA 差异"方向一致**，但需验证是否真因。

### 7.2 用自定义 DTB 复验（不重编 Asterinas）

- 从 `kvm.dtb` 反编译出 `kvm.dts`，把每个 CPU 的 `riscv,isa-extensions` / `riscv,isa` 替换为 TCG 的完整串，并补上 cbo block-size 属性；
- `dtc` 重新编译成 `kvm-tcgisa.dtb`（2 个 CPU 节点已带 TCG ISA，实测有效）；
- 用 `-dtb /tmp/kvm-tcgisa.dtb` 重新启动 Asterinas：
  - 现场寄存器显示 **确实用了新 DTB**（`a6=0xffffffc0bfe006ec`，DTB 偏移随新表改变）；
  - 但失败**完全相同**：`SEPC=0xffffffff80ab66f2`、`SCAUSE=0x14`、`HTVAL=0xa`。

**→ "ISA/DT 差异（含 Svpbmt）导致"的假设被实测排除**（至少不是唯一/直接原因）。

### 7.3 精确化故障点：GPA 0x28 的来源

对 `VA = 0xffffffff80ab66f2` 做 Sv39 索引分解：

```
L2(root) idx = 510
L1      idx = 5
L0      idx = 182
```

KVM 的 `gstage_page_fault` 把 `HTVAL=0xa` 还原为 GPA `= 0xa<<2 = 0x28`；而 **`0x28` 恰好 = 0（PA 0） + L1_idx(5) × 8**。
即：**运行时页表的 root[510] 是一个"合法非叶子 PTE 但子表物理地址为 0"的条目**（形如 `PTE_V | (0>>12)<<10`），页表走查在访问"位于物理地址 0 的 L1 表"时触发 G-stage 失败。而**引导用的静态 Sv39 表 root[510] = `0x200000CF`（正确的 1 GiB 叶子，指向 PA 0x80000000）**（已从镜像 dump 验证，且 guest 内存逐字节核对）。

**→ 卡点可精确定位为：Asterinas Rust 引导阶段安装的"运行时页表"的 root[510] 条目错误（子表指针为 0），不是 KVM 的页表能力问题。**
（对照：自研最小载荷的 Sv39 静态表 + 高半区跳转在 KVM 下完全正常，见 §2。）

### 7.4 90 分钟盒内的其余检查

| 检查 | 结果 |
| --- | --- |
| Image 加载正确性（monitor `xp` 比对 4 处：code/table/函数） | 与文件逐字节一致 |
| 静态引导 Sv39 表内容 | 正确（idx0/1/2/508/509/510 均符合预期） |
| 分页模式 | 反汇编 boot code 确认为 **Sv39 build**（`8<<60`） |
| `-smp`/`-m`/`-cpu host,sv48=false` | 现象不变 |
| 自定义 DTB（TCG ISA） | 无效，见 §7.2 |
| 与 TCG 对照（同 Image + OpenSBI） | 正常启动并跑完 974 ktest |

### 7.5 未完成与下一步（需隔离构建 + 打点，超出本 90 分钟盒）

1. 在隔离 worktree（不动主工作区）构建带打点的调试镜像：在 `bsp_boot_virt`/`riscv_boot` 进入、运行时页表构建前后、`activate_page_table` 前后打印 `satp`、root 表地址与 root[510] 的原始值；
2. 或直接用 QEMU `-s -S` + gdbstub 在 `activate_page_table` 后读 `satp` 并 dump root[510]（无需改内核）；
3. 重点排查：运行时页表构造时"高半区内核映射"的父表条目生成逻辑（为何子表 paddr 为 0）、以及是否与 `PagingConsts`/页大小/分配器在 KVM 环境下取到的内存区域有关；
4. 在修复前，**以 S6 的 Linux guest 作为板载 KVM 可用性与性能基线**（已足够），Asterinas-under-KVM 记为后续子任务。

### 7.6 板子状态（P2b 结束）

未重启（`boot_id 9c777a00-…` 不变）、无 QEMU 残留、kvm 模块仍加载、`/dev/kvm` 在、boot/extlinux 未改、未做持久化。新增临时产物仅 pku4090 `/tmp/{kvm.dtb,tcg.dtb,kvm.dts,tcg.dts,kvm-tcgisa.*}` 与板上 `/tmp/kvm-tcgisa.dtb`。
