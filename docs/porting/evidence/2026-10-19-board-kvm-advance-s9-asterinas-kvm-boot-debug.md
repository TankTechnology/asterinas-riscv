# S9：Asterinas-under-KVM 打点定位（进行中，宿主事件中断）

**日期**：2026-10-19（宿主 pku4090 时钟）
**范围**：隔离 worktree + 独立容器 + 打点镜像，全程未改主工作区、未改板子 boot、未重启板子；板子 KVM 保持加载。
**状态**：**有实质进展但未完成**——宿主 pku4090 在构建/测试期间意外重启，隔离调试树与第二轮打点镜像丢失；板子上仍保留第一轮打点镜像，可继续。

---

## 0. 结论（先给答案）

1. **KVM 下 Asterinas 首次被证实能进入 Rust 引导阶段**（此前无任何输出）：
   打点镜像在 KVM 客人里打印 `[DBG] A riscv_boot hart=0x0 dtb=0xbfe00000`。
2. 故障收窄到 **`riscv_boot` 内部、`start_kernel` 之前**（marker A 之后、后续阶段 marker 未出现），现象从"取指 guest-page fault"变为 **store guest-page fault**：
   - `SCAUSE=0x17`（store guest-page fault）、`STVAL=0xffffffff7fffffc0`、`HTVAL=0x1ffffff0`（→ GPA `0x7fffffc0`，在 DRAM（0x80000000）之下）
   - `SEPC=0xffffffff80c97922`；**`sp=0xffffffff7fffffa0`**——栈指针落在内核 VMA（0xffffffff80200000）**之下约 2.06 KiB**、位于未映射区间
3. 结合 boot 静态页表：`0xffffffff7fffffc0` 命中 idx509 的 1 GiB 叶子（映射 PA 0x40000000–0x80000000，非 RAM）→ G-stage 失败。**即：故障现场是一次使用"错误栈指针/错误指针"的写操作**，发生在 FDT 解析附近的早期引导代码。
4. 已准备好更细打点（每阶段 + sp），但因宿主重启中断，尚未跑出结果。

---

## 1. 隔离构建（可复现方法）

主工作区有**未提交的 futex 调试改动**，单独 `git archive HEAD` 构建会失败（`ostd::task::cpu_local_base_for_diagnosis` 等符号不一致）。因此采用：

```bash
# 在 pku4090 上（不修改主工作区）
DST=/mnt/mydisk/asterinas-kvm-dbg2
rsync -a --exclude=.git --exclude=target --exclude='target-*' --exclude=.worktrees \
      --exclude='osdk/target' --exclude='test/initramfs/build' --exclude=__pycache__ --exclude='*.pyc' \
      /mnt/shared/xaj/Program/asterinas/ $DST/
# 应用打点 patch（见 §2）
# 独立容器（同镜像、独立容器名；共享 cargo/nix 缓存卷；不碰主线容器）
tools/docker/run_dev_container.sh --image asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached -- \
  bash -lc "cd /root/asterinas && make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode"
```

构建成功：`target/osdk/aster-kernel-osdk-bin.Image`（16,269,424 B）
**sha256 `13a5ceda0399e53f362c6eb6a1e8fe3fd07915af2e78a17e9adbe46d76a5880d`**（板上仍保留在 `/tmp/asterinas-kvm-smoke/dbg.Image`）。

---

## 2. 打点内容（patch 已保存在宿主机本地备份）

- `ostd/src/lib.rs`：新增 `dbg_putc/dbg_str/dbg_hex/dbg_sp`（直接写 QEMU virt UART `0x1000_0000`，因为 KVM 下 early_println 的串口在 `arch::serial::init` 前不可用）
- `ostd/src/arch/riscv/boot/mod.rs`：`riscv_boot` 入口打印 hart/dtb（marker A）、FDT 解析后（F）、validate 前后（V/W）、`start_kernel` 前（B）
- `ostd/src/lib.rs::init`：C/D/E/F/G/H/I/J/K/L 阶段 marker
- `ostd/src/arch/riscv/mm/mod.rs::activate_page_table`：打印 root paddr、old/new satp，并在引导表仍生效时 dump root[0,2,256,508,509,510,511]
- `ostd/src/mm/kspace/mod.rs`：内核页表构建完成时打印 root paddr

---

## 3. 原始输出（KVM，第一轮打点镜像）

