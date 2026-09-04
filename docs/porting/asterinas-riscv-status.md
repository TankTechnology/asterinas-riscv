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
| RISC-V QEMU virt | inherited, rerun required for HEAD | upstream RISC-V CI |
| Sv39/Sv48 with Svadu/Svade | last verified; unbound to HEAD | downstream boot-matrix evidence |
| QEMU SiFive U boot (Asterinas) | last verified; unbound to HEAD | `make test_riscv_sifive_u` (Sv39 kernel and userspace marker) |
| QEMU SiFive U boot (Linux 6.12 reference) | last verified; unbound to HEAD | `make test_riscv_sifive_u_linux_reference` (`ASTERINAS_LINUX_REFERENCE_READY`) |
| QEMU virt display (simple-framebuffer -> VT console) | last verified; unbound to HEAD | `tools/riscv/qemu_desktop_boot.py` (framebuffer registration and 1280x1024 VT rendering) |
| EIC7700 DT registration isolation | last verified in QEMU only | `tools/riscv/eic7700_isolation.sh` (negative=0 / positive=1 registrations); no physical cache-behavior claim |
| Milk-V Megrez | previously verified on frozen candidates | `docs/porting/evidence/` |

An entry may say **currently verified** only when it names the full source
commit, run date, container image ID or digest, and retained result/evidence
path. A branch name or the phrase "reconstructed main" is not sufficient,
because it changes independently of the tested artifacts. Historical board
results remain **previously verified** when current board access is unavailable.

## Synchronizing upstream

Fetch and merge upstream without rewriting published history:

```bash
git fetch upstream main
git switch main
git merge --no-ff upstream/main
```

Run the RISC-V validation matrix before pushing the merge to `origin/main`.
