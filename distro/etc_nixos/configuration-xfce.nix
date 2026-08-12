# AsterNixOS configuration with the X11 (X.Org) XFCE desktop enabled.
#
# This file mirrors configuration.nix but turns the XFCE desktop on. It
# must NOT import ./configuration.nix: the installer renames the chosen
# config file to configuration.nix inside the target image, so such an
# import would become a self-reference and recurse infinitely.
#
# The desktop renders through the generic fbdev X driver
# (xf86-video-fbdev on /dev/fb0), so it works on any platform where the
# kernel hands over a firmware framebuffer — including RISC-V with the
# simple-framebuffer DT handoff. Build with:
#
#   ./tools/nixos/build_nixos.sh configuration-xfce.nix

{ config, lib, pkgs, ... }: {
  # Do not change these imports, which describe system-wide settings for AsterNixOS.
  imports = [ ./aster_configuration.nix ];

  networking.hostName = "asterinas"; # Define your hostname.
  # The DNS server.
  environment.etc."resolv.conf".text = ''
    nameserver 8.8.8.8
  '';

  # Enable the X11 (X.Org) desktop (XFCE).
  services.xserver.enable = true;
  services.xserver.desktopManager.xfce.enable = true;

  # List packages installed in system profile.
  # You can use https://search.nixos.org/ to find more packages (and options).
  environment.systemPackages = with pkgs; [ hello-asterinas ];

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
