# 两处内核修复后的全量回归

日期：2026-10-25
内核：`923ca5c53ea46e02`（含 `3f934bf7a` tmpfs 根 1777、`bd904ff64`
flock wake-one）
镜像：`target/debian-x86-toolchain3/rootfs/debian-root.ext2`
（desktop-drm-toolchain，2.0 GiB）；Claude 轮次用
`target/claude-r3/daily.ext2`（同一镜像 + claude + settings）
加速：KVM，4 vCPU / 8 GiB

## 结论摘要

- 两处修复后的内核在开发者工作流、GUI 并发/休眠、压力与 4×4 全量哈希上
  **无退化**；串口 `Uncaught panic` / `allocation failed` / `oom-killer`
  全为 0。
- **4×4 并行哈希全量 `/usr`：21,657 文件 / 990.6 MiB，干净复跑 38.9 s
  （独占时 31.7 s），0 错误，与单写者 `sha256sum` 基线逐条一致**
  （要求 ≥20,000 文件且 ≤60 s，通过）。与 GUI/Claude 并发的那次为
  81.95 s，只说明资源竞争，不计入验收。
- 日常矩阵原用镜像 `target/final-matrix/xwrap/image18.ext2` 不在本仓库
  （外部产物，已缺失），本次在 toolchain 镜像上跑标准 checklist：
  `APT`、`DESKTOP`、`APP`、`CLIPBOARD` 四个域 **ok**；`AUDIO` 域在
  `aplay` 上阻塞（无返回标记），`USB`/`SUSPEND` 未到达。属镜像/设备
  缺项，不是内核退化（见下文“限制”）。

## 回归结果

| 步骤 | 结果 | 耗时 | 关键数字 |
|---|---|---|---|
| 日常矩阵（toolchain 镜像） | **部分**（4 域 ok；AUDIO 阻塞） | — | APT/DESKTOP/APP/CLIPBOARD ok；panic 0 |
| 开发者工作流 | **PASS** | ~5.6 min | git 2.47.3 clone 3.2 s；g++ threads 输出 1000；pip six 1.17.0（PEP 668 回退）import ok；zlib 3× test OK |
| GUI 并发 + 休眠 | **PASS** | ~13 min | 10 个应用 / 14 个窗口（pcmanfm/eog/xterm/xclock/xcalc/xeyes）；1.5 GiB I/O dd/sha rc=0；suspend **3/3**；panic 0、allocation failed 0 |
| 压力（30 轮；脚本默认，要求 ≥10） | **PASS** | ~7 min | 内存分配 0/30 失败、编译 0/30 失败（30/30 ok）；MemAvailable 3,958,432 → 3,900,496 KB（-1.46%）；panic 0、allocation failed 0 |
| **4×4 并行哈希** | **PASS** | **38.9 s**（干净复跑；独占时 31.7 s） | **21,657 文件 / 990.6 MiB，verify 21,657/21,657、0 missing/extra/mismatch**；与 GUI/R5 并发时为 81.95 s（仅作参考，不计入验收） |

## B. Claude 第 5 轮（同日，见障碍文档）

三轮真实任务（fmt CMake+ctest、click pytest、loopback HTTP 100 并发）
全部通过，Asterinas 侧新增障碍 0；细节与证据在
`2026-10-25-claude-obstacles.md` 的“第五轮”一节。

## 日常矩阵的说明与限制

- 标准矩阵镜像 `image18.ext2` 是外部产物，不在仓库；没有它无法复现
  历史 10 域。
- toolchain 镜像没有 apt 包索引，`apt-get install hello` 直接失败；补
  `apt-get update` 后 `APT` 变为 ok。
- `AUDIO` 域在 `aplay -D hw:0,0` 上阻塞：该镜像/本次 QEMU 没有可用的
  HDA 输出，aplay 不返回；门禁在该域停止。
- 因此日常矩阵只能给出 4/8 域的证据；`DEB` 侧的两处修复未触及
  desktop/audio/USB 路径。

## 复现

```sh
K=$PWD/target/osdk/asterinas-osdk-bin.elf
I=$PWD/target/debian-x86/stage1/initramfs.cpio
R=$PWD/target/debian-x86-toolchain3/rootfs/debian-root.ext2

# 4×4 全量哈希（关键验收）
DAILY_USE_CHECKLIST=$PWD/tools/x86/debian/usr_hash_stress.sh \
make test_x86_daily_use X86_DEBIAN_KERNEL=$K X86_DEBIAN_STAGE1_INITRAMFS=$I \
  X86_DEBIAN_XORG_BASE_ROOT=$R X86_DEBIAN_XORG_ACCEL=kvm \
  X86_DEBIAN_XORG_TIMEOUT=900 DAILY_USE_WORKSPACE=$PWD/target/regress-hash

# 其余步骤（命令同上，仅替换 checklist 与 workspace）
tools/x86/debian/dev_workflow.sh       -> target/regress-dev
tools/x86/debian/gui_stress.sh         -> target/regress-gui
tools/x86/debian/stress_memory.sh      -> target/regress-stress（STRESS_ROUNDS=10）
tools/x86/debian/daily_use_checklist_regress.sh -> target/regress-daily2
```

## 证据

- `evidence/2026-10-25-post-fix-regression/daily-domains.txt`
- `target/regress-dev/console.txt`、`target/regress-gui/console.txt`、
  `target/regress-stress/console.txt`、`target/regress-hash/console.txt`
- 修复前的对照：`2026-10-25-parallel-perf-diag.md`
