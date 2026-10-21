# P6 前期准备：Firefox WebRender over PowerVR（2026-10-23）

## 0. 本轮取证范围

- 板子挂死，**没有**在 RockOS 上实际读取 Firefox 的 `about:support`；
  本文把“已从镜像/libxul 读到的事实”和“仍需板上确认的未知项”分开；
- 已检查本地 guest 镜像
  `target/debian-riscv/browser-web-board-r1/debian-root.ext2`：
  Firefox ESR **153.5.0**（BuildID `20261006170429`，
  `mozilla-esr153`），`libxul.so` 中存在 WebRender/EGL 相关字符串；
- 已检查 RockOS GPU 包配置：Xorg 用 `modesetting` +
  `kmsdev=/dev/dri/card0`，GPU 包 changelog 明确修过
  “firefox gpu shader”。

## 1. Firefox WebRender 走 PowerVR 的硬条件

### 1.1 已从 `libxul.so` 确认存在的扩展检查

EGL（字符串在 libxul 中实际出现）：

- `EGL_KHR_image_base`
- `EGL_KHR_image` / `EGL_KHR_image_pixmap` /
  `EGL_KHR_gl_texture_2D_image`
- `EGL_EXT_image_dma_buf_import`
- `EGL_EXT_image_dma_buf_import_modifiers`
- `EGL_KHR_fence_sync`
- `EGL_ANDROID_native_fence_sync`
- `EGL_EXT_platform_device`
- `EGL_MESA_platform_surfaceless`
- `EGL_KHR_create_context` / `EGL_EXT_create_context_robustness`
- `EGL_EXT_buffer_age` / `EGL_EXT_swap_buffers_with_damage`

GL：

- `GL_OES_EGL_image`
- `GL_EXT_texture_format_BGRA8888`
- `GL_OES_rgb8_rgba8`
- `GL_EXT_framebuffer_object`

### 1.2 仍未在字符串中直接确认、需要运行时验证的项（TODO）

以下扩展是 EGL/GL 平台常见要求，但本轮**没有**在 `libxul.so` 中找到
精确字符串，不能当成已确认：

- `EGL_KHR_platform_gbm` / `EGL_EXT_platform_base`：**TODO**。如果
  Firefox 在 X11 上走 GBM/EGL 平台，需要运行时用 `eglQueryString`
  或 Xorg/Firefox 日志确认；如果走 X11 EGL 平台，则不一定需要。
- GL/GLES 版本下限：**TODO**。WebRender 的 GL 后端通常要求
  GLES 3.0 或桌面 GL 3.3 级别；需在 guest 里用
  `glGetString(GL_VERSION)` / `eglconfigs` 确认。
- `gfx.webrender.*` 的默认值：**TODO**。`libxul` 里能看到
  `gfx.webrender.all`、`.software`、`.software.opengl`、
  `.fallback.software`、`.fallback.software.requires-gpu-process`、
  `.reject-software-driver`、`.compositor` 等开关字符串，但没有
  本轮可确认的默认值表；需要在 guest 的 `about:support` 读取。
- `MOZ_X11_EGL`：字符串存在；是否默认启用、RockOS 启动脚本是否设置
  **TODO**（板上 `about:support` 或环境变量确认）。

### 1.3 DRI3 / Present

- 本轮在 `libxul.so` 中**没有找到** `DRI3` 或 `Present` 字符串；
  这**不能**证明不需要，只能说明 Firefox 不直接以这些名字探测；
- WebRender 的 EGL 路径主要依赖 dmabuf/fence；DRI3/Present 更可能
  由 Xorg glamor/DRI3 或合成器路径间接需要；
- 结论：DRI3/Present 列为**条件性依赖**，在 P6 验证步骤中单独探测，
  不阻塞 P5b/P5c。

### 1.4 依赖顺序

```text
P5b: EGL 首次接触（eglconfigs / eglInitialize）
  → P5c: Xorg glamor（modesetting + EGL/GBM）
    → DRI3/Present（若 Firefox/合成器路径需要；条件性）
      → Firefox WebRender（EGL/GL 后端）
```

## 2. RockOS 侧的实际路径（已知/未知）

**已知**：

