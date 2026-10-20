# 开发板 KVM 可行性探测（EIC7700 / Milk-V Megrez / RockOS）

**探测日期**：2026-10-19（宿主时钟；板子时钟严重偏移，见 §3.1）
**执行者**：board-kvm-prober（只读探测；未重启、未写串口、未改任何 boot 配置）
**探测窗口**：2026-10-19 10:17–10:23 CST（pku4090 时钟）
**交付范围**：本文件是本次唯一写入仓库的文件；未触碰 Asterinas 主线工作、未触碰板子 `mmcblk1p2`。

## 0. 一句话摘要

**没有发现"更底层"的缺失**：设备树/内核都已声明并识别 H 扩展（`rv64imafdch…`，dmesg `base ISA extensions acdfhim`），
厂商内核配置为 `CONFIG_KVM=m`，且 `/lib/modules/6.6.87-win2030` 里就有与资产配对的 `kvm.ko`。
**唯一的断点**是：板子当前运行的是本地重编的 `6.6.87`（不带 `-win2030` localversion）内核，而板上没有任何
`/lib/modules/6.6.87` 模块树；`6.6.87-win2030` 的模块无法装进 `6.6.87`（vermagic 不匹配），所以 `/dev/kvm` 不存在。
因此 **KVM 可行性等价于一次"启动到厂商 `6.6.87-win2030` 内核并 `modprobe kvm`"的一次性启动**；
该启动的设计、菜单内容与 U-Boot 命令已在离线侧复核通过（对当前板子哈希仍然成立），但**尚未执行**。
执行它需要：**独占串口 + root 凭据 + 批准重启**；若厂商内核在用户态前挂死，板上没有可用的看门狗/远程硬复位，
需要有人在板子旁物理复位——这是必须由 Lead 决策的闸门。

---

## 1. 三条结论（先给答案）

### a. KVM 是否只缺"启动到 win2030 内核 + modprobe kvm"？

**是。现有证据只支持这一个断点，未发现设备树/固件层面的 H 扩展缺失。**

| 层 | 现状（本次只读实测） | 判定 |
| --- | --- | --- |
| 硬件/ISA 声明 | 4 hart 全部 `riscv,isa = rv64imafdch_zicsr_zifencei_zba_zbb_sscofpmf`（含 `h`） | 满足 |
| 设备树 | 同一字符串在 `/proc/device-tree/cpus/cpu@*/riscv,isa`；dmesg 解析为 `riscv: base ISA extensions acdfhim` | 满足 |
| 固件 | OpenSBI：`SBI specification v2.0`、`implementation ID=0x1 Version=0x10005`（OpenSBI v1.5），SBI SRST 存在 | 未见拦截 |
| 内核配置 | 运行内核 `/proc/config.gz` 与 `/boot/config-6.6.87-win2030` 均为 `CONFIG_KVM=m`；`CONFIG_MODULES=y`、`# CONFIG_MODULE_SIG is not set` | 满足 |
| KVM 模块 | `kvm.ko` 存在，`kvm_riscv_*` 符号齐全，含 "hypervisor extension available/not available" 探测字符串，vermagic `6.6.87-win2030`，`depends:` 为空 | 模块自身满足 |
| 运行内核 | `6.6.87`（zrp 本地重编，`#1 SMP Mon Sep 1 21:20:09 CST 2025`），`/lib/modules/6.6.87` **不存在** | **缺口** |
| 模块树 | 只有 `/lib/modules/6.6.87-win2030/`（734 个 .ko），与 `6.6.87` 不匹配；`modprobe -n -v kvm` → `FATAL: Module kvm not found in directory /lib/modules/6.6.87` | **缺口** |
| 配对内核 | `/boot/vmlinuz-6.6.87-win2030-origin` 解压后 banner 为 `6.6.87-win2030 … #2025.04.20.18.43+bb0c69aea`，与 `linux-image-6.6.87-win2030` 包（Apr 2025）及模块树配对 | 可用 |
| `/dev/kvm` | 不存在；`/proc/misc` 无 kvm；`/sys/module/kvm` 不存在；`lsmod` 无 kvm | 预期 |

决定性佐证：**`modprobe -n -v -S 6.6.87-win2030 kvm` 成功解析**为
`insmod /lib/modules/6.6.87-win2030/kernel/arch/riscv/kvm/kvm.ko`（exit 0）；
而当前 QEMU 唯一的报错就是 `Could not access KVM kernel module: No such file or directory`。

**结论 a 的边界（必须写清）**：内核的 H 判定来自设备树字符串，而不是硬件探针。因此"EIC7700 硅片上的 H 扩展
是否真的能跑 KVM host"仍无法在只读侧证明；这只能由那次启动后 `modprobe kvm` 的输出
（`kvm [..]: hypervisor extension available` vs `not available`，以及是否在写 `henvcfg` 等 H CSR 时异常）来裁决。
若届时失败，结论才应升级为"缺更底层支持（硅片/固件）"；在此之前，**不应**预先断言硬件缺陷。

### b. 一次性启动到 win2030 内核的最小步骤、回退与风险

设计已存在于仓库对象库（**未合入 main**）：commit `c70c24306`
（`docs/superpowers/specs/2026-09-15-rockos-kvm-one-shot-design.md`、`tools/riscv/rockos_kvm_once.py`）。
本次用**当前板子的** `/boot/extlinux/extlinux.conf`（sha256 `eb5f39a6…`，与设计里 pin 的 vendor sha 一致）重跑了
离线限定器，输出仍然通过，得到唯一的一次性菜单：

```
/boot/extlinux/asterinas-rockos-kvm-once-1f94698f13b7.conf   (440 bytes, sha256 1f94698f13b7052fefd497605e0600438337e7e88bb86172905195c89428ccb3)
```
内容（`linux` 指向 `-origin`，其余字段逐字复制 vendor `l0`）：
```
default kvm-once
menu title RockOS KVM one-shot
prompt 0
timeout 0

label kvm-once
	menu label RockOS 6.6.87-win2030 KVM one-shot
	linux /vmlinuz-6.6.87-win2030-origin
	initrd /initrd.img-6.6.87-win2030
	fdtdir /dtbs/linux-image-6.6.87-win2030/
	append root=PARTUUID=b0f77ad6-36cd-4a99-a8c0-31d73649aa09 console=ttyS0,115200 root=PARTUUID=b0f77ad6-36cd-4a99-a8c0-31d73649aa09 rootfstype=ext4 rootwait rw earlycon selinux=0 LANG=en_US.UTF-8
```
U-Boot 一次性命令（不 `saveenv`、不改默认选择器）：
```
sysboot mmc 1:1 any 0x88200000 /extlinux/asterinas-rockos-kvm-once-1f94698f13b7.conf || run bootcmd_rockos
```

**最小步骤**（全部尚未执行，逐条都需要批准/凭据）：
1. 只读预检（本次已完成）：资产哈希、vendor 菜单哈希、默认 `l0`、`/boot` 余量、模块 vermagic、串口空闲。
2. 在 pku4090 用单写者串口助手独占 FTDI（`/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0`，115200），
   先观察并确认当前 RockOS 状态，再发送任何字符；U-Boot 提示符下**绝不发空行**。
3. 在 RockOS 侧以 root 写入上面那个**新文件**（不编辑/不重命名/不覆盖 `extlinux.conf`、`asterinas.conf`、`vmlinuz-6.6.87-win2030`）。
4. root 执行一次软件重启；在 U-Boot 打断一次 autoboot，执行上面的 `sysboot … || run bootcmd_rockos`。
5. 等待用户态；验证 `uname -r == 6.6.87-win2030`、SSH/串口恢复。
6. root 执行 `modprobe kvm`；确认 `/dev/kvm` 为字符设备、`dmesg` 出现 KVM/H 扩展信息；
   跑一个**有界**的 headless QEMU `-accel kvm` 冒烟（先裸 KVM API，再 Asterinas 冻结镜像）。
7. 记录结果后回退：软件 `reboot` → U-Boot 默认路径（`bootcmd=run bootcmd_asterinas || run bootcmd_rockos || bootcmd_factory`，
   `/extlinux/asterinas.conf` 的 `l0` 仍是 RockOS）→ 回到 RockOS；核对 boot ID/选择器哈希/串口重开握手；
   经批准后删除一次性 conf 并 `sync`。

**回退收据（baseline，本次实测）**：

