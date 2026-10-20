# Megrez RockOS control recovery

## Result at 19:31 CST, 2026-09-22

The board is left running RockOS Linux 6.6.87.
Authenticated serial root access and existing-key SSH access both work.
One unattended software reboot returned to RockOS through the persistent menu.
Serial root access was then verified again after closing and reopening the host descriptor.
No further user reset or desktop terminal operation was required.

This supersedes the unavailable-control status in
[the earlier desktop investigation](2026-09-22-live-desktop-network-triage.md).
It does not establish Asterinas browser connectivity or Bilibili playback.

## Cause and scope

The serial listener was already running when the user reported the requested reset.
It captured fresh firmware, interrupted autoboot and selected RockOS.
Read-only U-Boot inspection established this persistent selection path:

```text
bootcmd=run bootcmd_asterinas || run bootcmd_rockos || run bootcmd_factory
bootcmd_asterinas=sysboot mmc 1:1 any 0x88200000 /extlinux/asterinas.conf
bootcmd_rockos=sysboot mmc 1:1 any 0x88200000 /extlinux/extlinux.conf
```

RockOS inspection found that `/boot/extlinux/asterinas.conf` was a single experimental entry,
not the previously qualified four-mode menu.
Its label was `asterinas-8ea7df5f62a3`,
with kernel `/asterinas-5444c9eb40e1.booti`, Stage1 `/stage1-5411f1acaac2.cpio`,
and arguments ending in `asterinas.reboot_after=900 -- --root-init=systemd`.
It had no debug-console opt-in.
This explains why the persistent selection did not recreate the intended management shell.
It is not evidence that a correct debug entry loses its shell merely because of reboot,
nor a diagnosis of the separate concurrency or Firefox problems.

## Applied repair

Restored the already-qualified candidate-2 menu using the existing promotion-script workflow.
The retained manifest and physical evidence are under:

```text
/home/ubuntu/.config/superpowers/worktrees/asterinas/megrez-boot-main/target/megrez-menu/candidate-2/manifest.json
/home/ubuntu/.config/superpowers/worktrees/asterinas/megrez-boot-main/target/megrez-menu/physical-v2
```

The generator accepted its retained physical qualification evidence.
The script verified the boot partition, vendor menu, candidate menu and all referenced Asterinas artifact hashes and sizes.
It backed up the old selector, atomically replaced the selector, synchronized storage and verified readback.

| Object | SHA-256 |
| --- | --- |
| Previous active selector, retained as backup | `5af16fb2040c1bc10ddb4d1044caa44b5f8f45c019703b139234eeb4c6ab5fcd` |
| Restored active selector | `02280720efe7a1ad0ac084cdc20429406631e12d2e16f05638544bab0883fb26` |
| Unchanged vendor `extlinux.conf` | `eb5f39a6e2db71ccc93ae005c488fd9f7ef353e75426e5a51e8923a9cbf2ebc5` |

Board backup:
`/boot/extlinux/asterinas-backup-5af16fb2040c1bc10ddb4d1044caa44b5f8f45c019703b139234eeb4c6ab5fcd.conf`.
The earlier short-hash backup was left untouched.
The uploaded audit script remains at
`/home/debian/.restore-menu-20260922-b2e407c388a1.sh`.

The restored menu defaults to RockOS (`l0`), with a ten-second menu timeout.
The existing firmware countdown is unchanged.
Basic, Probe and Desktop entries remain selectable.
Its Desktop entry explicitly enables `--debug-console=root --volatile-home`.
These are older qualified artifacts, not the new `gp` fix or newly patched Stage1.
No new Asterinas artifact was deployed or booted during this recovery.

No U-Boot environment was saved, no vendor menu was changed,
and no kernel, initrd, DTB or partition-2 filesystem was rewritten.
RockOS SSH was already enabled and active; no keys, passwords or SSH policy were changed.

## Live verification

