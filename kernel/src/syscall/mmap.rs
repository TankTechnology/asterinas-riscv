// SPDX-License-Identifier: MPL-2.0

use align_ext::AlignExt;

use super::{SyscallReturn, mlock::locked_memory_limit};
use crate::{
    fs::file::file_table::{RawFileDesc, get_file_fast},
    prelude::*,
    vm::{
        page_cache::VmoOptions,
        perms::VmPerms,
        vmar::{PageFaultInfo, VMAR_CAP_ADDR, VMAR_LOWEST_ADDR, VmarMapOffset},
    },
};

pub fn sys_mmap(
    addr: u64,
    len: u64,
    perms: u64,
    flags: u64,
    fd: u64,
    offset: u64,
    ctx: &Context,
) -> Result<SyscallReturn> {
    let perms = VmPerms::from_user_bits_truncate(perms as u32);
    let res = do_sys_mmap(
        addr as usize,
        len as usize,
        perms,
        flags as u32,
        fd as _,
        offset as usize,
        ctx,
    )?;
    Ok(SyscallReturn::Return(res as _))
}

fn do_sys_mmap(
    addr: Vaddr,
    len: usize,
    vm_perms: VmPerms,
    raw_flags: u32,
    raw_fd: RawFileDesc,
    offset: usize,
    ctx: &Context,
) -> Result<Vaddr> {
    debug!(
        "addr = 0x{:x}, len = 0x{:x}, perms = {:?}, flags = 0x{:x}, raw_fd = {}, offset = 0x{:x}",
        addr, len, vm_perms, raw_flags, raw_fd, offset
    );

    // RISC-V Linux rejects a byte offset that is not page-aligned at the
    // syscall boundary, before `ksys_mmap_pgoff` resolves the descriptor. See
    // `sys_mmap` in Linux:
    // <https://github.com/torvalds/linux/blob/v6.19/arch/riscv/kernel/sys_riscv.c#L21-L29>.
    check_offset_alignment(offset)?;

    let flags = MMapFlags::from_bits_truncate(raw_flags & !MAP_TYPE_MASK);

    // Linux resolves a file-backed mapping's descriptor before validating the
    // flags, length, or address. Apart from matching its observable error
    // ordering, doing the lookup once also keeps the exact file stable if the
    // file table is shared and another thread closes or reuses the descriptor.
    // See `ksys_mmap_pgoff` in Linux:
    // <https://github.com/torvalds/linux/blob/v6.19/mm/mmap.c#L566-L608>.
    let mut file_table = (!flags.contains(MMapFlags::MAP_ANONYMOUS))
        .then(|| ctx.thread_local.borrow_file_table_mut());
    let file = if let Some(file_table) = file_table.as_mut() {
        Some(get_file_fast!(file_table, raw_fd.try_into()?))
    } else {
        None
    };

    let option = MMapOptions::try_from(raw_flags)?;
    let len = check_len(len)?;
    let addr = if option.flags().is_fixed() {
        check_addr(addr, len)?;
        addr
    } else {
        adjust_addr_hint(addr, len)
    };
    check_offset_overflow(offset, len, option.flags())?;

    let mut vm_may_perms = VmPerms::ALL_MAY_PERMS;

    let user_space = ctx.user_space();
    let vmar = user_space.vmar();
    let vm_map_options = {
        let mut options = vmar.new_map(len, vm_perms)?;

        if option.flags().is_fixed() {
            if option.flags().contains(MMapFlags::MAP_FIXED_NOREPLACE) {
                options = options.offset(VmarMapOffset::FixedNoReplace(addr));
            } else {
                options = options.offset(VmarMapOffset::FixedReplace(addr));
            }
        } else {
            #[cfg(target_arch = "x86_64")]
            if option.flags().contains(MMapFlags::MAP_32BIT) {
                let addr_hint = if addr != 0 { Some(addr) } else { None };
                options = options.offset(VmarMapOffset::Map32Bit(addr_hint));
            } else if addr != 0 {
                options = options.offset(VmarMapOffset::Hint(addr))
            }
            #[cfg(not(target_arch = "x86_64"))]
            if addr != 0 {
                options = options.offset(VmarMapOffset::Hint(addr))
            }
        }

        if option.typ().is_shared() {
            options = options.is_shared(true);
        }

        if option.flags().contains(MMapFlags::MAP_LOCKED) {
            options = options.lock(locked_memory_limit(ctx)?);
        }

        if option.flags().contains(MMapFlags::MAP_GROWSDOWN) {
            options = options.grows_down();
        }

        if option.flags().contains(MMapFlags::MAP_ANONYMOUS) {
            // Linux rejects MAP_SHARED_VALIDATE for anonymous mappings.
            if option.typ() == MMapType::SharedValidate {
                return_errno_with_message!(
                    Errno::EINVAL,
                    "MAP_SHARED_VALIDATE and MAP_ANONYMOUS cannot be used together"
                );
            }
            // Anonymous shared mappings should share the same memory pages.
            if option.typ().is_shared() {
                let shared_vmo = {
                    let vmo_options = VmoOptions::new(len);
                    vmo_options.alloc()?
                };
                options = options.vmo(shared_vmo);
            }
        } else {
            let file = file.as_deref().unwrap();
            let access_mode = file.access_mode();
            if vm_perms.effective_access_perms().contains(VmPerms::READ)
                && !access_mode.is_readable()
            {
                return_errno_with_message!(Errno::EACCES, "the file is not opened readable");
            }
            if option.typ() == MMapType::Shared && !access_mode.is_writable() {
                if vm_perms.contains(VmPerms::WRITE) {
                    return_errno_with_message!(Errno::EACCES, "the file is not opened writable");
                }
                vm_may_perms.remove(VmPerms::MAY_WRITE);
            }

            options = options
                .may_perms(vm_may_perms)
                .mappable(file.as_ref())?
                .vmo_offset(offset)
                .handle_page_faults_around();
        }

        options
    };

    let map_addr = vm_map_options.build()?;

    if (option.flags().contains(MMapFlags::MAP_POPULATE)
        || option.flags().contains(MMapFlags::MAP_LOCKED))
        && !option.flags().contains(MMapFlags::MAP_NONBLOCK)
        && let Some(required_perms) = population_perms(vm_perms)
    {
        populate_mapping(vmar, map_addr, len, required_perms);
    }

    Ok(map_addr)
}

