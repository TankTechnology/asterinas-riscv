# Firefox diagnostic log channel r7

## Diagnostic channel result

This bounded headless DRM run used the Firefox-only `loglevel=7` bootargs so
the opt-in logger records are visible. The serial stream contains:

- `ASTERINAS_MMAP_DETAIL`: 128 `phase=enter` and 128 `phase=build-done`
  records, with event IDs 1 through 128 present in both sets;
- `ASTERINAS_FUTEX_WAIT_DETAIL`: 532 records;
- `ASTERINAS_FUTEX_WAKE_DETAIL`: 1024 records.

The mmap boundary is therefore fully parseable in this run; the previous r24
line-corruption concern is closed for the bounded diagnostic stream. These
records now go through the logger while ordinary desktop boots retain
`loglevel=4` and do not enable the profiles.

## Firefox result

The run did not satisfy E3. The Firefox service reported
`FIREFOX_DRM_GLXTEST rc=timeout`; the host gate ended with `reason=protocol`
before a Marionette greeting. This is a separate application-startup failure
caused by the diagnostic log volume and must not be treated as GPU acceleration
evidence.

## Artifacts

| Artifact | SHA-256 |
|---|---|
| `desktop-m4-drm.serial.log` | `1f5f8bac03adb6e96dba836d27a9a49694b5ff87cd39cbe9fe7b89f20e5f72c7` |
| `result.json` | `0492a4e6e4a24baa3e9f4419f1ad56f822bbf1a49724013b80e51e12f466cf14` |
| `lifecycle.json` | `2ef8f457d6780bda505865bf4966965b60b6fdd77f406edef33b5c642a77786e` |

Output group: `/mnt/mydisk/qemu-firefox-log-channel-r7/`.
