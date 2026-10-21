# P5b/P5c/P6 脚本静态审查（2026-10-23）

## 范围

不碰板子、不改内核/initramfs，只审查：

- `tools/riscv/p5b/p5b-stage.sh`
- `tools/riscv/p5b/p5b-guest.sh`
- `tools/riscv/p5c/p5c-stage.sh`
- `tools/riscv/p5c/p5c-guest.sh`
- `tools/riscv/p6/p6-guest.sh`

`shellcheck` 在宿主和构建容器里都未安装（`shellcheck` 不可用），
改用 `bash -n` / `sh -n` 语法检查加人工逐项审查；下方“已知限制”
记录了这个缺口。

## 发现与修正

### 共性

1. **破坏性 staging**：p5b/p5c 的 `p5b-stage.sh`/`p5c-stage.sh` 原来用
   `ssh ... "rm -rf '$DEST' && mkdir -p '$DEST'"`，重复执行会清空目标
   目录。改为 `mkdir -p` + `scp -r` 覆盖，保留既有文件和备份。
2. **缺少超时**：所有 `ssh`/`scp`/`tar`/`sha256sum` 以及 guest 的
   Firefox 启动都补了 `timeout`（30/60/120/300 s），避免板子 bus 挂住
   时无限等待。
3. **guest 脚本的 shell 选项不一致**：统一为
   `set -eu`（p5b/p5c guest）或 `set -u`（p6 骨架，保留后续解析
   TODO）；所有预期非零的命令都用 `|| rc=$?` / `|| true` 显式处理。
4. **备份逻辑**：`/opt/pvr` 和 Xorg 配置改为**首次运行才备份**为
   `.p5b.bak` / `.p5c.bak`，重复执行不会用“已安装版本”覆盖原始备份。
5. **库路径**：
   - `LD_LIBRARY_PATH` 保留调用者已有值；
   - `LIBGL_DRIVERS_PATH` 由 `/opt/pvr/dri` 加默认
     `/usr/lib/riscv64-linux-gnu/dri`，避免只设 PVR 目录导致 guest
     自带 Mesa 组件不可见；
   - `MESA_LOADER_DRIVER_OVERRIDE=pvr` 保持。
6. **SHA 校验顺序**：
   - 宿主脚本在 scp 前校验 `SHA256SUMS`；
   - p5c guest 在解包/复制前对 `$SRC` 再校验一次
     （`SHA256SUMS` 的路径与 `$SRC/opt`、`$SRC/etc` 布局一致）；
   - p5b 的就绪包 SHA 清单按 `opt/pvr/...` 布局生成，而 guest 目标
     是 `/home/debian/pvr/{bin,lib,dri}`，两者路径不同，所以 p5b guest
     **不做**二次 `sha256sum -c`，只由宿主校验 + scp 完整性保证；
     这是有意的取舍，记录在此。
7. **路径一致性**：
   - P5b 包 `opt/pvr/{bin,lib,dri}` → board `/home/debian/pvr/{bin,lib,dri}`
     → guest `/opt/pvr`；
   - P5c 包 `opt/pvr` + `etc` → board `/home/debian/pvr-p5c/opt|etc`
     → guest 同样布局；
   - HTTP 回退的 tar 路径与 guest 传入的 `SRC` 一致。
8. **进程收尾**：p5c 的 Xorg 用 `stop_xorg()` 先 TERM、0.5 s 后
   KILL；p6 的 Firefox 用 `timeout 30` 启动并在标记后 TERM/KILL。
9. **工具检查**：guest 脚本先检查 `timeout`、`Xorg`、`firefox` 是否
   存在，缺失时打印带 `reason=` 的失败 marker，而不是静默退出。

### 逐脚本

| 脚本 | 主要修正 |
|---|---|
| `p5b-stage.sh` | 去 `rm -rf`；ssh/scp/tar/sha 超时；`SSH_OPTS` 统一 |
| `p5b-guest.sh` | `set -eu`；首次备份；超时检查；默认 DRI 路径；rc 显式处理 |
| `p5c-stage.sh` | 去 `rm -rf`；超时；检查 `powervr.ini` 和 glamor 配置；远端校验 |
| `p5c-guest.sh` | `set -eu`；guest SHA 校验；首次备份；Xorg TERM/KILL；配置备份 |
| `p6-guest.sh` | `set -u`；检查 firefox/timeout；`timeout 30`；TERM/KILL；默认 DRI 路径 |

## 已知限制（未在本轮修）

- `shellcheck` 不可用，只有 `bash -n`/`sh -n`；静态 shellcheck 级别的
  引号/SC2086 类问题未工具化检查。
- p5b guest 不二次校验 SHA（原因见上，路径布局不同）。
- p5c 的 glamor 成功判据是 Xorg 日志正则
  `glamor.*(enabled|EGL)`，这是启发式；板上以实际 Xorg 日志为准。
- p6 仍是骨架：`about:support` 的合成器/WebRender 后端解析为
  `TODO`，目前会打印 `status=failed reason=todo-about-support-unparsed`，
  不应被误读为真实 WebRender 结论。
- guest 需自带 `timeout`、`sha256sum`、`Xorg`、`firefox`；脚本只报
  `reason=no-tool`/`no-firefox`，不会自动安装。

## 验证

```text
bash -n tools/riscv/p5b/p5b-stage.sh      OK
bash -n tools/riscv/p5c/p5c-stage.sh      OK
sh -n   tools/riscv/p5b/p5b-guest.sh      OK
sh -n   tools/riscv/p5c/p5c-guest.sh      OK
sh -n   tools/riscv/p6/p6-guest.sh        OK
```

本轮没有执行任何板级操作，也没有修改内核或 initramfs。
