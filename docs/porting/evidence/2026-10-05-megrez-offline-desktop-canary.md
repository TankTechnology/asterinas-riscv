# Megrez offline desktop canary preparation, 2026-10-05

This note records the software-side gate prepared after the first guarded
desktop attempt reached Asterinas' kernel, root handoff, debug console and
framebuffer, but stopped at `browser-start` because the safe device profile
deliberately removed GMAC and DWC3 input/network paths.

## Safety boundary

The `minimal` desktop profile changes only U-Boot's in-memory device tree. It
disables both EIC7700 Ethernet nodes and both DWC3 nodes for one boot, does not
save the environment or write MMC, and removes the online proxy/DNS boot
environment. The framebuffer and storage path remain enabled. No new physical
boot was started while preparing this canary.

## Offline browser path

The profile sets:

```text
ASTERINAS_DESKTOP_OFFLINE=1
ASTERINAS_WEB_NETWORK_MODE=direct
ASTERINAS_DESKTOP_START_URL=file:///usr/share/asterinas/physical-graphics/index.html
```

The browser launcher accepts only `about:blank` or the fixed local canary URL.
The device-access helper skips USB input identity checks in offline mode while
retaining the framebuffer and XKB-cache checks. The stage-1 graphics controller
selects a framebuffer-only Xorg layout with no InputDevice sections. The host
admission probe additionally checks the Firefox command line for the local URL;
the minimal profile cannot pass on the basis of a merely visible Firefox
window.

## Verification

Passed on the host:

```text
python3 -m unittest tools.riscv.tests.test_megrez_desktop_boot -q
  33 tests OK

python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_debian_dev_overlay \
  tools.riscv.tests.test_debian_rootfs \
  tools.riscv.tests.test_debian_m5_network \
  tools.riscv.tests.test_debian_m6_browser \
  tools.riscv.tests.test_debian_m7_baidu \
  tools.riscv.tests.test_debian_m8_browser_quality \
  tools.riscv.tests.test_debian_m9_software -q
  301 tests OK
```

The content-addressed development overlay was materialized on the system
disk (not the nearly-full shared disk) and verified with `debugfs`. Its image
hash is `8b8e8948ac9bc85bedefe0927753ce78cf9be2a56806c72d08654f901d0ceb9b`;
the image contains
`/etc/asterinas/display-providers/fbdev/xorg.conf.d/20-asterinas-no-input.conf`.

This is software/QEMU preparation only. It is not evidence of a successful
new physical desktop boot or of GPU/DRM acceleration. Before a physical run,
the updated stage-1/initramfs and rootfs must be published, then one guarded
boot must produce fresh Asterinas and RockOS recovery evidence.

## Follow-up diagnosis, 2026-10-06

The first QEMU failure was traced to the online clock synchronizer being kept
as an `ExecStartPre` process during an offline boot. Its Python process entered
an uninterruptible wait and held `asterinas-desktop-m5.service` in
`start-pre`. The rootfs now gates that pre-start from the kernel command line;
the offline gate exits successfully, and both desktop and browser services
reach `active/running` in QEMU.

The next canary reached `xorg-start` but did not create `/tmp/.X11-unix/X0`.
An isolated root-console probe showed that a single 4 KiB read from `/dev/fb0`
does not return on the packaged QEMU 10.2.1 bochs framebuffer. This is a
framebuffer read-path stall, separate from the clock gate and Xorg service
state. The RISC-V framebuffer mapping was changed to the idempotent
`WriteCombining`/PBMT_NC policy and the kernel was rebuilt with the required
`riscv_sv39_mode` feature; the current canary still needs a boot with that
kernel and a fresh read-probe result before this change can be considered
effective.

No physical board boot was attempted. The board recovery boundary therefore
remains unchanged.

## Fresh-main QEMU replay, 2026-10-06

The newly built `riscv64/SMP=4/riscv_sv39_mode` kernel was installed into a
copy of the offline QEMU boot disk and started with the same volatile
framebuffer device tree.  The host-side QEMU process ran inside the pinned
development image for 230 seconds.  It reached framebuffer registration,
virtio block probing, `root-found`, and the complete Stage1 mount sequence,
but did not produce a second `root@asterinas-debug:` prompt or any desktop
readiness marker before the bounded timeout.  Consequently the guarded
`/dev/fb0` read was not issued in this run.

The run is retained as a failure evidence point rather than a display result:

```text
kernel: target/osdk/aster-kernel-osdk-bin.Image
boot disk: boot-pbmt-nc.ext4 (copy of the offline canary disk)
QEMU result: CANARY_RESULT=timeout
commands sent: 19 (booti was sent)
markers: DEBIAN_STAGE1_PROGRESS step=root-found
```

This separates the next investigation from the earlier Xorg/fbdev read stall:
first restore a bounded debug-console handoff with the fresh main kernel,
then issue the single timed framebuffer read, and only after both are green
consider a physical boot.  The board was not touched.

The follow-up regression set is green:

```text
python3 -m unittest tools.riscv.tests.test_megrez_clock_sync \
  tools.riscv.tests.test_debian_rootfs -q
  197 tests OK

python3 -m unittest tools.riscv.tests.test_megrez_safety \
  tools.riscv.tests.test_megrez_hardware_probe \
  tools.riscv.tests.test_megrez_desktop_boot \
  tools.riscv.tests.test_megrez_clock_sync \
  tools.riscv.tests.test_debian_browser_web -q
  147 tests OK
```

The kernel build also completes with `TARGET_ARCH=riscv64 SMP=4
FEATURES=riscv_sv39_mode`. The framebuffer read probe still needs to be
re-run with that freshly packaged kernel; the current QEMU 10.2.1 run stopped
at the debug-console handoff before producing the probe marker.
