# M9 "real" bundle — the second profile generation. Two real, widely-used
# packages fetched from the Alpine riscv64 mirror as a *prebuilt closure* and
# staged under /m9/pkg/ by build_m9.sh:
#   - curl 8.21.0 (network client; its shared libs live in the base /usr/lib)
#   - jq   1.8.2  (JSON processor; libjq/libonig copied to the base /usr/lib)
#
# The builder copies the two binaries into $out/bin. This demonstrates a
# prebuilt closure of common software installed via `nix profile install`,
# forming a second profile generation over `core.nix`.
builtins.derivation {
  name = "m9-real";
  system = "riscv64-linux";
  builder = "/bin/sh";
  args = [ "-c" ''
    mkdir -p $out/bin
    cp /m9/pkg/curl $out/bin/curl
    cp /m9/pkg/jq   $out/bin/jq
    chmod 755 $out/bin/curl $out/bin/jq
  '' ];
  PATH = "/usr/bin:/bin:/usr/sbin:/sbin";
}
