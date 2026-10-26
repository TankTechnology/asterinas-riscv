# IPC 修复后 Firefox 是否真的画出页面（A 组复测）

日期：2026-10-26
内核：`e8b4a8194c0918ac`（含 IPC 修复 `f6e4188e6`）
门禁：`/tmp/vb/visual_gate.sh`（含上次 5 条踩坑修正），探针 v5 + 本次加的像素诊断
运行：A4（原探针，常规指标）、A5（+ XWD 像素统计）、A6（+ Firefox 完整错误行）

## 1. 运行命令（原始）

```sh
R=~/Program/asterinas-riscv
/tmp/vb/visual_gate.sh \
  --kernel    $R/target/osdk/asterinas-osdk-bin.elf \
  --initramfs $R/target/debian-x86/stage1/initramfs.cpio \
  --base-root $R/target/ff-lag/daily.ext2 \
  --workspace ~/vb-ws/A4 --loglevel 3 --timeout 800
# A5/A6 同命令，仅换 workspace
```

门禁结果（A4）：`XORG_CLIENT user=True pass=True command=True done=True`、
`screendump 1024x768 red=1.000`、`PASS`。

## 2. 核心问题的直接回答

| 问题 | 回答 | 原始行 |
|---|---|---|
| `ff_diag_procs` = ? | **7**（上次 1，Linux 对照 12） | `PROBE ff_diag_procs=7` |
| 页面是否真的出现像素变化？ | **没有**。首次诊断（A5）用 XWD 统计全屏：**92.5% 的采样像素是纯红 `#ff0000`**（即 X 根窗口），Firefox 窗口区域没有内容；按 `End` 后 md5 完全不变 | `PROBE page_pixels=w=1024 h=768 sampled=52480 unique=115 top=#ff0000 topfrac=0.925`<br>`PROBE page_shot_md5=697456a3ab7975e1e0899d9670276ca6`<br>`PROBE page_end_md5=697456a3ab7975e1e0899d9670276ca6`<br>`PROBE page_end_changed=0` |
| `first_frame_ms_1..10` | **NA ×10**（每项等 3000 ms） | `PROBE first_frame_ms_1=NA` … |
| `input_latency_ms_1..10` | **NA ×10** | `PROBE input_latency_ms_1=NA` … |
| 是否还有第三条 EPERM？ | **没有**。`ff_diag_err` / `ff_page_err` 只剩 a11y DBus 警告 | `PROBE ff_diag_err=: 'glib warning', file toolkit/xre/nsSigHandlers.cpp:201||** (firefox-esr:188): WARNING **: …Failed to create DBus proxy for org.a11y.Bus: …dbus-launch…|` |

## 3. 指标对比（A4/A5，中位数）

| 指标 | Asterinas（本轮 A4/A5） | Linux（B3 基线） | 相对差 |
|---|---|---|---|
| Firefox 冷启动 ×3 | **5053 / 5175 / 5232**（A5），4914 / 5565 / 4923（A4） | 969 | ≈ 5.3× 慢 |
| 滚动 20 页 ×3 | **1405 / 1373 / 1504**（A5），1404 / 1414 / 1376（A4） | 901 | ≈ +55 % |
| 首帧延迟 ×10 | **NA** | 31.5 ms | 不可测（无像素变化） |
| 输入延迟 ×10 | **NA** | 134.0 ms | 不可测 |
| glxgears FPS ×3 | 373.2 / 388.9 / 385.3 | 75.4 | 不可比（无 vsync 节流，见 RESULTS.md §4.1） |
| 渲染器 | llvmpipe (LLVM 19.1.7, 256 bits) / Mesa 25.0.7 | 同左 | 0 % |
| 截屏采集地板 ×10 | 63–106 ms | 12 ms | ≈ +6× |

冷启动/滚动与上次（5417 / 1372）基本持平：IPC 修复解决的是"内容进程能不能活"，
不是启动耗时。

## 4. 判据对照

| 判据 | 实际 |
|---|---|
| 首帧有数（<1000 ms） | ✗ 仍 NA |
| 仍 NA 但错误变了 | **✔ 正是这一档**：`pipe error: Operation not permitted` 消失、内容进程 1→7；但窗口没有任何绘制像素 |
| 仍 NA 且错误相同 | ✗ |

结论：**IPC EPERM 修复有效但不充分**。失败点从"内容进程在 IPC 上崩溃"前移到
"进程存活但页面不呈现"。这条不属于 IPC/EPERM 类问题（`ff_diag_err`/`ff_page_err`
已无 EPERM），需要按"窗口存在但零像素"继续查（合成器/呈现路径）。

## 5. 证据

- `evidence/2026-10-27-firefox-paint-after-ipc-fix/A4-probe-lines.txt`
- `…/A5-probe-lines.txt`（含 `page_pixels` 像素统计）
- A6：**无探针行**。该轮 guest 在 `XORG_SETROOT` 之后停摆（console 停在 `XORG_ok_END`），
  未进入探针，因此没有 Firefox 完整错误行（见 §6）
- 控制台原文：`~/vb-ws/A4/console.txt`、`~/vb-ws/A5/console.txt`、`~/vb-ws/A6/console.txt`
- 本次对 `/tmp/vb/visual_probe.sh` 的增量：注入 `/tmp/xwd_stats.py`（XWD 像素统计）+
  `page_pixels`/`page_end_changed`/`ff_page_err_hits` 三个诊断项；测量协议未改。
  原 v5 备份：`/tmp/vb/visual_probe_v5.sh`。

## 6. 下一卡点（A6 未取得）

为了拿到 Firefox 的完整错误行，A6 在探针里加了
`ff_page_err_hits`/`ff_page_log_bytes`（grep `error|eperm|fail|abort|crash|shutdown`）。
**A6 没有跑完**：控制台停在 433 行约 10 分钟无进展，QEMU 仍在但 guest 无输出，
与上一轮 `setpriv` 之后那次卡死同型（独立缺陷，见
`2026-10-27-ipc-sendmsg-eperm.md` §4.3）。本轮最终证据以 A4/A5 为准。

已能确定的是：`ff_page_err`（tail 4 行）在 A5 里**只有** a11y DBus 警告，
没有 EPERM；窗口存在（921x691）但全屏 92.5% 是根窗口红色，`End` 按键零像素变化。
也就是说，剩下的是**呈现路径**问题（窗口没有内容），不是 IPC 权限问题。
