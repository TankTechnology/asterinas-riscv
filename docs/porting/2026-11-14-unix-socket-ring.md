# unix socket 的环缓冲从 64 KiB 提到 256 KiB：IPC 吞吐 1.6–1.8 倍（2026-11-14）

## 1. 问题

板子上两个进程之间传 16 MiB 数据，无论消息多大都只有 **90–108 MB/s**：

```
msg=  4096  end-to-end  87.9 MB/s
msg= 65536  end-to-end 107.5 MB/s
msg=262144  end-to-end  89.6 MB/s
```

而同一台机器上 memcpy 64 MiB 是 **5.1 GB/s**，1 字节 ping-pong 往返是 112 µs。
纯拷贝成本（非阻塞填满/排空）是 268–581 MB/s。也就是说，剩下的差距是**握手**：
每 64 KiB 数据就要经历一次"写者塞满环 → 阻塞 → 读者排空 → 唤醒写者"。

环的容量写死在 `UNIX_STREAM_DEFAULT_BUF_SIZE = 65536`，而且 **`SO_SNDBUF`/`SO_RCVBUF`
虽然存进了 socket 选项、`getsockopt` 也能读回来，连接建立时根本没人用它**——环永远是
64 KiB：

```
$ setsockopt(fd, SO_SNDBUF, 4 MiB)  → 成功
$ getsockopt(fd, SO_SNDBUF)         → 4194304
$ 实际能写入多少不阻塞             → 65536      ← 之前
                                     → 262144     ← 之后
```

## 2. 改动

- `UNIX_STREAM_DEFAULT_BUF_SIZE`：64 KiB → **256 KiB**（同时是 `SO_SNDBUF`/`SO_RCVBUF`
  的默认值，也是 `SOCK_SEQPACKET` 单条消息的上限，与 Linux 的语义一致）。
- 新增 `stream_ring_capacity(send_buf, recv_buf)`：取两个方向里较大的请求值，钳在
  8 KiB–8 MiB，向上取到 2 的幂（`RingBuffer` 要求）。
- `Connected::new_pair` 多一个 `ring_capacity` 参数；
  `Init::into_connected`（connect/accept 路径）由 `push_incoming` 传入——那是唯一还能
  看到连接方 `setsockopt` 的地方；`socketpair` 没有机会改选项，用默认值。
- `OptionSet::ring_capacity()` 读的就是 `SocketOptionSet` 里的 `send_buf`/`recv_buf`。

改完之后 `setsockopt(SO_SNDBUF)` 在**连接建立之前**生效；连接建立之后再改仍然只改
选项值，不重建环（重建要在有数据的情况下搬移，暂不做）。

## 3. 结果

### QEMU（TCG，同一 initramfs，只换内核）

| 消息大小 | 64 KiB 环 | 256 KiB 环 | 倍数 |
| ---: | ---: | ---: | ---: |
| 4 KiB | 9.2 MB/s | 27.8 MB/s | 3.0× |
| 64 KiB | 7.6 MB/s | 31.5 MB/s | 4.1× |
| 256 KiB | 7.8 MB/s | 40.9 MB/s | 5.2× |

### 板子（真机）

| 消息大小 | 64 KiB 环 | 256 KiB 环 | 倍数 |
| ---: | ---: | ---: | ---: |
| 4 KiB | 88 MB/s | 116 MB/s | 1.3× |
| 64 KiB | 108 MB/s | **179 MB/s** | 1.7× |
| 256 KiB | 90 MB/s | 157 MB/s | 1.7× |

未受影响：1 字节往返延迟（108–121 µs，前后同一量级）、X11 协议往返、桌面与 VNC 一切正常。

内存代价：一个连接两个环，各 256 KiB，即 **512 KiB/连接**；板上 113 个 unix socket
约 56 MB，`free` 看不到明显变化（10 GB 机器）。

## 4. 它没有解决什么

X11 `PutImage` 吞吐**没有变化**（13.8 → 13.6 ms/MB）。用 `x11perf` 分账后原因很清楚：
1 MB 的 `PutImage` 里 Xorg 自己烧掉约 10 ms CPU，客户端一直在阻塞——瓶颈在服务器的
请求解析与像素转换，不在 socket。详见 `2026-11-14-video-playback.md`。

这次改动真正惠及的是**大数据量 IPC**：scp/ssh 通道、VNC 抓屏回传、X11 里那些
`GetImage` 密集的客户端，以及任何往 unix socket 灌帧的程序。

## 5. 验证方式

```sh
# 一、选项是否真的落到了环上（板上跑，静态 riscv64 二进制）
/tmp/sock_copy 4          # 打印 "accepted=262144"（原来 65536）

# 二、端到端吞吐
for m in 4096 65536 262144; do /tmp/sock_stream $m 16; done

# 三、QEMU A/B：把常量改回 64 * 1024 重编，用同一个 initramfs 再跑一遍
qemu-system-riscv64 -machine virt -m 2G -smp 4 -nographic \
  -kernel target/osdk/aster-kernel-osdk-bin.Image -initrd /tmp/ipcbench.cpio.gz \
  -append "console=ttyS0 loglevel=warn init=/init"
```

`ipcbench` 的 init 就是 `/tmp/ipcbench.cpio.gz` 里的 `/init`：一个静态 C 程序，跑
capacity / ping-pong / streaming 三组，打完 `BENCH done` 直接 `poweroff`。

真机部署走既有闸门：`tools/riscv/board/preflight_kernel.sh` → `deploy_kernel.sh`，
回滚镜像是 `/root/kernel-rel.img.bak`。本次已在板上验证（重启 14 s 回来，
`sha256` 与部署值一致，桌面、VNC 正常）。

## 6. 遗留：这个分支上 ktest 编译不过（与本次改动无关）

给 `stream_ring_capacity` 加了单元测试，但它跑不起来：
`kernel/src/device/sysfs.rs` 里 6 处 ktest 仍在调用已经不存在的 `build_dev_node` /
`build_class_node`（`HEAD` 里就是这样，是之前 DRM sysfs 重构留下的），
`cargo osdk test` 在编译期就失败。本次改动的调用点已按新签名修好（`connected.rs`
测试里的 `new_pair()` 补了第七个参数），等 sysfs 那 6 处修掉，整条 ktest 就能重新跑。

在此之前，环容量的行为靠实测确认：`/tmp/sock_copy 4` 打印的
`accepted=262144` 正是 `stream_ring_capacity(256K, 256K)` 的返回值。
