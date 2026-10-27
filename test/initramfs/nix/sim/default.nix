{ lib, stdenv }:
stdenv.mkDerivation {
  pname = "asterinas-sim-probe";
  version = "0.1.0";
  src = ./probe.c;
  dontUnpack = true;
  buildPhase = ''
    runHook preBuild
    cp "$src" probe.c
    $CC -O2 -static -o sim-probe probe.c -lpthread
    runHook postBuild
  '';
  installPhase = ''
    runHook preInstall
    mkdir -p $out/bin
    install -m755 sim-probe $out/bin/sim-probe
    runHook postInstall
  '';
  meta = { description = "Asterinas RISC-V QEMU capability probe"; };
}
