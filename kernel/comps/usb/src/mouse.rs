// SPDX-License-Identifier: MPL-2.0

//! USB HID boot-mouse report decoding and input-device integration.

use alloc::{sync::Arc, vec::Vec};

use aster_input::{
    event_type_codes::{EventTypes, KeyCode, KeyStatus, RelCode, SynEvent},
    input_dev::{InputCapability, InputDevice, InputEvent, InputId, RegisteredInputDevice},
};

const BUTTON_MASK: u8 = 0b111;
const BUTTONS: [(u8, KeyCode); 3] = [
    (1 << 0, KeyCode::BtnLeft),
    (1 << 1, KeyCode::BtnRight),
    (1 << 2, KeyCode::BtnMiddle),
];

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
    fn new(vendor_id: u16, product_id: u16) -> Self {
        let mut capability = InputCapability::new();
        capability.set_supported_event_type(EventTypes::SYN);
        for (_, button) in BUTTONS {
            capability.set_supported_key(button);
        }
        for axis in [RelCode::X, RelCode::Y, RelCode::Wheel] {
            capability.set_supported_relative_axis(axis);
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

pub(super) fn register(vendor_id: u16, product_id: u16) -> RegisteredInputDevice {
    aster_input::register_device(Arc::new(UsbBootMouseDevice::new(vendor_id, product_id)))
}

pub(super) struct HidBootMouse {
    previous_buttons: u8,
}

impl HidBootMouse {
    pub(super) const fn new() -> Self {
        Self {
            previous_buttons: 0,
        }
    }

    pub(super) fn decode(
        &mut self,
        report: [u8; 4],
        actual_length: usize,
    ) -> Result<Vec<InputEvent>, MouseReportError> {
        if !(3..=4).contains(&actual_length) {
            return Err(MouseReportError::InvalidLength);
        }

        let buttons = report[0] & BUTTON_MASK;
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

        let x = i32::from(report[1] as i8);
        let y = i32::from(report[2] as i8);
        if x != 0 {
            events.push(InputEvent::from_relative_move(RelCode::X, x));
        }
        if y != 0 {
            events.push(InputEvent::from_relative_move(RelCode::Y, y));
        }
        if actual_length == 4 {
            let wheel = i32::from(report[3] as i8);
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
    use ostd::prelude::ktest;

    use super::{HidBootMouse, MouseReportError, UsbBootMouseDevice};

    #[ktest]
    fn decodes_movement_button_transitions_and_sync() {
        let mut mouse = HidBootMouse::new();

        assert_eq!(
            mouse.decode([0b001, 5, (-3_i8) as u8, 0], 3),
            Ok(vec![
                InputEvent::from_key_and_status(KeyCode::BtnLeft, KeyStatus::Pressed),
                InputEvent::from_relative_move(RelCode::X, 5),
                InputEvent::from_relative_move(RelCode::Y, -3),
                InputEvent::from_sync_event(SynEvent::Report),
            ])
        );
        assert_eq!(
            mouse.decode([0, 0, 0, 0], 3),
            Ok(vec![
                InputEvent::from_key_and_status(KeyCode::BtnLeft, KeyStatus::Released),
                InputEvent::from_sync_event(SynEvent::Report),
            ])
        );
        assert_eq!(mouse.decode([0, 0, 0, 0], 3), Ok(vec![]));
    }

    #[ktest]
    fn decodes_three_buttons_signed_limits_and_optional_wheel() {
        let mut mouse = HidBootMouse::new();

        assert_eq!(
            mouse.decode([0b110, i8::MIN as u8, i8::MAX as u8, (-1_i8) as u8], 4),
            Ok(vec![
                InputEvent::from_key_and_status(KeyCode::BtnRight, KeyStatus::Pressed),
                InputEvent::from_key_and_status(KeyCode::BtnMiddle, KeyStatus::Pressed),
                InputEvent::from_relative_move(RelCode::X, i32::from(i8::MIN)),
                InputEvent::from_relative_move(RelCode::Y, i32::from(i8::MAX)),
                InputEvent::from_relative_move(RelCode::Wheel, -1),
                InputEvent::from_sync_event(SynEvent::Report),
            ])
        );
    }

    #[ktest]
    fn rejects_reports_outside_boot_mouse_lengths_without_changing_state() {
        let mut mouse = HidBootMouse::new();

        assert_eq!(
            mouse.decode([1, 0, 0, 0], 2),
            Err(MouseReportError::InvalidLength)
        );
        assert_eq!(
            mouse.decode([1, 0, 0, 0], 5),
            Err(MouseReportError::InvalidLength)
        );
        assert_eq!(
            mouse.decode([1, 0, 0, 0], 3),
            Ok(vec![
                InputEvent::from_key_and_status(KeyCode::BtnLeft, KeyStatus::Pressed),
                InputEvent::from_sync_event(SynEvent::Report),
            ])
        );
    }

    #[ktest]
    fn advertises_exact_boot_mouse_identity_and_capabilities() {
        let device = UsbBootMouseDevice::new(0x1234, 0xabcd);
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