The initial RockOS boot identity was
`ac580911-7f17-4bb1-bd78-eee072055a32`.
Serial reconnect returned the fresh marker `RECONNECT_6ab51d2098764cf6`,
UID 1000 and that same identity; authenticated sudo then established UID 0.
SSH independently returned the same boot identity.

From the authenticated root shell, `sync; systemctl reboot` was issued once.
The observer sent no U-Boot interruption or menu selection during this reboot.
The log records fresh OpenSBI/U-Boot, `Megrez operating system`,
automatic selection of RockOS and a fresh RockOS login.

| Post-reboot check | Observed result |
| --- | --- |
| Fresh serial login | `AFTER_REBOOT_2ef41ef904b1b826`, UID 1000, Linux 6.6.87 |
| Authenticated serial root | `CONTROL_51fbadf5891da417`, UID 0 |
| Serial descriptor closed and reopened | `CONTROL_5e7ceef9235c7bff`, UID 0 |
| Boot identity on all three checks | `6e0c2402-86cf-4fb6-99f4-385bbdc8543d`, different from the pre-reboot identity |
| Existing-key SSH with strict host verification | UID 1000, same post-reboot identity |
| SSH service after reboot | `enabled`, `active` |
| Ethernet | `end1` UP, `10.100.19.200/21`, gateway `10.100.16.1` |
| Selector, backup and vendor hashes over SSH | Match the identities above |

Nonce validation strips terminal escape sequences and splits carriage returns into lines,
then requires exactly one standalone response marker, rather than accepting command echo.
The first acquisition harness had falsely rejected a valid response because of its carriage-return handling.
That harness exit was not counted as a passed gate;
the subsequent fresh reconnect/root/reboot checks independently established access.

## Reuse and recovery

- UART: `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0`, 115200 baud.
  Use the existing `megrez_menu_board.attach` helper for single-owner locking.
  The final host descriptor is closed; the authenticated board-side root shell remains available.
  Its current prompt is `__ASTERINAS_ROCKOS_ROOT_PROMPT__ `.
  Observe and verify the current state before sending shell commands.
  Never send an empty line at U-Boot: it can repeat the previous command.
- SSH: `ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=5 debian@10.100.19.200`.
  Existing host credentials and known-host verification suffice.
  This is RockOS access, not an Asterinas SSH claim.
- Before a future transition, start the serial owner first and use authenticated software reboot.
  Fresh RockOS login uses the existing credential-input workflow;
  no credentials belong in evidence or command-line arguments.
- Keep RockOS as the persistent default while qualifying a new Asterinas generation separately.
  Do not restore the experimental backup merely to launch an experiment:
  it lacks the required debug-console flag.

## Evidence identity

Raw logs and the exact promotion script are retained locally under
`target/megrez-debug/rockos-control-20260922-_uybiihd/`.
This is a local `target` directory, not a claim that raw logs are committed.

| File | SHA-256 |
| --- | --- |
| `serial.log` | `cb2e6b40c9b25cc8785ab79ec6b76ac255f5569dbb126ecb1c67c19407cba2d6` |
| `reconnect-inventory.log` | `dc39b725c5a4c8a5c9994ab50edaeb0e710205121185d0717048ff448468d386` |
| `restore-menu.sh` | `b2e407c388a1c7ff009574bed7cd9708ee0f05fbb7411b233fc0d8b1c255a7d4` |
| `restore-and-software-reboot.log` | `4c139692df265f1bef8a0f71f2f61388dae7f0054648ecc224d007706c4618e6` |
| `root-after-reboot.log` | `77530b44e8c8bb4e630ec50179dcc91ca51cafde96ba12875511a1f1be7b181a` |
| `root-after-reconnect.log` | `3e5e233af78544e8b988de210b2bb1f5f66c1ed547fd2fda924f96d1299df554` |
| `ssh-after-reboot.log` | `a48f3756459726f0fa989caef81540ec9deb4f48c846b0c95477863719629ad4` |

The board remains in RockOS for controlled preparation.
Asterinas console-retention changes, the newer kernel, Firefox DNS and muted Bilibili playback still require their own hardware validation.
