# 页缓存顺序预读（batch cold page reads）

日期：2026-10-26
内核：改前 `e8b4a819`（`e3232e6b5` 的基线数据）→ 改后本 commit
微基准：`/tmp/vb/readbench_probe.sh`（与 `e3232e6b5` 同一程序、同一文件）
Firefox 探针：`/tmp/vb/present_probe7.sh`（v6 + `mkdir -p /tmp/ffs` + 输入前 `Home`）

## 1. 改动（一处，最小）

`kernel/core/src/vm/page_cache/vmo/mod.rs` 的 `commit_on_internal`（backed VMO）原来在缺页时
调用 `page.ensure_init(|lp| backend.read_page(idx, lp))`，一次只取一页、一个 bio。
现在改走新增的 `Vmo::read_page_with_prefetch`：

1. 目标页仍是未初始化状态时，从该页起**向后收集连续且未就绪的页**（最多 `READAHEAD_PAGES = 32`），
   缺失的页在 VMO 里就地分配 `CachePage::alloc_uninit()`；
2. 把这些页整体锁住（`lock_guard().into_owned()`），用**一个 `IoBatch`** 提交
   `backend.read_pages_async(PageRun::new(start_idx, …))`，然后 `wait_all()` —— **一次等待覆盖 N 页**；
3. 批量失败（提交错误、越界等）时**回退**到原来的单页 `read_page`，保证调用方总能前进；
4. 目标页最终由 `ensure_init` 收尾：批量已把它置为 up-to-date 时走快速路径。

开关：`READAHEAD_PAGES`（`vmo/mod.rs` 顶部 `static AtomicUsize`，默认 32），
配套 `set_readahead_pages()`/`readahead_pages()`，**设为 1 即恢复历史行为**（编译期可改默认值）。

边界与安全：
- 只用 in-ostd/in-core 的安全 API（`IoBatch`、`PageRun`、`CachePage::lock_guard`），
  未新增 `unsafe`，`deny(unsafe_code)` 不变；
- 预读范围按 `self.size().div_ceil(PAGE_SIZE)` 截断，不会越过文件尾；
- 只对 `CommitMode::Read`（`!skips_backend_read()`）生效，写路径（`Overwrite`）完全不变。

## 2. 微基准：冷读（同一程序、同一 164 MB `libxul.so`）

| 指标 | 改前（R2） | 改后（R4） | 变化 |
|---|---|---|---|
| 每 4 KiB 页 wall | **132.53 µs** | **29.74 µs** | **−77.6 %（4.5× 快）** |
| 每 4 KiB 页 cpu | 50.31 µs | 15.87 µs | −68.5 % |
| 整文件读一遍 | 5.311 s | **1.192 s** | **4.5× 快** |
| 吞吐 | 31 MB/s | **138 MB/s** | 4.5× |

判据：每页 < 40 µs ✔（29.7），吞吐 > 100 MB/s ✔（138）。仍比 Linux（5.26 µs / 780 MB/s）慢 5.6×。

原始行：

```
# 改后 R4
PROBE rb_libxul_all=phase=A0_cold_seq4096 reads=40070 bytes=164124984 wall_ns=1191654631
      cpu_ns=636000000 avg_wall_ns=29739 avg_cpu_ns=15872|phase=A1_warm_seq4096 …
# 改前 R2
PROBE rb_libxul_all=phase=A0_cold_seq4096 reads=40070 bytes=164124984 wall_ns=5310676198
      cpu_ns=2016000000 avg_wall_ns=132534 avg_cpu_ns=50311|…
```

## 3. 热读与其它文件系统：未回退

| 用例（avg_wall_ns） | 改前 | 改后 | 变化 |
|---|---|---|---|
| ext4 40 MB A0 冷读 | 4509 | 4713 | +4.5 %（噪声内） |
| ext4 40 MB A1 热读 | 4529 | 4600 | +1.6 %（±10 % 内） |
| ext4 A2 1 字节 | 4355 | 4288 | −1.5 % |
| ext4 A3 `readv` | 6850 | 5862 | −14 % |
| ext4 A4 `pread` | 5121 | 4442 | −13 % |
| libxul A1 热读 | 6154 | 4774 | −22 % |
| tmpfs A1 热读 | 4618 | 4438 | −3.9 % |

