# 板子网络地址约定（`BOARD_IP` / `BOARD_GW` / `PORTAL_IP`）

日期：2026-11-05

真实地址一律不入库（见 `xaj_vibenote.md` 第七节，由
`tools/check_secrets.sh` 机械检查）。本文是这条约定在工具侧的落点。

## 变量与兜底

| 变量 | 含义 | 未设置时的文档地址 |
| --- | --- | --- |
| `BOARD_IP` | 板子自己的 IPv4 | `192.0.2.10` |
| `BOARD_GW` | 板子所在网段的网关 | `192.0.2.1` |
| `PORTAL_IP` | 校园门户的 IPv4 | `192.0.2.20` |
| `PORTAL_HOST` | 门户主机名 | `auth.zgclab.edu.cn` |

`tools/riscv/megrez/board/megrez_recover.py` 在导入时读取这四个变量；未设置就回落到上表
的 RFC 5737 地址，并向 stderr 打印一行 `warning: <变量> is not set; using the
documentation address …`。其它脚本（门户登录示例、`board/megrez_boot_cmd.txt`）只把
地址当参数或占位符，不保存真实值。

## 板上地址的权威来源

板子上的真实地址来自内核命令行：

```text
asterinas.net=<设备>,<板子ip>/<前缀长度>,<网关>
```

`tools/riscv/debian/rootfs/asterinas_net_watchdog.sh` 优先解析这一项，
`BOARD_GW` 只作为解析不到时的兜底。U-Boot 的 `board/megrez_boot_cmd.txt` 里对应
字段是 `192.0.2.10/24,192.0.2.1`，本地复制时替换成实际值。

## 用法

```sh
BOARD_IP=… BOARD_GW=… python3 tools/riscv/megrez/board/megrez_recover.py
python3 tools/riscv/board portal --expect-ip "$BOARD_IP"
```

## 相关文件

- `tools/riscv/megrez/board/megrez_recover.py` —— 地址变量与兜底 warning。
- `tools/riscv/board/megrez_portal_login.py` —— 门户登录（凭据来自
  NetworkManager，不入库）。
- `tools/riscv/debian/rootfs/asterinas_net_watchdog.sh` —— 板上网关来源。
- `tools/riscv/megrez_boot_cmd.txt` —— U-Boot 引导脚本片段。
- `tools/check_secrets.sh` —— 提交前对暂存区做同一规则的检查。
