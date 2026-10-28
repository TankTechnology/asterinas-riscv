# E3 systemd sysctl probe r136

- Gate: `systemd-sysctl-probe`
- Claim: E3 supplemental diagnostic; this is not an official Firefox scene pass
- Status: `pass`
- Scope: one bounded QEMU boot; no Xorg, Firefox, network, serial input, or
  physical board access

## Inputs

| Input | SHA-256 |
|---|---|
| kernel (`aster-kernel-pidmax-corepattern-fix-r138.Image`) | `6d83c89b8281c2b381a0e1b4534851e455cc4f2770370ce2957e7135715df2c5` |
| U-Boot (`target/qemu-uboot/cache/u-boot-build/u-boot`) | `47516422aea981a45eb3da6de786d356dbd9059c96bd9fc11d8e3ba28aec7210` |
| DTB (`target/qemu-uboot/debian-root/qemu-virt.dtb`) | `d6e8cf8a76188f7f247568c1df255ff699fefd40d74f2fe063b62ffa3381e3c2` |
| Stage1 (`target/debian-riscv/stage1-procfix-20261017/initramfs.cpio`) | `ce7fb996e9a6ac48160d81c29347a68d88e99204adcc147051df76f645031c84` |
| base root image | `5d7e10e83e14b617505c091cac7ca320d08154d60ba0e6299deed6406a84bffc` |
| probe root image (derived overlay) | `5c2d88685289ec08d8f8b11f7555599e857b11b7b11fb5dd59fe4215af5cbe57` |
| probe manifest | `2e91be6d9f49d94f8f3a301ff986de05be91697421400ce687625697267a255c` |

The only input difference from the r128 failure baseline is the kernel:
r128 used `54dd701c…`; this run uses the r138 candidate `6d83c89b…`.
U-Boot, DTB, Stage1, and the base root image are byte-identical to r128.

Output directory: `/mnt/mydisk/qemu-systemd-sysctl-probe-r136`

## Probe steps

The instrumented `systemd-sysctl.service` ran `sysctl-probe.sh` and exited
successfully. `missing_control` is the expected-failure negative control.

| Step | rc | ms | Detail |
|---|---:|---:|---|
| `read_pid_max` | 0 | 829 | `value=2147483647` |
| `read_core_pattern` | 0 | 668 | `value=core` |
| `write_pid_max` | 0 | 522 | — |
| `readback_pid_max` | 0 | 779 | `value=4194304` |
| `write_core_pattern` | 0 | 899 | — |
| `readback_core_pattern` | 0 | 882 | `value=core` |
| `binary_core` | 0 | 804 | real `systemd-sysctl` on `10-coredump-debian.conf` |
| `binary_pid` | 0 | 1173 | real `systemd-sysctl` on `50-pid-max.conf` |
| `binary_all` | 0 | 945 | real `systemd-sysctl` with no file arguments |
| `missing_control` | 1 | 533 | `No such file or directory` (expected) |

## Serial evidence

```text
sysctl-probe.sh[31]: SYSCTL_PROBE begin
sysctl-probe.sh[31]: SYSCTL_PROBE step=read_pid_max rc=0 ms=829 value=2147483647
sysctl-probe.sh[31]: SYSCTL_PROBE step=read_core_pattern rc=0 ms=668 value=core
sysctl-probe.sh[31]: SYSCTL_PROBE step=write_pid_max rc=0 ms=522
sysctl-probe.sh[31]: SYSCTL_PROBE step=readback_pid_max rc=0 ms=779 value=4194304
sysctl-probe.sh[31]: SYSCTL_PROBE step=write_core_pattern rc=0 ms=899
sysctl-probe.sh[31]: SYSCTL_PROBE step=readback_core_pattern rc=0 ms=882 value=core
sysctl-probe.sh[31]: SYSCTL_PROBE step=binary_core rc=0 ms=804
sysctl-probe.sh[31]: SYSCTL_PROBE step=binary_pid rc=0 ms=1173
sysctl-probe.sh[31]: SYSCTL_PROBE step=binary_all rc=0 ms=945
sysctl-probe.sh[31]: SYSCTL_PROBE step=missing_control rc=1 ms=533 value=cat: /proc/sys/kernel/asterinas_probe_missing: No such file or directory
sysctl-probe.sh[31]: SYSCTL_PROBE end
[  OK  ] Finished systemd-sysctl.service - Apply Kernel Variables.
[  OK  ] Reached target sysinit.target - System Initialization.
```

## Interpretation

- The candidate kernel exposes both required keys and accepts both writes:
  `pid_max` reads back `4194304`, and `core_pattern` reads back `core`.
- The real Debian `systemd-sysctl` binary succeeds when run on each config file
  and when run with no arguments.
- The negative control returns `ENOENT`, so the probe can distinguish a missing
  key from a successful write.
- The instrumented unit completes and `sysinit.target` is reached.
- This closes the syscall-level question. It does not yet validate the
  unmodified unit; S2 does that.

## Probe corrections

The final receipt was produced after two host-side probe corrections; neither
changed the guest input:

1. `StandardOutput=console` / `StandardError=console` are invalid systemd
   output specifiers. The unit now uses `journal+console`.
2. The marker parser initially assumed `\n` line endings and no serial prefix.
   It now accepts the `sysctl-probe.sh[31]: ` prefix and `\r` line endings.
   `missing_control` is explicitly classified as an expected failure.

The serial log remains the primary raw artifact; `result.json` in the output
directory is the machine receipt from the final run.

## Next

Run S2 with the unmodified `systemd-sysctl.service` and the frozen base root
image. S2 passes only if the serial log contains `Started systemd-sysctl.service`
(not `[FAILED]`) and `Reached target sysinit.target`.
