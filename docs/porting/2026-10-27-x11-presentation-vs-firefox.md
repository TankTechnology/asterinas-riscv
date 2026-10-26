# 「窗口活着但零像素」到底卡在 X 服务端还是 Firefox

日期：2026-10-26
内核：`e8b4a8194c0918ac`（与上一轮相同，未重新编译）
门禁：`/tmp/vb/present_gate{1,2,3,4,5,6}.sh`（`visual_gate.sh` 副本，注入本文的六个探针）
探针：`/tmp/vb/present_probe{1,2,3,5,6}.sh`；新增 `/tmp/png_stats.py`（PNG 离屏像素统计）

## 0. 结论先行

**既不卡在 X 服务端，也不卡在 Firefox。页面能画出来，问题是上一轮探针自己的 bug。**

根因：原版探针（`visual_probe.sh` v5/v6）起页面实例时用的是
`firefox --profile /tmp/ffs file:///tmp/page.html`，但**它从来没有创建 `/tmp/ffs` 这个目录**
（diag 和冷启动阶段都建了 `/tmp/ffd`、`/tmp/ffp$i`，唯独页面阶段漏了）。Firefox 于是弹出
一个 **"Profile Missing"** 对话框而根本不去加载页面：

```
PROBE page_children=2 children:|0x20002b "Profile Missing": ("Firefox" "firefox-esr")  566x104+229+332|0x200001 "Firefox": ("firefox-esr" "Firefox-esr")  10x10+10+10|
PROBE page_mapstate=IsViewable
PROBE page_pixels=w=1024 h=768 sampled=52480 unique=115 top=#ff0000 topfrac=0.925
```

上一次报的「整屏 92.5% 红、窗口零像素」就是**这个 566×104 对话框**（7.5% 面积、115 种颜色）
加红色根窗口。页面窗口从未存在，所以 `first_frame_ms_*` / `input_latency_ms_*` 全 NA。

把 `/tmp/ffs` 建出来之后（P3、P6），页面立刻画出来，首帧 **约 50 ms**。

## 1. 运行命令（原始）

```sh
R=~/Program/asterinas-riscv
/tmp/vb/present_gate6.sh \
  --kernel    $R/target/osdk/asterinas-osdk-bin.elf \
  --initramfs $R/target/debian-x86/stage1/initramfs.cpio \
  --base-root $R/target/ff-lag/daily.ext2 \
  --workspace ~/vb-ws/P6 --loglevel 3 --timeout 900
```

六轮门禁均为 `screendump 1024x768 red=1.000` + `PASS`；`Uncaught panic` 计数 0。

## 2. A 组：X 服务端能不能画（P1，任务书要求必做）

| # | 用例 | 原始行 | 判读 |
|---|---|---|---|
| A1 | `xsetroot -solid red` → `#00ff00` | `a1_red_pixels=w=1024 h=768 sampled=52480 unique=1 top=#ff0000 topfrac=1.000`<br>`a1_green_pixels=w=1024 h=768 sampled=52480 unique=1 top=#00ff00 topfrac=1.000`<br>`a1_changed=1` | 整屏 1 色，红→绿完全翻转 |
| A2 | xterm 窗口自身 | `a2_xterm_win_pixels=w=426 h=240 sampled=51120 unique=2 top=#ffffff topfrac=0.987` | 白底黑字，窗口有内容 |
| A2 | xterm 是否上屏 | `a2_xterm_screen_pixels=w=1024 h=768 sampled=52480 unique=3 top=#00ff00 topfrac=0.869` | 86.9% 绿根 + xterm 区域 |
| A3 | 量化结论 | 同上（`xwd_stats.py`） | 见左 |
| — | X 扩展 | `x_extensions=DRI3,MIT-SHM,Present,RENDER,SHAPE,X-Resource,` | MIT-SHM / DRI3 / Present 都在 |
| — | 空闲 GL 客户端 | `a2_glx_window=none` | glxgears 20 s 内未出窗口（未取得） |

结论 A：**X 服务端、fbdev/modesetting 写入通路、窗口合成都正常**——任务书里
「A1 也不变 ⇒ X 服务端/DAMAGE/Present 有问题」这一档被排除。

## 3. B 组：Firefox 的合成 / 呈现路径（P1）

| 观测点 | 原始行 | 判读 |
|---|---|---|
| 离屏渲染（`--screenshot`） | `default_shot_pixels=w=900 h=700 sampled=52650 unique=196 top=#ffffff topfrac=0.915`（117 172 B PNG） | 渲染器出像素 |
| Firefox 窗口自身（`xwd -id`） | `default_win_pixels=w=921 h=691 sampled=53361 unique=488 top=#ffffff topfrac=0.824`<br>`default_win_md5=…`，2 s 后同值 | 窗口有内容且稳定 |
| Firefox 整屏 | `default_screen_pixels=w=1024 h=768 sampled=52480 unique=452 top=#ffffff topfrac=0.674` | **内容确实上屏** |
| MIT-SHM | `x_extensions=…MIT-SHM…` | 不存在「SHM 缺失导致静默降级」 |
| GPU 探测 | `default_gfx_hits=/tmp/default-win.out:[GFX1-]: glxtest: libEGL no display` | EGL 不可用（无 virgl），走 GLX/软件路径；GLX 可用（glxgears 373–389 FPS） |

