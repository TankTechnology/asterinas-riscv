# 真实开发者工作流：git / C++ / Python / zlib

日期：2026-10-25　HEAD：`3b71c16c6`
内核：`target/osdk/asterinas-osdk-bin.elf`（ring-buffer 修复后，
sha256 前缀 `20582f4b207b6cb1`）
镜像：`target/debian-x86-toolchain3/rootfs/debian-root.ext2`
（`desktop-drm-toolchain`，2.0 GiB / 409 包）

## 结论摘要

四项全部通过：

| 项 | 结果 | 关键证据 |
|---|---|---|
| A git | **pass** | `git 2.47.3`；`git clone --depth 1` GitHub zlib **rc=0，2.1 s，6.1 M**，HEAD `767c4c9`，工作区干净 |
| B C++ | **pass** | `g++ -O2 -std=c++17 -pthread` rc=0，运行输出 **1000** |
| C pip | **pass（需 PEP 668 覆盖）** | 原始 `pip install six` 报 `externally-managed-environment`；`--break-system-packages` 从 PyPI 成功，`six 1.17.0`、`click` 均可 import |
| D zlib | **pass** | 克隆源 `configure/make -j2/make test` rc=0/0/0，`zlib test OK` **3 项** |

全程无 panic（串口真实 panic 块 0）。

## A. git 工作流

```
apt update_rc=0 install_rc=0
git_version=git version 2.47.3
git_clone rc=0 dur=2.1s src=github
git_log=767c4c9 Honor deflateReset promise to reset the header. src=github size=6.1M dirty_files=0
```

- guest 的 `/etc/resolv.conf` 原本是 `1.1.1.1`，脚本先改为 slirp 的
  `10.0.2.3` 再 apt/联网；
- GitHub 在 guest 内可达，**没有用本地 bare 仓库回退**（脚本保留了该回退
  路径，仅在 clone 失败时启用）。

## B. C++ 工具链（STL + std::thread）

```
cpp gpp_rc=0 run_out=1000 err=
```

`g++` 随镜像的 `build-essential` 已存在，无需额外安装；`-pthread`
链接与 `std::thread` 运行正常，输出与期望一致（1000）。

## C. Python 生态（pip）

```
pip_version=pip 25.1.1 from /usr/lib/python3/dist-packages/pip (python 3.13)
pip_six rc=1 err=error: externally-managed-environment__× This environment is externally managed_
pip_six src=pypi-break-system rc=0 import_rc=0 import_out=six_version=1.17.0
pip_click rc=0 import_rc=0 out=<string>:1: DeprecationWarning: The '__version__' ...
```

- **第一条真实错误**：Debian trixie 的 Python 是
  **externally-managed**（PEP 668），任务书里的
  `python3 -m pip install --no-cache-dir six` 被拒绝；
- 归因：**镜像/发行版策略**，不是 Asterinas syscall 或网络问题——PyPI 本身
  可达，`--break-system-packages` 重试立即成功；
- `six 1.17.0`、`click` 均安装并 import 成功（click 只有 `__version__`
  弃用警告，rc=0）；
- 脚本还准备了本地 wheel（`/root/six-1.17.0-py2.py3-none-any.whl`）作为
  网络失败回退，本次未用到。

## D. 真实 C 项目全流程（zlib from git）

```
zlib cfg_rc=0 make_rc=0 test_rc=0 ok_lines=3 result=yes
```

使用 A 里 clone 的 `madler/zlib`（HEAD `767c4c9`）执行
`./configure && make -j2 && make test`，静态/共享/64-bit 三条测试路径均
`zlib test OK`，rc=0。

## 运行中的问题与归因

1. **首次运行在 guest 启动约 74 s 时失败**：门禁的宿主 shell 报
   `write error: no space left on device`——宿主磁盘被之前几个已完成工作区
   的 2 GiB 镜像副本占满，不是 guest 故障。清理
   `gui-fixed/gui-stress2/dev-workflow` 的 `daily.ext2`（保留 console）后
   重跑即通过。
2. pip 的 PEP 668 见上；属镜像配置，不是内核/网络缺陷。
3. 该 microvm 内核的 `dmesg`/syslog 仍未实现（`Function not implemented`），
   所以 panic 结论以串口日志为准（本次无 panic 块）。

## 判定

**四项均通过**：git clone、C++17 + threads、pip 装包并 import、
zlib 全流程测试。唯一需要操作的是 PEP 668 覆盖（`--break-system-packages`
或 venv），属于 Debian 镜像策略，不是 Asterinas 功能缺失。

## 证据

- `target/dev-workflow2/console.txt`（串口全量）
- `target/dev-workflow2/host.log`（门禁 `GATE_EXIT=2` 仅因自定义 checklist
  不输出标准 DAILY 域）
- 归档：`docs/porting/evidence/2026-10-25-dev-workflow/console-excerpt.txt`
