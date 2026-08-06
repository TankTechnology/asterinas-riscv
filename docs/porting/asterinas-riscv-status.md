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
| Sv39/Sv48 with Svadu/Svade | integration pending | downstream validation required |
| QEMU SiFive U firmware boot | integration pending | downstream validation required |
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