| 对象 | 值 |
| --- | --- |
| 当前 RockOS boot ID | `9c777a00-cc49-47c7-829a-c6abac19dbc9`（**已不是** 9/22 证据里的 `6e0c2402-…`；板上期间发生过重启） |
| `/boot/extlinux/extlinux.conf` | sha256 `eb5f39a6e2db71ccc93ae005c488fd9f7ef353e75426e5a51e8923a9cbf2ebc5`（默认 `l0` = RockOS） |
| `/boot/extlinux/asterinas.conf` | sha256 `02280720efe7a1ad0ac084cdc20429406631e12d2e16f05638544bab0883fb26` |
| 当前运行内核文件 `/boot/vmlinuz-6.6.87-win2030` | sha256 `dda6a176a46fa7e791c7f603840c6a0fc128d315cad7e9b30d886c926f965c98` |
| 配对内核 `/boot/vmlinuz-6.6.87-win2030-origin` | sha256 `4af973c27aa87679aad75966dd816665466837cb8adf473e3222c523195d1f3c` |
| `kvm.ko` | sha256 `defc0e79c280b0ebfc705df13fccdc3c927b69d657c3fcb493db085f0397c0df` |
| 板子状态 | 无 QEMU 进程；`mmcblk1p2` 未挂载；`/dev/kvm` 不存在；SSH active/enabled |

回退判据：回退后必须是**新的 boot ID**（不同于 `9c777a00-…`）、选择器两个哈希不变、SSH 返回 UID 1000、
串口关闭并重开同一设备后收到**恰一条**独立 nonce 标记。任何一条不满足都记 pending，不得记 passed。

**风险（按严重度）**：
1. **最严重：用户态前挂死 → 无法远程恢复。** 板上实测无 `/dev/watchdog`、`/sys/class/watchdog/` 为空、`panic=0`；
   pku4090 的 USB 上只有 FTDI UART（`0403:6001`），没有独立电源/复位控制器；9/16 证据也确认"只有 UART"。
   即：厂商内核若在 userspace 前挂住，只能靠**板边有人物理复位**。这正是 9/22 把一次性启动"明确推迟"的原因。
2. 厂商 `-origin` 内核在**可获取证据内从未在这块板上启动过**；不过它是 `linux-image-6.6.87-win2030` 包的原版
   （banner/Apr 20 构建、`System.map`/`config` Apr 21、`dpkg -V` 显示当前 vmlinuz 被改过），配对 initrd/DTB 也在，置信度中上。
3. 需要 root：`debian` 的 `sudo -n` 需要密码；`/boot` 是 root 所有。写 conf、`modprobe`、重启都必须 root/授权。
   板上没有 passwordless sudo —— **root 凭据必须由 Lead/操作者以带外方式提供**，不得写进证据或命令行。
4. `/boot` 已用 97%（`/dev/mmcblk1p1` 459M，剩 15.4M/16M）；440 字节 conf 没问题，但不要往 `/boot` 放别的东西。
5. U-Boot 交互纪律：单写者串口锁、不 `saveenv`、不发空行、保留完整 transcript。
6. 板子时钟比宿主慢约 10.6 天（见 §3.1），所有证据时间一律用宿主时钟，不要用板上 `date`。
7. 成功后 QEMU 9.2.0 与 Linux 6.6 KVM 的 SBI/ABI 兼容性未知；`-accel kvm` 仍可能因 QEMU 侧约束失败。
   这只能记为"该内核+该 QEMU 组合不可用"，不能反推板子 H 扩展有问题。
8. 不碰 `mmcblk1p2`（Asterinas 分区）、不碰任何 `asterinas-*` boot 产物、不下载/安装内核包。

### c. KVM 之后的真实收益，与"虚拟设备 ≠ 裸机"的边界

**机制**：KVM 把 guest 指令直接跑在 P550 上（VS 模式），只有 CSR/SBI/MMIO/页表等特权路径陷入 host；
TCG 则要逐指令翻译/执行。所以 KVM 抹掉的是"指令模拟"成本，**不抹掉**设备模拟成本。

**预期速度（不出数字结论，只给可辩护的量级）**：
- 对 **CPU-bound** 的 Asterinas guest（调度器、futex、syscall 热路径、JS/Gecko 主线程）：预期 **一个数量级左右** 的提升，
  因为 guest 用户态代码接近原生 P550（1.8 GHz × 4 hart，performance governor）。
- 对**端到端 Firefox 启动/整机场景**：提升会明显小于纯 CPU 项。virtio-blk/net/gpu、MMIO exits、虚拟定时器、
  fbdev/VRAM 路径仍由 QEMU 进程模拟，这些阶段不会按 CPU 倍率加速。
- 对**当前 pku4090 x86 TCG**：**不能从现有证据预测大小**。9/22 的同协议一次性数据（板 TCG 102.774 s vs
  x86 TCG 5.530 s）被原文明确标注为"one-shot、非受控基准、主机与 QEMU 版本不同"，
  **不得**当作 18.6× 之类的加速比使用。KVM 的真正价值是"**同 ISA + 接近原生**"，而不是保证绝对快过 x86 TCG；
  最终仍需用同一份冻结 kernel/Stage1 在两种宿主上各跑一遍才能比较。

**KVM 不会改变的边界（必须写进任何后续性能声明）**：
- 仍是 QEMU `virt` 合成机器：virtio-mmio 设备、G-stage 地址翻译、SBI/CSR/定时器陷入模拟；
  没有真实 eMMC/SD、DWMAC 以太网、USB、HDMI、GPU。
- 因此真实 MMC/DMA/cache/中断/显示/视频路径**只能在物理 Asterinas 启动上验证**；KVM 只适合吞吐、
  并发、syscall 行为与整机功能压力，不适合代替物理设备/物理延迟结论。
- KVM 会改变时序与交错：并发 bug 可能更容易复现，也可能"消失"；这是调试变量，不是正确性证明。
- 6.6 的 KVM RISC-V 不支持嵌套 H；guest 能看到的 AIA/中断控制器取决于 QEMU `virt` 配置（默认 PLIC）。

---

## 2. 访问通道（本次验证可用）

### 2.1 SSH（本次实际使用，唯一写操作是 SSH 只读命令）

从 **pku4090**（`ubuntu-MS-7D76`，10.100.19.216）到板子（10.100.19.200:22）用现有密钥即可，**无需密码**：

```bash
# 在 pku4090 上执行
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=5 debian@10.100.19.200
```
```
uid=1000(debian) gid=1000(debian) groups=1000(debian),4(adm),7(lp),24(cdrom),25(floppy),27(sudo),29(audio),30(dip),44(video),46(plugdev),102(bluetooth),103(netdev),995(input)
Linux rockos-eswin 6.6.87 #1 SMP Mon Sep  1 21:20:09 CST 2025 riscv64 GNU/Linux
```
- 用户 `debian`（UID 1000，在 `sudo` 组，但 **`sudo -n` 需要密码**，实测 `sudo: a password is required`）。
- SSH 服务 `active`/`enabled`，监听 `0.0.0.0:22`。
- 板子侧 `SSH_CLIENT=10.100.19.216`（即 pku4090）。**不要从 thinkpad 走**。
- 本次所有只读探测均用以下形式（脚本经 stdin 传入，不在板上落文件）：
```bash
# 在 pku4090 上执行；本地脚本经两层 ssh 的 stdin 传入
ssh -o BatchMode=yes pku4090-direct \
  'ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=5 debian@10.100.19.200 bash -s' \
  < board_kvm_probe.sh
```

### 2.2 串口（存在，本次**未使用、未持有、未写入**）

pku4090 上：
```
lrwxrwxrwx 1 root root 13 10月 17 19:58 usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0 -> ../../ttyUSB0
crw-rw---- 1 root dialout 188, 0 10月 17 19:58 /dev/ttyUSB0
```
- 设备：`/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0`，115200 8N1（沿用 9/22 恢复记录）。
- 本次 `fuser` / `lsof` 均无输出 → **当前无持有者**（空闲）。
- 使用方式（待批准后）：单写者助手 `tools/riscv/megrez_menu_board.attach` 独占；先观察确认当前 RockOS 状态，
  再发命令；U-Boot 下**绝不发空行**（会重复上一条命令）。
- 串口 root 登录需要既有凭据输入流程（密码不得进入证据/命令行）；本次未使用。

### 2.3 板子时钟偏移（会影响一切时间证据）

```
宿主 pku4090 : 2026-10-19T10:20:03+08:00
板子         : 2026-10-08T19:30:51+08:00     （慢约 10.6 天）
```
板上 `uptime` 显示 up 2 days（约 10-06 19:xx 板上时间启动）。**报告与后续证据一律用宿主时钟**；
不要用板上 `date`、文件 mtime 作为绝对时间证据。

---

## 3. 只读清单（命令原文 + 原始输出）

> 完整 raw transcript 见附录 A/B/C。以下按主题列出决定性命令与输出；所有命令均带超时、失败即记录、未重复超过 2 次。

