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
`modprobe`/`modinfo` are absent in this RockOS image. Thus this boot is not
a validated hardware-rendering baseline despite the installed userspace
package. We did not force-load the mismatched module or alter the RockOS
recovery installation.

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
