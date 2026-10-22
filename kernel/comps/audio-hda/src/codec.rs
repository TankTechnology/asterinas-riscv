// SPDX-License-Identifier: MPL-2.0

//! Codec access through the controller's immediate-command interface, and the
//! widget enumeration a playback stream needs.

use alloc::{format, string::String, vec::Vec};

use aster_core::prelude::*;
use ostd::{info, warn};

use super::{HdaController, bus::CorbRirb, regs::*};

/// One node of a codec's widget tree.
#[derive(Clone, Copy, Debug)]
pub(super) struct Widget {
    pub id: u8,
    pub type_: u32,
    pub caps: u32,
}

/// A codec behind one HDA controller.
#[derive(Debug)]
pub(super) struct HdaCodec {
    address: u8,
    vendor_id: u32,
    device_id: u32,
    revision_id: u32,
    /// The audio function group and its widgets.
    function: Option<(u8, Vec<Widget>)>,
}

impl HdaCodec {
    /// Reads the codec's identity and widget tree.
    pub(super) fn enumerate(
        controller: &HdaController,
        bus: &mut CorbRirb,
        address: u8,
    ) -> Result<Self> {
        let vendor_id = send_verb(controller, bus, address, 0, VERB_GET_PARAMETER, PARAM_VENDOR_ID)?;
        let device_id = send_verb(controller, bus, address, 0, VERB_GET_PARAMETER, PARAM_DEVICE_ID)?;
        let revision_id = send_verb(controller, bus, address, 0, VERB_GET_PARAMETER, PARAM_REVISION_ID)?;

        // The root node lists the function groups.
        let root_nodes = send_verb(controller, bus, address, 0, VERB_GET_PARAMETER, PARAM_NODE_COUNT)?;
        let (root_start, root_count) = (root_nodes >> 16, root_nodes & 0xFFFF);

        let mut function = None;
        for node in root_start..root_start + root_count {
            let node = node as u8;
            let function_type =
                send_verb(controller, bus, address, node, VERB_GET_PARAMETER, PARAM_FUNCTION_TYPE)?;
            // Type 0x01 is an audio function group.
            if function_type & 0xFF != 0x01 {
                continue;
            }

            let nodes = send_verb(controller, bus, address, node, VERB_GET_PARAMETER, PARAM_NODE_COUNT)?;
            let (start, count) = (nodes >> 16, nodes & 0xFFFF);
            let mut widgets = Vec::new();
            for widget in start..start + count {
                let caps =
                    send_verb(controller, bus, address, widget as u8, VERB_GET_PARAMETER, PARAM_WIDGET_CAPS)?;
                widgets.push(Widget {
                    id: widget as u8,
                    type_: (caps >> 20) & 0xF,
                    caps,
                });
            }
            function = Some((node, widgets));
            break;
        }

        Ok(Self {
            address,
            vendor_id,
            device_id,
            revision_id,
            function,
        })
    }

    /// Returns output-capable pins and the DAC each one lists as its source.
    pub(super) fn output_paths(
        &self,
        controller: &HdaController,
        bus: &mut CorbRirb,
    ) -> Result<Vec<(u8, u8)>> {
        let Some((_, widgets)) = &self.function else {
            return Ok(Vec::new());
        };

        let mut paths = Vec::new();
        for pin in widgets.iter().filter(|w| w.type_ == WIDGET_TYPE_PIN_COMPLEX) {
            let caps =
                send_verb(controller, bus, self.address, pin.id, VERB_GET_PARAMETER, PARAM_PIN_CAPS)?;
            if caps & PIN_CAP_OUTPUT == 0 {
                continue;
            }

            let count = send_verb(
                controller,
                bus,
                self.address,
                pin.id,
                VERB_GET_PARAMETER,
                PARAM_CONNECTION_COUNT,
            )? & 0x7F;
            if count == 0 {
                continue;
            }
            let connections =
                send_verb(controller, bus, self.address, pin.id, VERB_GET_CONNECTION_LIST, 0)?;
            // Long-form connection lists carry 16-bit node ids.
            let source = (connections & 0xFFFF) as u8;
            paths.push((pin.id, source));
        }

        Ok(paths)
    }

    pub(super) fn address(&self) -> u8 {
        self.address
    }

    /// Returns the first audio-output converter (DAC).
    pub(super) fn dac(&self) -> Option<u8> {
        self.function
            .as_ref()?
            .1
            .iter()
            .find(|widget| widget.type_ == WIDGET_TYPE_AUDIO_OUTPUT)
            .map(|widget| widget.id)
    }

    /// Returns the first pin complex; QEMU's codec exposes one output pin.
    pub(super) fn output_pin(&self) -> Option<u8> {
        self.function
            .as_ref()?
            .1
            .iter()
            .find(|widget| widget.type_ == WIDGET_TYPE_PIN_COMPLEX)
            .map(|widget| widget.id)
    }

    /// Logs the identity and widget tree; this is the acceptance of the
    /// controller bring-up increment.
    pub(super) fn log_summary(&self) {
        info!(
            "codec {} vendor={:#010x} device={:#010x} revision={:#010x}",
            self.address, self.vendor_id, self.device_id, self.revision_id
        );

        let Some((function, widgets)) = &self.function else {
            info!("codec {} has no audio function group", self.address);
            return;
        };

        let types: String = widgets
            .iter()
            .map(|widget| format!("{}:{:x}", widget.id, widget.type_))
            .collect::<Vec<_>>()
            .join(" ");
        info!(
            "codec {} audio function {} widgets=[{}]",
            self.address, function, types
        );
    }
}

/// Sends a raw 32-bit command through the ring.
pub(super) fn send_command(
    controller: &HdaController,
    bus: &mut CorbRirb,
    command: u32,
) -> Result<u32> {
    bus.send(controller, command)
}

/// Sends one 12-bit verb with its 8-bit payload through the command ring.
pub(super) fn send_verb(
    controller: &HdaController,
    bus: &mut CorbRirb,
    codec: u8,
    node: u8,
    verb: u16,
    payload: u8,
) -> Result<u32> {
    let command = ((codec as u32) << 28)
        | ((node as u32) << 20)
        | ((verb as u32) << 8)
        | payload as u32;
    bus.send(controller, command)
}
