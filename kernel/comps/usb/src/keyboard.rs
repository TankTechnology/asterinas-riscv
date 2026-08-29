// SPDX-License-Identifier: MPL-2.0

//! USB HID boot-keyboard report decoding and input-device integration.

use alloc::{sync::Arc, vec::Vec};

use aster_input::{
    event_type_codes::{EventTypes, KeyCode, KeyCodeSet, KeyStatus, SynEvent},
    input_dev::{InputCapability, InputDevice, InputEvent, InputId, RegisteredInputDevice},
};

const REPORT_LEN: usize = 8;
const KEY_ARRAY_OFFSET: usize = 2;
const ORDINARY_USAGE_COUNT: usize = REPORT_LEN - KEY_ARRAY_OFFSET;
const ERROR_ROLLOVER_USAGE: u8 = 0x01;
const MAX_BOOT_KEYBOARD_USAGE: u8 = 0x65;
const MODIFIERS: [(u8, KeyCode); 8] = [
    (1 << 0, KeyCode::LeftCtrl),
    (1 << 1, KeyCode::LeftShift),
    (1 << 2, KeyCode::LeftAlt),
    (1 << 3, KeyCode::LeftMeta),
    (1 << 4, KeyCode::RightCtrl),
    (1 << 5, KeyCode::RightShift),
    (1 << 6, KeyCode::RightAlt),
    (1 << 7, KeyCode::RightMeta),
];

#[derive(Debug)]
struct UsbBootKeyboardDevice {
    id: InputId,
    capability: InputCapability,
}

impl UsbBootKeyboardDevice {
    fn new(vendor_id: u16, product_id: u16) -> Self {
        Self {
            id: InputId::new(InputId::BUS_USB, vendor_id, product_id, 0x0001),
            capability: boot_keyboard_capability(),
        }
    }
}

fn boot_keyboard_capability() -> InputCapability {
    let mut capability = InputCapability::new();
    capability.set_supported_event_type(EventTypes::SYN);
    for (_, key) in MODIFIERS {
        capability.set_supported_key(key);
    }
    for usage in 0..=MAX_BOOT_KEYBOARD_USAGE {
        if let Some(key) = usage_to_key_code(usage) {
            capability.set_supported_key(key);
        }
    }
    capability
}

impl InputDevice for UsbBootKeyboardDevice {
    fn name(&self) -> &str {
        "usb_boot_keyboard"
    }

    fn phys(&self) -> &str {
        "xhci/input0"
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

#[cfg_attr(
    all(ktest, not(target_arch = "riscv64")),
    expect(
        dead_code,
        reason = "registration is architecture-specific; decoder tests are portable"
    )
)]
pub(super) fn register(vendor_id: u16, product_id: u16) -> RegisteredInputDevice {
    aster_input::register_device(Arc::new(UsbBootKeyboardDevice::new(vendor_id, product_id)))
}

pub(super) struct HidBootKeyboard {
    previous_modifier_mask: u8,
    previous_usages: [u8; ORDINARY_USAGE_COUNT],
    pressed_ordinary_keys: KeyCodeSet,
}

impl HidBootKeyboard {
    pub(super) fn new() -> Self {
        Self {
            previous_modifier_mask: 0,
            previous_usages: [0; ORDINARY_USAGE_COUNT],
            pressed_ordinary_keys: KeyCodeSet::new(),
        }
    }

