// SPDX-License-Identifier: MPL-2.0

//! The eBPF interpreter.
//!
//! Values are typed at run time: a register is either a scalar or a pointer of
//! a known origin, and every memory access goes through a check against that
//! origin's bounds. The verifier has already proved the same properties
//! statically; the run-time checks are the second line of defense, and they
//! also catch the cases the verifier deliberately accepts without proof (a
//! map element that a concurrent thread deleted, for instance).
//!
//! Nothing here allocates, and every map operation takes an IRQ-safe lock, so
//! a program can run from an interrupt handler or from the scheduler.

use ostd::{cpu::PinCurrentCpu, task::disable_preempt, util::id_set::Id};

use super::{
    TraceCtx,
    insn::{
        Class, Insn, MAX_INSNS, PSEUDO_MAP_FD, PSEUDO_MAP_INDEX, R0, R1, R2, R3, R4, R5, R10,
        REG_COUNT, STACK_SIZE, helper_id, op,
    },
    map::{MAX_RINGBUF_RECORD, Map, read_le},
    prog::Program,
};
use crate::{prelude::*, trace};

/// Instructions one run may execute. The verifier already rejects loops, so
/// this only bounds the straight-line length times the number of helper calls.
pub const MAX_STEPS: u64 = 1 << 20;

/// Bytes moved per map lock acquisition when a helper copies a value.
const COPY_CHUNK: usize = 256;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Scalar,
    Ctx,
    Map,
    MapValue,
    Stack,
    Uninit,
}

#[derive(Clone, Copy, Debug)]
pub struct Reg {
    pub kind: Kind,
    /// The map index, for `Map` and `MapValue`.
    pub map: u8,
    /// A scalar's value, a pointer's offset, or a map value's key.
    pub val: u64,
}

impl Reg {
    pub const UNINIT: Self = Self {
        kind: Kind::Uninit,
        map: 0,
        val: 0,
    };

    pub fn scalar(val: u64) -> Self {
        Self {
            kind: Kind::Scalar,
            map: 0,
            val,
        }
    }
}

struct Vm<'a> {
    regs: [Reg; REG_COUNT],
    stack: [u8; STACK_SIZE],
    /// The context a program reads through `r1`: the 48-byte event context,
    /// or the packet bytes for a socket filter.
    ctx: &'a [u8],
    /// The CPU this run is pinned to, for per-CPU maps.
    cpu: u32,
    /// The event that triggered this run; zero for a socket filter.
    pid: u32,
    tid: u32,
    timestamp_ns: u64,
}

