// SPDX-License-Identifier: MPL-2.0

//! `bpf(2)`: the eBPF control plane.
//!
//! The command set is the minimum a loader needs:
//!
//! - `BPF_MAP_CREATE`, `BPF_MAP_LOOKUP_ELEM`, `BPF_MAP_UPDATE_ELEM`,
//!   `BPF_MAP_DELETE_ELEM`, `BPF_MAP_GET_NEXT_KEY` on array and hash maps;
//! - `BPF_PROG_LOAD`, which runs the verifier and hands back a program fd;
//! - `BPF_RAW_TRACEPOINT_OPEN`, whose "name" is `subsystem:event` of one of
//!   this kernel's trace events. It attaches the program and returns a link
//!   fd; closing the fd detaches it.
//!
//! Loading is root-only, and the verifier ([`crate::bpf::verifier`]) is
//! conservative, so a program that loads cannot do anything outside maps and
//! the helpers it was allowed to call.

use super::SyscallReturn;
use crate::{
    bpf::{
        CTX_SIZE, Map, Program, SOCKET_CTX_MAX,
        attach::{self, LinkFile},
        insn::{Insn, MAX_INSNS},
        map::{BPF_MAP_TYPE_ARRAY, MAX_ENTRIES, MAX_KEY_SIZE, MAX_VALUE_SIZE, MapFile},
        prog::{BPF_PROG_TYPE_SOCKET_FILTER, BPF_PROG_TYPE_TRACE_EVENT, ProgFile},
        verifier,
    },
    fs::file::{
        FileLike,
        file_table::{FdFlags, RawFileDesc, get_file_fast},
    },
    prelude::*,
    trace::event,
};

const BPF_MAP_CREATE: u64 = 0;
const BPF_MAP_LOOKUP_ELEM: u64 = 1;
const BPF_MAP_UPDATE_ELEM: u64 = 2;
const BPF_MAP_DELETE_ELEM: u64 = 3;
const BPF_MAP_GET_NEXT_KEY: u64 = 4;
const BPF_PROG_LOAD: u64 = 5;
const BPF_RAW_TRACEPOINT_OPEN: u64 = 17;

/// The prefix of `union bpf_attr` that `BPF_MAP_CREATE` reads.
const MAP_CREATE_ATTR_LEN: usize = 48;
/// The prefix of `union bpf_attr` that the element commands read.
const MAP_ELEM_ATTR_LEN: usize = 32;
/// The prefix of `union bpf_attr` that `BPF_PROG_LOAD` reads.
const PROG_LOAD_ATTR_LEN: usize = 72;
/// The prefix of `union bpf_attr` that `BPF_RAW_TRACEPOINT_OPEN` reads.
const RAW_TRACEPOINT_ATTR_LEN: usize = 16;

/// Program name length, as in Linux's `BPF_OBJ_NAME_LEN`.
const NAME_LEN: usize = 16;

pub fn sys_bpf(cmd: u64, attr: u64, size: u64, ctx: &Context) -> Result<SyscallReturn> {
    if !ctx.posix_thread.credentials().euid().is_root() {
        return_errno_with_message!(Errno::EPERM, "loading bpf objects requires root");
    }

    let result = match cmd {
        BPF_MAP_CREATE => map_create(attr, size, ctx)?,
        BPF_MAP_LOOKUP_ELEM => map_lookup(attr, size, ctx)?,
        BPF_MAP_UPDATE_ELEM => map_update(attr, size, ctx)?,
        BPF_MAP_DELETE_ELEM => map_delete(attr, size, ctx)?,
        BPF_MAP_GET_NEXT_KEY => map_next_key(attr, size, ctx)?,
        BPF_PROG_LOAD => prog_load(attr, size, ctx)?,
        BPF_RAW_TRACEPOINT_OPEN => raw_tracepoint_open(attr, size, ctx)?,
        _ => return_errno_with_message!(Errno::EINVAL, "unknown bpf command"),
    };

    Ok(SyscallReturn::Return(result as isize))
}