## 4. C 组：环境变量变体（P1，四组各一次）

| 配置 | 窗口 md5 | 窗口像素 | 离屏 PNG |
|---|---|---|---|
| 默认 | `eca8576cdcd44b7e469ab3c4dcea3f14` | `unique=488 top=#ffffff 0.824` | 117 172 B |
| `MOZ_WEBRENDER=0` | `eca8576c…`（同） | 同 | 117 172 B |
| `MOZ_X11_EGL=0` | `eca8576c…`（同） | 同 | 117 172 B |
| `MOZ_WEBRENDER=0 MOZ_X11_USE_SHM=0` | `eca8576c…`（同） | 同 | 117 172 B |

四组**字节完全相同**：`glxtest: libEGL no display` ⇒ EGL 本来就不存在，`MOZ_X11_EGL=0` 是
空操作；关掉软件 WebRender 后走基础合成器，像素输出一致。因此本任务书里「哪组能画出像素」
在本次没有判别力——四组都画出来了。

## 5. 根因定位过程（单变量，逐条）

| 轮次 | 改动 | 结果 | 结论 |
|---|---|---|---|
| P1 | 新探针：A1/A2 + `xwd -id` + `--screenshot` + 四组 env | 窗口/整屏都有内容 | 呈现正常 |
| P2 | 复刻 A5 条件（红根、无干预），逐秒轮询 60 s | `b2_pixels_at5s=… top=#ffffff 0.678` | 画出 |
| P3 | 完整复刻 A5 序列（diag + 冷启动 ×3），页面阶段先等窗口清空 | `page_pixels_at5s=… top=#ffffff 0.678` | 画出 |
| P4 | **原版探针 v6 原样重跑**（对照） | `page_pixels=… unique=115 top=#ff0000 0.925`，`page_shot_md5=697456a3…` 与 A5 逐位相同 | 缺陷可 100% 复现 |
| P5 | 原版探针 + **只加一行 `xsetroot -solid red`** | 仍然 `unique=115 top=#ff0000 0.925`；新增诊断抓到 `"Profile Missing"` 窗口 | 证伪「需要别的 X 客户端活动/根重绘」假设；直接暴露真正原因 |
| P6 | 原版探针 + **只加一行 `mkdir -p /tmp/ffs`** | 见 §5.1 | 单变量确认根因 |

### 5.1 P6 结果（单变量确认）

原版探针 v6，**唯一改动**是页面实例启动前加一行 `mkdir -p /tmp/ffs`：

```
PROBE page_window_polls=7
PROBE page_pixels=w=1024 h=768 sampled=52480 unique=506 top=#ffffff topfrac=0.678
PROBE page_end_changed=1
PROBE page_pixels_after_end=w=1024 h=768 sampled=52480 unique=506 top=#ffffff topfrac=0.673
PROBE first_frame_ms_1=53 … first_frame_ms_8=56   （另 1 次 1187 ms、1 次 NA）
PROBE scroll20_ms_1..3=2731/2432/2135
PROBE firefox_cold_start_ms_1..3=5292/5457/5070
PROBE ff_diag_procs=7
```

与 P4（同一探针、**没有**这一行）逐项对照：

| 行 | P4（无 mkdir） | P6（+mkdir） |
|---|---|---|
| `page_pixels` | `unique=115 top=#ff0000 0.925` | `unique=506 top=#ffffff 0.678` |
| `page_end_changed` | `0` | `1` |
| `first_frame_ms_1` | `NA` | `53` |
| 屏幕上的窗口 | `"Profile Missing" 566x104`（P5 诊断） | Firefox 页面窗口 921x691 |

一行改动同时把「主色、按键响应、首帧可测性」三项全部翻正，根因确认。

### 5.2 一个尚未查清的旁证：Linux 侧同一探针为什么没弹对话框

A3 与 B3 用的是**同一份 v5 探针**（md5 `9ad73d98…`），页面阶段同样
`firefox --profile /tmp/ffs` 且同样没有建目录：

```
A3-probe-lines.txt: probe_version=5   first_frame_ms_1=NA      window_id=4194347
B3-probe-lines.txt: probe_version=5   first_frame_ms_1=47      window_id=2097195
```

Linux 侧拿到了首帧数字（47/…/449 ms），说明它**加载了页面**——即缺目录时 Linux 上的
Firefox 自己把 profile 建了出来，而 Asterinas 上退化成 "Profile Missing" 对话框。
这两侧行为不一致，指向 Firefox 创建 profile 目录的那条路径（`mkdir`/`openat` 语义）在我们的
内核上有差异。**此点尚未验证**（本轮时间用尽），下一步的最小验证是：

