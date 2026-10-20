// SPDX-License-Identifier: MPL-2.0

//! Classic BPF (cBPF) support.
//!
//! The instruction format (`struct sock_filter` in `linux/filter.h`) is shared
//! by seccomp filters and socket filters (`SO_ATTACH_FILTER`). This module
//! provides the instruction type and an interpreter.

use ostd::mm::VmIo;

use crate::prelude::*;

/// The maximum number of instructions in a classic BPF program
/// (`BPF_MAXINSNS` in `linux/filter_common.h`).
pub const BPF_MAX_INSNS: usize = 4096;

/// A classic BPF instruction (`struct sock_filter` in `linux/filter.h`).
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct SockFilter {
    pub code: u16,
    pub jt: u8,
    pub jf: u8,
    pub k: u32,
}

/// Reads a classic BPF program (`struct sock_fprog` in `linux/filter.h`) from
/// the current user space.
///
/// On 64-bit, `struct sock_fprog` is `{ u16 len; padding; sock_filter *filter }`
/// with `filter` at offset 8. The jump targets are validated up front (the
/// interpreter also rejects out-of-bounds jumps, but catching malformed
/// programs early matches Linux's `bpf_check_classic`).
pub fn read_prog_from_user(addr: Vaddr) -> Result<Vec<SockFilter>> {
    let task = ostd::task::Task::current().unwrap();
    let thread_local = AsThreadLocal::as_thread_local(&task).unwrap();
    let user_space = CurrentUserSpace::new(thread_local);

    let len = user_space.read_val::<u16>(addr)? as usize;
    let filter_addr = user_space.read_val::<Vaddr>(
        addr.checked_add(8)
            .ok_or_else(|| Error::with_message(Errno::EFAULT, "sock_fprog overflow"))?,
    )?;

    if len == 0 || len > BPF_MAX_INSNS {
        return_errno_with_message!(Errno::EINVAL, "invalid filter length");
    }

    let mut insns = Vec::with_capacity(len);
    for i in 0..len {
        let insn_addr = filter_addr
            .checked_add(i * size_of::<SockFilter>())
            .ok_or_else(|| Error::with_message(Errno::EFAULT, "filter address overflow"))?;
        insns.push(user_space.read_val::<SockFilter>(insn_addr)?);
    }

    // Validate that jump targets stay within the program.
    for (i, insn) in insns.iter().enumerate() {
        if insn.code & 0x07 != 0x05 {
            // Not a `JMP` instruction.
            continue;
        }
        let offsets: &[usize] = if insn.code & 0xf0 == 0x00 {
            // `JMP | JA`: the offset is in `k`.
            &[insn.k as usize]
        } else {
            &[insn.jt as usize, insn.jf as usize]
        };
        for &offset in offsets {
            if i.checked_add(offset + 1).is_none_or(|target| target > len) {
                return_errno_with_message!(Errno::EINVAL, "BPF jump out of bounds");
            }
        }
    }

    Ok(insns)
}

// Instruction classes (`code & 0x07`).
const BPF_LD: u16 = 0x00;
const BPF_LDX: u16 = 0x01;
const BPF_ST: u16 = 0x02;
const BPF_STX: u16 = 0x03;
const BPF_ALU: u16 = 0x04;
const BPF_JMP: u16 = 0x05;
const BPF_RET: u16 = 0x06;
const BPF_MISC: u16 = 0x07;

// Load/store sizes (`code & 0x18`).
const BPF_W: u16 = 0x00;
const BPF_H: u16 = 0x08;
const BPF_B: u16 = 0x10;

// Addressing modes (`code & 0xe0`).
const BPF_IMM: u16 = 0x00;
const BPF_ABS: u16 = 0x20;
const BPF_IND: u16 = 0x40;
const BPF_MEM: u16 = 0x60;
const BPF_LEN: u16 = 0x80;
const BPF_MSH: u16 = 0xa0;

/// The number of scratch memory slots of a classic BPF program.
const BPF_MEMWORDS: usize = 16;

