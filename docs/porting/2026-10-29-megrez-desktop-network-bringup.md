# Megrez 桌面：联网络与壁纸的两条断点

日期：2026-10-29
基线：`727878c26`（本地 `main`），板上 release 内核 `/root/kernel-rel.img`

本轮要解决两件事：桌面看不到背景、Firefox 打开 www.baidu.com 报
「Server Not Found」。结论：**浏览器那条是三个独立故障叠在一起，已修好并可复现；
壁纸那条定位到 gdk-pixbuf 取图失败，但还没有修**。

## 1. 浏览器：三个故障

### 1.1 DNS 指向一台不回答的服务器

镜像里 `/etc/resolv.conf` 只有 `nameserver 58.206.232.1`（校园网关），实测
UDP:53 到它 **3001 ms 超时**；而 `114.114.114.114`、`223.5.5.5` 都是 5–10 ms
正常。Firefox 的「Server Not Found」就是它。

修法：`/etc/resolv.conf` 改成 `114.114.114.114` / `223.5.5.5`（把 58.206.232.1
留在最后作兜底）。

### 1.2 板子时钟比真实时间慢 6 个月

板上 `date` 是 `Mon Apr 13 2026`，真实是 10 月 29 日。所有真实站点证书都变成
「not yet valid」，curl 报 `SSL certificate problem: certificate is not yet valid`，
Firefox 则是一张连不上/不安全页。

修法：`date -s @<host epoch>`（RTC 是错的，每次开机都要重新对时）。

### 1.3 校园网口未认证时 TLS 单播被拦

未认证时：DHCP 正常、HTTP 到任意地址被拦成门户跳转页、
**TCP 连接"成功"但 1 ms 就返回（本地拦截）**、DNS 全灭、出网 TCP 超时。
认证后一切正常：`curl https://www.baidu.com/` 直接 **200**，不需要代理。

认证（深澜 srun）在**只能拿到门户 IP、DNS 不可用**时，先给主机名兜底：

```sh
echo '58.206.248.10 auth.zgclab.edu.cn' >> /etc/hosts
python3 /root/srun.py <user> <pass>     # 凭据只作运行时参数，不落盘
```

返回 `"suc_msg":"login_ok"` 即成功；若返回 `ip_already_online_error` 说明门户
记录还在但网络侧已失效（本次就是这种），重启后再登录即可。

**每次重启后都要重新登录**：重启会丢掉会话，之后 DNS 与出网全部失效。

### 1.4 附带发现：Firefox 的 profile 目录在磁盘上被写坏了

删除前 `~/.mozilla/asterinas-browser-web/` 里
`MarionetteActivePort` 是指向某个 systemd unit 的**符号链接**、
`activity-stream.shortcut_cache.json` 是**目录**、
`bounce-tracking-protection.sqlite` 里装着 **17 MB 的内核镜像**，
`rm -rf` 报 `user.js: Stale file handle`，`mv -T user.js` 也因此失败，
启动脚本直接退出。整个目录删掉重建后正常。

这是 ext2 写入路径的损坏（不是配置问题），值得单独查。

## 2. 壁纸：取不到图，不是配置问题

现状：pcmanfm 的桌面窗口是**全屏**的，`desktop_bg=#071426` 也画出来了，
但壁纸无论用 BMP 还是 PNG、`center` 还是 `pixel` 都不出现；`lxpanel`
的面板同样不出现。日志里能对上的是：

```
(lxpanel): WARNING: Could not load a pixbuf from icon theme.
           This may indicate that pixbuf loaders or the mime database could not be found.
(Firefox): Could not load a pixbuf from /org/gtk/libgtk/icons/16x16/actions/dialog-warning.png
```

即 **GTK 这条链路取不到任何图片**：壁纸、面板图标、浏览器图标一起失效。

板上 gdk-pixbuf 现状：

- `loaders/` 里只有 ani、bmp、gif、icns、ico、pnm、qtif、svg、tga、tiff、
  xbm、xpm —— **没有 png、没有 jpeg 模块**（这两个本应编进
  `libgdk_pixbuf` 的 builtin）；还有一个重复的 `libpixbufloader_svg.so`
  （下划线版），像是构建期前缀混了。
- `loaders.cache` 12 条，路径是对的（`/usr/lib/riscv64-linux-gnu/...`），
  但查不到 `"bmp"` 条目。
