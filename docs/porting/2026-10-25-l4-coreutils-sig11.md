# L4 `coreutils` signal 11 归因：缺失的 `AT_EXECFN` 与初始栈顶部布局

日期：2026-10-25　仓库 HEAD：`3e0d377e6`（修复前）
验证内核：`f847a16a65498345…`（第一次 L4 重跑）；
`66004cd683dd451b…`（提交源码重建后的第二次 L4 重跑）

## 结论摘要

- **判定 A（Asterinas 侧特性缺失）**：内核**从未设置 `AT_EXECFN`**，也没有
  Linux 放在环境字符串之上的 execfn 路径字符串。于是 `envp[0]` 成为初始栈
  映射的**最顶端字符串**，其末尾正好贴着 `initial_top`（映射上界）。
- **失败链**：`dpkg-buildpackage` → `debian/rules clean` → GNU make 解析
  makefile 时查找一个 16 字节的环境变量名 → `recursively_expand_for_file`
  对每个 `environ[i]` 先读 `environ[i][name_len]`（**无长度/NUL 检查**）再
  `strncmp` → 对最顶端的 14 字节 `envp[0]` 读到 `+16`，越过 8 MiB 初始栈
  映射 **2 字节** → 用户态读页错误 → SIGSEGV（rc 255，两次尝试一致）。
- **为什么 Linux 不炸**：Linux 的初始栈在 env 字符串块**之上**还有
  `AT_EXECFN` 路径字符串（实测最高字符串即 exec 路径），make 的越界读落在
  同一映射内；Asterinas 缺这一项，最后 2 字节直接落到未映射区。
- **修复**：在初始栈顶部写入 exec 路径字符串并设置 `AT_EXECFN`，与 Linux
  布局一致（一处特性修复，4 个文件）。
- **L4 结果**：两次重跑（归因后 / 提交后）均为 3 包 **2 ok / 1 fail**。
  `hello` 与 `python3-minimal` 正常产出 `.deb`；`coreutils` 的
  **signal 11 已消失**（`debian/rules clean` 通过），改为 `configure` 的
  “不能以 root 运行” 保护错误——**环境问题（B），不是内核问题**，
  见 §5.2、§5.4。

## 1. 失败现场

| 项 | 值 |
|---|---|
| 失败命令 | `dpkg-buildpackage -b -us -uc`（`debian/rules clean` 子进程） |
| 实际崩溃程序 | `/usr/bin/make -f debian/rules clean` |
| 退出码 | **255**；`dpkg-buildpackage: error: debian/rules clean subprocess was killed by signal 11` |
| 尝试 | 2 次，均一致（本包共 320.69 s：builddeps 288.79 s + source 9.96 s + build 0.95 s；第二次 1.30/9.99/0.85 s） |
| core dump | 无（guest 未开启 core） |
| 资源 | 与资源无关：`disk_free_kb=2085172`；`clean` 在 1 秒内崩溃 |
| 证据 | `target/debian-source-build/console.txt`；诊断轮次 `target/coreutils-diag2/console.txt`（工作区已清理，全文归档于 `docs/porting/evidence/2026-10-25-l4-coreutils-sig11/console-diag2.txt.gz`） |

触发构造（旧文档已定位，本轮保留）：`coreutils` 的 `debian/rules` 设置了
`DPKG_EXPORT_BUILDFLAGS = 1`，`buildflags.mk` 导出 22 个
`export VAR = $(shell dpkg-buildflags --get …)` 形式的递归变量；
`dpkg-buildpackage` 把 `DEB_*` 放进环境。make 在解析/展开这些变量时会扫描
`environ`，从而踩到下面的越界读。**触发点是 export + 递归 `$(shell)`，
根因是环境扫描越过栈顶。**

## 2. 证据链

### 2.1 页错误地址：正好在初始栈映射之外 2 字节

诊断内核（`page_fault.rs` 临时打印最近映射；该诊断**未提交**，取证后已回退）
在两次尝试中各记录一次：

```
page fault at 0x7ffffff3a002:
  nearest mapping below=Some(140737479155712..140737487544320), above=None
```

