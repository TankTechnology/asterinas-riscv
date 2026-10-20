# Component Testing M0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> 本计划仅规定 M0 的只读调查与文档交付，不授权实现测试底座。
> 采用何种执行方式仍服从用户授权和当前 agent 协作规则。

**Goal:** 明确首个真实源码宿主测试的依赖边界、校准条件和第二案例候选，给出可审查的 M1 go/no-go 决策。

**Architecture:** 复用现有组件源码，后续通过测试专用环境承载选中的依赖组合。
M0 不创建 `asterinas-lite`、不移植 OSTD、不修改生产接口。
跨子系统整体方案见[设计文档](../specs/2026-09-10-component-testing-design.md)。

**Tech Stack:** Git 对象读取、GitHub CLI 显式指定仓库、Markdown 记录。
Rust 与 OSDK 在本阶段只做配置审查，不执行编译。

---

## 执行约束与文件边界

本文中的命令均在 `/mnt/shared/xaj/Program/asterinas` 下执行。
在其他工作区执行时，先核对该工作区的 AGENTS.md 和对象可用性。
命令读取固定提交，不将正在变化的工作区内容替换进历史案例。

本计划未来允许新增的唯一报告文件：

- `docs/superpowers/plans/2026-09-10-component-testing-m0-findings.md`。

本次写方案不创建这份调查结果文件，不把已有聊天分析冒充新一轮 M0 验收。
执行 M0 时用 apply_patch 新建；若该路径已存在，先读取并确认归属，不覆盖。
不得修改 `kernel/`、`ostd/`、根 Cargo 配置、Makefile、Docker launcher、CI 或已有模型。
不得提交、拉取、切分支、运行构建、操作板卡或更新 GitHub issue；这些需要独立授权。

## Task 1：核对工作区与固定对象

**Read:** `AGENTS.md`、Git 状态与对象。
**Record:** findings 文件的“身份与范围”部分。

- [ ] 读取根目录规则和现有修改：

```bash
cat AGENTS.md
git status --short --branch
```

预期：得到当前工作区规则和变更列表。
已有变更全部视为他人工作，不要求工作区干净，也不清理文件。

- [ ] 核对三个固定提交：

```bash
git rev-parse --verify 'c20bfac0f254defff82f72ab4c6eef2fb1af9fcd^{commit}'
git rev-parse --verify 'e20f2832bec51af6b6f501811247d67ae052d190^{commit}'
git rev-parse 'e20f2832bec51af6b6f501811247d67ae052d190^'
```

预期依次输出调查基线、修复提交和父提交，父提交应为：

```text
d93cd066d7afdb1c6531b52ba6bfbb223ec4e4f0
```

对象缺失时记录缺失，不自行 fetch 或改用相近版本。

- [ ] 读取跟踪 issue，不依赖 gh 默认仓库：

```bash
gh issue view 138 --repo TankTechnology/asterinas-riscv --json number,title,body,url,state
```

预期 URL 指向 `TankTechnology/asterinas-riscv/issues/138`。
若正文已有新决策，与本方案的差异单独列出；不得静默覆盖任一方。

## Task 2：还原 SCM 修复与真实依赖

**Read:** SCM 图、锁、等待队列、原子上下文和 Task 实现。
**Record:** findings 的“源文件清单”“保留/替换边界”和“历史判据”。

- [ ] 阅读修复，不只看 commit 标题：

```bash
git show e20f2832bec51af6b6f501811247d67ae052d190 -- kernel/src/net/socket/unix/scm_graph.rs kernel/src/net/socket/unix/scm_graph/test.rs
```

预期：图锁从 Mutex 改为 SpinLock；新增测试检查类型。
记录现有测试并未动态构造旧版争用，不能把它当作旧版已运行失败的证据。

- [ ] 阅读全部候选文件，而不把 `crate::prelude::*` 当作实际依赖全集：

```bash
git show c20bfac0f254defff82f72ab4c6eef2fb1af9fcd:kernel/src/net/socket/unix/scm_graph.rs
git show c20bfac0f254defff82f72ab4c6eef2fb1af9fcd:ostd/src/sync/mutex.rs
git show c20bfac0f254defff82f72ab4c6eef2fb1af9fcd:ostd/src/sync/wait.rs
git show c20bfac0f254defff82f72ab4c6eef2fb1af9fcd:ostd/src/sync/spin.rs
git show c20bfac0f254defff82f72ab4c6eef2fb1af9fcd:ostd/src/sync/guard.rs
git show c20bfac0f254defff82f72ab4c6eef2fb1af9fcd:ostd/src/task/atomic_mode.rs
git show c20bfac0f254defff82f72ab4c6eef2fb1af9fcd:ostd/src/task/preempt/guard.rs
```

若输出截断，按文件分段读到末尾。
列出实际引用的类型、宏、全局状态及所需方法。
每项标记为复用源码、测试专用适配、排除或需进一步调查。