- `xwininfo` 里 `pcmanfm`/`lxpanel` 各有一个 10x10 的辅助窗口，容易误判成
  「桌面窗口只有 10x10」，实际桌面窗口是全屏的。

### 已取证（用 ctypes 直接调板上的 libgdk_pixbuf，不需要编译器）

`/root/pbtest.py` 用 `ctypes` 调 `gdk_pixbuf_new_from_file`：

| 文件 | 结果 |
| --- | --- |
| 一份新生成的、560 字节的 1920×1080 PNG | `Couldn't recognize the image file format` |
| `/usr/share/asterinas/desktop-wallpaper.bmp` | 同上 |
| `libpixbufloader-bmp.so` 直接 `ctypes.CDLL` | **能加载**（模块本身没问题） |

同时排除的路：

- 文件读取：`open().read()` 和 `mmap` 读前 8 KiB 都正常（`BM` 魔数正确）。
- 镜像损坏：`dpkg -V libgdk-pixbuf-2.0-0` 无任何差异（VRC=0）；该包只带 11 个
  模块 .so，**png/jpeg 是 builtin**（包里没有对应 .so）。
- loaders.cache：内容与路径都对（`"bmp" 5 "gdk-pixbuf" "BMP" ...`），
  `GDK_PIXBUF_MODULE_FILE` / `GDK_PIXBUF_MODULEDIR` 两种覆盖都试过，仍然失败。

结论：**这台机器上 gdk-pixbuf 连 builtin 的 PNG 都认不出来**，而包文件是完好的。
下一步把它搬进 QEMU 用同一套 rootfs 复现：如果 QEMU 里正常，就说明问题在板子的
内核侧（例如页面错误处理，我们在日志里见过
`page fault handler failed ... errno: EACCES`）；如果 QEMU 里也不正常，就能本地
`strace/ltrace` 直接定位。

## 3. 每次开机把板子弄上网的最短路径

```sh
# U-Boot：用带地址的 bootargs 启动
ext4load mmc 1:1 0x88300000 stage1-daily.cpio; setenv initrd_size ${filesize}
ext4load mmc 1:1 0x88000000 megrez-daily.dtb
ext4load mmc 1:2 0x80200000 /root/kernel-rel.img
setenv bootargs console=ttyS0 loglevel=warn init=/init asterinas.mmc_write_partition2 \
asterinas.net=eic7700-rj45,58.206.232.122/24,58.206.232.1 -- --root-init=systemd \
--debug-console=isolated-root
booti 0x80200000 0x88300000:${initrd_size} 0x88000000

# guest：对时 → 登录门户 →（可选）起代理
date -s @<epoch>; python3 /root/srun.py <user> <pass>
```

`asterinas.net` 这条必须自己给：`/extlinux/extlinux.conf` 的条目带的是旧实验室
网络 `10.100.19.200/21`，按默认条目启动会拿到一个永远不通的地址。

## 4. QEMU 对照：同样的二进制在 QEMU 上正常

把板上的 release 内核（sha256 `03536979…`）、板上的 stage1（`stage1-qemu.cpio`）
和一份 Debian 桌面 rootfs 放进 QEMU virt（bochs-display + U-Boot 注入
simple-framebuffer），桌面完全正常：

```
DEBIAN_DESKTOP_M4_SHELL wallpaper=asterinas desktop=pcmanfm panel=lxpanel launchers=3
DEBIAN_DESKTOP_M4_CLIENTS window-manager=openbox file-manager=pcmanfm browser=netsurf terminal=xterm
DEBIAN_DESKTOP_M4_READY user=asterinas display=:0
```

截图里壁纸、lxpanel 面板、桌面图标、xterm、NetSurf 全在。

关键对照（同一台主机上取的文件哈希）：

| 文件 | QEMU 镜像 | 板子 |
| --- | --- | --- |
| `libgdk_pixbuf-2.0.so.0.4200.12` | `61c7d66b…` | `61c7d66b…` 相同 |
| `loaders.cache` | `80971cf3…` | `80971cf3…` 相同 |
| `libpixbufloader-bmp.so` | `b65b37a4…` | `b65b37a4…` 相同 |
| `libz.so.1.3.1` | `5ac32970…` | `5ac32970…` 相同 |
| `libglib-2.0.so.0.8400.4` | `c4564b54…` | `c4564b54…` 相同 |
| `libpng16.so.16.48.0` | `d5578731…` | `76505526…` **不同** |

