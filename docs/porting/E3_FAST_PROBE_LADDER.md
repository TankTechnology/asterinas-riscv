# E3 快速定位阶梯（替代整链 180s 探针）

> 目的：不再用"一个大场景跑 10 分钟"的方式定位。每一步都短（秒级～30s）、独立、带本地标记与日志，先拿到局部事实再往上叠。

## 已有事实（不要重复）
- glxtest：`RENDERER virgl`、`/dev/dri/renderD128` ✓
- Marionette greeting ✓；NewSession 请求 66B 到达 2828 端口 ✓
- 场景 DOM 跑到了（`dom_done=1 samples=32`），但 **WebGL context=0、WebRender compositor 证据缺失**：
  - `FIREFOX_DRM_FAIL_REASON reason=Firefox_WebGL_renderer_was_not_reported_context=0`
  - `reason=Firefox_browser_log_has_no_WebRender_compositor_evidence`
  - 有些轮次 `WebGL renderer is not virgl: Generic Renderer`

## 阶梯（按顺序，每步独立收据）
| 步 | 内容 | 预算 | 判据 |
|---|---|---|---|
| **L0** | `firefox --version`、profile/prefs 是否存在、`MOZ_LOG` 环境是否生效 | ≤10s | 二进制与 profile 正常 |
| **L1** | `glxtest`（已有）复核 renderer/EGL/device | ≤10s | renderer=virgl、renderD128 |
| **L2** | **headless WebRender 冒烟**：`MOZ_LOG=WebRender:5,Compositor:5,GLContext:5 firefox --headless --screenshot /tmp/l2.png 'data:text/html,<canvas id=c></canvas><script>document.title=!!document.getElementById("c").getContext("webgl")</script>'` | ≤30s | 退出码 0 + 截图非空 + 日志里出现 WebRender/Compositor 初始化行 |
| **L3** | Marionette greeting only | ≤20s | greeting marker |
| **L4** | NewSession only（现有短探针） | ≤20s | 响应帧出现 |
| **L5** | 简单页面（data:）加载 + 截图 | ≤30s | 截图非空 |
| **L6** | 固定 E3 场景（仅当 L2–L5 都过） | ≤120s | 场景像素哈希 |

**先做 L0→L2**：如果 L2 能在 30 秒内确认 WebRender 是否能初始化，就不用再等 180s 整链；把 L2 的 log 与截图作为 r141 收据。

## 日志开关（关键）
在 guest 启动 Firefox 前设置：
```sh
export MOZ_LOG="WebRender:5,Compositor:5,GLContext:5,gfx:5"
export MOZ_LOG_FILE=/tmp/firefox-moz.log
export RUST_LOG="webrender=debug"
```
然后 L2/L3/L4 各自只看本步的 log 片段，不做全量扫描。

## 纪律
- 每步 ≤30s（L6 除外），失败即记录并停，不连环重试
- 每步写收据：`2026-10-21-e3-ladder-L{0..6}.json`，含 exit code / 耗时 / 关键 marker / log tail
- r24 的结果保留；L2 与 r24 互补（r24 关 WebRender 看启动，L2 开 WebRender 看初始化）
