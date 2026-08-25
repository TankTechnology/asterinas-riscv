# RISC-V Sv39/Sv48 故障复盘与启动约束

日期：2026-08-25
当前修复：`b48cfeea3c8d04c4a95812b8228ccee04dbffe2b`

## 结论

Sv39 和 Sv48 不是可以由启动汇编单独切换的一个 `satp` 数值。分页模式还
共同决定页表层数、canonical 虚拟地址范围、线性映射基址、页表索引、BSP
与 AP 的启动页表，以及切换页表后的 TLB 状态。

历史上大量带 Sv39/Sv48 名称的提交包含诊断实验；实际问题可以归并为七类：

1. 启动汇编使用的 SATP mode 与 Rust `PagingConsts` 不一致；
2. leaf 层级或页表索引错误，尤其是 Sv39 下错误使用 1 GiB leaf；
3. Sv39 高半地址没有按 bit 38 正确符号扩展；
4. DTB/initramfs 保留了只在 early identity map 下有效的指针；
5. 切换 `satp` 或根页表后没有刷新旧 TLB translation；
6. Svade/Svadu 与 PTE A/D 位契约不一致；
7. DTB、QEMU CPU profile、SMP/内存参数与内核分页产物不匹配。

Megrez 已有默认 Sv48 到达 rootfs 的真机证据，因此不能把问题简化为
“Megrez 不支持 Sv48”。SiFive U 和当前 generic-Sv39 门禁则只允许 Sv39。
正确约束是：一次启动中的所有层必须使用同一分页模式。

## 历史故障链

| 提交 | 故障或修复 | 证据边界 |
|---|---|---|
| `4d377bef4` | Sv39 `boot.S` 与 Sv48 `PagingConsts` 失配 | 页表索引和线性地址不属于同一布局 |
| `f146652d8` | 将 RISC-V `PagingConsts` 切到 Sv39 | 页表层数由 4 改为 3，地址宽度由 48 改为 39 |
| `3e5b5fb44`、`50f537386`、`7aff69954` | 重建 Sv39 早期页表 | 用 2 MiB leaf 覆盖 identity/linear 区，避免错误的 1 GiB leaf 布局 |
| `bed951142` | 修复 Sv39 canonical 地址 | shifted address 按 Sv39 符号扩展 |
| `cfaeb5a58` | DTB 改用稳定 linear mapping | 最终页表撤销 identity mapping 后 DTB 仍可访问 |
| `b68c338d3` | initramfs 改用稳定 linear mapping | 避免晚期解包访问 early identity pointer |
| `fe1dcfdf7`、`491b35b0f` | 在 SATP/页表切换后刷新 TLB | 不再继续使用旧 translation |
| `82abd19be`、`a0f039e50` | 补齐 boot leaf A/D 位与 Svade 行为 | 无 Svadu 硬件更新时映射仍可访问 |
| `222a4c395`、`705e1b7b3` | 提前验证 DTB CPU/内存/中断/range | 把 stale DTB 与分页故障分开分类 |
| `b48cfeea3` | BSP/AP 遵循编译期唯一 paging mode | 修复本轮 Sv39 Rust + Sv48-first assembly 的重复失配 |

## 2026-08-25 重复故障

当前 Debian/Megrez 内核以 `riscv_sv39_mode` 编译，但吸收的通用
`bsp_boot.S` 仍先尝试 Sv48。Megrez 接受 Sv48，因而 CPU 实际启用了 Sv48
启动页表；Rust 随后按 Sv39 线性映射把 DTB 物理地址 `0xf0000000` 转为
`0xffffffc0f0000000`。该地址没有映射在正在使用的 Sv48 启动页表中，最终在
读取 DTB header 时产生 load page fault。

`b48cfeea3` 删除了独立的 Sv48-first 决策。编译期唯一
`BOOT_SATP_MODE` 现在同时传给 BSP 和 AP；Rust `PagingConsts`、启动页表、
最终页表及所有 hart 使用同一模式。修复后的 Sv39 Image 已在 Megrez 真机
完成 `Enter riscv_boot`、OSTD、4 hart、组件初始化、MMC 注册和 rootfs 解包。

## Linux 的处理方式

Linux RISC-V 要求固件以 `satp=0`、MMU 关闭的状态进入内核，并分
`setup_vm()` 与 `setup_vm_final()` 两阶段建立临时和最终映射。DTB 通过
fixmap 保持在两个阶段都可访问：

- <https://docs.kernel.org/next/arch/riscv/boot.html>
- <https://docs.kernel.org/next/arch/riscv/vm-layout.html>

当前 Linux 可以在运行时从 Sv57 降到 Sv48，再降到 Sv39，但降级是完整状态
转换，而不是汇编局部 fallback：

- 命令行和 DTB `mmu-type` 先限制最高允许模式，避免某些平台探测不支持的
  SATP mode 时挂死；
- 临时 identity page table 写入 `satp`，读回确认硬件是否接受；
- 降级同步更新 `satp_mode`、`pgtable_l5_enabled`、
  `pgtable_l4_enabled`、`kernel_map.page_offset` 和运行时 `VA_BITS`；
- `head.S` 读取已经决定的全局 `satp_mode`，不再自行选择另一个模式；
- 每次加载临时或最终 SATP 都配套 `sfence.vma`。

对应实现：

- <https://github.com/torvalds/linux/blob/master/arch/riscv/mm/init.c>
- <https://github.com/torvalds/linux/blob/master/arch/riscv/kernel/head.S>
- <https://github.com/torvalds/linux/blob/master/arch/riscv/include/asm/pgtable.h>

Asterinas 当前没有 Linux 这套完整的运行时动态 layout，因此采用编译期单模式
更安全。除非将页表层级、VA layout、direct map 和地址转换全部改造成由同一
运行时 `PagingMode` 驱动，否则不得恢复独立的汇编 fallback。

## 防复发约束

1. 每个 RISC-V Image 必须记录并打印 compiled/selected paging mode。
2. BSP、所有 AP、Rust `PagingConsts` 和最终页表只允许一个 mode 来源。
3. Sv39/Sv48 产物必须使用不同文件名和证据身份，不得复用模糊的 `Image`。
4. QEMU gate 同时约束 CPU `sv48` 能力、DTB `mmu-type`、SMP、内存与内核 feature。
5. Megrez Debian 主路径当前固定 Sv39；默认 Sv48 作为独立历史/回归路径保留。
6. DTB/initramfs 等跨页表阶段对象必须使用稳定 linear/fixmap 地址。
7. 每次 SATP 或根页表切换都必须显式刷新 TLB。
8. `Starting kernel ...` 只证明 U-Boot 完成跳转；只有分页 marker、
   `Enter riscv_boot` 和后续里程碑才能定位分页边界。