### 3.1 身份与启动

```bash
date -Is; cat /proc/sys/kernel/random/boot_id; uname -a; cat /proc/cmdline; cat /proc/version; hostname
```
```
board date: 2026-10-08T19:30:51+08:00
BOOT_ID: 9c777a00-cc49-47c7-829a-c6abac19dbc9
Linux rockos-eswin 6.6.87 #1 SMP Mon Sep  1 21:20:09 CST 2025 riscv64 GNU/Linux
root=PARTUUID=b0f77ad6-36cd-4a99-a8c0-31d73649aa09 console=ttyS0,115200 root=PARTUUID=... rootfstype=ext4 rootwait rw earlycon selinux=0 LANG=en_US.UTF-8 earlycon=sbi console=ttyS0,115200n8 clk_ignore_unused cma_pernuma=0x2000000 disable_bypass=false
Linux version 6.6.87 (zrp@DESKTOP-ZRP) (riscv64-linux-gnu-gcc (Ubuntu 11.4.0-1ubuntu1~22.04) 11.4.0, GNU ld ... 2.38) #1 SMP Mon Sep  1 21:20:09 CST 2025
```

### 3.2 CPU / H 扩展

```bash
head -30 /proc/cpuinfo; grep -i isa /proc/cpuinfo | head -4; grep -c '^processor' /proc/cpuinfo
for f in /proc/device-tree/cpus/cpu@*/riscv,isa; do printf '%s = ' "$f"; tr '\0' '\n' < "$f"; done
tr '\0' '\n' < /proc/device-tree/compatible
timeout 10 dmesg | grep -iE 'riscv: base ISA|Falling back|hypervisor|kvm'
```
```
isa : rv64imafdch_zicntr_zicsr_zifencei_zihpm_zba_zbb_sscofpmf      # ×4 hart
mmu : sv48 ; uarch : eswin,eic770x ; mvendorid 0x489 ; marchid 0x8000000000000008 ; mimpid 0x6220425
/proc/device-tree/cpus/cpu@0/riscv,isa = rv64imafdch_zicsr_zifencei_zba_zbb_sscofpmf   # 0..3 相同
sifive,hifive-unmatched-a00 / sifive,fu740-c000 / sifive,fu740 / eswin,eic7700
[0.000000] Falling back to deprecated "riscv,isa"
[0.000000] riscv: base ISA extensions acdfhim
```
- `h` 已在内核解析出的 base ISA 里（`acdfhim`）；`zba/zbb/sscofpmf` 等在字符串中。
- cpufreq：4 个 hart 均 `scaling_cur_freq=1800000` kHz，governor `performance`。

### 3.3 内核配置（KVM / 模块）

```bash
zcat /proc/config.gz | grep -iE 'CONFIG_(KVM|RISCV|HYPERVISOR)'
grep -E 'CONFIG_MODULES=|CONFIG_MODULE_SIG|CONFIG_MODULE_UNLOAD|CONFIG_KVM=' /boot/config-6.6.87-win2030
```
```
CONFIG_RISCV=y / CONFIG_RISCV_SBI=y / CONFIG_RISCV_ISA_C=y / CONFIG_RISCV_ISA_V=y / CONFIG_RISCV_ISA_ZICBOM=y ...
CONFIG_KVM_MMIO=y / CONFIG_KVM_GENERIC_DIRTYLOG_READ_PROTECT=y / CONFIG_KVM_XFER_TO_GUEST_WORK=y / CONFIG_KVM_GENERIC_HARDWARE_ENABLING=y
CONFIG_KVM=m
CONFIG_64BIT=y / CONFIG_SMP=y / CONFIG_RISCV_SBI=y
CONFIG_MODULES=y / CONFIG_MODULE_UNLOAD=y / # CONFIG_MODULE_SIG is not set
```
（运行内核 `/proc/config.gz` 与 vendor `/boot/config-6.6.87-win2030` 都包含 `CONFIG_KVM=m`；两者 sha256 不同：
`51ad03de…`（运行时解压后） vs `9a2964f4…`，属不同 build，不影响 KVM 结论。）

### 3.4 模块：精确的失配点

```bash
ls -la /lib/modules/; ls -la /lib/modules/6.6.87/; ls /lib/modules/*/kernel/arch/riscv/kvm/
modprobe -n -v kvm
modprobe -n -v -S 6.6.87-win2030 kvm
modinfo -k 6.6.87-win2030 kvm
ls -l /dev/kvm; cat /proc/misc | grep -i kvm; ls -d /sys/module/kvm; lsmod | grep -i kvm
```
```
/lib/modules/: 只有 6.6.87-win2030 （drwxr-xr-x 3 root root 4096 Apr 23 2025 6.6.87-win2030）
/lib/modules/6.6.87: No such file or directory
/lib/modules/6.6.87-win2030/kernel/arch/riscv/kvm/: kvm.ko  (518432 bytes, Apr 21 2025)
modprobe -n -v kvm                 -> modprobe: FATAL: Module kvm not found in directory /lib/modules/6.6.87   (exit 1)
modprobe -n -v -S 6.6.87-win2030 kvm -> insmod /lib/modules/6.6.87-win2030/kernel/arch/riscv/kvm/kvm.ko   (exit 0)
modinfo -k 6.6.87-win2030 kvm:
  filename: /lib/modules/6.6.87-win2030/kernel/arch/riscv/kvm/kvm.ko
  license: GPL ; author: Qumranet ; depends: ; intree: Y ; name: kvm
  vermagic: 6.6.87-win2030 SMP mod_unload riscv
/dev/kvm: No such file or directory ; /proc/misc: (无 kvm) ; /sys/module/kvm: No such file or directory ; lsmod: (无)
```
KVM 模块内部字符串（证明它是 RISC-V KVM host、带 H 扩展探测）：
```
kvm [%i]: hypervisor extension not available
kvm [%i]: hypervisor extension available
kvm_riscv_vcpu_enter_exit / kvm_riscv_reset_vcpu / kvm_riscv_vcpu_vreg_addr
kvm_riscv_vcpu_update_vstimecmp / kvm_riscv_vcpu_update_hrtimer / kvm_riscv_vcpu_hrtimer_expired
kvm_riscv_aia_available / kvm_riscv_aia_disable / kvm_riscv_aia_max_ids
kvm_riscv_gstage_vmid_update / kvm_riscv_local_hfence_gvma_gpa / kvm_riscv_hfence_vvma_all
```
另：`file kvm.ko` → `ELF 64-bit LSB relocatable, UCB RISC-V, RVC, soft-float ABI, BuildID[sha1]=9598c6b3…`。

### 3.5 启动资产与哈希（与 9/22 证据对齐）

```bash
sha256sum /boot/vmlinuz-6.6.87-win2030 /boot/vmlinuz-6.6.87-win2030-origin \
          /lib/modules/6.6.87-win2030/kernel/arch/riscv/kvm/kvm.ko \
          /boot/config-6.6.87-win2030 /boot/System.map-6.6.87-win2030 \
          /boot/extlinux/extlinux.conf /boot/extlinux/asterinas.conf \
          /boot/dtbs/linux-image-6.6.87-win2030/eswin/eic7700-milkv-megrez.dtb
for f in /boot/vmlinuz-6.6.87-win2030*; do zcat "$f" | strings -a | grep -m1 'Linux version 6.6.87'; done
dpkg -l | grep -iE 'linux-image|linux-headers'; dpkg -V linux-image-6.6.87-win2030
```
```
dda6a176a46fa7e791c7f603840c6a0fc128d315cad7e9b30d886c926f965c98  /boot/vmlinuz-6.6.87-win2030
4af973c27aa87679aad75966dd816665466837cb8adf473e3222c523195d1f3c  /boot/vmlinuz-6.6.87-win2030-origin   # 与 9/22 证据一致
defc0e79c280b0ebfc705df13fccdc3c927b69d657c3fcb493db085f0397c0df  .../kvm/kvm.ko                      # 与 9/22 证据一致
9a2964f4791f445604a6985a40a8441f594367a59449d164d439459f214412a9  /boot/config-6.6.87-win2030
77effdb35a32cf708555f769458f405780693da0d31588541b4d6d205e9d5eb8  /boot/System.map-6.6.87-win2030
eb5f39a6e2db71ccc93ae005c488fd9f7ef353e75426e5a51e8923a9cbf2ebc5  /boot/extlinux/extlinux.conf       # 与 9/22 恢复记录一致
02280720efe7a1ad0ac084cdc20429406631e12d2e16f05638544bab0883fb26  /boot/extlinux/asterinas.conf       # 与 9/22 恢复记录一致
02a8d43d581b4aa8e957e231ee90eba19ffd7e8cfcf74694e86a1fb9c6b37f17  .../eic7700-milkv-megrez.dtb        # 与 9/16 证据的原始 Megrez DTB 一致

/boot/vmlinuz-6.6.87-win2030          -> Linux version 6.6.87 (zrp@DESKTOP-ZRP) ... #1 SMP Mon Sep  1 21:20:09 CST 2025        [当前运行]
/boot/vmlinuz-6.6.87-win2030-origin   -> Linux version 6.6.87-win2030 (builder@rockos-riscv-builder) ... #2025.04.20.18.43+bb0c69aea SMP Sun Apr 20 18:58:14 UTC 2025  [与模块树配对]

ii  linux-headers-6.6.87-win2030  6.6.87-2025.04.20.18.43+bb0c69aea
ii  linux-image-6.6.87-win2030    6.6.87-2025.04.20.18.43+bb0c69aea
dpkg -V linux-image-6.6.87-win2030 -> ??5??????   /boot/vmlinuz-6.6.87-win2030     # 包内 vmlinuz 已被本地内核替换（-origin 即原版）
```
- `/boot/initrd.img-6.6.87-win2030` = 62,583,027 bytes（Apr 23 2025，随包）。
- `/boot/dtbs/linux-image-6.6.87-win2030/eswin/eic7700-milkv-megrez.dtb` 与 `/boot/eic7700-milkv-megrez.dtb`、
  `/boot/dtb/megrez-02a8d43d581b.dtb` 三者哈希相同 → vendor fdtdir 的 Megrez DTB 身份可核对。
