# M5/browser-web legacy audit r95

Date: 2026-10-20

本轮只清理仍位于 M5/browser-web 历史路径、且没有 E1--E5 管理链或当前
rootfs 构建入口的工具。扫描规则把源码、Makefile、profile、overlay、
动态导入和当前 E3 探针都视为活动依赖；仅被历史报告引用的文件不算活动依赖。

## 删除清单与决定

| 路径 | 活动引用 | 决定 | 理由 |
| --- | --- | --- | --- |
| `tools/riscv/xorg/render_matrix.sh` | 仅 `BROWSER-M5/M6/M7` 历史报告 | 删除 | 旧 NetSurf 多站点矩阵；没有 Makefile、profile、overlay、E1--E5 或管理链调用。 |
| `tools/riscv/tests/test_debian_m5_network.py` | 无 E1--E5 调用（已处于删除状态） | 保持删除 | 旧 M5 网络/浏览器复合测试；与当前 desktop-m4 合约不符，r93 已确认六项断言失败。 |
| `tools/riscv/debian/rootfs/browser_m5_marionette_gate.py` | 当前 E3 `firefox_drm_scene_gate.py`、probe overlay、Stage1 | 保留 | 是官方 Firefox Marionette 传输实现，删除会使 E3 运行时失去入口。 |
| `tools/riscv/debian/rootfs/browser_web_*.{py,sh,service,json,conf}` | `build_rootfs.sh`/`build_stage1.sh` 动态 profile 分支及测试 | 保留 | 仍是 `browser-web` profile 的构建输入；只删文件会留下悬空安装和 manifest 引用。 |
| `tools/riscv/debian/rootfs/browser_m5_{firefox,network_observer,window_observer}.sh`、`browser_m5_probe.html`、`browser_m5.webm.base64` | `build_rootfs.sh` 的 `browser-m5` 分支 | 保留 | 仍由公开 profile CLI 选择并安装；需另一个迁移轮次后才能安全移除。 |
| `tools/riscv/debian/rootfs/profiles.py` 中 `browser-m5`/`browser-web` 与 builder 对应分支 | rootfs builder、Stage1、旧测试 | 暂缓 | 这是 profile API 级删除，必须同时迁移 builder、manifest 和测试，不能在文件级清理中半删。 |
| `tools/riscv/xorg/BROWSER-M5*.md`、`BROWSER-M6*.md`、`BROWSER-M7*.md` | 文档/历史收据 | 保留 | 用户要求保留汇总文档和追溯材料。 |

本轮实际删除只有 `render_matrix.sh`；不触碰当前 dirty 修改，不修改 rootfs
构建行为。`test_debian_m5_network.py` 的删除来自 r93，本轮只验证其状态。

## 验证边界

* `tools/riscv/firefox_fast_check.sh` 不包含已退役 M5/browser-web 复合套件，
  但 E3 仍使用 `browser_m5_marionette_gate.py`，因此该文件不能列为孤儿。
* `refactor_reference_scan.py` 当前扫描 210 个 rootfs/test 候选；活动引用
  为零但仍被动态 profile 选择的 overlay 不作为可删除项。
* 下一次若要删除整个 `browser-web` profile，必须先提交单独的 profile/API
  迁移清单；本轮不把它误判为“文件未被字面引用”。

