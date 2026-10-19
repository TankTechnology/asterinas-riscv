# E1–E5 门禁与启动路径精简设计

## 目标

把 RISC-V 桌面验证从 M3–M9、网络压力、浏览器日常使用和多轮诊断脚本收敛为 E1–E5 五条可复核路径；保留历史收据和汇总文档，删除不会再被主路径调用的门禁实现、参数脚本和专属测试，并把启动瓶颈改成短时边界探针。

## 当前问题

- `Makefile` 同时暴露 M3、M4、M5、M6、M7、M8、M9、物理图形、浏览器日常使用和多种 Firefox 诊断入口。
- rootfs 目录同时包含多个互相包裹的 QEMU gate、session、evidence 和 recovery 脚本；同一个启动阶段经常被不同 wrapper 重复执行。
- Firefox 官方包路线已经确定，但源码构建、JIT overlay、启动缓存和 Marionette 诊断工具仍被保留在主工具集合中。
- 整机启动是当前最慢的诊断手段。已知的实际边界是 systemd `ExecStartPre` 完成后没有进入 `ExecStart`，以及 Firefox 用户态在 Marionette `NewSession`/等待路径停滞；这两类问题都不需要再次启动完整桌面。

## 设计

### 1. 唯一的运行层

`docs/porting/GATES-REFACTOR.md`、`manifest.lock`、`claims.md`、`verify.py` 和 `STATUS.md` 是唯一状态入口。活动门禁只保留：

| Claim | 运行入口 | 唯一断言 |
| --- | --- | --- |
| E1 | `e1-device` | render node 存在且 render ioctl 合约通过 |
| E2 | `e2-gl-renderer` | renderer 不含软件渲染器、Xorg 选择 virgl、固定像素哈希稳定 |
| E3 | `e3-firefox-scene` | Debian 官方 Firefox 完成固定场景协议并产生稳定哈希 |
| E4 | `e4-recovery` | 代表性故障或软件重启后回到已知状态 |
| E5 | `e5-board-manage` | 真机管理链具有 fresh nonce、UID 0、boot-id 和回退证据 |

每个入口使用同一个收据写入器：启动时原子写入 `running`，每个阶段用 `fsync + rename` 更新，超时或信号直接落终态。历史 receipt/evidence 不删除、不重新解释。

### 2. 启动与 rootfs 复用

保留一个固定的基础 rootfs 及 manifest。实验只创建小型 overlay；overlay 完成后用哈希绑定基础镜像和输入 manifest。禁止每次实验重新解包 Debian 基础系统。QEMU 参数由一个 Python 配置对象生成，调用方只传入 claim、输入哈希和超时预算，不再在 shell 和 Python 之间复制 bootargs。

### 3. 短时瓶颈探针

新增一个 `systemd_exec_boundary` 探针，复用相同 rootfs 和 Stage1，但只启动一个 service，输出以下阶段时间戳：

```text
probe=systemd-exec phase=unit-start
probe=systemd-exec phase=exec-start-pre-done
probe=systemd-exec phase=exec-start-enter
probe=systemd-exec phase=child-ready
probe=systemd-exec phase=exit
```

探针分别测试 root、`User=asterinas`、补充组和脚本 exec；每次最多 10 秒。它取代 r64–r71 这类整机桌面重复启动，不能直接充当 E2/E3 收据。

Firefox 诊断只保留一个用户态短探针：检查进程、Marionette greeting、`NewSession` 请求前后的线程/等待状态和有界响应。它不再启动 Xorg、截图或完整 Firefox 场景。

### 4. 删除范围

删除前由引用扫描确认；以下历史实现和对应专属测试从主树移除：

- M5–M9 网络、百度页、软件清单和浏览器质量门禁；
- Firefox 源码构建、builder preflight、JIT/profile/启动缓存和重复 Marionette 诊断入口；
- 仅包装另一个 gate 且没有 E1–E5 调用者的 M4 recovery、scanout、x11、xmessage 和 canary wrapper；
- 仅被上述删除项引用的 overlay、service、观测脚本和测试；
- Makefile 中对应目标、测试聚合项和重复参数。

保留：E1–E5 活动入口、基础 rootfs/stage1 构建、统一 gate runtime/protocol、显示 provider/device access、内核/DRM 实现、所有 `docs/porting/evidence/**`、`docs/porting/receipts/**`、`claims.md`、`STATUS.md` 以及能证明历史结论的汇总文档。

### 5. 验证策略

删除后只运行三类检查：

1. `python3 docs/porting/verify.py`，确认历史收据仍可解析；
2. E1/E2/E4 的静态分类器和收据写入单测；
3. 10 秒 `systemd_exec_boundary` 探针和一次 E2 QEMU 运行。

E3 和 E5 仍分别受 Firefox 用户态卡点和真机管理链约束，不以重跑长门禁掩盖失败。

## 迁移顺序

1. 生成引用清单和待删除 manifest；
2. 提取统一收据写入器与 QEMU 参数对象；
3. 加入 systemd exec 边界探针；
4. 删除旧入口、overlay、service 和专属测试，并更新 Makefile/README；
5. 运行短验证，生成重构收据和新的瓶颈报告；
6. 只有 E1–E5 入口稳定后，才恢复 E3 或真机 E5 的运行。

## 成功标准

- 主 Makefile 只暴露 E1–E5 以及基础构建目标；
- rootfs 工具目录中不存在无调用者的 M5–M9/Firefox 源码构建入口；
- 历史 evidence/receipts 可被 `verify.py` 读取；
- systemd `ExecStartPre → ExecStart` 的边界可在 10 秒内单独判定；
- 一次 E2 运行不再携带网络、Firefox 或真机实验；
- 所有删除项均能在 manifest 中追溯到删除理由和替代入口。

## 不在本次范围

本次不修改内核调度、futex、DRM 协议、PowerVR 驱动、GMAC/DWC3，也不进行真机启动。只有在短探针给出明确内核边界后，才单独安排内核修复。
