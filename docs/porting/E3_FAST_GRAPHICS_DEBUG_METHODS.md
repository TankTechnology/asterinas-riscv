# E3 快速图形调试方法（不跑完整 Firefox）

> 目的：记录在 Asterinas RISC-V guest 里调试 Firefox 图形栈时，可以替代
> “完整启动 Firefox + Marionette 全链路”的高效方法，供后续轮次直接取用。
> 本文是方法记录，不是门禁收据；验收仍以 `firefox_drm_scene_gate` 为准。

## 为什么需要替代路径

完整 E3 场景门禁一轮约 10 分钟，成本几乎全部来自冷启动：

| 阶段 | 典型耗时（TCG/QEMU） |
|---|---|
| guest 启动到桌面 | 60–90s |
| Firefox 冷启动 + NewSession | 250–300s |
| 场景 Navigate/首帧 | 2–300s（失败时卡满 300s 页面加载上限） |
| 恢复重启 | 另一次启动 |

而当前要定位的失败位于 **WebRender → Mesa virgl → Asterinas DRM** 层，
不需要每次重建完整浏览器会话。

## 1. Firefox 自带测试体系：为什么不能直接用

- `xpcshell` / `mochitest` / `gtest` / `reftest` / `wpt` 都要求**从源码构建的
  objdir**，由 `mach` 驱动；Debian 的 `firefox-esr` 发行二进制没有这些 harness。
- 这些套件以浏览器内部逻辑（JS、DOM、网络、UI）为主，不覆盖本次失败的
  GL 驱动/内核路径。
- WebRender 自带的 `wrench`/reftest 是 host 侧工具，测不到 Asterinas guest
  的 DRM/virgl。
- 在 TCG guest 里跑同一套 harness，启动成本并不低于完整 Firefox。

结论：没有“拿来即用”的 Firefox 单元测试捷径；需要构造本 guest 内的短探针。

## 2. 方法 A：Firefox headless 截图（首选）

不需要 Marionette、不需要 Xorg，但仍走 WebRender + EGL + virgl 的纹理/资源
分配路径，正好覆盖 `GL_OUT_OF_MEMORY` 这一类失败。

### A1. 30 秒冒烟（WebRender 能否初始化）

```sh
MOZ_LOG="WebRender:5,Compositor:5,GLContext:5,gfx:5" \
MOZ_LOG_FILE=/tmp/moz-l2.log \
firefox-esr --headless --window-size=1280,800 \
  --screenshot /tmp/l2.png \
  'data:text/html,<canvas id=c width=256 height=256></canvas><script>document.title=String(!!document.getElementById("c").getContext("webgl"))</script>'
```

判据：退出码 0、截图非空、日志出现 WebRender/Compositor 初始化行。
r141 的 L2 已按此路径验证（无 WebRender 初始化证据时能在 30s 内判定）。

### A2. 固定场景截图（更接近真实负载）

```sh
firefox-esr --headless --window-size=1280,800 \
  --screenshot /tmp/scene.png \
  file:///usr/share/asterinas/firefox-drm-scene.html
```

r141 L2b 实测：约 93.5s（含启动）产出 20 KB 截图，日志包含
`WebRender - OpenGL version new 3.2`。同一轮耗时约为完整门禁的 1/6。

### 局限

- 走 headless 合成器，不覆盖 X11/glamor/DRI3、合成器子进程、present 路径。
- 因此只能用于快速判定“GL/资源分配层是否复现”，不能替代最终场景门禁。

## 3. 方法 B：自写 EGL/GLES 分配压力探针

目的：最小复现 WebRender 的 GL 资源分配失败，秒级出结果，并可长期作为
fast ladder 的 L3 回归探针。

实现要点：

- C + `dlopen("libEGL.so.1")` / `dlopen("libGLESv2.so.2")`，自声明所需
  typedef/常量，**不依赖 guest 内的开发头文件**。
- EGL 平台用 gbm（guest 的 `FIREFOX_DRM_EGL_PROBE` 已证明
  `display=gbm renderer='virgl'` 可用），设备节点 `/dev/dri/renderD128`。
- 循环创建 N 张 W×H 纹理、FBO/附件、每次上传数据并 `glGetError()`；
  参数化纹理数量、尺寸、循环次数，输出串口 marker：

```text
GLSTRESS begin textures=32 width=1152 height=584
GLSTRESS error=0x0505 at iteration=7 texture=13
GLSTRESS done ok=0 frames=32
```

