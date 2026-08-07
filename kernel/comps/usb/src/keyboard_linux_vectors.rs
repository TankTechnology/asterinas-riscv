// SPDX-License-Identifier: MPL-2.0
// @generated; do not edit.
// Generation command: python3 tools/usb-hid/boot_keyboard_oracle.py
// Linux kernel: 6.5.0-15-generic
// hid-tools: 0.12
// evdev: 1.9.3
// USB HID: 1.11
// HID Usage Tables: 1.7
// Descriptor SHA256: 14bdd69b3b46b4e8a093865c10c75b6a9aaf85f7986f146d87a437e7f7afa476
// Scenarios SHA256: 860fe07554c599719d491560ce91a9162db48d731babc4c80a197bb72fd82d13

pub(super) struct LinuxStep {
    pub(super) report: [u8; 8],
    pub(super) events: &'static [(u16, u16, i32)],
}

pub(super) struct LinuxScenario {
    pub(super) name: &'static str,
    pub(super) steps: &'static [LinuxStep],
}

pub(super) static LINUX_SCENARIOS: &[LinuxScenario] = &[
    LinuxScenario {
        name: "usage_04",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 30, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 30, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_05",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x05, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 48, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x05, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 48, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_06",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x06, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 46, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x06, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 46, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_07",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x07, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 32, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x07, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 32, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_08",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x08, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 18, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x08, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 18, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_09",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x09, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 33, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x09, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 33, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_0a",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x0a, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 34, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x0a, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 34, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_0b",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x0b, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 35, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x0b, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 35, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_0c",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x0c, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 23, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x0c, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 23, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_0d",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x0d, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 36, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x0d, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 36, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_0e",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x0e, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 37, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x0e, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 37, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_0f",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x0f, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 38, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x0f, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 38, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_10",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x10, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 50, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x10, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 50, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_11",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x11, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 49, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x11, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 49, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_12",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x12, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 24, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x12, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 24, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_13",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x13, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 25, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x13, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 25, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_14",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x14, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 16, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x14, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 16, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_15",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x15, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 19, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x15, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 19, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_16",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x16, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 31, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x16, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 31, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_17",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x17, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 20, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x17, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 20, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_18",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x18, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 22, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x18, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 22, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_19",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x19, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 47, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x19, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 47, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_1a",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x1a, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 17, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x1a, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 17, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_1b",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x1b, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 45, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x1b, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 45, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_1c",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x1c, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 21, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x1c, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 21, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_1d",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x1d, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 44, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x1d, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 44, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_1e",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x1e, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 2, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x1e, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 2, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_1f",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x1f, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 3, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x1f, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 3, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_20",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x20, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 4, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x20, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 4, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_21",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x21, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 5, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x21, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 5, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_22",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x22, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 6, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x22, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 6, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_23",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x23, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 7, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x23, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 7, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_24",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x24, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 8, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x24, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 8, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_25",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x25, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 9, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x25, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 9, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_26",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x26, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 10, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x26, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 10, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_27",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x27, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 11, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x27, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 11, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_28",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x28, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 28, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x28, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 28, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_29",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x29, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 1, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x29, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 1, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_2a",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x2a, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 14, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x2a, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 14, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_2b",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x2b, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 15, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x2b, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 15, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_2c",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x2c, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 57, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x2c, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 57, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_2d",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x2d, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 12, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x2d, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 12, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_2e",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x2e, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 13, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x2e, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 13, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_2f",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x2f, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 26, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x2f, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 26, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_30",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x30, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 27, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x30, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 27, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_31",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x31, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 43, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x31, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 43, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_32",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x32, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 43, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x32, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 43, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_33",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x33, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 39, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x33, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 39, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_34",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x34, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 40, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x34, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 40, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_35",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x35, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 41, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x35, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 41, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_36",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x36, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 51, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x36, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 51, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_37",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x37, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 52, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x37, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 52, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_38",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x38, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 53, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x38, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 53, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_39",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x39, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 58, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x39, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 58, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_3a",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x3a, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 59, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x3a, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 59, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_3b",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x3b, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 60, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x3b, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 60, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_3c",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x3c, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 61, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x3c, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 61, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_3d",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x3d, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 62, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x3d, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 62, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_3e",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x3e, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 63, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x3e, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 63, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_3f",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x3f, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 64, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x3f, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 64, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_40",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x40, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 65, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x40, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 65, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_41",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x41, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 66, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x41, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 66, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_42",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x42, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 67, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x42, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 67, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_43",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x43, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 68, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x43, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 68, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_44",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x44, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 87, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x44, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 87, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_45",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x45, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 88, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x45, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 88, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_46",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x46, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 99, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x46, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 99, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_47",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x47, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 70, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x47, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 70, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_48",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x48, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 119, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x48, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 119, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_49",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x49, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 110, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x49, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 110, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_4a",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x4a, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 102, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x4a, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 102, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_4b",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x4b, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 104, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x4b, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 104, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_4c",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x4c, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 111, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x4c, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 111, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_4d",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x4d, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 107, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x4d, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 107, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_4e",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x4e, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 109, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x4e, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 109, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_4f",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x4f, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 106, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x4f, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 106, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_50",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x50, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 105, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x50, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 105, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_51",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x51, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 108, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x51, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 108, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_52",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x52, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 103, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x52, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 103, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_53",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x53, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 69, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x53, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 69, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_54",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x54, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 98, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x54, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 98, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_55",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x55, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 55, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x55, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 55, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_56",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x56, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 74, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x56, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 74, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_57",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x57, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 78, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x57, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 78, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_58",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x58, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 96, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x58, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 96, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_59",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x59, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 79, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x59, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 79, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_5a",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x5a, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 80, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x5a, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 80, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_5b",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x5b, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 81, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x5b, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 81, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_5c",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x5c, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 75, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x5c, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 75, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_5d",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x5d, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 76, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x5d, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 76, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_5e",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x5e, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 77, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x5e, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 77, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_5f",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x5f, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 71, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x5f, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 71, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_60",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x60, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 72, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x60, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 72, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_61",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x61, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 73, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x61, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 73, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_62",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x62, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 82, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x62, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 82, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_63",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x63, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 83, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x63, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 83, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_64",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x64, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 86, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x64, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 86, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "usage_65",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x65, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 127, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x65, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 127, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "modifier_0",
        steps: &[
            LinuxStep {
                report: [0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 29, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 29, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "modifier_1",
        steps: &[
            LinuxStep {
                report: [0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 42, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 42, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "modifier_2",
        steps: &[
            LinuxStep {
                report: [0x04, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 56, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x04, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 56, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "modifier_3",
        steps: &[
            LinuxStep {
                report: [0x08, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 125, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x08, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 125, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "modifier_4",
        steps: &[
            LinuxStep {
                report: [0x10, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 97, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x10, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 97, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "modifier_5",
        steps: &[
            LinuxStep {
                report: [0x20, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 54, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x20, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 54, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "modifier_6",
        steps: &[
            LinuxStep {
                report: [0x40, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 100, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x40, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 100, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "modifier_7",
        steps: &[
            LinuxStep {
                report: [0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 126, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 126, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "all_modifiers",
        steps: &[
            LinuxStep {
                report: [0xff, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[
                    (1, 29, 1),
                    (1, 42, 1),
                    (1, 56, 1),
                    (1, 125, 1),
                    (1, 97, 1),
                    (1, 54, 1),
                    (1, 100, 1),
                    (1, 126, 1),
                    (1, 30, 1),
                    (0, 0, 0),
                ],
            },
            LinuxStep {
                report: [0xff, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[
                    (1, 29, 0),
                    (1, 42, 0),
                    (1, 56, 0),
                    (1, 125, 0),
                    (1, 97, 0),
                    (1, 54, 0),
                    (1, 100, 0),
                    (1, 126, 0),
                    (1, 30, 0),
                    (0, 0, 0),
                ],
            },
        ],
    },
    LinuxScenario {
        name: "chord_2",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x05, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 30, 1), (1, 48, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x05, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 30, 0), (1, 48, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "chord_3",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x05, 0x06, 0x00, 0x00, 0x00],
                events: &[(1, 30, 1), (1, 48, 1), (1, 46, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x05, 0x06, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 30, 0), (1, 48, 0), (1, 46, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "chord_4",
        steps: &[
            LinuxStep {
                report: [0x01, 0x00, 0x04, 0x05, 0x06, 0x07, 0x00, 0x00],
                events: &[
                    (1, 29, 1),
                    (1, 30, 1),
                    (1, 48, 1),
                    (1, 46, 1),
                    (1, 32, 1),
                    (0, 0, 0),
                ],
            },
            LinuxStep {
                report: [0x01, 0x00, 0x04, 0x05, 0x06, 0x07, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[
                    (1, 29, 0),
                    (1, 30, 0),
                    (1, 48, 0),
                    (1, 46, 0),
                    (1, 32, 0),
                    (0, 0, 0),
                ],
            },
        ],
    },
    LinuxScenario {
        name: "chord_5",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x05, 0x06, 0x07, 0x08, 0x00],
                events: &[
                    (1, 30, 1),
                    (1, 48, 1),
                    (1, 46, 1),
                    (1, 32, 1),
                    (1, 18, 1),
                    (0, 0, 0),
                ],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x05, 0x06, 0x07, 0x08, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[
                    (1, 30, 0),
                    (1, 48, 0),
                    (1, 46, 0),
                    (1, 32, 0),
                    (1, 18, 0),
                    (0, 0, 0),
                ],
            },
        ],
    },
    LinuxScenario {
        name: "chord_6",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09],
                events: &[
                    (1, 30, 1),
                    (1, 48, 1),
                    (1, 46, 1),
                    (1, 32, 1),
                    (1, 18, 1),
                    (1, 33, 1),
                    (0, 0, 0),
                ],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[
                    (1, 30, 0),
                    (1, 48, 0),
                    (1, 46, 0),
                    (1, 32, 0),
                    (1, 18, 0),
                    (1, 33, 0),
                    (0, 0, 0),
                ],
            },
        ],
    },
    LinuxScenario {
        name: "zero_usage",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
        ],
    },
    LinuxScenario {
        name: "add_remove_modifier",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 30, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x02, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 42, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 42, 0), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 30, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "simultaneous_modifier_release",
        steps: &[
            LinuxStep {
                report: [0xff, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[
                    (1, 29, 1),
                    (1, 42, 1),
                    (1, 56, 1),
                    (1, 125, 1),
                    (1, 97, 1),
                    (1, 54, 1),
                    (1, 100, 1),
                    (1, 126, 1),
                    (1, 30, 1),
                    (0, 0, 0),
                ],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[
                    (1, 29, 0),
                    (1, 42, 0),
                    (1, 56, 0),
                    (1, 125, 0),
                    (1, 97, 0),
                    (1, 54, 0),
                    (1, 100, 0),
                    (1, 126, 0),
                    (1, 30, 0),
                    (0, 0, 0),
                ],
            },
        ],
    },
    LinuxScenario {
        name: "shift_a",
        steps: &[
            LinuxStep {
                report: [0x02, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 42, 1), (1, 30, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 42, 0), (1, 30, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "ctrl_alt_delete",
        steps: &[
            LinuxStep {
                report: [0x05, 0x00, 0x4c, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 29, 1), (1, 56, 1), (1, 111, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 29, 0), (1, 56, 0), (1, 111, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "six_key_partial_release",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09],
                events: &[
                    (1, 30, 1),
                    (1, 48, 1),
                    (1, 46, 1),
                    (1, 32, 1),
                    (1, 18, 1),
                    (1, 33, 1),
                    (0, 0, 0),
                ],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x06, 0x08, 0x00, 0x00, 0x00],
                events: &[(1, 48, 0), (1, 32, 0), (1, 33, 0), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 30, 0), (1, 46, 0), (1, 18, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "add_to_chord",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x05, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 30, 1), (1, 48, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x05, 0x06, 0x00, 0x00, 0x00],
                events: &[(1, 46, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 30, 0), (1, 48, 0), (1, 46, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "release_one_from_chord",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x05, 0x06, 0x00, 0x00, 0x00],
                events: &[(1, 30, 1), (1, 48, 1), (1, 46, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x06, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 48, 0), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 30, 0), (1, 46, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "replace_subset",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x05, 0x06, 0x00, 0x00, 0x00],
                events: &[(1, 30, 1), (1, 48, 1), (1, 46, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x01, 0x00, 0x04, 0x07, 0x08, 0x00, 0x00, 0x00],
                events: &[
                    (1, 29, 1),
                    (1, 48, 0),
                    (1, 32, 1),
                    (1, 46, 0),
                    (1, 18, 1),
                    (0, 0, 0),
                ],
            },
            LinuxStep {
                report: [0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 30, 0), (1, 32, 0), (1, 18, 0), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 29, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "reordered_array",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x05, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 30, 1), (1, 48, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x05, 0x04, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 48, 0), (1, 30, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "duplicate_usage",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x04, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 30, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 30, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "zero_filled_holes",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x00, 0x05, 0x00, 0x00, 0x00],
                events: &[(1, 30, 1), (1, 48, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 30, 0), (1, 48, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "replace_a_with_b",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 30, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x05, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 30, 0), (1, 48, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 48, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "backslash_alias_keycode_state",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x31, 0x32, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 43, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x31, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 43, 0), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x31, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 43, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x31, 0x32, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x32, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 43, 0), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x31, 0x32, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 43, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x32, 0x31, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 43, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "reserved_byte_change",
        steps: &[
            LinuxStep {
                report: [0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 29, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x01, 0x7f, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 29, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "omitted_intermediate_report",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 30, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x06, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 30, 0), (1, 46, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 46, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "unsupported_66",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x66, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
        ],
    },
    LinuxScenario {
        name: "unsupported_ff",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0xff, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
        ],
    },
    LinuxScenario {
        name: "error_1_empty",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
        ],
    },
    LinuxScenario {
        name: "error_1_held",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 30, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x05, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 30, 0), (1, 48, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 48, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "error_1_modifier",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 30, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x02, 0x00, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01],
                events: &[(1, 42, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 42, 0), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 30, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "error_2_empty",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x02, 0x02, 0x02, 0x02, 0x02, 0x02],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
        ],
    },
    LinuxScenario {
        name: "error_2_held",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 30, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x02, 0x02, 0x02, 0x02, 0x02, 0x02],
                events: &[(1, 30, 0), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x05, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 48, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 48, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "error_2_modifier",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 30, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x02, 0x00, 0x02, 0x02, 0x02, 0x02, 0x02, 0x02],
                events: &[(1, 42, 1), (1, 30, 0), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 42, 0), (1, 30, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 30, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "error_3_empty",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x03, 0x03, 0x03, 0x03, 0x03, 0x03],
                events: &[],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[],
            },
        ],
    },
    LinuxScenario {
        name: "error_3_held",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 30, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x03, 0x03, 0x03, 0x03, 0x03, 0x03],
                events: &[(1, 30, 0), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x05, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 48, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 48, 0), (0, 0, 0)],
            },
        ],
    },
    LinuxScenario {
        name: "error_3_modifier",
        steps: &[
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 30, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x02, 0x00, 0x03, 0x03, 0x03, 0x03, 0x03, 0x03],
                events: &[(1, 42, 1), (1, 30, 0), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 42, 0), (1, 30, 1), (0, 0, 0)],
            },
            LinuxStep {
                report: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                events: &[(1, 30, 0), (0, 0, 0)],
            },
        ],
    },
];
