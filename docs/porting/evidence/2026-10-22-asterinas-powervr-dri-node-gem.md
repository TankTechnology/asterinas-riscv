# Asterinas PowerVR render node + GEM 对象（Megrez P3/B3，2026-10-22）

## 结论

- B3 主机侧完成：现有 DRM 核心已经提供 `DriNode::Render`
  （`renderD128`），本轮在该节点上注册一个最小的 PowerVR GEM 分配
  ioctl，`DRM VERSION / GET_CAP / SET_CLIENT_CAP` 继续复用核心实现；
- GEM 对象后端复用 B2 的作业缓冲/PMR 路径：对象从设备级连续 DRI pool
  中切出，创建时按页映射进专用 `PvrGemContext`（MMUv4 页表），
  `GEM_CLOSE`/文件关闭在最后一个引用释放时解映射；
- 5 条 QEMU ktest 通过（3 条 GEM + MMU 跨表边界 + GEM 释放路径），
  另 4 条 MMU 回归 ktest 在本轮树中同样通过；
- **板级 marker 已定义但延后**：板子仍处于 B1 上传启动后的挂死状态
  （串口/网络均无响应），需要一次物理复位后统一补 B1/B2/B3 收据；
- 本轮不实现 vendor PVR bridge/ioctl 全量（`DRM_IOCTL_PVR_SRVKM_CMD`，
  B4 之后），不改变 P2/B1/B2 既有 marker 语义。

期望的板级 marker：

```text
ASTERINAS_POWERVR_DRI_NODE status=ready node=renderD128 gems=0
ASTERINAS_POWERVR_GEM status=ok handle=1 size=4096 offset=0x0 gpu_va=0x4000000000 gems=1
```

`DRI_NODE` 在 `ASTERINAS_POWERVR_OWNER status=ready` 之后打印；
`GEM` 在每次成功的 `DRM_IOCTL_PVR_GEM_CREATE` 之后打印。`gems` 是当前
job MMU 上下文中的活动映射数，`offset` 是传给 `mmap` 的 pool 偏移，
`gpu_va` 是对象在该上下文中的 GPU 虚拟地址。

## 实现

### ioctl 面（`dri.rs`）

- 新命令 `DRM_IOCTL_PVR_GEM_CREATE`：
  `_IOWR('d', 0x50, struct DrmPvrGemCreate)`，32 B，命令号
  `0xc0206450`；
- `struct DrmPvrGemCreate`：

  | 字段 | 方向 | 含义 |
  |---|---|---|
  | `size: u64` | in | 请求字节数 |
  | `flags: u32` | in | 必须为 0 |
  | `handle: u32` | out | 本文件的 GEM handle |
  | `offset: u64` | out | DRI pool 偏移，传给 `mmap` |
  | `gpu_va: u64` | out | job MMU 上下文中的 GPU VA |

- 该命令加入 render node 的 allowlist（`is_render_allowed`），因为
  PVR 客户端只通过 render node 分配 GEM；
- 非 `powervr_probe` 构建仍能编译该 ioctl 定义（dispatch 宏不接受
  match 分支上的 `cfg`），但 handler 返回 `EOPNOTSUPP`，不会误报支持。

### GEM 创建语义

1. `alloc_object()` 从设备级连续 pool（`VmoFlags::CONTIGUOUS`，64 MiB）
   切出页对齐 span，得到 pool 偏移；
2. `paddr = pool 基址 + offset`，交给
   `powervr_probe::gem::create(paddr, size)`；
3. 专用 `PvrGemContext` 使用独立 `GpuMmu4` 根，VA 从
   `0x40_0000_0000` 单调分配，4 KiB 页，单对象上限 64 MiB；
4. `map_external()` 只安装 PTE、不拥有物理页（页属于 DRI pool），
   权限为可读写 + `PM_META_PROTECT`；
5. GPU VA 记录在 `GemObject::pvr_vaddr`，随后为该文件分配 handle。

### mmap 与关闭语义

- mmap 不需要新路径：DRM 设备的 `Mappable` 返回整个 pool 的 `Vmo`，
  客户端把创建返回的 `offset` 作为 `mmap` 偏移即可，语义与
  `MODE_MAP_DUMB`/`VIRTGPU_MAP` 一致；
