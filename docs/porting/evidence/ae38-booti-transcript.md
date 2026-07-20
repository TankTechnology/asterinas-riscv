# `ae38e6c6` Megrez `booti` transcript

This is a line-ending-normalized transcript of the only `booti` command sent
during the 2026-07-15 `ae38e6c6` board run. The original CRLF serial capture is
kept outside Git; its SHA-256 is
`8666c40775ba323e8f390834c098faadbc4a1542c50a7bee13848dbaffcf226e`.

```text
booti 0x80200000 0x83000000:${initrd_size} 0xf0000000
## Flattened Device Tree blob at f0000000
   Booting using the fdt blob at 0xf0000000
Working FDT set to f0000000
ERROR: reserving fdt memory region failed (addr=fffff000 size=1000 flags=4)
   Using Device Tree in place at 00000000f0000000, end 00000000f0029fff
Working FDT set to f0000000

Starting kernel ...
```

No Asterinas output or automatic reset was observed during the following
120-second receive-only window. See the adjacent
[result summary](../../../porting/logs/megrez-upstream-ae38e6c6f279-20260715T044534Z/result.md)
for artifact identities and interpretation limits.
