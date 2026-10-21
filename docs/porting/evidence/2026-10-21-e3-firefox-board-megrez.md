# E3 Firefox Megrez 真机桌面就绪（r4，2026-10-21）

## 结论

- Asterinas RISC-V 在 Milk-V Megrez 上启动新的 browser-web 根镜像，
  用 fbdev/无输入/离线的 minimal profile 进入完整桌面就绪序列；
- Firefox ESR **153.5.0esr** 在物理 1920×1080 显示面上启动，加载本地场景
  `file:///usr/share/asterinas/physical-graphics/index.html`，窗口为
  `1280×972 "Mozilla Firefox"`；
- 非ce 框架的只读 admission 探针通过：`firefox_pid=98`、`uid=1000`、
  `visible_windows=1`、`local_page=1`、`x11_socket=1`、`watchdog=0`；
- 门禁 `status=pass`、`reason=desktop-ready`，generation
  `997c196e6410ad29…`，`display-ready` 在 295.0 s 出现，内核看门狗窗口
  600 s（余量约 305 s）。

这是**板级 Firefox 启动/桌面就绪**收据，不是 WebGL/WebRender 收据：板级
fbdev 路径使用软件渲染，virgl 证明见
[E3 QEMU 场景收据](2026-10-21-e3-firefox-scene-qemu-r191.md)。

## 输入

| 项 | 值 |
|---|---|
| 内核 | `a715ae3af6802ed9399e91c881021539ab87f4a33e7218a98d60ef201753f6dc`（merged main + DRI 修复） |
| 根镜像 | `440443f25c80baba890affbf1e2b0c027be0650b7d3fc9c3b5a833dc32bac9a0`（`browser-web-board-r1`，2 GiB） |
| Debian / Firefox | Debian 13.7；`firefox-esr 153.5.0esr-1~deb13u1` |
| Stage1（r4） | `7e2b8fbb6da34b73859e42b9fa7256c853bab08337f683094315b399cd60e942`，CRC32 `bb61260d`，1,114,112 字节 |
| DTB | `465cb129333cc334a3161fabb43d37df8738ac4baa86fcdb50260691a22a84ba` |
| Generation | `997c196e6410ad29caf474bb0e9269b888730cbbcdfdbf02bac0aabce624295d` |
| Boot 预算 | `asterinas.reboot_after=600`；readiness deadline = watchdog − 120 s |
| 设备 profile | `minimal`：两个 GMAC、两个 USB wrapper 均禁用；离线本地页 |
| 板型 | Milk-V Megrez / EIC7700，16 GiB DRAM，4 hart |

## 根因与修复

1. **板上的 p2 根文件系统是旧的 desktop-m4 镜像**。现场检查
   `/etc/systemd/system` 只有 `asterinas-desktop-m4*.service`，
   `command -v firefox` 为空；旧镜像也没有 offline 就绪脚本。因此旧板测
   必然在 `reason=browser-start` 停住。本次用 Asterinas LAN installer 重装
   `browser-web-board-r1`，安装器逐 32 MiB chunk 写入并回读校验：

   ```text
   DEBIAN_INSTALL_RESUME_START chunks=64 block_bytes=1048576
   ...
   DEBIAN_INSTALL_PASS sha256=440443f25c80baba890affbf1e2b0c027be0650b7d3fc9c3b5a833dc32bac9a0 bytes=2147483648
   ```

   （安装由 Asterinas 内核执行，未由 Linux 写入 p2；引导脚本经 RockOS
   p3 暂存，主机经 10.100.19.216:8080 提供分块。）

