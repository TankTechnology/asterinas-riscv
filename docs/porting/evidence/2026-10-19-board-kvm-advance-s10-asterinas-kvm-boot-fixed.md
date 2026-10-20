# S10：Asterinas-under-KVM 已跑通 —— 根因、修复与验证

**日期**：2026-10-19（宿主 pku4090 时钟）
**范围**：隔离 worktree + 独立容器 + 打点镜像；未改主工作区、未改板子 boot、未重启板子；板子 KVM 保持加载。
**结论**：**Asterinas 已在板载 KVM 客人中完整启动到用户态**，并通过 Stage1 probe 冒烟（`ASTERINAS_PROBE_PASS ... detail=boot-ok`、`DONE status=pass`、`REBOOT_READY`）。根因与两个修复均见下。

---

## 0. 一句话根因

QEMU 的 KVM 模式生成 guest DTB 时**不给 CPU 节点写 `mmu-type` 属性**（TCG 模式会写）；Asterinas 的 RISC-V 引导校验把"缺少 `mmu-type`"当成"不是 MMU-capable CPU"而跳过，最终 `panic!("[DTB] '/cpus' describes no MMU-capable CPU")`；且该 panic 发生在串口初始化之前，panic 打印被静默，表现为"KVM 下无输出/随即挂死"。

---

## 1. 证据链（打点镜像逐步收窄）

在 `ostd/src/arch/riscv/boot/` 与 `ostd/src/lib.rs::init` 加直接 UART 打点（KVM 下 `early_println` 在 `arch::serial::init` 前不可用，故直接写 UART；切换 satp 后改用 direct-map 地址 `0xffffffc010000000`），得到：

```
[DBG] A riscv_boot hart=0x0 dtb=0xbfe00000
[DBG] F fdt_parsed
[DBG] SP fdt sp=0xffffffff80243840          ← 正常 boot stack
[DBG] V validate begin
[DBG] SP pre_validate sp=0xffffffff80243840
（随后崩溃：SCAUSE=0x17 store guest-page fault,
  STVAL=0xffffffff7ffffbb0, SEPC=0xffffffff8086a178,
  ra=aster_kernel::thread::oops::panic_handler）
```

- 故障发生在 `validate_device_tree` 内、`validate_cpu_nodes` 阶段；
- 现场 `sp=0xffffffff7ffffbb0`（远低于内核 VMA），`pc` 位于 `core::sync::atomic::*`、`ra` 指向 `panic_handler` → **panic 处理路径本身崩掉**（串口未初始化，panic 信息丢失）；
- 用 `dtc` 对比 KVM/TCG guest DTB：**KVM 的 cpu 节点无 `mmu-type`；TCG 有 `mmu-type = "riscv,sv39"`**；
- 代码中 `validate_cpu_nodes` / `validate_interrupt_controllers` / `extension.rs` / `smp.rs` 均以 `mmu-type.is_some()` 过滤 CPU → KVM DT 下 CPU 数为 0 → panic。

---

## 2. 修复 A（无需改内核）：给 guest DTB 补 `mmu-type`

对 `-machine virt,dumpdtb=` 导出的 KVM DTB 反编译，在每个 cpu 节点插入：

```dts
mmu-type = "riscv,sv39";
```

用 `dtc` 重编为 `/tmp/kvm-mmu.dtb`（4,590 B，sha256 `11bf5af9a10fa1127efbeb4df3d4eaeec25ef65b27ad0900533d549a20a4c2a3`），启动命令加 `-dtb /tmp/kvm-mmu.dtb`。

**验证（未打点的干净镜像 `clean.Image`，sha256 `245d54df…`）**：KVM 下完整启动，串口日志到：

```
[     0.148] INFO: uart: Registered NS16550A as a console
[     0.293] INFO: ASTERINAS_USERMODE_STARTUP step=process-components-done
[     0.330] INFO: ASTERINAS_USERMODE_STARTUP step=stdio-ready
DEBIAN_STAGE1_PROGRESS step=start mode=probe
ASTERINAS_PROBE_READY v=1 pid=1
```

即 **内核启动 + Stage1 用户态启动成功**。

## 3. 修复 B（推荐上游）：内核放宽 `mmu-type` 要求

在 `ostd` 中把"缺 `mmu-type` 即跳过"的假设改为"RV64 S-mode CPU 均视为 MMU-capable"，并为缺失 `riscv,cbom-block-size` 提供 64 B 回退。改动共 5 处（diff 见 §6），编译出 `fixed.Image`（sha256 `fc854ea6…`）。

**验证（QEMU KVM 默认 DTB，不加任何 `-dtb`）**：Stage1 probe 3/3 通过：

| run | ready_at(wall) | complete_at(wall) | PASS | DONE | REBOOT_READY |
| --- | ---: | ---: | ---: | --- | --- |
| 1 | 2.342 s | 2.354 s | 1 | status=pass | ✓ |
| 2 | 2.344 s | 2.357 s | 1 | status=pass | ✓ |
| 3 | 2.392 s | 2.404 s | 1 | status=pass | ✓ |

