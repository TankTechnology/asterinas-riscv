// SPDX-License-Identifier: MPL-2.0

//! The eBPF verifier: a deliberately small, conservative static checker.
//!
//! What it rejects outright:
//!
//! - loops, and every jump that goes backwards (so a program is straight-line
//!   code with forward branches, and every run is bounded);
//! - jumps into the middle of a 64-bit immediate load;
//! - uninitialised registers, and reading stack bytes that were not written;
//! - arithmetic on pointers, except a constant adjustment of the stack pointer;
//! - memory accesses that cannot be typed against the context, the stack or a
//!   map value;
//! - any helper outside the whitelist, and arguments of the wrong type.
//!
//! What it does *not* prove: the memory checks are re-done at run time by the
//! interpreter, which is what actually keeps the kernel safe. The verifier can
//! therefore be conservative without being unsound -- when it is unsure (a
//! scalar's value, a stack pointer that was merged from two branches) it stops
//! reasoning and leaves the check to the interpreter.

use crate::bpf::{
    insn::{
        Class, Insn, MAX_INSNS, PSEUDO_MAP_FD, PSEUDO_MAP_INDEX, R0, R1, R2, R3, R4, R10,
        REG_COUNT, STACK_SIZE, helper_id, op,
    },
    interp::{Kind, Reg},
    map::Map,
};
use crate::prelude::*;

/// Stack words in the initialisation bitmap.
const STACK_WORDS: usize = STACK_SIZE / 64;

#[derive(Clone, Copy)]
struct State {
    regs: [Reg; REG_COUNT],
    /// One bit per stack byte: set once the byte has been written.
    stack: [u64; STACK_WORDS],
}

impl State {
    fn entry() -> Self {
        let mut regs = [Reg::UNINIT; REG_COUNT];
        regs[R1 as usize] = Reg {
            kind: Kind::Ctx,
            map: 0,
            val: 0,
        };
        regs[R10 as usize] = Reg {
            kind: Kind::Stack,
            map: 0,
            val: 0,
        };
        Self {
            regs,
            stack: [0; STACK_WORDS],
        }
    }

    /// Merges two paths reaching the same instruction.
    ///
    /// Only the registers the instruction (or what follows it) still reads
    /// have to agree -- a register a path is about to overwrite can hold
    /// anything. A register that is live and uninitialised on one path fails
    /// the load.
    fn merge(&mut self, other: &State, live: u16) -> Result<()> {
        for index in 0..REG_COUNT {
            if live & (1 << index) != 0 {
                self.regs[index] = merge_regs(self.regs[index], other.regs[index])?;
            }
        }
        for index in 0..STACK_WORDS {
            self.stack[index] &= other.stack[index];
        }
        Ok(())
    }
}

/// The registers each instruction reads *before* later instructions overwrite
/// them, as a bitmask per instruction.
///
/// Every jump goes forward, so one backwards pass is enough.
fn liveness(insns: &[Insn], boundaries: &[bool]) -> Vec<u16> {
    let len = insns.len();
    let mut live_in = vec![0u16; len];
    let mut index = len;

    while index > 0 {
        index -= 1;
        let insn = insns[index];

        if !boundaries[index] {
            // The second half of a 64-bit load simply passes through.
            live_in[index] = if index + 1 < len { live_in[index + 1] } else { 0 };
            continue;
        }

        let (used, defined) = use_def(insn);
        let mut live_out = 0u16;
        let mut successor = |slot: usize| {
            if slot < len {
                live_out |= live_in[slot];
            }
        };

        if is_load64(&insn) {
            successor(index + 2);
        } else if matches!(insn.class(), Class::Jmp | Class::Jmp32) {
            match insn.op() {
                op::EXIT => {}
                op::CALL => successor(index + 1),
                op::JA => successor(insn.jump_target(index).max(0) as usize),
                _ => {
                    successor(index + 1);
                    successor(insn.jump_target(index).max(0) as usize);
                }
            }
        } else {
            successor(index + 1);
        }

        live_in[index] = used | (live_out & !defined);
    }

    live_in
}