板上 gdk-pixbuf 的自检（`tools/riscv/diagnostics/gdk_pixbuf_probe.py`）：

- `gdk_pixbuf_get_formats()` 返回 **14 个格式**（png jpeg ani bmp gif icns ico
  pnm qtif tga tiff xbm xpm svg）——loader 注册是成功的；
- `gdk_pixbuf_new_from_file()` 对 PNG / BMP / **ASCII P3 PNM** 一律
  `Couldn't recognize the image file format`；
- 把同样的字节喂给 `GdkPixbufLoader`：`write` 成功、`close` 报
  `Unrecognized image file format`——即没有任何 loader 认领这份数据；
- 排除项：文件内容（sha256 与主机一致、PNG 魔数正确）、tmpfs 与 ext2 都失败、
  zlib 往返正常、`g_module_open` 成功、`gdk_pixbuf_new()` 分配正常；
- QEMU 用 `-cpu rv64,…,zba=true,zbb=true` 复跑，壁纸照常渲染 → **cpu 扩展不是原因**。

结论方向一度指向平台差异，最后发现问题出在**共享 MIME 数据库缺失**。

### 4.1 根因：板上没有 `/usr/share/mime/mime.cache`

gdk-pixbuf 用 GLib 的 `g_content_type_guess()`（shared-mime-info 数据库）决定
用哪个 loader。板子的 `/usr/share/mime/` 里只有一个 `packages` 目录，
**`mime.cache` 不存在**；QEMU 那份镜像里是 158,788 字节的完整缓存。

lxpanel 的告警其实早就明说了：
```
Could not load a pixbuf from icon theme.
This may indicate that pixbuf loaders or the mime database could not be found.
```
没有 mime 数据库时，任何格式（PNG/BMP、连纯 ASCII 的 P3 PNM）都会走到
`Couldn't recognize the image file format`——这与「库、模块、文件全部完好」的
观察完全吻合。

修复与验证（板上直接执行）：

```sh
update-mime-database /usr/share/mime     # 生成 158,788 字节的 mime.cache
```

- `python3 gdk_pixbuf_probe.py /root/red.png` → `PIXBUF_OK … 1920 1080`；
- 重启 pcmanfm 后**壁纸立刻出现**（帧缓冲抓图确认，1920×1080 全屏），
  lxpanel 面板也渲染出来了（顶部，白底待调色）。

仓库修复：`build_rootfs.sh` 里这段
`update-mime-database` 原来只对 `browser-m5|browser-web` 跑，现在对所有
`desktop-*` / `browser-*` profile 都跑（新增 `is_desktop_profile`），并且注释里
写清了这个教训。

## 5. 控制台被二进制流挂死之后

推内核时用「`stty raw -echo; head -c N > file` + 桥接器 RAW」失败（`stty raw`
没在负载前生效），二进制里的控制字符把前台读者挂住，之后**输入只回显、不执行**。
试过并全部无效：`^C`、`^D`（VEOF）、UART break（只回 `^@`，说明 BRKINT 关）、
DTR 掉线、RTS/DTR 复位序列、`reboot` 配四种行结束符、以及按 11 KiB/s 和
2 KB/s 灌几 MB 填充字节。唯一出路是断电重启。

两条固化下来的措施：

1. `tools/riscv/megrez_recover.py`：断电后一条命令恢复
   （等 U-Boot → 带 `asterinas.net=` 启动 → 对时 → 门户登录 → 检查桌面/DNS/HTTPS/壁纸）。
2. 引导参数建议加 `asterinas.reboot_after=<秒>`（内核 `kernel/src/boot_reboot.rs`），
   让卡死的板子自己重启；大文件一律走网络，串口只传文本脚本。

## 5.5 开机就报「Firefox profile 无法加载」还有第二个原因：缺 profiles.ini

修好只读根分区之后，Firefox 进程起来了、`prefs.js` 也写了，但屏幕上仍然是一个
`Profile Missing` 窗口。用 `xwininfo -root -tree` 才看见它：

```
0xa00001 "Firefox": ("firefox-esr" "Firefox-esr")  10x10+10+10
  0xa00002 "Profile Missing": ("Firefox" "firefox-esr")  561x105+1+20
```

