# Build system reference

A map of the build machinery behind the quick-start in the top-level
README. Paths are relative to `src/` unless stated otherwise.

Read the top-level [README](../README.md) first for the commands that
work on Apple Silicon (`--enable-native-code=aarch64le
--enable-cross-compiling`, `-fno-strict-aliasing`, GNU libtool). This
document explains what those commands do.

## 1. Build states

| State | How you get there |
| --- | --- |
| fresh | git checkout |
| distribution | `./Setup.sh` |
| configured | `./configure` |
| compiled | `make` |

`make clean` goes back to configured, `make distclean` to
distribution, `make maintainer-clean` to fresh.

`Setup.sh`:

- verifies that `${MIT_SCHEME_EXE:-mit-scheme}` runs
  (`--batch-mode --no-init-file --eval '(%exit)'`);
- runs autoheader and autoconf;
- populates `lib/` symlinks (see
  [BOOT-AND-PACKAGES.md](BOOT-AND-PACKAGES.md#5-bands-and-the-lib-layout));
- runs each subdirectory's `Setup.sh` (symlinks to `etc/Setup.sh`), then
  `autogen.sh` in plugin directories, then `etc/Stage.sh` and
  `compiler/Stage.sh`.

## 2. configure

| Option | Effect |
| --- | --- |
| `--enable-native-code[=ARCH]` | `aarch64le`, `aarch64be`, `x86-64`, `c`, `svm1-*` |
| `--with-compiler-target` | Compiler target when it differs from the host |
| `--enable-cross-compiling` | Build via tool bands and convert output; required to bootstrap the AArch64 port from an x86-64 host |
| `--enable-host-scheme-test` | Test the host Scheme |
| `--with-default-target` | Default make target (`all`, or `compile-microcode` for native releases) |
| `--with-scheme-build=DIR` | Use an existing build tree's `run-build` as host |
| `--enable-default-plugins`, `--enable-x11`, `--enable-edwin`, `--enable-imail`, `--enable-blowfish`, `--enable-gdbm`, `--enable-pgsql` | Optional subsystems |
| `--enable-debugging` | Debug build |

**Host Scheme selection** (`configure.ac`): with `--with-scheme-build`,
`DIR/run-build`; otherwise `MIT_SCHEME_EXE` if it runs; otherwise
`mit-scheme-ARCH`, then `mit-scheme`. configure then runs
`etc/create-makefiles.sh`. The result is `@MIT_SCHEME_EXE@` in
`Makefile.in`.

## 3. Makefile targets (`Makefile.in`)

| Group | Targets |
| --- | --- |
| Top level | `all` = `cross-host` then `cross-target` |
| Cross | `cross-host` (everything cross-compilable), `stamp_cross-finished` (LIARC bundles and `.moc` to `.com`), `cross-target` (bands, `microcode/scheme`, libraries, plugins) |
| Toolchain | `toolchain` runs `Makefile.tools`, building `tools/runtime.com`, `tools/syntaxer.com`, `tools/compiler.com` with the host Scheme. Cross builds only |
| Runtime | `syntax-runtime` (loads `runtime.sf`), `compile-runtime` (loads `runtime.cbf`) |
| SF | `syntax-sf`, `compile-sf`, `bundle-sf` |
| Compiler | `syntax-compiler`, `compile-compiler` (back, base, fggen, fgopt, machine, rtlbase, rtlgen, rtlopt), `bundle-compiler` |
| Other | cref, star-parser, ffi, sos, xml, ssp, libraries, plugins |
| Microcode | `microcode/scheme`, `compile-microcode` |
| Bands | `lib/runtime.com`, `lib/all.com` |
| Misc | `check`, `macosx-app`, `install`, `tags`, `save`/`restore` |
| Legacy C backend | `all-liarc`, `build-bands`, `liarc-dist`, `stamp_*liarc*` |

There is no `make-cross` target. Cross compiling is `--enable-cross-compiling`
plus the `cross-host` / `cross-target` targets.

**Native vs cross.** Native builds use `--band runtime.com` plus
`runtime/host-adapter.scm`. Cross builds use the `tools/*.com` bands, set
`compiler:cross-compiling?`, `sf/cross-compiling?` and
`package/cross-compiling?`, and convert host output with
`etc/crossbin` (`TOOL_CROSS_HOST`).

## 4. Building an incompatible compiler

When compiler data structures changed, `make` cannot rebuild the
compiler directly. From `src/compiler/`:

1. Start `scheme --band runtime.com`; `(load-option 'sf)`,
   `(load "compiler.sf")`.
2. Restart the same way; `(load-option 'sf)`, `(load "make")`,
   `(load "compiler.cbf")`.
3. Build the band: `(load-option 'cref)`, `(load-option 'sf)`,
   `(load "make")`, `(disk-save "compiler-band.com")`.

## 5. `src/etc` scripts

| Script | Purpose |
| --- | --- |
| `Setup.sh`, `Stage.sh`, `Clean.sh`, `Tags.sh` | Per-subdirectory templates symlinked in by the top-level `Setup.sh` |
| `functions.sh` | Shared helpers (`get_fasl_file`, `maybe_link`, `run_cmd`, ...) |
| `create-makefiles.sh HOST ARCH` | Per-arch Makefiles |
| `maybe-update-file.sh` | Copy only if content differs |
| `build-bands.sh`, `build-boot-compiler.sh`, `compile-boot-compiler.sh`, `native-prepare.sh`, `c-prepare.sh` | Bootstrap steps (mostly native/LIARC distributions) |
| `compile.sh`, `c-compile.sh`, `compile.scm` | Compile driver |
| `c-bundle.sh` | LIARC bundle builder |
| `make-native.sh`, `make-liarc.sh`, `make-liarc-dist.sh` | User-facing wrappers |
| `install-bin-symlinks.sh` | Install-time symlinks |
| `macos-codesign.sh`, `macos-make-dmg.sh`, `macosx/` | Signing, disk image, app bundle |
| `optiondb.scm`, `plugins.scm` | Option and plugin databases installed to `AUXDIR` |
| `crossbin.scm` | Host `.nib` to target `.bin` |
| `check-gc-tables.scm` | GC table consistency check |
| `ucd-*`, `iso8859-*`, `find-folded.scm` | Unicode and charset table generation |

## 6. Troubleshooting

- **Interpreter crashes at startup or during GC:** confirm
  `-fno-strict-aliasing` is in `CFLAGS`.
- **"unable to map executable shadow of heap":** the `mach_vm_remap`
  shadow could not be created; native code will fail. See
  [MICROCODE.md](MICROCODE.md#5-apple-silicon-specifics).
- **Killed immediately when running a signed build:** the hardened
  runtime needs the `allow-unsigned-executable-memory` entitlement.
  See the README's code-signing section.
- **Test suite dies partway with SIGSEGV:** heap exhaustion; pass
  `--heap 500000`.
- **`runtime/test-hash-table` seems to hang:** set `FAST=y`.
- **`make` stops in `imail`:** install `texinfo`.
- **Missing `libtool.m4`:** you are picking up Apple's `/usr/bin/libtool`;
  install GNU libtool.
- **`Setup.sh` complains it cannot run mit-scheme:** set `MIT_SCHEME_EXE`.