impl<'a> Vm<'a> {
    fn new(ctx: &'a [u8], cpu: u32, trace: Option<&TraceCtx>) -> Self {
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
            stack: [0; STACK_SIZE],
            ctx,
            cpu,
            pid: trace.map_or(0, |trace| trace.pid),
            tid: trace.map_or(0, |trace| trace.tid),
            timestamp_ns: trace.map_or(0, |trace| trace.timestamp_ns),
        }
    }

    fn map_arg<'p>(&self, prog: &'p Program, reg: u8) -> Result<(u8, &'p Arc<Map>)> {
        let reg = self.regs[reg as usize];
        if reg.kind != Kind::Map {
            return_errno_with_message!(Errno::EINVAL, "the argument is not a map");
        }
        let map = prog
            .maps()
            .get(reg.map as usize)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "the map index is out of range"))?;
        Ok((reg.map, map))
    }

    fn scalar_arg(&self, reg: u8) -> Result<u64> {
        let reg = self.regs[reg as usize];
        if reg.kind != Kind::Scalar {
            return_errno_with_message!(Errno::EINVAL, "the argument is not a scalar");
        }
        Ok(reg.val)
    }

    /// Copies `out.len()` bytes from the memory `reg` points at, starting
    /// `offset` bytes in.
    fn read_bytes(&self, prog: &Program, reg: Reg, offset: i64, out: &mut [u8]) -> Result<()> {
        match reg.kind {
            Kind::Ctx => {
                let addr = reg.val as i64 + offset;
                if addr < 0 {
                    return_errno_with_message!(Errno::EINVAL, "the access is outside the context");
                }
                // The bytes are the event context, or a packet that may be
                // shorter than the program expects: read what is there and
                // zero-fill the rest, so a short packet is data, not a fault.
                let start = addr as usize;
                let available = self.ctx.len().saturating_sub(start);
                let taken = out.len().min(available);
                if taken > 0 {
                    out[..taken].copy_from_slice(&self.ctx[start..start + taken]);
                }
                out[taken..].fill(0);
                Ok(())
            }
            Kind::Stack => {
                let addr = reg.val as i64 + offset;
                if addr < -(STACK_SIZE as i64) || addr + out.len() as i64 > 0 {
                    return_errno_with_message!(Errno::EINVAL, "the access is outside the stack");
                }
                let index = (addr + STACK_SIZE as i64) as usize;
                out.copy_from_slice(&self.stack[index..][..out.len()]);
                Ok(())
            }
            Kind::MapValue => {
                if offset < 0 {
                    return_errno_with_message!(Errno::EINVAL, "the access is outside the value");
                }
                let map = prog
                    .maps()
                    .get(reg.map as usize)
                    .ok_or_else(|| Error::with_message(Errno::EINVAL, "bad map index"))?;
                map.elem_read_bytes(self.cpu, reg.val, offset as usize, out)
            }
            _ => return_errno_with_message!(Errno::EINVAL, "the register is not a pointer"),
        }
    }

    fn write_bytes(&mut self, prog: &Program, reg: Reg, offset: i64, bytes: &[u8]) -> Result<()> {
        match reg.kind {
            Kind::Stack => {
                let addr = reg.val as i64 + offset;
                if addr < -(STACK_SIZE as i64) || addr + bytes.len() as i64 > 0 {
                    return_errno_with_message!(Errno::EINVAL, "the access is outside the stack");
                }
                let index = (addr + STACK_SIZE as i64) as usize;
                self.stack[index..][..bytes.len()].copy_from_slice(bytes);
                Ok(())
            }
            Kind::MapValue => {
                if offset < 0 {
                    return_errno_with_message!(Errno::EINVAL, "the access is outside the value");
                }
                let map = prog
                    .maps()
                    .get(reg.map as usize)
                    .ok_or_else(|| Error::with_message(Errno::EINVAL, "bad map index"))?;
                map.elem_write_bytes(self.cpu, reg.val, offset as usize, bytes)
            }
            Kind::Ctx => {
                return_errno_with_message!(Errno::EPERM, "the context is read-only");
            }
            _ => return_errno_with_message!(Errno::EINVAL, "the register is not a pointer"),
        }
    }

    fn load(&self, prog: &Program, reg: Reg, offset: i16, size: usize) -> Result<u64> {
        let mut buf = [0u8; 8];
        self.read_bytes(prog, reg, offset as i64, &mut buf[..size])?;
        Ok(read_le(&buf[..size]))
    }

    fn store(
        &mut self,
        prog: &Program,
        reg: Reg,
        offset: i16,
        size: usize,
        value: u64,
    ) -> Result<()> {
        self.write_bytes(prog, reg, offset as i64, &value.to_le_bytes()[..size])
    }
}

/// Runs `prog` on a trace event context, returning its `r0` and the number of
/// steps taken.
pub fn run(prog: &Program, ctx: &TraceCtx) -> Result<(u64, u64)> {
    let bytes = ctx.to_bytes();
    run_with_context_impl(prog, &bytes, Some(ctx))
}

/// Runs `prog` on an arbitrary context slice.
///
/// A socket filter passes the packet here. The run is pinned to one CPU for
/// its whole length, which is what makes a per-CPU map coherent within a run.
pub fn run_with_context(prog: &Program, ctx: &[u8]) -> Result<(u64, u64)> {
    run_with_context_impl(prog, ctx, None)
}