fn populate_mapping(vmar: &crate::vm::vmar::Vmar, addr: Vaddr, len: usize, perms: VmPerms) {
    // Like Linux's `mm_populate`, best-effort prefaulting must not turn a valid
    // mapping into an mmap failure. A later access will report any backing-store
    // error through the normal page-fault path.
    for page_addr in (addr..addr + len).step_by(PAGE_SIZE) {
        if let Err(err) = vmar.handle_page_fault(&PageFaultInfo::new(page_addr, perms)) {
            debug!(
                "failed to populate mmap page at 0x{:x}: {:?}",
                page_addr, err
            );
            break;
        }
    }
}

fn population_perms(vm_perms: VmPerms) -> Option<VmPerms> {
    [VmPerms::READ, VmPerms::WRITE, VmPerms::EXEC]
        .into_iter()
        .find(|perms| vm_perms.contains(*perms))
}

fn check_len(len: usize) -> Result<usize> {
    if len == 0 {
        return_errno_with_message!(Errno::EINVAL, "the mapping length is zero");
    }

    if len > VMAR_CAP_ADDR {
        return_errno_with_message!(Errno::ENOMEM, "the mapping length is too large");
    }

    Ok(len.align_up(PAGE_SIZE))
}

fn check_addr(addr: Vaddr, len: usize) -> Result<()> {
    if addr > VMAR_CAP_ADDR - len {
        return_errno_with_message!(Errno::ENOMEM, "the mapping address is too high");
    }

    if !addr.is_multiple_of(PAGE_SIZE) {
        return_errno_with_message!(Errno::EINVAL, "the mapping address is not aligned");
    }

    if addr < VMAR_LOWEST_ADDR {
        return_errno_with_message!(Errno::EPERM, "the mapping address is too low");
    }

    Ok(())
}

fn adjust_addr_hint(mut addr: Vaddr, len: usize) -> Vaddr {
    addr = addr.align_down(PAGE_SIZE);
    if addr == 0 {
        // No hint.
        return 0;
    }

    if addr < VMAR_LOWEST_ADDR {
        // This is Linux behavior.
        // Reference: <https://elixir.bootlin.com/linux/v6.19.3/source/mm/mmap.c#L219>.
        addr = VMAR_LOWEST_ADDR;
    }
    if addr > VMAR_CAP_ADDR - len {
        // Illegal hint. Treat it as if there were no hint.
        addr = 0;
    }

    addr
}

fn check_offset_alignment(offset: usize) -> Result<()> {
    if !offset.is_multiple_of(PAGE_SIZE) {
        return_errno_with_message!(Errno::EINVAL, "the mapping offset is not aligned");
    }

    Ok(())
}

fn check_offset_overflow(offset: usize, len: usize, flags: MMapFlags) -> Result<()> {
    if flags.contains(MMapFlags::MAP_ANONYMOUS) {
        return Ok(());
    }

    if offset
        .checked_add(len)
        .is_none_or(|end| end >= isize::MAX as usize)
    {
        return_errno_with_message!(Errno::EOVERFLOW, "the mapping offset overflows");
    }

    Ok(())
}

