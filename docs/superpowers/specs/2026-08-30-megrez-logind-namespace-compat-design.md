# Megrez Debian logind Namespace Compatibility Design

## Goal

Boot the signed Debian desktop root through Asterinas on Milk-V Megrez and
reach the existing M6 NetSurf/Baidu gate without waiting for unsupported
systemd mount-namespace isolation.

## Evidence and boundary

The protected physical run reaches Debian systemd, GMAC, MMC, xHCI, and USB
input, then repeatedly starts `(sd-mkuserns)` while reporting unsupported
`CLONE_NEWUSER`, `open_tree` (428), and `kcmp` (272).  The same immutable
kernel and a fresh copy of the same root image pass the QEMU M6 gate.  Upstream
systemd 257 classifies `ReadWritePaths=`, `PrivateTmp=`, `ProtectSystem=`,
`ProtectHome=`, the kernel-protection settings, and `ProtectControlGroups=` as
mount-namespace triggers.  Asterinas intentionally does not yet provide a
complete user-namespace capability and UID/GID-mapping model.

This change will not implement partial user namespaces, bypass Asterinas,
replace systemd-logind, or weaken unrelated services.

## Chosen approach

The signed Debian desktop profiles will install one Asterinas-specific
`systemd-logind.service` drop-in.  It resets only the settings from Debian's
vendor unit that require a private mount namespace:

- `PrivateTmp=no`;
- `ProtectControlGroups=no`;
- `ProtectHome=no`;
- `ProtectKernelLogs=no`;
- `ProtectKernelModules=no`;
- `ProtectSystem=no`;
- an empty `ReadWritePaths=` reset.

Other logind hardening, including its capability bound, no-new-privileges,
address-family restriction, syscall policy, device policy, and service user
model, remains unchanged.  The override is a compatibility measure for this
trusted system service, not a claim that Asterinas implements Linux namespace
isolation.

## Verification flow

1. A focused rootfs-generation test requires the exact drop-in, mode `0644`,
   and absence of unrelated overrides.
2. Build a fresh signed desktop root and validate its manifest, package lock,
   ext2 identity, and embedded unit.
3. Run the existing QEMU M6 desktop/Baidu gate.  It must emit the ordered
   udev, logind, input, Xorg, desktop, remote Baidu asset, JavaScript-status,
   and browser-ready markers without `(sd-mkuserns)` retry storms.
4. Install that exact root into Megrez partition 2 through Asterinas's bounded
   MMC/LAN workflow, then run one plan-bound protected board boot.  Success is
   the physical M6 marker sequence plus software return to U-Boot; failure
   leaves the previous published artifacts intact and the board recoverable.

## Deferred work

Complete `CLONE_NEWUSER`, UID/GID maps, namespaced capabilities, mount
namespaces, `open_tree`, and `kcmp` remain a separate kernel-compatibility
milestone.  They are valuable for general Debian sandbox fidelity but are not
required to prove the basic desktop/browser experience.