/// Runs a classic BPF program against `data`, returning the value of the
/// terminating `RET` instruction.
///
/// Returns `None` for malformed programs (unknown instructions, out-of-bounds
/// memory access, or falling off the end without a `RET`). The caller decides
/// how to map that to user-visible behavior (seccomp fails secure; socket
/// filters drop the packet).
///
/// `big_endian_loads` selects the byte order for multi-byte `ABS`/`IND` loads:
/// socket filters read packet data in network byte order (big endian), while
/// seccomp filters read the native-endian `seccomp_data` structure.
pub fn run_filter(insns: &[SockFilter], data: &[u8], big_endian_loads: bool) -> Option<u32> {
    let mut a: u32 = 0;
    let mut x: u32 = 0;
    let mut mem = [0u32; BPF_MEMWORDS];
    let mut pc: usize = 0;

    while pc < insns.len() {
        let ins = insns[pc];
        let code = ins.code;
        let k = ins.k;

        match code & 0x07 {
            BPF_LD => {
                a = match code & 0xe0 {
                    // `LD | IMM`: a = k
                    BPF_IMM => k,
                    // `LD | ABS`: a = data[k .. k + size]
                    BPF_ABS => load(data, k, 0, code & 0x18, big_endian_loads)?,
                    // `LD | IND`: a = data[x + k .. x + k + size]
                    BPF_IND => load(data, k, x, code & 0x18, big_endian_loads)?,
                    // `LD | MEM`: a = mem[k]
                    BPF_MEM => *mem.get(k as usize)?,
                    // `LD | LEN`: a = data.len()
                    BPF_LEN => data.len() as u32,
                    _ => return None,
                };
            }
            BPF_LDX => {
                x = match code & 0xe0 {
                    // `LDX | IMM`: x = k
                    BPF_IMM => k,
                    // `LDX | MEM`: x = mem[k]
                    BPF_MEM => *mem.get(k as usize)?,
                    // `LDX | LEN`: x = data.len()
                    BPF_LEN => data.len() as u32,
                    // `LDX | MSH`: x = 4 * (data[k] & 0x0f) (IP header length)
                    BPF_MSH => {
                        let byte = *data.get(k as usize)?;
                        4 * (byte as u32 & 0x0f)
                    }
                    // Not part of the classic socket-filter ISA, but accepted
                    // for compatibility with the previous seccomp interpreter.
                    BPF_ABS => load(data, k, 0, code & 0x18, big_endian_loads)?,
                    _ => return None,
                };
            }
            BPF_ST => {
                // `ST`: mem[k] = a
                *mem.get_mut(k as usize)? = a;
            }
            BPF_STX => {
                // `STX`: mem[k] = x
                *mem.get_mut(k as usize)? = x;
            }
            BPF_ALU => {
                // Bit 3 selects the source: K (immediate) or X (index register).
                let src = if code & 0x08 != 0 { x } else { k };
                a = match code & 0xf0 {
                    0x00 => a.wrapping_add(src), // ADD
                    0x10 => a.wrapping_sub(src), // SUB
                    0x20 => a.wrapping_mul(src), // MUL
                    0x30 if src != 0 => a / src, // DIV
                    0x40 => a | src,             // OR
                    0x50 => a & src,             // AND
                    0x60 => a.wrapping_shl(src), // LSH
                    0x70 => a.wrapping_shr(src), // RSH
                    0x80 => a.wrapping_neg(),    // NEG
                    0x90 if src != 0 => a % src, // MOD
                    0xa0 => a ^ src,             // XOR
                    _ => return None,
                };
            }
            BPF_JMP => {
                let jmp_op = code & 0xf0;
                if jmp_op == 0x00 {
                    // `JMP | JA`: unconditional jump by k instructions.
                    pc = pc.wrapping_add(k as usize).checked_add(1)?;
                    continue;
                }
                let src = if code & 0x08 != 0 { x } else { k };
                let taken = match jmp_op {
                    0x10 => a == src,       // JEQ
                    0x20 => a > src,        // JGT
                    0x30 => a >= src,       // JGE
                    0x40 => (a & src) != 0, // JSET
                    _ => return None,
                };
                let offset = if taken { ins.jt } else { ins.jf } as usize;
                pc = pc.wrapping_add(offset).checked_add(1)?;
                continue;
            }
            BPF_RET => {
                return match code & 0x18 {
                    BPF_W => Some(k), // `RET | K`
                    BPF_B => Some(a), // `RET | A`
                    _ => None,
                };
            }
            BPF_MISC => match code & 0xf8 {
                0x00 => x = a, // `MISC | TAX`
                0x80 => a = x, // `MISC | TXA`
                _ => return None,
            },
            _ => return None,
        }

        pc += 1;
    }

    // The program fell off the end without a `RET`.
    None
}

/// Loads a `size`-byte value from `data` at byte offset `base + extra`.
///
/// Socket filters read packet data in network byte order (big endian), while
/// seccomp filters read the native-endian `seccomp_data` structure.
fn load(data: &[u8], base: u32, extra: u32, size: u16, big_endian: bool) -> Option<u32> {
    let offset = (base as usize).checked_add(extra as usize)?;
    let end = offset.checked_add(match size {
        BPF_W => 4,
        BPF_H => 2,
        BPF_B => 1,
        _ => return None,
    })?;
    let bytes = data.get(offset..end)?;

    Some(match size {
        BPF_W if big_endian => u32::from_be_bytes(bytes.try_into().unwrap()),
        BPF_W => u32::from_ne_bytes(bytes.try_into().unwrap()),
        BPF_H if big_endian => u16::from_be_bytes(bytes.try_into().unwrap()) as u32,
        BPF_H => u16::from_ne_bytes(bytes.try_into().unwrap()) as u32,
        _ => bytes[0] as u32,
    })
}