/// Copies `len` bytes of `union bpf_attr` from the caller.
fn read_attr(ctx: &Context, attr: u64, size: u64, len: usize) -> Result<Vec<u8>> {
    if size < len as u64 {
        return_errno_with_message!(Errno::EINVAL, "the bpf attribute is too small");
    }
    read_user_bytes(ctx, attr, len)
}

fn u32_at(bytes: &[u8], offset: usize) -> u32 {
    u32::from_le_bytes([
        bytes[offset],
        bytes[offset + 1],
        bytes[offset + 2],
        bytes[offset + 3],
    ])
}

fn u64_at(bytes: &[u8], offset: usize) -> u64 {
    let mut value = [0u8; 8];
    value.copy_from_slice(&bytes[offset..offset + 8]);
    u64::from_le_bytes(value)
}

/// Reads a fixed name field, stopping at the first NUL.
fn name_at(bytes: &[u8], offset: usize) -> String {
    let field = &bytes[offset..offset + NAME_LEN];
    let end = field
        .iter()
        .position(|byte| *byte == 0)
        .unwrap_or(field.len());
    String::from_utf8_lossy(&field[..end]).into_owned()
}

fn read_user_bytes(ctx: &Context, addr: u64, len: usize) -> Result<Vec<u8>> {
    let mut buffer = vec![0u8; len];
    if len == 0 {
        return Ok(buffer);
    }
    let user_space = ctx.user_space();
    let mut reader = user_space.reader(addr as Vaddr, len)?;
    let mut writer = VmWriter::from(buffer.as_mut_slice());
    reader.read_fallible(&mut writer)?;
    Ok(buffer)
}

fn write_user_bytes(ctx: &Context, addr: u64, bytes: &[u8]) -> Result<()> {
    if bytes.is_empty() {
        return Ok(());
    }
    let user_space = ctx.user_space();
    let mut writer = user_space.writer(addr as Vaddr, bytes.len())?;
    let mut reader = VmReader::from(bytes);
    writer.write_fallible(&mut reader)?;
    Ok(())
}

/// Reads a NUL-terminated string of at most `max` bytes from the caller.
fn read_user_string(ctx: &Context, addr: u64, max: usize) -> Result<String> {
    let bytes = read_user_bytes(ctx, addr, max)?;
    let end = bytes
        .iter()
        .position(|byte| *byte == 0)
        .unwrap_or(bytes.len());
    Ok(String::from_utf8_lossy(&bytes[..end]).into_owned())
}

fn insert_file(ctx: &Context, file: Arc<dyn FileLike>, flags: FdFlags) -> Result<i32> {
    let file_table = ctx.thread_local.borrow_file_table();
    let mut file_table_locked = file_table.unwrap().write();
    let fd = file_table_locked.insert(file, flags)?;
    Ok(u32::from(fd) as i32)
}

/// Looks up a file descriptor and returns its map.
fn map_of_fd(ctx: &Context, fd: RawFileDesc) -> Result<Arc<Map>> {
    let file = {
        let mut file_table = ctx.thread_local.borrow_file_table_mut();
        get_file_fast!(&mut file_table, fd.try_into()?).into_owned()
    };
    let map_file = file
        .downcast_ref::<MapFile>()
        .ok_or_else(|| Error::with_message(Errno::EBADF, "the descriptor is not a bpf map"))?;
    Ok(map_file.map().clone())
}

fn prog_of_fd(ctx: &Context, fd: RawFileDesc) -> Result<Arc<Program>> {
    let file = {
        let mut file_table = ctx.thread_local.borrow_file_table_mut();
        get_file_fast!(&mut file_table, fd.try_into()?).into_owned()
    };
    let prog_file = file
        .downcast_ref::<ProgFile>()
        .ok_or_else(|| Error::with_message(Errno::EBADF, "the descriptor is not a bpf program"))?;
    Ok(prog_file.program().clone())
}

