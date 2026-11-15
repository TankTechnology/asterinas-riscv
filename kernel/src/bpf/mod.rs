// SPDX-License-Identifier: MPL-2.0

//! A small eBPF subsystem: maps, an interpreter, a conservative verifier, and
//! attachment to this kernel's own trace events.
//!
//! The design goal is a *useful* subset, not compatibility with every program
//! clang emits. What is here:
//!
//! - `BPF_MAP_TYPE_ARRAY` and `BPF_MAP_TYPE_HASH` stores ([`map`]).
//! - `bpf(2)` with map create/lookup/update/delete/next-key and program load.
//! - A verifier ([`verifier`]) that rejects every loop, every backward jump and
//!   every unproven memory access. Programs are only loadable by root.
//! - An interpreter ([`interp`]) with bounds checks at run time as well.
//! - Attachment to trace events ([`attach`]): an event firing calls the
//!   program with a small context. No kprobes, no instruction patching.
//!
//! Anything a program might use that is not listed above is rejected at load
//! time, so a program that loads cannot do anything but the above.

pub mod attach;
pub mod insn;
pub mod interp;
pub mod map;
pub mod prog;
pub mod verifier;

pub use map::Map;
pub use prog::Program;

/// Size of the context a trace event passes to a program.
pub const CTX_SIZE: usize = 48;

/// The context of a trace event, as a program sees it through `r1`.
///
/// ```text
/// 0   u64  timestamp_ns
/// 8   u32  pid
/// 12  u32  tid
/// 16  u32  event
/// 20  u32  cpu
/// 24  u64  arg0
/// 32  u64  arg1
/// 40  u64  arg2
/// ```
///
/// The arguments are the event's own: the syscall number and its first
/// arguments for `syscall:sys_enter`, the request size for a block event, the
/// interrupted address for a page fault, and so on (see each event's
/// `format` line under `/proc/trace/events`).
#[derive(Clone, Copy, Default, Debug)]
pub struct TraceCtx {
    pub timestamp_ns: u64,
    pub pid: u32,
    pub tid: u32,
    pub event: u32,
    pub cpu: u32,
    pub args: [u64; 3],
}

impl TraceCtx {
    pub fn to_bytes(self) -> [u8; CTX_SIZE] {
        let mut bytes = [0u8; CTX_SIZE];
        bytes[0..8].copy_from_slice(&self.timestamp_ns.to_le_bytes());
        bytes[8..12].copy_from_slice(&self.pid.to_le_bytes());
        bytes[12..16].copy_from_slice(&self.tid.to_le_bytes());
        bytes[16..20].copy_from_slice(&self.event.to_le_bytes());
        bytes[20..24].copy_from_slice(&self.cpu.to_le_bytes());
        for (index, arg) in self.args.iter().enumerate() {
            let start = 24 + index * 8;
            bytes[start..start + 8].copy_from_slice(&arg.to_le_bytes());
        }
        bytes
    }
}

/// The `/proc/self/fdinfo` body shared by the bpf file types.
///
/// This is called with the file table's lock held, so it copies no large
/// objects and drops no `Arc`.
pub(crate) fn fdinfo(
    common: &crate::fs::file::FileCommon,
    access: crate::fs::file::AccessMode,
    fd_flags: crate::fs::file::file_table::FdFlags,
    kind: &'static str,
) -> alloc::boxed::Box<dyn core::fmt::Display> {
    use crate::fs::file::CreationFlags;

    struct FdInfo {
        flags: u32,
        kind: &'static str,
    }

    impl core::fmt::Display for FdInfo {
        fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
            writeln!(f, "pos:\t{}", 0)?;
            writeln!(f, "flags:\t0{:o}", self.flags)?;
            writeln!(f, "bpf_type:\t{}", self.kind)
        }
    }

    let mut flags = common.status_flags().bits() | access as u32;
    if fd_flags.contains(crate::fs::file::file_table::FdFlags::CLOEXEC) {
        flags |= CreationFlags::O_CLOEXEC.bits();
    }
    alloc::boxed::Box::new(FdInfo { flags, kind })
}

