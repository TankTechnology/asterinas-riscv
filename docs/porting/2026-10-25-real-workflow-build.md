# 真实工作流：guest 内编译并测试 zlib 1.3.1

日期：2026-10-25　HEAD：`22e7aa049`
内核：`target/prime/kernels/x86.elf`（sha256 前缀 `9e8ee5dd544fa03a`）
镜像：`target/debian-x86-toolchain3/rootfs/debian-root.ext2`
（`desktop-drm-toolchain`，2.0 GiB / 409 包，含 gcc 14.2.0 + make 4.4.1）

## 结论摘要

- **判定：编译 + 测试通过**。
- zlib **1.3.1**：`configure` rc=0、`make -j2` rc=0、**`make test` rc=0**，
  测试输出 `*** zlib test OK ***`、`*** zlib shared test OK ***`、
  `*** zlib 64-bit test OK ***`，**OK 3 项 / FAILED 0 项**。
- 额外并发编译 `make -j4` 也 **rc=0**。
- 产物 `libz.so.1.3.1` 为 **ELF64 / DYN（共享库）**，`ldd` 依赖
  `linux-vdso.so.1`、`libc.so.6`、`ld-linux-x86-64.so.2`，动态链接正常。
- 过程中无 panic / OOM / `allocation failed`。

## 1. 源码获取方式

guest 内的 apt 只有二进制源（且 builder 会清掉 package lists），因此没有在
guest 里 `apt-get source`。采用的路径：

1. 宿主下载 `https://zlib.net/fossils/zlib-1.3.1.tar.gz`（备用 GitHub
   release），1.5 MB；
2. 记录 sha256：
   `9a93b2b7dfdac77ceba5a558a580e74667dd6fede4585b91eefb60f03b72df23`；
3. 用 `debugfs -w -R "write … /root/zlib-1.3.1.tar.gz"` 把 tarball 注入
   `desktop-drm-toolchain` 镜像的 `/root/`（不改 profile、不依赖 guest 网络）；
4. guest 侧脚本 `tools/x86/debian/zlib_workflow.sh` 解包、编译、测试。

镜像本身仍是上一步的 409 包 toolchain 镜像，只多了一个测试输入 tarball。

## 2. 编译关键输出

```
zlib_version=1.3.1
configure_rc=0
Checking for stdarg.h... Yes.
Checking whether to use vs[n]printf() or s[n]printf()... using vs[n]printf().
Checking for return value of vsnprintf()... Yes.
Checking for attribute(visibility) support... Yes.
make_rc=0
gcc -shared -Wl,-soname,libz.so.1,--version-script,zlib.map -O3 -fPIC \
    -D_LARGEFILE64_SOURCE=1 -DHAVE_HIDDEN -o libz.so.1.3.1 adler32.lo crc32.lo \
    deflate.lo infback.lo inffast.lo inflate.lo inftrees.lo trees.lo zutil.lo \
    compress.lo uncompr.lo gzclose.lo gzlib.lo gzread.lo gzwrite.lo -lc
```

## 3. `make test` 原始结果

```
make_test_rc=0
hello world
zlib version 1.3.1 = 0x1310, compile flags = 0xa9
...
large_inflate(): OK
...
		*** zlib test OK ***
		*** zlib shared test OK ***
		*** zlib 64-bit test OK ***
test_ok_lines=3
test_failed_lines=0
```

通过 3 项（静态、共享、64-bit），失败 0 项；退出码 0。

## 4. 产物检查

```
ELF Header:
  Class:  ELF64
  Type:   DYN (Shared object file)
	linux-vdso.so.1 (0x00007ffffefc6000)
	libc.so.6 => /lib/x86_64-linux-gnu/libc.so.6 (0x00007ffffeda8000)
	/lib64/ld-linux-x86-64.so.2 (0x00007ffffefc7000)
make_j4_rc=0
```

镜像里没有 `file` 命令，产物检查用 binutils 的 `readelf -h`（build-essential
自带）；`ldd` 输出证明共享库能正常解析运行时依赖。

## 5. 判定

**编译 + 测试通过**：zlib 从源码配置、编译、链接、跑完自带测试套件，
并发构建也成功；这是在 guest 内完成的真实工作流，而不只是一个 hello。

## 6. 限制与说明

1. 源码是宿主下载后注入镜像的（guest 内没有 deb-src / 网络源）；注入用
   `debugfs write`，可复现命令见第 1 节。
2. zlib 的测试规模较小（自带 example/minigzip 套件），但覆盖静态/共享/
   64-bit 三条真实链接路径；更重的项目（如 sqlite/lua）可复用同一脚本模式。
3. 门禁 `GATE_EXIT=2` 仍来自自定义 checklist 不输出标准
   `DAILY_<域>=ok` 行；`DAILY_CLIENT ... done=True`、`DAILY_DONE=ok`，
   工作流本身完整结束。
4. 本次未改任何 profile；使用的就是上一步的 `desktop-drm-toolchain` 镜像。

## 7. 证据

- `target/zlib-workflow/console.txt`（串口全量）
- `target/zlib-workflow/host.log`
- 归档：`docs/porting/evidence/2026-10-25-real-workflow-build/console-excerpt.txt`
- 源码 tarball：`/tmp/zlib-src/zlib-1.3.1.tar.gz`，
  sha256 `9a93b2b7dfdac77ceba5a558a580e74667dd6fede4585b91eefb60f03b72df23`