- `/usr/src/linux-headers-6.6.87-win2030/` 与 `Module.symvers`（Apr 21 2025）存在 → **只能**用 rebuild `-win2030` 模块；
  对正在跑的 `6.6.87`（zrp build）**没有** headers/source/symvers，无法合法重建匹配模块。

### 3.6 恢复面（决定风险）

```bash
ls -l /dev/watchdog*; ls -la /sys/class/watchdog/; cat /proc/sys/kernel/panic; dmesg | grep -i 'SRST\|watchdog'
systemctl is-active ssh; who; sudo -n -l
```
```
ls: cannot access '/dev/watchdog*': No such file or directory
/sys/class/watchdog/: 空
/proc/sys/kernel/panic = 0 ; panic_on_oops = 0 ; panic_on_warn = 0
[0.000000] SBI SRST extension detected                 # 软件 reboot 可用；但内核挂死时无效
[3.24...] win2030-eth-dwmac ...: Enable RX Mitigation via HW Watchdog Timer   # 网卡内部，非可复位看门狗
ssh: active / enabled
sudo: a password is required
```
pku4090 USB 清单：只有 FTDI UART（`0403:6001`）+ 键鼠/音频/灯控，**没有电源/复位继电器**。
结论：**没有可远程触发的硬复位通道**；用户态前挂死需人工物理复位。

### 3.7 磁盘 / 内存 / QEMU

```bash
df -h / /boot; free -h; lsblk; qemu-system-riscv64 --version; qemu-system-riscv64 -accel help
timeout 6 qemu-system-riscv64 -accel kvm -machine virt -display none -nodefaults -m 64 -S
```
```
/dev/mmcblk1p3  113G   73G   36G  68% /
/dev/mmcblk1p1  459M  414M   15.4M  97% /boot          # 余量很小
Mem: 9.7Gi total, 452Mi used, 8.9Gi free, 9.2Gi available; Swap: 0
mmcblk1: p1 /boot 500M, p2 4G（Asterinas，未挂载）, p3 / 114.6G
QEMU emulator version 9.2.0 (Debian 1:9.2.0+ds-5rockos1)
Accelerators supported in QEMU binary: tcg, kvm
qemu-system-riscv64: -accel kvm: Could not access KVM kernel module: No such file or directory
qemu-system-riscv64: -accel kvm: failed to initialize kvm: No such file or directory
```
（`-accel help` 里出现 `kvm` 只说明二进制编了 KVM，不证明可用——正是 9/22 证据的边界。）

### 3.8 板子当前是否"干净"

- 无 `qemu-system*` 进程；`mmcblk1p2` 未挂载；`/dev/kvm` 不存在；SSH 正常。
- `w` 只有本次 `debian` SSH（来自 10.100.19.216）与 lightdm 会话；未发现其他实验占用。
- `asterinas.conf` 默认 `l0` = RockOS；vendor `extlinux.conf` 默认 `l0` = RockOS（未改）。
- `/boot/extlinux/` 下没有 `*kvm*` 文件（一次性 conf 尚未创建）。

---

## 4. 待批准的"一步"

> **需要 Lead 明确批准，并回答一个前置问题。**

**前置问题（gate）**：板子目前在物理上是否有人/有人可到达？因为一旦厂商内核在 userspace 之前挂死，
现有恢复面只有"人工物理复位"（无 watchdog、无远程电源）。9/22 设计正是因此把一次性启动推迟。
若答案是否，**建议不执行**本次一次性启动，改为先做离线/低风险替代（见 §5）。

**若批准，请求授权的完整清单（一次 ONLY ONE 次启动，做完立即回退）**：
1. 独占串口 `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0`（115200），只发一次
   `sysboot mmc 1:1 any 0x88200000 /extlinux/asterinas-rockos-kvm-once-1f94698f13b7.conf || run bootcmd_rockos`；
   **不 `saveenv`**、不碰默认选择器。
2. 在 `/boot/extlinux/` 新写入 440 字节的 `asterinas-rockos-kvm-once-1f94698f13b7.conf`（不覆盖任何现有文件）。
3. 一次软件重启 + 一次 U-Boot autoboot 打断；root 执行一次 `modprobe kvm`；一次有界 headless QEMU `-accel kvm` 冒烟；
   一次回退重启。
4. root/sudo 凭据由操作者带外提供（本次探测器没有、也不应记录）。
5. 全程保留串口 transcript、命令 exit code、`dmesg | grep kvm`、`/dev/kvm` stat、QEMU 输出与回退后的 boot ID/选择器哈希。

**明确不做**（无论批准与否）：不碰 `mmcblk1p2`；不安装/替换内核包；不修改 vendor 菜单/`asterinas.conf`；
不动 `/boot/vmlinuz-6.6.87-win2030`；不把 `-origin` 设为持久默认；不写 U-Boot env。

**若 Lead 拒绝/暂缓**，两个低风险替代（都不能替代这次启动）：
- 从 zrp（`DESKTOP-ZRP`）取回 `6.6.87` 的源码树/`.config`/`Module.symvers`，为**运行内核**重建并安装匹配的 `kvm.ko`
  （改动更小，但依赖外部材料，且仍需 root/重启/同一恢复风险）；
- 先继续 x86 TCG 调试，KVM 只作为后续整机 Firefox 吞吐实验的可选项。

---

## 5. 批准后的验证顺序（一张清单）

1. 预检（只读）：`sha256sum` 重复本报告 §3.5 的七个哈希；确认 `/boot` 余量 ≥ 1MiB、串口空闲、无 QEMU。
2. 写入一次性 conf（root），`sync`，回读 sha256 = `1f94698f13b7052fefd497605e0600438337e7e88bb86172905195c89428ccb3`。
3. 串口独占 → 软件重启 → U-Boot 打断 → `sysboot … || run bootcmd_rockos`。
4. 用户态：`uname -r` 必须等于 `6.6.87-win2030`；SSH/串口恢复；`dmesg | grep -iE 'kvm|hypervisor'`。
5. root：`modprobe kvm` → `ls -l /dev/kvm`（字符设备）→ `dmesg` 看 `hypervisor extension available`。
6. KVM API 冒烟：
```bash
qemu-system-riscv64 -accel kvm -machine virt -cpu host -smp 4 -m 256 \
  -display none -nodefaults -S -monitor none -serial none
```
（`-cpu host` 与是否自动跳过 guest OpenSBI 以 QEMU 实际报错为准；不为了通过而乱加 `-bios`。）
7. Asterinas 冻结镜像冒烟（同 9/22 的 kernel.Image/Stage1，但 `-accel kvm`），有界超时；
   成功后再考虑用 `tools/riscv/dual_host_qemu_probe.py` 做 KVM vs TCG 的同输入计时。
8. 回退：`reboot` → 核验新 boot ID、两个选择器哈希不变、SSH UID 1000、串口 nonce 重开；
   经批准删除一次性 conf + `sync`，再次核对 `/boot` 余量与 vendor 菜单哈希。

