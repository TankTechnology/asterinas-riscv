// SPDX-License-Identifier: MPL-2.0

//! Minimal PVR Services bridge surface.
//!
//! The vendor user-mode DDK reaches every operation through
//! `DRM_IOCTL_PVR_SRVKM_CMD`, whose payload names a bridge group and function
//! plus user pointers to the packed input and output structures. This module
//! implements the transport (size-checked copies, a bounded command table) and
//! the version-negotiation commands. Render, memory and sync commands are
//! deliberately absent until the firmware-facing side exists; they are
//! counted and refused with `EOPNOTSUPP` rather than silently accepted.
//!
//! The layouts below are transcribed from the vendor UAPI
//! (`include/drm/pvr_drm.h`) and bridge headers. All bridge structures are
//! `__packed`, so the sizes are exact: a client that sends a different size is
//! a different ABI and is refused.

use alloc::vec;
use core::sync::atomic::{AtomicU64, Ordering};

use ostd::mm::VmIo;

use crate::{context::current_userspace, prelude::*};

/// Bridge group for connection and device queries.
pub(super) const BRIDGE_SRVCORE: u32 = 1;

/// `PVRSRV_BRIDGE_SRVCORE_CONNECT`.
pub(super) const SRVCORE_CONNECT: u32 = 0;
/// `PVRSRV_BRIDGE_SRVCORE_DISCONNECT`.
pub(super) const SRVCORE_DISCONNECT: u32 = 1;
/// `PVRSRV_BRIDGE_SRVCORE_ACQUIREGLOBALEVENTOBJECT`.
pub(super) const SRVCORE_ACQUIREGLOBALEVENTOBJECT: u32 = 2;
/// `PVRSRV_BRIDGE_SRVCORE_RELEASEGLOBALEVENTOBJECT`.
pub(super) const SRVCORE_RELEASEGLOBALEVENTOBJECT: u32 = 3;
/// `PVRSRV_BRIDGE_SRVCORE_GETDEVCLOCKSPEED`.
pub(super) const SRVCORE_GETDEVCLOCKSPEED: u32 = 8;
/// `PVRSRV_BRIDGE_SRVCORE_GETDEVICESTATUS`.
pub(super) const SRVCORE_GETDEVICESTATUS: u32 = 11;
/// `PVRSRV_BRIDGE_SRVCORE_GETMULTICOREINFO`.
pub(super) const SRVCORE_GETMULTICOREINFO: u32 = 12;
/// `PVRSRV_BRIDGE_SRVCORE_CMD_LAST` (the vendor group boundary).
const SRVCORE_CMD_LAST: u32 = 16;
/// `BRIDGE_DISPATCH_TABLE_START_ENTRY_COUNT`; the vendor's last group is
/// `PVRSRV_BRIDGE_RGXRAY` (139).
const BRIDGE_DISPATCH_TABLE_START_ENTRY_COUNT: u32 = 140;

/// `struct drm_pvr_srvkm_cmd` (32 bytes).
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmPvrSrvkmCmd {
    pub(super) bridge_id: u32,
    pub(super) bridge_func_id: u32,
    pub(super) in_data_ptr: u64,
    pub(super) out_data_ptr: u64,
    pub(super) in_data_size: u32,
    pub(super) out_data_size: u32,
}

/// `struct pvr_sync_rename_ioctl_data` (32 bytes).
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct PvrSyncRenameData {
    pub(super) name: [u8; 32],
}

/// `struct pvr_sw_sync_create_fence_data` (48 bytes).
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct PvrSwSyncCreateFenceData {
    pub(super) name: [u8; 32],
    pub(super) fence: i32,
    pub(super) pad: u32,
    pub(super) sync_pt_idx: u64,
}

/// `struct pvr_sw_timeline_advance_data` (8 bytes).
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct PvrSwTimelineAdvanceData {
    pub(super) sync_pt_idx: u64,
}

/// `struct drm_pvr_srvkm_init_data` (4 bytes).
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct PvrSrvkmInitData {
    pub(super) init_module: u32,
}

