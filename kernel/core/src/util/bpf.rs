// SPDX-License-Identifier: MPL-2.0

//! Classic socket-filter BPF used by Debian's udev monitor.

use ostd::mm::VmIo;

use crate::prelude::*;

#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub(crate) struct SockFilter {
    pub(crate) code: u16,
    pub(crate) jt: u8,
    pub(crate) jf: u8,
    pub(crate) k: u32,
}

const BPF_MAX_INSNS: usize = 4096;

pub(crate) fn read_prog_from_user(addr: Vaddr) -> Result<Vec<SockFilter>> {
    let task = ostd::task::Task::current().unwrap();
    let thread_local = AsThreadLocal::as_thread_local(&task).unwrap();
    let user_space = CurrentUserSpace::new(thread_local);
    let len = user_space.read_val::<u16>(addr)? as usize;
    let filter_addr = user_space.read_val::<Vaddr>(addr.checked_add(8).ok_or(Errno::EFAULT)?)?;
    if len == 0 || len > BPF_MAX_INSNS {
        return_errno_with_message!(Errno::EINVAL, "invalid socket filter length");
    }
    let mut program = Vec::with_capacity(len);
    for index in 0..len {
        program.push(
            user_space.read_val::<SockFilter>(
                filter_addr
                    .checked_add(index * size_of::<SockFilter>())
                    .ok_or(Errno::EFAULT)?,
            )?,
        );
    }
    validate(&program)?;
    Ok(program)
}

/// Validates the bounded, forward-only packet-filter subset used by libudev.
///
/// Other cBPF instructions are rejected instead of being accepted as no-ops.
fn validate(program: &[SockFilter]) -> Result<()> {
    if program.is_empty() || program.len() > BPF_MAX_INSNS {
        return_errno_with_message!(Errno::EINVAL, "invalid socket filter length");
    }
    for (pc, instruction) in program.iter().enumerate() {
        let offsets: &[usize] = match instruction.code {
            0x00 | 0x20 | 0x28 | 0x30 | 0x54 | 0x06 | 0x16 => &[],
            0x05 => &[instruction.k as usize],
            0x15 | 0x25 | 0x35 | 0x45 => &[instruction.jt as usize, instruction.jf as usize],
            _ => return_errno_with_message!(Errno::EINVAL, "unsupported socket filter instruction"),
        };
        for offset in offsets {
            if pc
                .checked_add(1)
                .and_then(|next| next.checked_add(*offset))
                .is_none_or(|target| target >= program.len())
            {
                return_errno_with_message!(Errno::EINVAL, "socket filter jump is out of bounds");
            }
        }
    }
    if !matches!(program.last().unwrap().code, 0x06 | 0x16) {
        return_errno_with_message!(Errno::EINVAL, "socket filter must end with a return");
    }
    Ok(())
}

/// Returns the retained packet length, or zero when the packet is rejected.
pub(crate) fn run(program: &[SockFilter], data: &[u8]) -> u32 {
    let mut accumulator = 0u32;
    let mut pc = 0usize;
    while pc < program.len() {
        let instruction = program[pc];
        match instruction.code {
            0x00 => accumulator = instruction.k,
            0x20 | 0x28 | 0x30 => {
                let offset = instruction.k as usize;
                let size = match instruction.code {
                    0x20 => 4,
                    0x28 => 2,
                    _ => 1,
                };
                let Some(end) = offset.checked_add(size) else {
                    return 0;
                };
                let Some(bytes) = data.get(offset..end) else {
                    return 0;
                };
                accumulator = match size {
                    4 => u32::from_be_bytes(bytes.try_into().unwrap()),
                    2 => u16::from_be_bytes(bytes.try_into().unwrap()) as u32,
                    _ => bytes[0] as u32,
                };
            }
            0x54 => accumulator &= instruction.k,
            0x05 => pc = pc.saturating_add(instruction.k as usize),
            0x15 | 0x25 | 0x35 | 0x45 => {
                let taken = match instruction.code {
                    0x15 => accumulator == instruction.k,
                    0x25 => accumulator > instruction.k,
                    0x35 => accumulator >= instruction.k,
                    _ => accumulator & instruction.k != 0,
                };
                pc = pc.saturating_add(if taken {
                    instruction.jt
                } else {
                    instruction.jf
                } as usize);
            }
            0x06 => return instruction.k,
            0x16 => return accumulator,
            _ => return 0,
        }
        pc = pc.saturating_add(1);
    }
    0
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::*;

    use super::*;

    #[ktest]
    fn rejects_invalid_programs() {
        assert!(validate(&[]).is_err());
        assert!(
            validate(&[SockFilter {
                code: 0x06,
                jt: 0,
                jf: 0,
                k: 0
            }])
            .is_ok()
        );
        assert!(
            validate(&[
                SockFilter {
                    code: 0x05,
                    jt: 0,
                    jf: 0,
                    k: 1
                },
                SockFilter {
                    code: 0x06,
                    jt: 0,
                    jf: 0,
                    k: 0
                },
            ])
            .is_err()
        );
        assert!(
            validate(&[SockFilter {
                code: 0xff,
                jt: 0,
                jf: 0,
                k: 0
            }])
            .is_err()
        );
        assert!(
            validate(&[SockFilter {
                code: 0x20,
                jt: 0,
                jf: 0,
                k: 0
            }])
            .is_err()
        );
    }

    #[ktest]
    fn matches_network_order_packet_and_rejects_short_loads() {
        let program = [
            SockFilter {
                code: 0x20,
                jt: 0,
                jf: 0,
                k: 0,
            },
            SockFilter {
                code: 0x15,
                jt: 0,
                jf: 1,
                k: 0xfeedcafe,
            },
            SockFilter {
                code: 0x06,
                jt: 0,
                jf: 0,
                k: 4,
            },
            SockFilter {
                code: 0x06,
                jt: 0,
                jf: 0,
                k: 0,
            },
        ];
        validate(&program).unwrap();
        assert_eq!(run(&program, &[0xfe, 0xed, 0xca, 0xfe]), 4);
        assert_eq!(run(&program, &[0, 0, 0, 0]), 0);
        assert_eq!(run(&program, &[0xfe, 0xed]), 0);
    }
}
