# 显示 2D：真正按 damage 传输（dirtyfb clips）

日期：2026-10-28　内核：`feat/drm-virtio-gpu`（x86 QEMU，virtio-gpu-pci，1024×768）
前置画像：`2026-10-27-display-2d-profile.md`（当时每次 present 都是整屏）

## 根因

`drm_mode_dirty_fb()` 里有一条 TODO："Honor dirtyfb flags, color, and clip
rectangles. For now, treat every dirtyfb request as a whole-framebuffer
refresh." 它把 Xorg 传来的 clip 数组只用于校验用户指针，然后按 plane 的
`source_rect()`（整屏）刷新。实测确认 Xorg modesetting + 软件 glamor 的呈现路径
**就是走 DIRTYFB**（typing 期间 `page_flip`/`atomic` 调用数为 0，dirty_fb 每帧一次），
所以每一帧都在搬 3 MiB。

## 修复

`kernel/comps/drm/src/ioctl/kms.rs`：

- `num_clips == 0` 保持 Linux 语义（整屏）；
- 否则读取 clip 矩形，按 framebuffer 尺寸裁剪，把**这些矩形**交给
  `refresh_dirty_fb`（ANNOTATE_COPY/FILL 一律视为脏区，刷新语义安全）；
- 空矩形列表直接返回。

## 前后对比（QEMU trace，固定 10 秒窗口）

| 负载 | 修复前 | 修复后 | 降幅 |
|---|---|---|---|
| 连续打字 | 102 次 × 1024×768（321 MB） | 103 次 × **642×410**（108 MB） | **3.0×** |
| 终端滚动 | 35 次 × 1024×768（110 MB） | 33 次 × **642×410**（34 MB） | **3.3×** |
| Firefox 本地页 | 965 次 × 1024×768（3.0 GB） | 520 次，多为 **1024×63** 横带（106 MB） | **28×** |

- Firefox 窗口内 Xorg CPU：0.25 s → **0.09 s**（同 10 秒窗口）。
- 传输矩形现在是真实脏区：终端窗口 642×410、Firefox 的地址栏/工具栏横带 1024×63、
  小部件 40×18/40×22。屏幕内容正常（screendump 非黑像素 100%），应用均正常运行。
- 每帧传输字节降到原来的 1/3（打字/滚动）到 1/28（Firefox）。RISC-V（TCG）上
  同样的字节节省的相对收益更大。

## 对原计划 1/3 的影响

- **1（按刷新率合并 present）**：修复后 present 速率已在 vblank 以下
  （打字 10.3/s、滚动 3.3/s、Firefox 52/s < 60 Hz），**没有可测量的合并目标**，
  本轮不引入该改动（避免为无收益的语义变化承担风险）。
- **3（合并 transfer+flush 通知）**：单次 flush 25–600 µs、transfer 视矩形而定；
  在当前 present 数下预期收益为每 10 秒几十毫秒（<1–3% CPU），低于本轮字节收益
  两个数量级，留待后续；接口层需要批量入队，属于驱动传输层改动。

## 验证

- 内核构建通过（x86_64/microvm）；临时 ioctl/计时打印已全部回滚，ELF 中
  `DRMIOCTL`/`GPUPROF` 计数为 0。
- 镜像 `e2fsck -fn` 退出码 0；VM 正常关机。

## 后续

- 若客户端恢复到 >60 presents/s（如浏览器合成更激进），再考虑 vblank 合并。
- 若 Xorg 能进一步缩小 damage（如只报光标/文本行区域），内核已无需再改。
