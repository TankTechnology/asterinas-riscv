# E3 kernel completion-yield experiment r11 (2026-10-18T14:57:01Z)

This run used kernel `7e4e944c...`, explicit MTTCG, and the original official
Firefox rootfs. The new virtio-gpu completion-yield change was intentionally
tested once with the watchdog/recovery chain.

## Failure and recovery

The serial receipt reaches the Debian handoff and virgl/renderD128 probes, then
records:

```
MIGHT_SLEEP_VIOLATION ... caller=.../kernel/comps/virtio/src/device/gpu/device.rs:109:9 ... preempt_count=1
ATOMIC_GUARD_TAKEN file=.../ostd/src/sync/spin.rs
PANIC_ENTRY ... unwind=true
OpenSBI v1.7
U-Boot 2026.07
```

`Task::yield_now()` was called while `control_queue`'s spin guard was held.
The kernel correctly rejected sleeping in an atomic section and recovered to
U-Boot. No Firefox scene, screenshot, frame, or WebRender receipt was produced.
The change has been removed from the working source; the original bounded spin
path remains in place until a lock-safe wait design is implemented.

## Durable artifacts

- serial: `target/e3-render-policy-r1/qemu-r5-export/firefox-drm-recovery.serial.log` (SHA-256 `0a3e8b410053db062191dc10f6ed7050c879f338a2b6d50e4234cf8e15b5e7db`)
- result: `target/e3-render-policy-r1/qemu-r5-export/result.json` (SHA-256 `a5b1f84c08a5f403cf0a645b112387e57cecf9b86dd1e78eabd775800f94c625`; protocol failure)
- lifecycle: `target/e3-render-policy-r1/qemu-r5-export/lifecycle.json` (SHA-256 `e7471472413c230ac0f6674acf7d9e3106a59d7ea16bd1c9d1c51caea65d981d`; terminal failed)
- manifest.lock SHA-256: `68645b154bb876db055a7e49b32a55b0f26e3a4bab44e89be4b2223f7abd39f8`
- kernel SHA-256: `7e4e944c7634d0d22310b82cd125bbf747ef2c69798da01e0a1228ef8f4ff15d`
- root image SHA-256: `aa26ef744b3c0e8568bbbaafbbfe300199ef14e354a698410b8b73bab4b80ad7`

No physical board was accessed. The watchdog/recovery chain remained intact.