- [ ] 核对真正的切换边界：

```bash
git show c20bfac0f254defff82f72ab4c6eef2fb1af9fcd:ostd/src/task/mod.rs
git show c20bfac0f254defff82f72ab4c6eef2fb1af9fcd:ostd/src/task/scheduler/mod.rs
git show c20bfac0f254defff82f72ab4c6eef2fb1af9fcd:ostd/src/task/processor.rs
git show c20bfac0f254defff82f72ab4c6eef2fb1af9fcd:ostd/src/lib.rs
```

必须回答：park 何时直接返回，何时请求实际切换；
`might_sleep` 在何处执行，bootstrap 豁免怎样结束；
为何真实 Task 与 `switch_to_task` 不适合作为小原型的保留边界。

- [ ] 比较修复前后依赖身份：

```bash
git diff d93cd066d7afdb1c6531b52ba6bfbb223ec4e4f0 e20f2832bec51af6b6f501811247d67ae052d190 -- ostd Cargo.toml Cargo.lock rust-toolchain.toml
```

输出为空表示这些路径在该次修复中没有变化，不代表与调查基线相同。
若有差异，列明哪些会影响测试；不从较新的调查基线直接拼装旧组件而省略说明。

- [ ] 额外比较历史版本与调查基线，防止将较新语义套入旧案例：

```bash
git diff d93cd066d7afdb1c6531b52ba6bfbb223ec4e4f0 c20bfac0f254defff82f72ab4c6eef2fb1af9fcd -- ostd/src/sync ostd/src/task ostd/src/lib.rs ostd/Cargo.toml Cargo.toml Cargo.lock rust-toolchain.toml
```

方案编写时，这些指定路径的差异为空；执行 M0 时仍应重新核对。
它不代表全部依赖闭包已经完成核验。
新发现的传递依赖也加入比较；出现差异时，读取对应历史版本的完整实现。
校准中的 Task、park、唤醒检查和 `might_sleep` 以历史版本自身语义为准。

## Task 3：明确构建和执行隔离，不运行实验

**Read:** OSTD 依赖、工具链和开发容器说明。
**Record:** findings 的“宿主构建风险”“安全前提”。

- [ ] 审查当前依赖和工具链：

```bash
git show c20bfac0f254defff82f72ab4c6eef2fb1af9fcd:ostd/Cargo.toml
git show c20bfac0f254defff82f72ab4c6eef2fb1af9fcd:rust-toolchain.toml
cat tools/docker/README.md
```

调查基线工具链为 `nightly-2026-07-21`。
历史 old/new 的工具链也需分别记录，不假定与调查基线相同。
记录 nightly、裸机 target 依赖、宏、cfg、独立 workspace 及源码装配的风险。
容器文档若存在他人修改，只读取，不改写。

- [ ] 给出 M1 的权限与资源前提，不以“Docker”替代安全分析。

报告必须说明：构建遵循项目容器规则；待测程序如何以非特权身份执行；
怎样限制 CPU 时间、内存、进程数和文件访问；
怎样阻止真实设备/网络访问；
如何由父进程识别 panic、超时和基础设施错误。
若现有环境无法满足，记录必须先解决的条件，不运行试验来碰运气。

- [ ] 审查 `ostd/host-test/` 作为测试边界的适用性。

它是拟议位置，不是现有能力。
列出原始 unsafe 来源和 guard 的假设，确认没有要求在 `kernel/` 添加 unsafe。
若需要改变 OSTD 的安全契约，M1 进入接口/安全评审，不由适配器私下规避。

## Task 4：盘点已有测试与第二案例

**Read:** Makefile、现有宿主模型、Ramfs/PageCache、syscall 和 Git 历史。
**Record:** findings 的“现有设施”“跨子系统候选”。

- [ ] 定位已有入口和模型：

```bash
git show c20bfac0f254defff82f72ab4c6eef2fb1af9fcd:Makefile
git show c20bfac0f254defff82f72ab4c6eef2fb1af9fcd:tools/riscv/dwmac_rx_liveness_model.rs
git show c20bfac0f254defff82f72ab4c6eef2fb1af9fcd:tools/riscv/tests/test_dwmac_rx_liveness_model.py
```

区分普通 crate 测试、内核 ktest、用户态回归和手写模型。
记录哪些机制可复用，不能把模型测试数计入真实组件源码覆盖数。
只读命令不产生运行时间证据，耗时字段应明确记为未测量。

- [ ] 审查非并发的文件系统候选：

```bash
git show c20bfac0f254defff82f72ab4c6eef2fb1af9fcd:kernel/src/fs/fs_impls/ramfs/fs.rs
git show c20bfac0f254defff82f72ab4c6eef2fb1af9fcd:kernel/src/vm/page_cache/mod.rs
git show c20bfac0f254defff82f72ab4c6eef2fb1af9fcd:kernel/src/syscall/truncate.rs
git log --oneline -20 c20bfac0f254defff82f72ab4c6eef2fb1af9fcd -- kernel/src/fs/fs_impls/ramfs kernel/src/vm/page_cache kernel/src/syscall/truncate.rs
```

