# S11：PR-ready 最小修复 + Asterinas-under-KVM 收益首测（S8 部分）

**日期**：2026-10-19（宿主 pku4090 时钟）
**范围**：隔离树内整理 PR patch；干净 PR-check 构建与验证；未改主工作区、未改板子 boot、未重启板子；KVM 保持加载。
**承接**：S10（根因=QEMU KVM guest DTB 缺 `mmu-type`；修复 A/B 验证通过）。

---

## 0. 结论

1. **PR-ready 最小提交已就绪**：单文件 `git format-patch`，4 文件 +5/−18，`git apply --check` 对主工作区 HEAD **通过**（未改动主工作区）。
2. **干净验证通过**：从主工作区快照（不含任何调试打点代码）新建副本 → **只应用该 patch** → 构建 → KVM 下 Stage1 probe **3/3 PASS**（`ASTERINAS_PROBE_PASS detail=boot-ok`、`DONE status=pass`、`REBOOT_READY`）。
3. **S8 部分收益数据**：Asterinas-under-KVM 启动到 probe READY 的宿主 wall 中位 **2.322 s**（含 ssh+QEMU 启动）、`complete` 2.333 s；guest 侧 `stdio-ready` 0.338 s。与 S6 的 Linux guest KVM 基线（0.876 s 到 init、相对板 TCG 6.2×、内核侧 13×）并列给出；**Asterinas 的 TCG 对照缺失**（当前 main 镜像 TCG+OpenSBI 无输出，见 s10 §11，已转主线），不做不严谨对比。

---

## 1. PR-ready 提交

| 项 | 值 |
| --- | --- |
| patch 文件 | `/mnt/mydisk/asterinas-kvm-fix/0001-riscv-accept-CPU-nodes-without-mmu-type-QEMU-KVM-gue.patch` |
| patch sha256 | `56e886d19c9c49cd790dbca565a350ccec55b348ca5573fc04943ca32293894d` |
| 提交对象 | 4 文件：`ostd/src/arch/riscv/boot/mod.rs`、`ostd/src/arch/riscv/boot/smp.rs`、`ostd/src/arch/riscv/cpu/extension.rs`、`ostd/src/arch/riscv/mm/mod.rs`（+5/−18） |
| apply 校验 | 在主工作区 `/mnt/shared/xaj/Program/asterinas` 上 `git apply --check` → **OK**（只读检查，无改动） |
| 建议拆成两个提交 | ①`riscv: accept CPU nodes without mmu-type` ②`riscv: fall back to 64B cbom block size`（当前合并为一个 commit 便于交付；正文已说明两处动机） |

commit message（见 patch 头部）要点：QEMU KVM 模式生成的 guest DT 不含 CPU `mmu-type`；旧逻辑把它当作"非 MMU-capable CPU"跳过 → `/cpus` 里 CPU 数为 0 → 串口初始化前 panic（静默）；RV64 S-mode CPU 必有 MMU，因此接受缺省；同时为缺失的 `riscv,cbom-block-size` 回退 64 B。附 3/3 KVM 验证证据与 s10 sha256。

### 1.1 干净 PR-check 验证（关键）

```bash
# 主工作区快照（不含本任务任何调试打点）→ 只应用 patch → 独立容器构建
rsync -a --exclude=.git --exclude=target ... /mnt/shared/xaj/Program/asterinas/ /mnt/mydisk/asterinas-kvm-prcheck/
git apply /mnt/mydisk/asterinas-kvm-fix/0001-*.patch
tools/docker/run_dev_container.sh --image asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached -- \
  bash -lc "cd /root/asterinas && make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode"
```

产物 `prcheck.Image`：sha256 `85fcd8ac249f48c05fe978ca59f717761a6d7ab8fc8d869a3d678a3679ce92e3`（16,261,320 B）。

KVM（默认 guest DTB，**不加 `-dtb`**）Stage1 probe 3/3：

