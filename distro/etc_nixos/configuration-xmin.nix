# AsterNixOS configuration with a minimal X11 graphics stack.
#
# Goal: the smallest off-the-shelf path to "graphical app on screen" —
# Xorg (fbdev on /dev/fb0) + twm + xterm + the Mednafen multi-system
# emulator (NES included). No desktop environment. Like
# configuration-xfce.nix, this file must NOT import ./configuration.nix:
# the installer renames the chosen config to configuration.nix inside the
# target image, which would make such an import infinitely recursive.
#
# Build with:
#
#   ./tools/nixos/build_nixos.sh configuration-xmin.nix
#
# After boot, log in on the serial console and run `start_xmin`, or let
# the xmin-desktop systemd service bring X up on tty1 automatically.
# Launch a game with: mednafen /path/to/rom.nes

{ config, lib, pkgs, ... }:
let
  startXmin = pkgs.writeScriptBin "start_xmin" ''
    #!/bin/sh
    source /etc/profile

    # Step 1: run dbus
    mkdir -p /var/lib/dbus /usr/share/X11/xorg.conf.d
    [ -f /var/lib/dbus/machine-id ] || dbus-uuidgen --ensure=/var/lib/dbus/machine-id
    if command -v dbus-launch >/dev/null 2>&1; then
      eval "$(dbus-launch --sh-syntax)"
    fi

    # Step 2: run Xorg (fbdev + evdev, see the xorgserver overlay's
    # 10-fbdev.conf for the ServerLayout this references)
    XKB_DATA="/run/current-system/sw/share/X11/xkb"
    MODULE_PATH="/run/current-system/sw/lib/xorg/modules"
    nohup Xorg :0 vt1 \
      -modulepath "$MODULE_PATH" \
      -xkbdir "$XKB_DATA" \
      -logverbose 0 \
      -logfile /var/log/xorg_debug.log \
      -novtswitch \
      -keeptty \
      -keyboard keyboard \
      -pointer mouse0 \
      > /var/log/xorg.log 2>&1 &

    # Step 3: a minimal window manager and a terminal
    export DISPLAY=:0
    nohup twm > /var/log/twm.log 2>&1 &
    nohup xterm > /var/log/xterm.log 2>&1 &
  '';
in {
  # Do not change these imports, which describe system-wide settings for AsterNixOS.
  imports = [ ./aster_configuration.nix ];

  networking.hostName = "asterinas"; # Define your hostname.
  # The DNS server.
  environment.etc."resolv.conf".text = ''
    nameserver 8.8.8.8
  '';

  services.xserver.enable = true;

  environment.systemPackages = with pkgs; [
    hello-asterinas
    xorg.xf86videofbdev # the fbdev X driver, rendering on /dev/fb0
    xorg.twm # minimal window manager
    xterm
    mednafen # off-the-shelf multi-system emulator (NES for Super Mario)
    startXmin
  ];

  # Bring X up on tty1 automatically; conflicts with the getty there.
  systemd.services."xmin-desktop" = {
    description = "Minimal X Desktop (Xorg + twm)";
    wantedBy = [ "multi-user.target" ];
    conflicts = [ "getty@tty1.service" ];
    serviceConfig = {
      Environment = "DISPLAY=:0";
      ExecStart = "${startXmin}/bin/start_xmin";
      StandardOutput = "tty";
      StandardError = "tty";
      KillMode = "process";
      Delegate = "yes";
      Restart = "no";
      Type = "simple";
    };
  };

  system.nixos.distroName = "Asterinas NixOS";

  # This option defines the first version of NixOS you have installed on this particular machine,
  # and is used to maintain compatibility with application data (e.g. databases) created on older NixOS versions.
  #
  # Most users should NEVER change this value after the initial install, for any reason,
  # even if you've upgraded your system to a new NixOS release.
  #
  # This value does NOT affect the Nixpkgs version your packages and OS are pulled from,
  # so changing it will NOT upgrade your system - see https://nixos.org/manual/nixos/stable/#sec-upgrading for how
  # to actually do that.
  #
  # This value being lower than the current NixOS release does NOT mean your system is
  # out of date, out of support, or vulnerable.
  #
  # Do NOT change this value unless you have manually inspected all the changes it would make to your configuration,
  # and migrated your data accordingly.
  #
  # For more information, see `man configuration.nix` or https://nixos.org/manual/nixos/stable/options#opt-system.stateVersion .
  system.stateVersion = "25.05"; # Did you read the comment?
}
