# S12（更新）：KVM 开机自动加载 —— 已完成并验证（未做冷启动）

**日期**：2026-10-19（宿主 pku4090 时钟；板子时钟慢 ~10.6 天，不用板上时间）
**状态**：**已完成**（由 Lead 以 root 身份在板上实施；本文由 board-kvm-advancer 只读复核并归档）。
**板子**：未重启、未改 boot/extlinux、未 saveenv；`boot_id 9c777a00-cc49-47c7-829a-c6abac19dbc9` 未变；`systemd` uptime 起始 2026-10-06 19:20:19（板钟）。**未做冷启动（reboot）验证**——按"板子无 watchdog、不可冒险"的纪律规避，留作后续条件满足时再验。

> ⚠️ 本文替代之前 "s12-autoload-blocked"：先前阻塞原因是 **sudo/登入方式**，非权限缺失。本板经验：**必须 TTY 交互 + 密码后带回车**（`ssh -tt` + expect/pexpect）；`echo pw | sudo -S` 这类非 TTY 管道在本板上会被拒（`sudo: no password was provided` / `对不起，请重试`）。记录为操作坑。

---

## 1. 落盘产物（root 操作，只读复核）

| 对象 | 权限/大小 | sha256 |
| --- | --- | --- |
| `/lib/modules/asterinas-kvm/kvm-6.6.87-patched.ko` | `-rw-r--r-- root:root`，518,432 B | `5035bff0eebfc02a9c848f3315c60c24debbbc8725de1dc94dc5d9cef17f9145`（即 S5 的 vermagic 微调版：`6.6.87 SMP mod_unload riscv`；不依赖 `/lib/modules/6.6.87` 模块树） |
| `/etc/systemd/system/asterinas-kvm.service` | `-rw-r--r-- root:root`，351 B | `663c0c266184426d7b9eaeecb8da8adb4d41a0aae38e55e8f9962ce14157291c` |
| `/etc/udev/rules.d/99-asterinas-kvm.rules` | `-rw-r--r-- root:root`，40 B | `37f666f1cdb7d8a62171de69b531681dcb0fba74236729dac8b6c019232eba84` |

unit 内容（实测）：

```ini
[Unit]
Description=Load Asterinas patched kvm.ko for QEMU -accel kvm
After=local-fs.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'lsmod | grep -q "^kvm " || /sbin/insmod /lib/modules/asterinas-kvm/kvm-6.6.87-patched.ko'
ExecStartPost=/bin/sh -c 'chmod 666 /dev/kvm 2>/dev/null || true'

[Install]
WantedBy=multi-user.target
```

udev 内容（实测）：

```
KERNEL=="kvm", GROUP="kvm", MODE="0666"
```

> 权限设计是"双保险"：unit 的 `ExecStartPost` 在加载后 `chmod 666`；udev 规则保证设备重插/重建时也归为 0666，使普通用户（`debian`）无需额外授权即可跑 QEMU `-accel kvm`。

## 2. 启用与不重启验证

| 步骤 | 结果（Lead 实施 + 本文件复核） |
| --- | --- |
| `systemctl daemon-reload && systemctl enable asterinas-kvm.service` | `enabled` |
| `systemctl is-active` | `active (exited)`；`ExecStart` / `ExecStartPost` 均 `status=0/SUCCESS` |
| `systemd-analyze verify /etc/systemd/system/asterinas-kvm.service` | 无输出，`verify_rc=0` |
| `systemctl stop` → `rmmod kvm` | `RMDMOD_OK`；`/dev/kvm` **消失** |
| `systemctl start` | `START_OK`；`kvm` 重新 loaded；`/dev/kvm` 回来且为 `crw-rw-rw-`；service `active (exited)` |
| dmesg | `kvm [12571]: hypervisor extension available`（本次重新加载，另见 S5 的 `kvm [5622]`） |
| 复核 | `lsmod` 有 `kvm 294912 0`；`/dev/kvm` = `crw-rw-rw-+ root kvm 10,232`；无 QEMU 进程；`boot_id` 未变 |

（Lead 原始 transcript 摘要：

```
systemctl stop → rmmod kvm → RMOD_OK → /dev/kvm GONE
systemctl start → START_OK → /dev/kvm back (crw-rw-rw-), kvm loaded, enabled+active
dmesg: kvm [12571]: hypervisor extension available
```
）

## 3. 尚未验证 / 后续冷启动验收清单

未做 reboot 验证。未来若条件允许（有人可就近物理复位），冷启动后按下列清单验收：

1. `systemctl is-enabled asterinas-kvm.service` → `enabled`
2. `systemctl is-active asterinas-kvm.service` → `active (exited)`
3. `lsmod | grep '^kvm '` 有输出；dmesg 有 `hypervisor extension available`
4. `ls -l /dev/kvm` → `crw-rw-rw-`（或 udev 规则生效后的 0666），`debian` 对 `/dev/kvm` 可读写
5. 一次有界冒烟：`qemu-system-riscv64 -accel kvm -machine virt -cpu host -S -display none -nodefaults` + monitor `info kvm` → `kvm support: enabled`（≤10 s）
6. 回退命令（如需要）：`sudo systemctl disable --now asterinas-kvm.service && sudo rmmod kvm`（不影响 boot 配置）
7. 记录 `boot_id` 与上述输出。

## 4. 变更清单

- 板子：仅新增上述 3 个文件（1 模块 + 1 unit + 1 udev 规则）；启用 1 个 service。**未改 boot/extlinux、未 saveenv、未重启、无其它系统文件改动**。
- 仓库：本文件（s12 更新）+ s13（Phase 2 收口）；未改主工作区。
