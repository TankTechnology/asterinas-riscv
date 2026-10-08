// SPDX-License-Identifier: MPL-2.0

//! RISC-V ptrace general-purpose register ABI.

use ostd::{arch::cpu::context::UserContext, user::UserContextApi};

use crate::prelude::*;

/// Linux `struct user_regs_struct` in the `NT_PRSTATUS` regset.
///
/// Reference: <https://codebrowser.dev/linux/linux/arch/riscv/include/uapi/asm/ptrace.h.html#24>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub struct CUserRegsStruct {
    pc: usize,
    ra: usize,
    sp: usize,
    gp: usize,
    tp: usize,
    t0: usize,
    t1: usize,
    t2: usize,
    s0: usize,
    s1: usize,
    a0: usize,
    a1: usize,
    a2: usize,
    a3: usize,
    a4: usize,
    a5: usize,
    a6: usize,
    a7: usize,
    s2: usize,
    s3: usize,
    s4: usize,
    s5: usize,
    s6: usize,
    s7: usize,
    s8: usize,
    s9: usize,
    s10: usize,
    s11: usize,
    t3: usize,
    t4: usize,
    t5: usize,
    t6: usize,
}

impl CUserRegsStruct {
    /// Captures the Linux ABI register order while the tracee is stopped.
    pub fn from_user_context(ctx: &UserContext) -> Self {
        let regs = ctx.general_regs();
        Self {
            pc: ctx.instruction_pointer(),
            ra: regs.ra,
            sp: regs.sp,
            gp: regs.gp,
            tp: regs.tp,
            t0: regs.t0,
            t1: regs.t1,
            t2: regs.t2,
            s0: regs.s0,
            s1: regs.s1,
            a0: regs.a0,
            a1: regs.a1,
            a2: regs.a2,
            a3: regs.a3,
            a4: regs.a4,
            a5: regs.a5,
            a6: regs.a6,
            a7: regs.a7,
            s2: regs.s2,
            s3: regs.s3,
            s4: regs.s4,
            s5: regs.s5,
            s6: regs.s6,
            s7: regs.s7,
            s8: regs.s8,
            s9: regs.s9,
            s10: regs.s10,
            s11: regs.s11,
            t3: regs.t3,
            t4: regs.t4,
            t5: regs.t5,
            t6: regs.t6,
        }
    }
}
