# Claude Code 三轮递进任务：OS 障碍清单与 tmpfs 修复

日期：2026-10-25　HEAD：`ad9e5f829`
guest 内核：`20582f4b207b6cb1`（ring-buffer 修复后的树）
修复后内核：`548e5f0debee52c0`；修复提交：`3f934bf7a`
镜像：`target/debian-x86-toolchain3/rootfs/debian-root.ext2`
（`desktop-drm-toolchain`，2.0 GiB）

## 结论摘要

- **轮 1（Python 生态）通过**：agent 自建 `strmath` 库 + pytest 项目，
  `pytest -q` **66 passed / 0 failed**，rc=0，301.5 s。
- **轮 2（中等 C 项目）通过**：Lua v5.4.9 `make -j4` 构建成功，
  自带 `testes/all.lua` 跑完并输出 `final OK !!!`，rc=0，141.3 s。
- **轮 3（多进程/多线程/文件锁压力工具）产出完整但未跑完**：agent 写出
  `stress_hash.py` / `monitor.py` / `verify.py`，小树验证 4/4 通过、
  `xargs -P8 sha256sum` 对 `/usr`（24,270 文件 / 1,093.5 MiB）
  **74.40 s 全量成功**；4 进程 × 4 线程的全量运行超过单轮预算被中止
  （agent 工具 `Exit code 137`、轮级 rc=124），**无 OOM、无 panic**。
- **发现 1 个 Asterinas 侧障碍并已修复**：`/dev/shm` 根目录是
  **0755 root:root**，非 root 用户的 `multiprocessing.Lock()` 直接
  `PermissionError: [Errno 13]`；Linux tmpfs 根是 **1777**。已把 tmpfs
  根 inode 模式改为 `0777|S_ISVTX`，复验 `mp_test rc=0`。
- 其余 5 条障碍均为镜像/项目/预算层面（PATH、缺 dev 包、测试的 stdin
  语义、单轮时间上限），不是内核缺陷。
- 三轮串口：`Uncaught panic` **0**、`allocation failed` **0**、
  `oom-killer` **0**、`Out of memory` **0**。

## 1. 三轮结果

| 轮 | 任务 | 结果 | rc | 耗时 | 峰值 load / 最低 MemAvailable |
|---|---|---|---|---|---|
| 1 | pip 装 pytest + 自建 20+ 测试项目 | **66 passed / 0 failed** | 0 | 301.5 s | 0.073 / 3.04 GiB |
| 2 | clone + `make -j4` Lua 5.4.9 + 自带测试 | 构建成功，`final OK !!!` | 0 | 141.3 s | 0.220 / 2.90 GiB |
| 3 | 多进程 + 多线程 + 文件锁并行哈希工具 | 工具与基线成功；全量运行超预算 | 124 | 910.1 s | 5.021 / 1.93 GiB |

三轮都以 guest 内普通用户 `asterinas`(uid 1000) 运行，
命令形态为 `claude -p "<prompt>" --dangerously-skip-permissions
--output-format text`，每轮上限 900 s。

### 轮 1：真实 Python 生态

- 产物：`/home/asterinas/work/pyproj`（`strmath` 包 + `pyproject.toml`
  + 参数化测试）；
- 关键命令：`python3 -m pip install --no-cache-dir
  --break-system-packages pytest`；`cd pyproj && PATH="$HOME/.local/bin:$PATH"
  pytest -q`；
- 结果：**66 passed / 0 failed（0.06 s，rc=0）**；
- 第一条真实错误：`pytest: command not found` —— pip 装到
  `~/.local/bin`，该目录不在镜像 PATH；随后 `ModuleNotFoundError:
  strmath` 由 `pyproject.toml` 的 `pythonpath = ["."]` 修复；
- agent 同时修好了会话层 hook 故障（`/usr/local/bin/claude-event-hook.py`
  缺失、`/tmp/claude-events.jsonl` 属主不对），属客户端/环境问题，
  已在 `2026-10-25-claude-code-in-guest.md` 记录过。

### 轮 2：真实 C 项目 + 并行构建

- 产物：`/home/asterinas/work/cproj/lua-5.4.9`；
- 构建：`make -j4` 成功（clean build 约 5 s），`./lua -v` → `Lua 5.4.9`；
- 测试：`testes/all.lua` 全量 rc=0，输出 `final OK !!!`（约 7 s；
  跳过需 `testC` 的 API 测试与缺失的 `pt_BR` locale）；
- 第一条真实错误：`lua.c:444: fatal error: readline/readline.h:
  No such file or directory` → 安装 `libreadline-dev` 后恢复
  `-DLUA_USE_READLINE` / `-lreadline` 重新 clean build；
