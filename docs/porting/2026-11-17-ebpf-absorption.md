# eBPF subset: where it lives in the rebase tree (2026-11-17)

The eBPF work (`e546e7f95` P1, `675cb4dfc` P2 on
`feat/kernel-observability-io`) is **already part of the rebase tree** — it
came along with the replay and needed only import-level adaptation, not a
re-port. This note records where it landed so the next session does not go
looking for it in the old `kernel/src/` layout.

## Code

| Piece | Path in this tree |
| --- | --- |
| Verifier, interpreter, maps, programs, trace attachment | `kernel/core/src/bpf/{verifier,interp,map,prog,attach,insn}.rs` |
| `bpf(2)` | `kernel/core/src/syscall/bpf.rs`, `SYS_BPF = 280` in `syscall/arch/generic.rs` |
| `SO_ATTACH_BPF` / detach | `kernel/core/src/syscall/setsockopt.rs`, `kernel/core/src/net/socket/options/mod.rs`, `kernel/core/src/net/socket/netlink/common/{bound,unbound,mod}.rs` |
| `SockFilter` (classic BPF) | `kernel/core/src/util/bpf.rs` |
| kallsyms helper | `kernel/core/src/trace/kallsyms.rs` |
| `/proc/trace/bpf` | `kernel/core/src/fs/fs_impls/procfs/trace.rs` |
| Userspace tool | `tools/riscv/bpf/bpftool_lite.py` (pushed to the board as `/root/bpftool_lite.py`, sha256 `675340fe…`) |

The only differences from the `feat` branch are small: `map.rs` and `prog.rs`
each lost a line to the new `FileCommon::new`/`MappableObject` import shape.

## Verification

`make ktest-local KTEST_CRATE_DIR=kernel/core` — all eight eBPF tests pass
inside `aster-core`'s 253:

```
aster_core::bpf::test::a_program_counts_events_per_pid ... ok
aster_core::bpf::test::the_context_carries_the_event_arguments ... ok
aster_core::bpf::test::a_hash_map_round_trips_a_value ... ok
aster_core::bpf::test::a_per_cpu_map_keeps_one_value_per_cpu ... ok
aster_core::bpf::test::a_ring_buffer_carries_a_record ... ok
aster_core::bpf::test::a_socket_filter_reads_the_packet ... ok
aster_core::bpf::test::the_verifier_refuses_unsafe_programs ... ok
aster_core::bpf::test::the_kallsyms_helper_loads ... ok
```

## Board run: blocked by the desktop boot race

`bpftool_lite.py verifier|count|percpu|ringbuf|sockfilter|ksym` needs a shell on
the board with Python, i.e. the deployed rootfs running on this kernel. Five
cold boots of the rebase kernel in a row failed before the debug console came
up (`docs/porting/2026-11-17-desktop-boot-race.md`), so the runtime check has
not been made on hardware yet. The kernel-side paths are the same code the
ktests exercise, and nothing in the eBPF subset is hardware-specific beyond
`get_smp_processor_id`, so the unit coverage is the meaningful signal until
the boot race is fixed.
