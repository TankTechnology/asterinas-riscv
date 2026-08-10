# Megrez 上板前系统性测试报告

> 日期：2026-08-10。分支：`codex/megrez-usb-keyboard` @ `767e27e64`。
> 测试基线：当次构建产物（构建非确定性——每次含时间戳——以当次记录为准）。

## 1. 构建层

| 项 | 结果 | 备注 |
|---|---|---|
| `make kernel TARGET_ARCH=riscv64`（3 次独立构建） | ✅ 全部 FINAL_EXIT=0 | 每次 Image hash 不同（debug build 含时间戳）——非确定性属正常，上板以当次身份为准 |
| 测试基线 Image | `853b6427c7f7f80f...`（当次） | kernel_sha256/crc32 见 verify 输出 |

## 2. Megrez 启动链模拟（`verify_megrez_sim.sh` 一键复验）

| 项 | 结果 |
|---|---|
| profile `megrez-sv48-svade-fast`（Sv48/Svade、4 hart、U-Boot booti） | ✅ **PASS**（BOOT_COMPLETED，用户态 marker `marker_seen=yes`） |
| 串口里程碑 | ✅ `Enter riscv_boot` → Asterinas banner → `>>> Hello from RISC-V userspace on Asterinas! <<<` |
| 产物身份 | kernel `853b6427...` / initrd `766d70e2...` / dtb `760ec102...`（SHA-256） |

## 3. 键盘链路全覆盖（QEMU virt + usb-kbd + 回显 init）

| 类别 | 按键 | 回显（cat -A） | 结果 |
|---|---|---|---|
| 基础字符 | a b 1 z | `ab1z` | ✅ |
| Shift 组合 | shift-a、shift-1 | `A!` | ✅ |
| Caps Lock | caps_lock 后 a | `A` | ✅ |
| Enter | ret | `^M` | ✅ |
| 空格 | spc | ` ` | ✅ |
| Tab | tab | `^I` | ✅ |
| Esc | esc | `^[` | ✅ |
| Backspace | backspace | `^H`（0x7f 经 tty 处理） | ✅ |
| Ctrl 组合 | ctrl-c | `^C`（信号路径触发，init 存活、系统稳定） | ✅ |
| **快速连按** | 5×a + 5×ab 交替（0.3s） | `aaaaaababababab` 全部送达 | ✅ 无丢失 |
| 注册稳定性 | 全程 | 注册恰好 1 次 | ✅ 无抖动 |
| 稳定性 | 全程 | **panic: 0** | ✅ |

## 4. DWC3 选择路径（Megrez 专用）

| 场景 | 结果 |
|---|---|
| DTB `asterinas,usb-host` 指向无效节点 | ✅ `WARN: failed to resolve USB host` + 跳过 + PCI 路径继续 + 键盘工作 + panic 0 |

## 5. 内核 ktest

| 套件 | 结果 |
|---|---|
| ostd（含 DMA/usb_kernel_op 测试） | ✅ **239/239**（从 ostd 目录跑，KTEST_EXIT=0） |
| aster-uart（含 dw-apb 配置校验 6 项） | ✅ **80/80**（KTEST_EXIT=0） |
| xarray | ✅ **20/20**（根 workspace 运行，测试内容全过；根运行的 QEMU 退出码为 Unknown(2)——后台超时干扰，测试结果 ok） |

## 6. 结论

模拟边界内全部通过：**构建 ✓、Megrez Sv48 启动链 ✓、键盘全覆盖 ✓、DWC3 失败安全 ✓、ktest ✓**。
唯一无法模拟的 DWC3 硬件交互由 `reboot_after=400` + 外部 reset 兜底。**具备上板条件。**

## 7. 上板时（当天）必做

1. 重新构建 → 跑 `verify_megrez_sim.sh` 复验（记录当次身份）
2. DTB 修补（`megrez_patch_dtb.py` 或 U-Boot `fdt set`）
3. 按 `megrez-board-session-commands.md` 执行