fn map_create(attr: u64, size: u64, ctx: &Context) -> Result<i32> {
    let bytes = read_attr(ctx, attr, size, MAP_CREATE_ATTR_LEN)?;
    let map_type = u32_at(&bytes, 0);
    let key_size = u32_at(&bytes, 4);
    let value_size = u32_at(&bytes, 8);
    let max_entries = u32_at(&bytes, 12);
    let flags = u32_at(&bytes, 16);
    let name = name_at(&bytes, 28);

    if flags != 0 {
        return_errno_with_message!(Errno::EINVAL, "map flags are not supported");
    }
    if key_size > MAX_KEY_SIZE {
        return_errno_with_message!(Errno::EINVAL, "the key size is too large");
    }
    if value_size > MAX_VALUE_SIZE {
        return_errno_with_message!(Errno::EINVAL, "the value size is too large");
    }
    if max_entries > MAX_ENTRIES {
        return_errno_with_message!(Errno::EINVAL, "too many entries");
    }
    let known = matches!(
        map_type,
        crate::bpf::map::BPF_MAP_TYPE_ARRAY
            | crate::bpf::map::BPF_MAP_TYPE_HASH
            | crate::bpf::map::BPF_MAP_TYPE_PERCPU_ARRAY
            | crate::bpf::map::BPF_MAP_TYPE_PERCPU_HASH
            | crate::bpf::map::BPF_MAP_TYPE_RINGBUF
    );
    if !known {
        return_errno_with_message!(Errno::EINVAL, "unsupported map type");
    }

    let map = Map::create(map_type, key_size, value_size, max_entries, &name)?;
    let file: Arc<dyn FileLike> = Arc::new(MapFile::new(map));
    insert_file(ctx, file, FdFlags::empty())
}

fn map_lookup(attr: u64, size: u64, ctx: &Context) -> Result<i32> {
    let bytes = read_attr(ctx, attr, size, MAP_ELEM_ATTR_LEN)?;
    let map = map_of_fd(ctx, u32_at(&bytes, 0) as RawFileDesc)?;
    let key_addr = u64_at(&bytes, 8);
    let value_addr = u64_at(&bytes, 16);
    if value_addr == 0 {
        return_errno_with_message!(Errno::EINVAL, "the value buffer is null");
    }

    let key = read_user_bytes(ctx, key_addr, map.key_size() as usize)?;
    let mut value = vec![0u8; map.element_size()];
    map.lookup(&key, &mut value)?;
    write_user_bytes(ctx, value_addr, &value)?;
    Ok(0)
}

fn map_update(attr: u64, size: u64, ctx: &Context) -> Result<i32> {
    let bytes = read_attr(ctx, attr, size, MAP_ELEM_ATTR_LEN)?;
    let map = map_of_fd(ctx, u32_at(&bytes, 0) as RawFileDesc)?;
    let key_addr = u64_at(&bytes, 8);
    let value_addr = u64_at(&bytes, 16);
    let flags = u64_at(&bytes, 24);

    let key = read_user_bytes(ctx, key_addr, map.key_size() as usize)?;
    let value = read_user_bytes(ctx, value_addr, map.element_size())?;
    map.update(&key, &value, flags)?;
    Ok(0)
}

fn map_delete(attr: u64, size: u64, ctx: &Context) -> Result<i32> {
    let bytes = read_attr(ctx, attr, size, MAP_ELEM_ATTR_LEN)?;
    let map = map_of_fd(ctx, u32_at(&bytes, 0) as RawFileDesc)?;
    let key_addr = u64_at(&bytes, 8);

    let key = read_user_bytes(ctx, key_addr, map.key_size() as usize)?;
    map.delete(&key)?;
    Ok(0)
}

