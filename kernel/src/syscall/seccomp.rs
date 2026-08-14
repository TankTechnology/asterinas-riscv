// SPDX-License-Identifier: MPL-2.0

use ostd::mm::VmIo;

use super::SyscallReturn;
use crate::{
    prelude::*,
    process::signal::{constants::SIGSYS, c_types::siginfo_t, sig_num::SigNum, signals::Signal},
};

/// Seccomp modes (values of the per-thread `seccomp_mode` field).
pub const SECCOMP_MODE_DISABLED: u32 = 0;
pub const SECCOMP_MODE_STRICT: u32 = 1;
pub const SECCOMP_MODE_FILTER: u32 = 2;

// --- `seccomp(2)` operations (`linux/seccomp.h`) ---
const SECCOMP_SET_MODE_STRICT: u32 = 0;
const SECCOMP_SET_MODE_FILTER: u32 = 1;

/// `si_code` value for a seccomp-generated `SIGSYS` (`asm-generic/siginfo.h`).
const SYS_SECCOMP: i32 = 1;

/// The syscalls permitted in `SECCOMP_SET_MODE_STRICT`, by riscv64 asm-generic
/// number: `read`, `write`, `exit` and `rt_sigreturn`. `exit_group` is, per the
/// `seccomp(2)` man page, deliberately *not* permitted.
///
/// Reference: <https://man7.org/linux/man-pages/man2/seccomp.2.html>
const STRICT_ALLOWED: &[u64] = &[63 /* read */, 64 /* write */, 93 /* exit */, 139 /* rt_sigreturn */];

/// The maximum number of instructions in a seccomp BPF filter (`BPF_MAXINSNS`).
const BPF_MAXINSNS: usize = 4096;

// --- seccomp filter return values (`linux/seccomp.h`) ---
const SECCOMP_RET_KILL_THREAD: u32 = 0x0000_0000;
const SECCOMP_RET_KILL_PROCESS: u32 = 0x8000_0000;
const SECCOMP_RET_TRAP: u32 = 0x0003_0000;
const SECCOMP_RET_ERRNO: u32 = 0x0005_0000;
const SECCOMP_RET_LOG: u32 = 0x7ffc_0000;
const SECCOMP_RET_ALLOW: u32 = 0x7fff_0000;
const SECCOMP_RET_ACTION_FULL: u32 = 0xffff_0000;
const SECCOMP_RET_DATA: u32 = 0x0000_ffff;

/// `AUDIT_ARCH_RISCV64` = `EM_RISCV | __AUDIT_ARCH_64BIT | __AUDIT_ARCH_LE`.
const AUDIT_ARCH_RISCV64: u32 = 0xc000_00f3;

// --- classic BPF opcodes (`linux/filter.h`) ---
const BPF_LD: u16 = 0x00;
const BPF_LDX: u16 = 0x01;
const BPF_ALU: u16 = 0x04;
const BPF_JMP: u16 = 0x05;
const BPF_RET: u16 = 0x06;

/// The outcome of evaluating a thread's seccomp policy for a syscall.
#[derive(Debug, PartialEq)]
pub enum SeccompDecision {
    /// The syscall is allowed to proceed.
    Allow,
    /// The syscall is blocked: deliver `SIGSYS` and return `ENOSYS`.
    Kill,
    /// The syscall is blocked: return the given `-errno` without a signal.
    Errno(i32),
}

/// A classic BPF instruction (`struct sock_filter` in `linux/filter.h`).
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct SockFilter {
    pub code: u16,
    pub jt: u8,
    pub jf: u8,
    pub k: u32,
}

/// Returns whether the calling thread's seccomp policy blocks `syscall_number`.
///
/// Strict mode rejects any syscall outside the strict allowlist. Filter mode
/// evaluates the thread's classic-BPF program against `seccomp_data` built from
/// `args` and `instruction_pointer`, then maps the filter's return value to an
/// action. A thread with no policy (disabled) always allows.
pub fn check(
    ctx: &Context,
    syscall_number: u64,
    args: &[u64; 6],
    instruction_pointer: usize,
) -> SeccompDecision {
    match ctx.posix_thread.seccomp_mode() {
        SECCOMP_MODE_STRICT => {
            if STRICT_ALLOWED.contains(&syscall_number) {
                SeccompDecision::Allow
            } else {
                SeccompDecision::Kill
            }
        }
        SECCOMP_MODE_FILTER => {
            let Some(filter) = ctx.posix_thread.seccomp_filter() else {
                return SeccompDecision::Allow;
            };
            let data = build_seccomp_data(syscall_number, args, instruction_pointer);
            seccomp_action_to_decision(run_filter(&filter, &data))
        }
        _ => SeccompDecision::Allow,
    }
}