/// Marks the first slot of every instruction.
fn boundaries(insns: &[Insn]) -> Vec<bool> {
    let mut marks = vec![false; insns.len()];
    let mut index = 0usize;
    while index < insns.len() {
        marks[index] = true;
        index += if is_load64(&insns[index]) { 2 } else { 1 };
    }
    marks
}

/// The registers an instruction reads and writes.
fn use_def(insn: Insn) -> (u16, u16) {
    let bit = |reg: u8| 1u16 << reg;
    match insn.class() {
        Class::Ld => (0, bit(insn.dst)),
        Class::Ldx => (bit(insn.src), bit(insn.dst)),
        Class::St => (bit(insn.dst), 0),
        Class::Stx => (bit(insn.dst) | bit(insn.src), 0),
        Class::Alu | Class::Alu64 => {
            let mut used = if insn.op() == op::MOV { 0 } else { bit(insn.dst) };
            if insn.uses_src_reg() {
                used |= bit(insn.src);
            }
            (used, bit(insn.dst))
        }
        Class::Jmp | Class::Jmp32 => match insn.op() {
            op::EXIT => (bit(R0), 0),
            op::CALL => {
                let arguments = (1u16 << 1) | (1u16 << 2) | (1u16 << 3) | (1u16 << 4) | (1u16 << 5);
                (arguments, arguments | bit(R0))
            }
            op::JA => (0, 0),
            _ => {
                let mut used = bit(insn.dst);
                if insn.uses_src_reg() {
                    used |= bit(insn.src);
                }
                (used, 0)
            }
        },
        Class::Invalid => (0, 0),
    }
}

fn merge_regs(a: Reg, b: Reg) -> Result<Reg> {
    if a.kind == Kind::Uninit && b.kind == Kind::Uninit {
        return Ok(Reg::UNINIT);
    }
    if a.kind == Kind::Uninit || b.kind == Kind::Uninit {
        // Allowing this would let one path read a register that the other
        // never wrote.
        return Err(Error::with_message(
            Errno::EINVAL,
            "a register is not initialised on every path",
        ));
    }
    if a.kind != b.kind || a.map != b.map {
        // A pointer on one path and something else on the other -- typically
        // a map lookup's value-or-null. Demote to a plain scalar: the
        // interpreter refuses any memory access through a scalar, so the
        // imprecision costs a run-time check, not safety.
        return Ok(Reg::scalar(0));
    }
    Ok(Reg {
        kind: a.kind,
        map: a.map,
        val: if a.val == b.val { a.val } else { 0 },
    })
}

