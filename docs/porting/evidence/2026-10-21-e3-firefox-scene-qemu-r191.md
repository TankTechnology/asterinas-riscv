# E3 QEMU Firefox 场景门禁通过（r191，2026-10-21）

## 结论

- Firefox ESR 在 Asterinas RISC-V QEMU guest 上**正常启动**，Marionette
  NewSession 通过；
- 官方固定场景 `file:///usr/share/asterinas/firefox-drm-scene.html`
  完成加载与首帧渲染，**WebGL 上下文创建成功**；
- 截图、帧样本（32 帧）、停止后**恢复重启并再次 NewSession** 全部通过；
- 场景分类器与台架生命周期结果均为 `passed=true`。

## 输入

| 项 | 值 |
|---|---|
| 内核 | `9998ecb2428c601dfc22986da10328899024abc676772a4a2e254edf9a8997d6` |
| 根镜像 | `78febb53b17de76894dbdc653ef1bec03d01c89e90937d9d6e387036c74594ab` |
| DTB（4 GiB `/memory`） | `d4784ac20ee04cca88660544961f05971ea98d0224f01dabbe3f0b984c61d715` |
| QEMU | 10.2.1，`egl-headless,rendernode=/dev/dri/renderD129`，`-m 4G`，SMP=4 |
| GEM 池 | `asterinas.dri_pool_mb=512` |
| Firefox | `firefox-esr 140.16.0esr-1~deb13u1`（packages.lock sha256 `9d5fd036…`） |
| 台架命令 | `desktop_m4_drm_gate.py`，`ASTERINAS_QEMU_MEMORY=4G`，`--boot-timeout 1140` |

## 关键串口收据

```text
FIREFOX_DRM_BOUNDARY name=new-session status=pass elapsed_ms=282363.5
FIREFOX_DRM_SCENE_STATE location=scene ready=complete dom_marker=started \
  dom_done=1 dom_samples=32 webgl_context=1 webgl_debug=1 renderer=Generic_Renderer
FIREFOX_DRM_VIEWPORT width=1280 height=696 dpr=1
FIREFOX_DRM_BROWSER_EVIDENCE webrender=1 virgl=1 swgl=0 llvmpipe=0 softpipe=0
FIREFOX_DRM_SUPPORT compositing=WebRender webrender=available renderer=virgl source=browser-log
FIREFOX_DRM_GL renderer=Generic_Renderer
FIREFOX_DRM_SCREENSHOT width=1280 height=696 \
  sha256=e58042a85b9cf4ad16e9ff99ab879a084203c510da2741b77e9a564342d938e8
FIREFOX_DRM_FRAMES samples=32 min_ms=19.000 p50_ms=21.000 p95_ms=61.000 max_ms=4014.000
FIREFOX_DRM_RECOVERY restart=pass
```

场景分类器输出：

```json
{"p95_ms": 61.0, "package": "firefox-esr",
 "package_version": "140.16.0esr-1~deb13u1",
 "packages_lock_sha256": "9d5fd036dfae9b113a218d4d522e8e22158074ef18efb599c2be69b0ef1970c6",
 "passed": true, "reason": "passed", "renderer": "virgl", "samples": 32,
 "screenshot_sha256": "e58042a85b9cf4ad16e9ff99ab879a084203c510da2741b77e9a564342d938e8"}
```

台架 `result.json`：`passed=true`，`reason=pass`，
`final_root_sha256=5a4a5d8c842315c03665a3142d9a28da0b8df3b8a06f59874615cb2a4913a919`。

## 关键修复

1. `VIRTGPU_RESOURCE_INFO` 的 UAPI 方向（commit `4eabcfca8`）：
   Linux 语义是输入 `bo_handle`、输出 `res_handle`；原实现反向，导致 Mesa
   跨进程导入 dma-buf 失败并报 `GL_OUT_OF_MEMORY`，进而 DRI3 pixmap
   BadAlloc、Xorg glamor FBO 分配失败、WebRender OOM。这是场景首帧失败的根因。
2. SCM_RIGHTS 的 dma-buf/fence leaf 分类（`28a15360d`）：解除 X11 DRI3
   传递 dma-buf fd 的硬死锁。
3. virtio-gpu `present_framebuffer` 自旋锁作用域（`28a15360d`）：避免第二次
   present 的 `MIGHT_SLEEP_VIOLATION`。
4. 门禁链路修复：stderr 取证（release 构建无 NSPR 日志）、CORE 里程碑、
   `pageLoadStrategy=none`、恢复阶段用 NewSession、停止容忍未回收僵尸、
   `hash_final_root` 的 `del prepared` 缺陷（`615d41cc3`/`3fea08188`/`66f137a1b`）。

## 确定性

r189 与 r191 两次独立运行得到**相同的截图 sha256**
（`e58042a8…`）与相同的 32 帧 DOM 采样，说明固定场景在该配置下可重复。
（窗口内容视口在 1278×690 与 1280×696 之间随 GTK resize 时序略有变化，
门禁以页面自报的 `innerWidth/innerHeight` 为准。）

## 局限

- **4 GiB guest 必须配 4 GiB `/memory` 的 DTB**；`-m 4G` 配 2 GiB DTB 会让
  U-Boot 把 FDT 放到内核声明的 RAM 之外，内核在 early boot 无输出挂起。
- 该场景依赖 virtio-gpu + virgl（`renderer=virgl`）。Megrez 真机的
  PowerVR/原生显示路径没有 virgl，因此本场景收据**不能**直接在板子上复现；
  板测对应的目标是 Firefox 正常启动/桌面就绪，需按板级载具另行验证。
