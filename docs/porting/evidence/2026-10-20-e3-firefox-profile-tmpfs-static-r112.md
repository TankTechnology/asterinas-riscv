# E3 Firefox profile tmpfs hypothesis r112

- gate: `e3-firefox-profile-tmpfs-static`
- claim: E3 diagnostic preparation
- status: `pass` for static contract; runtime `pending`

The r111c serial receipt showed the Firefox Web Content child in `D (disk sleep)` immediately after NewSession was sent. The minimal host/guest change adds an opt-in `ASTERINAS_FIREFOX_PROFILE_DIR` path for probe-only boots; when set to an absolute safe `/run/...` path, profile creation and Firefox `--profile` use the runtime-backed directory. The normal scene keeps `/home/asterinas/.mozilla/asterinas-firefox-drm-scene`.

Static validation: 38 Firefox gate unit tests, Python compilation and `git diff --check` pass.

Runtime was intentionally not started: `/mnt/shared` has 58 GiB free, below the 60 GiB precondition for a new QEMU gate. No new rootfs was materialized and no guest result is claimed.
