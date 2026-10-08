// SPDX-License-Identifier: MPL-2.0

//! USB HID mouse report decoding and input-device integration.
//!
//! Every report is read through a [`MouseReportLayout`]: the descriptor-derived
//! one when the device described a report this kernel understands, and the
//! fixed boot layout otherwise. The boot report has no wheel field, so a device
//! that only ever sends it cannot report one.

use alloc::{sync::Arc, vec::Vec};

use aster_input::{
    event_type_codes::{EventTypes, KeyCode, KeyStatus, RelCode, SynEvent},
    input_dev::{InputCapability, InputDevice, InputEvent, InputId, RegisteredInputDevice},
};
use ostd::bus::usb::MouseReportLayout;

/// The boot mouse report: three button bits, then two eight-bit displacements.
pub(super) const BOOT_MOUSE_LAYOUT: MouseReportLayout = MouseReportLayout {
    buttons_offset: 0,
    buttons_bits: 3,
    x_offset: 8,
    x_bits: 8,
    y_offset: 16,
    y_bits: 8,
    wheel_offset: None,
    wheel_bits: 0,
    report_bytes: 3,
};

const BUTTONS: [(u8, KeyCode); 3] = [
    (1 << 0, KeyCode::BtnLeft),
    (1 << 1, KeyCode::BtnRight),
    (1 << 2, KeyCode::BtnMiddle),
];

/// Reads one little-endian bit field out of a report without interpreting it.
fn raw_field(bytes: &[u8], offset: u8, bits: u8) -> Option<u32> {
    if bits == 0 || bits >= 32 {
        return None;
    }
    if usize::from(offset) + usize::from(bits) > bytes.len() * 8 {
        return None;
    }
    let mut raw = 0u32;
    for bit in 0..usize::from(bits) {
        let index = usize::from(offset) + bit;
        let value = (bytes[index / 8] >> (index % 8)) & 1;
        raw |= u32::from(value) << bit;
    }
    Some(raw)
}

