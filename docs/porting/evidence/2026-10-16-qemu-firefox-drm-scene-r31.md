# QEMU Firefox DRM scene r31

r31 added a bounded MOZ-log diagnostic. The fixed scene, DOM completion, and
screenshot path all reached their gates, but Firefox's MOZ log was exactly
zero bytes:

```text
FIREFOX_DRM_MOZLOG bytes=0 sha256=e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 webrender=0 rendercompositorogl=0 virgl=0 swgl=0 llvmpipe=0 softpipe=0
```

The strict renderer rule therefore failed closed. This does not distinguish
an unconfigured MOZ logger from a software compositor. The next run adds a
real WebGL `UNMASKED_RENDERER_WEBGL` probe to the local scene and requires its
renderer to be reported as virgl independently of the Xorg receipt.
