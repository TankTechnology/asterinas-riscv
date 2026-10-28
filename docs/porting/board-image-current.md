# Megrez 单镜像 / 单启动流程（整理目标）

状态：**已安装并启动（2026-10-28）**。唯一日常 rootfs 已写入 p2，
p1 只保留一个 Asterinas daily 启动项；p3 保留 p2 备份与 997c known-good。

## 0. 结论

板子上需要的是**一套**东西，不是每个里程碑一套：

```text
p1 /boot:  RockOS 启动文件 + 一个 Asterinas 启动项（kernel/stage1/DTB）
p2:        一个 Debian rootfs（日常使用）
p3:        RockOS 恢复系统 + 一个 Asterinas generation
```

现在 p2 只有一个 rootfs；看起来“镜像很多”的主要原因是：

- `p1 /boot`：**25 个历史 `asterinas-*.booti`**（每个 6–16 MB）+ **42 个
  `asterinas-menu-*.conf`**；`/boot` 459 MB 已用 414 MB，只剩 16 MB；
- `p3 /home/debian/asterinas/boot`：23 个旧 generation 目录；
- host `target/`：旧的 `e3-render-policy-r1` rootfs、若干 `stage1-*` 和
  历史构建产物。

这些是**迭代历史**，不是运行时需要的多个 Debian 系统。

## 1. 唯一的日常镜像

选中 `browser-web` profile（就是板上已验证的旧桌面镜像所属的 profile），
只做两项扩展：

- 加 `vim`、`build-essential`（gcc/g++/make/libc6-dev）；
- rootfs 从 2 GiB 增到 **3 GiB**（p2 是 4 GiB）。

它已经包含：

```text
Firefox ESR + libavcodec-extra61
openbox + pcmanfm + lxpanel + xterm + xdotool
Xorg fbdev（软件渲染，不使用 GPU 加速）
sudo + polkitd + apt
m5 网络 services + curl/iproute2/iputils-ping
P1 的 desktop-ready / DNS / 时钟 / safe-reboot 修复
```

身份：

```text
profile: browser-web
label:   ASTER_BROWSERWEB
uuid:    c2ce5134-afcc-4d7c-b71e-7e6d4a8f2b10
size:    3 GiB
```

构建（host 专用 RISC-V rootfs builder 容器，避免污染宿主）：

```bash
docker run --rm --network=host \
  -v "$PWD:/root/asterinas" \
  -v asterinas-riscv-rootfs-cache:/root/asterinas/target/debian-riscv/cache \
  -w /root/asterinas \
  -e SOURCE_DATE_EPOCH=$(date +%s) \
  asterinas/asterinas:0.18.0-20260702-riscv-rootfs \
  tools/riscv/debian/rootfs/build_rootfs.sh --profile browser-web
```

输出：`target/debian-riscv/browser-web/rootfs/`。
构建完成后补记 `debian-root.ext2`、`packages.lock`、`rootfs-manifest.json`
的 SHA-256。

## 2. 单启动流程

```text
U-Boot
  → 一个 Asterinas kernel + stage1 initramfs + DTB
  → stage1 挂载 p2 的 ASTER_BROWSERWEB
  → systemd → 桌面（openbox/PCManFM/lxpanel/xterm/Firefox）
```

RockOS 的 `vmlinuz-6.6.87-win2030` / `initrd` / `dtbs` 和 extlinux 的
RockOS 标签保持不动，作为恢复路径。

板端需要收敛成：

- p1 `/boot`：RockOS 文件 + **一个** Asterinas kernel/stage1/DTB 和
  **一个** Asterinas 菜单项；
- p3 `/home/debian/asterinas/boot`：**一个**当前 generation；
- p2：**一个**日常 rootfs；
- 不再保留 per-milestone 的 `.booti` / menu / rootfs。

## 3. 板端待执行清单（需要授权）

以下操作会写启动分区或覆盖 p2，按之前约束需要明确授权：

1. `p1 /boot` 删除 25 个历史 `asterinas-*.booti`、42 个
   `asterinas-menu-*.conf`、旧 canary/backup conf；保留 RockOS 文件；
   放入一个当前 Asterinas kernel/stage1/DTB 和一个菜单项。
   预计释放约 300 MB，解决 `/boot` 只剩 16 MB 的问题。