记录内存文件系统仍需哪些内核依赖。
可调查创建权限或截断/扩展等语义，但必须区分“有历史修复证据”与“拟议性质测试”。
若找到适合的历史修复，读完整 diff 和回归测试后再列为入选候选。
不存在合适故障证据时，不编造失败提交来满足清单。

- [ ] 阅读另外两个边界案例：

```bash
gh issue view 132 --repo TankTechnology/asterinas-riscv --json number,title,body,url,state
gh issue view 94 --repo TankTechnology/asterinas-riscv --json number,title,body,url,state
```

分别记录未驻留用户页访问、真实设备完成可见性会怎样影响宿主测试结论。
不得推断已经解决它们，或将通用 QEMU 网络通过当作 DWMAC 硬件通过。

- [ ] 为网络、文件系统、syscall、驱动各给出一个边界判断。

每项包含：目标代码、已知/候选性质、保留依赖、替代依赖、独立判据、真实性限制和接入成本判断。
选出一个独立于 SCM 的非并发候选；允许结论为“当前不适合宿主化”。
没有第二个合适案例时，可以建议只做 M1 边界实验，但不能承诺跨子系统可复用。

## Task 5：形成首个 pilot 的验收设计

**Read:** 设计文档第 6、8、10 节。
**Record:** findings 的“场景与结果判据”“M1 决策”。

- [ ] 将五个场景逐一写成前置条件、输入动作、关键事件和预期判据。

五个场景为：旧版无争用 atomic Drop、旧版有争用 atomic Drop、
旧版普通上下文等待、新版有争用 atomic Drop，以及等待前唤醒。
两个争用版本必须均有争用证据，不能以新版偶然走快路径作为通过。

- [ ] 明确重复试验与失败分类。

采用设计中的每场景至少 100 次有效运行、初始最多 200 次总尝试的工程验收门槛。
有效运行必须满足该场景前置条件；保存所有尝试及试验前固定的超时/资源预算。
构建失败、未满足前置条件、无关 panic、超时与目标判据分别报告。
预算耗尽但有效运行不足时不通过验收；未解释的异常不能从结果中删去。
旧版的预期违约不等于该组件 PASS，宿主校准不等于已经在内核复现。

- [ ] 明确代码预算和停止条件。

记录首个原型累计新增 2,000–5,000 行的粗估，以及复用源码不计入的口径。
列出最可能突破预算的依赖；若需要整个 OSTD 移植，推荐重新划界或采用精简 QEMU。
不在本阶段写出尚不存在的宿主测试命令，也不声称编译可行性已验证。

## Task 6：交付只读调查报告并审查

**Create after M0 execution:** `docs/superpowers/plans/2026-09-10-component-testing-m0-findings.md`。
**Do not modify:** 所有代码、构建配置、已有研究资料和 GitHub issue。

- [ ] 用 apply_patch 整理以下部分，保持事实与建议分离：

1. 身份与范围：调查提交、历史 old/new、工具链、读取的 issue、允许的操作。
2. 源文件清单：路径、对象身份、实际依赖与保留/替换决定。
3. 首个边界：Task/park/unpark 的语义、排除项和安全前提。
4. 跨子系统候选：四类边界与独立的非并发候选。
5. 验收设计：场景、判据、重复次数和结果分类。
6. 成本：已有测试入口、未测量的耗时、代码预算与风险。
7. 决策：允许进入 M1 设计、要求先解决具体条件，或不建议宿主化。

- [ ] 执行文档级检查：

```bash
git diff --check --no-index -- /dev/null docs/superpowers/plans/2026-09-10-component-testing-m0-findings.md
git status --short --branch
```

第一条仅检查新报告的空白问题，不是内核测试。
`--no-index` 会因新文件与空文件存在差异而返回 1；
应结合诊断输出判断空白问题，不能仅凭返回 1 就认定检查工具失败。
状态中只能包含执行者新增的报告以及执行前已存在或其他 agent 新产生的变更。
若出现非预期差异，调查归属，不回滚他人的改动。

- [ ] 由未参与编写的读者核对：

能否识别首个执行范围；是否误把推断当成运行结果；
old/new 的判据是否一致；是否把模型或宿主告警算成真实 bug；
是否仍需全内核迁移才能运行；有哪些明确的 no-go 条件。
修正文档中的实质问题后，向用户交付链接与决策，不自动进入 M1。

## M0 完成定义

完成的是一份有源码证据的原型决策，不是已经实现组件测试。
所有“尚未运行”的项目保持该状态，不能用文档审查通过替代编译和执行结果。
若用户批准进入 M1，再创建隔离工作区及代码级计划，
按真实 adapter 单测、历史正负控制、结果分类和复现依次实施。