| run | ready_at (wall) | complete_at (wall) | PASS | DONE | REBOOT_READY |
| --- | ---: | ---: | ---: | --- | --- |
| 1 | 2.319 s | 2.331 s | 1 | status=pass | ✓ |
| 2 | 2.334 s | 2.346 s | 1 | status=pass | ✓ |
| 3 | 2.322 s | 2.333 s | 1 | status=pass | ✓ |

日志原文：

```
ASTERINAS_PROBE_READY v=1 pid=1
ASTERINAS_PROBE_PASS v=1 nonce=<…> seq=0 name=boot detail=boot-ok
ASTERINAS_PROBE_DONE v=1 nonce=<…> count=1 status=pass
ASTERINAS_PROBE_REBOOT_READY v=1 nonce=<…>
```

---

## 2. S8 部分：Asterinas-under-KVM 启动时间（与 Linux guest 基线并列）

### 2.1 口径

- `ready_at`/`complete_at`：**宿主 wall**（pku4090 `time.monotonic()` 包裹 ssh + QEMU 启动 + 客户启动），含固定开销约 1.3–1.5 s；
- guest 内核 printk 时间戳：更纯的"客户内部"口径（不含 ssh/QEMU 启动）；
- 同一块板、同一 QEMU 9.2.0、同一 initramfs、同一 `-m 2G -smp 2`、`-accel kvm`；未打点镜像（PR-check 构建）。

### 2.2 结果

| 场景 | 指标 | KVM（板） | TCG（板） | 备注 |
| --- | --- | ---: | ---: | --- |
| **Asterinas → Stage1 probe READY** | 宿主 wall 中位（3 次） | **2.322 s**；complete 2.333 s | **缺失** | guest `stdio-ready` = 0.338 s；组件初始化完成 0.293 s |
| **Linux 6.6.87 → init**（S6） | 到 init 标记中位（3 次） | 0.836 s（wall 0.876 s） | 5.379 s（wall 5.450 s） | **6.2× wall / 6.4× 到标记；内核侧 13.0×** |
| 纯 CPU 依赖链（S5 微基准） | 400M 次 ALU 循环 | ≈0.45 s（≈native 0.445 s） | 2.05 s | **KVM/TCG ≈4.5×** |

### 2.3 缺失与边界（必须随数字引用）

- **Asterinas 的 TCG 对照没有拿到**：同一 main 镜像在 TCG+OpenSBI 下无内核输出（见 s10 §11 的最小复现命令；环境本身正常——Sep-30 ktest 镜像可跑）。因此**不给**"Asterinas KVM vs TCG"倍率，避免不严谨对比。
- 上述 Asterinas 数字是"启动到 Stage1 probe READY"，**不是** Xorg/Firefox 整机场景；整机倍率需后续按 S8 方案测。
- KVM 只加速指令执行；virtio/MMIO/中断/定时器仍由 QEMU 模拟，整机收益必然低于纯 CPU 段。

---

## 3. 经验与交接

- **QEMU KVM 模式的 guest DTB ≠ TCG 的 guest DTB**（KVM 缺 CPU `mmu-type`、缺 cbo block size、ISA 扩展集更小、设备节点更少）——这是同类"KVM 下内核静默挂死"问题的第一条排查入口。对比方法见 s10 §10。
- **TCG 无输出问题**（当前 main 镜像 + `-bios default`）：最小复现命令与现象已写入 s10 §11，转主线会话（可能与 Image/U-Boot 启动交接同源）。

---

## 4. 产物与收据