/// Builds the `seccomp_data` structure (64 bytes on riscv64) that a filter's
/// `BPF_LD | BPF_W | BPF_ABS` instructions read from.
fn build_seccomp_data(syscall_number: u64, args: &[u64; 6], instruction_pointer: usize) -> [u8; 64] {
    let mut data = [0u8; 64];
    data[0..4].copy_from_slice(&(syscall_number as i32).to_ne_bytes());
    data[4..8].copy_from_slice(&AUDIT_ARCH_RISCV64.to_ne_bytes());
    data[8..16].copy_from_slice(&(instruction_pointer as u64).to_ne_bytes());
    for (i, arg) in args.iter().enumerate() {
        data[16 + 8 * i..24 + 8 * i].copy_from_slice(&arg.to_ne_bytes());
    }
    data
}

/// Maps a seccomp filter return value to a [`SeccompDecision`].
fn seccomp_action_to_decision(action: u32) -> SeccompDecision {
    match action & SECCOMP_RET_ACTION_FULL {
        SECCOMP_RET_ALLOW | SECCOMP_RET_LOG => SeccompDecision::Allow,
        SECCOMP_RET_ERRNO => SeccompDecision::Errno((action & SECCOMP_RET_DATA) as i32),
        // KILL (thread/process), TRAP, TRACE and USER_NOTIF all block the syscall
        // and deliver `SIGSYS` (there is no ptrace/user-notification support, so
        // the signal path is the closest available behaviour).
        _ => SeccompDecision::Kill,
    }
}

/// Runs a classic-BPF program against `data` and returns the `RET` value.
///
/// This implements the subset of classic BPF that libseccomp emits for syscall
/// filtering: `LD` (immediate and absolute word load), `LDX`, the `ALU`
/// arithmetic/logic ops, the `JMP` comparisons and `RET`. Any unrecognized or
/// out-of-bounds instruction yields `SECCOMP_RET_KILL_THREAD` (fail secure).
fn run_filter(prog: &[SockFilter], data: &[u8; 64]) -> u32 {
    let mut a: u32 = 0;
    let mut x: u32 = 0;
    let mut pc: usize = 0;

    while pc < prog.len() {
        let ins = prog[pc];
        let code = ins.code;
        let k = ins.k;

        match code & 0x07 {
            BPF_LD => match code & 0xe0 {
                // `LD | W | IMM`: a = k
                0x00 => a = k,
                // `LD | W | ABS`: a = *(u32 *)(data + k)
                0x20 => match load_word(data, k) {
                    Some(word) => a = word,
                    None => return SECCOMP_RET_KILL_THREAD,
                },
                _ => return SECCOMP_RET_KILL_THREAD,
            },
            BPF_LDX => match code & 0xe0 {
                0x00 => x = k,
                0x20 => match load_word(data, k) {
                    Some(word) => x = word,
                    None => return SECCOMP_RET_KILL_THREAD,
                },
                _ => return SECCOMP_RET_KILL_THREAD,
            },
            BPF_ALU => {
                // bit 3 selects the source: K (immediate) or X (index register).
                let src = if code & 0x08 != 0 { x } else { k };
                a = match code & 0xf0 {
                    0x00 => a.wrapping_add(src),                    // ADD
                    0x10 => a.wrapping_sub(src),                    // SUB
                    0x20 => a.wrapping_mul(src),                    // MUL
                    0x30 if src != 0 => a / src,                    // DIV
                    0x40 => a | src,                                // OR
                    0x50 => a & src,                                // AND
                    0x60 => a.wrapping_shl(src),                    // LSH
                    0x70 => a.wrapping_shr(src),                    // RSH
                    0x80 => a.wrapping_neg(),                       // NEG
                    0x90 if src != 0 => a % src,                    // MOD
                    0xa0 => a ^ src,                                // XOR
                    _ => return SECCOMP_RET_KILL_THREAD,
                };
            }
            BPF_JMP => {
                let jmp_op = code & 0xf0;
                if jmp_op == 0x00 {
                    // `JMP | JA`: unconditional jump by k instructions.
                    pc = pc.wrapping_add(k as usize + 1);
                    continue;
                }
                let src = if code & 0x08 != 0 { x } else { k };
                let taken = match jmp_op {
                    0x10 => a == src,          // JEQ
                    0x20 => a > src,           // JGT
                    0x30 => a >= src,          // JGE
                    0x40 => (a & src) != 0,    // JSET
                    _ => return SECCOMP_RET_KILL_THREAD,
                };
                let offset = if taken { ins.jt } else { ins.jf } as usize;
                pc = pc.wrapping_add(offset + 1);
                continue;
            }
            BPF_RET => {
                return match code & 0x18 {
                    0x00 => k, // `RET | K`
                    0x10 => a, // `RET | A`
                    _ => SECCOMP_RET_KILL_THREAD,
                };
            }
            _ => return SECCOMP_RET_KILL_THREAD,
        }

        pc += 1;
    }

    // The program fell off the end without a `RET`; fail secure.
    SECCOMP_RET_KILL_THREAD
}