2. **Megrez 的 systemd handoff 不把 `systemd.setenv=` 导入 unit 环境**。
   `/proc/1/environ` 有 `ASTERINAS_DESKTOP_OFFLINE=1`，但
   `systemctl show-environment` 只有 `LANG`/`PATH`；readiness 服务因此把
   offline 当成 0，走 online 路径等待键盘/鼠标输入，约 128 s 后报
   `ASTERINAS_DESKTOP_BOOT_FAIL reason=browser-start`。修复
   `f05894d26`：readiness 脚本从 `/proc/cmdline` 读取
   `ASTERINAS_DESKTOP_OFFLINE`、`ASTERINAS_WEB_NETWORK_MODE`、
   `ASTERINAS_DESKTOP_START_URL`、`ASTERINAS_BROWSER_WEB_SESSION`、
   `ASTERINAS_BROWSER_WEB_BASIC_ONLY`，并在启动 desktop/browser unit 前用
   `systemctl set-environment` 发布给 service manager。

3. **首次通过时余量过小**。r3（watchdog 300 s）的 `display-ready` 在
   290.7 s，距恢复重启只有 9 s。修复 `d94a6d880`：板级 boot 预算改为
   600 s，readiness deadline 由 `watchdog − 120 s` 推导；主机 marker 等待
   同步放宽到 540 s。r4 在 295.0 s 就绪，余量约 305 s。

## 有序串口收据（r4）

```text
ASTERINAS_DESKTOP_DISPLAY_READY
ASTERINAS_DESKTOP_FIREFOX_READY pid=98 user=1000
ASTERINAS_DESKTOP_WATCHDOG_DISARMED
ASTERINAS_DESKTOP_BOOT_READY firefox_pid=98
__ASTERINAS_DESKTOP_ADMISSION_492ca0f7be8c5284__ boot_id=eece68b1-fd49-4169-9a83-05a9b19acdad \
  firefox_pid=98 firefox_uid=1000 visible_windows=1 watchdog=0 local_page=1 \
  debug_console=1 x11_socket=1 \
  __ASTERINAS_DESKTOP_ADMISSION_END_492ca0f7be8c5284__
```

阶段时间（`start-r4/result.json`）：

| 阶段 | 秒 |
|---|---|
| kernel-entered | 1.284 |
| stage1-start | 12.988 |
| root-found | 13.063 |
| root-handoff | 13.380 |
| debug-console-ready | 35.447 |
| display-ready | 295.036 |
| firefox-ready | 295.036 |
| kernel-watchdog-disarmed | 295.036 |
| guest-watchdog-disarmed | 295.329 |
| desktop-ready | 295.329 |

门禁 `result.json`：`status=pass`，`reason=desktop-ready`，
`firefox_pid=98`，`firefox_uid=1000`，`visible_windows=1`，`watchdog=0`，
`local_page=true`，`x11_socket=true`，`debug_console=true`，
`boot_id=eece68b1-fd49-4169-9a83-05a9b19acdad`，`elapsed_seconds=352.3`。

人工检查（同一代的交互式 boot）：`xdotool getwindowgeometry` 返回
`1280×972 "Mozilla Firefox"`；`/home/asterinas/browser-web-timeline.log`
含 `BOOT_NETWORK_READY`、`BOOT_X_SOCKET_READY`、`BOOT_FIREFOX_WRAPPER_START`、
`BOOT_FIREFOX_EXEC`；Xorg 以 `-configdir
/run/asterinas-physical-display-providers/fbdev/xorg.conf.d` 在 fbdev 上运行。

## 证据路径（raw，未跟踪）

- `target/megrez-root-install-20261021-r1/installer.watch.serial.log`
  （安装 PASS 与 chunk 记录）
- `target/megrez-desktop-boot/start-r4/serial.log`、`result.json`
- `target/megrez-desktop-boot/prepare-r4/publication.serial.log`
- `target/megrez-desktop-boot/start-r3/result.json`（首次通过，
  290.7 s、9 s 余量）

## 局限

- minimal profile 禁用两个 GMAC 与两个 USB wrapper；本结果证明离线本地
  场景的启动/显示就绪，不证明联网浏览或输入设备路径。
- fbdev profile 设置 `MOZ_AVOID_OPENGL_ALTOGETHER=1`，不声明 WebRender/GL；
  QEMU virgl 场景收据另见上文链接。
- 板子保留在桌面运行状态，两个看门狗均已 disarm；未执行 `saveenv`，
  未修改持久 U-Boot 环境。