**失败分类（写清，避免误判）**：
- `modprobe kvm` 报 vermagic/unknown symbol → 内核/模块配对仍不对（先查 `uname -r` 与模块树）。
- `dmesg` 报 `hypervisor extension not available` 或 H CSR 异常 → **这才是**"更底层缺失（硅片/固件）"的证据。
- `/dev/kvm` 存在但 QEMU `-accel kvm` 失败 → QEMU/KVM ABI 或 QEMU 限制，不等于板子 H 扩展坏。
- 内核在用户态前挂死 → 记"未恢复的一步性启动失败"，按设计需要物理复位；不要反复重试。

---

## 附录 A：核心只读清单 raw output（原样）

命令入口（在 pku4090 上）：
```bash
ssh -o BatchMode=yes pku4090-direct 'ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=5 debian@10.100.19.200 bash -s' < board_kvm_probe2.sh > board_probe2.txt 2>&1
```
输出：
```
############ A IDENTITY ############
board date: 2026-10-08T19:27:18+08:00
BOOT_ID: 9c777a00-cc49-47c7-829a-c6abac19dbc9
uname -r: 6.6.87
uname -a: Linux rockos-eswin 6.6.87 #1 SMP Mon Sep  1 21:20:09 CST 2025 riscv64 GNU/Linux
--- cmdline ---
root=PARTUUID=b0f77ad6-36cd-4a99-a8c0-31d73649aa09 console=ttyS0,115200 root=PARTUUID=b0f77ad6-36cd-4a99-a8c0-31d73649aa09 rootfstype=ext4 rootwait rw earlycon selinux=0 LANG=en_US.UTF-8 earlycon=sbi console=ttyS0,115200n8 clk_ignore_unused cma_pernuma=0x2000000 disable_bypass=false

############ B MODULE TOOLING ############
command -v modprobe: /usr/sbin/modprobe
command -v insmod: /usr/sbin/insmod
command -v lsmod: /usr/sbin/lsmod
command -v modinfo: /usr/sbin/modinfo
command -v kmod: /usr/bin/kmod
--- ls /sbin | grep mod ---
depmod
groupmod
insmod
lsmod
modinfo
modprobe
rmmod
usermod
depmod
groupmod
insmod
lsmod
modinfo
modprobe
rmmod
usermod
--- busybox insmod ---
--- dpkg kmod ---
ii  kmod                                    33+20240816-2                        riscv64      tools for managing Linux kernel modules
ii  libkmod2:riscv64                        33+20240816-2                        riscv64      libkmod shared library

############ C BOOT CONFIG FILES ############
=== /boot/extlinux/extlinux.conf ===
## /boot/extlinux/extlinux.conf
##
## IMPORTANT WARNING
##
## The configuration of this file is generated automatically.
## Do not edit this file manually, use: u-boot-update

default l0
menu title U-Boot menu
prompt 2
timeout 50


label l0
	menu label RockOS GNU/Linux 6.6.87-win2030
	linux /vmlinuz-6.6.87-win2030
	initrd /initrd.img-6.6.87-win2030
	fdtdir /dtbs/linux-image-6.6.87-win2030/
	
	append root=PARTUUID=b0f77ad6-36cd-4a99-a8c0-31d73649aa09 console=ttyS0,115200 root=PARTUUID=b0f77ad6-36cd-4a99-a8c0-31d73649aa09 rootfstype=ext4 rootwait rw earlycon selinux=0 LANG=en_US.UTF-8

label l0r
	menu label RockOS GNU/Linux 6.6.87-win2030 (rescue target)
	linux /vmlinuz-6.6.87-win2030
	initrd /initrd.img-6.6.87-win2030
	fdtdir /dtbs/linux-image-6.6.87-win2030/
	append root=PARTUUID=b0f77ad6-36cd-4a99-a8c0-31d73649aa09 console=ttyS0,115200 root=PARTUUID=b0f77ad6-36cd-4a99-a8c0-31d73649aa09 rootfstype=ext4 rootwait rw earlycon selinux=0 LANG=en_US.UTF-8 single

label shyper
	menu label Rust-Shyper
	linux /Imagezrp_riscv64_eswin
	fdtdir /dtbs/linux-image-6.6.87-win2030/

=== /boot/extlinux/asterinas.conf ===
default l0
menu title Megrez operating system
prompt 1
timeout 100

label l0
	menu label RockOS GNU/Linux 6.6.87-win2030
	linux /vmlinuz-6.6.87-win2030
	initrd /initrd.img-6.6.87-win2030
	fdtdir /dtbs/linux-image-6.6.87-win2030/
	
	append root=PARTUUID=b0f77ad6-36cd-4a99-a8c0-31d73649aa09 console=ttyS0,115200 root=PARTUUID=b0f77ad6-36cd-4a99-a8c0-31d73649aa09 rootfstype=ext4 rootwait rw earlycon selinux=0 LANG=en_US.UTF-8

label asterinas-basic
menu label Asterinas Basic
linux /asterinas-485b9079c204.booti
initrd /stage1-d62ab8325e03.cpio
fdt /megrez-465cb129333c.dtb
append console=ttyS0 loglevel=info asterinas.klog_capture=info init=/init -- --root-init=basic

label asterinas-probe
menu label Asterinas Probe
linux /asterinas-485b9079c204.booti
initrd /stage1-d62ab8325e03.cpio
fdt /megrez-465cb129333c.dtb
append console=ttyS0 loglevel=info asterinas.klog_capture=info init=/init asterinas.reboot_after=90 -- --root-init=probe-auto

label asterinas-desktop
menu label Asterinas Desktop
linux /asterinas-485b9079c204.booti
initrd /stage1-d62ab8325e03.cpio
fdt /megrez-465cb129333c.dtb
append console=tty0 loglevel=off asterinas.klog_capture=info init=/init asterinas.mmc_write_partition2 systemd.mask=asterinas-browser-web-evidence.service systemd.mask=asterinas-desktop-m5-network.service systemd.mask=serial-getty@ttyS0.service systemd.mask=console-getty.service systemd.setenv=ASTERINAS_BROWSER_WEB_BASIC_ONLY=1 systemd.setenv=ASTERINAS_WEB_NETWORK_MODE=direct -- --root-init=systemd --debug-console=root --volatile-home

=== mount / ===
/dev/mmcblk1p3 / ext4 112.7G 35.1G
/dev/mmcblk1p1 /boot ext4 458.3M 15.4M

############ D KERNEL IMAGE PROVENANCE ############
### /boot/vmlinuz-6.6.87-win2030
-rw-r--r-- 1 root root 9419871 Sep  6  2025 /boot/vmlinuz-6.6.87-win2030
dda6a176a46fa7e791c7f603840c6a0fc128d315cad7e9b30d886c926f965c98  /boot/vmlinuz-6.6.87-win2030
/boot/vmlinuz-6.6.87-win2030: gzip compressed data, was "vmlinux-6.6.87-win2030", last modified: Sat Sep  6 12:13:41 2025, from Unix, original size modulo 2^32 26571776
vmlinux-6.6.87-win2030
### /boot/vmlinuz-6.6.87-win2030-origin
-rw-r--r-- 1 root root 9706299 Sep  6  2025 /boot/vmlinuz-6.6.87-win2030-origin
4af973c27aa87679aad75966dd816665466837cb8adf473e3222c523195d1f3c  /boot/vmlinuz-6.6.87-win2030-origin
/boot/vmlinuz-6.6.87-win2030-origin: gzip compressed data, max compression, from Unix, original size modulo 2^32 26572288
### /boot/Imagezrp_riscv64_eswin
-rw-r--r-- 1 root root 30426216 Sep  8  2025 /boot/Imagezrp_riscv64_eswin
677f00a05f00de20c9bcbf7630b0d5b1d2629a1403ffcf4f7b74706d220859fa  /boot/Imagezrp_riscv64_eswin
/boot/Imagezrp_riscv64_eswin: u-boot legacy uImage, rust_shyper, Linux/RISC-V, OS Kernel Image (Not compressed), 30426152 bytes, Fri Sep 12 06:45:13 2025, Load Address: 0X80200000, Entry Point: 0X80200000, Header CRC: 0X2E68E858, Data CRC: 0X3818660
Linux version 6.6.87 (zrp@DESKTOP-ZRP) (riscv64-linux-gnu-gcc (Ubuntu 11.4.0-1ubuntu1~22.04) 11.4.0, GNU ld (GNU Binutils for Ubuntu) 2.38) # SMP
Linux version 6.6.87 (zrp@DESKTOP-ZRP) (riscv64-linux-gnu-gcc (Ubuntu 11.4.0-1ubuntu1~22.04) 11.4.0, GNU ld (GNU Binutils for Ubuntu) 2.38) #13 SMP Mon Sep  8 22:27:31 CST 2025
6.6.87 SMP mod_unload riscv
eswin,win2030-pcie
--- running kernel config identity vs win2030 config ---
51ad03deedea2198f19809f1647a448f7fc14df4ca304df12690f2e296acf405  -
9a2964f4791f445604a6985a40a8441f594367a59449d164d439459f214412a9  /boot/config-6.6.87-win2030
--- config LOCALVERSION / KVM / H ---
CONFIG_LOCALVERSION=""
# CONFIG_LOCALVERSION_AUTO is not set
CONFIG_RISCV_ISA_C=y
CONFIG_RISCV_ISA_SVNAPOT=y
CONFIG_RISCV_ISA_SVPBMT=y
CONFIG_RISCV_ISA_V=y
CONFIG_RISCV_ISA_V_DEFAULT_ENABLE=y
CONFIG_RISCV_ISA_ZBB=y
CONFIG_RISCV_ISA_ZICBOM=y
CONFIG_RISCV_ISA_ZICBOZ=y
CONFIG_RISCV_ISA_FALLBACK=y
CONFIG_KVM=m
--- running config KVM/H ---
CONFIG_LOCALVERSION=""
# CONFIG_LOCALVERSION_AUTO is not set
CONFIG_RISCV_ISA_C=y
CONFIG_RISCV_ISA_SVNAPOT=y
CONFIG_RISCV_ISA_SVPBMT=y
CONFIG_RISCV_ISA_V=y
CONFIG_RISCV_ISA_V_DEFAULT_ENABLE=y
CONFIG_RISCV_ISA_ZICBOM=y
CONFIG_RISCV_ISA_ZICBOZ=y
CONFIG_RISCV_ISA_FALLBACK=y
CONFIG_KVM=m
--- initrd ---
-rw-r--r-- 1 root root 62583027 Apr 23  2025 /boot/initrd.img-6.6.87-win2030

############ E DMESG ############
dmesg_restrict=0 kptr_restrict=0
[    0.000000] SBI specification v2.0 detected
[    0.000000] SBI implementation ID=0x1 Version=0x10005
[    0.000000] SBI TIME extension detected
[    0.000000] SBI IPI extension detected
[    0.000000] SBI RFENCE extension detected
[    0.000000] SBI SRST extension detected
[    0.000000] SBI HSM extension detected
[    0.000000] Falling back to deprecated "riscv,isa"
[    0.000000] riscv: base ISA extensions acdfhim
[    0.000000] Kernel command line: root=PARTUUID=b0f77ad6-36cd-4a99-a8c0-31d73649aa09 console=ttyS0,115200 root=PARTUUID=b0f77ad6-36cd-4a99-a8c0-31d73649aa09 rootfstype=ext4 rootwait rw earlycon selinux=0 LANG=en_US.UTF-8 earlycon=sbi console=ttyS0,115200n8 clk_ignore_unused cma_pernuma=0x2000000 disable_bypass=false
[    0.000000] riscv: providing IPIs using SBI IPI extension
[    4.996590] cpuidle-riscv-sbi: idle driver registered for all CPUs
[    5.738876] riscv-pmu-sbi: SBI PMU extension is available
[    5.744299] riscv-pmu-sbi: 16 firmware and 6 hardware counters
--- dmesg head 15 ---
[    0.000000] Linux version 6.6.87 (zrp@DESKTOP-ZRP) (riscv64-linux-gnu-gcc (Ubuntu 11.4.0-1ubuntu1~22.04) 11.4.0, GNU ld (GNU Binutils for Ubuntu) 2.38) #1 SMP Mon Sep  1 21:20:09 CST 2025
[    0.000000] Machine model: Milk-V Megrez
[    0.000000] SBI specification v2.0 detected
[    0.000000] SBI implementation ID=0x1 Version=0x10005
[    0.000000] SBI TIME extension detected
[    0.000000] SBI IPI extension detected
[    0.000000] SBI RFENCE extension detected
[    0.000000] SBI SRST extension detected
[    0.000000] earlycon: uart0 at MMIO32 0x0000000050900000 (options '115200n8')
[    0.000000] printk: bootconsole [uart0] enabled
[    0.000000] efi: UEFI not found.
[    0.000000] Reserved memory: created CMA memory pool at 0x00000002dfc00000, size 512 MiB
[    0.000000] OF: reserved mem: initialized node linux,cma, compatible id shared-dma-pool
[    0.000000] OF: reserved mem: 0x00000002dfc00000..0x00000002ffbfffff (524288 KiB) map reusable linux,cma
[    0.000000] OF: reserved mem: 0x0000000059000000..0x00000000593fffff (4096 KiB) nomap non-reusable sprammemory@59000000
--- dmesg tail 6 ---
[   19.506010] dw-hdmi-eswin 502a0000.hdmi: got edid: width[0] x height[0]
[   19.620854] dw-hdmi-eswin 502a0000.hdmi: got edid: width[0] x height[0]
[   21.001436] win2030-eth-dwmac 50410000.ethernet end1: Link is Up - 1Gbps/Full - flow control rx/tx
[  929.988586] dw-hdmi-eswin 502a0000.hdmi: got edid: width[0] x height[0]
[14743.879815] dw-hdmi-eswin 502a0000.hdmi: got edid: width[0] x height[0]
[17492.777415] dw-hdmi-eswin 502a0000.hdmi: got edid: width[0] x height[0]

############ F QEMU ############
/usr/bin/qemu-system-riscv64
/usr/bin/qemu-img
QEMU emulator version 9.2.0 (Debian 1:9.2.0+ds-5rockos1)
Copyright (c) 2003-2024 Fabrice Bellard and the QEMU Project developers
--- accel help ---
Accelerators supported in QEMU binary:
tcg
kvm
--- package ---
ii  ipxe-qemu                               1.0.0+git-20190125.36a4c85-5.1       all          PXE boot firmware - ROM images for qemu
ii  qemu-block-extra                        1:9.2.0+ds-5rockos1                  riscv64      extra block backend modules for qemu-system and qemu-utils
ii  qemu-efi-aarch64                        2024.08-4                            all          UEFI firmware for 64-bit ARM virtual machines
ii  qemu-efi-arm                            2024.08-4                            all          UEFI firmware for 32-bit ARM virtual machines
ii  qemu-efi-riscv64                        2024.08-4                            all          UEFI firmware for RISCV64 virtual machines
ii  qemu-system                             1:9.2.0+ds-5rockos1                  riscv64      QEMU full system emulation binaries
ii  qemu-system-arm                         1:9.2.0+ds-5rockos1                  riscv64      QEMU full system emulation binaries (arm)
ii  qemu-system-common                      1:9.2.0+ds-5rockos1                  riscv64      QEMU full system emulation binaries (common files)

############ G RESOURCES ############
Filesystem      Size  Used Avail Use% Mounted on
/dev/mmcblk1p3  113G   73G   36G  68% /
/dev/mmcblk1p1  459M  414M   16M  97% /boot
               total        used        free      shared  buff/cache   available
Mem:           9.7Gi       452Mi       8.9Gi        17Mi       471Mi       9.2Gi
Swap:             0B          0B          0B
 19:27:22 up 2 days, 7 min,  4 users,  load average: 0.15, 0.11, 0.09
--- lsblk ---
NAME        MAJ:MIN RM   SIZE RO TYPE MOUNTPOINTS
mmcblk1     179:0    0 119.1G  0 disk
├─mmcblk1p1 179:1    0   500M  0 part /boot
├─mmcblk1p2 179:2    0     4G  0 part
└─mmcblk1p3 179:3    0 114.6G  0 part /
--- mmc partitions ---
brw-rw---- 1 root disk 179, 0 Dec  2  2024 /dev/mmcblk1
brw-rw---- 1 root disk 179, 1 Dec  2  2024 /dev/mmcblk1p1
brw-rw---- 1 root disk 179, 2 Dec  2  2024 /dev/mmcblk1p2
brw-rw---- 1 root disk 179, 3 Dec  2  2024 /dev/mmcblk1p3

############ H KDEP / MODULE DETAIL ############
kernel/arch/riscv/kvm/kvm.ko:
--- modules.builtin kvm ---
--- kvm.ko modinfo via strings-ish (vermagic) ---
vermagic=6.6.87-win2030 SMP mod_unload riscv
__UNIQUE_ID_vermagic246
--- module symbols sha ---
defc0e79c280b0ebfc705df13fccdc3c927b69d657c3fcb493db085f0397c0df  /lib/modules/6.6.87-win2030/kernel/arch/riscv/kvm/kvm.ko

############ I WIN2030 TREE ARCH/RISCV ############
/lib/modules/6.6.87-win2030/kernel/arch/riscv/kvm/kvm.ko
--- total modules count ---
734

############ J CPUFEATURE H EVIDENCE ############
rv64imafdch_zicntr_zicsr_zifencei_zihpm_zba_zbb_sscofpmf
--- /sys/devices/system/cpu/cpu0/isa ? ---
--- device-tree riscv,isa-extensions ---
--- opensbi version in DT ---

############ K SUDO / ROOT ACCESS ############
sudo: a password is required
sudo -n exit=1

############ DONE ############
```