判据要**看窗口**，不能只看进程在不在——`ps` 里有 `firefox-esr`、
`firefox-web-stderr.log` 里也干干净净（这条路径不写日志）。

真因：`~/.mozilla/firefox/profiles.ini` 不存在。Firefox **即使拿到
`--profile <绝对路径>`，也要通过 profile 注册表解析**，注册表缺失就直接弹
「profile cannot be loaded」，哪怕目录、权限、`prefs.js` 全都正常（当成 uid 1000
往目录里写文件是成功的，headless 启动也是成功的——只有 GUI 这条路会查注册表）。
这个目录是早先清理坏掉的 profile 时连同 `.mozilla/firefox` 一起删掉的。

修法：把注册表指回同一个绝对路径，`IsRelative=0`。

```ini
[General]
StartWithLastProfile=1
Version=2

[Profile0]
Name=default
IsRelative=0
Path=/home/asterinas/.mozilla/asterinas-browser-web
Default=1
```

板上验证：写入后 `Profile Missing` 窗口消失，出现 1280x972 的
`Mozilla Firefox` 窗口；把服务 `systemctl stop/start asterinas-browser-web`
之后同样正常。镜像里已经固化（`build_rootfs.sh` 生成该文件，
`browser_web_firefox.sh` 每次启动前重写一遍，profile 被删也能自愈）。

## 6. 开机就报「Firefox profile 无法加载」= 根分区是只读的

现象：直接上电（走 p1 上原厂的 `extlinux.conf` → `bootcmd_rockos`），桌面起来后
Firefox 弹 `Your Firefox profile cannot be loaded. It may be missing or
inaccessible.`。

根因不在 Firefox，也不在 profile：那条 `extlinux.conf` 的 `append` 是实验室时期的
旧参数，**既没有 `asterinas.mmc_write_partition2`，也带着过期的 `10.100.x` 地址**。
少了写门参数，内核把 p2 挂成只读，Firefox 建不了/写不了 profile 目录，于是报
「inaccessible」；同一个原因还会让关机卡死（见 `mmc-write-partition2-gate`）。
用带该参数的方式引导后，Firefox 正常起进程、profile 目录内容完整可写。

修复不能改 `extlinux.conf`：p1 在板上**既写不了也读不了**（内核写门只放行 p2，
`mount -t ext4` 连 `-o ro` 都报 bad superblock）。所以把整条引导序列放进 U-Boot
环境变量（UTF-8 文本，`saveenv` 落到 SPIFlash）：

```
setenv bootcmd 'ext4load mmc 1:1 0x88300000 stage1-daily.cpio; \
setenv initrd_size ${filesize}; \
ext4load mmc 1:1 0x88000000 megrez-daily.dtb; \
ext4load mmc 1:2 0x80200000 /root/kernel-rel.img; \
setenv bootargs console=ttyS0 loglevel=warn init=/init asterinas.mmc_write_partition2 \
asterinas.net=eic7700-rj45,58.206.232.122/24,58.206.232.1 \
-- --root-init=systemd --debug-console=isolated-root; \
booti 0x80200000 0x88300000:${initrd_size} 0x88000000'
saveenv
```

这样每次上电都自带 `asterinas.mmc_write_partition2` 和
`asterinas.net=eic7700-rj45,58.206.232.122/24,58.206.232.1`，根分区 `rw`，
桌面和 Firefox 不再需要主机介入。回滚：

```
setenv bootcmd 'run bootcmd_asterinas || run bootcmd_rockos || run bootcmd_factory'
saveenv
```

**坑（花了一次引导才弄清）**：U-Boot 的 `source` 只吃 **mkimage 包过头的脚本镜像**，
直接把文本文件 `ext4load` 进来再 `source` 会报
`Wrong image format for "source" command`；而 `ext4load` 本身是成功的（`&&` 照样
往下走），于是链条落到 `|| run bootcmd_rockos`，静默走回 extlinux 的老参数 ——
看起来像"新配置没生效"。要用文件形式，必须先在主机上
`mkimage -A riscv -O linux -T script -C none -d megrez_boot_cmd.txt boot.scr`
再把 `boot.scr` 推到 p2。

注意 U-Boot 的自动引导倒计时（30 秒）期间串口要留出被打断的余量；用
`megrez_console.py` 逐字符打字时，倒计时归零前可能只打进去半个命令，
需要重新 `reset` 再抓一次。