日志原文：

```
ASTERINAS_PROBE_READY v=1 pid=1
ASTERINAS_PROBE_PASS v=1 nonce=<…> seq=0 name=boot detail=boot-ok
ASTERINAS_PROBE_DONE v=1 nonce=<…> count=1 status=pass
ASTERINAS_PROBE_REBOOT_READY v=1 nonce=<…>
```

> `ready_at` 为宿主 wall（含 ssh+QEMU 启动约 1.3–1.5 s）；guest 侧时间戳：组件初始化完成 ~0.29 s、`stdio-ready` 0.338 s、probe PASS 紧随其后。

**推荐**：修复 B 比"要求用户改 DTB"更实际（KVM guest DT 由 QEMU 生成；且 RV64 S-mode 有 MMU 是合理假设）。修复 A 保留为无改内核的即时 workaround。

---

## 4. TCG 对照（未取得，单独记录）

同一 `clean.Image` 在 **TCG + OpenSBI**（`-bios default -kernel clean.Image`）下：OpenSBI 正常启动并打印 `Domain0 Next Address 0x80200000`，但**内核无任何输出**（40 s 内）。环境本身正常（Sep-30 的 ktest 镜像在同样 TCG 配置下可跑，S5 已记录 31.4 s 跑完 974 个 ktest），故这是当前 main 镜像在 TCG+OpenSBI 路径下的独立问题，**与 KVM 修复无关**，建议单开子任务。
因此本轮没有"Asterinas KVM vs TCG"的同输入计时；KVM 侧结果可与 S6（Linux guest TCG vs KVM）和 9/22 的板 TCG 一次性数据（102.774 s，非同镜像、仅参考）并列说明。

---

## 5. 复现命令（KVM，修复 B）

```bash
# 板上（QEMU 9.2，/dev/kvm 已由 S5 的 kvm 模块提供）
qemu-system-riscv64 -machine virt -accel kvm -cpu host -bios none \
  -m 2G -smp 2 -nographic -nic none -no-reboot \
  -kernel /tmp/out/stub2.bin \
  -device loader,file=/tmp/asterinas-kvm-smoke/fixed.Image,addr=0x80200000,force-raw=on \
  -initrd /tmp/asterinas-kvm-smoke/initramfs.cpio \
  -append "console=ttyS0 loglevel=info init=/init -- --root-init=probe"
# guest READY 后在串口发送：
# ASTERINAS_PROBE_RUN v=1 nonce=<32hex> probes=boot shell=0
```

（`stub2.bin` 仅 `li t0,0x80200000; jr t0`；原因：KVM 直载入口是 0x80000000，Asterinas Image 链接在 0x80200000。）

---

## 6. 修复 diff（相对隔离树 `d1d5975`，含 cbo 回退）

```diff
diff --git a/ostd/src/arch/riscv/boot/mod.rs b/ostd/src/arch/riscv/boot/mod.rs
@@ validate_cpu_nodes
-        if cpu.property("mmu-type").is_none() {
-            // Management or monitor harts without an MMU are skipped by the
-            // kernel as well, so they are not checked here.
-            continue;
-        }
@@ filter
-                    && node.property("mmu-type").is_some()
@@ validate_interrupt_controllers
-        if cpu.property("mmu-type").is_none() {
-            continue;
-        }
@@ filter
-                    && node.property("mmu-type").is_some()
diff --git a/ostd/src/arch/riscv/boot/smp.rs
-                && cpu_node.property("mmu-type").is_some()
diff --git a/ostd/src/arch/riscv/cpu/extension.rs
-        if cpu.property("mmu-type").is_none() {
-            continue;
-        }
diff --git a/ostd/src/arch/riscv/mm/mod.rs
-            .find(|cpu| cpu.property("mmu-type").is_some())
+            .find(|_| true)
             .property("riscv,cbom-block-size")
-            .expect("Failed to find `riscv,cbom-block-size` property of the CPU node")
-            .as_usize()
-            .expect("Failed to parse `riscv,cbom-block-size` property of the CPU node")
+            .and_then(|p| p.as_usize())
+            .unwrap_or(64)
```

完整 diff（94 行）在 pku4090:`/mnt/mydisk/asterinas-kvm-dbg3`（`git diff HEAD~2 HEAD`）。

> 注意：该树基于**主工作区当前 WIP 快照**（主工作区 HEAD 单独构建不一致，原因是未提交的 futex 调试改动）；本 diff 触及的 4 个文件与 futex 改动无关，预计可干净 rebase 到 main HEAD。上游化时建议拆成两个提交：`riscv: accept CPU nodes without mmu-type` 与 `riscv: fall back to 64B cbom block size`。

---

## 7. 产物与哈希