## 附录 B：内核/模块/QEMU 定向 raw output（原样）

```bash
ssh -o BatchMode=yes pku4090-direct 'ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=5 debian@10.100.19.200 bash -s' < board_kvm_probe3.sh > board_probe3.txt 2>&1
```
输出：
```
===== 1 KERNEL BANNERS (decompressed vmlinuz) =====
### /boot/vmlinuz-6.6.87-win2030
Linux version 6.6.87 (zrp@DESKTOP-ZRP) (riscv64-linux-gnu-gcc (Ubuntu 11.4.0-1ubuntu1~22.04) 11.4.0, GNU ld (GNU Binutils for Ubuntu) 2.38) # SMP
Linux version 6.6.87 (zrp@DESKTOP-ZRP) (riscv64-linux-gnu-gcc (Ubuntu 11.4.0-1ubuntu1~22.04) 11.4.0, GNU ld (GNU Binutils for Ubuntu) 2.38) #1 SMP Mon Sep  1 21:20:09 CST 2025
exit=0
--- utsrelease-ish strings ---
6.6.87 SMP mod_unload riscv
6.6.87
6.6.87
### /boot/vmlinuz-6.6.87-win2030-origin
Linux version 6.6.87-win2030 (builder@rockos-riscv-builder) (riscv64-linux-gnu-gcc-13 (Ubuntu 13.2.0-23ubuntu4) 13.2.0, GNU ld (GNU Binutils for Ubuntu) 2.42) #2025.04.20.18.43+bb0c69aea SMP
Linux version 6.6.87-win2030 (builder@rockos-riscv-builder) (riscv64-linux-gnu-gcc-13 (Ubuntu 13.2.0-23ubuntu4) 13.2.0, GNU ld (GNU Binutils for Ubuntu) 2.42) #2025.04.20.18.43+bb0c69aea SMP Sun Apr 20 18:58:14 UTC 2025
exit=0
--- utsrelease-ish strings ---
6.6.87-win2030 SMP mod_unload riscv
6.6.87-win2030
6.6.87-win2030

===== 2 MODPROBE DRY-RUNS =====
--- modprobe -n -v kvm (running kernel) ---
modprobe: FATAL: Module kvm not found in directory /lib/modules/6.6.87
exit=1
--- modprobe -n -v -S 6.6.87-win2030 kvm ---
insmod /lib/modules/6.6.87-win2030/kernel/arch/riscv/kvm/kvm.ko
exit=0
--- modinfo -k 6.6.87-win2030 kvm ---
filename:       /lib/modules/6.6.87-win2030/kernel/arch/riscv/kvm/kvm.ko
license:        GPL
author:         Qumranet
depends:
intree:         Y
name:           kvm
vermagic:       6.6.87-win2030 SMP mod_unload riscv
parm:           halt_poll_ns:uint
parm:           halt_poll_ns_grow:uint
parm:           halt_poll_ns_grow_start:uint
parm:           halt_poll_ns_shrink:uint
--- modinfo -k 6.6.87-win2030 kvm (vermagic/depends) ---
6.6.87-win2030 SMP mod_unload riscv


===== 3 KVM MODULE INTERNALS =====
6kvm [%i]: hypervisor extension not available
6kvm [%i]: hypervisor extension available
kvm_riscv_set_irq
kvm_riscv_vcpu_enter_exit
kvm_riscv_reset_vcpu
kvm_riscv_vcpu_vreg_addr
kvm_riscv_vcpu_update_vstimecmp
kvm_riscv_vcpu_update_hrtimer
kvm_riscv_vcpu_vstimer_expired
kvm_riscv_vcpu_hrtimer_expired
kvm_riscv_vcpu_pmu_deinit.part.0
kvm_riscv_aia_available
kvm_riscv_vcpu_timer_pending
kvm_riscv_gstage_vmid_update
kvm_riscv_aia_disable
kvm_riscv_aia_max_ids
kvm_riscv_vcpu_timer_reset
kvm_riscv_local_hfence_gvma_gpa
kvm_riscv_hfence_vvma_all
kvm_riscv_vcpu_sbi_return
--- file ---
/lib/modules/6.6.87-win2030/kernel/arch/riscv/kvm/kvm.ko: ELF 64-bit LSB relocatable, UCB RISC-V, RVC, soft-float ABI, version 1 (SYSV), BuildID[sha1]=9598c6b31be16cdf770d1846cc0ff5ec2d7ba46f, not stripped

===== 4 VENDOR KERNEL CONFIG CRITICALS =====
CONFIG_64BIT=y
CONFIG_RISCV_SBI=y
CONFIG_SMP=y
CONFIG_RISCV_ISA_C=y
CONFIG_RISCV_ISA_SVNAPOT=y
CONFIG_RISCV_ISA_SVPBMT=y
CONFIG_RISCV_ISA_V=y
CONFIG_RISCV_ISA_V_DEFAULT_ENABLE=y
CONFIG_RISCV_ISA_ZBB=y
CONFIG_RISCV_ISA_ZICBOM=y
CONFIG_RISCV_ISA_ZICBOZ=y
CONFIG_RISCV_ISA_FALLBACK=y
CONFIG_KVM=m
CONFIG_MODULES=y
CONFIG_MODULE_UNLOAD=y
# CONFIG_MODULE_UNLOAD_TAINT_TRACKING is not set
# CONFIG_MODULE_SIG is not set
--- running kernel tainted ---
0

===== 5 PACKAGES / HEADERS =====
ii  linux-headers-6.6.87-win2030            6.6.87-2025.04.20.18.43+bb0c69aea    riscv64      Linux kernel headers for 6.6.87-win2030 on riscv64
ii  linux-image-6.6.87-win2030              6.6.87-2025.04.20.18.43+bb0c69aea    riscv64      Linux kernel, version 6.6.87-win2030
--- /usr/src ---
total 16
drwxr-xr-x  4 root root 4096 Aug 28  2025 .
drwxr-xr-x 11 root root 4096 Apr 23  2025 ..
drwxr-xr-x  5 root root 4096 Apr 23  2025 linux-headers-6.6.87-win2030
drwxr-xr-x  4 root root 4096 Aug 28  2025 python3.12
--- /boot/dtbs ---
total 15
drwxr-xr-x 3 root root  1024 Apr 23  2025 .
drwxr-xr-x 7 root root 12288 Oct  6 18:41 ..
drwxr-xr-x 3 root root  1024 Apr 23  2025 linux-image-6.6.87-win2030
eswin

===== 6 QEMU KVM ATTEMPT (read-only, bounded) =====
qemu-system-riscv64: -accel kvm: Could not access KVM kernel module: No such file or directory
qemu-system-riscv64: -accel kvm: failed to initialize kvm: No such file or directory
reset=0
--- /dev/kvm ---
ls: cannot access '/dev/kvm': No such file or directory
--- /proc/misc ---

===== 7 FIRMWARE / PLATFORM IDENTITY =====
model: Milk-V Megrez
timebase:  1078071040
compatible:
sifive,hifive-unmatched-a00
sifive,fu740-c000
sifive,fu740
eswin,eic7700
chosen/bootargs:
root=PARTUUID=b0f77ad6-36cd-4a99-a8c0-31d73649aa09 console=ttyS0,115200 root=PARTUUID=b0f77ad6-36cd-4a99-a8c0-31d73649aa09 rootfstype=ext4 rootwait rw earlycon selinux=0 LANG=en_US.UTF-8
--- fw_env ---
ls: cannot access '/etc/fw_env.config': No such file or directory
cat: /etc/fw_env.config: No such file or directory

===== 8 BOOT PARTITION FREE / EXTLINUX IDENTITY =====
Filesystem      Size  Used Avail Use% Mounted on
/dev/mmcblk1p1  459M  414M   16M  97% /boot
eb5f39a6e2db71ccc93ae005c488fd9f7ef353e75426e5a51e8923a9cbf2ebc5  /boot/extlinux/extlinux.conf
02280720efe7a1ad0ac084cdc20429406631e12d2e16f05638544bab0883fb26  /boot/extlinux/asterinas.conf
dda6a176a46fa7e791c7f603840c6a0fc128d315cad7e9b30d886c926f965c98  /boot/vmlinuz-6.6.87-win2030
77effdb35a32cf708555f769458f405780693da0d31588541b4d6d205e9d5eb8  /boot/System.map-6.6.87-win2030
9a2964f4791f445604a6985a40a8441f594367a59449d164d439459f214412a9  /boot/config-6.6.87-win2030
--- /boot/file count ---
79

===== 9 H EXTENSION: every evidence line =====
[    0.000000] Falling back to deprecated "riscv,isa"
[    0.000000] riscv: base ISA extensions acdfhim

===== DONE =====
```

