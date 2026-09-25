# Megrez PowerVR render path: RockOS reference inventory

The GPU render path is separate from the EIC7700 display controller. The
RockOS source has an `es_drm` KMS driver and a PowerVR `pvrsrvkm` render
driver. The latter is not a small extension to Asterinas's current
`/dev/dri/card0`: it owns device memory, firmware, command submission,
synchronization, recovery, and a vendor bridge ABI. Asterinas currently has
same-device PRIME import only, so GPU-to-display buffer sharing is also a
separate piece of work.

On the actual RockOS partition, `eswin-eic7x-gpu` is installed at
`24.2+0rockos3`; the PowerVR-tuned Mesa EGL/GBM/DRI/Vulkan packages are
`1:22.3.5+1rockos1+0pvr2`. The RockOS kernel source's PowerVR version header
identifies DDK `24.2@6643903`. Its `pvr_drm.h` exposes a packed
`PVR_SRVKM_CMD` bridge ioctl and a separate render initialization ioctl,
among other sync ioctls. The matching implementation dispatches these through
`DRM_RENDER_ALLOW`, and the generated bridge headers cover memory, sync,
render, and device services. The source inspected was RockOS kernel commit
`bf2ec5d53002c16bc1bc593b92516eb6c2866176`.

The running RockOS kernel reports `6.6.87`, while the installed
`pvrsrvkm.ko` is under `/lib/modules/6.6.87-win2030/` and has vermagic
`6.6.87-win2030 SMP mod_unload riscv`. Its SHA-256 is
`5ca417c9ed90b59b26f3f58fb046c9df2f49d0e69be758d4bf6133f491b95aa1`.
The running configuration says `CONFIG_DRM_IMG_VOLCANIC=m`, but no module is
loaded, the `51400000.gpu` platform device has no bound driver, and
`/dev/dri` contains only the `es_drm` card node, with no render node.
The module tools exist under `/usr/sbin` but are outside the interactive
user's default `PATH`. This boot is not a validated hardware-rendering baseline
despite the installed userspace package. We did not force-load the
mismatched module or alter the RockOS recovery installation.

The mismatch has a concrete cause. `/boot/extlinux/extlinux.conf` selects
`/boot/vmlinuz-6.6.87-win2030`, but that file contains a custom `6.6.87`
kernel built on 2025-09-01. The package's matching `6.6.87-win2030` image
survives as `/boot/vmlinuz-6.6.87-win2030-origin`. In addition,
`/etc/modprobe.d/blacklist-pvr.conf` explicitly blacklists `pvrsrvkm`; `dpkg`
does not own that blacklist file. The installed module's OF alias matches
`img,gpu` and has no module dependencies. The vendor Vulkan ICD points to
`libVK_IMG.so`; `/lib/firmware/rgx.fw.30.3.408.101` and
`rgx.sh.30.3.408.101` are present. These files indicate the intended driver
combination, but they do not prove that this board has rendered a frame.

A decompressed copy of the matching package kernel was staged at
`/home/debian/asterinas/rockos-gpu-reference-20260926/Image` with SHA-256
`790380493872e387781a96ff99b64737daaf6f4c05b5894ba500a6aa39273b81`.
The initrd and package Megrez DTB SHA-256 values are
`64cab5cd753da2b4c48b5f1f16544edd2562d2eb6e4022c93b686d220e7405e2`
and `02a8d43d581b4aa8e957e231ee90eba19ffd7e8cfcf74694e86a1fb9c6b37f17`.
No persistent boot entry was changed. A temporary hardware-reference boot is
deferred until a recovery method is available: this U-Boot has no `wdt`
command, and the current RockOS boot exposes no `/dev/watchdog`. An early
hang in an untested kernel would otherwise leave no verified remote reset.

The RockOS driver Makefile defaults `RGX_BVNC` and `RGX_BNC` to
`30.3.408.101`, but a build default is not a readout of this board's silicon.
The upstream Mesa PowerVR Vulkan driver documents support by exact BVNC, and
its current actively developed list does not contain that Makefile default.
Therefore the upstream DRM/Mesa path and RockOS's vendor bridge cannot be
treated as interchangeable. Read the physical BVNC from a working driver or
hardware probe, then decide which kernel UAPI and userspace stack can actually
support it. A product name alone is insufficient for that decision.

The GPU development gate should first establish a separate, recoverable
RockOS boot where the running kernel and `pvrsrvkm` module match. Confirm a
render node, a hardware renderer, and a minimal EGL/Vulkan pixel probe there;
capture the exact opened nodes, ioctls, firmware files, and dma-buf/fence
traffic. Then implement an Asterinas render node and its memory/firmware/
submission/synchronization contract against that trace, followed by
cross-device PRIME sharing with the display path. Only after a hardware pixel
probe passes should Xorg acceleration and Firefox's compositor/WebGL be
enabled in a selected boot. The current display scanout experiment remains
useful independently of that work.

Raw package, sysfs, module and Xorg observations are in
`/home/ubuntu/.codex/asterinas-native-scanout-20260926/rockos-gpu-inventory*.json`.
The upstream references are the [RockOS FAQ](https://docs.rockos.dev/en/docs/faq/),
[RockOS PowerVR UAPI](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/include/drm/pvr_drm.h),
the [RockOS driver Makefile](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/Makefile),
the [RockOS PowerVR DRM implementation](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/services/server/env/linux/pvr_drm.c),
and the [Mesa PowerVR support matrix](https://docs.mesa3d.org/drivers/powervr.html).