fn run_with_context_impl(
    prog: &Program,
    ctx: &[u8],
    trace_context: Option<&TraceCtx>,
) -> Result<(u64, u64)> {
    if prog.insns().len() > MAX_INSNS {
        return_errno_with_message!(Errno::EINVAL, "the program is too long");
    }

    // The guard stays for the whole run: a migration halfway through would
    // make two accesses to the same per-CPU element disagree.
    let guard = disable_preempt();
    let cpu = guard.current_cpu().as_usize() as u32;
    let mut vm = Vm::new(ctx, cpu, trace_context);

    let mut pc = 0usize;
    let mut steps = 0u64;

    loop {
        steps += 1;
        if steps > MAX_STEPS {
            return_errno_with_message!(Errno::EINVAL, "the program ran too long");
        }
        let insn = *prog.insns().get(pc).ok_or_else(|| {
            Error::with_message(Errno::EINVAL, "the program counter left the code")
        })?;

        let mut next = pc + 1;
        match insn.class() {
            Class::Ld => {
                // Only the two-slot 64-bit immediate load exists here.
                if insn.code != 0x18 {
                    return_errno_with_message!(Errno::EINVAL, "unsupported load");
                }
                let high = *prog
                    .insns()
                    .get(pc + 1)
                    .ok_or_else(|| Error::with_message(Errno::EINVAL, "a truncated load"))?;
                match insn.src {
                    PSEUDO_MAP_INDEX => {
                        vm.regs[insn.dst as usize] = Reg {
                            kind: Kind::Map,
                            map: insn.imm as u8,
                            val: 0,
                        };
                    }
                    PSEUDO_MAP_FD | 0 => {
                        let imm = (insn.imm as u32 as u64) | ((high.imm as u32 as u64) << 32);
                        vm.regs[insn.dst as usize] = Reg::scalar(imm);
                    }
                    _ => return_errno_with_message!(Errno::EINVAL, "unsupported pseudo load"),
                }
                next = pc + 2;
            }
            Class::Ldx => {
                let size = insn
                    .width()
                    .ok_or_else(|| Error::with_message(Errno::EINVAL, "bad access width"))?;
                let base = vm.regs[insn.src as usize];
                let value = vm.load(prog, base, insn.off, size)?;
                vm.regs[insn.dst as usize] = Reg::scalar(value);
            }
            Class::St => {
                let size = insn
                    .width()
                    .ok_or_else(|| Error::with_message(Errno::EINVAL, "bad access width"))?;
                let base = vm.regs[insn.dst as usize];
                vm.store(prog, base, insn.off, size, insn.imm as i64 as u64)?;
            }
            Class::Stx => {
                let size = insn
                    .width()
                    .ok_or_else(|| Error::with_message(Errno::EINVAL, "bad access width"))?;
                let base = vm.regs[insn.dst as usize];
                let value = vm.scalar_arg(insn.src)?;
                vm.store(prog, base, insn.off, size, value)?;
            }
            Class::Alu64 => alu(&mut vm, insn, false)?,
            Class::Alu => alu(&mut vm, insn, true)?,
            Class::Jmp | Class::Jmp32 => {
                if insn.op() == op::EXIT {
                    if vm.regs[R0 as usize].kind != Kind::Scalar {
                        return_errno_with_message!(Errno::EINVAL, "r0 is not a value at exit");
                    }
                    return Ok((vm.regs[R0 as usize].val, steps));
                }
                if insn.op() == op::CALL {
                    call_helper(&mut vm, prog, insn)?;
                } else {
                    next = jump(&vm, insn, pc, insn.class() == Class::Jmp32)?;
                }
            }
            Class::Invalid => return_errno_with_message!(Errno::EINVAL, "invalid opcode"),
        }

        pc = next;
    }
}

fn alu(vm: &mut Vm, insn: Insn, is_32: bool) -> Result<()> {
    let dst = vm.regs[insn.dst as usize];
    let src = if insn.uses_src_reg() {
        vm.regs[insn.src as usize]
    } else {
        Reg::scalar(insn.imm as i64 as u64)
    };

    if insn.op() == op::MOV {
        // Moving a pointer is how a program keeps one across an instruction.
        if is_32 && src.kind != Kind::Scalar {
            return_errno_with_message!(Errno::EINVAL, "a 32-bit move cannot carry a pointer");
        }
        vm.regs[insn.dst as usize] = if is_32 {
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
        // The one pointer arithmetic allowed: a context or stack pointer moved
        // by a constant, which is how a program reaches a field or the rest of
        // its frame.
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
            vm.regs[insn.dst as usize] = Reg {
                kind: dst.kind,
                map: 0,
                val: moved as u64,
            };
            return Ok(());
        }
        return_errno_with_message!(Errno::EINVAL, "arithmetic on a pointer");
    }

    let bits = if is_32 { 32 } else { 64 };
    let a = if is_32 {
        dst.val as u32 as u64
    } else {
        dst.val
    };
    let b = if is_32 {
        src.val as u32 as u64
    } else {
        src.val
    };

    let result = match insn.op() {
        op::ADD => a.wrapping_add(b),
        op::SUB => a.wrapping_sub(b),
        op::MUL => a.wrapping_mul(b),
        op::DIV => {
            if b == 0 {
                0
            } else {
                a / b
            }
        }
        op::MOD => {
            if b == 0 {
                a
            } else {
                a % b
            }
        }
        op::OR => a | b,
        op::AND => a & b,
        op::XOR => a ^ b,
        op::LSH => a.wrapping_shl((b % bits as u64) as u32),
        op::RSH => a.wrapping_shr((b % bits as u64) as u32),
        op::ARSH => {
            let shift = (b % bits as u64) as u32;
            if is_32 {
                (a as u32 as i32 >> shift) as u32 as u64
            } else {
                (a as i64 >> shift) as u64
            }
        }
        op::NEG => {
            if is_32 {
                (a as u32).wrapping_neg() as u64
            } else {
                a.wrapping_neg()
            }
        }
        _ => return_errno_with_message!(Errno::EINVAL, "unsupported alu operation"),
    };

    vm.regs[insn.dst as usize] = if is_32 {
        Reg::scalar(result as u32 as u64)
    } else {
        Reg::scalar(result)
    };
    Ok(())
}

