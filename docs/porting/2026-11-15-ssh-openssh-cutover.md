# SSH 切到 OpenSSH：从"假 pty"到真终端（2026-11-15）

这块板子上 `ssh -tt board` 一直拿不到 tty：`test -t 0` 为假，`/dev/pts/` 是空的。
本文记录它到底是什么、怎么一步步定位、以及怎么在板上完成切换（含验收与回滚）。

结论先行：**不是内核的 bug**，是板子上那个 dropbear 二进制被我们打了"假 pty"补丁；
内核的 pty 与 SCM_RIGHTS 早已修好，只是这块板子的镜像比那次改动旧。现已切到 OpenSSH。

## 1. 症状与三层原因

```
$ ssh -tt board 'test -t 0 && echo tty || echo not-tty'
not-tty
$ ssh -tt board 'readlink /proc/self/fd/0'
pipe:[412]
```

1. **二进制层**：`/usr/sbin/dropbear` 是仓库里 check-in 的
   `tools/riscv/debian/rootfs/dropbear-patched-riscv64`，对应
   `dropbear-asterinas-fake-pty.patch`。补丁把 `sessionpty()` 里的
   `pty_allocate()` 整段删掉，只应答客户端的 pty 请求并吃掉 `sessionwinchange` /
   `get_termmodes` 两个包，`sessioncommand()` 因为 `master == -1` 落到
   `noptycommand()`，数据全走管道。客户端以为自己有终端，服务端什么都没分配。
2. **镜像层**：构建脚本（`build_rootfs.sh`）现在已经默认装 OpenSSH
   （`asterinas-openssh.service`），dropbear 只作为禁用的兜底。但这块板子的
   rootfs 是更早一代，**它带了 `asterinas-debug-console.target.wants/` 里的启用
   链接，却没装 unit 文件**——悬空链接被 systemd 静默忽略，于是 22 端口继续由
   dropbear 提供。
3. **历史层**：补丁写于 2026-11-04，当时内核确实有两个 bug 让 pty 中继打不通
   （`poll()` 同一 fd 注册两次时掩码被覆盖、SCM_RIGHTS 携带 pty master 被判成
   非叶节点）。两个都修了（见 `2026-11-04-remote-access-and-deepseek.md`），
   OpenSSH 的 `ssh -tt` 随后验证通过，所以补丁的理由已经消失。

## 2. 怎么调试（可复现）

按"先证伪内核、再定位服务器"的顺序，全部在板上远程完成：

1. **看谁在听 22**：`ss -lnt` + `systemctl is-active asterinas-dropbear`。
2. **看二进制来源**：`ls -l /usr/sbin/dropbear`（大小/时间戳与仓库里那份一致），
   再读 `dropbear-asterinas-fake-pty.patch`——一眼就能看到 `pty_allocate` 被删。
3. **证伪内核 pty 栈**（三条独立证据）：
   - `python3 -c "import pty; pty.openpty()"` → 成功，`ttyname` 是 `/dev/pts/0`；
   - `setsid()` + `ioctl(TIOCSCTTY, 1)` → 成功（内核那条
     `stealing TTY from another session is not supported` 只是 warn，
     `set_control` 随后成功，是红鲱鱼）；
   - `script -qc 'test -t 0 && echo OK' /dev/null` → `OK` + `/dev/pts/0`。
4. **同内核换服务器做对照（关键）**：把 Debian 自带的 OpenSSH 起在备用端口

   ```
   mkdir -p /run/sshd
   /usr/sbin/sshd -p 2222 -o PermitRootLogin=prohibit-password -o UsePAM=no
   ```

   `ssh -tt -p 2222 …` 立刻得到 `STDIN_TTY` / `/dev/pts/0` / 正常回显。同一内核、
   同一网络，只换服务器就好 → 问题在 dropbear。
5. **顺带压测中继**（历史症状是"写回不来"）：`seq 1 20000` 出方向 20 000 行、
   `seq 1 20000 | ssh -tt … wc -l` 进方向 20 000 行，双向无丢无卡。

## 3. 板上完成切换

仓库的 unit 直接可用（`tools/riscv/debian/rootfs/asterinas-openssh.service`），
`WantedBy=asterinas-debug-console.target`，`ExecStartPre` 清 `/run/nologin`（本镜像
起不到 `systemd-user-sessions.service`），`KeyringMode=inherit`。

```
# 串口控制台上做（端口 22 的服务切换会掐断自己的 SSH 会话）
install -m 0644 asterinas-openssh.service /etc/systemd/system/
systemctl daemon-reload
systemctl disable --now asterinas-dropbear.service
systemctl enable --now asterinas-openssh.service
```

坑：**不要通过 22 端口的会话执行"停 dropbear"那一步**——会话进程所在的 cgroup 会
被一起收掉，后面的命令不再执行（本次就是这样把 openssh 停在了 enabled 但未启动）。
用串口控制台，或者先在一个不依赖 22 的通道（例如临时 sshd 2222）上做。

桌面用户（`asterinas`）的登录密钥用镜像自带工具加：

```
/usr/lib/asterinas/add-ssh-key 'ssh-ed25519 AAAA... comment'
```

## 4. 验收（2026-11-15，重启前后各一遍）

| 项 | 结果 |
| --- | --- |
| `ssh -tt root@board 'tty'` | `/dev/pts/0`，`test -t 0` 为真 |
| `ssh -tt asterinas@board` | `bash`、pty、家目录正确（login shell 包装脚本走 `bash -l` 分支） |
| scp 32 KiB / 128 KiB 往返 | 两端 sha256 一致 |
| sftp `ls` | 正常（`/usr/lib/openssh/sftp-server` 可解析） |
| rsync 单文件 | 校验一致 |
| 窗口尺寸 | 本地 pty 设 120×40 → 板上 `stty size` 读到 `40 120` |
| 全屏程序 | `vi /etc/hostname` 正常进 alternate screen，`:q!` 退出 rc=0 |
| 重启后 | `asterinas-openssh=active`、`asterinas-dropbear=inactive`、`--failed` 为空、`-tt` 仍可用 |
| 桌面 | desktop / browser-web / vnc 全部 active，不受影响 |

## 5. 回滚

```
systemctl disable --now asterinas-openssh.service
systemctl enable --now asterinas-dropbear.service
```

两个 unit 都监听 22，只能有一个启用；串口控制台始终可用，所以回滚不需要动镜像。
`asterinas-login-shell` 仍保留 `script` 兜底：如果哪天又换成 no-pty 的服务器，
交互式登录依然能拿到板侧 pty。

## 6. 后续（镜像侧）

- 新镜像已按 `build_rootfs.sh` 装 unit 并启用；**检查目标是 unit 文件本身存在**，
  不只是 `target.wants` 里的链接——本次事故就是链接在、文件不在。
- 这份镜像里 `ssh.service` / `sshd.service` 仍是 masked（与 `asterinas-openssh`
  同一端口，保持屏蔽是对的）；`asterinas-dropbear` 保留为禁用兜底。
- `dropbear-asterinas-fake-pty.patch` 与那份二进制可以等下一次镜像大版本时再决定是否移除。