- 映射下界 `140737479155712 = 0x7fffff73a000`；
- 映射上界 `140737487544320 = 0x7ffffff3a000`；
- 故障地址 `0x7ffffff3a002 = 映射上界 + 2`；
- 区间长度 `0x800000` = **8 MiB**，即 `INIT_STACK_SIZE`；
- 上方没有任何映射 → 一次纯用户态 READ 越界，不是内核缺页处理缺陷
  （栈映射本身完整；也没有 grow-down 语义可救“栈顶之上”）。

第二次尝试：故障 `0x7ffffff6a002`，映射
`0x7fffff76a000..0x7ffffff6a000`，同样是映射上界 + 2。

### 2.2 故障指令：GNU make 的 `recursively_expand_for_file`

诊断内核读取故障上下文与指令字节（`exception.rs` 的临时打印，取证后已回退）：

```
faulting context: rip=0x5555555ba632 rsp=0x7ffffff35d20 rbp=0x7ffffff39ff2
                  rax=0x10 rbx=0x5555556b5fa0 rcx=0x5555556b8d50
                  rdx=0x5555556b5ca0 rsi=0x0 rdi=0x5555556b5ca0
faulting instruction: [42, 80, 7c, 2d, 00, 3d, 75, e6, …]
```

- `rip - PIE base = 0x5555555ba632 - 0x5555555a9000 = 0x11632`；
- 该字节序列在镜像中唯一，位于 `/usr/bin/make` 的
  `recursively_expand_for_file`（`strlen` 之后的环境扫描循环）；
- 反汇编（完整片段见证据目录）：

```asm
115f4: call   strlen@plt            ; rdi = 变量名
115f9: mov    r13, rax              ; r13 = name_len（本例 16）
11609: mov    r12, [rip+__environ]
11610: mov    rbp, [r12]            ; rbp = environ[0]
11632: cmp    BYTE PTR [rbp+r13], 0x3d   ; 读 environ[i][name_len]，与 '=' 比较
11638: jne    11620                  ; 不等 → 下一个 environ 项
1163a: … strncmp(rbp, name, r13) …
```

- `rax = r13 = 0x10`（16）；`rbp = 0x7ffffff39ff2 = 映射上界 - 0xe`（14）；
- 即 `environ[0]` 是栈顶那条 **14 字节**的字符串（含 NUL，与
  `SHELL=/bin/sh` 一致，属推断），make 读它的第 16 字节 → `上界 + 2`；
- **make 没有检查 `strlen(environ[i]) >= name_len`**：它的预检查
  `entry[name_len] == '='` 只在“字符串足够长”时才是合法访问。

### 2.3 Asterinas 的初始栈把 `envp[0]` 放在了最顶端

活树 `kernel/core/src/process/process_vm/init_stack/mod.rs`：

- `map_and_write()` 把 8 MiB VMO 映射到
  `[initial_top - 8 MiB, initial_top)`，写入器 `pos` 从 **`initial_top`** 开始；
- `write_envp_strings()` 按 `self.envp.iter()` **正序**写入（每个字符串
  向下生长），所以 `envp[0]` 最先写、位于最顶端，末尾正好是 `initial_top`；
- 之后才写 `argv`、`AT_RANDOM`、auxv、指针数组、`argc`。

`write_argv_strings()` 已经是 `iter().rev()`（`argv[0]` 在 argv 块最低端），
但 envp 没有对应处理；更关键的是**整个字符串块上方没有任何映射内字节**。

### 2.4 Linux 的布局：`AT_EXECFN` 字符串在 env 块之上

在本机 Linux（Arch，7.2.6）用同一环境实测初始栈（程序见证据目录）：

```
argv[0]=0x7fffbbfcca40  environ[0]=0x7fffbbfcca51  argv[last]=0x7fffbbfcca40
highest=0x7fffbbfcdfd4 (env[78])
最高字符串 end=0x7ffe1a580fe6  stack VMA end=0x7ffe1a581000  gap=26
end 之上的字节: 2f 74 6d 70 2f 73 74 61 63 6b 6c 61 79 6f 75 74 33 00 00…
                "/tmp/stacklayout3\0" + 对齐 0
```

