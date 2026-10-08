# M9 "core" bundle — the first profile generation. Four prebuilt riscv64 musl
# binaries (cross-compiled on the host by build_m9.sh, staged at
# /m9/prebuilt/<name>) are copied into $out/bin by a /bin/sh builder. This is
# path B from M6/M8: the derivation is realised in-guest by the riscv64 nix,
# but the "build" is a file copy, not a compile (in-guest gcc is still blocked
# by the ET_EXEC + PT_INTERP loader gap).
builtins.derivation {
  name = "m9-core";
  system = "riscv64-linux";
  builder = "/bin/sh";
  args = [ "-c" ''
    mkdir -p $out/bin
    for t in hello nixos-info fortune heartbeat; do
      cp /m9/prebuilt/$t $out/bin/$t
      chmod 755 $out/bin/$t
    done
  '' ];
  PATH = "/usr/bin:/bin:/usr/sbin:/sbin";
}