/// The packed BVNC of the Megrez AXM-8-256: 30.3.408.101.
pub(super) const PACKED_BVNC: u64 = 0x001e_0003_0198_0065;
/// `PVRSRV_ERROR` value for success.
const PVRSRV_OK: u32 = 0;
/// `PVRSRV_ERROR_NOT_SUPPORTED` (`include/pvrsrv_errors.h`, entry 20).
/// The vendor KM returns it for `GETMULTICOREINFO` when the device has no
/// multicore information (`RGXGetMultiCoreInfo`).
const PVRSRV_ERROR_NOT_SUPPORTED: u32 = 20;
/// `PVRSRV_DEVICE_STATUS_OK`.
const PVRSRV_DEVICE_STATUS_OK: u32 = 1;
/// A fake global-event-object handle. The UM only passes it back to the
/// release command and this kernel never dereferences it.
const GLOBAL_EVENT_OBJECT_HANDLE: u64 = 0x1000;
/// P2's measured GPU core clock, reported by the clock query while there is
/// no live firmware service to ask.
const GPU_CORE_HZ: u32 = 800_000_000;

static INIT_CALLS: AtomicU64 = AtomicU64::new(0);
static BRIDGE_CALLS: AtomicU64 = AtomicU64::new(0);
static UNSUPPORTED_CALLS: AtomicU64 = AtomicU64::new(0);

#[cfg(not(ktest))]
fn emit_marker() {
    aster_logger::println!(
        "ASTERINAS_POWERVR_BRIDGE status=ok init={} cmds={} unsupported={}",
        INIT_CALLS.load(Ordering::Relaxed),
        BRIDGE_CALLS.load(Ordering::Relaxed),
        UNSUPPORTED_CALLS.load(Ordering::Relaxed),
    );
}

/// A kernel unit test has no console device to print to.
#[cfg(ktest)]
fn emit_marker() {}

/// Handles `DRM_IOCTL_PVR_SRVKM_INIT`.
pub(super) fn init(init_module: u32) -> Result<()> {
    // 1 = services, 2 = sync, 3 = export-fence sync (the vendor UAPI values).
    if !(1..=3).contains(&init_module) {
        return_errno_with_message!(Errno::EINVAL, "unknown PVR init module");
    }
    INIT_CALLS.fetch_add(1, Ordering::Relaxed);
    emit_marker();
    Ok(())
}

/// A sync ioctl that is encoded but has no implementation yet.
pub(super) fn unsupported_sync() -> Result<()> {
    UNSUPPORTED_CALLS.fetch_add(1, Ordering::Relaxed);
    emit_marker();
    return_errno_with_message!(Errno::EOPNOTSUPP, "PVR sync ioctl is not implemented")
}

/// The exact `(in_size, out_size)` pair for every command this skeleton
/// serves.
///
/// Commands whose input structure is only a placeholder are registered with
/// an in-size of 0 in the vendor dispatch table
/// (`server_srvcore_bridge.c`: `NULL, 0`), and the vendor UM sends no input
/// buffer for them. The bridge must accept that, not the C `sizeof`.
fn expected_sizes(bridge_id: u32, func_id: u32) -> Option<(usize, usize)> {
    match (bridge_id, func_id) {
        (BRIDGE_SRVCORE, SRVCORE_CONNECT) => Some((16, 17)),
        (BRIDGE_SRVCORE, SRVCORE_DISCONNECT) => Some((0, 4)),
        (BRIDGE_SRVCORE, SRVCORE_ACQUIREGLOBALEVENTOBJECT) => Some((0, 12)),
        (BRIDGE_SRVCORE, SRVCORE_RELEASEGLOBALEVENTOBJECT) => Some((8, 4)),
        (BRIDGE_SRVCORE, SRVCORE_GETDEVCLOCKSPEED) => Some((0, 8)),
        (BRIDGE_SRVCORE, SRVCORE_GETDEVICESTATUS) => Some((0, 8)),
        (BRIDGE_SRVCORE, SRVCORE_GETMULTICOREINFO) => Some((12, 16)),
        _ => None,
    }
}

