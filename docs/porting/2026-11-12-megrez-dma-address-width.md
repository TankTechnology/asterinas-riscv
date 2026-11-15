# 2026-11-12 板载网卡「全网不通」的根因：DMA 地址宽度被 ring 地址决定

## 症状

同一块 Megrez 板、同一份 `dwmac` 驱动与网络栈（与已知可用的内核逐字相同），
换上新内核后：

- 控制台、桌面、systemd 全部正常，`ip addr` 有地址、网关与静态邻居都对；
- `ping` 网关、门户、公网 **100% 丢包**，`curl` 到门户 TCP 直接超时；
- `U-Boot` 在同一时刻 `dhcp` 成功、`ping` 主机成功、`tftp` 6.7 MB 成功。

也就是说：链路与交换机没问题，**只有内核跑起来之后才不通**。

## 定位过程（直接测量，不做二分）

1. 打开 driver 里已有的 `ASTERINAS_GMAC_*` 统计（`loglevel` 提到 info / 用
   `warn!` 输出），确认 **TX 描述符会被硬件消费**（`consumed=true`、
   `tx_reclaimed` 单调增长），排除「MAC 卡住不取描述符」。
2. 打出一帧 TX 的头部：`dst=ff:ff:ff:ff:ff:ff, src=00:48:54:71:00:47,
   ethertype=0x0806`，ARP 字段（`00 01 08 00 06 04 00 01`）完全正确 ——
   CPU 侧组帧没问题。
3. 打出一帧 RX 的 16 字节：`alias` 与 `cached` 两条路径读到的都是 **全 0**，
   而描述符报的长度是 64。设备写进去的数据根本不在 CPU 能看到的地址上。
4. 对照 `ASTERINAS_GMAC_DMA_CONTRACT` 的 ring 地址：不通的那几次
   `ring_paddr=0xdff1a000`（**< 4 GiB**），能收到真实帧的那几次
   `ring_paddr=0x2ff52a000`（> 4 GiB）。

## 根因

`dma_system_bus_mode(tx_high, rx_high)` 只在**描述符环本身**高于 4 GiB 时才
置 `DMA_SYSTEM_BUS_MODE.EAME`（bit 11，扩展地址模式）；而描述符里携带的是
**每一个帧缓冲区**的 64 位地址，缓冲区可以落在 16 GiB DRAM 的任何位置。

于是：ring 恰好被分配到 4 GiB 以下时，EAME 关闭，MAC 把 TDES1/RDES1 的高
32 位丢掉，收发都指向了错误的物理地址——TX 发出去的是垃圾、RX 写到了别处，
但描述符照样 OWN 清零、照样报长度，所以上层看到的是「网卡正常、网络不通」。

内存分配位置由帧分配器的行为决定，因此**换了分配相关的提交之后才开始复现**，
看上去像「性能提交把网络搞坏了」，实际是一条一直存在的地址宽度 bug。

## 修复

无论 ring 落在哪里，都启用扩展地址模式：

```rust
pub const fn dma_system_bus_mode() -> u32 {
    DMA_ADDRESS_ALIGNED_BEATS
        | DMA_AXI_WRITE_LIMIT_2
        | DMA_AXI_READ_LIMIT_2
        | DMA_AXI_BURSTS_16_8_4
        | DMA_EXTENDED_ADDRESS_MODE
}
```

`kernel/comps/dwmac/src/regs.rs` + 一处调用点，ktest
`dma_always_uses_extended_buffer_addresses` 锁住「永远置位」。

## 验证（真机，2026-11-12）

| 项目 | 结果 |
| --- | --- |
| `ping 58.206.232.1`（网关） | 3/3，0.71/0.76/0.84 ms |
| `ping 58.206.198.47`（主机） | 3/3，3.1/3.4/3.6 ms |
| `curl http://<门户>/` | 302（未认证时门户拦截，符合预期） |
| 从主机 `curl` 20 MB | 200，9.83 s，**2.03 MB/s**（与门户限速一致） |
| RX/TX 统计 | RX 19074 帧，TX 512 帧，`rx_buffer_unavailable=5` |

顺便确认：`RX alias 视图 == cached 视图`，说明 EIC7700 的 uncached 平台别名
窗口本身工作正常（25 次 `ASTERINAS_DMA_ALIAS_SELFTEST ok=true`），此前怀疑的
别名/缓存一致性问题可以排除。

## 顺手修掉的两件板级问题

1. **每 900 s 自动重启**：`asterinas-watchdog-disarm.service` 只被
   `basic.target` 拉起，而该内核上 `basic.target` 不会 active，于是 disarm 永远
   不执行，`/proc/sys/kernel/asterinas_reboot_watchdog` 一直是 1。现在同时挂到
   `sysinit.target.wants/`（`Wants=` 不引入顺序环），`build_rootfs.sh` 已同步。
2. **根分区被写满导致的 `Stale file handle`**：`/dev/mmcblk0p2` 一度 100%，
   Firefox 的 `profiles.ini` 等目录项损坏。清掉 `/root` 里十余份历史内核镜像
   （约 120 MB）并把损坏的 `~/.mozilla` 整体改名后，浏览器可正常重建 profile。
