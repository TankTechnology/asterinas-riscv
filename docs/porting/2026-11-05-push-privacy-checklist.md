# 推送前隐私检查清单

日期：2026-11-05

本仓库是下游分支（`origin` = `TankTechnology/asterinas-riscv`）。**默认不推送**；
确需推送时，按下表逐项处理，处理完再决定推哪个远端（绝不要推 `upstream`）。

## 已确认的策略（2026-11-05）

| 项 | 处理 |
| --- | --- |
| 个人 QQ 邮箱（`patches-from-riscv/` 的 `From:` 行） | **无需处理**（用户确认可公开） |
| **实验室内网 IPv4**（板子、网关、实验室网段） | **推送前必须替换**为占位符 |
| **SSH 密钥路径**（`.ssh/<board-key>` 之类） | **推送前必须替换**为 `~/.ssh/<key>` |
| 校园门户主机名 | 视为内网设施，同样替换为占位符；脚本保留可配置项 |

当前规模（本地，2026-11-05 统计）：实验室内网 IPv4 出现在 **188 个文件、838 处**；
SSH 密钥路径出现在 **4 个文件**。

其中 `kernel/src/net/iface/init.rs` 把板子的启动地址**硬编码**在源码里（31 处
相关地址），不是纯文档文本：发布前应改为可配置项（环境/编译期默认值），
不能用一刀切的文本替换，否则内核会带着占位符启动失败。

## 已确认不在 git 中的东西（2026-11-05 全对象扫描）

- 校园门户账号/密码；
- DeepSeek / Kimi API key；
- 会话中任何一次 `stty -echo` 传递的凭据。

扫描方法：对 `git cat-file --batch-all-objects` 的全部对象做字节包含检查，覆盖
所有分支与不可达对象（当时 137,859 个对象，0 命中）。

## 脱敏流程（地址清单只放本地，不入库）

模式清单放在 `/tmp/publish-sanitize.sed`（或任何不被 git 跟踪的位置），仓库里
只保留流程说明：

```sh
# 1) 本地维护替换规则（不入库）：把实验室网段与密钥路径映射到占位符
# 2) 只对将被推送的跟踪文件做替换
git grep -l -I -E '<由本地清单生成的模式>' | xargs sed -i -f /tmp/publish-sanitize.sed
# 3) 人工复核 kernel/src/net/iface/init.rs 等"硬编码功能值"的改动
# 4) 重新扫描确认零命中后再提交/推送
```

## 每次推送前的复核命令

```sh
# 1) 凭据（从环境/密钥环读取，值不落盘、不进 argv）
python3 tools/riscv/compat/credential_scan.py   # 如后续实现

# 2) 看将要推送的内容里有什么
git log origin/main..HEAD --stat

# 3) 内网地址与个人标识
git log origin/main..HEAD -p | grep -nE '\b(10|192\.168|172\.(1[6-9]|2[0-9]|3[01]))\.[0-9]+\.[0-9]+' | head
git log origin/main..HEAD -p | grep -nE '@qq\.com|@gmail\.com' | head
```

## 同步策略

- `origin/main` 可能已有新提交：先 `git fetch origin`，再变基或合并，**不要强推**。
- 推送只针对 `origin`；`upstream` 是上游 asterinas，本仓库不向其提 PR。
- 脱敏若涉及已推送的提交，需要 `git filter-repo` + 强推并通知协作者；未推送时
  直接改写本地历史即可。