#[cfg(ktest)]
mod test {
    use ostd::prelude::ktest;

    use super::{
        CTX_SIZE, Map, Program, TraceCtx,
        insn::{Insn, PSEUDO_MAP_FD},
        map::{BPF_MAP_TYPE_ARRAY, BPF_MAP_TYPE_HASH},
        verifier,
    };
    use crate::prelude::*;

    fn insn(code: u8, dst: u8, src: u8, off: i16, imm: i32) -> Insn {
        Insn {
            code,
            dst,
            src,
            off,
            imm,
        }
    }

    /// `array[pid & 1023] += 1`, the smallest useful trace-event program.
    ///
    /// It reads the pid out of the context through the stack, so it exercises
    /// the context pointer, stack stores and stack reads, a map lookup and a
    /// read-modify-write of a map value.
    fn counter_program() -> Vec<Insn> {
        vec![
            insn(0xbf, 6, 1, 0, 0),             // r6 = r1
            insn(0x18, 1, PSEUDO_MAP_FD, 0, 0), // r1 = map
            insn(0, 0, 0, 0, 0),
            insn(0xbf, 3, 6, 0, 0),    // r3 = r6
            insn(0x61, 4, 3, 8, 0),    // r4 = *(u32 *)(r3 + 8)
            insn(0x57, 4, 0, 0, 1023), // r4 &= 1023
            insn(0x63, 10, 4, -4, 0),  // *(u32 *)(r10 - 4) = r4
            insn(0xbf, 2, 10, 0, 0),   // r2 = r10
            insn(0x07, 2, 0, 0, -4),   // r2 += -4
            insn(0x85, 0, 0, 0, 1),    // call map_lookup_elem
            insn(0x15, 0, 0, 4, 0),    // if r0 == 0 goto exit
            insn(0x79, 1, 0, 0, 0),    // r1 = *(u64 *)(r0 + 0)
            insn(0x07, 1, 0, 0, 1),    // r1 += 1
            insn(0x7b, 0, 1, 0, 0),    // *(u64 *)(r0 + 0) = r1
            insn(0xb7, 0, 0, 0, 0),    // r0 = 0
            insn(0x95, 0, 0, 0, 0),    // exit
        ]
    }

    fn load(program: Vec<Insn>, map: Arc<Map>) -> Result<Arc<Program>> {
        let mut insns = program;
        let mut maps = Vec::new();
        let mut resolve = |_fd: u32| Ok(map.clone());
        verifier::verify(&mut insns, &mut maps, CTX_SIZE, &mut resolve)?;
        Ok(Program::new(insns, maps, 0x8000_0001, "counter", "GPL"))
    }

    #[ktest]
    fn a_program_counts_events_per_pid() {
        let map = Map::create(BPF_MAP_TYPE_ARRAY, 4, 8, 1024, "counts").unwrap();
        let program = load(counter_program(), map.clone()).unwrap();

        let ctx = TraceCtx {
            timestamp_ns: 42,
            pid: 77,
            tid: 77,
            event: 3,
            cpu: 1,
            args: [0; 3],
        };
        for _ in 0..3 {
            assert_eq!(program.run(&ctx), 0, "the program returned zero");
        }

        let key = (77u32 & 1023).to_le_bytes();
        let mut value = [0u8; 8];
        map.lookup(&key, &mut value).unwrap();
        assert_eq!(u64::from_le_bytes(value), 3, "three events were counted");
        assert_eq!(program.run_errors(), 0, "no run faulted");
    }

    #[ktest]
    fn the_context_carries_the_event_arguments() {
        let map = Map::create(BPF_MAP_TYPE_ARRAY, 4, 8, 4, "unused").unwrap();
        let mut insns = vec![
            insn(0x79, 0, 1, 24, 0), // r0 = *(u64 *)(ctx + 24)
            insn(0x95, 0, 0, 0, 0),  // exit
        ];
        let mut maps = Vec::new();
        let mut resolve = |_fd: u32| Ok(map.clone());
        verifier::verify(&mut insns, &mut maps, CTX_SIZE, &mut resolve).unwrap();
        let program = Program::new(insns, maps, 0x8000_0001, "args", "GPL");

        let ctx = TraceCtx {
            args: [0x1122_3344_5566_7788, 9, 10],
            ..TraceCtx::default()
        };
        assert_eq!(program.run(&ctx), 0x1122_3344_5566_7788);
    }

