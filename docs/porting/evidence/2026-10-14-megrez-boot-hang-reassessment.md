# Megrez 真机启动卡死复盘（2026-10-14）

本次复盘只读取历史收据和当前 RockOS 状态，没有发送串口命令、没有启动
Asterinas、没有访问板端 MMIO、没有重启或修改启动项。

## 当前状态

开发板当前不是死机，而是 RockOS：

- `10.100.19.200` 可 ping，TCP/22 可连接；现有 SSH 密钥可以登录
  `debian`，UID 为 1000。
- 当前 RockOS boot-id 为
  `160963dd-5642-4904-9af0-9e5137fd10ce`，内核为 `6.6.87`，运行约 20 小时。
- 稳定 FTDI 路径存在且没有主机进程占用；空闲串口被动监听 8 秒只收到
  `NUL + LF`。RockOS 空闲时没有串口主动输出，这不能作为死机证据。
- 厂商 `es_drm` 已注册 `card0` 和 `fb0`，HDMI 为 connected，当前模式为
  `2560x1600`，fb0 为 32bpp、stride `10240`。
- `/dev/dri/renderD128` 不存在，因此当前 RockOS 只有扫描输出，没有可供
  Mesa/Firefox 使用的 render node。
- systemd 的 degraded 原因是 swap 分区失败，与显示卡死无关。

## 历史故障的分类

### 已确认的真实 Asterinas 失联

2026-10-03 的 Basic 周期到达四核启动、framebuffer、MMC、GMAC、两个 DWC3、
USB HID、Basic shell 和 `ASTERINAS_SOFTWARE_REBOOT_ARMED`，最后一行是 GMAC
接收进度；随后 UART、网络和软件恢复都没有返回。该记录见
[Basic hard-hang incident](2026-10-03-megrez-basic-console-hard-hang.md)。

它证明了控制路径丢失，但没有证明是哪个硬件模块锁死。最强假设是 GMAC/USB
初始化后的中断路径、软件 timer/SBI reset 路径或两者的组合。因为当时的
“Basic”同时启动了多个外设，所以日志不能归因到单一设备；软件
`reboot_after` 也不能覆盖关中断、内核死锁或 SBI 复位失败。

### 已定位并修正的 GMAC-1 卡点

2026-10-05/06 的 GMAC-1 只读 Probe 在第一条 `0x0000` MMIO 读取处没有返回，
watchdog 随后恢复。读前 marker 已把它精确分类为 `mmio-read:0x0000`，没有
PHY、MDIO、DMA、IRQ、TX/RX 或网络副作用。

RockOS 的 vendor 驱动只有在 HSP、RGMII、时钟和 reset 完成后才能读取同一控制器；
QEMU 模型也复现了 clock-gated 读失败。2026-10-07 的修正版先执行选定端口的
platform 初始化，再读取六个固定寄存器，单次真机周期通过并恢复 RockOS。这个
问题已从“未初始化平台的危险只读 Probe”收敛为“必须先完成平台 gate”，不应再
直接重试旧路径。

### 多次被误判为卡死的控制链问题

历史记录还包含以下宿主或状态判断错误：

- 使用错误管理地址 `192.168.137.100`，而板端实际地址是 `10.100.19.200`；
- 把 RockOS/U-Boot 的空闲串口静默当作内核死机；
- 临时监听器在软件重启后退出，把板子留在无人管理的 U-Boot 提示符；
- 重启后才发现输出目录、导入或参数错误；
- 串口逐字节发送和日志洪泛耗尽 watchdog 时间预算；
- 把命令回显、时间戳或旧 `login:` 文本误当成新状态；
- 异常后连续重试，或只重开串口却没有新的 nonce/UID/boot-id 收据。

这些问题会造成“SSH 不通、串口没输出、显示器不动”的表象，却不能证明板端
已经挂死。

## 防复发规则

下一次真机周期必须遵守以下顺序：

1. 先用正确地址和现有 SSH 密钥确认 RockOS、boot-id、UID 和 uptime；串口只允许
   一个 owner，被动读取不产生 U-Boot 命令。
2. 由同一个串口 owner 持有从重启、OpenSBI、U-Boot、启动、watchdog 到恢复的
   全部生命周期；禁止临时监听器先重启、另一个进程再猜测接管。
3. 启动前完成 artifact、输出目录、脚本导入、DTB 和恢复 latch 校验；禁止用空
   回车获取 U-Boot 提示符，避免重放旧的 `booti` 或 `load`。
4. Asterinas 首轮只启用 UART、timer、MMC、Basic 和硬件 watchdog；GMAC、DWC3、
   桌面、Firefox 和网络全部关闭。缺少 completion marker 时停止，等待新的
   OpenSBI/U-Boot 和 RockOS 收据，不自动重试。
5. 每次只增加一个外设。GMAC-1 必须保留平台初始化和首条 MMIO 的读前 marker；
   首个读取超时立即结束该周期。
6. 只有同时得到新 firmware epoch、重开串口认证、UID 0、boot-id 和 RockOS
   回退收据，才把“恢复成功”与“测试失败”分别记录；否则状态为 unknown，不能
   推断板端根因。

2026-10-11 的 fbdev 桌面 watchdog 周期已经证明这条管理链可以保护窄范围的
桌面启动、fb0 小区域读写和软件重启；它不等价于 DRM/GPU 加速已在真机通过。

