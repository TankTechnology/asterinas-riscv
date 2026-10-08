"""Fixed serial interaction for the BusyBox controlling-terminal gate."""

from __future__ import annotations

from qemu_uboot_session import SerialInputStep, SerialInteraction


INTERACTION_NAME = "megrez-busybox-fixed-command"
LS_CD_INTERACTION_NAME = "megrez-busybox-ls-cd"
STANDARD_COMMANDS_INTERACTION_NAME = "megrez-busybox-standard-commands"
FULL_INITRAMFS_INTERACTION_NAME = "riscv-full-initramfs-standard-commands"
SERIAL_RELIABILITY_INTERACTION_NAME = "riscv-full-initramfs-serial-reliability"
READY_LINE = b"ASTERINAS_MEGREZ_SHELL_READY_20260721"
COMMAND_BYTES = b"printf 'ASTERINAS_MEGREZ_SHELL_ACK_20260721\\n'\n"
ACK_LINE = b"ASTERINAS_MEGREZ_SHELL_ACK_20260721"
LS_CD_COMMAND_BYTES = (
    b"printf 'ASTERINAS_MEGREZ_SHELL_ACK_20260721\\n'; "
    b"ls / && cd /tmp && [ \"$(pwd)\" = /tmp ] && "
    b"printf 'ASTERINAS_MEGREZ_LS_CD_ACK_20260721\\n'\n"
)
LS_CD_ACK_LINE = b"ASTERINAS_MEGREZ_LS_CD_ACK_20260721"
STANDARD_COMMANDS_BYTES = (
    b"ls / && cd /tmp && [ \"$(pwd)\" = /tmp ] && "
    b"mkdir asterinas-busybox-tree && "
    b"printf 'standard-busybox-tree\\n' > asterinas-busybox-tree/token && "
    b"[ \"$(cat asterinas-busybox-tree/token)\" = standard-busybox-tree ] && "
    b"rm asterinas-busybox-tree/token && "
    b"rmdir asterinas-busybox-tree && "
    b"printf 'ASTERINAS_MEGREZ_STANDARD_COMMANDS_ACK_20260721\\n'\n"
)
STANDARD_COMMANDS_ACK_LINE = b"ASTERINAS_MEGREZ_STANDARD_COMMANDS_ACK_20260721"
FULL_INITRAMFS_READY_LINE = b"Usage: /init <prog> [args...]"
FULL_INITRAMFS_INPUT_READY_TOKEN = b"~ # "
FULL_INITRAMFS_COMMAND_BYTES = (
    b"ls / && cd /tmp && [ \"$(pwd)\" = /tmp ] && "
    b"[ \"$(readlink /bin)\" = usr/bin ] && "
    b"[ -d /nix/store ] && "
    b"mkdir asterinas-full-initramfs && "
    b"printf 'full-initramfs\\n' > asterinas-full-initramfs/token && "
    b"[ \"$(cat asterinas-full-initramfs/token)\" = full-initramfs ] && "
    b"rm asterinas-full-initramfs/token && "
    b"rmdir asterinas-full-initramfs && "
    b"printf 'ASTERINAS_RISCV_FULL_INITRAMFS_ACK_20260721\\n'\n"
)
FULL_INITRAMFS_ACK_LINE = b"ASTERINAS_RISCV_FULL_INITRAMFS_ACK_20260721"
SERIAL_INTERRUPT_READY_LINE = b"ASTERINAS_RISCV_SERIAL_INTERRUPT_READY_20260722"
SERIAL_RELIABILITY_ACK_LINE = b"ASTERINAS_RISCV_SERIAL_RELIABILITY_ACK_20260722"
SERIAL_RELIABILITY_COMMAND_BYTES = (
    b"cd /tmx\x7fp && [ \"$(pwd)\" = /tmp ] && serial_value="
    + b"x" * 256
    + b" && [ \"${#serial_value}\" -eq 256 ] && "
    + b"printf 'ASTERINAS_RISCV_SERIAL_INTERRUPT_READY_20260722\\n' && "
    + b"sleep 30\n"
)
SERIAL_RELIABILITY_COMPLETION_BYTES = (
    b"printf 'ASTERINAS_RISCV_SERIAL_RELIABILITY_ACK_%s\\n' 20260722 "
    b"> /dev/ttyS0\n"
)


def interaction_by_name(name: str) -> SerialInteraction:
    if name == INTERACTION_NAME:
        return SerialInteraction(
            ready_line=READY_LINE,
            input_steps=(SerialInputStep(input_bytes=COMMAND_BYTES),),
            completion_line=ACK_LINE,
        )
    if name == LS_CD_INTERACTION_NAME:
        return SerialInteraction(
            ready_line=READY_LINE,
            input_steps=(SerialInputStep(input_bytes=LS_CD_COMMAND_BYTES),),
            completion_line=LS_CD_ACK_LINE,
        )
    if name == STANDARD_COMMANDS_INTERACTION_NAME:
        return SerialInteraction(
            ready_line=READY_LINE,
            input_steps=(SerialInputStep(input_bytes=STANDARD_COMMANDS_BYTES),),
            completion_line=STANDARD_COMMANDS_ACK_LINE,
        )
    if name == FULL_INITRAMFS_INTERACTION_NAME:
        return SerialInteraction(
            ready_line=FULL_INITRAMFS_READY_LINE,
            input_steps=(
                SerialInputStep(
                    input_bytes=FULL_INITRAMFS_COMMAND_BYTES,
                    ready_token=FULL_INITRAMFS_INPUT_READY_TOKEN,
                ),
            ),
            completion_line=FULL_INITRAMFS_ACK_LINE,
        )
    if name == SERIAL_RELIABILITY_INTERACTION_NAME:
        return SerialInteraction(
            ready_line=FULL_INITRAMFS_READY_LINE,
            input_steps=(
                SerialInputStep(
                    input_bytes=SERIAL_RELIABILITY_COMMAND_BYTES,
                    ready_token=FULL_INITRAMFS_INPUT_READY_TOKEN,
                ),
                SerialInputStep(
                    input_bytes=b"\x03",
                    ready_line=SERIAL_INTERRUPT_READY_LINE,
                ),
                SerialInputStep(
                    input_bytes=SERIAL_RELIABILITY_COMPLETION_BYTES,
                    ready_token=b"/tmp # ",
                ),
            ),
            completion_line=SERIAL_RELIABILITY_ACK_LINE,
        )
    raise ValueError(f"unknown serial interaction: {name}")