- RockOS 的 Xorg 配置使用 `Driver "modesetting"`，
  `Option "kmsdev" "/dev/dri/card0"`；
- RockOS GPU 包提供 Mesa 22.0.3 的 `pvr_dri.so`、DDK 24.2 的
  `libpvr_dri_support.so`/`libsrv_um.so`/`libusc.so`；
- 包 changelog 有 “fix firefox gpu shader”，说明厂商栈以 Firefox
  为实际目标；
- Firefox ESR 153.5.0 的 `libxul.so` 包含上节的 EGL/GL 扩展检查。

**未知（TODO，等板上确认）**：

- RockOS 上 Firefox 的版本是否与本地 guest 镜像一致；
- `about:support` 里 “Compositing” 是否为 WebRender、
  “WebRender” 后端是否为 OpenGL/EGL（而非 Software）；
- Firefox 实际使用 GLX 还是 EGL（`MOZ_X11_EGL` 是否设置）；
- glamor 在 RockOS 上是否真的走 EGL（Xorg 日志中的
  “glamor: EGL version”）。

## 3. P6 验证步骤、判据与 marker

| 步骤 | 命令/观察点 | 成功判据 | marker |
|---|---|---|---|
| 1. EGL | `eglconfigs`（P5b） | 进程退出 0，打印 EGL configs/版本 | `ASTERINAS_POWERVR_UMD status=ok stage=egl` |
| 2. Xorg glamor | P5c `p5c-guest.sh`，查看 `/tmp/xorg-pvr.log` | X socket 就绪，日志含 glamor EGL | `ASTERINAS_POWERVR_XORG status=ok backend=glamor` |
| 3. DRI3/Present | `xdpyinfo -display :99 \| grep -i present`、`xrandr --listproviders` | Present/DRI3 扩展存在（若 WebRender 需要） | `ASTERINAS_POWERVR_DRI3 status=ok\|failed reason=...` |
| 4. WebRender | 启动 Firefox（`MOZ_X11_EGL=1`，`gfx.webrender.all=true`），读 `about:support` | Compositing=WebRender；WebRender 后端为 OpenGL/EGL，非 software；测试页可见 | `ASTERINAS_POWERVR_WEBRENDER status=ok backend=gl\|failed reason=...` |

**WebRender 失败时的 reason 分类**（供脚本填写）：

- `reason=no-egl`（P5b 未过）
- `reason=no-glamor`（P5c 未过）
- `reason=no-dri3`（若确认需要 Present/DRI3）
- `reason=egl-ext-missing`（缺少上节硬性扩展）
- `reason=gl-version`（GL/GLES 版本不足）
- `reason=webrender-software`（Firefox 回退到 Software WebRender）
- `reason=firefox-start`（Firefox 本身未启动）

## 4. 最小验证脚本骨架

`tools/riscv/p6/p6-guest.sh` 只做骨架：检查 P5b/P5c 前置、设置
WebRender 相关环境、启动 Firefox 并收集日志；具体 `about:support`
解析和 marker 判据留 TODO，等板子恢复后补实。

## 5. 已知事实 vs 未知项汇总

| 类别 | 内容 |
|---|---|
| 已确认 | Firefox ESR 153.5.0；libxul 含 `EGL_KHR_image_base`、`EGL_EXT_image_dma_buf_import(_modifiers)`、`EGL_KHR_fence_sync`、`EGL_ANDROID_native_fence_sync`、`EGL_EXT_platform_device`、`EGL_MESA_platform_surfaceless`、`GL_OES_EGL_image`、`GL_EXT_texture_format_BGRA8888`、`GL_OES_rgb8_rgba8`；`MOZ_X11_EGL` 字符串存在；RockOS Xorg 用 modesetting/card0；厂商包修过 Firefox shader |
| TODO | `EGL_KHR_platform_gbm`/`EGL_EXT_platform_base` 是否实际需要；GL 版本下限；`gfx.webrender.*` 默认值；DRI3/Present 是否硬依赖；RockOS Firefox 版本与 `about:support`；RockOS 实际 GLX/EGL 路径 |

## 6. 边界

- 本轮不碰板子、不改内核/initramfs；
- 未把任何 TODO 当成已确认事实；P6 的通过必须以板上
  `about:support` + 可见帧 + marker 为准。
