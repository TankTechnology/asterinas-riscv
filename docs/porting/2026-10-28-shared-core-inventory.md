# x86/RISC-V 共享面盘点（P0）

日期：2026-10-28
基线：`origin/main` = `73fe2ea64`；对照 `origin/feat/drm-virtio-gpu` =
`e9216a061`

## 1. 结论

1. **arch 专属代码并不是主要浪费**。设备组件层里 `cfg(target_arch)` 的总数只有
   约 24 处（见下表），大部分组件（block/input/nvme/network/…）本身与架构无关。
2. **真正的浪费是同一设备类存在 2–3 份实现**：
   - `main:kernel/comps/`：19 个组件，约 5.3 万行；
   - `feat:kernel/comps/`：20 个，比 main 多
     `drm`(36 文件/8401 行)、`drm-virtio-gpu`(331)、`simpledrm`(278)、
     `audio-hda`(1680)、`usb-xhci`(2116)；
   - `feat:kernel/core/comps/`：14 个，是**第三份**变体，而且不是简单旧拷贝——
     `nvme`/`softirq`/`systree`/`time` 在 core 里反而更大（更新）。
3. 因此共享化的第一步不是"抽 arch 接口"，而是**把三份变体收敛成一份**，并规定
   新的设备类一律"共享 core + 薄后端"（DRM 已是样板）。

## 2. 盘点数据

行数=该 crate 下所有 `.rs` 行数之和；`cfg_arch`=含 `cfg(target_arch` 的行数。
"状态"是 `git rev-parse <ref>:<path>` 的树对象比较。

| crate | main 行数 | feat/comps 行数 | feat/core 行数 | main==feat/comps | cfg_arch(main) |
|---|---:|---:|---:|---|---:|
| block | 2123 | 2123 | 1955 | diff（bio.rs 5 行） | 0 |
| cmdline | 1531 | 1531 | 1554 | SAME | 0 |
| console | 157 | 157 | 82 | SAME | 0 |
| framebuffer | 1727 | 1711 | 1620 | diff（framebuffer.rs 2/18） | 1 |
| input | 1399 | 1399 | 1256 | SAME | 0 |
| logger | 1191 | 1191 | 162 | SAME | 1 |
| mlsdisk | 13617 | 13617 | 13656 | SAME | 0 |
| network | 998 | 998 | 167 | SAME | 0 |
| nvme | 2238 | 2238 | **2811** | SAME | 0 |
| pci | 3011 | 3011 | 2087 | SAME | 3 |
| softirq | 681 | 681 | **820** | SAME | 0 |
| systree | 1727 | 1727 | **2024** | SAME | 0 |
| time | 922 | 922 | 935 | SAME | 6 |
| virtio | 10730 | 10728 | 9205 | diff（gpu/network 设备 36/38 行） | 1 |
| dwmac | 4306 | 4302 | — | diff（lib.rs 4 行） | 3 |
| i8042 | 1330 | 1543 | — | diff（keyboard/controller +290/−76） | 1 |
| mmc | 3752 | 3743 | — | diff（9 行） | 5 |
| usb | 4619 | 4619 | — | SAME | 2 |
| uart | 3326 | 1684 | — | **大分叉**（+214/−1852） | 0 |

feat 独有：

| crate | 文件/行数 | 用途 |
|---|---:|---|
| `drm` | 36 / 8401 | 共享 DRM/KMS 核心（aster-drm） |
| `drm-virtio-gpu` | 1 / 331 | virtio-gpu 驱动插件（x86 + RISC-V 共用） |
| `simpledrm` | 1 / 278 | 固件 framebuffer 后端 |
| `audio-hda` | 7 / 1680 | Intel HDA（x86） |
| `usb-xhci` | 5 / 2116 | xHCI 主机控制器（两架构共用） |
| `device`（core 里） | 26 / 4494 | 设备模型/总线/驱动注册（main 在 `kernel/src/device`） |

### 分叉明细