/// Mirrors `BridgedDispatchKM` (`services/server/common/srvcore.c`): an
/// out-of-range group or a function past the group boundary is
/// `PVRSRV_ERROR_BRIDGE_EINVAL`; a group with no dispatch entry in this
/// build is DummyBW's `PVRSRV_ERROR_BRIDGE_ENOTTY`. The vendor maps both
/// with `PVRSRVToNativeError` to `-EINVAL` and `-ENOTTY`.
fn unsupported_bridge_errno(bridge_id: u32, func_id: u32) -> Errno {
    if bridge_id >= BRIDGE_DISPATCH_TABLE_START_ENTRY_COUNT {
        return Errno::EINVAL;
    }
    if bridge_id == BRIDGE_SRVCORE && func_id > SRVCORE_CMD_LAST {
        return Errno::EINVAL;
    }
    Errno::ENOTTY
}

/// Dispatches one `SRVKM_CMD` request.
pub(super) fn srvkm(req: &DrmPvrSrvkmCmd) -> Result<()> {
    let Some((in_size, out_size)) = expected_sizes(req.bridge_id, req.bridge_func_id) else {
        UNSUPPORTED_CALLS.fetch_add(1, Ordering::Relaxed);
        emit_marker();
        let errno = unsupported_bridge_errno(req.bridge_id, req.bridge_func_id);
        return_errno_with_message!(errno, "unsupported PVR bridge command");
    };

    if req.in_data_size as usize != in_size || req.out_data_size as usize != out_size {
        return_errno_with_message!(Errno::EINVAL, "PVR bridge size mismatch");
    }

    let mut input = vec![0u8; in_size];
    if in_size > 0 {
        current_userspace!().read_bytes(req.in_data_ptr as usize, &mut input)?;
    }
    let output = run_command(req.bridge_id, req.bridge_func_id, &input)?;
    if out_size > 0 {
        current_userspace!().write_bytes(req.out_data_ptr as usize, &output)?;
    }

    BRIDGE_CALLS.fetch_add(1, Ordering::Relaxed);
    emit_marker();
    Ok(())
}

/// Executes a supported bridge command, returning its packed output.
fn run_command(bridge_id: u32, func_id: u32, input: &[u8]) -> Result<Vec<u8>> {
    match (bridge_id, func_id) {
        (BRIDGE_SRVCORE, SRVCORE_CONNECT) => Ok(connect_response()),
        (BRIDGE_SRVCORE, SRVCORE_DISCONNECT) => {
            let mut out = vec![0u8; 4];
            out[0..4].copy_from_slice(&PVRSRV_OK.to_le_bytes());
            Ok(out)
        }
        (BRIDGE_SRVCORE, SRVCORE_ACQUIREGLOBALEVENTOBJECT) => {
            let mut out = vec![0u8; 12];
            out[0..8].copy_from_slice(&GLOBAL_EVENT_OBJECT_HANDLE.to_le_bytes());
            out[8..12].copy_from_slice(&PVRSRV_OK.to_le_bytes());
            Ok(out)
        }
        (BRIDGE_SRVCORE, SRVCORE_RELEASEGLOBALEVENTOBJECT) => {
            let mut out = vec![0u8; 4];
            out[0..4].copy_from_slice(&PVRSRV_OK.to_le_bytes());
            Ok(out)
        }
        (BRIDGE_SRVCORE, SRVCORE_GETDEVICESTATUS) => {
            let mut out = vec![0u8; 8];
            out[0..4].copy_from_slice(&PVRSRV_OK.to_le_bytes());
            out[4..8].copy_from_slice(&PVRSRV_DEVICE_STATUS_OK.to_le_bytes());
            Ok(out)
        }
        (BRIDGE_SRVCORE, SRVCORE_GETDEVCLOCKSPEED) => {
            let mut out = vec![0u8; 8];
            out[0..4].copy_from_slice(&PVRSRV_OK.to_le_bytes());
            out[4..8].copy_from_slice(&GPU_CORE_HZ.to_le_bytes());
            Ok(out)
        }
        (BRIDGE_SRVCORE, SRVCORE_GETMULTICOREINFO) => {
            let mut out = vec![0u8; 16];
            // The vendor wrapper echoes the caller's caps pointer into the
            // out structure. The single-core AXM-8-256 has no multicore
            // data, so `RGXGetMultiCoreInfo` returns
            // `PVRSRV_ERROR_NOT_SUPPORTED`.
            out[0..8].copy_from_slice(&input[0..8]);
            out[8..12].copy_from_slice(&PVRSRV_ERROR_NOT_SUPPORTED.to_le_bytes());
            Ok(out)
        }
        _ => return_errno_with_message!(Errno::EOPNOTSUPP, "unsupported PVR bridge command"),
    }
}

