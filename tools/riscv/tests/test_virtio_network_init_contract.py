#!/usr/bin/env python3

from __future__ import annotations

import unittest
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[3]
VIRTIO = ROOT / "kernel/comps/virtio/src/lib.rs"


class VirtioNetworkInitContractTests(unittest.TestCase):
    def test_network_dma_pool_is_initialized_only_for_network_transport(self) -> None:
        source = VIRTIO.read_text()

        match_body = source.split("let res = match device_type", 1)[1]
        pre_match = source.split("let res = match device_type", 1)[0]
        self.assertEqual(
            source.count("device::socket::init();"),
            1,
            "the socket registry must be initialized once even without a socket device",
        )
        self.assertIn(
            "device::socket::init();",
            pre_match,
            "socket lookup must remain safe when no vsock transport is present",
        )
        self.assertRegex(
            match_body,
            r"(?s)VirtioDeviceType::Socket => \{.*?device::socket::init_buffers\(\);",
            "socket DMA pools must be initialized in the socket transport arm",
        )

        for device_kind in ("network",):
            init_call = f"device::{device_kind}::init();"
            self.assertEqual(
                source.count(init_call),
                1,
                f"{device_kind} DMA pools must have one guarded initialization site",
            )
            self.assertRegex(
                match_body,
                rf"(?s)VirtioDeviceType::{device_kind.title()} => \{{.*?{re.escape(init_call)}",
                f"{device_kind} pools must be initialized in its transport arm",
            )
            self.assertNotIn(
                init_call,
                pre_match,
                f"a board without a {device_kind} transport must not allocate its DMA pages",
            )


if __name__ == "__main__":
    unittest.main()