/// Reads one signed two's-complement bit field out of a report.
fn signed_field(bytes: &[u8], offset: u8, bits: u8) -> Option<i32> {
    let raw = raw_field(bytes, offset, bits)?;
    let sign = 1u32 << (bits - 1);
    Some(if raw & sign != 0 {
        raw as i32 - (1i32 << bits)
    } else {
        raw as i32
    })
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum MouseReportError {
    InvalidLength,
}

#[derive(Debug)]
struct UsbBootMouseDevice {
    id: InputId,
    capability: InputCapability,
}

impl UsbBootMouseDevice {
    fn new(vendor_id: u16, product_id: u16, layout: MouseReportLayout) -> Self {
        let mut capability = InputCapability::new();
        capability.set_supported_event_type(EventTypes::SYN);
        for (_, button) in BUTTONS {
            capability.set_supported_key(button);
        }
        capability.set_supported_relative_axis(RelCode::X);
        capability.set_supported_relative_axis(RelCode::Y);
        // Advertising a wheel a device never sends is what made a boot-protocol
        // mouse look like it had one, so the axis follows the layout.
        if layout.wheel_offset.is_some() {
            capability.set_supported_relative_axis(RelCode::Wheel);
        }
        Self {
            id: InputId::new(InputId::BUS_USB, vendor_id, product_id, 0x0001),
            capability,
        }
    }
}

impl InputDevice for UsbBootMouseDevice {
    fn name(&self) -> &str {
        "usb_boot_mouse"
    }

    fn phys(&self) -> &str {
        "xhci/input1"
    }

    fn uniq(&self) -> &str {
        ""
    }

    fn id(&self) -> InputId {
        self.id
    }

    fn capability(&self) -> &InputCapability {
        &self.capability
    }
}

pub(super) fn register(
    vendor_id: u16,
    product_id: u16,
    layout: MouseReportLayout,
) -> RegisteredInputDevice {
    aster_input::register_device(Arc::new(UsbBootMouseDevice::new(
        vendor_id, product_id, layout,
    )))
}

pub(super) struct HidBootMouse {
    previous_buttons: u8,
    layout: MouseReportLayout,
}

impl HidBootMouse {
    pub(super) const fn new(layout: MouseReportLayout) -> Self {
        Self {
            previous_buttons: 0,
            layout,
        }
    }

    /// Decodes one report through the layout the device reported.
    ///
    /// A report too short for that layout is read as the boot report instead,
    /// because a three-byte report is the boot report by definition: a device
    /// that keeps sending one after being asked for the report protocol must
    /// not be mistaken for one that moved. Only a report short for both layouts
    /// is rejected.
    pub(super) fn decode(
        &mut self,
        report: [u8; 8],
        actual_length: usize,
    ) -> Result<Vec<InputEvent>, MouseReportError> {
        let bytes = report
            .get(..actual_length)
            .ok_or(MouseReportError::InvalidLength)?;
        let layout = if actual_length >= usize::from(self.layout.report_bytes) {
            self.layout
        } else {
            BOOT_MOUSE_LAYOUT
        };
        if actual_length < usize::from(layout.report_bytes) {
            return Err(MouseReportError::InvalidLength);
        }

        let buttons = u8::try_from(
            raw_field(bytes, layout.buttons_offset, layout.buttons_bits)
                .ok_or(MouseReportError::InvalidLength)?,
        )
        .map_err(|_| MouseReportError::InvalidLength)?;

        let mut events = Vec::new();
        for (mask, button) in BUTTONS {
            let was_pressed = self.previous_buttons & mask != 0;
            let is_pressed = buttons & mask != 0;
            if was_pressed != is_pressed {
                events.push(InputEvent::from_key_and_status(
                    button,
                    if is_pressed {
                        KeyStatus::Pressed
                    } else {
                        KeyStatus::Released
                    },
                ));
            }
        }
        self.previous_buttons = buttons;

        for (offset, bits, code) in [
            (layout.x_offset, layout.x_bits, RelCode::X),
            (layout.y_offset, layout.y_bits, RelCode::Y),
        ] {
            let value = signed_field(bytes, offset, bits).ok_or(MouseReportError::InvalidLength)?;
            if value != 0 {
                events.push(InputEvent::from_relative_move(code, value));
            }
        }
        if let Some(offset) = layout.wheel_offset {
            let wheel = signed_field(bytes, offset, layout.wheel_bits)
                .ok_or(MouseReportError::InvalidLength)?;
            if wheel != 0 {
                events.push(InputEvent::from_relative_move(RelCode::Wheel, wheel));
            }
        }
        if !events.is_empty() {
            events.push(InputEvent::from_sync_event(SynEvent::Report));
        }
        Ok(events)
    }
}

#[cfg(ktest)]
mod tests {
    use alloc::vec;

    use aster_input::{
        event_type_codes::{EventTypes, KeyCode, KeyStatus, RelCode, SynEvent},
        input_dev::{InputDevice, InputEvent, InputId},
    };
    use ostd::{bus::usb::MouseReportLayout, prelude::ktest};

    use super::{BOOT_MOUSE_LAYOUT, HidBootMouse, MouseReportError, UsbBootMouseDevice};

    /// The layout the attached Megrez mouse describes: three button bits, two
    /// twelve-bit displacements, then an eight-bit wheel.
    const TWELVE_BIT_MOUSE_LAYOUT: MouseReportLayout = MouseReportLayout {
        buttons_offset: 0,
        buttons_bits: 3,
        x_offset: 8,
        x_bits: 12,
        y_offset: 20,
        y_bits: 12,
        wheel_offset: Some(32),
        wheel_bits: 8,
        report_bytes: 5,
    };

    fn fill(prefix: &[u8]) -> [u8; 8] {
        let mut report = [0u8; 8];
        report[..prefix.len()].copy_from_slice(prefix);
        report
    }

    #[ktest]
    fn decodes_movement_button_transitions_and_sync() {
        let mut mouse = HidBootMouse::new(BOOT_MOUSE_LAYOUT);

        assert_eq!(
            mouse.decode(fill(&[0b001, 5, (-3_i8) as u8]), 3),
            Ok(vec![
                InputEvent::from_key_and_status(KeyCode::BtnLeft, KeyStatus::Pressed),
                InputEvent::from_relative_move(RelCode::X, 5),
                InputEvent::from_relative_move(RelCode::Y, -3),
                InputEvent::from_sync_event(SynEvent::Report),
            ])
        );
        assert_eq!(
            mouse.decode(fill(&[0]), 3),
            Ok(vec![
                InputEvent::from_key_and_status(KeyCode::BtnLeft, KeyStatus::Released),
                InputEvent::from_sync_event(SynEvent::Report),
            ])
        );
        assert_eq!(mouse.decode(fill(&[0]), 3), Ok(vec![]));
    }

    #[ktest]
    fn reads_the_twelve_bit_axes_and_wheel_the_descriptor_defines() {
        let mut mouse = HidBootMouse::new(TWELVE_BIT_MOUSE_LAYOUT);

        // X=5 sits in bits 8..19 and Y=-3 in bits 20..31, so the middle byte
        // carries X's high nibble below Y's low nibble.
        assert_eq!(
            mouse.decode(fill(&[0b001, 0x05, 0xd0, 0xff, 0x01]), 5),
            Ok(vec![
                InputEvent::from_key_and_status(KeyCode::BtnLeft, KeyStatus::Pressed),
                InputEvent::from_relative_move(RelCode::X, 5),
                InputEvent::from_relative_move(RelCode::Y, -3),
                InputEvent::from_relative_move(RelCode::Wheel, 1),
                InputEvent::from_sync_event(SynEvent::Report),
            ])
        );
    }

    #[ktest]
    fn falls_back_to_the_boot_report_a_device_keeps_sending() {
        // A device that ignored SET_PROTOCOL still sends three-byte reports,
        // which must keep moving the pointer rather than be rejected.
        let mut mouse = HidBootMouse::new(TWELVE_BIT_MOUSE_LAYOUT);

        assert_eq!(
            mouse.decode(fill(&[0, 7, (-2_i8) as u8]), 3),
            Ok(vec![
                InputEvent::from_relative_move(RelCode::X, 7),
                InputEvent::from_relative_move(RelCode::Y, -2),
                InputEvent::from_sync_event(SynEvent::Report),
            ])
        );
    }

    #[ktest]
    fn rejects_reports_short_for_every_layout_without_changing_state() {
        let mut mouse = HidBootMouse::new(TWELVE_BIT_MOUSE_LAYOUT);

        assert_eq!(
            mouse.decode(fill(&[1]), 2),
            Err(MouseReportError::InvalidLength)
        );
        assert_eq!(
            mouse.decode([1, 0, 0, 0, 0, 0, 0, 0], 9),
            Err(MouseReportError::InvalidLength)
        );
        assert_eq!(
            mouse.decode(fill(&[1]), 3),
            Ok(vec![
                InputEvent::from_key_and_status(KeyCode::BtnLeft, KeyStatus::Pressed),
                InputEvent::from_sync_event(SynEvent::Report),
            ])
        );
    }

    #[ktest]
    fn advertises_a_wheel_only_when_the_layout_has_one() {
        let without = UsbBootMouseDevice::new(0x1234, 0xabcd, BOOT_MOUSE_LAYOUT);
        assert!(without.capability().support_relative_axis(RelCode::X));
        assert!(without.capability().support_relative_axis(RelCode::Y));
        assert!(!without.capability().support_relative_axis(RelCode::Wheel));

        let with = UsbBootMouseDevice::new(0x1234, 0xabcd, TWELVE_BIT_MOUSE_LAYOUT);
        assert!(with.capability().support_relative_axis(RelCode::Wheel));
    }

    #[ktest]
    fn advertises_exact_boot_mouse_identity_and_capabilities() {
        let device = UsbBootMouseDevice::new(0x1234, 0xabcd, TWELVE_BIT_MOUSE_LAYOUT);
        let capability = device.capability();

        assert_eq!(device.name(), "usb_boot_mouse");
        assert_eq!(device.phys(), "xhci/input1");
        assert_eq!(device.id().bustype(), InputId::BUS_USB);
        assert_eq!(device.id().vendor(), 0x1234);
        assert_eq!(device.id().product(), 0xabcd);
        for event_type in [EventTypes::SYN, EventTypes::KEY, EventTypes::REL] {
            assert!(capability.support_event_type(event_type));
        }
        for key in [KeyCode::BtnLeft, KeyCode::BtnRight, KeyCode::BtnMiddle] {
            assert!(capability.support_key(key));
        }
        for axis in [RelCode::X, RelCode::Y, RelCode::Wheel] {
            assert!(capability.support_relative_axis(axis));
        }
        assert!(!capability.look_like_keyboard());
    }
}
