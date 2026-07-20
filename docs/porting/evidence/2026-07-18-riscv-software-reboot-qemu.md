# RISC-V 软件恢复 QEMU 冻结证据

本页记录 2026-07-18 在冻结代码提交上完成的 timer/panic 双场景结果。原始
QEMU 文件位于生成目录且不提交；这里提交可复核的输入身份、结果摘要与
SHA-256，防止把可移动分支或另一份 Image 的结果误当成本轮证据。

## 冻结身份

| 项目 | 值 |
|---|---|
| Asterinas 代码 | `7f691c479df1b5319f71a6ad738f36541d90ca54` |
| 分支 | `codex/riscv-software-reboot` |
| 开发容器 | `asterinas/asterinas:0.18.0-20260702` |
| QEMU | `QEMU emulator version 10.2.1` |
| profile | `megrez-sv48-svade-fast`：4 hart、2 GiB、Sv48、强制 Svade、`zkr=false` |
| U-Boot | `ece349ade2973e220f524ce59e59711cc919263f` |
| kernel ELF SHA-256 | `872a2bcf41f061bca03c75966547508c19412d55a55ae2f1497214ac44f0450e` |
| Linux Image SHA-256 | `18293ad79e1c0372b8fa54452b44e0db6644ca10aabbfeb38d3af6a7ce8281a9` |
| QEMU monitor/QMP | 禁用；`-monitor none`，未开放 QMP |
| 启动盘 | `snapshot=on`；每个场景审计前后 SHA-256 相同 |

代码提交冻结后，在固定容器中从仓库根目录重新构建默认 Sv48 内核、生成
Linux Image，再运行双场景门禁：

```bash
test "$(git rev-parse HEAD)" = \
  "7f691c479df1b5319f71a6ad738f36541d90ca54"
test -z "$(git status --short)"
make kernel TARGET_ARCH=riscv64
mkdir -p target/qemu-uboot/software-reboot/7f691c479-inputs
python3 tools/riscv/make_booti.py \
  target/osdk/aster-kernel-osdk-bin.qemu_elf \
  target/qemu-uboot/software-reboot/7f691c479-inputs/asterinas.booti
sha256sum --check <<'EOF'
872a2bcf41f061bca03c75966547508c19412d55a55ae2f1497214ac44f0450e  target/osdk/aster-kernel-osdk-bin.qemu_elf
18293ad79e1c0372b8fa54452b44e0db6644ca10aabbfeb38d3af6a7ce8281a9  target/qemu-uboot/software-reboot/7f691c479-inputs/asterinas.booti
EOF
make test_riscv_software_reboot \
  ASTERINAS_RISCV_BOOTI="$PWD/target/qemu-uboot/software-reboot/7f691c479-inputs/asterinas.booti" \
  RISCV_SOFTWARE_REBOOT_OUT_DIR="$PWD/target/qemu-uboot/software-reboot/7f691c479"
```

## 结果

| 场景 | 审计 | `booti` 次数 | 触发到恢复固件 | 首次 `booti` 到恢复固件 | 清理 | 启动盘 |
|---|---:|---:|---:|---:|---:|---:|
| timer（QEMU 10 秒） | `PASS` | 1 | 12.2142 秒 | 16.3523 秒 | complete | unchanged |
| panic | `PASS` | 1 | 2.2267 秒 | 6.6600 秒 | complete | unchanged |

两个场景都在触发后按顺序重新观察到 OpenSBI、U-Boot 2026.07 与提示符；
`recovery_complete=true`、`cleanup_complete=true`，且审计未发现失败项。

## 原始证据哈希

### Timer

| 文件/对象 | SHA-256 |
|---|---|
| `result.json` | `39591a9e09d021100fd37b2c01499e13f9fdb2e8a387700261ec928c9f17d675` |
| `serial.log` | `9fb6ad2c757160b58677338df2fea97a86e039d4c70316ecc053fa1b0138dc79` |
| `recovery-epoch.txt` | `b06463a72859f521acde8589fdb2f841ed9452435202b11c696ce41122462769` |
| `artifacts.json` | `b8dfa5339ba1b6101b1006ea4455860e9edcaaaabc8a9239846321964d37c193` |
| `initramfs.cpio.gz` | `7cd5ce87b456ff6add1be32bf877cbdc3b7069962f31ab5463d1899663ef112c` |
| `qemu-virt.dtb` | `0cb7e956e3df6a9e88ff839712f0d0f39da73b0ac6b8605acb16e9d4949077f4` |
| 启动盘（前后相同） | `230660e3e8d390f2ac05c92eed2e91c4f42c0cbe1c80698ac255a3e51883e53a` |

### Panic

| 文件/对象 | SHA-256 |
|---|---|
| `result.json` | `1e1ca737a57c9ae9374d6a77ae4f20c5f657cb92d1f95d59e781442657b37510` |
| `serial.log` | `b21d3608f56e4fb1bb624076f5343c269f96d1f1acb7abdd962d051ba377805b` |
| `recovery-epoch.txt` | `13fdf256e91162a2d5c0b49b6a322cbb003ccad6e9904b6a763c69b4aa22d056` |
| `artifacts.json` | `ac9f2e8f091081c73ecd2d2343e52c6bc41cca8ba3f0fd57a91457b9d003000f` |
| `initramfs.cpio.gz` | `4478149cf5f54e35c5481e72260eff64e1e72ada4f596fc1e9af2747fc7a7058` |
| `qemu-virt.dtb` | `16d5e3df3eb0b650e350cf63ddb65d2d85d0e39d2fec52e6e2e8bd37c136762f` |
| 启动盘（前后相同） | `870840e8c93a9e9fb95e139b0fa44c5fcdd2cb8c70abafd89034c6613298e457` |

## 证据边界

本轮证明冻结的 Asterinas Image 能经真实通用 U-Boot `booti` 进入用户态，
随后通过 timer 或 fatal panic 请求 SBI cold reboot，并在 QEMU `virt` 的
OpenSBI/U-Boot 上进入新的固件周期。它不证明 EIC7700 厂商 SBI 的 reset
实现、真实 Megrez DTB/保留内存、PMIC/WDT、缓存一致性或完全关中断后的恢复。
当前 Image 仍需在具备外部断电/复位兜底的 Megrez 上验证。

本页提交的是原始证据的身份与摘要，不是原始串口日志的替代品；需要逐字审计
时必须取得与上述 SHA-256 对应的本地或 CI artifact。