- 另一次 `files.lua:84` 失败：stdin 是可 seek 的 `/dev/null`，改管道
  stdin 后全套通过（测试自身对 stdin 的假设，不是内核问题）。

### 轮 3：压力型工作负载

- 产物（guest 镜像内）：
  ```
  /home/asterinas/work/stressproj/stress_hash.py   13,039 B
  /home/asterinas/work/stressproj/monitor.py        4,018 B
  /home/asterinas/work/stressproj/verify.py         4,118 B
  /home/asterinas/work/stressproj/out/{small.raw,small.baseline,usr.baseline.z,...}
  ```
- 规模摸底：`/usr` 共 **24,270** 个普通文件、**1,093.5 MiB**；
  `ulimit -n` = 524288，`file-max` = 9223372036854775807；
- 独立基线：`find /usr ... | xargs -0 -n 64 -P 8 sha256sum --zero`
  **rc=0，74.40 s，24,270 条记录**（证明多进程/管道/文件 I/O 在高并发下
  正常）；
- 小树验证：`--root /tmp/smallroot --procs 2 --threads 2 --batch 2`
  输出 `files_discovered=4 hashed=4 hash_errors=0 worker_failures=0`；
- 全量运行：`--root /usr --procs 4 --threads 4 --batch 32` 在
  `monitor.py` 下启动后，agent 的 Bash 工具在 600 s 上限处收到
  `Exit code 137`，随后轮级 900 s 上限到期（rc=124），agent 来不及写
  最终报告；该轮 `max_load=5.021`、最低 MemAvailable 1.93 GiB，
  **无 OOM、无 panic、无 allocation failed**。

## 2. OS 障碍清单

| # | 轮 | 任务 | 第一条真实错误 | 疑似根因 | Asterinas 侧 | 处置 |
|---|---|---|---|---|---|---|
| 1 | 1 | 装 pytest | `pytest: command not found` | pip `--user` 装到 `~/.local/bin`，不在镜像 PATH | 否（镜像 PATH 策略） | agent 前置 `PATH="$HOME/.local/bin:$PATH"` |
| 2 | 1 | 导入自建库 | `ModuleNotFoundError: No module named 'strmath'` | 项目未声明 import 路径 | 否（项目配置） | `pyproject.toml` 加 `pythonpath = ["."]` |
| 3 | 2 | 构建 Lua | `lua.c:444: fatal error: readline/readline.h: No such file or directory` | 镜像缺 `libreadline-dev` 头文件 | 否（缺 dev 包） | `apt-get install libreadline-dev` 后重建 |
| 4 | 2 | Lua 自带测试 | `files.lua:84` 一次失败 | stdin 是 `/dev/null`（可 seek），测试期望管道 | 否（测试对 stdio 的假设） | 改用管道 stdin，全套通过 |
| 5 | 3 | `multiprocessing.Lock()` / Pool | `PermissionError: [Errno 13] Permission denied`（`multiprocessing/synchronize.py` `SemLock.__init__` ← `ctx.Lock()`） | `/dev/shm` 为 `drwxr-xr-x root root`(0755)；tmpfs/ramfs 根 inode 被硬编码为 0755，也不解析 `mode=` 挂载选项 | **是（已修）** | 见第 3 节：tmpfs 根改为 `0777\|S_ISVTX`(1777) |
| 6 | 3 | 全量并行哈希 `/usr` | agent 工具 `Exit code 137`；轮级 `rc=124` | 4×4 进程/线程扫 1,093.5 MiB 超过单轮 900 s / 工具 600 s 预算 | 否（预算；无 OOM/panic） | 直接用小规模 + 独立 `xargs -P8` 基线验证能力 |

**Asterinas 侧障碍：1 项（第 5 条），已修复并复验。** 第 1–4 条是镜像/
项目层面的环境差异；第 6 条是测试时长预算，不是内核失败。第 5 条在
修复前也确实被 agent 自己绕过（把 worker pool 从
`multiprocessing.Lock` 改成 fork+pipe），说明它是真实存在的可用性缺陷：
**非 root 用户在这台 Debian 上无法使用 Python 的进程同步原语**。

## 3. 修复：tmpfs 根目录 1777

### 根因

- `TmpFs` 是 `RamFs` 的别名/包装，根 inode 在
  `RamFs::new_with_sb` 里创建，模式硬编码为
  `mkmod!(a+rx, u+w)` = **0755**；
