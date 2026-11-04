# 推送前隐私检查清单

日期：2026-11-05

本仓库是下游分支（`origin` = `TankTechnology/asterinas-riscv`）。**默认不推送**；
确需推送时，按下表逐项处理，处理完再决定推哪个远端（绝不要推 `upstream`）。

## 必须处理的项

| 项 | 位置 | 处理方式 |
| --- | --- | --- |
| 个人邮箱（`patches-from-riscv/` 里补丁的 `From:` 行） | `docs/porting/patches-from-riscv/*.patch`（引入自提交 `365c67448`） | 推送前用 `git filter-repo`/交互式变基替换为项目地址或删除该提交；这是改写历史，必须在**尚未推送**时做 |
| 板子/网关/实验室内网 IP | 文档、`tools/riscv/board/*`、`tools/riscv/compat/*`、性能文档 | 替换为 `<board>`、`<gateway>` 等占位符，或改为示例地址段 |
| 校园门户主机名与参数名 | `tools/riscv/board/megrez_portal_login.py`、相关文档 | 保留脚本逻辑，主机名改为可配置（默认值用占位符） |
| SSH 密钥路径 | 文档 | 无害但可统一为 `~/.ssh/<key>` |

## 已确认不在 git 中的东西（2026-11-05 全对象扫描）

- 校园门户账号/密码；
- DeepSeek / Kimi API key；
- 会话中任何一次 `stty -echo` 传递的凭据。

扫描方法：对 `git cat-file --batch-all-objects` 的全部对象做字节包含检查，覆盖
所有分支与不可达对象（当时 137,859 个对象，0 命中）。

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
