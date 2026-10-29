// SPDX-License-Identifier: MPL-2.0

//! Parses the device-tree `simple-framebuffer` node into boot framebuffer info.

use fdt::{Fdt, node::FdtNode};

use crate::boot::BootloaderFramebufferArg;

pub(super) fn parse(fdt: &Fdt<'_>) -> Option<BootloaderFramebufferArg> {
    fdt.all_nodes().find_map(parse_node)
}

fn parse_node(node: FdtNode<'_, '_>) -> Option<BootloaderFramebufferArg> {
    let is_simple_framebuffer = node
        .compatible()?
        .all()
        .any(|compatible| compatible == "simple-framebuffer");
    if !is_simple_framebuffer {
        return None;
    }

    if let Some(status) = node.property("status")
        && !matches!(status.as_str()?, "ok" | "okay")
    {
        return None;
    }

    let region = node.reg()?.next()?;
    let address = region.starting_address as usize;
    let size = region.size?;
    let width = node.property("width")?.as_usize()?;
    let height = node.property("height")?.as_usize()?;
    let bpp = match node.property("format")?.as_str()? {
        "x8r8g8b8" => 32,
        "r5g6b5" => 16,
        "r8g8b8" => 24,
        _ => return None,
    };

    let minimum_size = width
        .checked_mul(height)?
        .checked_mul(bpp)?
        .div_ceil(8);
    if address == 0 || width == 0 || height == 0 || size < minimum_size {
        return None;
    }

    Some(BootloaderFramebufferArg {
        address,
        width,
        height,
        bpp,
    })
}

#[cfg(ktest)]
mod tests {
    use fdt::Fdt;

    use super::{parse, parse_node};
    use crate::prelude::ktest;

    const FIXTURE: &[u8] = include_bytes!("fixtures/simple-framebuffer.dtb");

    fn fixture() -> Fdt<'static> {
        Fdt::new(FIXTURE).unwrap()
    }

    #[ktest]
    fn parses_enabled_simple_framebuffer() {
        let framebuffer = parse(&fixture()).unwrap();

        assert_eq!(framebuffer.address, 0xfd80_0000);
        assert_eq!(framebuffer.width, 1920);
        assert_eq!(framebuffer.height, 1080);
        assert_eq!(framebuffer.bpp, 32);
    }

    #[ktest]
    fn rejects_disabled_missing_and_overflowing_metadata() {
        let fdt = fixture();

        for node_name in [
            "framebuffer@fd000000",
            "framebuffer@fd400000",
            "framebuffer@fffffffffffff000",
        ] {
            let node = fdt.all_nodes().find(|node| node.name == node_name).unwrap();
            assert!(parse_node(node).is_none(), "accepted {node_name}");
        }
    }

    #[ktest]
    fn ignores_non_simple_framebuffers() {
        let fdt = fixture();
        let node = fdt
            .all_nodes()
            .find(|node| node.name == "framebuffer@fe000000")
            .unwrap();

        assert!(parse_node(node).is_none());
    }
}
