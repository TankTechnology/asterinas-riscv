// SPDX-License-Identifier: MPL-2.0

//! DRM (Direct Rendering Manager) ioctl definitions and wire types.
//!
//! This module collects all `#[repr(C)]` structs that mirror the Linux UAPI
//! headers and the `ioc!()` type aliases that drive the typed ioctl dispatch in
//! [`super::DriHandle::ioctl`].

use super::{
    DrmGemClose, DrmGemFlink, DrmGemOpen, DrmGetCap, DrmModeAtomic, DrmModeCardRes,
    DrmModeCreateDumb, DrmModeCreatePropertyBlob, DrmModeCrtc, DrmModeCrtcPageFlip, DrmModeCursor,
    DrmModeCursor2, DrmModeDestroyDumb, DrmModeDestroyPropertyBlob, DrmModeFbCmd, DrmModeFbCmd2,
    DrmModeFbDirtyCmd, DrmModeGetBlob, DrmModeGetConnector, DrmModeGetEncoder, DrmModeGetPlane,
    DrmModeGetPlaneRes, DrmModeGetProperty, DrmModeMapDumb, DrmModeObjGetProperties,
    DrmPrimeHandle, DrmSetClientCap, DrmVersion,
    virtio_gpu::{
        DrmVirtgpu3dTransferFromHost, DrmVirtgpu3dTransferToHost, DrmVirtgpu3dWait,
        DrmVirtgpuContextInit, DrmVirtgpuExecbuffer, DrmVirtgpuGetCaps, DrmVirtgpuGetparam,
        DrmVirtgpuMap, DrmVirtgpuResourceCreate, DrmVirtgpuResourceInfo,
    },
};
use crate::util::ioctl::{InData, InOutData, NoData, ioc};

// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm.h>.
pub(super) type GetVersion = ioc!(DRM_IOCTL_VERSION, b'd', 0x00, InOutData<DrmVersion>);
pub(super) type GetCap = ioc!(DRM_IOCTL_GET_CAP, b'd', 0x0c, InOutData<DrmGetCap>);
pub(super) type SetClientCap = ioc!(DRM_IOCTL_SET_CLIENT_CAP, b'd', 0x0d, InData<DrmSetClientCap>);
pub(super) type SetMaster = ioc!(DRM_IOCTL_SET_MASTER, b'd', 0x1e, NoData);
pub(super) type DropMaster = ioc!(DRM_IOCTL_DROP_MASTER, b'd', 0x1f, NoData);

// GEM ioctls.
pub(super) type GemClose = ioc!(DRM_IOCTL_GEM_CLOSE, b'd', 0x09, InData<DrmGemClose>);
pub(super) type GemFlink = ioc!(DRM_IOCTL_GEM_FLINK, b'd', 0x0a, InOutData<DrmGemFlink>);
pub(super) type GemOpen = ioc!(DRM_IOCTL_GEM_OPEN, b'd', 0x0b, InOutData<DrmGemOpen>);

// PRIME / dma-buf sharing. `DRM_CAP_PRIME` is advertised as IMPORT|EXPORT above;
// these ioctls are the actual handle <-> fd conversions Mesa's virgl driver uses
// to attach a GBM dumb buffer to a virgl resource.
pub(super) type PrimeHandleToFd = ioc!(DRM_IOCTL_PRIME_HANDLE_TO_FD, b'd', 0x2d, InOutData<DrmPrimeHandle>);
pub(super) type PrimeFdToHandle = ioc!(DRM_IOCTL_PRIME_FD_TO_HANDLE, b'd', 0x2e, InOutData<DrmPrimeHandle>);

// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm_mode.h>.
pub(super) type ModeGetResources = ioc!(DRM_IOCTL_MODE_GETRESOURCES, b'd', 0xa0, InOutData<DrmModeCardRes>);
pub(super) type ModeGetCrtc = ioc!(DRM_IOCTL_MODE_GETCRTC, b'd', 0xa1, InOutData<DrmModeCrtc>);
pub(super) type ModeSetCrtc = ioc!(DRM_IOCTL_MODE_SETCRTC, b'd', 0xa2, InOutData<DrmModeCrtc>);
pub(super) type ModeCursor = ioc!(DRM_IOCTL_MODE_CURSOR, b'd', 0xa3, InOutData<DrmModeCursor>);
pub(super) type ModeGetEncoder = ioc!(DRM_IOCTL_MODE_GETENCODER, b'd', 0xa6, InOutData<DrmModeGetEncoder>);
pub(super) type ModeGetConnector = ioc!(DRM_IOCTL_MODE_GETCONNECTOR, b'd', 0xa7, InOutData<DrmModeGetConnector>);
pub(super) type ModeAddFb = ioc!(DRM_IOCTL_MODE_ADDFB, b'd', 0xae, InOutData<DrmModeFbCmd>);
pub(super) type ModePageFlip = ioc!(DRM_IOCTL_MODE_PAGE_FLIP, b'd', 0xb0, InOutData<DrmModeCrtcPageFlip>);
pub(super) type ModeDirtyFb = ioc!(DRM_IOCTL_MODE_DIRTYFB, b'd', 0xb1, InOutData<DrmModeFbDirtyCmd>);
pub(super) type ModeCreateDumb = ioc!(DRM_IOCTL_MODE_CREATE_DUMB, b'd', 0xb2, InOutData<DrmModeCreateDumb>);
pub(super) type ModeMapDumb = ioc!(DRM_IOCTL_MODE_MAP_DUMB, b'd', 0xb3, InOutData<DrmModeMapDumb>);
pub(super) type ModeDestroyDumb = ioc!(DRM_IOCTL_MODE_DESTROY_DUMB, b'd', 0xb4, InOutData<DrmModeDestroyDumb>);
pub(super) type ModeObjGetProperties = ioc!(DRM_IOCTL_MODE_OBJ_GETPROPERTIES, b'd', 0xb9, InOutData<DrmModeObjGetProperties>);
pub(super) type ModeCursor2 = ioc!(DRM_IOCTL_MODE_CURSOR2, b'd', 0xbb, InOutData<DrmModeCursor2>);

