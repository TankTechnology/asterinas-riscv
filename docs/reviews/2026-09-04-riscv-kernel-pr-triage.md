# RISC-V 内核与 Firefox PR 整理

日期：2026-09-04

## 结论

当前 `codex/firefox-startup-compat` 已同步 `origin/main`，并吸收
`#134 Preserve sigaction metadata after SA_RESETHAND`。Firefox/browser-web
构建使用的是更新后的 schema 7 provenance 流程，因此旧的 schema 6/
`browser-m5` 堆叠 PR 不再直接合入。

## 已关闭入口

| PR | 原因 |
|---|---|
| #78 | VirtIO 键盘 gate 已被 PCI xHCI 键盘/鼠标链路取代 |
| #84, #86 | 安全来源校验和 Firefox rootfs builder 已重写到 browser-web schema 7 |
| #105–#125 | 实现或测试已由 `origin/main` 以等价提交吸收，旧堆叠基线失效 |

关闭只影响 PR 入口，不删除远程分支或提交。后续若发现未覆盖的增量，
从当前主线重新开一个小 PR。

## 保留入口

| PR | 处理建议 | 对 Firefox/网络的关系 |
|---|---|---|
| #134 | 已摘入当前分支，验证后进入统一主线 | 信号元数据语义直接影响复杂用户态程序 |
| #103 | 保留，等浏览器网络基线稳定后 rebase | SMP affinity 对多进程/多线程有间接帮助 |

## 当前 browser-web 安全能力

当前分支已包含：

- Debian base/security signed source 校验；
- Packages 索引与下载包的 source-role 绑定；
- manifest schema 7 与运行时 digest；
- Firefox trust gate 和静态 rootfs 检查；
- 现有 browser-web/签名来源测试 79 项通过。

这些能力解决的是“启动哪个可信 Firefox rootfs”的问题，不等同于
GMAC、DNS、TLS 或公网访问本身已经在真机闭环。

## QEMU Firefox 基线（SMP=4）

本轮使用重建后的 stage1 initramfs 做了两层验证：

1. 启动 profile 在约 35 秒内依次达到基础桌面、X socket、Firefox exec
   和 Marionette 四个标记；带进程诊断参数的路径同样通过。
2. 使用冻结的 RISC-V Firefox JIT overlay 后，完整 browser-web gate
   通过：DNS、HTTP、HTTPS、百度首页与图片、fixture 搜索/下载、Bilibili
   页面、Marionette 及浏览器能力检查均通过。百度结果为
   `external-captcha`，这是站点策略，不是网络或内核失败。

默认 Debian ESR rootfs 不包含 JIT overlay 时，WebAssembly 能力会报告
`false-capability:wasm`，因此不能把“Firefox 进程启动”误报为“现代网页兼容”。
后续 browser-web 镜像应明确使用 overlay 产物，并在发布前运行
`firefox_jit_overlay.py` 与 `browser_web_trust_check.py`。

stage1 initramfs 是生成产物，源码更新后必须重新执行：

```bash
docker run --rm --network=host -v "$PWD:/root/asterinas" \
  -w /root/asterinas asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached \
  bash tools/riscv/debian/rootfs/build_stage1.sh \
  target/debian-riscv/stage1/initramfs.cpio
```

Firefox 启动 profile 现在会在启动 QEMU 前检查 archive 是否含有
`systemd-arguments` 环境转发标记，过期产物会快速失败并提示重建，而不会
再运行数分钟后才显示 `qemu-fixture-config`。