| 对象 | 值 |
| --- | --- |
| `clean.Image`（未打点，repo 当前构建） | sha256 `245d54df230f0d8878411d1efd982d5e4189d469f6172379c6c246f56c9fba1e`（16,261,864 B） |
| `fixed.Image`（修复 B 构建） | sha256 `fc854ea68e6ff672b8da7f8fd8fa9c2a558722d0965cf14ab1d94d37c78334b1`（16,274,176 B） |
| 打点调试镜像（用于定位） | sha256 `3db52010…`（15,? B，见 receipts） |
| `kvm-mmu.dtb`（修复 A 的 DTB） | sha256 `11bf5af9a10fa1127efbeb4df3d4eaeec25ef65b27ad0900533d549a20a4c2a3` |
| 隔离树 | pku4090:`/mnt/mydisk/asterinas-kvm-dbg3`（含全部打点与修复 commit） |
| probe 收据 | pku4090:`/tmp/board-kvm-advance/receipts/s10-kvm-fixed{,-r1,-r2}/`、`s10-kvm/`、`s10-tcg*/` |
| 完整 boot log | pku4090:`/tmp/clean-boot.log`、`/tmp/board-kvm-advance/receipts/*/probe-*.log` |

板子临时产物：`/tmp/asterinas-kvm-smoke/{clean,fixed,dbg,dbg3,dbg4}.Image`、`/tmp/kvm-mmu.dtb`、`/tmp/out/stub2.bin`（均 tmpfs，非持久化）。

## 8. 板子状态

`boot_id 9c777a00-…` 未变（全程未重启）、kvm 模块仍加载、`/dev/kvm` 在、无 QEMU 残留、`/boot/extlinux/*` 未改、未持久化。

## 9. 下一步建议

1. 把修复 B 以最小 PR 形式上游化（两个提交，附 KVM 启动日志与 probe 收据）；
2. 可选：给 QEMU 提交"KVM 模式也为 CPU 节点生成 mmu-type"（或使用 `-dtb` 修正脚本）作为 belt-and-braces；
3. 单开子任务排查"当前 main 镜像在 TCG+OpenSBI 下无输出"的问题；
4. 在 Asterinas KVM 可稳定跑通的基础上，按 S8 方案测量整机收益（boot / Xorg / Firefox / futex 并发），对照 S6 的 Linux guest 与板 TCG。

---

## 10. 经验记录：QEMU KVM 模式的 guest DTB 与 TCG **不同**

同类问题的第一条排查入口：**先对比两种加速器实际交给 guest 的 DTB**。

```bash
# 在板上分别导出（不启动 guest）
qemu-system-riscv64 -machine virt,dumpdtb=/tmp/kvm.dtb -accel kvm -cpu host -bios none -m 2G -smp 2 -display none
qemu-system-riscv64 -machine virt,dumpdtb=/tmp/tcg.dtb -accel tcg -cpu "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true" -bios default -m 2G -smp 2 -display none
dtc -I dtb -O dts /tmp/kvm.dtb > /tmp/kvm.dts; dtc -I dtb -O dts /tmp/tcg.dtb > /tmp/tcg.dts
diff -u /tmp/kvm.dts /tmp/tcg.dts
```

实测差异（2026-10-19，QEMU 9.2.0）：

| 项 | KVM guest | TCG guest |
| --- | --- | --- |
| CPU `mmu-type` | **缺失** | `riscv,sv39` |
| CPU `riscv,cbom/cbop/cboz-block-size` | **缺失** | 有（0x40） |
| CPU `riscv,isa` 扩展集 | 仅宿主旧式 ISA 串（`rv64imafdc_…_zba_zbb`，无 sstc/svpbmt/svade/zicboz 等） | 完整多字母扩展集 |
| 设备节点 | 更少（KVM 设备模型） | 更多 |

推论：Asterinas 等内核若在引导期依赖 DT 的**可选属性**（MMU 类型、CBO block size、ISA 扩展），在 KVM 模式下可能直接 panic/走不同分支；排查时不应假设"同一 `-machine virt` 的 guest DT 相同"。

## 11. TCG 无输出的最小复现（转主线会话）

现象：同一 `clean.Image` 在 TCG+OpenSBI 下 OpenSBI 正常，但内核无任何输出（40 s 内，无 `Enter riscv_boot` 等）。

```bash
# 板上
qemu-system-riscv64 -machine virt -accel tcg \
  -cpu "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true" \
  -bios default -m 2G -smp 2 -nographic -nic none -no-reboot \
  -kernel /tmp/asterinas-kvm-smoke/clean.Image \
  -initrd /tmp/asterinas-kvm-smoke/initramfs.cpio \
  -append "console=ttyS0 loglevel=info init=/init -- --root-init=probe"
# 观测：OpenSBI 打印到 "Domain0 Next Address : 0x80200000"/MEDELEG 后停止；无内核输出。
```

对照：Sep-30 的 ktest 镜像在同样 TCG 配置下可运行并跑完 974 个 ktest（S5 precheck，31.4 s），故 TCG 环境本身正常；问题在"当前 main 镜像 + TCG+OpenSBI 交接"路径，建议与主线正在查的 Image/U-Boot 启动交接问题一并处理。
