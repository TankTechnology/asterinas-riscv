# DRM/main performance candidate on Megrez

The board is running the candidate built from
`codex/drm-main-perf-20260925` at
`a51dee42bdf91a82b9e54e63a4195dbe072e0aea`.
The release Sv39/SMP4 kernel Image SHA-256 is
`92e46db13dc2c23ecbdcf4b7eb67b8aa27b8a259f5cf99461e6c9c0eeeefabe7`.
It booted with the previously verified Stage1
`ea446515661f2380568426b3c37da0b6a0698fc51f88d32c62807fce04890962`
and prepared 1920 × 1080 Megrez DTB
`465cb129333cc334a3161fabb43d37df8738ac4baa86fcdb50260691a22a84ba`.

The current Asterinas boot ID is
`613f0ade-3352-45f3-bbd9-6b7d4959521f`.
The selected local debug console returned UID 0 and that boot ID through
`/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0`.
The host closed and reopened the serial descriptor, then obtained a fresh
nonce-framed UID 0 response with the same boot ID.
The 420-second software recovery timer was armed for the boot and disarmed
only after the DRM card, Xorg at 1920 × 1080, and Firefox service were ready.
Firefox had MainPID 129 and `NRestarts=0` at admission.
The board remains booted; the host does not hold its serial port.

The artifacts were placed as new files on RockOS partition 3 under
`/home/debian/asterinas/scanout-perf-a51dee42b/`.
RockOS verified all three SHA-256 sums before and after the directory was
atomically renamed into place.
U-Boot loaded each artifact from `mmc 1:3` and verified its byte count and
CRC32 before `booti`.
No existing image, Stage1, DTB, partition-2 root filesystem, U-Boot
environment, or menu entry was replaced.
The persistent selector `/boot/extlinux/asterinas.conf` retained SHA-256
`02280720efe7a1ad0ac084cdc20429406631e12d2e16f05638544bab0883fb26`.
An ordinary reboot still defaults to RockOS.

There were two controlled candidate boots.
The first reached the root debug console, DRM desktop, and Firefox, and
actually disarmed the timer; the host script incorrectly rejected the
two-line response `ASTERINAS_SOFTWARE_REBOOT_DISARMED` followed by `0`.
Its recovery handler requested a software reboot and observed RockOS return.
The parser was corrected to check the final value, and the second boot passed
the full readiness and serial-reopen checks.
This was a host verification-script error, not an observed kernel failure.

The new `ASTERINAS_DRM_SCANOUT` log was read from the running kernel.
At successful-present count 41 it reported one 8,294,400-byte full present
in 78.627 ms and 40 dirty presents copying 79,241,304 bytes in 773.250 ms
cumulatively; the largest dirty present was 80.688 ms.
Between counts 36 and 41, five dirty presents copied 648,000 bytes in
6.371 ms cumulatively, or 1.274 ms per present for that short interval.
This demonstrates that partial updates can be much cheaper than a full
1920 × 1080 copy on this board.
The numbers cover startup and mostly idle display updates; they do not
measure an interactive Firefox scroll or establish a user-visible speedup.
The Firefox main thread's cumulative `schedstat` was
`23836467000 1346867000 2553` when sampled, but no active-use delta was
collected, so it should not be treated as a workload wait ratio.

Private local artifacts are under
`/home/ubuntu/.codex/asterinas-board-prepare-20260925/`:
`manifest.json`, `SHA256SUMS`, `boot-commands.txt`,
`candidate-boot.result.json`, `candidate-boot.serial.log`, and
`live-scanout-snapshot.txt`.
The first attempt's separate result and serial log remain there too.
No credentials are recorded in this note.
