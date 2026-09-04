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

