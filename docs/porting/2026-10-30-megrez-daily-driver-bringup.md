# Megrez：日常可用性收口（时钟 / 联网 / 浏览 / 存储故障恢复）

日期：2026-10-30
硬件：Megrez（EIC7700，4×RV64 @1.6 GHz，16 GiB）+ 校园网有线口

## 1. 现在一次普通上电会发生什么

| 阶段 | 结果 |
| --- | --- |
| U-Boot `bootcmd` | `dhcp` 协商 PHY（可选，内核自己也能协商）→ 引导 `/root/kernel-rel.img` |
| 内核 | 约 1.1 s；`eth0` 直接可用，不需要 U-Boot 先联网 |
| systemd | 约 38 s 到 `graphical.target`（此前 57 s） |
| 时钟 | 桌面服务 `ExecStartPre` + 浏览器 wrapper 各对一次时，源是网关的 HTTP `Date:` |
| 桌面 | Xorg + openbox 会话 |
| 浏览器 | Firefox 以 `--marionette` 起在 `about:blank`，wrapper fork 的导航器把它开到目标页 |
| 验收 | `tools/riscv/megrez_smoke.sh --reboot` 五步全绿 |

## 2. 这一轮修掉的五个缺陷

1. **PHY 协商**：驱动只读不协商；补上后仍失败，真因是选择窗口 3 s < RTL8211F 需要的
   ~3.5 s，且重启协商会先把已有链路打断。窗口放到 10 s，已连上的端口不再重启。
2. **时钟**：同步器写死旧实验室代理地址；单元还写成 `DefaultDependencies=no` +
   `WantedBy=basic.target`，这种组合在本镜像里**永不运行**。改为网关优先 + 挂在桌面
   服务的 pre-start 上。
3. **浏览器空白页**：Firefox 只被起在 `about:blank`，没有任何驱动去导航；新增
   `browser-web-navigate`（Marionette），由 wrapper fork，不依赖 systemd 作业调度。
4. **eMMC 卡死**：多块传输的 auto CMD12 失败（bit24）后驱动只复位数据线，卡还停在等
   stop 的状态，此后所有传输 EIO、连 `reboot` 都读不进来。改为复位 CMD+DAT、清中断、
   补发 CMD12。
5. **TCP 序号 panic**：smoltcp 的 `SeqNumber - SeqNumber` 断言被越窗报文触发，整机
   halt。vendor 到 `third_party/smoltcp` 并改为饱和。

## 3. 崩溃后的人工/自动善后

崩溃时正在写的文件会留下「目录项在、inode 没了」的名字：`stat`/`rm` 都返回
`ESTALE`。表现是 Firefox 永远打不开窗口（`user.js` 写不进去），而它自己的日志里
什么都没有。

两条处理：

- **自动**：wrapper 在 profile 播种失败时把整个 profile 目录改名让开、重建再试一次，
  控制台打 `ASTERINAS_FIREFOX_WEB_PROFILE_ROTATED`；
- **检测**：看到这条标记就说明上一次是**非正常掉电/崩溃**，需要留意数据。

真正的根治是离线 `e2fsck`（挂载中的根无法就地修复），列入后续。

## 4. 验收入口

```bash
# 控制台桥（另开一个终端常驻）
tools/riscv/megrez_console.py bridge /dev/ttyUSB0 --log /tmp/board2.log --fifo /tmp/board2.cmd

# 冷启动 + 五项验收，约 5 分钟
tools/riscv/megrez_smoke.sh --reboot
```

五项：时钟偏差 ≤10 min、`https://www.baidu.com/` = 200、导航器报 OK、截图像素判读
（≥200 色且亮度不在两端）、`g++` 编译运行。

## 5. 已知未解

- eMMC 仍会偶发不响应（恢复后继续跑，但会丢一次 I/O）；
- 崩溃后的坏目录项需要离线 fsck 才能彻底清；
- `Journal` 不可用（`systemctl status` 看不到日志），排障依赖控制台。