## 附录 C：恢复面/包完整性 raw output（原样）

```bash
ssh -o BatchMode=yes pku4090-direct 'ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=5 debian@10.100.19.200 bash -s' < board_kvm_probe4.sh > board_probe4.txt 2>&1
```
输出：
```
===== 1 RECOVERY SURFACE =====
--- watchdog devices ---
ls: cannot access '/dev/watchdog*': No such file or directory
total 0
drwxr-xr-x  2 root root 0 Oct  8 19:30 .
drwxr-xr-x 69 root root 0 Jan  1  1970 ..
[    3.246940] win2030-eth-dwmac 50400000.ethernet: Enable RX Mitigation via HW Watchdog Timer
[    3.588633] win2030-eth-dwmac 50410000.ethernet: Enable RX Mitigation via HW Watchdog Timer
--- panic settings ---
/proc/sys/kernel/panic = 0
/proc/sys/kernel/panic_on_oops = 0
/proc/sys/kernel/panic_on_warn = 0
--- SBI SRST ---
[    0.000000] SBI SRST extension detected

===== 2 SESSIONS / SSH =====
active
enabled
--- w ---
 19:30:12 up 2 days, 9 min,  4 users,  load average: 0.35, 0.21, 0.13
USER     TTY      FROM             LOGIN@   IDLE   JCPU   PCPU WHAT
debian            10.100.19.216    19:30    2:50   0.00s  0.02s sshd-session: debian [priv]
debian            -                19:30    2:50   0.00s  0.39s /usr/lib/systemd/systemd --user
lightdm           -                Tue19    2:50   0.00s  0.14s lightdm --session-child 19 22
lightdm           -                Tue19    2:50   0.00s  0.66s /usr/lib/systemd/systemd --user
--- sshd listen ---
LISTEN 0      128          0.0.0.0:22        0.0.0.0:*

===== 3 PACKAGE INTEGRITY =====
--- dpkg -V linux-image-6.6.87-win2030 ---
??5??????   /boot/vmlinuz-6.6.87-win2030
exit=0
--- package files of interest ---
/boot/dtbs
/boot/dtbs/linux-image-6.6.87-win2030
/boot/dtbs/linux-image-6.6.87-win2030/eswin
/boot/dtbs/linux-image-6.6.87-win2030/eswin/eic7700-d314.dtb
/boot/dtbs/linux-image-6.6.87-win2030/eswin/eic7700-evb-a2.dtb
/boot/dtbs/linux-image-6.6.87-win2030/eswin/eic7700-evb-a3.dtb
/boot/dtbs/linux-image-6.6.87-win2030/eswin/eic7700-evb.dtb
/boot/dtbs/linux-image-6.6.87-win2030/eswin/eic7700-hifive-premier-p550.dtb
/boot/dtbs/linux-image-6.6.87-win2030/eswin/eic7700-milkv-megrez.dtb
/boot/dtbs/linux-image-6.6.87-win2030/eswin/eic7700-osm.dtb

===== 4 DTB / FDTDIR =====
/boot/dtbs/linux-image-6.6.87-win2030/eswin/eic7700-sbc-a1.dtb
/boot/dtbs/linux-image-6.6.87-win2030/eswin/eic7702-evb-a1.dtb
/boot/dtbs/linux-image-6.6.87-win2030/eswin/eic7702-evb-a1-d0.dtb
/boot/dtbs/linux-image-6.6.87-win2030/eswin/eic7700-milkv-megrez.dtb
/boot/dtbs/linux-image-6.6.87-win2030/eswin/eic7702-tb.dtb
/boot/dtbs/linux-image-6.6.87-win2030/eswin/eswin-win2030.dtb
/boot/dtbs/linux-image-6.6.87-win2030/eswin/eic7700-z530.dtb
/boot/dtbs/linux-image-6.6.87-win2030/eswin/eic7700-evb.dtb
/boot/dtbs/linux-image-6.6.87-win2030/eswin/eic7700-hifive-premier-p550.dtb
/boot/dtbs/linux-image-6.6.87-win2030/eswin/eic7702-evb-a1-interleave.dtb
--- /boot/dtb ---
total 166
drwxr-xr-x 2 root root   1024 Aug  5 13:38 .
drwxr-xr-x 7 root root  12288 Oct  6 18:41 ..
-r--r--r-- 1 root root 154800 Aug  5 13:38 megrez-02a8d43d581b.dtb
--- candidate megrez dtbs sha ---
02a8d43d581b4aa8e957e231ee90eba19ffd7e8cfcf74694e86a1fb9c6b37f17  /boot/dtbs/linux-image-6.6.87-win2030/eswin/eic7700-milkv-megrez.dtb
02a8d43d581b4aa8e957e231ee90eba19ffd7e8cfcf74694e86a1fb9c6b37f17  /boot/eic7700-milkv-megrez.dtb

===== 5 EXISTING KVM-ONCE FILES =====
kvm-conf-count=0

===== 6 HEADERS / MODULE BUILD MATERIAL =====
arch
include
Makefile
Module.symvers
scripts
-rw-r--r-- 1 root root 1068456 Apr 21  2025 /usr/src/linux-headers-6.6.87-win2030/Module.symvers
lrwxrwxrwx 1 root root 37 Apr 21  2025 /lib/modules/6.6.87-win2030/build -> /usr/src/linux-headers-6.6.87-win2030

===== 7 TFTP / QEMU SMOKE PREP =====
--- asterinas probe artifacts on RockOS cache (if any) ---
ls: cannot access '/tmp/asterinas-qemu-probe/': No such file or directory
--- python3 ---
Python 3.12.6
--- qemu kvm smoke attempt with Asterinas? (only check /dev/kvm absence) ---
ls: cannot access '/dev/kvm': No such file or directory

===== 8 ROOT CAPABILITY CHECK =====
sudo: a password is required
sudo -n -l exit=0
/usr/bin/su

===== DONE =====
```