fn jump(vm: &Vm, insn: Insn, pc: usize, is_32: bool) -> Result<usize> {
    let dst = vm.regs[insn.dst as usize];
    let src = if insn.uses_src_reg() {
        vm.regs[insn.src as usize]
    } else {
        Reg::scalar(insn.imm as i64 as u64)
    };

    let taken = match insn.op() {
        op::JA => true,
        op::JEQ | op::JNE | op::JGT | op::JGE | op::JLT | op::JLE | op::JSET => {
            if dst.kind != Kind::Scalar {
                // A live pointer is never equal to the scalar zero, which is
                // what `if (ptr == 0)` asks (a failed lookup is the scalar
                // zero, so it takes the other arm).
                match insn.op() {
                    op::JEQ => false,
                    op::JNE => true,
                    _ => {
                        return_errno_with_message!(Errno::EINVAL, "unsupported pointer comparison")
                    }
                }
            } else if src.kind != Kind::Scalar {
                return_errno_with_message!(Errno::EINVAL, "unsupported comparison");
            } else {
                compare(insn.op(), dst.val, src.val, is_32)
            }
        }
        _ => return_errno_with_message!(Errno::EINVAL, "unsupported jump"),
    };

    if taken {
        let target = insn.jump_target(pc);
        if target < 0 {
            return_errno_with_message!(Errno::EINVAL, "a negative jump target");
        }
        Ok(target as usize)
    } else {
        Ok(pc + 1)
    }
}

fn compare(op: u8, a: u64, b: u64, is_32: bool) -> bool {
    if is_32 {
        let a = a as u32;
        let b = b as u32;
        return match op {
            op::JEQ => a == b,
            op::JNE => a != b,
            op::JGT => a > b,
            op::JGE => a >= b,
            op::JLT => a < b,
            op::JLE => a <= b,
            op::JSET => a & b != 0,
            op::JSGT => (a as i32) > (b as i32),
            op::JSGE => (a as i32) >= (b as i32),
            op::JSLT => (a as i32) < (b as i32),
            op::JSLE => (a as i32) <= (b as i32),
            _ => false,
        };
    }
    match op {
        op::JEQ => a == b,
        op::JNE => a != b,
        op::JGT => a > b,
        op::JGE => a >= b,
        op::JLT => a < b,
        op::JLE => a <= b,
        op::JSET => a & b != 0,
        op::JSGT => (a as i64) > (b as i64),
        op::JSGE => (a as i64) >= (b as i64),
        op::JSLT => (a as i64) < (b as i64),
        op::JSLE => (a as i64) <= (b as i64),
        _ => false,
    }
}

/// Collects the key bytes a helper was handed.
fn helper_key(vm: &Vm, prog: &Program, reg: u8, key_size: u32) -> Result<[u8; 8]> {
    let mut key = [0u8; 8];
    vm.read_bytes(
        prog,
        vm.regs[reg as usize],
        0,
        &mut key[..key_size as usize],
    )?;
    Ok(key)
}

