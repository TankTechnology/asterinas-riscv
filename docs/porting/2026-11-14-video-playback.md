# 板子上放视频：解码够用，瓶颈在小海思没有的共享内存路径（2026-11-14）

## 1. 一句话结论

软件解码不是瓶颈——1080p 能解 31 fps、720p 能解 79 fps。**瓶颈是显示路径**：
mpv 的 `x11` 输出不使用 MIT-SHM，一帧 1080p 要经 X server 的 `PutImage` 走一遍
约 110 ms；换成 `XShmPutImage` 同一帧只要 0.3 ms。用自带的 `tools/riscv/apps/xshm_play.c`
走共享内存后，**720p 原生分辨率 33 fps（流畅），1080p 18 fps，480p 63 fps**。

## 2. 实测数据（板子，1920×1080 framebuffer，EIC7700）

### 2.1 解码

180 帧的全部时间（`ffmpeg -f null`）：

| 素材 | 耗时 | 每帧 |
| --- | ---: | ---: |
| 1080p | 5.79 s | 32 ms |
| 1080p + 转 RGB24 | 8.53 s | 47 ms |

720p / 480p 早前测到解码 79 fps / 152 fps。**结论：解码有余量，转换也不贵。**

### 2.2 各种显示路径（同一个 1080p 素材，60 帧）

| 路径 | 耗时 | 每帧 | fps |
| --- | ---: | ---: | ---: |
| `mpv --vo=null`（只解码+转换，丢弃） | 3.74 s | 62 ms | 16.0 |
| `mpv --vo=x11 --fs` | 11.16 s | 186 ms | **5.4** |
| `mpv --vo=x11 --geometry=640x360` | 9.02 s | 150 ms | 6.7 |
| `xshm_play`（拉伸到全屏） | 3.4 s | 56 ms | 17.9 |
| `xshm_play --no-scale` 1080p | 3.4 s | 55 ms | 18 |
| `xshm_play --no-scale` 720p | 1.8 s | 30 ms | **33** |
| `xshm_play --no-scale` 480p | 0.9 s | 16 ms | **63** |
| `ffmpeg -f fbdev /dev/fb0` 1080p | 9.0 s | 50 ms | 20 |
| `ffmpeg -f fbdev /dev/fb0` 720p | 5.0 s | 28 ms | 36 |
| `ffmpeg -f fbdev /dev/fb0` 480p | 2.8 s | 16 ms | 64 |

窗口大小几乎不影响 mpv 的成绩（640×360 和全屏一样慢），因为它的 x11 输出总是先在
源分辨率上做 YUV→RGB 转换，再缩放。

### 2.3 为什么是 X server

`x11perf` 直接量 X 服务器（同一台机器，同一块屏）：

| 操作 | 每次 | 折算 |
| --- | ---: | ---: |
| `PutImage 500x500`（1 MB） | 9.3 – 13.8 ms | 72 – 107 MB/s |
| `ShmPutImage 500x500` | 0.35 – 0.47 ms | **2.2 – 2.9 GB/s** |
| `GetImage 100x100`（40 KB） | 0.85 ms | 47 MB/s |
| `NoOperation`（往返） | 0.2 µs | — |

`PutImage` 那一次，Xorg 进程在 42.7 s 的墙钟里烧了 41.6 s CPU——**瓶颈在服务器端**
解析并转换客户端发来的图像，不在 socket、也不在 framebuffer。

旁证：`/dev/fb0` 写入 558 MB/s（8.3 MB / 14.8 ms），`memcpy` 64 MiB 5.1 GB/s，
unix socket 约 180 MB/s（见 `2026-11-14-unix-socket-ring.md`）。把这些都换成无限快，
`PutImage` 仍然只有 100 MB/s。

### 2.4 mpv 为什么用不上共享内存

- mpv 的 `--vo=x11` 不调用 `XShmPutImage`：`/proc/<mpv>/maps` 里没有 SysV 共享内存段，
  `ipcs -m` 也是空的；它只链接了 `XShm*` 符号（`vo_xv` 用的，而这块 X 没有 Xv）。
- `--vo=xv` 报 `Error opening/initializing the selected video_out`（无 Xv 扩展）。
- `--vo=sdl` 报 `No supported renderer`；`--vo=gpu/gl` 需要 DRI，板上没有 `/dev/dri`。

所以**在不改 mpv 的前提下，x11 路线封顶就是 5–7 fps**。

## 3. 交付：`xshm_play`

`tools/riscv/apps/xshm_play.c`——150 行左右的播放器，libav 解码、libswscale 直接写进
XShm 段、`XShmPutImage` 交给服务器。板上构建：

```sh
apt-get install -y libx11-dev libxext-dev libavformat-dev libavcodec-dev \
                   libavutil-dev libswscale-dev
gcc -O2 -I/usr/include/riscv64-linux-gnu -o xshm_play xshm_play.c \
    -lX11 -lXext -lavformat -lavcodec -lswscale -lavutil
DISPLAY=:0 ./xshm_play /root/test-720p.mp4 --loop --no-scale
```

它每 2 秒打印一行 `frames/fps/decode/scale/put`，各阶段成本可以直接归因：

```
frames=69 fps=34.2 decode=0.0ms scale=6.6ms put=0.30ms     ← 720p 原生分辨率
frames=37 fps=18.1 decode=0.0ms scale=14.6ms put=0.30ms    ← 1080p
```

`put` 从 110 ms 掉到 0.3 ms，是这条链上唯一一处数量级的变化。

验收：播放 720p 时用 `xwd -root` 抓屏，画面就是视频本身（彩条 + 内嵌时间码在走）。

> 注意 `decode` 一列接近 0 是测量口径问题：libavcodec 的帧级线程在工作线程里解码，
> 时间花在 `receive_frame` 的等待上，只有 `scale` 和 `put` 是同步的。真实解码成本用
> `ffmpeg -f null` 的 32 ms/帧（1080p）作准。

## 4. 还能往哪走

| 方向 | 收益 | 代价 |
| --- | --- | --- |
| 全屏时用更快的缩放器（`SWS_POINT`） | 480p/720p 拉伸从 27/34 ms 降到个位数 | 画面发虚 |
| 加黑边而不是拉伸（保持宽高比） | 720p 全屏仍走 `--no-scale` 的时间 | 上下留黑边 |
| 解码多线程调优 / 只解 I/P 帧 | 1080p 从 18 fps 往上 | 需要改 libav 参数 |
| 把 `PutImage` 快路径做进 X server | 对所有非 SHM 客户端都有用 | 改的是 Debian 的 Xorg，不是内核 |

**1080p 的天花板是解码（32 ms/帧 = 31 fps）**，显示路径已经不是限制了。

## 5. 操作提示

- 放视频会盖住整个桌面（override-redirect 全屏窗口），`Ctrl-C` 或 `kill` 退出。
- 素材放在 `/root/test-{480p,720p,1080p}.mp4`。
- 板上 gcc 曾因 `libgcc-14-dev` 文件丢失而无法链接（`crtbeginS.o` 缺失、
  `/usr/lib/gcc/riscv64-linux-gnu` 整个目录不见了）；`dpkg -i` 重装该包即修复，
  `apt-get --reinstall` 反而报错。
