# QEMU Firefox DRM scene r48

Date: 2026-10-17 (Asia/Shanghai)

## Result

**FAIL CLOSED before Firefox.** This single diagnostic changed only the
temporary DRM Xorg configuration from `DefaultDepth 24` to `DefaultDepth 32`;
the r47 GLES/hardware-feature profile was retained. Xorg exited during startup,
so no Firefox scene, renderer, screenshot, or frame receipt exists.

The decisive serial evidence is:

```text
(EE) Server terminated with error (1). Closing log file.
BROWSER_WEB_DESKTOP_STAGE=xorg-failed ... reason=process-exited
```

Therefore depth 32 is not a viable guest display configuration and is not a
Firefox fix. The repository configuration remains unchanged (`DefaultDepth
24`).

- Serial log: `serial.log`
- Structured result: `result.json`
- QEMU only; no physical board access
