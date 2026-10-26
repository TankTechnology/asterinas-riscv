# 探针修复后的 Firefox 完整延迟表（首帧 / 输入 / 滚动 / 冷启动）

日期：2026-10-26
内核：`e8b4a8194c0918ac`（未重编译）
探针：`/tmp/vb/present_probe7.sh` = `present_probe6.sh`（v6 + `mkdir -p /tmp/ffs`）+ 两处非测量性改动：
1. 输入延迟阶段前加一行 `xdotool key Home`（把页面回到顶部；`Down → 首像素变化` 的测量逻辑未变）
2. 末尾追加任务 B 的 profile 自动创建测试

两侧同探针、同镜像、同 QEMU 几何（仅内核与分辨率不同：Asterinas 1024×768，Linux 1280×800）。

## 1. 完整对比表

| 指标 | Asterinas（P7 / P8 合并，中位） | Linux（L2 / L3 合并，中位） | 相对差 |
|---|---|---|---|
| Firefox 冷启动 ×3 | **4979 ms**（P7 5238 / P8 4944） | **938 ms**（L2 940 / L3 935） | **5.3× 慢** |
| 滚动 20 页 ×3 | **2408 ms**（P7 2357 / P8 2458） | **1188 ms**（L2 1247 / L3 1128） | **+103 %** |
| 首帧延迟 ×10 | **57 ms**（P7 58.5 / P8 56） | **16.5 ms**（L2 17.5 / L3 15.5） | **+245 %** |
| 输入延迟 ×10 | **54.5 ms**（P7 52.5 / P8 62.5） | **16.5 ms**（L2 30 / L3 15） | **+230 %** |
| *[参考]* 截屏地板 | 63–106 ms | 12 ms | +6× |
| *[参考]* 分辨率 | 1024×768 | 1280×800（多 30 % 像素） | 对 Linux 不利 |

合并方法：每侧的 ×10 指标把两轮 20 个样本一起排序取中位；×3 指标把两轮 6 个样本一起取中位。

原始行（P7）：

```
PROBE firefox_cold_start_ms_1=5295
PROBE firefox_cold_start_ms_2=4934
PROBE firefox_cold_start_ms_3=5238
PROBE scroll20_ms_1=2912
PROBE scroll20_ms_2=2357
PROBE scroll20_ms_3=2248
PROBE first_frame_ms_1..10=52/47/52/53/57/60/60/64/952/64
PROBE input_latency_ms_1..10=52/50/55/52/54/53/59/48/48/69
PROBE page_pixels=w=1024 h=768 sampled=52480 unique=506 top=#ffffff topfrac=0.678
PROBE ff_diag_procs=8
PROBE probe_total_ms=110309
```

原始行（P8）：

```
PROBE firefox_cold_start_ms_1=4938
PROBE firefox_cold_start_ms_2=5014
PROBE firefox_cold_start_ms_3=4944
PROBE scroll20_ms_1=2458
PROBE scroll20_ms_2=2919
PROBE scroll20_ms_3=2021
PROBE first_frame_ms_1..10=51/46/48/82/58/55/57/51/976/66
PROBE input_latency_ms_1..10=43/70/44/64/49/72/79/73/56/61
PROBE np_dir_exists=0
PROBE probe_total_ms=…
```

原始行（L2 / L3，Linux）：

```
PROBE firefox_cold_start_ms_1..3=1044/940/881   （L2）
PROBE firefox_cold_start_ms_1..3=1008/935/907   （L3）
PROBE scroll20_ms_1..3=2109/1247/1014           （L2）
PROBE scroll20_ms_1..3=947/2136/1128            （L3）
PROBE first_frame_ms_1..10=16/23/18/17/15/67/135/15/17/330        （L2）
PROBE first_frame_ms_1..10=15/16/17/17/16/15/15/15/15/293         （L3）
PROBE input_latency_ms_1..10=28/30/30/31/30/16/32/30/16/30        （L2）
PROBE input_latency_ms_1..10=15/15/16/17/15/16/15/15/18/15        （L3）
PROBE page_pixels=w=1280 h=800 sampled=42800 unique=306 top=#ffffff topfrac=0.694   （L2、L3 相同）
PROBE ff_diag_procs=12
```

