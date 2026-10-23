# Debian 包生态兼容性（L1：popcon top-50）

## 方法

- 清单：`tools/x86/debian/popcon_top50.txt`（popcon 前列，排除
  `libreoffice*`/`firefox*`/`thunderbird*`/`texlive*`/`chromium*`）；
- 探测脚本：`tools/x86/debian/popcon_packages.sh`（guest 内逐包
  `apt-get install --no-install-recommends`，记录 rc/耗时/smoke，串口打
  `POPCON pkg=… rc=… smoke=… dur=…`，JSON 落到 `/tmp/popcon-result.json`）；
- 运行方式：沿用 `daily_use_gate.sh` 的 QEMU 启动链（待本轮执行）。

## 结果

| 指标 | 数值 |
|---|---|
| 安装成功 | 待测 |
| smoke 可用 | 待测 |
| 总耗时 | 待测 |

## 失败归因

| 包 | rc | 归因 |
|---|---|---|
| — | — | — |

> 本轮尚未执行（上下文预算耗尽）。脚本与清单元数据已就位，运行命令：
> 在门禁里把 `popcon_packages.sh` 与清单注入 `/usr/local/bin` 与
> `/usr/local/share/popcon_top50.txt` 后执行，或直接
> `make test_x86_daily_use`（需先在清单中调用它）。