- 最高的初始栈字符串是 **`AT_EXECFN` 的 exec 路径**，不是 `envp[0]`；
- `envp[0]` 在 env 块的最低端，且 env 块上方还有路径字符串 + 对齐字节；
- make 对任意 `environ[i]` 读 `entry[name_len]` 时，越界部分落在同一映射内，
  不会 SIGSEGV（本例 name_len=16、envp[0] 48 字节，甚至不会越界）；
- 这也解释了“同一 coreutils 在 Linux 上正常、只在 Asterinas 上崩”。

### 2.5 Asterinas 从未设置 `AT_EXECFN`

活树检索：

```
kernel/core/src/process/process_vm/init_stack/aux_vec.rs:46:  AT_EXECFN = 31,
```

只有枚举定义，**没有 `aux_vec.set(AuxKey::AT_EXECFN, …)`**；`load_elf.rs`
设置的 auxv 为 `AT_SYSINFO_EHDR / AT_SECURE / AT_PAGESZ / AT_PHDR /
AT_PHNUM / AT_PHENT / AT_ENTRY / AT_BASE`，`init_stack` 只补 `AT_RANDOM`。
也就是说，Linux 用户态可见的 `AT_EXECFN` 特性在 Asterinas 上**完全缺失**，
连带其字符串占据的顶部映射字节也不存在。

## 3. 判定

| 类别 | 是否成立 | 依据 |
|---|---|---|
| **A：Asterinas 缺 syscall/特性 → 能修** | **成立** | 缺 `AT_EXECFN` auxv 特性及顶部字符串布局；修复后越界读落回映射内 |
| B：上游 coreutils/make 自身 bug | 部分是（潜在 UB），但不作为结论 | make 的 `entry[name_len]` 读确实无长度检查，是潜在未定义行为；但它在 Linux（事实标准布局）上稳定工作，移植目标就是复现 Linux 行为。Asterinas 的布局差异是**可修的那一半** |
| C：资源/超时/磁盘 | 排除 | `disk_free_kb=2,085,172`；`clean` 0.85–0.95 s 内崩溃；无 OOM、无 ENOSPC、无超时 |

因此按 A 处理：做**一处**特性修复，然后只重跑 L4 一次。

## 4. 修复

### 4.1 改动

| 文件 | 改动 |
|---|---|
| `kernel/core/src/process/process_vm/init_stack/mod.rs` | writer 增加 `execfn: CString`；`write()` 最先把 execfn 字符串写在 envp 之上，并 `aux_vec.set(AT_EXECFN, ptr)` |
| `kernel/core/src/process/program_loader/mod.rs` | `ProgramToLoad` 保存 `execfn`；`from_executable()` 用 `path_resolver.make_abs_path(executable.path())` 计算**被 exec 的文件**的绝对路径（shebang 时为脚本路径，对应 Linux 的 `bprm->filename`），随 `load_to_vmar()` 下传 |
| `kernel/core/src/process/program_loader/elf/load_elf.rs` | `load_elf_to_vmar()` 增加 `execfn` 参数并传给 `map_and_write_init_stack()` |
| `kernel/core/src/process/process_vm/mod.rs` | `map_and_write_init_stack()` 增加 `execfn` 参数 |

核心写入顺序（`InitStackWriter::write`）：

```rust
// Linux lays the AT_EXECFN string out above the environment strings, so the
// bytes above envp[0] stay mapped; some programs read slightly past a short
// environment string while matching names.
let execfn_pointer = self.write_cstring(&self.execfn)?;
self.auxvec.set(AuxKey::AT_EXECFN, execfn_pointer);

// Write envp strings.
let envp_end = self.pos();
…
```

布局变化：

```
修复前（顶部）: [ envp[0] ][ envp[1] ] … [ argv … ]      envp[0] 贴住 initial_top
修复后（顶部）: [ execfn ][ envp[0] ][ envp[1] ] … [ argv … ]
                ^ AT_EXECFN 指向这里，并为 envp[0] 的越界读提供映射内落点
```

`envp_range` / `argv_range` 仍按实际写入位置记录（`envp_end` 在 execfn
之后取），`/proc/<pid>/environ`、`cmdline` 等读取路径不受影响。