    #[ktest]
    fn a_hash_map_round_trips_a_value() {
        // Deliberately larger than the number of keys: a table that is exactly
        // full hides a broken probe order, because a lookup then has no empty
        // slot to stop at.
        let map = Map::create(BPF_MAP_TYPE_HASH, 4, 8, 16, "hash").unwrap();
        let key = 5u32.to_le_bytes();
        map.update(&key, &7u64.to_le_bytes(), 0).unwrap();

        let mut value = [0u8; 8];
        map.lookup(&key, &mut value).unwrap();
        assert_eq!(u64::from_le_bytes(value), 7, "the value came back");

        map.update(&key, &9u64.to_le_bytes(), 0).unwrap();
        map.lookup(&key, &mut value).unwrap();
        assert_eq!(u64::from_le_bytes(value), 9, "the update landed");

        map.delete(&key).unwrap();
        assert!(map.lookup(&key, &mut value).is_err(), "the key is gone");

        // Every key must be findable after its insert, whether or not the
        // table is full.
        let sparse: Vec<u32> = (0..5u32).map(|key| key * 7).collect();
        for key in &sparse {
            map.update(&key.to_le_bytes(), &(*key as u64).to_le_bytes(), 0)
                .unwrap();
            for live in sparse.iter().filter(|live| *live <= key) {
                let mut value = [0u8; 8];
                map.lookup(&live.to_le_bytes(), &mut value)
                    .unwrap_or_else(|_| panic!("key {live} is unfindable"));
                assert_eq!(u64::from_le_bytes(value), *live as u64);
            }
        }
        for key in &sparse {
            map.delete(&key.to_le_bytes()).unwrap();
        }

        // Deleting must not hide the keys that probed past the hole.
        let keys: Vec<u32> = (0..8u32).collect();
        for key in &keys {
            map.update(&key.to_le_bytes(), &(*key as u64).to_le_bytes(), 0)
                .unwrap();
        }
        for key in &keys {
            map.delete(&key.to_le_bytes()).unwrap();
            for other in keys.iter().filter(|other| *other > key) {
                let mut value = [0u8; 8];
                map.lookup(&other.to_le_bytes(), &mut value)
                    .unwrap_or_else(|_| panic!("key {other} survived the delete of {key}"));
                assert_eq!(u64::from_le_bytes(value), *other as u64);
            }
        }
    }

    #[ktest]
    fn the_verifier_refuses_unsafe_programs() {
        let map = Map::create(BPF_MAP_TYPE_ARRAY, 4, 8, 4, "small").unwrap();

        // A backward jump.
        let mut program = counter_program();
        program[10].off = -6;
        assert!(load(program, map.clone()).is_err(), "a loop was accepted");

        // A jump inside the 64-bit immediate load.
        let mut program = counter_program();
        program[10].off = -8;
        assert!(
            load(program, map.clone()).is_err(),
            "a bad target was accepted"
        );

        // Arithmetic on a map pointer.
        let mut program = counter_program();
        program.insert(11, insn(0x07, 0, 0, 0, 8)); // r0 += 8
        assert!(
            load(program, map.clone()).is_err(),
            "pointer arithmetic was accepted"
        );

        // Reading the stack before writing it.
        let mut program = counter_program();
        program[6] = insn(0x00, 0, 0, 0, 0); // drop the stack store
        assert!(
            load(program, map.clone()).is_err(),
            "an uninitialised read was accepted"
        );

        // A program that does not end with exit.
        let mut program = counter_program();
        program.pop();
        assert!(load(program, map).is_err(), "a missing exit was accepted");
    }
}
