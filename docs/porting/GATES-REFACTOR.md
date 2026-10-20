# Asterinas 桌面加速：门禁重构设计（GATES-REFACTOR）

> **状态**：用户已批准（2026-10-18）
> **适用**：`docs/porting/` 下所有加速门禁；**替代**此前 r1–r51 那套复合长门禁
> **一句话**：把"一个大脚本跑 40 分钟验 6 个断言"，改成"4+1 个单断言门禁，每个 5–20 分钟，各自产出可校验收据"。

## 0. 为什么要重构（有据可查）

- 同一门禁迭代到 **r51**，仍在"宿主 QEMU 冷启动 / 容器重建 / 僵尸进程 / 磁盘 I/O 等待"上打转；
- **18 小时没有一份 evidence 落盘**（最新仍为 2026-10-17 23:16 的 r14）——根因：门禁常在**Python 清理阶段**结束，写收据的代码路径未执行；
- 失败边界被"等待与清理"掩盖：**宿主 QEMU + DTB/Stage1 + rootfs + DRM 桌面 + Firefox 混在一次长运行里**（对该会话的自我复盘原文）。

## 1. 目标命题（唯一需要证明的东西）

> **在 Asterinas(RISC-V) 上，真实用户态应用（Firefox）的绘制确实经过 `/dev/dri` → virgl → 宿主 GPU，而非软件渲染。**

## 2. 最小证据集（4 条 QEMU + 1 条真机）

| Claim | 单断言判据 | 最小输入 | 预计 | 允许失败 |
|---|---|---|---|---|
| **E1 设备契约** | guest 存在 `/dev/dri/card0` 与 `renderD128`；render ioctl 可用 | 1 次启动 + 1 条 probe | 5 min | 是（probe 不计收据） |
| **E2 GL 真走 GPU** | EGL/GLES 探针 renderer **不含 `llvmpipe`**；渲染输出**非零像素**；**Xorg renderer = virgl** | 1 次 Xorg 启动 + glxinfo/eglinfo + 1 张截图 | 10 min | 否 |
| **E3 应用真的用上** | **官方 Firefox** 启动 + Marionette 应答；固定 HTML 场景渲染出**稳定像素哈希**；且与 E2 同路径 | 固定 rootfs + 固定 HTML | 20 min | 否 |
| **E4 可恢复** | 1 次受控重启 / 1 次代表性故障注入后回到已知良好状态 | 同一 guest | 10 min | 否 |
| **E5 真机（独立线）** | 板子一次性启动到**管理链可证状态**（boot-id/串口所有权/fb0）+ 可回退 | 板子 + 串口 | 30 min | 是（首次探测） |

**合计 ≈ 1 小时**（对比：当前 51 轮、单轮 20–40 分钟且无收据）。

## 3. 口径决策（用户已拍板）

1. **E3 的 renderer 口径**：`about:support` 显示 `Generic Renderer` **可接受**——以「renderer 不含 `llvmpipe`」+「实际使用 `renderD128`」+「固定场景像素哈希稳定」三项为准。**不允许**为此再打 Firefox 补丁。
2. **E5 允许先动**：确认管理链**本身就是 E5 的第一步**，与 QEMU 完全解耦；不得以"管理链未确认"为由推迟 E5 的只读确认。
3. **E2/E3 不得用"看起来对"代替**：必须有可复核的字符串/哈希/文件路径。

## 4. 裁撤清单（立即停止）

- ❌ **复合长门禁**：Host QEMU + DTB/Stage1 + rootfs + DRM 桌面 + Firefox 不得混在单次运行
- ❌ **5 秒冷启动预检作为门禁**：降级为 probe；门禁只用「首次 30s 预热（写 host-warm receipt）+ 之后 5s 硬限」，超时分类为 `cold-preheat-timeout` / `hot-runtime-timeout`
- ❌ **6 断言一票否决**：改为单断言
- ⚠️ **fault injection 全覆盖**（SETCRTC/transfer/flush/context/submit/fence）：只保留 **1 个**代表性注入
- ⚠️ **多场景截图 / 滚动画布 / 视频帧 / 帧率**：保留 **1 个固定场景 + 1 张固定哈希**；帧率降级 optional
- ❌ **cross-build / Firefox 源码构建**的一切门禁（方案 A 已冻结）
- ❌ **容器重建作为解法**：环境应固化为 Dockerfile + 镜像 + 启动脚本