// Definition of mmap flags, conforming to the Linux mmap interface:
// <https://man7.org/linux/man-pages/man2/mmap.2.html>.
//
// The first 4 bits of the flag value represents the type of the mapping,
// while other bits are used as the flags of the mapping.

/// The mask for the mapping type.
const MAP_TYPE_MASK: u32 = 0xf;

#[repr(u8)]
#[derive(Clone, Copy, Debug, PartialEq, TryFromInt)]
enum MMapType {
    Shared = 0x1,
    Private = 0x2,
    SharedValidate = 0x3,
}

impl MMapType {
    pub(self) fn is_shared(self) -> bool {
        matches!(self, Self::Shared | Self::SharedValidate)
    }
}

bitflags! {
    // If you update the flags here, please also check and update `LEGACY_MMAP_FLAGS` below.
    struct MMapFlags : u32 {
        const MAP_FIXED           = 0x10;
        const MAP_ANONYMOUS       = 0x20;
        #[cfg(target_arch = "x86_64")]
        const MAP_32BIT           = 0x40;
        const MAP_GROWSDOWN       = 0x100;
        const MAP_DENYWRITE       = 0x800;
        const MAP_EXECUTABLE      = 0x1000;
        const MAP_LOCKED          = 0x2000;
        const MAP_NORESERVE       = 0x4000;
        const MAP_POPULATE        = 0x8000;
        const MAP_NONBLOCK        = 0x10000;
        const MAP_STACK           = 0x20000;
        const MAP_HUGETLB         = 0x40000;
        const MAP_FIXED_NOREPLACE = 0x100000;
    }
}

#[cfg(target_arch = "x86_64")]
const ARCH_LEGACY_MMAP_FLAGS: MMapFlags = MMapFlags::MAP_32BIT;

#[cfg(not(target_arch = "x86_64"))]
const ARCH_LEGACY_MMAP_FLAGS: MMapFlags = MMapFlags::empty();

// Reference: <https://elixir.bootlin.com/linux/v6.18.1/source/include/linux/mman.h#L35-L59>
const LEGACY_MMAP_FLAGS: MMapFlags = MMapFlags::MAP_FIXED
    .union(MMapFlags::MAP_ANONYMOUS)
    .union(MMapFlags::MAP_GROWSDOWN)
    .union(MMapFlags::MAP_DENYWRITE)
    .union(MMapFlags::MAP_EXECUTABLE)
    .union(MMapFlags::MAP_LOCKED)
    .union(MMapFlags::MAP_NORESERVE)
    .union(MMapFlags::MAP_POPULATE)
    .union(MMapFlags::MAP_NONBLOCK)
    .union(MMapFlags::MAP_STACK)
    .union(MMapFlags::MAP_HUGETLB)
    .union(ARCH_LEGACY_MMAP_FLAGS);

impl MMapFlags {
    pub(self) fn is_fixed(self) -> bool {
        self.contains(Self::MAP_FIXED) || self.contains(Self::MAP_FIXED_NOREPLACE)
    }
}

#[derive(Debug)]
struct MMapOptions {
    typ: MMapType,
    flags: MMapFlags,
}

impl TryFrom<u32> for MMapOptions {
    type Error = Error;

    fn try_from(value: u32) -> Result<Self> {
        let typ_raw = (value & MAP_TYPE_MASK) as u8;
        let typ = MMapType::try_from(typ_raw)?;

        // According to the Linux behavior, unknown flags are silently ignored unless
        // `MAP_SHARED_VALIDATE` is specified.
        let flags_raw = value & !MAP_TYPE_MASK;
        if typ == MMapType::SharedValidate && (flags_raw & !LEGACY_MMAP_FLAGS.bits()) != 0 {
            return_errno_with_message!(Errno::EOPNOTSUPP, "the mapping flags are not supported");
        }
        let flags = MMapFlags::from_bits_truncate(flags_raw);

        Ok(MMapOptions { typ, flags })
    }
}

impl MMapOptions {
    pub(self) fn typ(&self) -> MMapType {
        self.typ
    }

    pub(self) fn flags(&self) -> MMapFlags {
        self.flags
    }
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::population_perms;
    use crate::vm::perms::VmPerms;

    #[ktest]
    fn population_uses_an_available_mapping_permission() {
        assert_eq!(population_perms(VmPerms::empty()), None);
        assert_eq!(population_perms(VmPerms::EXEC), Some(VmPerms::EXEC));
        assert_eq!(population_perms(VmPerms::WRITE), Some(VmPerms::WRITE));
        assert_eq!(
            population_perms(VmPerms::READ | VmPerms::WRITE),
            Some(VmPerms::READ)
        );
    }
}
