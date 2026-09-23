{ lib, stdenvNoCC, callPackage, testSuite ? "ltp", gvisorTest ? null
, workDir ? "/tmp", smp ? 1, }: rec {
  inherit testSuite;
  ltp = callPackage ./ltp.nix { };
  # FIXME: Build gvisor syscall test with nix.
  gvisor = if gvisorTest == null then
    builtins.path {
      name = "gvisor-prebuilt";
      path = builtins.getEnv "GVISOR_PREBUILT_DIR";
    }
  else
    assert gvisorTest == "ioctl_test";
    stdenvNoCC.mkDerivation {
      pname = "gvisor-focused-prebuilt";
      version = "0.1.0";
      dontUnpack = true;
      buildCommand = ''
        mkdir -p $out
        cp ${
          builtins.path {
            name = gvisorTest;
            path = "${builtins.getEnv "GVISOR_PREBUILT_DIR"}/${gvisorTest}";
          }
        } $out/${gvisorTest}
      '';
    };
  kselftest = callPackage ./kselftest.nix { };

  conformanceSrc = lib.fileset.toSource {
    root = ./../../src/conformance;
    fileset = ./../../src/conformance;
  };
  xfstests = callPackage ./xfstests.nix { inherit conformanceSrc; };

  package = stdenvNoCC.mkDerivation {
    pname = "conformance";
    version = "0.1.0";
    src = conformanceSrc;
    buildCommand = ''
      cd $src
      mkdir -p $out
      export INITRAMFS=$out
      export CONFORMANCE_TEST_SUITE=${testSuite}
      export CONFORMANCE_TEST_WORKDIR=${workDir}
      export SMP=${toString smp}
      ${lib.optionalString (testSuite == "ltp")
      "export LTP_PREBUILT_DIR=${ltp}"}
      ${lib.optionalString (testSuite == "gvisor")
      "export GVISOR_PREBUILT_DIR=${gvisor}"}
      ${lib.optionalString (testSuite == "gvisor" && gvisorTest != null)
      "export GVISOR_TESTS=${gvisorTest}"}
      ${lib.optionalString (testSuite == "kselftest")
      "export KSELFTEST_PREBUILT_DIR=${kselftest}"}
      ${lib.optionalString (testSuite == "xfstests")
      "export XFSTESTS_PREBUILT_DIR=${xfstests}"}
      make
    '';
  };
}