## 5. 目录结构

```
docs/porting/
  manifest.lock            # 冻结输入哈希（qemu/kernel/rootfs/dtb/stage1/scene.html）
  claims.md                # E1..E5 一句话判据 + 阈值（人可读，唯一真相）
  GATES-REFACTOR.md        # 本文
  gates/
    e1-device.sh           # 单断言
    e2-gl-renderer.sh
    e3-firefox-scene.sh
    e4-recovery.sh
    e5-board-manage.sh
  receipts/
    2026-10-18-e1.json
  verify.py                # 只读：receipts + manifest → STATUS.md
  STATUS.md                # 机器生成：E1✅ E2✅ E3❌(卡在X) E4⏳ E5⏳
```

## 6. 收据 schema（原子写）

```json
{
  "schema": 1,
  "claim": "E2",
  "gate": "e2-gl-renderer",
  "status": "pass|fail|timeout|aborted|running",
  "started_utc": "…", "finished_utc": "…",
  "manifest_sha256": "…",
  "input_hashes": {"qemu":"…","kernel":"…","rootfs":"…","dtb":"…","scene_html":"…"},
  "assertions": [{"name":"renderer_not_llvmpipe","expected":"…","actual":"…","ok":true}],
  "markers": ["…"],
  "artifacts": {"log":"…","screenshot":"…","sha256":"…"},
  "reason": "…",
  "recovery": "pass|fail|not-run"
}
```

**写入规则**：`started` 时即写 `status:"running"`；阶段转换、通过、超时、信号、子进程退出**均需更新**；写临时文件后 `fsync + rename`。

## 7. 四条铁律

1. **一收据一断言**；组合结论由 `verify.py` 从多张收据拼接，不在长脚本里一次断言完。
2. **收据先写后跑、每阶段更新** → 再不允许"死在清理阶段所以没写收据"。
3. **输入冻结 + 哈希绑定**：哈希不匹配的收据**无效**。
4. **失败即冻结（两次规则）**：同一断言**连续两次在同一日志边界失败** → 写 `blocked` + 最小反例，**禁止第三次重跑**。

## 8. 执行顺序

- **Step 1（今天，不跑 QEMU，1 轮）**：把 Task 1–5 既有收据登记进 `manifest.lock` + `claims.md`，写 `verify.py`，生成 `STATUS.md`。
  → 先回答：**我们手上到底已经证明了什么**（预计 E1/E2 已 ✅）。
- **Step 2（≤30 分钟）**：只补 **E3**，分三次跑：Firefox spawn → Marionette 应答 → 固定场景像素哈希；任一次失败即冻结并交反例。
- **Step 3（并行）**：**E5 真机管理链**（恢复 RockOS / 确认串口所有权 / 记录 boot-id / 准备回退）。
- **Step 4**：**E4** 恢复实验。

## 9. 验收

- `verify.py` 退出码 0 且 `STATUS.md` 由机器生成（不得手写）；
- 每条 claim 的收据含 `manifest_sha256` 与 `artifacts`；
- 任何 `status:"running"` 超过 2 倍预计时长 → 视为 `timeout`，需写收据后停止。

## 10. 禁止事项（违反即视为偏离）

1. 再为 Firefox 打**任何补丁**（`Generic Renderer` 已接受）；
2. 新增 cross-build / builder 环境工程；
3. 同一输入重复重跑超过 2 次；
4. 把宿主环境问题混进 guest 断言的收据；
5. 用"再试一次"代替"定位失败边界"；
6. 无收据的口头结论。
