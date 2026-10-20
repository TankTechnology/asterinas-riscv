# S7c：Asterinas-under-KVM P2c 取证与 handoff（2 小时盒内）

**日期**：2026-10-19（宿主时钟）
**范围**：授权 A 之后的第二轮 2 小时盒；隔离目录/只读取证，未改主工作区、未改板子 boot、未重启。
**结论**：**未修复**。已排除若干假设并把现场收窄；给出下一步可执行的修复路径。

---

## 0. 先纠正一个前提（重要）

Lead 假设"TCG 路径里高半区 root[510] 是由 Asterinas Stage1（M-mode SBI）建好再交给内核的"——**与仓库现状不符**：

- `ostd/src/arch/riscv/boot/bsp_boot.S` 里 BSP 自己在物理地址运行，静态构建
  `sv48_boot_l4pt` / `sv39_boot_l3pt`（符号地址 `0x80201000/0x80202000/0x80203000`），写 `satp` 后跳到高半区；
- `ostd/src/arch/riscv/boot/ap_boot.S` 的 AP 页表指针 `__ap_boot_page_table_pointer` 由 BSP 填写；
- **Stage1 是 initramfs 里的用户态 init**（`/init` + `--root-init=…`），不参与内核页表构建。
- TCG 与 KVM 的内核入口相同（都是 S-mode，a0=hartid、a1=DTB），差别只在固件/宿主环境。

因此"照抄 Stage1 的建表逻辑进 stub"这条路**不成立**：内核自己会建两次表（静态引导表 + Rust 运行时表）。

## 1. 本盒子新增取证（全部只读）

### 1.1 QEMU KVM gdbstub 不可用（重要限制）

```
qemu-system-riscv64: -gdb tcp::1234: gdbstub: current accelerator doesn't support guest debugging
qemu-system-riscv64: could not start gdbserver
```

本厂 KVM（6.6.87-win2030 模块）未提供 guest 调试能力，**无法用 gdbstub 读 satp/单步**。TCG 侧 gdbstub 可用但 TCG 本身是好的，不能复现。

### 1.2 失败后 guest 内存状态（`pmemsave` 全 2 GiB 取证）

- 失败后用 QEMU monitor `pmemsave 0x80000000 0x80000000` 导出全部 guest RAM，扫描 512 KiB 页表候选。
- **静态引导页表完好**：`0x80203ff0..0x80203fff` = `0x200000CF, 0`（root[510] 仍是正确的 1 GiB 叶子）。
- **boot stack 未被写穿**：`0x80204000` 起为全 0（未发生"栈溢出覆盖页表"）。
- **全 2 GiB 内没有任何 4 KiB 页满足"root[510] = V-only 且 PPN=0"**（扫描 31/36 个命中全是内核栈页，含返回地址模式，非页表）。
  → 之前"sapt root[510] 指向 PA 0"的推断**不能由内存证据支持**，更可能是该厂 KVM 在 `-EOPNOTSUPP` 路径上打印的 `HTVAL=0xa` 不可靠（或该 trap 不是普通 G-stage fault）。

### 1.3 其它现场值

| 项 | 值 | 说明 |
| --- | --- | --- |
| `__ap_boot_page_table_pointer`（`0xffffffff81161310` → 物理 `0x81161310`） | **0** | 故障发生在启动 AP 之前（BSP 尚未填这个指针） |
| 静态表 root[510] | `0x200000CF` | 正确；若 satp 仍是引导表，该 VA 应可翻译 |
| `boot_stack_bottom/top` | `0x80204000 / 0x80244000` | 故障时 sp=`0xffffffff80204030`（栈底部附近，但栈内容全 0 → 疑点：sp 被重设/另有栈） |
| FA 地址符号化 | `core::cmp::Ord::min` 附近（用 `target/osdk/aster-kernel-osdk-bin.qemu_elf`，与 Image 同一构建 11:16） | pc==ra → 更像"合法返回地址"; 该页本应被引导表映射 |

### 1.4 结论性推理

1. 引导表完好 + 该 VA 在引导表覆盖范围内 → 若 satp 仍是引导表，不可能取指失败；
2. 因此故障时 **satp 已切到 Rust 运行时页表**，而该表在这个 VA 上映射错误/缺失；
3. 该厂 KVM 的 trap 打印（HTVAL=0xa）不足以直接定位出错表项，需要**内核侧打点**才能继续。

## 2. 下一步（按性价比，均需隔离构建/不改语义）

1. **隔离 worktree + 带打点的调试镜像**（约 40–60 min）：
   - `git worktree add /tmp/asterinas-kvm-debug <commit>`（或整目录拷贝），**不动主工作区**；
   - 在 `ostd/src/arch/riscv/boot/mod.rs::riscv_boot` 与 `mm/mod.rs::activate_page_table` 前后加最小 `early_println!`：打印 `satp`、root paddr、root[510]/root[2]/root[0] 原始值、pc/ra；
   - 用**新的 docker 容器**（同镜像，挂载隔离目录）构建 `FEATURES=riscv_sv39_mode` 镜像；
   - KVM 下启动，对比 TCG 轨迹，定位首次出现"root[510] 异常"的时刻与写它的代码。
2. 若打点发现是"运行时表尚未完成切换就跳转"类顺序问题：确认 KVM 下是否 lost interrupt / SBI 时序差异（TCG 侧 OpenSBI 的 M-mode 固件可能改变了时序）。
3. 若打点确认是映射构造错误：在隔离 worktree 里做最小修复 patch，重编 + KVM 回归（目标：看到 `INFO: Initializing frame metadata…`）。
4. **暂不采用**：修改板子/boot/重启/持久化；这些都不在本任务边界内。

## 3. 已交付的其它结论（可独立使用）

- 板载 KVM 可用性：S5（模块加载 + QEMU KVM + guest 执行）与 S6（Linux guest 启动到 init，6.2–6.4×，内核侧 13×）已足够作为"板子 KVM 可用"的决定性证据。
- Asterinas-under-KVM 的准确卡点：**运行时页表在 KVM 下对内核高半区文本映射错误**；不是模块/KVM 参数/ISA DT/静态引导表问题（均已实测排除）。

## 4. 产物

| 路径（板子） | 内容 |
| --- | --- |
| `/tmp/ram2g.bin` | 失败后 2 GiB guest RAM 快照（可删除，占 2 GiB tmpfs） |
| `/tmp/asterinas-kvm-smoke/real.Image` | 与 qemu_elf 同构建的 Asterinas 内核镜像（16,261,864 B，sha256 `245d54df…`） |
| pku4090 `/tmp/board-kvm-advance/*.py` | monitor/取证脚本（dump2g/scanroot/ptdump 等） |
| 本文件 | 取证与 handoff |

板子状态：`boot_id 9c777a00-…` 不变、无 QEMU 残留、kvm 模块仍加载、`/dev/kvm` 在、boot/extlinux 未改。

**建议**：把 P2c 收束为"隔离构建 + 打点"的后续子任务；当前以 S6 的 Linux guest 作为板载 KVM 的可用性与性能基线。