/// Calls a helper. `r1..=r5` hold the arguments and are clobbered; the result
/// lands in `r0`.
fn call_helper(vm: &mut Vm, prog: &Program, insn: Insn) -> Result<()> {
    if insn.src != 0 {
        return_errno_with_message!(Errno::EINVAL, "only helper calls are supported");
    }
    let id = insn.imm as u32;

    // Helper failures are reported in `r0` as a negative errno, as Linux does;
    // only an argument that cannot be interpreted at all aborts the run.
    let outcome: Result<Reg> = match id {
        helper_id::MAP_LOOKUP_ELEM => {
            let (map_index, map) = vm.map_arg(prog, R1)?;
            let key = match helper_key(vm, prog, R2, map.key_size()) {
                Ok(key) => key,
                Err(error) => {
                    vm.regs[R0 as usize] = Reg::scalar(errno_value(&error));
                    vm.clear_call_clobbers();
                    return Ok(());
                }
            };
            let key = read_le(&key[..map.key_size() as usize]);
            if map.elem_exists(key) {
                Ok(Reg {
                    kind: Kind::MapValue,
                    map: map_index,
                    val: key,
                })
            } else {
                Ok(Reg::scalar(0))
            }
        }
        helper_id::MAP_UPDATE_ELEM => {
            let (_, map) = vm.map_arg(prog, R1)?;
            let key = helper_key(vm, prog, R2, map.key_size())?;
            let key = read_le(&key[..map.key_size() as usize]);
            let flags = vm.scalar_arg(R4)?;
            let mut result = Ok(Reg::scalar(0));
            if let Err(error) = map.ensure_elem(key, flags) {
                result = Ok(Reg::scalar(errno_value(&error)));
            } else {
                let value_size = map.value_size() as usize;
                let mut buf = [0u8; COPY_CHUNK];
                let mut offset = 0;
                while offset < value_size {
                    let chunk = (value_size - offset).min(COPY_CHUNK);
                    if let Err(error) =
                        vm.read_bytes(prog, vm.regs[R3 as usize], offset as i64, &mut buf[..chunk])
                    {
                        result = Ok(Reg::scalar(errno_value(&error)));
                        break;
                    }
                    if let Err(error) = map.elem_write_bytes(vm.cpu, key, offset, &buf[..chunk]) {
                        result = Ok(Reg::scalar(errno_value(&error)));
                        break;
                    }
                    offset += chunk;
                }
            }
            result
        }
        helper_id::MAP_DELETE_ELEM => {
            let (_, map) = vm.map_arg(prog, R1)?;
            let key = helper_key(vm, prog, R2, map.key_size())?;
            let result = match map.delete(&key[..map.key_size() as usize]) {
                Ok(()) => Ok(Reg::scalar(0)),
                Err(error) => Ok(Reg::scalar(errno_value(&error))),
            };
            result
        }
        helper_id::RINGBUF_OUTPUT => {
            let (_, map) = vm.map_arg(prog, R1)?;
            let size = vm.scalar_arg(R3)? as usize;
            if size == 0 || size > MAX_RINGBUF_RECORD {
                Ok(Reg::scalar(errno_value(&Error::new(Errno::EINVAL))))
            } else {
                let mut record = [0u8; MAX_RINGBUF_RECORD];
                match vm.read_bytes(prog, vm.regs[R2 as usize], 0, &mut record[..size]) {
                    Ok(()) => match map.ring_output(&record[..size]) {
                        Ok(_) => Ok(Reg::scalar(0)),
                        Err(error) => Ok(Reg::scalar(errno_value(&error))),
                    },
                    Err(error) => Ok(Reg::scalar(errno_value(&error))),
                }
            }
        }
        helper_id::KSYMS_LOOKUP => {
            let mut name = [0u8; 64];
            let len = (vm.scalar_arg(R2)? as usize).min(name.len());
            if len == 0 {
                Ok(Reg::scalar(0))
            } else {
                match vm.read_bytes(prog, vm.regs[R1 as usize], 0, &mut name[..len]) {
                    Ok(()) => {
                        let text = core::str::from_utf8(&name[..len]).unwrap_or("\0");
                        Ok(Reg::scalar(trace::kallsyms::lookup_name(text).unwrap_or(0)))
                    }
                    Err(error) => Ok(Reg::scalar(errno_value(&error))),
                }
            }
        }
        helper_id::KTIME_GET_NS => Ok(Reg::scalar(vm.timestamp_ns)),
        helper_id::GET_CURRENT_PID_TGID => Ok(Reg::scalar(((vm.pid as u64) << 32) | vm.tid as u64)),
        helper_id::GET_SMP_PROCESSOR_ID => Ok(Reg::scalar(vm.cpu as u64)),
        helper_id::TRACE_PRINTK => {
            let reg = vm.regs[R1 as usize];
            let mut format = [0u8; 64];
            let len = match vm.scalar_arg(R2) {
                Ok(len) => (len as usize).min(format.len()),
                Err(error) => return Err(error),
            };
            vm.read_bytes(prog, reg, 0, &mut format[..len])?;
            let mut args = [0u64; 3];
            for (slot, reg) in args.iter_mut().zip([R3, R4, R5]) {
                *slot = vm.scalar_arg(reg)?;
            }
            print_formatted(vm, &format[..len], &args);
            Ok(Reg::scalar(0))
        }
        _ => return_errno_with_message!(Errno::EINVAL, "unknown helper"),
    };

    match outcome {
        Ok(reg) => vm.regs[R0 as usize] = reg,
        Err(error) => vm.regs[R0 as usize] = Reg::scalar(errno_value(&error)),
    }
    vm.clear_call_clobbers();
    Ok(())
}

