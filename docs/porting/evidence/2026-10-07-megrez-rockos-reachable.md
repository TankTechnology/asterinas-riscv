# Megrez RockOS reachability check

Date: 2026-10-07 (Asia/Shanghai)

Status: **reachable over the RockOS network management path; serial output
silent**

This check records the state that must not be misclassified as a complete
board hang. No reset, U-Boot command, Asterinas boot, or filesystem write was
performed.

## Read-only observations

- The stable host serial path
  `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0` was present.
- A three-second passive serial read returned zero bytes, and no host process
  owned the serial device.
- `10.100.19.200` replied to one bounded ICMP probe (`0.226 ms`).
- TCP port 22 accepted a connection and returned the OpenSSH banner.
- An existing-key, strict-host-checking SSH session completed a read-only
  identity query:

  ```text
  uid=1000
  Linux rockos-eswin 6.6.87 ... riscv64 GNU/Linux
  PRETTY_NAME="Debian GNU/Linux trixie/sid"
  boot_id=872ba41f-9a4a-46ae-ad8f-2f13dd8795ce
  uptime=12:13
  ```

The SSH identity and Debian kernel identify the current system as RockOS. The
successful query proves that the board is executing and that the network
management path is usable; it does not prove that the serial console or an
Asterinas guest is healthy.

## Recovery interpretation

The earlier [RockOS control recovery](2026-09-22-rockos-control-recovery.md)
showed that an authenticated RockOS software reboot can return through a fresh
OpenSBI/U-Boot epoch and back to RockOS. That procedure remains available, but
was deliberately not run during this observation. Before any future transition
to Asterinas, attach the single serial owner, capture a fresh firmware prompt,
and verify a new RockOS boot identity after reboot.

The [2026-10-03 Basic hard-hang incident](2026-10-03-megrez-basic-console-hard-hang.md)
remains a historical unrecovered Asterinas incident. It must not be applied to
this observation: the current board has a live RockOS SSH service.