/// Checks a program, resolving the map descriptors it references.
///
/// `lookup_map` turns a file descriptor into a map; it is called for each
/// `BPF_PSEUDO_MAP_FD` load, whose instruction the verifier rewrites into a
/// [`PSEUDO_MAP_INDEX`] load naming the program's map table.
pub fn verify(
    insns: &mut [Insn],
    maps: &mut Vec<Arc<Map>>,
    ctx_size: usize,
    lookup_map: &mut dyn FnMut(u32) -> Result<Arc<Map>>,
) -> Result<()> {
    verify_structure(insns)?;
    let live = liveness(insns, &boundaries(insns));

    let mut states: Vec<Option<State>> = vec![None; insns.len()];
    states[0] = Some(State::entry());

    let mut index = 0usize;
    while index < insns.len() {
        let Some(state) = states[index] else {
            // Unreachable code: nothing to check, but a 64-bit load still
            // occupies two slots.
            index += if is_load64(&insns[index]) { 2 } else { 1 };
            continue;
        };

        let insn = insns[index];
        let mut out = state;
        let mut fallthrough = true;
        let mut branch: Option<usize> = None;

        match insn.class() {
            Class::Ld => {
                if insn.code != 0x18 {
                    return_errno_with_message!(Errno::EINVAL, "unsupported load class");
                }
                let high = insns[index + 1];
                match insn.src {
                    0 => {
                        let imm = (insn.imm as u32 as u64) | ((high.imm as u32 as u64) << 32);
                        out.regs[insn.dst as usize] = Reg::scalar(imm);
                    }
                    PSEUDO_MAP_FD => {
                        let fd = insn.imm as u32;
                        let map = lookup_map(fd)?;
                        let slot = match maps.iter().position(|known| Arc::ptr_eq(known, &map)) {
                            Some(slot) => slot,
                            None => {
                                maps.push(map);
                                maps.len() - 1
                            }
                        };
                        if slot >= u8::MAX as usize {
                            return_errno_with_message!(Errno::EINVAL, "too many maps");
                        }
                        out.regs[insn.dst as usize] = Reg {
                            kind: Kind::Map,
                            map: slot as u8,
                            val: 0,
                        };
                        // Rewrite for the interpreter.
                        insns[index].src = PSEUDO_MAP_INDEX;
                        insns[index].imm = slot as i32;
                    }
                    _ => return_errno_with_message!(Errno::EINVAL, "unsupported pseudo load"),
                }
                // The second slot is part of the load; skip it.
                let next_index = index + 2;
                if next_index >= insns.len() {
                    return_errno_with_message!(Errno::EINVAL, "execution runs off the end");
                }
                states[next_index] = merge_slot(states[next_index].take(), out, live[next_index])?;
                index = next_index;
                continue;
            }
            Class::Ldx => {
                let size = width(insn)?;
                let base = state.regs[insn.src as usize];
                check_load(&base, insn.off, size, maps, ctx_size)?;
                out.regs[insn.dst as usize] = Reg::scalar(0);
            }
            Class::St => {
                let size = width(insn)?;
                let base = state.regs[insn.dst as usize];
                check_store(&base, insn.off, size, maps, &mut out)?;
            }
            Class::Stx => {
                let size = width(insn)?;
                let base = state.regs[insn.dst as usize];
                if state.regs[insn.src as usize].kind != Kind::Scalar {
                    return_errno_with_message!(Errno::EINVAL, "a pointer is stored as a value");
                }
                check_store(&base, insn.off, size, maps, &mut out)?;
            }
            Class::Alu64 | Class::Alu => {
                alu(&mut out, insn, insn.class() == Class::Alu)?;
            }
            Class::Jmp | Class::Jmp32 => {
                let is_32 = insn.class() == Class::Jmp32;
                match insn.op() {
                    op::EXIT => {
                        if state.regs[R0 as usize].kind != Kind::Scalar {
                            return_errno_with_message!(
                                Errno::EINVAL,
                                "r0 is not a value at exit"
                            );
                        }
                        fallthrough = false;
                    }
                    op::CALL => {
                        call(&mut out, insn, maps)?;
                    }
                    op::JA => {
                        fallthrough = false;
                        branch = Some(target(insn, index, insns.len())?);
                    }
                    _ => {
                        check_condition(&state, insn, is_32)?;
                        branch = Some(target(insn, index, insns.len())?);
                    }
                }
            }
            Class::Invalid => return_errno_with_message!(Errno::EINVAL, "invalid opcode"),
        }

        if let Some(branch) = branch {
            states[branch] = merge_slot(states[branch].take(), out, live[branch])?;
        }
        if fallthrough {
            let next = index + 1;
            if next >= insns.len() {
                return_errno_with_message!(Errno::EINVAL, "execution runs off the end");
            }
            states[next] = merge_slot(states[next].take(), out, live[next])?;
        }
        index += 1;
    }

    Ok(())
}

fn merge_slot(existing: Option<State>, incoming: State, live: u16) -> Result<Option<State>> {
    match existing {
        None => Ok(Some(incoming)),
        Some(mut state) => {
            state.merge(&incoming, live)?;
            Ok(Some(state))
        }
    }
}

