# E3 official Firefox overlay preseed

## Gate

`e3-official-firefox-overlay-preseed`: derive a disposable rootfs from the
frozen `desktop-m4-firefox` rootfs without debootstrap, package downloads, or
Firefox source changes. The derived image must retain the base package lock,
pass the frozen-root validation, and bind every replaced file to the overlay
manifest.

## Materialized artifact

- Base rootfs: `target/e3-render-policy-r1/rootfs`
- Derived rootfs: `/mnt/mydisk/asterinas-firefox-rootfs-20261019-preseed`
- Profile: `desktop-m4-firefox`
- Base image SHA-256: `aa26ef744b3c0e8568bbbaafbbfe300199ef14e354a698410b8b73bab4b80ad7`
- Derived image SHA-256: `59f3b672134c19c221dbcb6f996e90764f8165ed5ff62b58f6cea7e44a22fbce`
- Overlay specification SHA-256: `21ffda96fa8924cd1654d4b1762b35d0dbe31bf15f76999a02b511548163bd4c`
- Derived overlay manifest: `/mnt/mydisk/asterinas-firefox-rootfs-20261019-preseed/dev-overlay-manifest.json`

The overlay replaces exactly one regular file:

| Destination | Source SHA-256 | Purpose |
|---|---|---|
| `/usr/lib/asterinas/firefox-drm-scene-gate` | `e39c71c6790e4a211b5a21bf8cfc4c48480b1d4877f0cf52b636934c001f019b` | Official Debian Firefox scene gate with bounded startup/network prefs. |
No Firefox binary, package, or source patch was added. The base scene HTML,
Marionette helper, and `firefox-drm-scene.service` remain byte-identical to the
frozen rootfs. Keeping the service file from the base is required because its
`Requires=asterinas-desktop-m4.service` dependency is the service present in
this profile.

## Verification

```text
python3 -m unittest tools.riscv.tests.test_debian_dev_overlay
Ran 24 tests in 5.466s
OK

validate_frozen_root(derived image, derived manifest, packages.lock)
DERIVED_ROOT_VALIDATE=pass
```

## Result

The overlay preseed gate passed. This is a static artifact gate only; it does
not claim that Firefox WebRender or the fixed scene has run. An initial r16
runtime of the first draft reached `DEBIAN_DESKTOP_M4_READY` but emitted no
`FIREFOX_DRM_*` marker because that draft replaced the service dependency with
the absent `asterinas-desktop-m5.service`; that draft image SHA was
`fb1168bbb11669b51578fdb5bd5cfac40d5f48105949c5ff7ac26b52107ae4d5` and is
not an accepted E3 input. The corrected image retained the M4 service and was
used by r17; r17 reached Firefox `new-session` but hit the same Marionette
TCP-connect boundary. Its runtime receipt is
`docs/porting/receipts/2026-10-19-e3-firefox-r17-marionette-timeout.json`.
Further E3 runtime retries are frozen until the Firefox/Marionette startup
condition changes.
