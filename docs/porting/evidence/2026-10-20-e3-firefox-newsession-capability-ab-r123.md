# E3 Firefox NewSession capability A/B r123

This was the single bounded A/B requested after the r122 history
reconciliation.  Both arms used the same kernel, U-Boot, DTB, Stage1,
rootfs, QEMU GL render node, and 180-second host budget.  The only requested
variable was the NewSession capability arm:

| Arm | `ASTERINAS_FIREFOX_NEWSESSION_CAPS` | Output directory | Terminal state |
|---|---|---|---|
| A | `minimal` | `/mnt/mydisk/qemu-firefox-e3-r122-a` | failed at host protocol budget |
| B | `legacy` (three top-level fields) | `/mnt/mydisk/qemu-firefox-e3-r122-b` | failed at host protocol budget |

## Boundary result

Neither arm emitted `FIREFOX_DRM_PROBE_NEWSESSION_REQUEST`,
`FIREFOX_DRM_PROBE_BOUNDARY`, or a Marionette greeting.  Both serial logs
reached the same last ordered kernel marker:

```text
ASTERINAS_BOOT_PHASE phase=init-spawned
```

The output lifecycle files ended with `status=failed`, `phase=finished`,
`reason=protocol` after the host-side 180-second boundary.  The serial tails
contain only startup syscall/futex diagnostics followed by the explicit
QEMU termination signal.  Since neither request body was sent, this run does
not decide whether minimal or legacy capability negotiation is causal.

## Inputs and output hashes

Both arms used the same input set:

```text
kernel 54dd701c273b296c783269973bb1328abcecc6c0a0aea2a3c14bd13e8a6badad
rootfs ff510634d6bbfd3b20c2e44e7e5a555e49898b6e22647af6081f2df7f26d9c7f
dtb    d6e8cf8a76188f7f247568c1df255ff699fefd40d74f2fe063b62ffa3381e3c2
stage1 ce7fb996e9a6ac48160d81c29347a68d88e99204adcc147051df76f645031c84
```

| Arm | `lifecycle.json` | `result.json` | serial log |
|---|---|---|---|
| A | `943abb4419a85a7ec1c0fe6252cf79f2d5bb2fbccf50b8c84a45ff5de3cfae21` | `58a697da2bacca2d880e2269145c4351fbac10854a47b35a456071d780f5027c` | `2a928644e3cf388d67da194f190dc781df9292bef0a66fd4b48dc222ebe4f4a6` |
| B | `00dff72903d253b4172638cb4d1fdfc9483d4dffe8ce45d51d10a0baca3ad5e5` | `81069da2b274d7de72aeabd3514ce0b8657c71fc30bb063bea8e52e024fb69bab` | `a8e3e529f0746eddf3ae01da5e5f2373c483a2ac882cf65e4d8518b5dc34887f` |

## Decision

The capability hypothesis remains **unresolved**.  The useful next boundary
is the cold desktop launch itself: make that boundary independently bounded
and observable before spending another capability run.  Do not treat this
receipt as an E3 acceleration pass, and do not repeat this same A/B without a
changed launch boundary.