    pub(super) fn decode(&mut self, report: [u8; REPORT_LEN]) -> Vec<InputEvent> {
        let modifier_mask = report[0];
        let usages = &report[KEY_ARRAY_OFFSET..];
        let has_rollover = usages.contains(&ERROR_ROLLOVER_USAGE);

        let mut events = Vec::new();
        for (mask, key) in MODIFIERS {
            let was_pressed = self.previous_modifier_mask & mask != 0;
            let is_pressed = modifier_mask & mask != 0;
            if was_pressed != is_pressed {
                events.push(InputEvent::from_key_and_status(
                    key,
                    if is_pressed {
                        KeyStatus::Pressed
                    } else {
                        KeyStatus::Released
                    },
                ));
            }
        }

        if !has_rollover {
            for index in 0..ORDINARY_USAGE_COUNT {
                let previous_usage = self.previous_usages[index];
                if previous_usage != 0
                    && !self.previous_usages[..index].contains(&previous_usage)
                    && !usages.contains(&previous_usage)
                    && let Some(key) = usage_to_key_code(previous_usage)
                {
                    self.update_ordinary_key(&mut events, key, KeyStatus::Released);
                }

                let usage = usages[index];
                if usage != 0
                    && !usages[..index].contains(&usage)
                    && !self.previous_usages.contains(&usage)
                    && let Some(key) = usage_to_key_code(usage)
                {
                    self.update_ordinary_key(&mut events, key, KeyStatus::Pressed);
                }
            }
        }

        self.previous_modifier_mask = modifier_mask;
        // The versioned Linux UHID oracle in `keyboard_linux_vectors.rs` shows that modifiers
        // advance on ErrorRollOver, while both raw and logical ordinary-key state are retained
        // because the reported ordinary usages are untrustworthy.
        if !has_rollover {
            self.previous_usages.copy_from_slice(usages);
        }
        if !events.is_empty() {
            events.push(InputEvent::from_sync_event(SynEvent::Report));
        }
        events
    }

    fn update_ordinary_key(
        &mut self,
        events: &mut Vec<InputEvent>,
        key: KeyCode,
        status: KeyStatus,
    ) {
        let is_pressed = status == KeyStatus::Pressed;
        if self.pressed_ordinary_keys.contain(key) == is_pressed {
            return;
        }

        if is_pressed {
            self.pressed_ordinary_keys.set(key);
        } else {
            self.pressed_ordinary_keys.clear(key);
        }
        events.push(InputEvent::from_key_and_status(key, status));
    }
}

