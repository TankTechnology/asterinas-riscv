# Asterinas RISC-V downstream

`asterinas-riscv` is an independently maintained RISC-V downstream of
Asterinas. Its `main` branch is our product integration line. We selectively
port relevant changes from `asterinas/asterinas:main` while retaining our
published history and Megrez-specific development.

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

The two main branches last shared commit `4bad9d34a` on 2026-08-05. As of
2026-09-27, official `main` is `3eb661e12` and downstream `main` before the
current intake is `a53c5212a` (344 official-only and 1856 downstream-only
commits). A wholesale history merge would mix unrelated work and make board
regressions difficult to isolate. Keep one main development checkout and take
small, independently verifiable changes instead:

1. Fetch official `main` and record its commit ID. Compare each candidate with
   our implementation; classify it as already covered, applicable, requiring
   adaptation, or irrelevant to our supported platforms.
2. Port one coherent change at a time. Preserve the official commit ID in the
   downstream commit message and keep board-specific extensions explicit.
3. Run the focused regression first, then build and boot RISC-V QEMU. Changes
   that affect Megrez boot, storage, or display need the corresponding physical
   gate before being reported as board-verified. Do not infer current board
   status from older artifacts.
4. Push only the tested downstream commit to `origin/main`. Do not keep a
   long-lived integration worktree for each intake.

First intake, against official `3eb661e12`:

| Official commit | Disposition | Reason |
| --- | --- | --- |
| `d238e948a` | Ported; QEMU validated | DT header reservations and the DT blob must stay out of the frame allocator; adapted around downstream framebuffer reservation. |
| `ac790aa89` | Separate boot intake | BSS initialization changes the early assembly path and needs the Sv39/Sv48 boot matrix. |
| `0fa78e601` | Separate memory-management intake | Svade A/D handling overlaps downstream PTE and early-boot changes; review together with existing tests. |
| `abbdf55ad` | Deferred | Ext2 BIO batching is performance work and does not solve the current SDHC flush/durability gap. |