- 部署方式：与 `firefox_scene_blank.service` 相同的 overlay + systemd unit，
  由串口采集 marker；一轮 = 启动 60–80s + 探针数秒。
- 交叉编译：容器 `asterinas-qemu-gl-runtime` 内已有
  `riscv64-linux-gnu-gcc`，并已安装 `libc6-dev-riscv64-cross`（2026-10-21）。
  编译示例：

```sh
riscv64-linux-gnu-gcc -O2 -Wall -Wextra -o glstress glstress.c -ldl
```

快速二分维度：GEM 池大小（`asterinas.dri_pool_mb`）、guest 内存
（`ASTERINAS_QEMU_MEMORY`）、纹理数量/尺寸、是否启用 mipmap/多重采样。

局限：探针的分配模式是人工构造的，可能复现不了 WebRender 特有的组合；
需要先用方法 A 校准，再决定探针是否足够代表。

## 4. 方法 C：单会话批量实验

Marionette 会话建立后，`ExecuteScript` 通常只需 0.1–0.5s；把多组实验写进
一个场景页面或一条脚本，一次会话内跑完，摊薄约 300s 的 NewSession 成本。

适合：

- WebGL 上下文参数矩阵（`alpha`/`antialias`/`preserveDrawingBuffer` 等）；
- 纹理尺寸/数量阈值探测、context lost 行为、renderer 字符串读取；
- 同一会话内的多个 `Navigate()`（本地 file:// 页面切换）。

不适合：

- 需要重启浏览器或 GPU/合成器进程才能暴露的问题。本项目观察到的
  “CompositorBridgeChild AbnormalShutdown → 主进程退出/SIGSEGV”属于此类，
  仍需要多次冷启动。

## 5. 方法 D：Mesa / GL 观测开关

| 开关 | 作用 | 注意 |
|---|---|---|
| `MESA_DEBUG=1` | Mesa 错误/警告输出到 stderr | 低开销，可常开 |
| `LIBGL_DEBUG=verbose` | EGL/驱动加载与回退路径 | 定位“实际用了哪个驱动” |
| `EGL_LOG_LEVEL=debug` | EGL 配置/回退的详细输出 | 输出量大，按需开 |
| `GALLIUM_TRACE=/tmp/gl.trace` | 抓取真实 GL 调用序列 | 会引入 trace 驱动，改变调用栈；只在定位具体调用时开 |
| `MOZ_LOG=...WebRender:5` | WebRender 内部渲染日志 | **Debian ESR release 构建把 NSPR logging 编译掉了**，本 guest 实测 `MOZ_LOG_FILE` 恒为空；不要依赖它取证，改用浏览器 stderr 的 crash annotation（`RenderThread`/`NewRenderer`/`wr_renderer`） |

host 侧同时观察 QEMU `-display egl-headless` 的 stderr 与 virglrenderer
日志；若怀疑 host 资源限制，可对比不同 render node（当前
`/dev/dri/renderD129` 为 512 MiB VRAM 的 AMD iGPU）。

## 6. 与现有阶梯的关系

在 `E3_FAST_PROBE_LADDER.md` 的 L0/L1 之后，建议扩展为：

| 步 | 内容 | 预算 |
|---|---|---|
| L2a | headless `data:` 冒烟（方法 A1） | ≤30s |
| L2b | headless 固定场景截图（方法 A2） | ≤120s |
| L3 | EGL/GLES 分配压力探针（方法 B） | boot+秒级 |
| L4+ | Marionette greeting/NewSession/场景（现有） | 现有预算 |

纪律不变：每步独立收据、失败即停、不连环重试；完整场景门禁只在本地事实
齐全后运行。

## 7. 当前状态（2026-10-21）

- 已定位并修复：`GL_OUT_OF_MEMORY` 的根因是内核
  `VIRTGPU_RESOURCE_INFO` 的 UAPI 方向写反（输入 `bo_handle`、输出
  `res_handle`），导致 Mesa 跨进程导入 dma-buf 时收到 `EINVAL`。
  修复后场景/WebGL 直接跑通（r183）。
- 经验：`dri_trace_fail` 的拒绝日志最初被 `?`/`return_errno!` 的早退绕过，
  必须在闭包里分发 ioctl 才能记录；否则“没有拒绝”会误导诊断。
- 方法 B（EGL/GLES 压力探针）仍值得补：下一次图形回归可以在分钟级
  复现，而不必等 Firefox 冷启动。