fn is_load64(insn: &Insn) -> bool {
    insn.class() == Class::Ld && insn.code == 0x18
}

fn width(insn: Insn) -> Result<usize> {
    insn.width()
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "bad access width"))
}

fn target(insn: Insn, index: usize, len: usize) -> Result<usize> {
    let target = insn.jump_target(index);
    if target <= index as isize {
        return Err(Error::with_message(
            Errno::EINVAL,
            "a backward jump (loops are not supported)",
        ));
    }
    if target as usize >= len {
        return Err(Error::with_message(Errno::EINVAL, "a jump leaves the code"));
    }
    Ok(target as usize)
}

/// Checks the shape of the code: registers in range, forward jumps that land
/// on instruction boundaries, every 64-bit load complete, and an `exit` last.
fn verify_structure(insns: &[Insn]) -> Result<()> {
    let len = insns.len();
    if len == 0 {
        return_errno_with_message!(Errno::EINVAL, "the program is empty");
    }
    if len > MAX_INSNS {
        return_errno_with_message!(Errno::EINVAL, "the program is too long");
    }

    let mut boundaries = vec![false; len];
    let mut index = 0usize;
    while index < len {
        boundaries[index] = true;
        if is_load64(&insns[index]) {
            if index + 1 >= len {
                return_errno_with_message!(Errno::EINVAL, "a truncated 64-bit load");
            }
            let high = insns[index + 1];
            if high.code != 0 || high.dst != 0 || high.src != 0 || high.off != 0 {
                return_errno_with_message!(Errno::EINVAL, "a malformed 64-bit load");
            }
            index += 2;
        } else {
            index += 1;
        }
    }

    let mut index = 0usize;
    while index < len {
        if !boundaries[index] {
            // The second slot of a 64-bit load; already checked above.
            index += 1;
            continue;
        }
        let insn = insns[index];
        if insn.code == 0 {
            return_errno_with_message!(Errno::EINVAL, "an unexpected zero instruction");
        }
        if insn.dst as usize >= REG_COUNT || insn.src as usize >= REG_COUNT {
            return_errno_with_message!(Errno::EINVAL, "a register number is out of range");
        }
        if insn.dst as usize == R10 as usize && writes_dst(insn) {
            return_errno_with_message!(Errno::EINVAL, "the frame pointer is written");
        }

        match insn.class() {
            Class::Jmp | Class::Jmp32 => match insn.op() {
                op::EXIT => {}
                op::CALL => {
                    if insn.src != 0 {
                        return_errno_with_message!(
                            Errno::EINVAL,
                            "only helper calls are supported"
                        );
                    }
                }
                _ => {
                    let target = target(insn, index, len)?;
                    if !boundaries[target] {
                        return_errno_with_message!(
                            Errno::EINVAL,
                            "a jump lands inside a 64-bit load"
                        );
                    }
                }
            },
            _ => {}
        }
        index += 1;
    }

    let last = insns[len - 1];
    if !(last.class() == Class::Jmp || last.class() == Class::Jmp32) || last.op() != op::EXIT {
        return_errno_with_message!(Errno::EINVAL, "the program does not end with exit");
    }

    Ok(())
}

fn writes_dst(insn: Insn) -> bool {
    matches!(insn.class(), Class::Ld | Class::Ldx | Class::Alu | Class::Alu64)
}

