# 门禁声明（GATES-REFACTOR）

本文件是当前状态的人读真相；机器状态由同目录的 `verify.py` 根据
`manifest.lock` 和 `receipts/*.json` 生成。每条声明只对应一个可证伪的
判据集合，历史证据不会被当成新的运行。

| Claim | 一句话判据与阈值 | 当前状态 | 支撑收据 |
|---|---|---|---|
| **E1 设备契约** | 同一次受控 QEMU 输入中必须同时出现 `/dev/dri/card0`、`/dev/dri/renderD128`，且 render ioctl 权限探针通过。 | ✅ | `receipts/2026-10-18-e1-device.json`; `receipts/2026-10-18-e1-render-ioctl.json`（源：`2026-10-15-qemu-drm-render-node-current.md`） |
| **E2 GL 真走 GPU** | Xorg 必须报告 `renderer=virgl`，EGL/GL renderer 不含 `llvmpipe`，固定截图必须是非零且有稳定 SHA-256。 | ✅ | `receipts/2026-10-19-e2-qemu-desktop-r10-renderer.json`; `receipts/2026-10-19-e2-qemu-desktop-r10-pixel.json`（源：`evidence/2026-10-19-qemu-desktop-m4-r10.md`；旧 r5 收据仍保留） |
| **E3 应用真的用上** | 官方 Debian Firefox 必须完成 Marionette、固定场景，并在同一路径产生不含 `llvmpipe` 的 renderer、`renderD128` 使用证据和稳定场景像素哈希。 | ❌（未达到；当前阻塞在冷启动/Firefox 边界） | `receipts/2026-10-20-e3-firefox-newsession-capability-ab-r101.json`（A/B capability 均 greeting、virgl、`renderD128` 通过，但 360 秒内均无 NewSession 响应）；`receipts/2026-10-20-e3-firefox-ppoll-runtime-r99.json`（父进程 `ppoll`、worker `futex` 边界）；`receipts/2026-10-20-e3-firefox-profile-tmpfs-runtime-r116.json`（`/run` profile 仍无 NewSession 响应，profile 假设关闭）；`receipts/2026-10-20-e3-firefox-tcp-trace-coverage-r117.json`（旧 trace 覆盖缺口已分类）；`receipts/2026-10-20-e3-firefox-user-recv-trace-r118.json`（用户态 recv 取证已编译）；`receipts/2026-10-20-e3-firefox-user-recv-runtime-r119.json`（66 字节 NewSession 请求已到达 2828 端口，但无响应帧）；`receipts/2026-10-20-e3-firefox-thread-topology-r121.json`（post-NewSession 的 Firefox worker `futex`/`ppoll` 等待已分类，未推断内核修复）；`receipts/2026-10-20-e3-firefox-newsession-history-reconciliation-r122.json`（r34 历史确有 745 字节 NewSession 响应，但整体 WebGL 失败，capability 因果未记录）；`receipts/2026-10-20-e3-firefox-newsession-capability-ab-r123.json`（A/B 均未进入 capability 边界，启动预算不足）；`receipts/2026-10-20-e3-firefox-short-probe-budget-r124.json`（短探针预算会传入 guest，且请求体会先记录）；`receipts/2026-10-20-e3-firefox-probe-launch-prune-r125.json`（短探针禁用普通 NetSurf 启动） |
| **E4 可恢复** | 同一 QEMU 代际在桌面就绪后软件重启，必须观察到新的 OpenSBI/U-Boot epoch 并回到已知提示符。 | ✅ | `receipts/2026-10-18-e4-recovery.json`（源：`/mnt/mydisk/firefox-official-debian-20261018/qemu-drm-desktop-m4-r5-evidence.md`） |
| **E5 真机管理链** | 只读确认必须同时具备 RockOS fresh-nonce/UID 0/boot-id、串口单一所有者，以及可回退收据；UID 0 认证边界已连续两次失败，当前冻结等待已认证 RockOS root 路径。 | ❌（blocked） | `receipts/2026-10-18-e5-uid0-blocked.json`; 仍缺串口 nonce 和回退收据 |

E1/E2/E4 的来源是独立 QEMU 收据；它们不证明 E3 或板载 PowerVR。E5
本轮没有写串口、重启或启动 Asterinas，未满足项必须保持 ⏳。