## 2. 旧基线为什么不能用

`RESULTS.md` 里的 Linux 基线（首帧 31.5 ms、输入 134.0 ms、滚动 901 ms）是用 v5 探针测的，
而 v5 同样没建 `/tmp/ffs`。L1 在 Linux 上直接验证了这一点：

```
PROBE p_pre_exists=0
PROBE p_dir_exists=0
PROBE p_windows=0x20002b "Profile Missing": ("Firefox" "firefox-esr")  566x104+357+348
```

即 Linux 上 Firefox 也不会自动创建缺失的 `--profile` 目录，弹出的对话框与 Asterinas 一模一样。
所以旧基线的首帧/输入数字来源可疑；本报告改用 L2/L3（修好探针后重测）作为 Linux 基线。

## 3. 任务 B：Firefox 为什么不自动创建 profile 目录（结论：不是内核问题）

| 侧 | 用例 | 原始行 | 结论 |
|---|---|---|---|
| Asterinas（P7） | `firefox --profile /tmp/nonexist` | `np_dir_exists=0`、`np_windows=…"Profile Missing" 566x104…` | 不创建，弹对话框 |
| Linux（L1） | 同上 | `p_pre_exists=0`、`p_dir_exists=0`、`p_windows=…"Profile Missing" 566x104…` | **同样不创建，同样弹对话框** |
| Linux（L1 对照） | 目录已存在（空） | `p_exists_dir=1`、`p_exists_entries=.parentlock,.startup-incomplete,…`（约 40 项）、`p_exists_windows=0x20002b "Mozilla Firefox" 1152x720` | 正常加载并自动填充 profile |
| Asterinas（P6/P7） | 目录已存在（`mkdir`） | `page_pixels=… top=#ffffff 0.678` | 正常加载 |

**结论**：两侧行为完全一致——Firefox 的 `--profile <path>` 要求目录已存在，缺失时弹
"Profile Missing" 对话框并且**不会**帮你创建。这不是 Asterinas 内核的缺陷，任务书里
「Linux 上会自动创建」的前提本身不成立（之前那条推测已在
`2026-10-27-x11-presentation-vs-firefox.md` §5.2 标注更正）。

因此任务 B 在「只定位不修」的意义上已经收口：没有需要修的内核问题；探针建目录即可。

## 4. 怎么读这张表

- **冷启动 5.3×** 是最硬的差异：Asterinas 六次 4934–5295 ms、Linux 六次 881–1044 ms，
  两侧区间完全不重叠，不是噪声。
- **滚动 +103 %**：Linux 的 1188 ms 里含探针地板（20 ×（xdotool 启动 ≈15 ms + 30 ms sleep）），
  两侧各自都有 2021–2919 / 947–2136 的波动，所以这一项的置信度低于冷启动。
- **首帧 +245 %、输入 +230 %**：Linux 的分布很紧（首帧 15–18 ms 占 15/20，输入 15–17 ms 占 18/20），
  Asterinas 集中在首帧 46–66 ms、输入 43–79 ms，两侧几乎不重叠。这是本次最可复现的两项。
- 分辨率对 Linux 不利（1280×800 比 1024×768 多 30 % 像素），所以这些差距不是「Linux 像素少所以快」。

## 5. 证据

- `evidence/2026-10-27-firefox-latency-after-probe-fix/P7-probe-lines.txt`（89 行）
- `…/P8-probe-lines.txt`（89 行，Asterinas 第二次运行）
- `…/L1-probe-lines.txt`（14 行，Linux 侧 profile 行为测试）
- `…/L2-probe-lines.txt`、`…/L3-probe-lines.txt`（各 89 行，Linux 基线重测）
- 控制台：`~/vb-ws/{P7,P8,L1,L2,L3}/console.txt`；门禁日志 `/tmp/vb-{P7,P8,L1,L2,L3}-gate.log`
- 探针：`/tmp/vb/present_probe7.sh`、`/tmp/vb/profile_probe.sh`；
  门禁：`/tmp/vb/present_gate7.sh`（Asterinas）、`/tmp/vb/present_gate7_linux.sh`、`/tmp/vb/profile_gate_linux.sh`