- **12/19 的组件在 main 与 feat/comps 完全相同**（cmdline、console、input、
  logger、mlsdisk、network、nvme、pci、softirq、systree、time、usb）。
- 其余 7 个有 diff：block（5 行）、framebuffer（2/18）、virtio（gpu/network 设备
  36/38）、dwmac（4 行）、i8042（+290/−76）、mmc（9 行）、uart（大分叉）。
- `feat/core/comps` 对每个 crate 都与 main 不同；其中 `nvme`(+573)、
  `softirq`(+139)、`systree`(+297)、`time`(+13) 更大，说明 core 树在这些组件
  上有 main 还没有的更新——**合并必须按 crate 做三方比较，不能整棵树取一侧**。

## 3. 迁移规则

1. **唯一组件树 = `main:kernel/comps/`**。`feat/core/comps` 不再作为代码归宿；
   其中的新改动按 crate 回流（先 `nvme`、`softirq`、`systree`、`time`、`device`，
   它们有新功能），其余与 main 等价或更旧。
2. **每 crate 一个实现**，合并规则：
   - `SAME`：直接丢弃 feat 侧副本；
   - 小 diff（block/framebuffer/dwmac/mmc）：逐 hunk 判断，优先保留 main 的新语义；
   - 大分叉（uart）：以功能为准重建——main 的 `arch/{x86,riscv}` 后端 +
     feat 的 arm/loongarch 后端合并到同一 crate 的 `arch/` 目录；
   - i8042：feat 的 +290 行（键盘重复率/控制器状态机）需要核对后回流。
3. **feat 独有的设备类直接迁入** `kernel/comps/`：`drm`、`drm-virtio-gpu`、
   `simpledrm`、`usb-xhci`、`audio-hda`；`device` 的设备模型与 main 的
   `kernel/src/device` 合并（main 的注册表/devtmpfs 是现成的）。
4. **架构差异只允许出现在 `arch/` 或 `backend/` 子模块**。设备 core 里出现
   `cfg(target_arch)` 必须在评审里解释；新设备类的验收标准是"core 不含
   cfg(target_arch)，后端各一个文件"。

### 度量（防回归）

- 组件层 `cfg(target_arch)` 总数不随新功能增加（当前约 20）；
- 每个设备类只有一份 `Cargo.toml`/core；重复 crate 名出现即 CI 失败（可加一条
  `tools/` 脚本检查 workspace members 的重名）；
- 同一 core 在两个架构的 QEMU 上跑同一套门禁（见 P1）。

## 4. P1：DRM shared-core（#203 ①）落 main 的步骤

DRM 是唯一已经"共享 core + 薄后端"跑通两架构的样板：
`aster-drm` 核心（`kernel/comps/drm`）+ `drm-virtio-gpu`/`simpledrm` 插件 +
RISC-V `eic7700` 后端；x86 与 RISC-V 的 virtio-gpu 2D 走同一驱动。

落 main 的适配面（预计，不含实现顺序）：

1. `kernel/comps/drm`：把 `aster_core::prelude` 依赖换成 main 的
   `crate::prelude`/ostd；设备注册接 main 的 `kernel/src/device` 与
   `/dev/dri`；事件/时间用 main 的 `events`/`time`。
2. `kernel/comps/drm-virtio-gpu`：接 main 的 `kernel/comps/virtio` GPU 设备。
3. `kernel/comps/simpledrm`：接 main 的 `aster-framebuffer`。
4. 删除/降级 main 的 `kernel/src/device/dri.rs` 单体实现，只保留注册与后端选择。
5. 域验证：x86 与 RISC-V QEMU 各跑一次 conformance（列 mode、一次 flip、
   原子提交），矩阵与现有 `tools/x86/debian`、`tools/riscv` 门禁共用。

难点与依赖：`drm` 核心 8401 行、36 个文件，依赖 feat 的 `device` 模型与
`aster_core` 框架；这一步本质上是一次"组件化移植"，不是 cherry-pick。
建议先做 1（core 编译进 main，后端仍用现有 x86 单体路径），再逐步切 2/3/4。