2. `p3 /home/debian/asterinas/boot` 删除旧 generation，保留当前
   known-good 的一个；RockOS 系统文件不动。
3. `p2` 用新的 3 GiB rootfs 覆盖旧 `browser-web-board-r1`；
   旧镜像在 host 仍有完整副本，可回滚。
4. 启动验证后，把旧 host rootfs/stage1 归档/删除。

## 4. 安装后的验收

串口/桌面：

```text
command -v sudo && sudo -n true
apt-get --version
vim --version | head -1
gcc --version | head -1
firefox --version
systemctl --failed
```

桌面：背景/窗口管理器/PCManFM/xterm/Firefox 正常；网络按旧 m5
的真实条件验证（DNS/代理），不能只用 QEMU slirp 的通过来代替板端。
如果网络仍需要 host proxy，按现状记录，不把它说成“已独立联网”。

## 5. 不做的

- 不再为每个里程碑新增 rootfs/profile；
- 不动 RockOS 的系统文件、恢复分区；
- 不在没有授权时覆盖 p2 或删除启动分区文件；
- 不使用 GPU 加速；桌面保持 fbdev 软件渲染。

## 6. 2026-10-28 真机结果（已完成）

镜像（host）：

```text
target/debian-riscv/browser-web/rootfs/debian-root.ext2
3,221,225,472 B
SHA-256 3b5807511393c1be65f9c3461504e6db45565b3cff309b6e37ef7bc4249ff34d
LABEL   ASTER_BROWSERWEB
UUID    c2ce5134-afcc-4d7c-b71e-7e6d4a8f2b10
ext2    786432 x 4096 B
```

包锁包含 Firefox 153.5.1esr-1~deb13u1、vim 2:9.1.1230-2、
gcc 14.2.0-19、build-essential 12.12、sudo 1.9.16p2-3+deb13u2、
polkitd 126-2。

板端：

- p2 已写入该镜像；写入前 p2 的 4 GiB 备份在 p3
  `/home/debian/daily-upload/p2-backup-20261028.ext2`。
- p1 `extlinux.conf` 默认 `asterinas-daily`；`kernel-daily.Image`
  SHA-256 `9d664d4b…`、`stage1-daily.cpio` SHA-256 `45477225…`、
  `megrez-daily.dtb` SHA-256 `465cb129…`。
- 串口：`ASTERINAS_DESKTOP_BOOT_WAIT reason=x11-socket` →
  `reason=xorg-fbdev` → `reason=openbox` →
  `ASTERINAS_DESKTOP_DISPLAY_READY` →
  `ASTERINAS_DESKTOP_FIREFOX_READY pid=151` →
  `ASTERINAS_DESKTOP_WATCHDOG_DISARMED` →
  `ASTERINAS_DESKTOP_BOOT_READY firefox_pid=151`。
- 桌面进程：Xorg :0（fbdev）、openbox、firefox-esr（about:blank，
  marionette）；vim 9.1、gcc 14.2.0、sudo 可用。
- guest 网络：eth0 10.100.19.200/21，默认路由 10.100.16.1；宿主可
  ping 板子。

已知问题（下一轮）：

- 镜像 `/etc/resolv.conf` 仍是构建容器的 `nameserver 10.0.2.3`，
  不是可用解析器；`/etc/hostname` 是构建机的 `ubuntu-MS-7D76`。
- 实验网网关 10.100.16.1 在宿主上也不可达（宿主默认走 wlan
  10.100.79.1），所以 guest 的直连外网不通。
- 宿主 proxy bridge（10.100.19.216:17893 → mihomo 7890；
  15354 → DNS）已能监听，宿主侧 `curl -x` 返回 200；guest 侧
  `curl -x` 尚未跑通，guest 的 TCP/console 需要单独排查。
- 旧 p2（只有 m4 单元）与新旧 stage1（期望 m5）都不兼容；p1 上
  997c known-good 项保留，但不再作为日常桌面。
