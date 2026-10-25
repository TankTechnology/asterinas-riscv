# GTK 桌面应用闭包：pcmanfm / eog 起窗

日期：2026-10-25　改动前 HEAD：`f6403f491`
验证内核：`target/prime/kernels/x86.elf`，sha256 前缀 `9e8ee5dd544fa03a`

## 结论摘要

- **pcmanfm：ok，窗口 `0x200002`**；**eog：ok，窗口 `0x400002`**。
- 日常可用矩阵里唯一的 `missing` 项（GTK 桌面应用）改为 **ok**。
- 闭包口径：
  - 历史实测（从最小 Debian trixie 求闭包）：
    `apt-get -s install --no-install-recommends pcmanfm eog` = **118 包**；
  - 本次实际构建：`desktop-drm` 剖面**已含 pcmanfm/GTK3/Xorg**，只补 `eog`
    与日常门禁需要的 `sudo`，包数 **331 → 357（+26）**；apt 取包 **278 条
    / 147.4 MB**（`Get:` 行求和；debootstrap 的 `I: Retrieving` 行不带尺寸），
    镜像 **1.1 GiB**。
- 首次运行两个 GTK 应用都在客户端侧报
  `glx: failed to create DRI3 screen`（进程活着但没有窗口）；补上与 Xorg
  相同的软件渲染环境后双双起窗。

## 1. 闭包来源与口径

`docs/porting/daily-use-report.md` 的 Q2 记录了 118 包的来源：
在 Debian 容器里执行
`apt-get -s install --no-install-recommends pcmanfm eog`，统计 `^Inst` 行。
仓库内没有单独的 `matrix.tsv` 生成脚本，该数字以报告正文为准（`matrix.tsv`
不在仓库中）。

本次不重复“从零装 GTK”：`tools/debian/rootfs/profiles.py` 的 `desktop-drm`
剖面本来就有 `pcmanfm`、GTK3、Xorg。实际增量：

| 口径 | 包数 | 说明 |
|---|---|---|
| 历史最小闭包 | 118 | `pcmanfm eog` 从无到有（报告 Q2） |
| `desktop-drm` 构建前 | 331 | 现有 `target/debian-x86-x11-drm/packages.lock` |
| 构建后 | 357 | `target/debian-x86-gtk2/rootfs/packages.lock` |
| 增量 | **+26** | `eog` 及其依赖 + `sudo` |
| apt 下载 | 278 条 / **147.4 MB** | `Get:` 行求和（`build.log`） |
| 镜像 | **1.1 GiB** | ext2，357 包 |

## 2. 改动（文件:行号）

| 文件:行号 | 改动 |
|---|---|
| `tools/debian/rootfs/profiles.py:316` | `desktop-drm` 的 `requested_packages` 增加 `eog` |
| `tools/debian/rootfs/profiles.py:327` | 同列表增加 `sudo`（日常门禁以 asterinas 执行 `sudo`） |
| `tools/riscv/debian/rootfs/build_rootfs.sh:1201` | 提取 `configure_x86_serial_getty()`（ttyS0 console-getty + 登录 helper） |
| `tools/riscv/debian/rootfs/build_rootfs.sh:1368` | `desktop-drm` 的 amd64 构建应用串口登录面（helper + getty + sudoers） |
| `tools/x86/debian/daily_use_checklist.sh:255` | 应用域：`eog` 用真实 PNG；记录窗口 id（xdotool，缺失时 xwininfo 兜底）；客户端补软件渲染环境 |
| `tools/x86/debian/daily_use_gate.sh:510` | 移除镜像自启的桌面会话 unit，让清单自己拥有 `:0` |

内核无改动；这是镜像/脚本层的构建阶段加包与登录面配置。

## 3. 构建

```sh
sudo tools/riscv/debian/rootfs/build_rootfs.sh \
    --architecture amd64 \
    --profile desktop-drm \
    --output-dir target/debian-x86-gtk2/rootfs
```

- **BUILD_EXIT=0**；测得的镜像为第二次构建（含登录面）：
  日志 birth→mtime `11:59:20 → 12:01:32`，**2m12s**；首次带新包的完整构建
  约 13 分钟；
