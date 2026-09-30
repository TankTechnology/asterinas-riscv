# Megrez PowerVR configuration-heap board check (2026-09-30)

The selected Asterinas desktop boot ran release Image SHA-256
`a17b29237ecd7723e12778c1a4bf50fc1d8ae7d48dd590cc9d7af04bf00efabf`,
built from main commit `947766861`. The one-shot RockOS/U-Boot handoff checked
the Image, Stage1 initramfs SHA-256
`666d58af0673e2af52c9f1f36b5438095c759d95b3efcd800180054606bb3d71`,
and DTB SHA-256
`465cb129333cc334a3161fabb43d37df8738ac4baa86fcdb50260691a22a84ba`
before boot. RockOS boot ID before the handoff was
`2a5d3bd7-d0e0-45c5-af9c-7ce8c704592a`. Its persistent default boot entry
was not changed.

The selected kernel command line included both `asterinas.powervr=1` and
`asterinas.powervr_dma_stage=1`. The Asterinas boot ID was
`19fab661-4684-46e2-8194-085ec07ce96f`. Fresh nonce-framed root-console
commands checked UID 0 and this boot ID; after closing and reopening the
serial connection, commands still worked. The debug-console and browser-web
services were active, the desktop-ready marker matched the boot ID, and Xorg
reported the fbdev desktop at 1920×1080. This checks the system and control
path, not the actual HDMI pixels.

The [sanitized DMA transcript](stage.serial.log) records four firmware
segments with CPU readback `ok` (52,064, 18,432, 73,312, and 9,984 bytes).
It reports a prepared GPU MMU root and mapped connection-control, OSINIT, and
SYSINIT pages at `0xe1c1fd0000`, `0xe1c1fe0000`, and `0xe1c1ff0000`.
Closing the device restored the original CRG values. The log explicitly says
`fw_config_initialized=0`, `gpu_root_installed=0`, and
`gpu_visibility=unverified`: **the GPU did not execute firmware or render a
pixel in this test**.

The first two one-shot handoffs omitted one of the required PowerVR flags,
which prevented staging. The guest-side staging command now checks both flags
before reading firmware frames or opening the control device. The corrected
third handoff is the successful transcript above. Recovery of this exact
Asterinas boot back to RockOS has not yet been checked; earlier one-shot
handoffs did recover. The root debug console was responsive after staging.

The next hardware gate is a complete FWIF object and dependent allocations,
GPU-private root installation, and bounded META master boot with a
firmware-started handshake, timeout/fault evidence, and a verified recovery
path. Command submission, fence completion, and readback of a known pixel must
then precede any desktop or Firefox GPU-acceleration claim.
