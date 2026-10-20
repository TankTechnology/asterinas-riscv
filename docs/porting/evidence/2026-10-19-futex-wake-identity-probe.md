# Futex wake identity probe

The existing `pthread_cond_handoff` regression was rerun with
`asterinas.futex_profile=1`.  It passed all 4096 handoffs while emitting the
new pid/tid wake markers.

Parsed from the QEMU serial log:

| Measure | Value |
|---|---:|
| wake records retained | 128 (diagnostic cap) |
| private wake records | 122 |
| shared wake records | 6 |
| `woken=1` | 57 |
| `woken=0` | 71 |
| dominant test process | pid 10 |
| dominant futex address | `0x3ffff1dbd0` (62 records) |

The successful handoff result and nonzero wake count show that the added
identity fields are usable and do not break the futex path.  The `woken=0`
records are expected for wake attempts with no waiter at that instant; this
probe does not establish the Firefox failure because it uses a different
process topology and futex addresses.

## Artifacts

| Artifact | SHA-256 |
|---|---|
| kernel | `0510a722cd317911013387a2a6f983733147319852b610d8c6628a5c465a8b5a` |
| `qemu-serial.log` | `222f79b7d543523d97a10b26ad21ee0ab27af19f818f89175ae3f2199e2313d3` |
| `qemu.log` | `757d41a6fc49086126973a841c47b8e9dbe0edbaacf8ad3ec946f2bd1d2c7fe9` |

Command:

```text
make run_kernel TARGET_ARCH=riscv64 SMP=4 AUTO_TEST=pthread_cond_handoff
  ... --kcmd-args="asterinas.futex_profile=1"
```