结论：热路径没有回退（全部在 ±10 % 内或更好）；tmpfs 不受影响（不走块设备，符合预期）。

## 4. Firefox 冷启动：只小幅改善（未达标）

| 指标 | 改前（P7/P8 中位） | 改后（P9） | 变化 |
|---|---|---|---|
| 冷启动 | 4979 ms（4934 / 5014 / 4944） | **4535 ms**（4646 / 4535 / 4490） | −8.9 % |
| 滚动 20 页 | 2408 ms | 2232 ms（2355 / 2164 / 2178） | −7.3 % |
| 首帧（有效中位） | 57 ms | 52 ms | −8.8 % |
| 输入延迟（中位） | 54.5 ms | 53 ms | −2.8 % |

**判据 < 3500 ms 未达成**（4535 ms）。原因与 `6fd578e32` 的三段分解一致：
Firefox 冷启动 81.9 % 花在「首个 `XMapWindow` 之后到窗口可见」，那段是 Firefox 自身的
应用初始化（JS、prefs、addon、内容进程），不是文件 I/O；本次改动只削掉了 I/O 那一部分，
所以只有约 9 % 的整体收益，与微基准 4.5× 的 I/O 改善并不矛盾。

## 5. 7 域矩阵：全绿

```
DAILY_APT=ok xclip+xterm+alsa-utils-installed
DAILY_DESKTOP=ok Xorg+DRI3
DAILY_APP=ok xterm
DAILY_CLIPBOARD=ok xclip-round-trip
DAILY_AUDIO=ok aplay-hw:0,0
DAILY_USB=ok mounted-/dev/sda-read-ASTERINAS-USB-OK
DAILY_SUSPEND=ok woke-up-disk-and-io-ok
DAILY_DONE=ok
```

`Uncaught panic` 计数 **0**，`allocation failed` 计数 **0**。
`DAILY_INPUT_METHOD=fail` 属既有缺口（不在这 7 域之内，与本改动无关）。
门禁：`make test_x86_daily_use … DAILY_USE_CHECKLIST=tools/x86/debian/daily_use_checklist_regress.sh`。

## 6. 风险与已知限制

1. **批次在页表锁/缺页上下文里同步等待**：一次最多 32 页的 I/O 可能在持有地址空间相关锁时完成，
   会放大单次缺页的最坏延迟（矩阵与 Firefox 探针未见卡顿或 panic，但这是设计上的已知代价）。
2. **内存放大**：每个首次缺页最多预分配 32 页（128 KiB）缓存页；顺序读场景是收益，
   稀疏随机读场景是浪费。后续可按"顺序度"（连续命中次数）动态启用。
3. **没有跨调用状态**：本次实现是"缺页时向后批量取"，不是 Linux 那种带方向/窗口的 readahead 状态机；
   非顺序访问也会触发一次批量。
4. 块层仍按页提交（`submit_read_page_run` 逐页提交 bio），当前收益来自**一次等待覆盖多页**；
   若能让块层真正合并相邻请求，还有进一步空间（Linux 的 5.6× 差距很可能在此）。

## 7. 证据

- `evidence/2026-10-27-page-cache-readahead/R4-readahead-probe-lines.txt`（改后微基准）
- `evidence/2026-10-27-page-cache-readahead/P9-firefox-probe-lines.txt`（改后 Firefox）
- `evidence/2026-10-27-page-cache-readahead/matrix.txt`（7 域矩阵）
- 改前基线：`evidence/2026-10-27-read-microbench/R2-asterinas-probe-lines.txt`、
  `evidence/2026-10-27-firefox-latency-after-probe-fix/P7-probe-lines.txt`
