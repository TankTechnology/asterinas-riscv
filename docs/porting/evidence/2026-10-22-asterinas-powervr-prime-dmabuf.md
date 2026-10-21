# Asterinas PowerVR PRIME / dma-buf（Megrez P4，2026-10-22）

## 结论

- P4 主机侧完成：B3 的 PVR GEM 对象可以经 `PRIME_HANDLE_TO_FD` 导出为
  dma-buf、再经 `PRIME_FD_TO_HANDLE` 导入；导入保留对象原有的固件 MMU
  映射（`pvr_vaddr`），并新增引用/句柄；
- dma-buf 文件实现 `DMA_BUF_IOCTL_SYNC`（0x40086200）：按
  `START/END × READ/WRITE` 做 clean/invalidate，真实 cache 维护走新增的
  ostd `mm::dma::sync_dma_range`（ZICBOM `cbo.clean/inval/flush`），
  QEMU/无 ZICBOM 时安全退化为 no-op；
- 导出附带最小隐式同步：优先挂 B2 最后完成作业的 fence，否则挂一个已
  signal 的 fence；不是完整的 dma-resv，属于 implicit-sync 桩；
- 2 条 QEMU ktest 通过（各 `1 passed; 0 failed; 351 filtered out`）：
  `gpu_prime_export_import_roundtrip`、`gpu_dmabuf_cpu_access_sync`；
- **板级 marker 已定义但延后**：板子仍需物理复位后统一补批量收据。

期望的板级 marker（只对带 `pvr_vaddr` 的对象打印）：

```text
ASTERINAS_POWERVR_PRIME status=ok exported=1 imported=1
```

## 实现

### 导出（`dri/prime.rs`）

- 导出描述符 `DmaBufExport { object_id, offset, size, format, pvr_vaddr }`：
  - `size` 为对象字节数，`format` 暂为 GEM 的 bpp（原始 PVR GEM 为 0）；
  - `pvr_vaddr` 是该对象在 job MMU 上下文中的 GPU VA；
- `describe_object()` 在设备级对象空间里取对象并增加引用；导出失败不会
  留下多计数的引用；
- dma-buf fd 持有 pool、对象 id、描述符、GPU 资源（若有）和 fence，
  关闭 fd 时释放对象引用。

### 导入

- `PRIME_FD_TO_HANDLE` 目前只接受本驱动导出的 `DmaBufFile`：校验
  `size` 一致后增加对象引用，并在当前文件的 handle 表中建立新句柄；
- 对象在 B3 创建时已经映射进 `PvrGemContext`，导入不做第二次映射，
  因此 `pvr_vaddr` 在导出/导入前后保持一致；
- **外来 dma-buf**（其他驱动/设备导出的 fd）还没有通用 provider
  注册表，无法转换成 Asterinas 的池内 GEM；记入 P4a，不在本轮假装支持。

### `DMA_BUF_IOCTL_SYNC`

- 命令：`_IOW('b', 0, struct dma_buf_sync)` = `0x40086200`，参数是
  `u64 flags`；
- flag 语义（未知位、以及既无 READ 又无 WRITE 的请求一律 `EINVAL`）：

  | flags | 动作 |
  |---|---|
  | `START|READ` | invalidate（CPU 读之前丢弃旧 cache line） |
  | `START|WRITE` | invalidate（CPU 写之前丢弃旧 line） |
  | `END|WRITE` | clean（让设备看到 CPU 写入） |
  | `END|READ` | 无需维护 |

- 物理地址取 `pool.paddr() + object.offset`，长度取对象大小；
- 新增 `ostd::mm::dma::sync_dma_range(paddr, size, direction)`：逐页检查
  frame metadata（`frame::meta::is_in_use`）后才执行 cache 指令，避免把
  未支撑的物理范围交给 `cbo.*`；内核侧因此不需要 `unsafe`（kernel crate
  仍保持 `#![deny(unsafe_code)]`）；
- fdinfo 增加 `drm-dmabuf` 与 `drm-dmabuf-cache` 行，便于板级核对
  object/size/format/gpu_va/fence 以及 clean/invalidate 次数。

### 隐式同步（fence 桩）

- B2 no-op 作业完成时除了 signal 自己的 fence，还调用
  `fence::record_completed()` 记录“最后完成作业的 fence”；
- 导出 dma-buf 时通过 `fence::completed_or_signaled()` 取该 fence；
  若还没有作业完成，则创建一个立即 signal 的 fence；
- fdinfo 的 `fence=signalled|pending` 反映状态。真正的 dma-resv
  多 fence 合并、导出 fence fd 属于后续 sync bridge。

## ktest

| 测试 | 覆盖 | 结果 |
|---|---|---|
| `gpu_prime_export_import_roundtrip` | 导出描述符的 offset/size/format/gpu_va、引用计数 +1；导入同对象、引用 +1；两次释放后对象仍在，第三次释放回收 | `1 passed; 0 failed; 351 filtered out` |
| `gpu_dmabuf_cpu_access_sync` | START/END × READ/WRITE 的 clean/invalidate 分类、非法 flag 拒绝、cache 计数、在真实 DMA 分配上调用 `sync_dma_range` 两个方向 | `1 passed; 0 failed; 351 filtered out` |

原始日志（未跟踪）：`target/kt-p4-gpu_prime_export_import_roundtrip.serial.log`、
`target/kt-p4-gpu_dmabuf_cpu_access_sync.serial.log`。

## 板级待补

1. 物理复位后随批量启动，用一个最小客户端对 PVR GEM 做
   `PRIME_HANDLE_TO_FD → PRIME_FD_TO_HANDLE`，确认
   `ASTERINAS_POWERVR_PRIME status=ok` 且 fdinfo 中 `gpu_va` 不变；
2. 在 Megrez 非一致 DMA 上执行 `DMA_BUF_IOCTL_SYNC`，确认 ZICBOM 路径
   的 clean/invalidate 计数增长且 CPU/GPU 数据一致；
3. 外来 dma-buf 导入、dma-resv/fence fd 导出仍待 P4a/后续 bridge。

## 边界

- 不实现 gbm（P5 再议）；不实现 vendor DMABUF bridge；
- 不改变 P2/B1–B4 既有 marker 语义。
