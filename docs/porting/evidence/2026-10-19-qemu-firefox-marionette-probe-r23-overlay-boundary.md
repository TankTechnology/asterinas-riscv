# Firefox Marionette probe r23 overlay boundary

r23 kept the detached host runner and QEMU alive, and the host selected
probe boot arguments.  The rootfs, however, still contained the ordinary
`firefox_drm_scene.service`; the probe service overlay had not been
materialized.  Consequently the guest ran the full scene script and emitted
`FIREFOX_DRM_PHASE`, with no `FIREFOX_DRM_PROBE_*` markers.  The run ended at
the same NewSession timeout and is recorded as `inconclusive`, not as a guest
probe result.

Artifacts: `/mnt/mydisk/qemu-firefox-marionette-probe-r23-container/`

| Artifact | SHA-256 |
|---|---|
| serial log | `8847e55d2a59687426e4834aefb49c57b834322ce4ec3a1e6ad13cc6391ba527` |
| result | `4841c045b6f172840f693a55469bc6fe224e7e35d44da440f76507443af2f093` |
| lifecycle | `db4a6fe1fd0dcae27663dec6be9306a7c920a36da7dc19ab7cd435a0dcececc9` |

