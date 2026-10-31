{
  lib,
  stdenvNoCC,
  pkgs,
  conformanceSrc,
}:

let
  xfstests = pkgs.xfstests.overrideAttrs (old: rec {
    version = "2026.06.21";
    src = pkgs.fetchzip {
      url = "https://git.kernel.org/pub/scm/fs/xfs/xfstests-dev.git/snapshot/xfstests-dev-v${version}.tar.gz";
      hash = "sha256-hngS9Hnsz9XKQ42yh6mcXHiTOzL+Zk9hRpai7e2tU0E=";
    };
    nativeBuildInputs = (old.nativeBuildInputs or [ ]) ++ [ pkgs.pkg-config ];
  });

  standaloneCoreutils = pkgs.coreutils.override { singleBinary = false; };

  runtimeDeps = with pkgs; [
    standaloneCoreutils
    perl
    bash
    gnugrep
    gnused
    gawk
    coreutils
    glibc.bin
    findutils
    util-linux
    bc
    kmod
    xfsprogs
    e2fsprogs
    attr
  ];

  sbinDeps = with pkgs; [
    util-linux
    kmod
    xfsprogs
    e2fsprogs
  ];

  runtimePath =
    lib.makeBinPath runtimeDeps
    + ":"
    + lib.concatMapStringsSep ":" (package: "${package}/sbin") sbinDeps
    + ":/bin:/usr/bin:/sbin:/usr/sbin";

in
stdenvNoCC.mkDerivation {
  name = "xfstests";

  buildCommand = ''
    mkdir -p $out/xfstests

    cp ${conformanceSrc}/xfstests/run_xfstests.sh $out/xfstests/
    sed -i "s|__RUNTIME_PATH__|${runtimePath}|" $out/xfstests/run_xfstests.sh
    chmod +x $out/xfstests/run_xfstests.sh
    for fs_dir in ${conformanceSrc}/xfstests/*; do
      if [ -d "$fs_dir" ]; then
        cp -r "$fs_dir" $out/xfstests/
      fi
    done

    # The links keep the compiled suite alive during GC
    ln -s ${xfstests}/lib/xfstests/* $out/xfstests/

    # The cross build's patchShebangs step leaves a few scripts pointing at
    # the build-host bash.  Replace those files (and the directory symlinks
    # that contain them) with target-bash versions; the remaining entries
    # stay symlinked so the compiled suite and its closure are preserved.
    for dir in src ltp; do
      if [ -L "$out/xfstests/$dir" ]; then
        real_dir=$(readlink -f "$out/xfstests/$dir")
        rm "$out/xfstests/$dir"
        mkdir "$out/xfstests/$dir"
        for entry in "$real_dir"/*; do
          ln -s "$entry" "$out/xfstests/$dir/$(basename "$entry")"
        done
      fi
    done
    for rel in check src/dmerror ltp/rwtest.sh; do
      target="$out/xfstests/$rel"
      real=$(readlink -f "$target")
      rm "$target"
      cp "$real" "$target"
      sed -i "1s|^#!.*|#!${pkgs.bash}/bin/bash|" "$target"
      chmod +x "$target"
    done
  '';
}
