# Claude Code 在 Asterinas 的 Debian guest 内做真实工作

日期：2026-10-25　HEAD：`14306e1d9`
内核：`target/osdk/asterinas-osdk-bin.elf`（ring-buffer 修复后，
sha256 前缀 `20582f4b207b6cb1`）
镜像：`target/debian-x86-toolchain3/rootfs/debian-root.ext2`
（`desktop-drm-toolchain`，2.0 GiB / 409 包；注入后剩余 616 MB）

## 结论摘要

- **阶段一通过**：286 MB 的 Claude Code 二进制在 guest 内可运行，
  `claude --version` → `2.1.302 (Claude Code)`；`claude -p "reply with
  exactly: OK"` **rc=0，9.6 s**，输出 `OK`。
- **阶段二通过（2/2 轮）**：agent 自己选择并做成了一件实用的事——
  在 `/home/asterinas/work` 写了一个 **`abi-probe`**（Linux ABI 探针 CLI，
  C + Makefile + Python 自测 + README + 结果报告）：
  - 第 1 轮（503.5 s）：33 pass / 0 fail / 4 skip，340 项检查；同一套
    连跑 25 轮结果一致；并自行诊断/修复了环境里的 hook 配置问题；
  - 第 2 轮（84.4 s）：新增 `statx` 探针与专项测试，版本 1.1.0；
    38 探针、34 pass / 0 fail / 4 skip、373 项检查；`make test` 373 项全过，
    连跑 5 次稳定，`make report` 重新生成 `results/report.{txt,json}`。
- **内核侧无阻塞**：整轮串口 `ERROR : Uncaught panic` = **0**，
  `allocation failed` = **0**；agent 正常使用文件、进程、fork、pipe、
  statx/fallocate/sendfile 等 syscall。
- **判定：可用**。唯一的阻塞项是 Claude Code 自身的“root 不得使用
  `--dangerously-skip-permissions`”策略，用 guest 内的普通用户
  `asterinas`(uid 1000) 运行后解决；不是 Asterinas 缺陷。

## 1. 阶段一：二进制与认证

注入方式（宿主）：

```sh
tools/x86/debian/claude_ingest.sh
```

- 写入 `/opt/claude-code/bin/claude`（285,793,528 B，0755）；
- 只读复制宿主 `~/.claude/settings.json` 到
  `/root/.claude/settings.json`（0600，含 API env；内容不打印）；
- 镜像空闲块 219,736 → 150,406（4 KiB 块），注入后剩余 **616 MB**。

guest 内：

```
claude_version rc=0 out=2.1.302 (Claude Code)
claude_ok rc=0 dur=9.6s out=[claude-code:unrecognized_model] {"model":"deepseek-flash[1m]","query_source":"sdk"} OK
```

- glibc 2.41 满足二进制的 GLIBC_2.26 上限；
- DNS 改为 slirp 的 `10.0.2.3` 后，`api.deepseek.com` 的 HTTPS 认证/请求
  正常；
- `unrecognized_model` 只是客户端不识别 `deepseek-flash[1m]` 这个 ID 的
  **警告**，请求本身成功（返回 OK）。

## 2. 阶段二：agent 自己选择并执行的两轮工作

两轮都使用同一命令形态（非交互、跳过权限询问、纯文本输出）：

```sh
cd /home/asterinas/work
claude -p "<prompt>" --dangerously-skip-permissions --output-format text
```

### 第 1 轮（rc=0，503.5 s）

agent 选择的实用任务：写一个 **Linux ABI 探针 CLI** `abi-probe`：

- `src/abi_probe.c`、`Makefile`、`tests/selftest.py`、`README.md`、
  `results/report.{txt,json}`；
- 初版 33 pass / 0 fail / 4 skip，共 340 项检查；连续 25 轮结果一致、
  无临时文件残留；
- 同时处理了环境自身的 hook 问题（见障碍表第 2 条），并给出回退方法。

### 第 2 轮（rc=0，84.4 s）

在上一步产物上新增 **`statx` 探针**：

- 校验返回 mask、大小、文件类型、nlink，并与 `fstat` 交叉比对 ino/mode，
  另测 `AT_EMPTY_PATH`；
- 注册进 `g_probes`，版本 `1.0.0` → `1.1.0`；
- `tests/selftest.py` 增加 statx 专项；README 更新；
- `make test` **373 项全过**；全量连跑 5 次稳定为 38 探针、
  **34 pass / 0 fail / 4 skip**；`make report` 重新生成报告；
- 该轮自报“无障碍”；唯一说明是工作目录不是 git 仓库，故未提交。