- 产物：`target/debian-x86-gtk2/rootfs/debian-root.ext2`（1.1 GiB，
  357 包，`eog`/`sudo`/`pcmanfm` 均在位）；
- 内核沿用 `target/prime/kernels/x86.elf`（sha256 `9e8ee5dd544fa03a`）。

## 4. guest 内验证（原始输出）

串口（`target/daily-use/console.txt`）：

```
thinkpad login: asterinas
Password: asterinas
asterinas@thinkpad:/$ sudo sh -c '/usr/local/bin/daily-use-checklist > /dev/console 2>&1'; echo DAILY_SHELL_END
DAILY_DESKTOP=ok Xorg+DRI3
DAILY_APP=ok xterm
DAILY_APP_pcmanfm=ok window=0x200002
DAILY_APP_eog=ok window=0x400002
```

第一次运行（修复前）的原始失败行：

```
DAILY_APP_pcmanfm=fail no-window alive:glx:_failed_to_create_dri3_screen
DAILY_APP_eog=fail no-window alive:glx:_failed_to_create_dri3_screen
```

修复：启动 GTK 客户端时带上与 Xorg 相同的软件渲染变量：

```
env MESA_LOADER_DRIVER_OVERRIDE=kms_swrast LIBGL_ALWAYS_SOFTWARE=1 \
    GALLIUM_DRIVER=softpipe GDK_GL=disable <app> <file>
```

说明：

- 本次运行在应用域（Domain 4b）拿到窗口证据后即停止；清单的稳定性域是
  30 分钟负载，与本任务无关，未执行；
- `DAILY_APT`/`DAILY_CLIPBOARD`/`DAILY_XAPP_*` 显示失败/缺失，是因为
  builder 有意删除了 `/var/lib/apt/lists`，而清单直接 `apt-get install`
  未先 `apt-get update`；`x11-apps`/`xclip` 也不在本剖面。它们**不影响**
  pcmanfm/eog（两者在构建阶段已装好，窗口检测走 x11-utils 的 xwininfo）。

## 5. 判定

- **pcmanfm：ok**（窗口 `0x200002`）
- **eog：ok**（窗口 `0x400002`）
- 矩阵项 **GTK 桌面应用：missing → ok**。

## 6. 后续与限制

1. 运行期 `apt` 域：在清单的 APT 段先执行 `apt-get update`，或在镜像中保留
   package lists，可让 APT/剪贴板/输入法域恢复（与本闭包无关）。
2. `x11-apps`、`xclip`、`xdotool` 若希望清单直接使用，可加入剖面；本轮
   为保持“只做 GTK 闭包”未加。
3. 登录面目前只对 amd64 的 `desktop-drm` 生效；如果需要其他桌面剖面也能
   串口登录，可把同一调用扩展到那些分支。
4. 日常门禁会移除镜像自启的 `asterinas-desktop-drm*` unit；专门的桌面启动
   门禁应保留它们，两者场景不同。

## 7. 复现

```sh
# 镜像（构建阶段加包 + 串口登录面）
sudo tools/riscv/debian/rootfs/build_rootfs.sh \
    --architecture amd64 --profile desktop-drm \
    --output-dir target/debian-x86-gtk2/rootfs

# guest 验证（拿到 DAILY_APP_* 后可提前停止，稳定性域为 30 分钟）
make test_x86_daily_use \
    X86_DEBIAN_KERNEL=$PWD/target/prime/kernels/x86.elf \
    X86_DEBIAN_STAGE1_INITRAMFS=$PWD/target/debian-x86/stage1/initramfs.cpio \
    X86_DEBIAN_XORG_BASE_ROOT=$PWD/target/debian-x86-gtk2/rootfs/debian-root.ext2 \
    X86_DEBIAN_XORG_ACCEL=kvm X86_DEBIAN_XORG_TIMEOUT=1800
```

证据：`target/daily-use/console.txt`、`target/daily-use/host.log`；
构建日志 `target/debian-x86-gtk2/build.log`。
