# LMBench and ICMP integration check

Integration commit `4aca2192992e1296889858f3c08aca39d99000ce` merges the
review-ready native LMBench branch (`a8f2e445f35694c102a0a776d20803237eec3313`)
with the physical-board ICMP branch (`4e51337349101c23155c9bbb1448c8775b9d67bc`).
The merge had no conflicts and the worktree was clean after testing.

The persistent project container built this exact combined source with
`make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode`. The resulting
development Image SHA-256 is
`a8423fca2a66ca09b97bce60e7d05ede211e0733f8439fafad3dfb34f6a391be`.
The [QEMU kernel-test log](bigtcp-ktest-qemu-serial.log.gz) records all **21/21**
`aster-bigtcp` tests passing, including both ICMP Echo tests. The
[native-runner unit log](lmbench-native-unit.log) records **23/23** passing.

The full native LMBench ALL result on the LMBench parent is recorded in
[the original qualification](../2026-09-24-native-lmbench-no-egrep/README.md),
and the ICMP candidate's Ethernet behavior was verified on Megrez in
[the board qualification](../2026-09-24-icmp-echo/README.md). This integration
check confirms the two changes build and pass their focused tests together;
it does not claim a fresh native ALL run or a board LMBench performance score
for the merged Image.
