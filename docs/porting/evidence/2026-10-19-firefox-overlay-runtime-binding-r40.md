# Firefox Marionette runtime binding r40

The overlay audit found that the guest scene script imports
`/usr/lib/asterinas/browser_m5_marionette_gate.py` before falling back to the
repository source. The first r40 materialization therefore would have kept
the old client despite the host-side `select` change. The overlay now includes
the shared client explicitly and r41 was materialized with three files.

## Bound artifact

| Artifact | SHA-256 |
|---|---|
| root image | `c8bd5b2508f453bb4ac630d215738832a062f9158e55d9b9f6f2e801a44e8550` |
| root manifest | `d2b6c2061f224f7b1b832a86c83eecbc6e3f954f98e12b27d9364abb54eac0c9` |
| overlay manifest | `a156c6476a6c9230031b9dece1d110635361a1ed29176050f16bcd1b872296fd` |
| shared Marionette client | `e7aa1bcf71e249ed23c423dddf67165d54faa3c00b15f10ffd13af0f6288d760` |

Output: `/mnt/mydisk/asterinas-firefox-rootfs-20261019-r41/`

This is still a host/materialization contract, not a Firefox runtime result.
The next QEMU probe remains gated on `/mnt/shared` having at least 60G free.