### 4.2 为什么这能覆盖本次崩溃

崩溃是对 `envp[0]`（14 字节，顶端）读 `+16`：修复后 `envp[0]` 上方有
execfn 字符串（`/usr/bin/make` 或 `/bin/make` 这类路径），
`envp[0][16]` 恰好落在 `execfn_start + 2`，在映射内。若 `envp[0]` 更短，
读取位置会向 execfn 内部推移；只要不超过 execfn 长度仍然安全。这与 Linux
的顶部余量是同一类（有限）保护，对本次的 14 字节 `envp[0]` 完全覆盖。
make 的循环读到 `'='` 比较失败后继续下一个 environ 项，不再触发页错误。

## 5. 验证

### 5.1 构建与重跑参数

| 项 | 值 |
|---|---|
| 内核构建 | `OSDK_LOCAL_DEV=1 cargo osdk build --target-arch x86_64 --scheme microvm`（仓库内 `osdk/` 0.19.0 安装到 `/tmp/tp/osdk-install`；`rust-strip` 用 `llvm-strip` shim） |
| 内核 | ELF 22 MB。第一次 L4 用 **`f847a16a65498345`**；其后只做了一处 `rustfmt` 换行重排（token 不变），重建为 **`66004cd683dd451b`**，第二次 L4 用该内核 |
| L4 workspace | 第一次 `target/debian-source-build-execfn/`（未覆盖基线 `target/debian-source-build/`）；第二次 `target/debian-source-build-2/` |
| 基础根镜像 | `target/systemd-health/daily.ext2`（`X86_DEBIAN_XORG_BASE_ROOT`） |
| 重跑次数 | 归因后一次（`execfn` workspace）+ 提交后源码重建一次（`-2` workspace） |

### 5.2 第一次重跑（内核 `f847a16a`，3 包 2 ok / 1 fail）

| 包 | 结果 | 耗时 | 关键数字 |
|---|---|---|---|
| `hello` | **ok** | 282.73 s | build 87.81 s；2 个 `.deb` |
| `coreutils` | **fail** | 561.63 s | **`debian/rules clean` 已通过（signal 11 消失）**；两次都在 `configure` 阶段退出（build rc=2，194.27 s / 203.02 s） |
| `python3-minimal` | **ok** | 366.56 s | build 28.07 s；19 个 `.deb` |

`DPKGB summary total=3 ok=2 fail=1`；`GATE_EXIT=0`。

**关键结论：本轮修复命中了原故障。** 原崩溃点 `debian/rules clean` 已正常完成，
`coreutils` 的构建推进到 `debian/rules:37 override_dh_auto_configure`
（随后 `binary` 报 Error 2），不再是 SIGSEGV。

### 5.3 新暴露的失败：`configure` 的 root 保护（环境问题，非内核）

guest 日志 `/var/tmp/dpkg-logs/pkg2-build.log`（随 workspace 镜像保留）：

```
checking for mknodat... yes
checking whether mknod can create fifo without root privileges...
configure: error: in '/tmp/dpkg-build/pkg2/coreutils-9.7':
configure: error: you should not run configure as root
  (set FORCE_UNSAFE_CONFIGURE=1 in environment to bypass this check)
…
make[1]: *** [debian/rules:37: override_dh_auto_configure] Error 25
```

- GNU coreutils 的 `configure` 在 “mknod 能否在非 root 下创建 fifo” 的
  运行测试前有 root 保护；guest 里 `dpkg-buildpackage` 以 **root** 运行，
  因此退出 1。Debian 官方构建使用非 root 构建用户，不会触发。
- 这与内核无关：同一镜像/同一命令在 Linux 上以 root 构建也会得到同样的
  错误。**新失败归类 B（环境/构建用户）**，不是 A。
- 最小后续（未在本轮实施，遵守“只改一处 + 只重跑一次”）：在 L4 的
  `dpkg-buildpackage` 环境里加 `FORCE_UNSAFE_CONFIGURE=1`（或改用非 root
  构建用户），然后再跑一次 L4。coreutils 的 `clean` 已证明不再崩，
  该环境项是它进入真正编译前的最后一道门。

