# desktop-systemd-core-masks r134

## Scope

Bounded generic-QEMU diagnostic with no display device, no network, no physical
hardware, and no Firefox/Xorg workload.

## Result

- output: `/mnt/mydisk/qemu-desktop-systemd-core-masks-r134`
- serial SHA-256: `1ed6fd266d78796a36b833b9c43bfb77fc8b69a5c150f9c3633d0436f23e6137`
- lifecycle SHA-256: `3051924979e209374919e8ff7b164afc8e6ebb4e5b07c4bdc66d97caf37d5909`
- result SHA-256: `5912bc734c2d18cde60d93b7b2ba82a2bee1a57e6a853641c91ac8525fb00d8c`

Observed result: masking sysctl, sysusers and tmpfiles-dev together allowed sysinit.target.

## Conclusion

This control run does not close sysinit; masking sysctl, sysusers and tmpfiles-dev together allowed sysinit.target. It supports the r135 isolation and must not be treated as a Firefox or desktop result.