- 挂载时没有任何 `mode=` 解析，所以 `/dev/shm` 只能继承 0755；
- Linux `mm/shmem.c` 的 tmpfs 根默认模式是
  `S_IRWXUGO | S_ISVTX`（**1777**，再叠加 umask；内核初始化时 umask 为 0），
  因此 Debian 的 `/dev/shm` 对所有用户可写；
- Python `multiprocessing` 在 Linux 上通过 `shm_open` 在 `/dev/shm`
  建信号量/共享内存，非 root 在 0755 目录里创建对象必然 EACCES。

### 改动

- `kernel/core/src/fs/fs_impls/ramfs/fs.rs`
  - `new_with_sb` 新增 `root_mode: InodeMode` 参数（`fs.rs:72`），
    根 inode 使用它（`fs.rs:83`）；
  - `new_internal`（ramfs/rootfs）继续传 `mkmod!(a+rx, u+w)`（0755），
    **行为不变**（Linux ramfs 根同样是 0755）。
- `kernel/core/src/fs/fs_impls/tmpfs/fs.rs`
  - 传 `mkmod!(a+rwx) | InodeMode::S_ISVTX`（`fs.rs:63`），即 1777；
  - 新增两个 ktest：`tmpfs_root_mode_is_1777`（断言 `root_inode().mode()`
    等于 `0777|S_ISVTX`）与 `ramfs_root_mode_stays_0755`（守卫 ramfs/
    rootfs 不被连带改动）。

改动只影响 tmpfs 根目录的初始模式与构造参数；文件/子目录创建、权限
检查逻辑未动。ramfs 侧的改动只是把原先硬编码的 0755 提升为
`new_with_sb` 的参数，ramfs/rootfs 的取值与行为保持不变。

修复提交：`3f934bf7a fix(tmpfs): make the tmpfs root mode 1777 like linux`。

## 4. 复验

修复后内核 `548e5f0debee52c0`，门禁脚本
`tools/x86/debian/tmpfs_mode_check.sh`（以 uid 1000 写入 `/dev/shm` 并跑
`multiprocessing.Lock` + `Pool`）：

```
tmpfs_check start
tmpfs_mode=1777 root root
tmpfs_mount=tmpfs on /dev/shm type tmpfs (rw,nosuid,nodev,strictatime)
shm_write_uid1000 rc=0 err=
mp_test rc=0 out=pool [1, 2, 3] lock_ok True
DAILY_DONE=ok
```

- `stat` → `1777 root root`；
- uid 1000 在 `/dev/shm` 建文件 rc=0；
- `ctx.Lock()` 与 `Pool.map` 全部成功（`pool [1, 2, 3]`、`lock_ok True`）；
- 串口 `Uncaught panic` = 0、`allocation failed` = 0。

### ktest

两个用例：`tmpfs_root_mode_is_1777`（`root_inode().mode()` 必须是
`0777|S_ISVTX`）与 `ramfs_root_mode_stays_0755`（ramfs 根保持 0755）。

结果：

```
running 181 tests in crate "aster-core"
test ...::tmpfs::fs::tests::tmpfs_root_mode_is_1777 ... ok
test ...::tmpfs::fs::tests::ramfs_root_mode_stays_0755 ... ok
test result: ok. 2 passed; 0 failed; 179 filtered out.
```

（QEMU isa-debug-exit 返回码 **33** = `poweroff(ExitCode::Success)`。）

两个坑，如实记录：

1. **OSDK 的过滤是按测试路径的分量精确后缀匹配，不是子串**。
   `cargo osdk test tmpfs_root_mode` 一个用例也选不中，但整个 38 包运行
   仍以 **rc=0** 结束（`0 passed; 181 filtered out`），看起来像成功。
   必须写完整用例名（`tmpfs_root_mode_is_1777`）。
2. **ktest 环境没有初始化时钟**：`Inode::mode()` 首次调用在
   `time/clocks/system_wide.rs:85` 的 `Option::unwrap()` 上 panic。用例
   需先调用 `crate::time::clocks::init_for_ktest()`（configfs 的 ktest
   同样如此）。

为省掉再跑一遍 38 个 test-base 包（每包一次 QEMU，TCG 下约 15 分钟），
验证时只重建了 `aster-core` test base（OSDK 相同的 RUSTFLAGS 与
`--cfg ktest`），把 whitelist 设为两个完整用例名，并按
`make_elf_for_qemu()` 的做法把 ELF 的 `e_machine` 改为 `EM_386`，然后
直接启动该测试内核。`cargo osdk test` 会覆盖
`target/osdk/asterinas-osdk-bin.elf`；本次跑前备份、跑后已恢复修复内核
`548e5f0d`。

## 5. 内核健康