/// `PVRSRV_BRIDGE_OUT_CONNECT`, packed: BVNC (8), error (4), capabilities (4),
/// kernel arch (1).
fn connect_response() -> Vec<u8> {
    let mut out = vec![0u8; 17];
    out[0..8].copy_from_slice(&PACKED_BVNC.to_le_bytes());
    out[8..12].copy_from_slice(&PVRSRV_OK.to_le_bytes());
    out[12..16].copy_from_slice(&0u32.to_le_bytes());
    // The vendor `PVRSRV_ConnectKM` writes 64 (or 32) here, not a bit
    // flag (`services/server/common/srvcore.c`); the UM reads it to pick
    // the pointer width for the bridge payloads.
    out[16] = 64;
    out
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    fn stats() -> (u64, u64, u64) {
        (
            INIT_CALLS.load(Ordering::Relaxed),
            BRIDGE_CALLS.load(Ordering::Relaxed),
            UNSUPPORTED_CALLS.load(Ordering::Relaxed),
        )
    }

    #[ktest]
    fn gpu_pvr_srvkm_init_ioctl() {
        assert!(init(1).is_ok());
        assert!(init(2).is_ok());
        assert!(init(3).is_ok());
        assert_eq!(init(0).unwrap_err().error(), Errno::EINVAL);
        assert_eq!(init(4).unwrap_err().error(), Errno::EINVAL);
        assert!(stats().0 >= 3);
    }

    #[ktest]
    fn gpu_pvr_bridge_rejects_bad_size() {
        let req = DrmPvrSrvkmCmd {
            bridge_id: BRIDGE_SRVCORE,
            bridge_func_id: SRVCORE_CONNECT,
            in_data_ptr: 0,
            out_data_ptr: 0,
            in_data_size: 8,
            out_data_size: 17,
        };
        assert_eq!(srvkm(&req).unwrap_err().error(), Errno::EINVAL);

        let req = DrmPvrSrvkmCmd {
            in_data_size: 16,
            out_data_size: 16,
            ..req
        };
        assert_eq!(srvkm(&req).unwrap_err().error(), Errno::EINVAL);
    }

    #[ktest]
    fn gpu_pvr_bridge_unknown_cmd() {
        let before = stats().2;
        let req = DrmPvrSrvkmCmd {
            bridge_id: 254,
            bridge_func_id: 0,
            in_data_ptr: 0,
            out_data_ptr: 0,
            in_data_size: 0,
            out_data_size: 0,
        };
        // Out-of-range group: the vendor dispatch returns
        // PVRSRV_ERROR_BRIDGE_EINVAL.
        assert_eq!(srvkm(&req).unwrap_err().error(), Errno::EINVAL);
        assert_eq!(stats().2, before + 1);

        // A valid group that this build does not implement follows the
        // vendor DummyBW path, PVRSRV_ERROR_BRIDGE_ENOTTY.
        let req = DrmPvrSrvkmCmd {
            bridge_id: 6,
            ..req
        };
        assert_eq!(srvkm(&req).unwrap_err().error(), Errno::ENOTTY);

        // A function past the SRVCORE boundary is EINVAL again.
        let req = DrmPvrSrvkmCmd {
            bridge_id: BRIDGE_SRVCORE,
            bridge_func_id: 200,
            ..req
        };
        assert_eq!(srvkm(&req).unwrap_err().error(), Errno::EINVAL);

        assert_eq!(unsupported_sync().unwrap_err().error(), Errno::EOPNOTSUPP);
    }

    #[ktest]
    fn gpu_pvr_bridge_min_surface() {
        // The vendor CONNECT out structure carries the kernel pointer
        // width (64), not a bit flag.
        let response = connect_response();
        assert_eq!(response[16], 64);

        // Empty-placeholder inputs are registered with size 0 by the vendor
        // dispatch table and are sent with no input buffer by the UM.
        assert_eq!(
            expected_sizes(BRIDGE_SRVCORE, SRVCORE_DISCONNECT),
            Some((0, 4))
        );
        assert_eq!(
            expected_sizes(BRIDGE_SRVCORE, SRVCORE_ACQUIREGLOBALEVENTOBJECT),
            Some((0, 12))
        );
        assert_eq!(
            expected_sizes(BRIDGE_SRVCORE, SRVCORE_GETDEVCLOCKSPEED),
            Some((0, 8))
        );
        assert_eq!(
            expected_sizes(BRIDGE_SRVCORE, SRVCORE_GETDEVICESTATUS),
            Some((0, 8))
        );

        let out = run_command(BRIDGE_SRVCORE, SRVCORE_ACQUIREGLOBALEVENTOBJECT, &[]).unwrap();
        assert_eq!(out.len(), 12);
        assert_eq!(
            u64::from_le_bytes(out[0..8].try_into().unwrap()),
            GLOBAL_EVENT_OBJECT_HANDLE
        );
        assert_eq!(
            u32::from_le_bytes(out[8..12].try_into().unwrap()),
            PVRSRV_OK
        );

        assert_eq!(
            expected_sizes(BRIDGE_SRVCORE, SRVCORE_RELEASEGLOBALEVENTOBJECT),
            Some((8, 4))
        );
        let input = 0u64.to_le_bytes();
        let out = run_command(BRIDGE_SRVCORE, SRVCORE_RELEASEGLOBALEVENTOBJECT, &input).unwrap();
        assert_eq!(out.len(), 4);
        assert_eq!(u32::from_le_bytes(out[0..4].try_into().unwrap()), PVRSRV_OK);

        assert_eq!(
            expected_sizes(BRIDGE_SRVCORE, SRVCORE_GETMULTICOREINFO),
            Some((12, 16))
        );
        let mut input = [0u8; 12];
        input[0..8].copy_from_slice(&0x1234_5678u64.to_le_bytes());
        input[8..12].copy_from_slice(&4u32.to_le_bytes());
        let out = run_command(BRIDGE_SRVCORE, SRVCORE_GETMULTICOREINFO, &input).unwrap();
        assert_eq!(out.len(), 16);
        assert_eq!(
            u64::from_le_bytes(out[0..8].try_into().unwrap()),
            0x1234_5678
        );
        assert_eq!(
            u32::from_le_bytes(out[8..12].try_into().unwrap()),
            PVRSRV_ERROR_NOT_SUPPORTED
        );
        assert_eq!(u32::from_le_bytes(out[12..16].try_into().unwrap()), 0);
    }

    #[ktest]
    fn pvr_connect_reports_the_megrez_bvnc() {
        let response = connect_response();
        assert_eq!(response.len(), 17);
        assert_eq!(
            u64::from_le_bytes(response[0..8].try_into().unwrap()),
            PACKED_BVNC
        );
        assert_eq!(u32::from_le_bytes(response[8..12].try_into().unwrap()), 0);
        assert_eq!(u32::from_le_bytes(response[12..16].try_into().unwrap()), 0);
        assert_eq!(response[16], 64);
    }
}