/// Loads a 32-bit little-endian word from `data` at byte offset `k`.
fn load_word(data: &[u8; 64], k: u32) -> Option<u32> {
    let offset = k as usize;
    if offset.checked_add(4)? > data.len() {
        return None;
    }
    Some(u32::from_ne_bytes([
        data[offset],
        data[offset + 1],
        data[offset + 2],
        data[offset + 3],
    ]))
}

/// A `SIGSYS` signal raised by seccomp.
///
/// The `_sigsys` detail fields (`si_call_addr` / `si_syscall` / `si_arch`) are
/// not populated (the `siginfo_t` model does not expose them yet); `si_code` is
/// set to `SYS_SECCOMP` so a proper `SIGSYS` handler can distinguish seccomp
/// kills from other sources.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct SigsysSignal {
    syscall: u32,
}

impl SigsysSignal {
    pub const fn new(syscall: u32) -> Self {
        Self { syscall }
    }
}

impl Signal for SigsysSignal {
    fn num(&self) -> SigNum {
        SIGSYS
    }

    fn to_info(&self) -> siginfo_t {
        let _ = self.syscall;
        siginfo_t::new(SIGSYS, SYS_SECCOMP)
    }
}

pub fn sys_seccomp(operation: u32, flags: u32, args: Vaddr, ctx: &Context) -> Result<SyscallReturn> {
    debug!(
        "seccomp operation = {}, flags = {:#x}, args = {:#x}",
        operation, flags, args
    );

    match operation {
        SECCOMP_SET_MODE_STRICT => {
            // Strict mode takes no flags and no args.
            if flags != 0 {
                return_errno_with_message!(Errno::EINVAL, "seccomp strict mode takes no flags");
            }
            if args != 0 {
                return_errno_with_message!(Errno::EINVAL, "seccomp strict mode takes no args");
            }
            ctx.posix_thread.set_seccomp_mode(SECCOMP_MODE_STRICT);
            Ok(SyscallReturn::Return(0))
        }
        SECCOMP_SET_MODE_FILTER => {
            // `SECCOMP_FILTER_FLAG_TSYNC` / `SECCOMP_FILTER_FLAG_NEW_LISTENER` /
            // `SECCOMP_FILTER_FLAG_WAIT_KILLABLE_RECV` are not supported yet.
            if flags != 0 {
                return_errno_with_message!(Errno::EINVAL, "unsupported seccomp filter flags");
            }
            if args == 0 {
                return_errno_with_message!(Errno::EFAULT, "null sock_fprog pointer");
            }

            let user_space = ctx.user_space();
            // `struct sock_fprog` is `{ u16 len; struct sock_filter *filter }`;
            // on riscv64 the pointer is 8-byte aligned, so `filter` sits at
            // offset 8. Read the two fields separately to avoid reading the
            // struct's implicit padding.
            let len = user_space.read_val::<u16>(args)? as usize;
            let filter_addr = user_space.read_val::<Vaddr>(
                args.checked_add(8)
                    .ok_or_else(|| Error::with_message(Errno::EFAULT, "sock_fprog overflow"))?,
            )?;
            if len == 0 || len > BPF_MAXINSNS {
                return_errno_with_message!(Errno::EINVAL, "invalid filter length");
            }

            let mut filters = Vec::with_capacity(len);
            for i in 0..len {
                let addr = filter_addr
                    .checked_add(i * size_of::<SockFilter>())
                    .ok_or_else(|| Error::with_message(Errno::EFAULT, "filter address overflow"))?;
                filters.push(user_space.read_val::<SockFilter>(addr)?);
            }

            // Validate jumps stay in-bounds (the interpreter also fails secure on
            // out-of-bounds jumps, but reject obvious malformed programs up front
            // like Linux's verifier does).
            for (i, ins) in filters.iter().enumerate() {
                if ins.code & 0x07 != BPF_JMP {
                    continue;
                }
                let offsets: &[usize] = if ins.code & 0xf0 == 0x00 {
                    // `JMP | JA`: offset is in `k`.
                    &[ins.k as usize]
                } else {
                    &[ins.jt as usize, ins.jf as usize]
                };
                for &offset in offsets {
                    if i.checked_add(offset + 1).is_none_or(|t| t > len) {
                        return_errno_with_message!(Errno::EINVAL, "BPF jump out of bounds");
                    }
                }
            }

            ctx.posix_thread
                .set_seccomp_filter(Arc::from(filters.into_boxed_slice()));
            ctx.posix_thread.set_seccomp_mode(SECCOMP_MODE_FILTER);
            Ok(SyscallReturn::Return(0))
        }
        _ => return_errno_with_message!(Errno::EINVAL, "unknown seccomp operation"),
    }
}
