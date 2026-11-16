// SPDX-License-Identifier: MPL-2.0

use bitflags::bitflags;
use int_to_c_enum::TryFromInt;

/// VirtioNet header precedes each packet
///
/// The wire size of the header depends on the transport, not on the feature
/// set alone:
///
/// * a legacy transport uses the plain 10-byte `virtio_net_hdr`;
/// * a VIRTIO 1.0+ transport always carries the trailing `num_buffers` field,
///   so the header is 12 bytes even though this driver does not negotiate
///   `VIRTIO_NET_F_MRG_RXBUF`.
///
/// Writing the wrong size shifts every frame by two bytes: the device reads
/// the end of the header as the start of the Ethernet header (TX) or the
/// driver skips two bytes of the frame (RX). Both look like a dead link.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct VirtioNetHdr {
    flags: u8,
    gso_type: u8,
    hdr_len: u16,
    gso_size: u16,
    csum_start: u16,
    csum_offset: u16,
}

/// Wire size of [`VirtioNetHdr`] on a legacy transport.
pub(super) const LEGACY_HDR_LEN: usize = size_of::<VirtioNetHdr>();

/// Wire size of [`VirtioNetHdr`] on a VIRTIO 1.0+ transport: the 10-byte
/// header plus the always-present `num_buffers` field.
pub(super) const MODERN_HDR_LEN: usize = LEGACY_HDR_LEN + size_of::<u16>();

bitflags! {
    #[derive(Clone, Copy, Debug, Default)]
    pub(super) struct Flags: u8 {
        const VIRTIO_NET_HDR_F_NEEDS_CSUM = 1;
        const VIRTIO_NET_HDR_F_DATA_VALID = 2;
        const VIRTIO_NET_HDR_F_RSC_INFO   = 4;
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