fn alu(state: &mut State, insn: Insn, is_32: bool) -> Result<()> {
    let dst = state.regs[insn.dst as usize];
    let src = if insn.uses_src_reg() {
        state.regs[insn.src as usize]
    } else {
        Reg::scalar(insn.imm as i64 as u64)
    };

    if insn.op() == op::MOV {
        if is_32 && src.kind != Kind::Scalar {
            return_errno_with_message!(Errno::EINVAL, "a 32-bit move cannot carry a pointer");
        }
        state.regs[insn.dst as usize] = if is_32 {
            Reg::scalar(src.val as u32 as u64)
        } else {
            src
        };
        return Ok(());
    }

    if src.kind != Kind::Scalar {
        return_errno_with_message!(Errno::EINVAL, "the source operand is not a value");
    }

    if dst.kind != Kind::Scalar {
        if (dst.kind == Kind::Ctx || dst.kind == Kind::Stack)
            && !is_32
            && !insn.uses_src_reg()
            && (insn.op() == op::ADD || insn.op() == op::SUB)
        {
            let delta = insn.imm as i64;
            let delta = if insn.op() == op::ADD { delta } else { -delta };
            let moved = dst.val as i64 + delta;
            let in_region = match dst.kind {
                Kind::Ctx => moved >= 0,
                _ => moved <= 0 && moved >= -(STACK_SIZE as i64),
            };
            if !in_region {
                return_errno_with_message!(Errno::EINVAL, "the pointer left its region");
            }
            state.regs[insn.dst as usize] = Reg {
                kind: dst.kind,
                map: 0,
                val: moved as u64,
            };
            return Ok(());
        }
        return_errno_with_message!(Errno::EINVAL, "arithmetic on a pointer");
    }

    state.regs[insn.dst as usize] = Reg::scalar(0);
    Ok(())
}

fn check_load(
    base: &Reg,
    offset: i16,
    size: usize,
    maps: &[Arc<Map>],
    ctx_size: usize,
) -> Result<()> {
    match base.kind {
        Kind::Ctx => {
            let addr = base.val as i64 + offset as i64;
            if addr < 0 || addr as usize + size > ctx_size {
                return_errno_with_message!(Errno::EINVAL, "the access is outside the context");
            }
            Ok(())
        }
        Kind::Stack => check_stack(base, offset, size).map(|_| ()),
        Kind::MapValue => {
            let map = maps
                .get(base.map as usize)
                .ok_or_else(|| Error::with_message(Errno::EINVAL, "bad map index"))?;
            if offset < 0 || offset as usize + size > map.value_size() as usize {
                return_errno_with_message!(Errno::EINVAL, "the access is outside the value");
            }
            Ok(())
        }
        _ => return_errno_with_message!(Errno::EINVAL, "the register is not a pointer"),
    }
}

fn check_store(
    base: &Reg,
    offset: i16,
    size: usize,
    maps: &[Arc<Map>],
    state: &mut State,
) -> Result<()> {
    match base.kind {
        Kind::Stack => {
            let index = check_stack(base, offset, size)?;
            for byte in index..index + size {
                state.stack[byte / 64] |= 1 << (byte % 64);
            }
            Ok(())
        }
        Kind::MapValue => {
            let map = maps
                .get(base.map as usize)
                .ok_or_else(|| Error::with_message(Errno::EINVAL, "bad map index"))?;
            if offset < 0 || offset as usize + size > map.value_size() as usize {
                return_errno_with_message!(Errno::EINVAL, "the access is outside the value");
            }
            Ok(())
        }
        Kind::Ctx => return_errno_with_message!(Errno::EPERM, "the context is read-only"),
        _ => return_errno_with_message!(Errno::EINVAL, "the register is not a pointer"),
    }
}

/// Bounds-checks a stack access and returns the first byte's frame index.
fn check_stack(base: &Reg, offset: i16, size: usize) -> Result<usize> {
    let addr = base.val as i64 + offset as i64;
    if addr < -(STACK_SIZE as i64) || addr + size as i64 > 0 {
        return_errno_with_message!(Errno::EINVAL, "the access is outside the stack");
    }
    Ok((addr + STACK_SIZE as i64) as usize)
}