```
$ qemu-system-riscv64 -machine virt -accel kvm -cpu host -bios none -m 2G -smp 2 -nographic -nic none -no-reboot \
    -kernel /tmp/out/stub2.bin \
    -device loader,file=/tmp/asterinas-kvm-smoke/dbg.Image,addr=0x80200000,force-raw=on \
    -initrd /tmp/asterinas-kvm-smoke/initramfs.cpio \
    -append "console=ttyS0 loglevel=info init=/init -- --root-init=probe"

[DBG] A riscv_boot hart=0x0000000000000000 dtb=0x00000000bfe00000
error: kvm run failed Operation not supported
 pc       ffffffff80c97922
 mhartid  0000000000000000
 ...
 x1/ra    ffffffff80b67d88 x2/sp    ffffffff7fffffa0 x3/gp    ffffffff81163120
 x5/t0    0000000000000038 x6/t1    0000000000001252 x7/t2    ffffffffd00dfeed
 x10/a0   ffffffff81163540 x11/a1   0000000000000001
 x16/a6   ffffffc0bfe0052c x17/a7   0000000000000040
host dmesg:
kvm [..]: VCPU exit error -95
kvm [..]: SEPC=0xffffffff80c97922 SSTATUS=0x200004120 HSTATUS=0x2002001c0
kvm [..]: SCAUSE=0x17 STVAL=0xffffffff7fffffc0 HTVAL=0x1ffffff0 HTINST=0x0
```

分析：
- `a1=1`、`t2=0xffffffffd00dfeed`、`a6` 指向 FDT 的 direct-map 地址 → 已在 FDT 解析路径；
- `HTVAL=0x1ffffff0` → GPA `0x7fffffc0`，VS-stage 用 boot 表 idx509（PA 0x40000000–0x80000000，非 RAM）翻译得到 → G-stage 失败；
- `sp=0xffffffff7fffffa0` 是错误值（内核 boot stack 应在 `0xffffffff80204000–0xffffffff80244000`）；`pc` 在 `0xffffffff80c97922`（内核文本），`ra=0xffffffff80b67d88`；
- **下一步需要 marker F/V/W + sp 轨迹**（patch 已写，构建因宿主重启丢失）。

## 4. 已排除（本轮之前/本轮）

- KVM 管道、模块、QEMU 参数、Image 加载、静态引导页表、Sv39 build、sv48/smp/mem、ISA/DT（svpbmt）差异（S5/S7/S7b/S7c 已证）
- gdbstub：本厂 KVM 报 `gdbstub: current accelerator doesn't support guest debugging`（不可用）

## 5. 宿主事件（重要，非本任务操作）

- **pku4090 在 2026-10-19 15:0x 重启**（`uptime` 1 min；所有 docker 容器 Exited(255)；/tmp 被清空）。
- `/mnt/mydisk` 当前**未挂载**（`/dev/sda` 3.6T 仍在，但挂载需 root；`sudo` 需密码，无法自行恢复）→ **隔离调试树 `/mnt/mydisk/asterinas-kvm-dbg2` 与第二轮构建产物丢失**。
- `/mnt/shared`（含主工作区 `asterinas`）完好；`docker` 服务 active、镜像仍在。
- **板子未受影响**：`boot_id 9c777a00-…` 未变、kvm 模块仍加载、`/dev/kvm` 在、无 QEMU 残留、`/tmp/asterinas-kvm-smoke/dbg.Image` 仍在（tmpfs）。
- 影响：主工作区 futex 调试会话的容器全部退出（需其自身/Lead 恢复）；本任务隔离树需重建。

## 6. 恢复与继续（预计 10–15 分钟）

1. 在 `/mnt/shared/xaj/Program/`（或挂回 `/mnt/mydisk`）重建源码副本（rsync 同 §1，排除大目录）；
2. 应用本机备份的 `patch_dbg.py` + `patch_dbg2.py`（新增 SP/阶段 marker）；
3. 用同一独立容器重建（约 4–5 分钟，缓存已热）；
4. KVM 启动新镜像，读取 marker `F/V/W` 与各阶段 `sp`，定位 sp 何时/被哪段代码改坏（候选：FDT 解析、`paddr_to_vaddr` 计算、early info 解析路径）；
5. 找到后最小修复（debug 分支），再用 KVM 验证能否进入 `start_kernel` 并出串口日志。

## 7. 产物/证据

| 位置 | 内容 |
| --- | --- |
| 板子 `/tmp/asterinas-kvm-smoke/dbg.Image` | 第一轮打点镜像（sha `13a5ceda…`） |
| pku4090 `/tmp/board-kvm-advance/dbgboot.sh`、板侧 `/tmp/out/stub2.bin` | 复现脚本/跳转 stub（板 tmpfs 仍在） |
| 本机 `/tmp/patch_dbg.py`、`/tmp/patch_dbg2.py` | 打点 patch（隔离树丢失后用于重建） |
| 本文件 | 证据 |