- `GEM_CLOSE` 或文件关闭走既有 `release_object_locked()`：
  - 引用计数减一，最后一个引用时调用 `gem::destroy(vaddr)`；
  - `destroy()` 清除该范围的 PTE 并读回校验，然后从活动映射表移除；
  - 对象空间是设备级的，因此 PRIME/flink 共享的对象只在最后一个
    handle 释放时解映射。

### MMU 外部映射（`mmu.rs`）

- 新增 `ExternalMap { start, end, pages }`，与 `map_owned()` 的
  `OwnedMap` 并存；
- 抽出 `install_ptes()`，`map_owned()` 与 `map_external()` 共用
  PC/PD/PT 建立与回读校验；
- `check_overlap()` 同时检查 owned 与 external 两类映射；
- `unmap_external()` 逐页清 PTE 并回读，缺页/未知映射返回明确错误；
- 安装失败会把页表标记为 `poisoned`，与 owned 路径一致，不留下
  半安装状态。

## ktest

| 测试 | 覆盖 | 结果 |
|---|---|---|
| `gpu_gem_create_maps_and_unmaps_a_contiguous_range` | 创建即映射、PTE 有效、guard 页未映射、解映射后 PTE 清零、重复解映射报错 | `1 passed; 0 failed; 346 filtered out` |
| `gpu_gem_create_keeps_distinct_context_ranges` | 两个对象获得互不重叠的连续 VA，单独解映射不影响另一个 | `1 passed; 0 failed; 346 filtered out` |
| `gpu_gem_create_rejects_invalid_sizes` | 0 字节与超过 64 MiB 的请求被拒绝，不产生映射 | `1 passed; 0 failed; 346 filtered out` |
| `gpu_mmu4_maps_only_an_owned_dma_allocation_across_table_boundary` | `install_ptes()` 重构后 owned 路径跨 PT/PD 边界仍正确 | `1 passed; 0 failed; 346 filtered out` |
| `the_last_release_frees_the_object_and_its_name` | 最后一个引用释放时对象与名字一起回收（GEM 关闭路径回归） | `1 passed; 0 failed; 346 filtered out` |

原始日志（未跟踪）：`target/kt-b3-gpu_gem_create_*.serial.log`、
`target/kt-b3-gpu_mmu4_maps_only_an_owned_dma_allocation_across_table_boundary.serial.log`、
`target/kt-b3-release.serial.log`。其余 4 条 MMU ktest 日志也在
`target/kt-b3-gpu_mmu4_*.serial.log`，均为 `1 passed; 0 failed`。

## 输入与验证环境

| 项 | 值 |
|---|---|
| 构建 | `make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=powervr_probe`，`cargo fmt --check` 通过 |
| ktest | `cargo osdk test <name> --features=powervr_probe --target-arch riscv64 --scheme riscv` |
| 板级 | 延后；复位后用同一 `FEATURES=powervr_probe` 镜像并带 `asterinas.powervr=1` 启动 |

## 板级待补项

1. B1 `ASTERINAS_POWERVR_KCCB_PIPELINE`、B2
   `ASTERINAS_POWERVR_JOB` 的板级收据；
2. B3 `ASTERINAS_POWERVR_DRI_NODE` + `ASTERINAS_POWERVR_GEM`：用一个
   最小客户端（或直接 ioctl）调用 0x50，确认固件 MMU 上下文接受该
   PTE；若固件未观察到新页表，按 B2a 计划在提交前加
   `RGXFWIF_KCCB_CMD_MMUCACHE`；
3. 当前 `PvrGemContext` 只有页表根，尚未作为 per-job 上下文安装进
   GPU catalogue；上下文激活属于 B3/B4 后续，板级收据需要区分
   “PTE 已建立”与“固件已使用该上下文”。

## 边界与下一步

- 本轮只实现最小 GEM 创建/映射/解映射与 mmap 偏移，不实现 vendor
  `PVR_SRVKM_CMD` bridge、sync ioctl、render context/freelist；
- **B4**：fence/sync（时间线、可等待 fd）与 PVR bridge 的最小命令集；
- **B5**：GLES 后端路线；
- 板子复位后按 B1 → B2 → B3 顺序补 marker，再更新本文件与计划状态。
