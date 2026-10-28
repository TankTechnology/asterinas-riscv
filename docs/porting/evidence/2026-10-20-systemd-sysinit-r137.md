# E3 systemd sysinit r137

- Gate: `systemd-sysinit`
- Claim: E3 supplemental diagnostic; this is not an official Firefox scene pass
- Status: `pass`
- Scope: one bounded QEMU boot with the unmodified
  `systemd-sysctl.service`; no Xorg, Firefox, network, serial input, or
  physical board access

## Inputs

| Input | SHA-256 |
|---|---|
| kernel (`aster-kernel-pidmax-corepattern-fix-r138.Image`) | `6d83c89b8281c2b381a0e1b4534851e455cc4f2770370ce2957e7135715df2c5` |
| U-Boot (`target/qemu-uboot/cache/u-boot-build/u-boot`) | `47516422aea981a45eb3da6de786d356dbd9059c96bd9fc11d8e3ba28aec7210` |
| DTB (`target/qemu-uboot/debian-root/qemu-virt.dtb`) | `d6e8cf8a76188f7f247568c1df255ff699fefd40d74f2fe063b62ffa3381e3c2` |
| Stage1 (`target/debian-riscv/stage1-procfix-20261017/initramfs.cpio`) | `ce7fb996e9a6ac48160d81c29347a68d88e99204adcc147051df76f645031c84` |
| frozen base root image | `5d7e10e83e14b617505c091cac7ca320d08154d60ba0e6299deed6406a84bffc` |
| base rootfs manifest | `f06e1088517d405c50a9fec7734995dc8af54910bb5b0e01a2629ddd4aa52e51` |

The base root image is the same input used by r128. This run does not use the
r136 probe overlay; `systemd-sysctl.service` is the original Debian unit.
The kernel is the r138 candidate, the only changed input relative to r128.

Output directory: `/mnt/mydisk/qemu-systemd-sysinit-r137`

## Serial evidence

```text
         Starting systemd-sysctl.service - Apply Kernel Variables...
[  OK  ] Finished systemd-sysctl.service - Apply Kernel Variables.
[  OK  ] Reached target sysinit.target - System Initialization.
```

There is no `[FAILED] Failed to start systemd-sysctl.service` line.

## Result

`result.json` reports:

```text
passed: true
reason: pass
mode: service
profile: desktop-m4-firefox
debian_release: 13.7
```

## Interpretation

- The unmodified Debian `systemd-sysctl.service` completes on the r138
  candidate kernel.
- `sysinit.target` is reached within the 180-second boot budget.
- This closes the systemd-sysctl boundary isolated by r135 for this frozen
  input set.
- It does not claim the E3 official Firefox scene, Xorg, DRM, or physical
  PowerVR acceleration. It also does not claim that the desktop or Firefox
  services were started; the boot target was `sysinit.target`.

## Next

S3 is the existing Firefox short-probe / NewSession work, in a separate gate.
Do not treat this sysinit result as a Firefox result.
