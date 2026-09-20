# Firefox daily-use physical baseline

## Scope

Exactly three qualified, recovered, one-profile-per-boot Megrez runs were admitted.
Browser timings are not USB-to-HDMI latency, and procfs placeholder fault fields were not used.

## Immutable identities

```json
{
  "boardSerial": "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0",
  "bootargsSha256": "fefdf884880a5e1d044b034d1801a3c22951ce6729d18cbf125e64813e32631b",
  "browserPackage": "firefox-esr@packages-lock:f3f63d893702f7a1a97d2cd7fdcf063de475069f437cf8af25cefa875b4e910a",
  "commit": "55ee5c64e24981ff10a9f76c77b5ae1f875a1eae",
  "displayProvider": "fbdev",
  "dtb": "465cb129333cc334a3161fabb43d37df8738ac4baa86fcdb50260691a22a84ba",
  "fixture": {
    "allowedPeer": "10.100.19.200",
    "bindAddress": "10.100.19.216",
    "payloadBytes": 65536,
    "payloadSha256": "7daca2095d0438260fa849183dfc67faa459fdf4936e1bc91eec6b281b27e4c2",
    "port": 17894
  },
  "hartCount": 4,
  "kernel": "84246481e4eca725c4092939866a9c5a340e8e2896c3f49f8f90ceecca4ceaf8",
  "rootfsManifest": "aed14b710fe8f5fb9ac78b4b83ccb967c0b36ad58e3b822bf560459929b5b893",
  "stage1": "9bcf5f7b2f2755c98fa3c4461314a876b5a2c3551c3746fa3a0a614b08fceafb"
}
```

## Daily-use capability coverage

- document: pass
- storage: pass
- execution: pass
- rendering-media: pass
- navigation: pass
- download: pass
- contexts: pass

Daily-use limitations:

- guest-and-browser-clocks-separated
- kernel-diagnostics-unavailable
- physical-scanout-unsupported
- public-network-excluded
- synthetic-input-timing

## Primary metrics (ms)

| Metric | Run 1 | Run 2 | Run 3 | Supported | Median | Min | Max |
|---|---:|---:|---:|---:|---:|---:|---:|
| keyboardFirstRafP95Ms | 23.000 | 22.000 | 73.000 | 3/3 | 23.000 | 22.000 | 73.000 |
| keyboardNextRafP95Ms | 41.000 | 53.000 | 77.000 | 3/3 | 53.000 | 41.000 | 77.000 |
| pointerFirstRafP95Ms | 98.000 | 122.000 | 78.000 | 3/3 | 98.000 | 78.000 | 122.000 |
| pointerNextRafP95Ms | 147.000 | 130.000 | 78.000 | 3/3 | 130.000 | 78.000 | 147.000 |
| scrollFirstRafP95Ms | 62.000 | 174.000 | 74.000 | 3/3 | 74.000 | 62.000 | 174.000 |
| scrollNextRafP95Ms | 72.000 | 182.000 | 78.000 | 3/3 | 78.000 | 72.000 | 182.000 |
| navigationCommandMs | 116.504 | unsupported | 137.799 | 2/3 | unsupported | unsupported | unsupported |
| navigationResponseToDomMs | 241.000 | unsupported | 204.000 | 2/3 | unsupported | unsupported | unsupported |
| contextSwitchTotalMs | 951.791 | 1256.545 | 1215.464 | 3/3 | 1215.464 | 951.791 | 1256.545 |

## Attribution and admission

Classification: **runnable-delayed** (3/3 per-run agreement).
This is a mechanism-class admission rule, not proof of a specific function or subsystem.

| Run | Classification | Wait ratio | Main runtime share | CPU occupancy | Unaccounted |
|---|---|---:|---:|---:|---:|
| 1 | runnable-delayed | 0.3531 | 0.4446 | 1.0926 | 0.0000 |
| 2 | runnable-delayed | 0.2842 | 0.4762 | 1.1397 | 0.0000 |
| 3 | runnable-delayed | 0.3065 | 0.4620 | 1.1785 | 0.0000 |

## Inputs and hashes

- `target/firefox-daily-use-physical/release-jit-crossarch-run-3` — `9c055489781d599410eb0cd4404d92d13c636b2b592ee9f26b0204a0bdb65f37`
- `target/firefox-daily-use-physical/release-jit-crossarch-run-4` — `eb5e0e3a8f10fa3b134bd4cfbb9328360125c68102df5bab1bf4770089e2c8da`
- `target/firefox-daily-use-physical/release-jit-crossarch-run-5` — `a615b965b828e36f1c448e5440dc5d6211d3fafabd98288248da942ec31d5d03`

## Limitations

- mechanism-class-admission-is-not-proof-of-a-specific-function-or-subsystem
- browser-timings-are-not-usb-to-hdmi-latency
- procfs-placeholder-fault-fields-were-not-used
- primary-metric-aggregates-require-three-supported-runs
