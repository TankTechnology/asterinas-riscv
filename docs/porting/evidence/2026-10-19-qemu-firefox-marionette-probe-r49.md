# Firefox Marionette capability-shape probe r49

r49 uses the complete capability object that succeeded in the older r34
runtime (`acceptInsecureCerts`, `pageLoadStrategy=none`, and
`strictFileInteractability`). Display readiness and virgl setup are unchanged.

## Result

`xdpyinfo` and glxtest pass with `virgl` and `/dev/dri/renderD128`, greeting
passes, but NewSession still times out. The capability-shape hypothesis is
therefore rejected for the current guest/kernel state.

No scene, screenshot, frame-rate, or recovery phase was run.

## Artifacts

| Artifact | SHA-256 |
|---|---|
| serial log | `bc53ae2b0f0464372d896136d56e5fd3abf0174f7769413ce3220b9e77af653f` |
| result | `76cbe1f52548fe78673313b3981b19e6e8eb344ebdf9158b835842ac182fead4` |
| lifecycle | `bf67d5bc09149a305c2b566c73d4d6fb29047f9035ce8cef9bd65993f0b1bb89` |
| root image | `62510146ff58cafd421d9660a01b2a6e3dde29bfdc458cee85a2ce0729df7932` |
| root manifest | `1759e93550a96f7de4230b5586facb97145954c78172473a0ef8c9954784e1db` |

Output group: `/mnt/mydisk/qemu-firefox-marionette-probe-r49-container/`
