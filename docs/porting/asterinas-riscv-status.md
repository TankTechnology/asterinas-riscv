# Asterinas RISC-V downstream

`asterinas-riscv` is an independently maintained RISC-V downstream of
Asterinas. Its default branch is a tested integration line that periodically
merges changes from `asterinas/asterinas:main`.

## Upstream relationship

We do not plan to proactively submit pull requests to the upstream Asterinas
repository. Upstream maintainers are welcome to reuse changes from this
repository under the project's license. If they request assistance with a
specific change, we remain willing to provide technical context, validation
evidence, or implementation support under an agreed scope.

## Support status

| Environment | Status | Evidence |
| --- | --- | --- |
| RISC-V QEMU virt | inherited upstream baseline | upstream RISC-V CI |
| Sv39/Sv48 with Svadu/Svade | currently verified | downstream boot matrix on the reconstructed `main` |
| QEMU SiFive U boot (Asterinas) | currently verified | `make test_riscv_sifive_u` (Sv39 kernel, userspace marker) on the reconstructed `main` |
| QEMU SiFive U boot (Linux 6.12 reference) | currently verified | `make test_riscv_sifive_u_linux_reference` (Linux boots to `ASTERINAS_LINUX_REFERENCE_READY`) on the reconstructed `main` |
| QEMU virt display (simple-framebuffer -> VT console) | currently verified | `tools/riscv/qemu_desktop_boot.py` (bochs-display, framebuffer registered, VT renders 1280x1024) on the reconstructed `main` |
| EIC7700 L3 cache isolation | currently verified | `tools/riscv/eic7700_isolation.sh` (negative=0 / positive=1 registrations, pure QEMU) |
| Milk-V Megrez | previously verified | `docs/porting/evidence/` |

Entries move to **currently verified** only after validation is reproduced on
the reconstructed `main`. Historical board results remain explicitly marked as
**previously verified** when current board access is unavailable.

## Synchronizing upstream

Fetch and merge upstream without rewriting published history:

```bash
git fetch upstream main
git switch main
git merge --no-ff upstream/main
```

Run the RISC-V validation matrix before pushing the merge to `origin/main`.
