# 板子桌面可以从任意机器远程访问（2026-11-14）

## 1. 结论

板子的 1920×1080 桌面现在跑在 VNC 上，任何一台能路由到板子的机器都能连——

```
主机 58.206.198.47/22  ──校园网路由──  板子 58.206.232.122/24:5900
```

两条机器不在同一子网，路是通的（整晚的 SSH 就走这条路），但**板子的门户认证必须
有效**：未认证时校园网只放行门户本身，任何到板子的连接都会超时。

## 2. 组成

| 部分 | 位置 |
| --- | --- |
| 服务单元 | `tools/riscv/debian/rootfs/asterinas-vnc.service` → 板 `/etc/systemd/system/` |
| 密码文件 | `/etc/asterinas/vnc-passwd`（0600，由 `x11vnc -storepasswd` 生成） |
| 日志 | `/run/asterinas-vnc.log`（记录每个连入地址） |
| 监听 | `0.0.0.0:5900`，RFB 003.008 + VNC 认证 |

`x11vnc -loop` 让它跟着 X 显示一起重启；单元挂 `asterinas-debug-console.target`
（这个镜像真正会激活的 target），`After=asterinas-desktop-m5.service`。

## 3. 怎么连

主机上已经有 `remmina` 和 `gvncviewer`：

```sh
gvncviewer 58.206.232.122:5900          # 或
remmina -c vnc://58.206.232.122:5900    # 密码见 /etc/asterinas/vnc-passwd
```

更安全的做法是走 SSH 隧道，不把 5900 暴露在校园网上：

```sh
ssh -L 5900:127.0.0.1:5900 -p 22 root@58.206.232.122
gvncviewer 127.0.0.1:5900
```

## 4. 验收

主机侧用 RFB 客户端完整走了一遍认证并抓帧：

```
banner RFB 003.008
security types [2]          ← VNC 认证
authenticated
desktop 1920x1080 name=b'asterinas:0'
saved /tmp/vnc-frame.png (1067 distinct scanlines)
```

抓到的画面就是板子的桌面：Firefox 开着网页、lxpanel 在底部、壁纸与时钟都在。
重启后 `asterinas-vnc` 自动回来（`systemctl is-active` = active），主机侧重新认证
抓帧同样成功，启动总时长 26.9 s。

## 5. 注意事项

- **密码只有 8 位有效**：这是 VNC 认证协议（DES 挑战）的上限，不是我们的选择。
  在校园网上裸开 5900 时，它比"没有密码"好，但不如 SSH 隧道。
- 密码文件只存在板上；重新生成会与现有客户端断开。
- 性能：软件渲染 + 单流，1920×1080 下拖动窗口会有可见延迟；`-noxdamage` 已开启
  （我们的 X 没有 XDAMAGE 支持时的必需项）。