impl Vm<'_> {
    /// A helper call clobbers `r1..=r5`.
    fn clear_call_clobbers(&mut self) {
        for reg in 1..=5usize {
            self.regs[reg] = Reg::UNINIT;
        }
    }
}

fn errno_value(error: &Error) -> u64 {
    (-(error.error() as i32) as i64) as u64
}

/// Renders `bpf_trace_printk`'s format into the trace ring.
///
/// Only `%d`, `%u`, `%x`, `%p`, `%c`, `%%` and optional length modifiers are
/// understood; a program that needs more should write into a map instead.
/// Nothing here allocates.
fn print_formatted(vm: &Vm, format: &[u8], args: &[u64; 3]) {
    let mut out = [0u8; 96];
    let mut len = 0usize;
    let mut next_arg = 0usize;
    let mut i = 0usize;

    while i < format.len() && len < out.len() {
        if format[i] != b'%' || i + 1 >= format.len() {
            push_bytes(&mut out, &mut len, &[format[i]]);
            i += 1;
            continue;
        }

        // Skip a length modifier.
        let mut j = i + 1;
        while j < format.len() && matches!(format[j], b'l' | b'h' | b'z') {
            j += 1;
        }
        if j >= format.len() {
            break;
        }

        let arg = if next_arg < 3 {
            let arg = args[next_arg];
            next_arg += 1;
            arg
        } else {
            0
        };

        match format[j] {
            b'%' => push_bytes(&mut out, &mut len, b"%"),
            b'd' | b'i' => push_number(&mut out, &mut len, arg as i64, true, 10),
            b'u' => push_number(&mut out, &mut len, arg as i64, false, 10),
            b'x' | b'p' => {
                push_bytes(&mut out, &mut len, b"0x");
                push_number(&mut out, &mut len, arg as i64, false, 16);
            }
            b'c' => push_bytes(&mut out, &mut len, &[arg as u8]),
            other => {
                push_bytes(&mut out, &mut len, b"%");
                push_bytes(&mut out, &mut len, &[other]);
            }
        }
        i = j + 1;
    }

    let marker = trace::event::event_named("tracing", "trace_marker");
    let text = core::str::from_utf8(&out[..len]).unwrap_or("<binary>");
    if let Some(event) = marker {
        let timestamp = if vm.timestamp_ns != 0 {
            core::time::Duration::from_nanos(vm.timestamp_ns)
        } else {
            ostd::timer::Jiffies::elapsed().as_duration()
        };
        trace::ring::push_at(event.id(), vm.pid, vm.tid, text.as_bytes(), timestamp);
    }
}

fn push_bytes(out: &mut [u8; 96], len: &mut usize, bytes: &[u8]) {
    for byte in bytes {
        if *len >= out.len() {
            return;
        }
        out[*len] = *byte;
        *len += 1;
    }
}

/// Appends `value` in `base`, with a sign when `signed`.
fn push_number(out: &mut [u8; 96], len: &mut usize, value: i64, signed: bool, base: u64) {
    let mut digits = [0u8; 24];
    let mut count = 0usize;
    let negative = signed && value < 0;
    let mut magnitude = if negative {
        (value as i128).unsigned_abs() as u128
    } else {
        value as u64 as u128
    };

    loop {
        let digit = (magnitude % base as u128) as u32;
        digits[count] = core::char::from_digit(digit, base as u32).unwrap_or('0') as u8;
        count += 1;
        magnitude /= base as u128;
        if magnitude == 0 {
            break;
        }
    }

    if negative {
        push_bytes(out, len, b"-");
    }
    while count > 0 {
        count -= 1;
        push_bytes(out, len, &[digits[count]]);
    }
}
