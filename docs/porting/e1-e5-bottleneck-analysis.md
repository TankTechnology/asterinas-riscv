# E1–E5 运行瓶颈与重构边界

本轮只做宿主侧重构和证据整理，没有启动 QEMU、Firefox 或开发板。这里的
“瓶颈”按已经落盘的收据判断，不把超时本身当作根因。

## 当前事实

| 层 | 当前状态 | 直接证据 | 结论 |
|---|---|---|---|
| E1 设备契约 | 通过 | `docs/porting/receipts/2026-10-18-e1-device.json`、`2026-10-18-e1-render-ioctl.json` | render node 与权限探针不是当前首要瓶颈 |
| E2 GL/scanout | 通过 | `docs/porting/receipts/2026-10-19-e2-qemu-desktop-r10-renderer.json`、`2026-10-19-e2-qemu-desktop-r10-pixel.json` | QEMU 显示链已有独立通过证据 |
| E3 Firefox | 未通过 | `docs/porting/receipts/2026-10-19-e3-firefox-r18-newsession-timeout.json` | Firefox 已到 Marionette，但 `NewSession` 后没有响应；继续延长整机超时不会增加定位信息 |
| 桌面服务交接 | 未通过 | `docs/porting/receipts/2026-10-19-desktop-service-tty-boundary-r66.json`、`2026-10-19-desktop-ordered-unit-replay-r68.json` | ExecStartPre 已完成，`ExecStart` marker 未出现；PAM/TTY 不是唯一解释，下一层是 `User=asterinas` 与进程 exec/调度交接 |
| E4 恢复 | 通过 | `docs/porting/receipts/2026-10-18-e4-recovery.json` | 恢复链可以独立验收，不应与 Firefox 场景耦合 |
| E5 真机管理链 | 未通过 | `docs/porting/receipts/2026-10-18-e5-uid0-blocked.json` | 缺少已认证 UID 0 nonce 和完整回退收据；本轮不触碰真机 |

## 为什么以前很慢

1. 旧入口把 rootfs、DTB、桌面服务、GL、Firefox 和恢复合并成一条长门禁，
   任何一个阶段阻塞都会占用整段 TCG 时间。
2. 旧 Makefile 暴露 M5 网络压力、M5 浏览器、M6/M7/M9 应用门禁和两个
   网络模式参数；这些路径与 E1–E5 没有共享的通过判据，却会诱导重复启动。
3. rootfs builder 同时维护多代桌面/浏览器 profile。若调用者没有明确使用
   `--reuse-existing`，就会重复做解包、安装和归一化；这属于构建入口问题，
   不是内核显示性能问题。
4. Firefox 的有效阻塞点已经收敛到用户态 `NewSession`/futex/ppoll 交接；
   继续跑完整 900 秒场景只会重复得到同一边界。

## 本轮已完成的收敛

- 删除 41 个零活动引用的历史门禁、诊断测试和旧 overlay；保留证据、收据
  和 E1–E5 核心实现。删除清单与状态见
  `docs/porting/refactor-deletions.json`。
- Makefile 只保留 E1–E5 rootfs 单元集合，移除 M5 网络/浏览器、M6、M7、
  M9 运行入口及其参数。
- `make test_riscv_debian_rootfs_unit`：325 tests，全部通过；这是宿主单元
  验证，不等价于 QEMU 或真机通过。

## 下一步边界

下一轮只处理 rootfs builder 的 profile 分支：保留 `minimal-m1`、
`systemd-m2`、`desktop-m3`、`desktop-m4`、`desktop-m4-firefox`，把 M5–M9
配置从默认构建路径移出，并让 `--reuse-existing` 成为官方 Firefox 场景的
默认复用入口。完成后只需运行 builder 静态/单元测试；在这些入口稳定前不再
启动长时间 Firefox 门禁，也不进行真机实验。
