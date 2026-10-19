// SPDX-License-Identifier: MPL-2.0

use bitflags::bitflags;
use int_to_c_enum::TryFromInt;

/// VirtioNet header precedes each packet
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct VirtioNetHdr {
    flags: Flags,
    gso_type: u8,
    hdr_len: u16,
    gso_size: u16,
    csum_start: u16,
    csum_offset: u16,
    // With VIRTIO 1.0+ (PCI modern), this field is always part of the header
    // and the header is 12 bytes even when `VIRTIO_NET_F_MRG_RXBUF` is not
    // negotiated.  In legacy mode it is present only if that feature is
    // negotiated, which this driver never does, so a legacy device uses the
    // first 10 bytes alone.
    num_buffers: u16,
}

/// Length of the virtio-net header in legacy (pre-1.0) mode without
/// `VIRTIO_NET_F_MRG_RXBUF`.
pub(super) const VIRTIO_NET_HDR_LEGACY_LEN: usize = 10;

const _: () = assert!(size_of::<VirtioNetHdr>() == 12);

bitflags! {
    #[repr(C)]
    #[derive(Default, Pod)]
    pub(super) struct Flags: u8 {
        const VIRTIO_NET_HDR_F_NEEDS_CSUM = 1;
        const VIRTIO_NET_HDR_F_DATA_VALID = 2;
        const VIRTIO_NET_HDR_F_RSC_INFO = 4;
    }
}

#[expect(non_camel_case_types)]
#[expect(dead_code)]
#[repr(u8)]
#[derive(Clone, Copy, Debug, Default, TryFromInt)]
pub(super) enum GsoType {
    #[default]
    VIRTIO_NET_HDR_GSO_NONE = 0,
    VIRTIO_NET_HDR_GSO_TCPV4 = 1,
    VIRTIO_NET_HDR_GSO_UDP = 3,
    VIRTIO_NET_HDR_GSO_TCPV6 = 4,
    VIRTIO_NET_HDR_GSO_UDP_L4 = 5,
    VIRTIO_NET_HDR_GSO_ECN = 0x80,
}
