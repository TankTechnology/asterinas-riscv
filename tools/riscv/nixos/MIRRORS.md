# 国内直连镜像指南 (不烧代理流量)

thinkpad mihomo 是 rule 模式: *.cn 域名 + 局域网 = DIRECT 直连, 其余走代理。
以下源全部直连免费, 优先使用。

## 已验证可用的直连源 (2026-08-13)

### busybox riscv64 静态版 — 推荐直接用, 不用从源码编译!
TUNA Alpine 镜像:
  https://mirrors.tuna.tsinghua.edu.cn/alpine/v3.22/main/riscv64/
  实测存在: busybox-1.37.0-r20.apk (静态链接 musl)
  用法: 下载 .apk 后 `tar -xzf` 两层解包 (apk 是 gzip 包裹的 tar, 内含 .tar.gz 数据段)

### pacman (主机工具链)
已配置: TUNA → USTC → 阿里云(代理备用)

### AUR / archlinuxcn
已配置: USTC → TUNA

### Rust crates
~/.cargo 依赖已缓存; 直连镜像慢 (rsproxy 4KB/s), 新依赖建议:
- 默认走代理 (MB 级, 可接受)
- 或经 pku4090 中转

## 流量现状 (2026-08-13)
- 重启后 27h: WiFi 共 ~10.6GB, 代理仅 ~15KB
- 代理总量 (历史): 2.07GB down / 2.82GB up
- deepseek API 直连不烧流量 (中国服务器)

## 结论
NixOS 轨道依赖安装几乎可 100% 走国内直连。