/// Whether the stack bytes an argument points at were all written.
fn stack_initialised(state: &State, base: &Reg, size: usize) -> bool {
    match base.kind {
        Kind::Stack => {
            let Ok(index) = check_stack(base, 0, size) else {
                return false;
            };
            (index..index + size).all(|byte| state.stack[byte / 64] & (1 << (byte % 64)) != 0)
        }
        // A map value is always readable; the interpreter checks its bounds.
        Kind::MapValue | Kind::Ctx => true,
        _ => false,
    }
}

fn check_condition(state: &State, insn: Insn, is_32: bool) -> Result<()> {
    let dst = state.regs[insn.dst as usize];
    let src = if insn.uses_src_reg() {
        state.regs[insn.src as usize]
    } else {
        Reg::scalar(insn.imm as i64 as u64)
    };

    if dst.kind == Kind::Scalar && src.kind == Kind::Scalar {
        return Ok(());
    }
    if dst.kind == Kind::Scalar || src.kind == Kind::Scalar {
        // A pointer only compares equal to zero; that is how a program checks
        // a map lookup for null.
        let pointer = if dst.kind == Kind::Scalar { src } else { dst };
        let scalar = if dst.kind == Kind::Scalar { dst } else { src };
        let null_check = !is_32
            && scalar.val == 0
            && matches!(insn.op(), op::JEQ | op::JNE);
        if null_check && pointer.kind != Kind::Uninit {
            return Ok(());
        }
        return_errno_with_message!(Errno::EINVAL, "unsupported comparison");
    }
    Err(Error::with_message(
        Errno::EINVAL,
        "two pointers cannot be compared",
    ))
}

fn call(state: &mut State, instruction: Insn, maps: &[Arc<Map>]) -> Result<()> {
    let id = instruction.imm as u32;
    let arg = |state: &State, reg: u8| state.regs[reg as usize];

    let result = match id {
        helper_id::MAP_LOOKUP_ELEM | helper_id::MAP_UPDATE_ELEM | helper_id::MAP_DELETE_ELEM => {
            let map_reg = arg(state, R1);
            if map_reg.kind != Kind::Map {
                return_errno_with_message!(Errno::EINVAL, "the first argument is not a map");
            }
            let map = maps
                .get(map_reg.map as usize)
                .ok_or_else(|| Error::with_message(Errno::EINVAL, "bad map index"))?;
            let key = arg(state, R2);
            if !stack_initialised(state, &key, map.key_size() as usize) {
                return_errno_with_message!(Errno::EINVAL, "the key is not readable");
            }
            match id {
                helper_id::MAP_LOOKUP_ELEM => Reg {
                    kind: Kind::MapValue,
                    map: map_reg.map,
                    val: 0,
                },
                helper_id::MAP_UPDATE_ELEM => {
                    let value = arg(state, R3);
                    if !stack_initialised(state, &value, map.value_size() as usize) {
                        return_errno_with_message!(Errno::EINVAL, "the value is not readable");
                    }
                    if arg(state, R4).kind != Kind::Scalar {
                        return_errno_with_message!(Errno::EINVAL, "the flags are not a value");
                    }
                    Reg::scalar(0)
                }
                _ => Reg::scalar(0),
            }
        }
        // ktime_get_ns, get_current_pid_tgid, get_smp_processor_id: no arguments.
        helper_id::KTIME_GET_NS
        | helper_id::GET_CURRENT_PID_TGID
        | helper_id::GET_SMP_PROCESSOR_ID => Reg::scalar(0),
        helper_id::TRACE_PRINTK => {
            if !stack_initialised(state, &arg(state, R1), 1) {
                return_errno_with_message!(Errno::EINVAL, "the format string is not readable");
            }
            if arg(state, R2).kind != Kind::Scalar {
                return_errno_with_message!(Errno::EINVAL, "the format length is not a value");
            }
            Reg::scalar(0)
        }
        _ => return_errno_with_message!(Errno::EINVAL, "unknown helper"),
    };

    for reg in 1..=5usize {
        state.regs[reg] = Reg::UNINIT;
    }
    state.regs[R0 as usize] = result;
    Ok(())
}