fn map_next_key(attr: u64, size: u64, ctx: &Context) -> Result<i32> {
    let bytes = read_attr(ctx, attr, size, MAP_ELEM_ATTR_LEN)?;
    let map = map_of_fd(ctx, u32_at(&bytes, 0) as RawFileDesc)?;
    let key_addr = u64_at(&bytes, 8);
    let next_addr = u64_at(&bytes, 16);
    if next_addr == 0 {
        return_errno_with_message!(Errno::EINVAL, "the output buffer is null");
    }

    let key = if key_addr == 0 {
        None
    } else {
        Some(read_user_bytes(ctx, key_addr, map.key_size() as usize)?)
    };
    let next = map.next_key(key.as_deref())?;
    let Some(next) = next else {
        return_errno_with_message!(Errno::ENOENT, "there is no next key");
    };
    write_user_bytes(ctx, next_addr, &next)?;
    Ok(0)
}

fn prog_load(attr: u64, size: u64, ctx: &Context) -> Result<i32> {
    let bytes = read_attr(ctx, attr, size, PROG_LOAD_ATTR_LEN)?;
    let prog_type = u32_at(&bytes, 0);
    let insn_count = u32_at(&bytes, 4) as usize;
    let insns_addr = u64_at(&bytes, 8);
    let license_addr = u64_at(&bytes, 16);
    let name = name_at(&bytes, 48);

    if prog_type != BPF_PROG_TYPE_TRACE_EVENT && prog_type != BPF_PROG_TYPE_SOCKET_FILTER {
        return_errno_with_message!(Errno::EINVAL, "unsupported program type");
    }
    if insn_count == 0 || insn_count > MAX_INSNS {
        return_errno_with_message!(Errno::EINVAL, "unsupported instruction count");
    }
    if license_addr == 0 {
        return_errno_with_message!(Errno::EINVAL, "the license is missing");
    }
    let license = read_user_string(ctx, license_addr, 32)?;

    let code = read_user_bytes(ctx, insns_addr, insn_count * Insn::SIZE)?;
    let mut insns = Vec::with_capacity(insn_count);
    for chunk in code.chunks_exact(Insn::SIZE) {
        let mut raw = [0u8; Insn::SIZE];
        raw.copy_from_slice(chunk);
        insns.push(Insn::from_bytes(&raw));
    }

    let mut maps = Vec::new();
    let mut resolve = |fd: u32| map_of_fd(ctx, fd as RawFileDesc);
    let ctx_size = if prog_type == BPF_PROG_TYPE_SOCKET_FILTER {
        SOCKET_CTX_MAX
    } else {
        CTX_SIZE
    };
    verifier::verify(&mut insns, &mut maps, ctx_size, &mut resolve)?;

    let program = Program::new(insns, maps, prog_type, &name, &license);
    let file: Arc<dyn FileLike> = Arc::new(ProgFile::new(program));
    insert_file(ctx, file, FdFlags::empty())
}

fn raw_tracepoint_open(attr: u64, size: u64, ctx: &Context) -> Result<i32> {
    let bytes = read_attr(ctx, attr, size, RAW_TRACEPOINT_ATTR_LEN)?;
    let name_addr = u64_at(&bytes, 0);
    let prog_fd = u32_at(&bytes, 8);

    let name = read_user_string(ctx, name_addr, 64)?;
    let (subsystem, event_name) = name
        .split_once(':')
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "the name is not subsystem:event"))?;
    let event = event::event_named(subsystem, event_name)
        .ok_or_else(|| Error::with_message(Errno::ENOENT, "no such trace event"))?;

    let program = prog_of_fd(ctx, prog_fd as RawFileDesc)?;
    if program.prog_type() != BPF_PROG_TYPE_TRACE_EVENT {
        return_errno_with_message!(
            Errno::EINVAL,
            "only trace-event programs can be attached to trace events"
        );
    }
    attach::attach(event.id(), program)?;

    let file: Arc<dyn FileLike> = Arc::new(LinkFile::new(event.id()));
    insert_file(ctx, file, FdFlags::empty())
}
