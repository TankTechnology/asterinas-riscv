# Firefox Marionette independent probe r43

r43 is the bounded probe with the corrected shallow process-tree collector.
The guest contains the select-based Marionette client and the tree diagnostic
in the overlay.

## Result

The greeting and complete NewSession request still traverse the guest TCP
path, but no response header arrives. The final Firefox parent remains alive
with 50 threads. The only observed Firefox child is:

```text
pid=447 name=Socket Process state=S (sleeping) threads=5
relation=direct depth=1 syscall=number=73 (ppoll)
```

No Content, RDD, GPU, or Renderer descendant exists in the bounded process
tree at the NewSession boundary. This moves the failure boundary before those
child processes are created, in Firefox parent/renderer initialization. It is
not a TCP delivery or Socket Process response failure.

No scene, screenshot, WebRender, frame-rate, or recovery phase was run.

## Artifacts

| Artifact | SHA-256 |
|---|---|
| serial log | `f6730432f680229f1565fadd9cd3d3215db0bec2078dc83412d40881a9d04a6c` |
| result | `07a6b611eb7b64a74e9610f051e3c97fe797852b5fc7bc38476f2921114509bc` |
| lifecycle | `200a54cdb8a760aa43154cf2a37af86891bf5c3c3323d1a1d7624f7c617f7aec` |
| root image | `dc6b592576283deec5e99b5f9f89cf6fd943ab9b6b566a1a61f161d15221e639` |
| root manifest | `bc0b956c08655aa659a5d353a211c0203f0d3db7739fca1acc73d04e7873e1f9` |
| kernel | `829f9fa348d15bc3826c5543aea0ade0408c241d816e4d48db53de4007249297` |

Output group: `/mnt/mydisk/qemu-firefox-marionette-probe-r43-container/`

Gate: `firefox-marionette-probe`; claim: `E3`; status: `blocked`.