| 轮 | `Uncaught panic` | `allocation failed` | `oom-killer` | `Out of memory` |
|---|---|---|---|---|
| 1 | 0 | 0 | 0 | 0 |
| 2 | 0 | 0 | 0 | 0 |
| 3 | 0 | 0 | 0 | 0 |

轮 3 在 `max_load=5.021`、MemAvailable 降到 1.93 GiB 的情况下仍然没有
内存/分配类故障，退出原因是时间预算，不是内核错误。

## 6. 已知限制（如实记录）

1. 轮 3 **没有 agent 的最终报告**：轮级 900 s 与 agent 工具 600 s 上限
   先于任务完成生效，`claude-r3-round3.log` 只有模型警告头。工具的
   产物与中间结果保留在镜像里，`multiprocessing` 能力用独立脚本复验。
2. 修复前 agent 已用 fork+pipe 绕过 `/dev/shm` 限制，因此轮 3 的
   “工具能跑” 不是修复后才成立的；修复解决的是**同步原语可用性**这一
   类真实工作负载。
3. tmpfs **仍不解析 `mode=`/`uid=`/`gid=` 挂载选项**；本修复只对齐了
   Linux 的默认根模式。若用户显式挂载 `tmpfs -o mode=0700`，该选项仍被
   忽略——这是已知缺口，未在本次改动范围内。
4. `/dev/shm` 容量仍为 2 GiB；更大的共享内存需求需要调挂载参数（同第 3 条
   限制）。
5. 探针里的 `AttributeError: module '_multiprocessing' has no attribute
   'SEMAPHORE'` 是探针使用 Python 内部常量的写法问题，不是内核问题；
   同一次探针里的 `PermissionError` 才是真实障碍。
6. 镜像 PATH 不含 `~/.local/bin`、缺 `libreadline-dev` 属于镜像配方选择；
   本次按“独立 profile 不改默认”原则没有改默认镜像。

## 7. 复现

```sh
# ktest（两个根模式用例；会覆盖 target/osdk 的内核 ELF，跑完需重建/恢复）
PATH=/tmp/tp/osdk-install/bin:/tmp/tp/shims:$PATH OSDK_LOCAL_DEV=1 \
  VDSO_LIBRARY_DIR=/home/arch-anjie/Program/linux_vdso OVMF=off \
  cargo osdk test tmpfs_root_mode --target-arch x86_64 --scheme microvm

# 三轮任务（宿主；脚本内 apt 装 git/python3-pip，准备 asterinas 的
# settings 与工作目录，逐轮 900 s 上限并采样 load/MemAvailable）
DAILY_USE_CHECKLIST=$PWD/tools/x86/debian/claude_round3_run.sh \
make test_x86_daily_use \
  X86_DEBIAN_KERNEL=$PWD/target/osdk/asterinas-osdk-bin.elf \
  X86_DEBIAN_STAGE1_INITRAMFS=$PWD/target/debian-x86/stage1/initramfs.cpio \
  X86_DEBIAN_XORG_BASE_ROOT=$PWD/target/debian-x86-toolchain3/rootfs/debian-root.ext2 \
  X86_DEBIAN_XORG_ACCEL=kvm X86_DEBIAN_XORG_TIMEOUT=4200 \
  DAILY_USE_WORKSPACE=$PWD/target/claude-r3

# tmpfs 模式 + 非 root multiprocessing 复验
DAILY_USE_CHECKLIST=$PWD/tools/x86/debian/tmpfs_mode_check.sh \
make test_x86_daily_use \
  X86_DEBIAN_KERNEL=$PWD/target/osdk/asterinas-osdk-bin.elf \
  X86_DEBIAN_STAGE1_INITRAMFS=$PWD/target/debian-x86/stage1/initramfs.cpio \
  X86_DEBIAN_XORG_BASE_ROOT=$PWD/target/debian-x86-toolchain3/rootfs/debian-root.ext2 \
  X86_DEBIAN_XORG_ACCEL=kvm \
  DAILY_USE_WORKSPACE=$PWD/target/tmpfs-check
```

## 8. 证据

- `evidence/2026-10-25-claude-obstacles/round1.log`、`round2.log`、
  `round3.log`（agent 各轮报告原文；轮 3 仅有模型警告头）
- `…/session-r3-errors.txt`（轮 3 会话里的 `/dev/shm` stat、`Lock()`
  失败栈、`Exit code 137`、小树结果、串口计数）
- `…/stressproj-files.txt`、`…/stress_hash.py`（压力工具源码与镜像内清单）
- `…/tmpfs-check.txt`（修复后内核的门禁输出）
- 串口全量：`target/claude-r3/console.txt`（三轮）、
  `target/tmpfs-check/console.txt`（修复复验）
