# Firefox startup serialization and measurement check

Date: 2026-09-25.

The current-main QEMU Firefox startup check exposed two independent issues.
The desktop and browser services both invoked `desktop-m5-device-access` during graphical boot.
In two boots before the fix, one invocation failed to create `/run/asterinas-input/keyboard` because the other had already created it.
The browser service recovered on restart, but the first attempt was lost.
The helper now holds a bounded `flock` on a volatile `/run` file for the full device-setup operation.

The previous startup sampler also expected `x-socket-ready` before Firefox `exec`.
The second pre-fix boot logged Firefox `exec` first, so the sampler waited until its 450-second deadline despite a listening Marionette endpoint and an active browser workload.
The sampler now waits for the three serial-visible events in their actual arrival order and returns a failure if any endpoint is absent.
It no longer waits for `BOOT_BASIC_TARGET`, which the unprivileged timeline service may write only to its file.

The fixed development-overlay root image had SHA-256 `94ed7690035fd54bd383e3b60ec31d4e647918742774fb15a8f0f9293e7666aa`.
The rebuilt Stage1 initramfs had SHA-256 `b08c80ec84f48777026893595bd3c613f99f640e152a06dcf10894529da03649`.
The QEMU kernel Image had SHA-256 `a64142bd775fd46c958125d9a85256bd45e96d984d47695e09327bb496dbc01b`.
The [startup result](startup-profile.json) records Firefox `exec` at host elapsed 17.811 seconds, X socket readiness at 18.209 seconds, and Marionette listening at 26.643 seconds.
Its [serial log](startup.serial.log.gz) contains no input-link collision or browser-service restart.
These are TCG host timings for one startup, not a measured speedup over the earlier build.

The same inputs passed the [debug-console QEMU gate](debug-console-result.json).
It reported PID 1 as systemd, active graphical and desktop services, UID 0 on the opt-in serial console, and all ten direct-network checks.
The [serial log](debug-console.serial.log.gz) shows the root-console readiness and browser desktop readiness events.
The first attempt to run that gate from `/dev/shm` failed before QEMU emitted serial output because its `cache=directsync` disk mode did not start on that tmpfs path.
Moving only the gate's working output to the host filesystem produced the passing result; the large working disk was removed afterward.

For the next standard browser baseline, a local copy of the unmodified [WebKit Speedometer 3.1 source](https://github.com/WebKit/Speedometer/tree/1386415be8fef2f6b6bbdbe1828872471c5d802a) was pinned to commit `1386415be8fef2f6b6bbdbe1828872471c5d802a`.
RockOS fetched its `index.html` from the host over the board Ethernet link with HTTP 200.
RockOS currently runs Firefox 131.0.2 at 2560×1600; the signed Asterinas Firefox evidence uses 143.0.3, and Asterinas still inherits a 1920×1080 firmware framebuffer.
No Speedometer score was collected, and scores from those unmatched browser/display configurations would not establish an Asterinas-versus-RockOS speed ratio.
