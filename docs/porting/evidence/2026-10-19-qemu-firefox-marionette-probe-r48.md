# Firefox Marionette syscall-argument probe r48

r48 uses the expanded syscall argument summary from r47.

## Result

The display and virgl prerequisites remain green:

```text
FIREFOX_DRM_DISPLAY_WAIT status=pass
FIREFOX_DRM_GLXTEST ... RENDERER virgl ... DRM_RENDERDEVICE /dev/dri/renderD128
```

The NewSession request still does not complete. At the captured boundary the
Firefox main thread and Web Content descendants expose current `ppoll(73)` or
`futex(98)` calls with arguments; no current mmap/open/read call remains in
the bounded samples. This means the new argument surface is ready for a future
run, but this run does not prove an ext2 read or mmap fault.

No scene, screenshot, frame-rate, or recovery phase was run.

## Artifacts

| Artifact | SHA-256 |
|---|---|
| serial log | `58e22ac34ba8ae1c62128000f272be8b6e905f2a7f338ef26e7153f0ffafa27d` |
| result | `3bec33af004974a22858627ac59ef3081d1697575767cdc9c604c403459cca8b` |
| lifecycle | `639ff07645b737a71c59a7f3a6c725e475c9e81e9bcb7fb1cb524cdce91c5cdc` |
| root image | `ad7e95a22020d0e2e01b402172185f7b41b6134fa2f0b8fbd05839959dd596ee` |
| root manifest | `cb38a652c24ea4907f5bd44403a3a27b8ad2c6f63be781a4f92dec73782d5057` |

Output group: `/mnt/mydisk/qemu-firefox-marionette-probe-r48-container/`