/// `DRM_IOCTL_MODE_RMFB` (`_IOWR('d', 0xaf, unsigned int)`).
///
/// Unlike the other mode ioctls, `RMFB` passes its argument by value rather than
/// by pointer, so it is dispatched by raw command instead of a typed `ioc!`.
pub(super) const MODE_RMFB_CMD: u32 = 0xc00464af;

// Atomic modesetting and plane ioctls.
pub(super) type ModeGetProperty = ioc!(DRM_IOCTL_MODE_GETPROPERTY, b'd', 0xaa, InOutData<DrmModeGetProperty>);
pub(super) type ModeGetPropertyBlob = ioc!(DRM_IOCTL_MODE_GETPROPBLOB, b'd', 0xac, InOutData<DrmModeGetBlob>);
pub(super) type ModeAtomic = ioc!(DRM_IOCTL_MODE_ATOMIC, b'd', 0xbc, InOutData<DrmModeAtomic>);
pub(super) type ModeCreatePropertyBlob = ioc!(DRM_IOCTL_MODE_CREATEPROPBLOB, b'd', 0xbd, InOutData<DrmModeCreatePropertyBlob>);
pub(super) type ModeDestroyPropertyBlob = ioc!(DRM_IOCTL_MODE_DESTROYPROPBLOB, b'd', 0xbe, InOutData<DrmModeDestroyPropertyBlob>);
pub(super) type ModeGetPlaneRes = ioc!(DRM_IOCTL_MODE_GETPLANERESOURCES, b'd', 0xb5, InOutData<DrmModeGetPlaneRes>);
pub(super) type ModeGetPlane = ioc!(DRM_IOCTL_MODE_GETPLANE, b'd', 0xb6, InOutData<DrmModeGetPlane>);
pub(super) type ModeAddFb2 = ioc!(DRM_IOCTL_MODE_ADDFB2, b'd', 0xb8, InOutData<DrmModeFbCmd2>);

// virtio-gpu specific ioctls.
pub(super) type VirtgpuExecbuffer = ioc!(DRM_VIRTGPU_EXECBUFFER, b'd', 0x42, InOutData<DrmVirtgpuExecbuffer>);
pub(super) type VirtgpuGetparam = ioc!(DRM_VIRTGPU_GETPARAM, b'd', 0x43, InOutData<DrmVirtgpuGetparam>);
pub(super) type VirtgpuResourceCreate = ioc!(DRM_VIRTGPU_RESOURCE_CREATE, b'd', 0x44, InOutData<DrmVirtgpuResourceCreate>);
pub(super) type VirtgpuResourceInfo = ioc!(DRM_VIRTGPU_RESOURCE_INFO, b'd', 0x45, InOutData<DrmVirtgpuResourceInfo>);
pub(super) type VirtgpuTransferFromHost = ioc!(DRM_VIRTGPU_TRANSFER_FROM_HOST, b'd', 0x46, InOutData<DrmVirtgpu3dTransferFromHost>);
pub(super) type VirtgpuTransferToHost = ioc!(DRM_VIRTGPU_TRANSFER_TO_HOST, b'd', 0x47, InOutData<DrmVirtgpu3dTransferToHost>);
pub(super) type VirtgpuWait = ioc!(DRM_VIRTGPU_WAIT, b'd', 0x48, InOutData<DrmVirtgpu3dWait>);
pub(super) type VirtgpuGetCaps = ioc!(DRM_VIRTGPU_GET_CAPS, b'd', 0x49, InOutData<DrmVirtgpuGetCaps>);
pub(super) type VirtgpuContextInit = ioc!(DRM_VIRTGPU_CONTEXT_INIT, b'd', 0x4b, InOutData<DrmVirtgpuContextInit>);
pub(super) type VirtgpuMap = ioc!(DRM_VIRTGPU_MAP, b'd', 0x41, InOutData<DrmVirtgpuMap>);