## 附录 D：一次性菜单的离线复核（对当前板子重算）

```
$ python3 - <qualified script from commit c70c24306>   # vendor conf 从板子现读，sha256 eb5f39a6… 与设计 pin 一致
PATH: /extlinux/asterinas-rockos-kvm-once-1f94698f13b7.conf
COMMAND: sysboot mmc 1:1 any 0x88200000 /extlinux/asterinas-rockos-kvm-once-1f94698f13b7.conf || run bootcmd_rockos
CONF_SHA256: 1f94698f13b7052fefd497605e0600438337e7e88bb86172905195c89428ccb3
CONF_BYTES: 440
```
即 9/22 的离线限定器对**今天**的板子状态仍然通过：kernel sha `4af973c2…`、module sha `defc0e79…`、
vendor conf sha `eb5f39a6…` 全部匹配，`default l0` 仍是 RockOS。

---

## 证据身份与采集说明

- 采集窗口：2026-10-19 10:17–10:23 CST（pku4090 时钟）。板子时钟慢约 10.6 天，见 §2.3。
- 采集方式：只读；未重启、未写串口、未 `modprobe`、未写入板子文件系统、未改任何 boot 配置。
- 本次唯一写入：本文件（在 pku4090 的 `/mnt/shared/xaj/Program/asterinas/docs/porting/evidence/` 下新建）。
- 相关既有证据：`2026-09-22-rockos-hosted-asterinas-probe.md`、`2026-09-16-browser-interaction-performance-baseline.md`、
  `2026-09-22-rockos-control-recovery.md`；一次性设计提交 `c70c24306`（未合入 main，对象库可读）。
- 错误与限制：
  - 第一次探测脚本用 `head` 截断管道导致 SIGPIPE，输出只到一半；已重跑并保存（附录 A）。
  - 板子时钟不可用于绝对时间；本文所有时间戳以宿主观测为准。
  - 未读取 U-Boot 环境（`/etc/fw_env.config` 不存在，Linux 侧无法读 U-Boot env）；U-Boot 行为依据
    9/22 恢复记录与 `megrez_menu_board` 工具约定。
  - 未读取 KVM API（`/dev/kvm` 不存在）；未做任何"写"性质的可行性验证（如 insmod/强制加载），
    这是本次纪律要求。
