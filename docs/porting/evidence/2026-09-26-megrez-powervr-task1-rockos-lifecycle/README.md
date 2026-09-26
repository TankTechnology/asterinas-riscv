# Megrez PowerVR reference software and lifecycle

This is a **RockOS reference experiment**, not Asterinas GPU rendering. The
matching `6.6.87-win2030` kernel was selected for one boot from U-Boot; the
persistent RockOS default was unchanged. The [manifest](manifest.json) pins the
seven files used by the reference firmware and EGL/GLES/GBM stack, including
their package versions, sizes, and SHA-256 digests. The earlier 41-digit shader
digest was a transcription error; the independently recomputed digest is
`9b7a9779c982ef3c29be2f77378c6b037fc264b978f9416eadc6cee553bba67b`.
No proprietary binary is committed to Git.

The [bounded kernel transcript](bounded-kernel.log), with trailing log spaces
trimmed, starts at
`ASTERINAS_PVR_TASK12_e50c734dd54381c9_START` and ends at the matching `END`.
After stopping LightDM, `modprobe -r pvrsrvkm` completed, the module and render
node were absent, and `SysDevDeInit` reported GPU reset asserted and clocks
disabled. Reloading the module registered BVNC `30.3.408.101`; the next client
open loaded `rgx.fw.30.3.408.101` and `rgx.sh.30.3.408.101`. The unchanged
16 × 16 probe then returned the PowerVR renderer, white left pixels, and black
right pixels. Its [188 bridge calls](bridge-status.log) covered 26 functions;
the [summary](bridge-summary.json) records zero outer ioctl failures and zero
inner bridge errors. LightDM and the render node were present again after the
experiment. The exact outcomes are in [result.json](result.json).

`SysDevDeInit` and the absent module/render node establish the driver and
hardware shutdown boundary. The kernel log does not print a separate firmware
shutdown acknowledgement. In the pinned RockOS
[`pvrsrv.c`](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/services/server/common/pvrsrv.c),
device teardown calls `MMU_DeInitDevice`, `DevDeInitRGX`, and then
`SysDevDeInit`; initialization calls `SysDevInit`, physical-heap setup,
`MMU_InitDevice`, and `RGXInit`. This makes a userspace-only bridge proxy
insufficient: Asterinas must still supply firmware, GPU MMU, memory, interrupt,
and power ownership. A root-only service can translate the observed client
requests after those kernel facilities exist; the 26 commands cannot be empty
success stubs.

For a **local, isolated staging copy** on the matching RockOS installation,
copy `rockos-files.sha256` to the board and run as root:

```sh
sha256sum -c rockos-files.sha256
stage=/home/debian/asterinas/powervr-reference
awk '{print $2}' rockos-files.sha256 | while IFS= read -r path; do
    install -D -m 0644 "$path" "$stage$path"
done
awk '{print $1 "  ." $2}' rockos-files.sha256 |
    (cd "$stage" && sha256sum -c)
```

The private staging tree preserves source paths and leaves RockOS's installed
libraries and the Asterinas root filesystem untouched. It is input material
for the later firmware and bridge tasks; installing these files alone does not
create an Asterinas PowerVR render device.