产物路径（guest 镜像内）：

```
/home/asterinas/work/abi-probe/{src,tests,README.md,Makefile,results,abi-probe}
/home/asterinas/work/hook-fix/
/home/asterinas/work/round1.log, round2.log
```

## 3. 障碍清单

| # | 障碍 | 第一条真实错误 | 疑似根因 | 是否 Asterinas 侧 | 处置 |
|---|---|---|---|---|---|
| 1 | 非交互 agent 无法以 root 运行 | `--dangerously-skip-permissions cannot be used with root/sudo privileges for security reasons` | Claude Code 的安全策略（拒绝 root） | **否**（客户端策略） | 改为以 `asterinas`(1000) 运行，独立 `~/.claude/settings.json` 与 `/home/asterinas/work`；同一命令重跑两轮均 rc=0 |
| 2 | hook 配置指向不存在的脚本 | `/usr/local/bin/claude-event-hook.py` 不存在；`/tmp/claude-events.jsonl` 为 root-only，重定向失败导致 Stop hook 反复失败 | 宿主 settings.json 的 hook + 首轮 root 会话遗留的属主 | **否**（宿主配置/文件属主） | **agent 自行修复**：在配置路径安装最小 JSONL 透传脚本、把日志属主改为 asterinas、权限 644→600，并逐行 `json.loads` 验证 |
| 3 | 模型 ID 警告 | `[claude-code:unrecognized_model] {"model":"deepseek-flash[1m]"}` | 客户端内置模型清单不含该 ID | **否**（客户端） | 不阻塞；API 正常返回 |
| 4 | 目录不是 git 仓库 | （agent 自述，非错误） | 工作目录未初始化 git | 否 | 仅说明未提交 |

**Asterinas 侧障碍：0 项。** 本次工作负载中，agent 的文件写入、进程创建、
`fork/wait4`、pipe、`statx`、`fallocate`、`sendfile`、`close_range` 等操作
均正常；串口无 panic、无 allocation failed。

## 4. 阶段三：选一个修掉并复验

- 选择的修复：**障碍 1**（同时它也是唯一真正阻塞 agent 工作的问题）；
- 修复方式：`claude_ingest_run.sh` 在两轮前准备
  `/home/asterinas/.claude/settings.json` 与 `/home/asterinas/work`，
  用 `setpriv --reuid=1000 --regid=1000 --init-groups` + `HOME=/home/asterinas`
  运行同一条 claude 命令；
- 复验：两个 round 均 **rc=0**（503.5 s / 84.4 s），产物完整；
- 障碍 2 由 agent 在运行中自行修复（也记录在 `round1.log` 中）。

## 5. 判定

**可用**：一个真实的 AI 编码 agent 在 Asterinas 的 Debian 里完成了
“自己选题 → 写代码 → 写测试 → 跑测试 → 生成报告 → 再迭代加功能”的完整闭环，
没有遇到 Asterinas 侧的功能缺口；仅有的阻塞来自 Claude Code 自己的 root
安全策略，已用普通用户运行规避。

## 6. 复现

```sh
# 宿主：注入二进制与 settings（只读复制，不打印密钥）
tools/x86/debian/claude_ingest.sh

# guest：验证 + 两轮自由任务（脚本以 asterinas 用户运行 agent）
DAILY_USE_CHECKLIST=$PWD/tools/x86/debian/claude_ingest_run.sh \
make test_x86_daily_use \
  X86_DEBIAN_KERNEL=$PWD/target/osdk/asterinas-osdk-bin.elf \
  X86_DEBIAN_STAGE1_INITRAMFS=$PWD/target/debian-x86/stage1/initramfs.cpio \
  X86_DEBIAN_XORG_BASE_ROOT=$PWD/target/debian-x86-toolchain3/rootfs/debian-root.ext2 \
  X86_DEBIAN_XORG_ACCEL=kvm X86_DEBIAN_XORG_TIMEOUT=3000 \
  DAILY_USE_WORKSPACE=$PWD/target/claude-guest2
```

## 7. 证据

- `docs/porting/evidence/2026-10-25-claude-code-in-guest/round1.log`（agent 第 1 轮报告）
- `…/round2.log`（第 2 轮报告）
- `…/abi-probe-README.md`、`…/abi-probe-report.txt`（agent 产物的报告）
- `…/work-tree.txt`（镜像内产物树）
- `…/console-excerpt.txt`（`--version`/`-p OK`/两轮 rc/work files + panic 计数）
- 串口全量：`target/claude-guest2/console.txt`；首轮 root 策略错误：
  `target/claude-guest/console.txt`
