# Megrez CPU clock attribution for the Firefox desktop baseline

Date: 2026-09-25. This is a short physical-board attribution experiment on
`main`, not a Firefox speedup. The production RockOS menu, the signed Debian
root, and all persistent Asterinas boot selectors were unchanged.

## Result

An opt-in Asterinas boot probe counted CPU cycles against the independent
device-tree timebase for three 10 ms windows on the boot hart:

| Sample | Cycles | Time ticks at 1 MHz | Estimated core frequency |
| --- | ---: | ---: | ---: |
| 1 | 14,000,369 | 10,000 | 1,400,036,900 Hz |
| 2 | 13,999,711 | 10,000 | 1,399,971,100 Hz |
| 3 | 13,999,375 | 10,000 | 1,399,937,500 Hz |

The median is 1.3999711 GHz. This is a direct cycle/time measurement on
Megrez, not an estimate inferred from a browser benchmark. The opt-in kernel
parameter is `asterinas.cpu_clock_probe=1`; without it the new branch
returns immediately. The probe changes no PLL, voltage, or clock selector.
QEMU can report a different cycle/time ratio, so its output is only a smoke
test for the probe and must not be treated as a physical CPU frequency.

The measured release Image SHA-256 was
`d911bf9578f1c5ca219f6d16755b1bb4fea62c4745f38fe4f775b1c94085f852`,
size 6,096,616 bytes. U-Boot CRC32 `1dffe69c` matched its loaded bytes.
It used the existing Stage1 SHA-256
`ea446515661f2380568426b3c37da0b6a0698fc51f88d32c62807fce04890962`,
DTB SHA-256
`465cb129333cc334a3161fabb43d37df8738ac4baa86fcdb50260691a22a84ba`,
and installed signed Firefox 143 root SHA-256
`d4f4e88fb20a8938e7270f4ceaf9c79855ba3d038acbd6f1030fe269dfca9bd4`.
The Asterinas boot ID was `5bda187d-8e47-422d-b9b2-41b22eabde2d`.
The [boot serial log](cpu-clock-probe-boot.serial.log.gz) contains the
U-Boot artifact checks, three probe lines, and isolated root-console marker.

## Same-binary RockOS control

The [static ALU probe](../../../../tools/riscv/debian/rootfs/cpu_throughput_probe.c)
pins CPU 0, performs 20 million deterministic iterations, checks the same
`7ead77efdc3bdb43` result, and reports both wall and thread CPU time.
The RISC-V executable SHA-256 was
`283970d2f3cae1d3036b00856302e6a542f84bf706f8bd770d512ab0f586be9f`.
It was built once with `riscv64-linux-gnu-gcc -static -O2 -Wall -Wextra -Werror`
and used unchanged under both kernels.

| Environment | Three thread-CPU samples (ms) | Median (ms) |
| --- | --- | ---: |
| RockOS 1.8 GHz, first control | 66.773, 66.905, 67.131 | 66.905 |
| RockOS 1.4 GHz | 85.948, 85.911, 85.912 | 85.912 |
| RockOS 1.8 GHz, restored control | 66.785, 66.766, 66.751 | 66.766 |
| Asterinas, warmed first boot | 87.412, 87.463, 87.410 | 87.412 |
| Asterinas, separate probe boot | 89.974, 90.731, 89.834 | 89.974 |

The RockOS A/B/A [raw samples](cpu-clock-rockos-frequency-aba.txt)
verify the governor and current frequency on each leg and restore
`performance`/1.8 GHz in a `finally` block. The Asterinas warmed median
was 1.7% slower than RockOS at 1.4 GHz on this narrow compute test. The
separate Asterinas boot was modestly slower; this variability and different
system activity prevent a precise cross-kernel overhead claim.

RockOS boot ID `b6eabb99-32e0-42d2-b078-33ae82d03c93` also supplied a
read-only [PLL register A/B/A](cpu-clock-rockos-registers-aba.txt):
at 1.8 GHz, CPU PLL registers `0x64/0x68` were
`0x12c01301/0x0000000a`; at 1.4 GHz they were
`0x0e901301/0x0555555a`; returning to 1.8 restored the first pair.
These register offsets agree with the
[ESWIN Linux clock-driver submission](https://lists.openwall.net/linux-kernel/2026/03/03/527).
The board's live CPU OPP table declares 800,000 µV for 1.4 GHz and
900,000 µV for 1.8 GHz. That is an operating-point contract, not a
measurement of the actual rail voltage. RockOS's regulator summary did not
show a CPU regulator, so its voltage control path remains unresolved.
The [NuttX EIC7700 clock implementation](https://apache.googlesource.com/nuttx/+/1686bb6c9eb741a4ed852c724c2ed609fbda0cad%5E%21/)
documents the clock-source parking and bus-ratio ordering needed for a safe
PLL transition. No Asterinas PLL write was attempted.

## Recovery and remaining work

The probe boot retained the existing opt-in isolated root console. A freshly
reopened serial connection proved UID 0 and its boot ID. An authenticated
`sync` and software reboot returned to U-Boot; the operator then selected
the firmware's existing `bootcmd_rockos` entry. The
[recovery serial log](cpu-clock-probe-recovery.serial.log.gz) reaches RockOS's
login prompt. A new RockOS login and nonce-framed root command proved boot ID
`b6eabb99-32e0-42d2-b078-33ae82d03c93`, governor
`performance`, 1.8 GHz, and partition 2 unmounted. The two boot selector
SHA-256 values remained `02280720efe7a1ad0ac084cdc20429406631e12d2e16f05638544bab0883fb26`
and `eb5f39a6e2db71ccc93ae005c488fd9f7ef353e75426e5a51e8923a9cbf2ebc5`.
The temporary Image was removed after its exact SHA-256 was rechecked;
`/boot` availability returned to 14,139,392 bytes.

The first recovery attempt accidentally interrupted U-Boot autoboot while
trying to log into RockOS. The board was still interactive at the U-Boot
prompt; explicitly running the verified vendor `bootcmd_rockos` restored
RockOS. The second recovery used one serial owner to catch U-Boot and then
select RockOS. Neither event tested recovery from an unresponsive desktop.

A separate Sv48/SMP=4 release QEMU `AUTO_TEST=boot` run passed with the
probe disabled. The native static C build passed `-Wall -Wextra -Werror`.
The finding gives a concrete explanation for some CPU-heavy Firefox cost,
but no browser A/B improvement has been measured. Even a pure CPU-bound
workload moving from 1.4 to 1.8 GHz has an ideal ceiling of about 1.29×;
the desktop's twofold stretch target also needs work on the measured SWGL
YUV presentation path. A future CPU-frequency feature must first identify
the Megrez CPU rail and safe operating-point transition, include a rollback
path, and validate the actual clock and desktop workload after each change.