```sh
firefox --profile /tmp/nonexist about:blank &     # 然后
ls -ld /tmp/nonexist; xwininfo -root -children    # 看目录是否被创建、窗口标题是什么
```

注意：如果确认是这个差异，那么**上一轮 A/B 基线里 A 组的所有"页面"数字（首帧 NA、滚动
1373–1504 ms）都是在对话框上测的**，A/B 对比需要以 P3/P6 的数字重算。

## 6. 修正后的性能数字（页面真的画出来之后）

P3（完整复刻 A5 序列 + 建好 profile）测得的数字：

| 指标 | Asterinas P3 | Asterinas P6 | Linux B3 基线 | 相对差（对 P3 / P6） |
|---|---|---|---|---|
| Firefox 冷启动（窗口出现） | **4918 ms**（4986/4918/4713） | **5292 ms**（5292/5457/5070） | 969 ms | 5.1× / 5.5× 慢 |
| 滚动 20 页 ×3 | **1960 ms**（1940/2005/1960） | **2432 ms**（2731/2432/2135） | 901 ms | +118 % / +170 % |
| 首帧延迟（有效值中位） | **50 ms**（45/49/44/52/50/48/51/56，另 977、NA） | **53 ms**（53/46/55/49/64/53/51/56，另 1187、NA） | 31.5 ms | +59 % / +68 % |
| 输入延迟 ×10 | **NA ×10** | 134.0 ms | 未取得（见 §7） |
| 截屏采集地板 | 63–106 ms | 12 ms | ≈ +6× |

上一轮报告的「冷启动 5053–5232 ms、滚动 1373–1504 ms、首帧 NA」来自对话框那一次，
**滚动数字应作废**（那些按键没有触发任何重绘）；以本表为准。

## 7. 仍未取的项（如实记录）

- `input_latency_ms_*` = NA ×10：探针在页尾用 `Down` 触发变化，但此前 3×20 次 `Page_Down`
  已把页面推到底部，`Down` 不再产生像素变化。**探针设计问题**，下一轮应先 `Home` 再按一次。
- `a2_glx_window=none`：glxgears 20 s 内没等到窗口，未取得 GL 客户端的窗口像素；
  GLX 路径本身有 FPS 数据（373–389 FPS）。
- `about:support` 未抓：guest 内无 Marionette/无头自动化入口；以 `glxtest: libEGL no display`
  与四组环境变量字节一致作为合成路径证据，强度弱于 about:support，已注明。

## 8. 结论

1. **不卡在 X 服务端**：根窗口整屏翻转（`unique=1`）、xterm 上屏、MIT-SHM/DRI3/Present 齐全，
   QEMU `screendump` 与 `xwd` 两侧一致。
2. **不卡在 Firefox**：离屏 PNG、窗口自身 488 色、整屏 67.4% 白底内容，四组环境变量一致。
3. **上一轮的「零像素」是探针 bug**：`--profile /tmp/ffs` 目录不存在 ⇒ Firefox 弹
   "Profile Missing" 对话框，页面窗口从未创建。修复后页面正常绘制。
4. 需要修的是**探针**（建 profile 目录、输入延迟先回顶、等待真正的新窗口），
   不是内核，也不是 Firefox。
5. 修正后的真实差距在性能：冷启动 5.1–5.5×、滚动 +118～170 %、首帧 +59～68 %、截屏地板 +6×。
6. 遗留一条**未验证**的线索：同一份探针在 Linux 上缺 profile 目录时 Firefox 自己把目录建出来
   并加载了页面（B3 有首帧数字），在 Asterinas 上却退化成 "Profile Missing" 对话框（§5.2）。
   这可能是内核侧 `mkdir/openat` 语义差异，建议作为下一个最小任务。

## 9. 证据

- `evidence/2026-10-27-x11-presentation-vs-firefox/P1-probe-lines.txt`（85 行；A1/A2/A3 + 四组 env）
- `…/P2-probe-lines.txt`（33 行；A5 条件受控复现 + 强制重绘）
- `…/P4-probe-lines.txt`（84 行；原版探针对照，复现 A5）
- `…/P5-probe-lines.txt`（86 行；`xsetroot` 单变量 + `page_children` 抓到 "Profile Missing"）
- `…/P3-probe-lines.txt`、`…/P6-probe-lines.txt`（完整序列复刻与单变量确认）
- 控制台原文：`~/vb-ws/P{1,2,3,4,5,6}/console.txt`；门禁日志 `/tmp/vb-P{1,2,3,4,5,6}-gate.log`
- 探针：`/tmp/vb/present_probe1.sh`（= `present_probe.sh`）、`present_probe2.sh`、
  `present_probe3.sh`、`present_probe5.sh`、`present_probe6.sh`；原版备份 `/tmp/vb/visual_probe_v6.sh`