fn usage_to_key_code(usage: u8) -> Option<KeyCode> {
    Some(match usage {
        0x04 => KeyCode::A,
        0x05 => KeyCode::B,
        0x06 => KeyCode::C,
        0x07 => KeyCode::D,
        0x08 => KeyCode::E,
        0x09 => KeyCode::F,
        0x0a => KeyCode::G,
        0x0b => KeyCode::H,
        0x0c => KeyCode::I,
        0x0d => KeyCode::J,
        0x0e => KeyCode::K,
        0x0f => KeyCode::L,
        0x10 => KeyCode::M,
        0x11 => KeyCode::N,
        0x12 => KeyCode::O,
        0x13 => KeyCode::P,
        0x14 => KeyCode::Q,
        0x15 => KeyCode::R,
        0x16 => KeyCode::S,
        0x17 => KeyCode::T,
        0x18 => KeyCode::U,
        0x19 => KeyCode::V,
        0x1a => KeyCode::W,
        0x1b => KeyCode::X,
        0x1c => KeyCode::Y,
        0x1d => KeyCode::Z,
        0x1e => KeyCode::Num1,
        0x1f => KeyCode::Num2,
        0x20 => KeyCode::Num3,
        0x21 => KeyCode::Num4,
        0x22 => KeyCode::Num5,
        0x23 => KeyCode::Num6,
        0x24 => KeyCode::Num7,
        0x25 => KeyCode::Num8,
        0x26 => KeyCode::Num9,
        0x27 => KeyCode::Num0,
        0x28 => KeyCode::Enter,
        0x29 => KeyCode::Esc,
        0x2a => KeyCode::Backspace,
        0x2b => KeyCode::Tab,
        0x2c => KeyCode::Space,
        0x2d => KeyCode::Minus,
        0x2e => KeyCode::Equal,
        0x2f => KeyCode::LeftBrace,
        0x30 => KeyCode::RightBrace,
        0x31 => KeyCode::Backslash,
        // Linux aliases usages 0x31 and 0x32 to KEY_BACKSLASH; see
        // `backslash_alias_keycode_state` in the versioned oracle fixture.
        0x32 => KeyCode::Backslash,
        0x33 => KeyCode::Semicolon,
        0x34 => KeyCode::Apostrophe,
        0x35 => KeyCode::Grave,
        0x36 => KeyCode::Comma,
        0x37 => KeyCode::Dot,
        0x38 => KeyCode::Slash,
        0x39 => KeyCode::CapsLock,
        0x3a => KeyCode::F1,
        0x3b => KeyCode::F2,
        0x3c => KeyCode::F3,
        0x3d => KeyCode::F4,
        0x3e => KeyCode::F5,
        0x3f => KeyCode::F6,
        0x40 => KeyCode::F7,
        0x41 => KeyCode::F8,
        0x42 => KeyCode::F9,
        0x43 => KeyCode::F10,
        0x44 => KeyCode::F11,
        0x45 => KeyCode::F12,
        0x46 => KeyCode::SysRq,
        0x47 => KeyCode::ScrollLock,
        0x48 => KeyCode::Pause,
        0x49 => KeyCode::Insert,
        0x4a => KeyCode::Home,
        0x4b => KeyCode::PageUp,
        0x4c => KeyCode::Delete,
        0x4d => KeyCode::End,
        0x4e => KeyCode::PageDown,
        0x4f => KeyCode::Right,
        0x50 => KeyCode::Left,
        0x51 => KeyCode::Down,
        0x52 => KeyCode::Up,
        0x53 => KeyCode::NumLock,
        0x54 => KeyCode::KpSlash,
        0x55 => KeyCode::KpAsterisk,
        0x56 => KeyCode::KpMinus,
        0x57 => KeyCode::KpPlus,
        0x58 => KeyCode::KpEnter,
        0x59 => KeyCode::Kp1,
        0x5a => KeyCode::Kp2,
        0x5b => KeyCode::Kp3,
        0x5c => KeyCode::Kp4,
        0x5d => KeyCode::Kp5,
        0x5e => KeyCode::Kp6,
        0x5f => KeyCode::Kp7,
        0x60 => KeyCode::Kp8,
        0x61 => KeyCode::Kp9,
        0x62 => KeyCode::Kp0,
        0x63 => KeyCode::KpDot,
        0x64 => KeyCode::Key102nd,
        0x65 => KeyCode::Compose,
        _ => return None,
    })
}

#[cfg(ktest)]
#[path = "keyboard_linux_vectors.rs"]
mod keyboard_linux_vectors;

#[cfg(ktest)]
mod tests {
    use alloc::vec::Vec;

    use aster_input::{
        event_type_codes::{KeyCode, KeyStatus, SynEvent},
        input_dev::{InputDevice, InputEvent},
    };
    use ostd::prelude::ktest;

    use super::{HidBootKeyboard, UsbBootKeyboardDevice, keyboard_linux_vectors};

    #[ktest]
    fn matches_linux_boot_keyboard_scenarios() {
        for scenario in keyboard_linux_vectors::LINUX_SCENARIOS {
            let mut keyboard = HidBootKeyboard::new();
            for (step_index, step) in scenario.steps.iter().enumerate() {
                let actual = keyboard
                    .decode(step.report)
                    .iter()
                    .map(InputEvent::to_raw)
                    .collect::<Vec<_>>();
                assert_eq!(
                    actual, step.events,
                    "Linux scenario {} step {}",
                    scenario.name, step_index
                );
            }
        }
    }

    #[ktest]
    fn advertises_only_boot_keyboard_keys() {
        let device = UsbBootKeyboardDevice::new(0, 0);
        let capability = device.capability();

        for key in [
            KeyCode::A,
            KeyCode::LeftCtrl,
            KeyCode::Key102nd,
            KeyCode::SysRq,
            KeyCode::Compose,
        ] {
            assert!(capability.support_key(key), "missing {key:?}");
        }
        for key in [
            KeyCode::Menu,
            KeyCode::Mute,
            KeyCode::VolumeDown,
            KeyCode::VolumeUp,
        ] {
            assert!(!capability.support_key(key), "unexpected {key:?}");
        }
    }