| 对象 | 位置 / 值 |
| --- | --- |
| PR patch | `/mnt/mydisk/asterinas-kvm-fix/0001-*.patch`（sha256 `56e886d1…`）；全文见 §5 |
| PR-check 干净树 | pku4090:`/mnt/mydisk/asterinas-kvm-prcheck`（主工作区快照 + 仅该 patch） |
| PR-check Image | sha256 `85fcd8ac…`，板上 `/tmp/asterinas-kvm-smoke/prcheck.Image` |
| probe 收据 | pku4090:`/tmp/board-kvm-advance/receipts/s11-prcheck-r{1,2,3}/` |
| 隔离完整树（含打点/修复历史） | pku4090:`/mnt/mydisk/asterinas-kvm-dbg3` |
| s10 | `docs/porting/evidence/2026-10-19-board-kvm-advance-s10-asterinas-kvm-boot-fixed.md`（sha256 `cf43c6d0…`，含经验与 TCG 复现） |

## 5. Patch 全文

```diff
From fd5e5321ca5cf97fc724962c4c72ee27a9e05995 Mon Sep 17 00:00:00 2001
From: KVM debug <dev@asterinas>
Date: Mon, 19 Oct 2026 15:30:53 +0800
Subject: [PATCH] riscv: accept CPU nodes without mmu-type (QEMU KVM guest DT)

QEMU's KVM accelerator generates a guest device tree that omits the
"mmu-type" property on each CPU node, while the TCG accelerator sets
'mmu-type = "riscv,sv39"'. The RISC-V boot code treated a missing
'mmu-type' as "not an MMU-capable (application) CPU" and skipped such
nodes, so on a KVM guest the DT validation panicked with
"[DTB] '/cpus' describes no MMU-capable CPU" before the serial console
was initialized (silent failure), and the kernel never booted.

RV64 S-mode CPUs always have an MMU, so treat CPU nodes without
'mmu-type' as application CPUs. Also fall back to a 64-byte cache-block
size when 'riscv,cbom-block-size' is missing, which is the case for the
QEMU KVM guest DT.

Validated on a Milk-V Megrez (EIC7700) board with QEMU 9.2.0
`-accel kvm -cpu host`: Asterinas boots to the Stage1 probe userland and
passes the boot probe 3/3 runs
(ASTERINAS_PROBE_PASS ... detail=boot-ok, DONE status=pass,
REBOOT_READY). Evidence:
docs/porting/evidence/2026-10-19-board-kvm-advance-s10-asterinas-kvm-boot-fixed.md
(sha256 2a3980ec3dab73a1a7a936d71f5bdfa95c0b925d0e661898d3d9f9c13cf0cf59).
---
 ostd/src/arch/riscv/boot/mod.rs      | 10 ----------
 ostd/src/arch/riscv/boot/smp.rs      |  1 -
 ostd/src/arch/riscv/cpu/extension.rs |  3 ---
 ostd/src/arch/riscv/mm/mod.rs        |  9 +++++----
 4 files changed, 5 insertions(+), 18 deletions(-)

diff --git a/ostd/src/arch/riscv/boot/mod.rs b/ostd/src/arch/riscv/boot/mod.rs
index 04465cf..0830a24 100644
--- a/ostd/src/arch/riscv/boot/mod.rs
+++ b/ostd/src/arch/riscv/boot/mod.rs
@@ -175,11 +175,6 @@ fn validate_cpu_nodes(fdt: &Fdt, bootstrap_hart_id: usize) {
         if cpu.property("device_type").and_then(|prop| prop.as_str()) != Some("cpu") {
             continue;
         }
-        if cpu.property("mmu-type").is_none() {
-            // Management or monitor harts without an MMU are skipped by the
-            // kernel as well, so they are not checked here.
-            continue;
-        }

         let Some(hart_id) = cpu.property("reg").and_then(|reg| reg.as_usize()) else {
             panic!(
@@ -193,7 +188,6 @@ fn validate_cpu_nodes(fdt: &Fdt, bootstrap_hart_id: usize) {
             .filter(|node| node.name.split('@').next() == Some("cpu"))
             .filter(|node| {
                 node.property("device_type").and_then(|prop| prop.as_str()) == Some("cpu")
-                    && node.property("mmu-type").is_some()
             })
             .filter_map(|node| node.property("reg").and_then(|reg| reg.as_usize()))
             .filter(|candidate| *candidate == hart_id)
@@ -348,9 +342,6 @@ fn validate_interrupt_controllers(fdt: &Fdt) {
         if cpu.property("device_type").and_then(|prop| prop.as_str()) != Some("cpu") {
             continue;
         }
-        if cpu.property("mmu-type").is_none() {
-            continue;
-        }

         let mut controllers = cpu.children().filter(|child| {
             child
@@ -385,7 +376,6 @@ fn validate_interrupt_controllers(fdt: &Fdt) {
             .filter(|node| node.name.split('@').next() == Some("cpu"))
             .filter(|node| {
                 node.property("device_type").and_then(|prop| prop.as_str()) == Some("cpu")
-                    && node.property("mmu-type").is_some()
             })
             .flat_map(|node| node.children())
             .filter(|child| {
diff --git a/ostd/src/arch/riscv/boot/smp.rs b/ostd/src/arch/riscv/boot/smp.rs
index 93b0008..ba461f7 100644
--- a/ostd/src/arch/riscv/boot/smp.rs
+++ b/ostd/src/arch/riscv/boot/smp.rs
@@ -77,7 +77,6 @@ fn for_each_hart_id(mut f: impl FnMut(u32)) {
             // harts. Here we simply skip those harts without MMU, which is
             // supposed to work in most cases.
             if device_type.as_str() == Some("cpu")
-                && cpu_node.property("mmu-type").is_some()
                 && let Some(reg) = cpu_node.property("reg")
             {
                 f(reg.as_usize().unwrap() as u32);
diff --git a/ostd/src/arch/riscv/cpu/extension.rs b/ostd/src/arch/riscv/cpu/extension.rs
index c6f9387..e7b7730 100644
--- a/ostd/src/arch/riscv/cpu/extension.rs
+++ b/ostd/src/arch/riscv/cpu/extension.rs
@@ -18,9 +18,6 @@ pub(in crate::arch) fn init() {
         // FIXME: We should find a robust method to identify the management
         // harts. Here we simply skip those harts without MMU, which is supposed
         // to work in most cases.
-        if cpu.property("mmu-type").is_none() {
-            continue;
-        }

         cpu_count += 1;

diff --git a/ostd/src/arch/riscv/mm/mod.rs b/ostd/src/arch/riscv/mm/mod.rs
index 0b1a7c5..a805c49 100644
--- a/ostd/src/arch/riscv/mm/mod.rs
+++ b/ostd/src/arch/riscv/mm/mod.rs
@@ -190,16 +190,17 @@ pub(crate) unsafe fn sync_dma_range<D: DmaDirection>(range: Range<Vaddr>) {

     static CMO_MANAGEMENT_BLOCK_SIZE: Once<usize> = Once::new();
     let cmo_management_block_size = *CMO_MANAGEMENT_BLOCK_SIZE.call_once(|| {
+        // KVM guests may omit `mmu-type`/`riscv,cbom-block-size` from CPU
+        // nodes; fall back to the default cache-block size (64 B) in that case.
         DEVICE_TREE
             .get()
             .unwrap()
             .cpus()
-            .find(|cpu| cpu.property("mmu-type").is_some())
+            .find(|_| true)
             .expect("Failed to find an application CPU node in device tree")
             .property("riscv,cbom-block-size")
-            .expect("Failed to find `riscv,cbom-block-size` property of the CPU node")
-            .as_usize()
-            .expect("Failed to parse `riscv,cbom-block-size` property of the CPU node")
+            .and_then(|p| p.as_usize())
+            .unwrap_or(64)
     });

     for addr in range.step_by(cmo_management_block_size) {
--
2.34.1

```

## 6. 板子状态

`boot_id 9c777a00-…` 未变（全程未重启）、kvm 模块仍加载、`/dev/kvm` 在、无 QEMU 残留、`/boot/extlinux/*` 未改、未持久化；板上仅 `/tmp` 临时镜像与收据（tmpfs）。
