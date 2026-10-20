# E3 Firefox short-probe budget contract r124

The r123 A/B never reached the Firefox service because the desktop launch
itself consumed the 180-second host budget.  This bounded code change makes a
future short probe observable and prevents a service-level constant from
silently overriding the host-selected budget:

- `ASTERINAS_FIREFOX_PROBE_TIMEOUT` is validated to 10..600 seconds;
- the host gate adds the selected value to `systemd.setenv` and requires the
  host budget to be guest budget + 60 seconds;
- `firefox_marionette_probe.service` no longer hard-codes 300 seconds;
- the guest probe logs the exact canonical NewSession JSON, byte count, and
  SHA-256 before sending it.

The new overlay was materialized without starting QEMU:

```text
output=/mnt/mydisk/asterinas-firefox-rootfs-20261020-capab-ab-r122c
root_image_sha256=5d7e10e83e14b617505c091cac7ca320d08154d60ba0e6299deed6406a84bffc
overlay_manifest_sha256=f73eaade222f2e658142c62369887d085f515ce72c0078c367fb10dd444f46ea
```

Static validation passed: 38 Firefox gate unit tests, Python compileall for
the rootfs/tests trees, and `git diff --check`.