    #[ktest]
    fn emits_press_hold_and_release_as_linux_input_events() {
        let mut keyboard = HidBootKeyboard::new();
        let l_pressed = [0, 0, 0x0f, 0, 0, 0, 0, 0];

        assert_eq!(
            keyboard.decode(l_pressed),
            [
                InputEvent::from_key_and_status(KeyCode::L, KeyStatus::Pressed),
                InputEvent::from_sync_event(SynEvent::Report),
            ]
        );
        assert!(keyboard.decode(l_pressed).is_empty());
        assert_eq!(
            keyboard.decode([0; 8]),
            [
                InputEvent::from_key_and_status(KeyCode::L, KeyStatus::Released),
                InputEvent::from_sync_event(SynEvent::Report),
            ]
        );
    }

    #[ktest]
    fn emits_shift_before_the_shifted_key() {
        let mut keyboard = HidBootKeyboard::new();

        assert_eq!(
            keyboard.decode([0x02, 0, 0x04, 0, 0, 0, 0, 0]),
            [
                InputEvent::from_key_and_status(KeyCode::LeftShift, KeyStatus::Pressed),
                InputEvent::from_key_and_status(KeyCode::A, KeyStatus::Pressed),
                InputEvent::from_sync_event(SynEvent::Report),
            ]
        );
    }

    #[ktest]
    fn decodes_ctrl_c_press_and_release_sequence() {
        let mut keyboard = HidBootKeyboard::new();

        assert_eq!(
            keyboard.decode([0x01, 0, 0, 0, 0, 0, 0, 0]),
            [
                InputEvent::from_key_and_status(KeyCode::LeftCtrl, KeyStatus::Pressed),
                InputEvent::from_sync_event(SynEvent::Report),
            ]
        );
        assert_eq!(
            keyboard.decode([0x01, 0, 0x06, 0, 0, 0, 0, 0]),
            [
                InputEvent::from_key_and_status(KeyCode::C, KeyStatus::Pressed),
                InputEvent::from_sync_event(SynEvent::Report),
            ]
        );
        assert_eq!(
            keyboard.decode([0x01, 0, 0, 0, 0, 0, 0, 0]),
            [
                InputEvent::from_key_and_status(KeyCode::C, KeyStatus::Released),
                InputEvent::from_sync_event(SynEvent::Report),
            ]
        );
        assert_eq!(
            keyboard.decode([0; 8]),
            [
                InputEvent::from_key_and_status(KeyCode::LeftCtrl, KeyStatus::Released),
                InputEvent::from_sync_event(SynEvent::Report),
            ]
        );
    }

    #[ktest]
    fn rollover_preserves_ordinary_keys_but_updates_modifiers() {
        let mut keyboard = HidBootKeyboard::new();
        let a_pressed = [0, 0, 0x04, 0, 0, 0, 0, 0];
        keyboard.decode(a_pressed);

        assert_eq!(
            keyboard.decode([0x02, 0, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01]),
            [
                InputEvent::from_key_and_status(KeyCode::LeftShift, KeyStatus::Pressed),
                InputEvent::from_sync_event(SynEvent::Report),
            ]
        );
        assert_eq!(
            keyboard.decode(a_pressed),
            [
                InputEvent::from_key_and_status(KeyCode::LeftShift, KeyStatus::Released),
                InputEvent::from_sync_event(SynEvent::Report),
            ]
        );
        assert_eq!(
            keyboard.decode([0; 8]),
            [
                InputEvent::from_key_and_status(KeyCode::A, KeyStatus::Released),
                InputEvent::from_sync_event(SynEvent::Report),
            ]
        );
    }
}
