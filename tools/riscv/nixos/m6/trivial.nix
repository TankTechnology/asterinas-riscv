# M6 minimal validation: a derivation whose builder is the system shell and
# whose only action is to write a fixed string to $out. No compiler, no
# nixpkgs, no sandbox — it exercises the full "realise a derivation into
# /nix/store" path (drv instantiation, store-path hashing, forking the builder,
# atomic rename of the output).
builtins.derivation {
  name = "m6-trivial";
  system = "riscv64-linux";
  builder = "/bin/sh";
  args = [ "-c" "echo -n hello-from-nix-store > $out" ];
}