## 7. 跑久了「有链路、收得到、发不出去」：内核发送路径卡死

现象（2026-10-29，机器连续运行约 47 分钟后）：

| 探针 | 卡住时 | 重启后同一探针 |
| --- | --- | --- |
| `curl http://58.206.232.1/`（网关） | `000` 超时 | `200` |
| `curl http://58.206.248.10/`（门户） | `000` | `302` |
| `ip neigh` | 始终为空 | 有解析 |

一开始怀疑校园网把端口隔离了，用两个决定性实验排除掉了：

1. **U-Boot 自己的 DHCP**（`setenv autoload no; dhcp`）：
   `DHCP client bound to address 58.206.232.122 (1306 ms)` ——
   网线、交换机、DHCP 全部正常，而且租约给的正是我们用的地址，
   所以既不是端口隔离也不是 DHCP snooping。
2. **收发计数**：`/proc/net/dev` 里 eth0 的 rx 一直在涨（约 10 包/秒的广播流量），
   tx 在没有任何发送时**完全不动**；而一旦触发发送，计数会暴涨
   （发 50 个 UDP 包 tx 涨 3784、发 3 个 DHCP DISCOVER tx 涨 1272），
   这是重传风暴而不是正常的 1:1 计数。

结论：**入向正常、出向在内核里卡住**（ARP 请求发出去收不到应答只是表象）。
这和我们记过的 `browser-m8-kernel-flaky-net`（`can_send()` 门控、poll 调度器
哨兵撞车）属于同一类问题，但这次出现在 dwmac 而不是 virtio-net 上，尚未定位到
具体代码。目前的恢复手段是重启；`kernel/comps/dwmac/src/device.rs` 里已经有
`ASTERINAS_GMAC_HW_STATE`（MAC/MTL/DMA 状态机快照，debug 级）可以拿来对拍，
下次复现时用 `loglevel=debug` 起一次即可拿到卡住瞬间的寄存器状态。

## 8. 串口长命令会被截断，进而把板子启动参数弄坏

`setenv bootargs console=ttyS0 loglevel=warn init=/init asterinas.mmc_write_partition2
asterinas.net=… -- --root-init=systemd --debug-console=isolated-root` 这一行约 200 字符，
逐字符打字时如果脚本的等待时间不够（当时只等了 6 秒），后半截被下一条命令覆盖，
`bootargs` 变成残缺版本。用残缺参数引导 `kernel-sig.img` 的结果是：

```
[     3.180] ERROR : Uncaught panic:
    Failed to run the init process: Error { errno: ENOENT, msg: Some("found a negative dentry") }
```

stage1 找不到 init（`-- --root-init=systemd` 丢了），panic 后内核不再读串口，
**只能物理复位**。教训：

- 长命令行按「字符数 × 0.05 秒 + 10 秒」留等待，或者干脆不要手打长行；
- 换内核做 A/B 时**不要改 U-Boot 参数**：保存的 `bootcmd` 只认
  `/root/kernel-rel.img`，直接用 `cp` 把候选内核换到这个路径、`sync; reboot`
  即可，测完再 `cp` 回来。这样每轮只有两条短命令。

## 9. 这次网络故障的最终取证（2026-10-29 晚）

白天两次「断网」的结论在晚间被推翻了，记录一下，避免下次再走弯路：

| 观察 | 断网时 | 说明 |
| --- | --- | --- |
| rx 与 tx 计数 | 20 s 内 rx +1689、tx +585 | 驱动与链路都没死，"发不出去"是误判 |
| 网关 ARP / DHCP / HTTP | 全部无应答 | 端口侧不再回包 |
| 发 10 个广播报文的 tx 帧数 | **256** | 发送放大 ~25.6 帧/报文 |
| 健康时 50 个广播的 tx 帧数 | 126 | 健康时也 >1（约 2.5 帧/报文） |

另外用 **U-Boot 自己的 DHCP** 做了对照：`DHCP client bound to address
58.206.232.122 (1306 ms)`，说明网口、交换机、DHCP 在引导阶段都是好的，重启后
内核侧也立刻恢复（`GATEWAY=200`、`PORTAL=302`）。

所以现在的判断是：**发送放大触发交换机端口保护**（风暴控制/端口安全），端口被
掐掉后就表现为"广播还能收到、我们发的一切都没人应答"，而重启让端口重新被授权。
真机侧的对策已经落地：