证据：`target/debian-source-build-execfn/source-build/`（TSV/JSON）与
`console.txt`；guest 日志已归档为
`docs/porting/evidence/2026-10-25-l4-coreutils-sig11/pkg2-build.log.gz`
（原 workspace 的 4 GiB 镜像已清理）。第二次重跑的 guest 日志仍可从
`target/debian-source-build-2/daily.ext2:/var/tmp/dpkg-logs/pkg2-build.log`
取到。

### 5.4 第二次重跑：提交后源码重建（`target/debian-source-build-2/`）

按 `EXECFN.md` 的重跑要求，用**已提交源码重建**的内核
（sha256 `66004cd683dd451b`，与第一次仅差一处 `rustfmt` 换行）在全新
workspace 再跑一次完整 L4：

| 包 | 结果 | 耗时 | 关键数字 |
|---|---|---|---|
| `hello` | **ok** | 275.66 s | build 86.59 s；2 个 `.deb` |
| `coreutils` | **fail** | 560.55 s | `clean` 通过；两次都在 `configure` 以 root 保护退出（build rc=2，194.44 s / 204.22 s） |
| `python3-minimal` | **ok** | 364.16 s | build 27.60 s；19 个 `.deb` |

`DPKGB summary total=3 ok=2 fail=1`；`GATE_EXIT=0`。guest 日志
`/var/tmp/dpkg-logs/pkg2-build.log` 再次出现
`you should not run configure as root`。结论与第一次逐项一致：
**signal 11 未复现，原故障已修复；L4 不是 3/3 的原因完全是 root
构建保护，不是第二次越界。**

## 6. 残留风险与后续

1. **make 的越界读本身仍是未定义行为**：修复提供的是与 Linux 同类的
   “顶部路径字符串 + 对齐字节”映射内余量，其大小受 exec 路径长度限制；
   若某个程序查找的变量名长度超过 `envp[0] 长度 + execfn 长度`，理论上仍可能
   越界（Linux 同样如此，只是其路径/对齐恰好够用）。这不是内核能给出的保证，
   长期应推动上游 make 做长度检查。
2. **Asterinas 的 auxv 仍不完整**：`AT_PLATFORM`、`AT_HWCAP`、`AT_UID/
   AT_EUID/AT_GID/AT_EGID`、`AT_CLKTCK` 等也未设置（本轮未改，避免扩大
   范围）；这些是后续独立的小项，需逐项对照 Linux 并用 LTP/程序验证。
3. **envp 写入顺序与 Linux 仍不同**（`envp[0]` 在最顶端，Linux 在 env 块
   最低端）。本轮不改：修复点已足以覆盖越界读，改顺序属于额外的布局对齐，
   需要单独评估兼容性收益与风险。
4. **基线更新**：L4 仍为 **2 ok / 1 fail**，但失败原因已从 signal 11 变为
   root 构建环境（两次重跑一致）。应把 `docs/porting/debian-source-build.md`
   的“已知失败”与判定规则改为指向本文，并在 `daily-use-matrix.md`、
   `2026-10-25-ladder-final.md` 的 L4 行注明“signal 11 已由内核
   `f847a16a`/`66004cd6` 修复，剩余为 `configure` 的 root 保护”。
   在 L4 环境中加 `FORCE_UNSAFE_CONFIGURE=1` 并重跑全绿后，再把基线更新为
   **3 ok**。

## 证据索引

- `docs/porting/evidence/2026-10-25-l4-coreutils-sig11/console-excerpt.txt`
  （两次页错误 + 故障上下文 + 指令字节）
- `…/console-diag2.txt.gz`、`…/console-attr.txt.gz`（诊断轮次串口全量）
- `…/console-execfn-l4.txt.gz`（第一次重跑串口全量）
- `…/pkg2-build.log.gz`（coreutils guest 构建日志，含 configure root 保护）
- `…/make-disassembly.txt`（`recursively_expand_for_file` 反汇编）
- `…/linux-layout.txt`（Linux 初始栈布局实测）
- `target/debian-source-build-execfn/`（第一次重跑 workspace）
- `target/debian-source-build-2/`（第二次重跑 workspace，含完整 guest 镜像）
