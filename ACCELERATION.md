# Acceleration Guide for this Machine (from the orchestrator)

The ThinkPad has 16 threads / 15 GiB RAM but is only ~16% loaded. Use it.

## Concrete levers

1. **QEMU -smp 1 -> 4 (or 8)**
   All smoke scripts (tools/riscv/nixos/{m2,m3}/boot_*_smoke.py and m4) use
   `-smp 1`. Bump to `-smp 4`: 4x guest parallelism AND it verifies kernel SMP
   correctness (upstream LTP gate already ran riscv64 SMP=4 successfully).
   NixOS multi-threaded builds will need SMP anyway.

2. **Run multiple QEMU instances in parallel**
   ~11 GiB RAM is free; a 2 GiB guest leaves room for 4-5 concurrent boots.
   Independent tests (repro vs smoke vs next probe) can run side by side.

3. **Background long builds**
   Kernel rebuilds take minutes. `nohup ... > /tmp/x.log 2>&1 &` and keep
   analyzing/reading code in the meantime instead of blocking on the build.

4. **cargo/make/ninja parallelism**
   cargo defaults to -j16 (all cores). ninja/make: pass `-j16`.

5. **Be generous with parallel tool calls**
   The machine is 84% idle. Prefer fanning out independent commands.

## Verify before/after
- before: `uptime` load ~2.5 on 16 threads
- after: load should reach 6-10 during bursts