- 驱动里的发送环停滞探测（`ASTERINAS_GMAC_TX_STALL`，warn 级，带 DMA 状态并尝试
  rearm 自愈）；
- `asterinas-net-watchdog.service` 把"端口失效"变成带计数的日志事件；
- 引导阶段加 `asterinas.reboot_after=900`，卡在早期单元启动时板子自己重启。

**还没解释清楚的是放大本身**（25.6 帧/报文）。下一次复现时 `ASTERINAS_GMAC_TX_STALL`
的 `submitted/reclaimed/outstanding/dma_status` 会指出是环没有被回收还是 MAC 在重传。

## 10. 网络「跑一阵就坏」的真因：PHY 从没协商过（2026-10-30）

前面 §7 的结论（发送放大触发端口保护）是错的。用两个实验推翻了它：

| 引导方式 | `GW=` | 说明 |
| --- | ---: | --- |
| 直接走保存的 U-Boot `bootcmd` | 000 | 收得到广播，发出去没人认 |
| 先在 U-Boot 里 `dhcp`，再引导内核 | **200** | 网络完全正常 |

U-Boot 的 `dhcp` 会打印 `Waiting for PHY auto negotiation to complete..... done`，
而我们的 dwmac 驱动（`kernel/comps/dwmac/src/phy.rs`）**只读** `BMCR`/`BMSR`，
既不启动自动协商，也不清 `POWER_DOWN`/`ISOLATE` 位。冷启动时 PHY 可能仍处于
掉电或隔离状态，驱动读到一个不可用的链路就把 MAC 按错误的速率/双工配好了：
入向能收到（对端发来的帧照样进 FIFO），出向的帧对端不认——于是 ARP 无应答、
DHCP 无 OFFER、TCP 超时，而 `/proc/net/dev` 的 rx/tx 仍在涨。

「跑一阵就坏、重启就好」也是同一个原因：重启后如果恰好经过 U-Boot 的网络
（或 PHY 自己协商上了）就好，否则就是坏的。

两处修复：

1. **引导串里加 `dhcp;`**（`tools/riscv/megrez_boot_cmd.txt` 与板上 U-Boot 环境
   都已写入并 `saveenv` 到 SPI flash）。现在普通上电就会先协商 PHY，实测
   `DHCP client bound to address 58.206.232.122 (291 ms)` → 内核里 `GW=200`。
2. **驱动里补上协商**：新增 `start_auto_negotiation()`（置
   `ANENABLE|ANRESTART`、清 `POWER_DOWN|ISOLATE`），在端口选择的第一轮轮询前
   调用一次。装上它之后就不再依赖 U-Boot 的那一步。

### 10.1 补上协商还不够：窗口只有 3 秒

第一版驱动修复装上后，**冷启动仍然没有以太网**，而且连「U-Boot 先 `dhcp`」
那条路也一起坏了：`start_auto_negotiation()` 会重启协商，链路因此先掉下去约
3 秒，而 `select_linked_port()` 的窗口只有 3 秒（`MAX_LINK_SELECTION_NS`），
链路还没回来就判了 `NoLink`，驱动于是干脆不注册网卡。

决定性证据是内核初始化耗时：

| 引导方式 | `Startup finished in` 里的 kernel 段 |
| --- | ---: |
| U-Boot 已协商好（内核直接拿到链路） | 1.126 s |
| 不跑 `dhcp`，内核自己发起协商 | 4.661 s |

多出来的 3.5 s 正是 RTL8211F 从 restart 到链路可用的时间。三处改动：

- 窗口 3 s → 10 s（选端口正常时第一轮就返回，只有冷启动才真的等）；
- 超时时把两个端口的 `BMCR`/`BMSR` 读回来打 `warn`：没有网卡时日志是唯一线索，
  现在能直接看出是「协商没启动」还是「协商没完成」；
- 已经拿到可用链路的端口不再重启协商（否则每次走 `dhcp` 的引导都白掉 3 秒）。

改完后在**不跑 U-Boot `dhcp`** 的条件下验证通过：`eth0` 拿到
`58.206.232.122/24`、默认路由正常、网关 `GW=404`（网关的 HTTP 应答）、
宿主机文件服务 `HOST=200`。`bootcmd` 里保留 `dhcp;` 只是省掉那 3.5 秒，
不再是网络能不能用的前提。
