// SPDX-License-Identifier: MPL-2.0

//! eBPF instructions, decoded from the 8-byte wire format.
//!
//! The encoding is Linux's (`struct bpf_insn` in `linux/bpf.h`): one byte of
//! opcode split into class, size, mode and operation, a nibble each for the
//! destination and source register, a 16-bit offset and a 32-bit immediate.
//! Only what this kernel implements is listed.

/// One decoded instruction.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Insn {
    pub code: u8,
    pub dst: u8,
    pub src: u8,
    pub off: i16,
    pub imm: i32,
}

impl Insn {
    pub const SIZE: usize = 8;

    pub fn from_bytes(bytes: &[u8; Self::SIZE]) -> Self {
        Self {
            code: bytes[0],
            dst: bytes[1] & 0x0f,
            src: bytes[1] >> 4,
            off: i16::from_le_bytes([bytes[2], bytes[3]]),
            imm: i32::from_le_bytes([bytes[4], bytes[5], bytes[6], bytes[7]]),
        }
    }

    pub fn class(&self) -> Class {
        match self.code & 0x07 {
            0x00 => Class::Ld,
            0x01 => Class::Ldx,
            0x02 => Class::St,
            0x03 => Class::Stx,
            0x04 => Class::Alu,
            0x05 => Class::Jmp,
            0x06 => Class::Jmp32,
            0x07 => Class::Alu64,
            _ => Class::Invalid,
        }
    }

    /// Access width, from the size bits.
    pub fn width(&self) -> Option<usize> {
        match self.code & 0x18 {
            0x00 => Some(4),
            0x08 => Some(2),
            0x10 => Some(1),
            0x18 => Some(8),
            _ => None,
        }
    }

    /// The operation, for ALU and JMP classes.
    pub fn op(&self) -> u8 {
        self.code & 0xf0
    }

    /// Whether the source operand is a register rather than the immediate.
    pub fn uses_src_reg(&self) -> bool {
        self.code & 0x08 != 0
    }

    /// The absolute jump target, relative to the instruction's own index.
    pub fn jump_target(&self, index: usize) -> isize {
        index as isize + 1 + self.off as isize
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Class {
    Ld,
    Ldx,
    St,
    Stx,
    Alu,
    Jmp,
    Jmp32,
    Alu64,
    Invalid,
}

/// ALU/JMP operations (the high nibble of the opcode).
pub mod op {
    pub const ADD: u8 = 0x00;
    pub const SUB: u8 = 0x10;
    pub const MUL: u8 = 0x20;
    pub const DIV: u8 = 0x30;
    pub const OR: u8 = 0x40;
    pub const AND: u8 = 0x50;
    pub const LSH: u8 = 0x60;
    pub const RSH: u8 = 0x70;
    pub const NEG: u8 = 0x80;
    pub const MOD: u8 = 0x90;
    pub const XOR: u8 = 0xa0;
    pub const MOV: u8 = 0xb0;
    pub const ARSH: u8 = 0xc0;
    #[allow(dead_code)] // byte swap: part of the ISA, not implemented yet
    pub const END: u8 = 0xd0;

    pub const JA: u8 = 0x00;
    pub const JEQ: u8 = 0x10;
    pub const JGT: u8 = 0x20;
    pub const JGE: u8 = 0x30;
    pub const JSET: u8 = 0x40;
    pub const JNE: u8 = 0x50;
    pub const JSGT: u8 = 0x60;
    pub const JSGE: u8 = 0x70;
    pub const CALL: u8 = 0x80;
    pub const EXIT: u8 = 0x90;
    pub const JLT: u8 = 0xa0;
    pub const JLE: u8 = 0xb0;
    pub const JSLT: u8 = 0xc0;
    pub const JSLE: u8 = 0xd0;
}

/// Registers.
pub const R0: u8 = 0;
pub const R1: u8 = 1;
pub const R2: u8 = 2;
pub const R3: u8 = 3;
pub const R4: u8 = 4;
pub const R5: u8 = 5;
pub const R10: u8 = 10;
pub const REG_COUNT: usize = 11;

/// Largest stack a program may use, below `r10`.
pub const STACK_SIZE: usize = 512;

/// Instruction limit, as Linux's `BPF_MAXINSNS`.
pub const MAX_INSNS: usize = 4096;

/// `src_reg` of a 64-bit immediate load that names a map by file descriptor,
/// as the loader writes it.
pub const PSEUDO_MAP_FD: u8 = 1;
/// `src_reg` of a 64-bit immediate load that names a map by its index in the
/// program's map table. The verifier rewrites a [`PSEUDO_MAP_FD`] load into
/// this form, which is what the interpreter executes.
pub const PSEUDO_MAP_INDEX: u8 = 2;

/// Helper ids the interpreter implements (Linux numbers where they exist).
pub mod helper_id {
    /// `bpf_map_lookup_elem`
    pub const MAP_LOOKUP_ELEM: u32 = 1;
    /// `bpf_map_update_elem`
    pub const MAP_UPDATE_ELEM: u32 = 2;
    /// `bpf_map_delete_elem`
    pub const MAP_DELETE_ELEM: u32 = 3;
    /// `bpf_ktime_get_ns`
    pub const KTIME_GET_NS: u32 = 5;
    /// `bpf_get_smp_processor_id`
    pub const GET_SMP_PROCESSOR_ID: u32 = 8;
    /// `bpf_get_current_pid_tgid`
    pub const GET_CURRENT_PID_TGID: u32 = 14;
    /// `bpf_trace_printk`: formats into the trace ring.
    pub const TRACE_PRINTK: u32 = 6;
}
