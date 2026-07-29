# MIT/GNU Scheme 12.1 — native Apple Silicon port

MIT/GNU Scheme 12.1 built to run natively on Apple Silicon (macOS,
AArch64), including the LIAR native-code compiler.

This is an **unofficial** port. It is not affiliated with MIT or the
MIT/GNU Scheme maintainers; see [upstream](https://www.gnu.org/software/mit-scheme/)
for the official releases.

## Status

Working. The full tree builds, the native compiler compiles AArch64
machine code *on* AArch64 and runs it, and **96 of 97 tests pass**.

The one failure, `microcode/test-flonum-except`, is an Apple Silicon
hardware limitation rather than a defect in the port: floating-point
exceptions do not trap. Upstream documents this itself — `microcode/floenv.h`
enables the trapping workaround only for `__APPLE__ && __x86_64__`,
noting "the x86 hardware supports it, but aarch64 hardware generally
doesn't."

The [Reference Manual and User's Manual](https://www.gnu.org/software/mit-scheme/documentation/)
apply to this port unchanged.

## Install the binary

Both downloads on
[Releases](https://github.com/mjchaker/mit-scheme-apple-silicon/releases)
hold the same binaries, signed with a Developer ID and notarized by
Apple.

Prefer the disk image. Its notarization ticket is stapled, so macOS
validates it without a network round trip. Open
`mit-scheme-12.1-aarch64le-darwin.dmg`, drag the folder inside to
wherever you want it, and run `bin/mit-scheme` from there.

The tarball suits scripted installs:

```sh
tar xzf mit-scheme-12.1-aarch64le-darwin.tar.gz
./mit-scheme-12.1-aarch64le-darwin/bin/mit-scheme
```

Either way it is relocatable: `bin/mit-scheme` derives its library
path from its own location, so it runs from wherever you put it.

> A notarization ticket can be stapled to an image or an installer,
> never to a loose tree of executables. The binaries in the tarball
> carry the same notarization, but Gatekeeper confirms it online
> instead of locally. In practice `tar` does not propagate the
> quarantine attribute, so a `curl`-and-`tar` install is never
> checked at all.

## Build from source

MIT/GNU Scheme is self-hosting, so building requires an existing
Scheme. There is no native AArch64 release to bootstrap from, so use
the official x86-64 macOS binary under Rosetta 2 as the host and
cross-compile. Full details in
[PORTING-APPLE-SILICON.md](PORTING-APPLE-SILICON.md).

```sh
brew install texinfo autoconf automake libtool

cd src
MIT_SCHEME_EXE=/path/to/host/mit-scheme ./configure \
    CFLAGS="-g -O2 -fno-strict-aliasing" \
    --enable-native-code=aarch64le --enable-cross-compiling \
    --prefix="$HOME/opt/mit-scheme"
make && make install
```

Two flags are not optional:

- `-fno-strict-aliasing` — clang miscompiles the microcode at `-O3`
  without it, and the interpreter crashes.
- GNU `libtool` — macOS's `/usr/bin/libtool` is Apple's unrelated
  static-library tool and provides no `libtool.m4`.

Run the tests with:

```sh
cd src && FAST=y ./run-build --heap 500000 --batch-mode \
    --load ../tests/check.scm --eval '(%exit)'
```

Both flags matter: the default heap is too small to run the suite in
one process, and without `FAST=y` some tests run 256 randomized stress
iterations instead of 16 (`runtime/test-hash-table` alone can take
over ten minutes rather than ~1.5 seconds).

## How the port works

Apple Silicon enforces W^X: memory cannot be writable and executable
at once. MIT/GNU Scheme's native-code system assumes exactly that,
because compiled code allocates data through the free pointer *while
executing from the same heap*.

`MAP_JIT` does not solve this — its write-protect toggle is per-thread
and strictly exclusive, so a heap that interleaves code with data the
running code allocates cannot use it.

Instead the heap is **mapped twice**: writable at the canonical
addresses every Scheme object uses, and read/execute in a shadow alias
created with `mach_vm_remap`. Compiled code executes in the shadow
view. Heap layout, the garbage collector, and every data pointer are
unchanged; only PC values need translating.

The invariant that makes it work: **every stored PC offset is
delta-free**, and the shadow delta is added only where an executable
PC is materialized. That is what lets a dumped band load under
whatever delta the kernel gives the next process — which matters
because the delta varies per run and `mach_vm_remap` cannot be pinned
to a fixed offset.

[PORTING-APPLE-SILICON.md](PORTING-APPLE-SILICON.md) has the full
design, the measurements behind each decision, and the failure modes
worth knowing when debugging.

## Code signing

Compiled Scheme executes out of the heap, so the process creates
executable pages Apple did not sign. Under the hardened runtime —
which notarization requires — that needs an entitlement:

```sh
src/etc/macos-codesign.sh "Developer ID Application: NAME (TEAMID)" \
    "$HOME/opt/mit-scheme"
```

Pass the whole prefix, not just `bin`. An install has a second
executable, `lib/mit-scheme-ARCH-VERSION/macosx-starter`, which ships
with only the linker's ad-hoc signature; notarization rejects the
entire submission over it.

The entitlement is `com.apple.security.cs.allow-unsigned-executable-memory`
([macos-entitlements.plist](src/etc/macos-entitlements.plist)).
Measured under the hardened runtime:

| entitlements | result |
| --- | --- |
| none | killed at startup |
| `allow-jit` only | killed at startup |
| `allow-unsigned-executable-memory` | runs normally |

`allow-jit` is the wrong entitlement here: it governs `MAP_JIT`
regions, which this port deliberately does not use.

### Notarized disk image

`macos-codesign.sh` signs an install tree in place.
[macos-make-dmg.sh](src/etc/macos-make-dmg.sh) turns one into
something distributable: it stages the tree, replaces `bin/mit-scheme`
with the wrapper that makes it relocatable, signs the executables,
builds a disk image, then notarizes and staples it.

```sh
xcrun notarytool store-credentials mit-scheme \
    --apple-id YOU@example.com --team-id TEAMID

src/etc/macos-make-dmg.sh -p mit-scheme -t \
    "Developer ID Application: NAME (TEAMID)" "$HOME/opt/mit-scheme"
```

That writes `mit-scheme-12.1-aarch64le-darwin.dmg` and verifies it with
`stapler validate` and `spctl`. Omit `-p` to build and sign without
contacting Apple.

`-t` writes `mit-scheme-12.1-aarch64le-darwin.tar.gz` alongside it from
the same staging area, so the two downloads hold identical binaries and
the tarball inherits the image's notarization. It is written only after
everything else succeeds, so a rejected submission cannot leave behind
a tarball that looks shippable.

Notarization needs a **Developer ID Application** certificate. The
`Apple Development` certificates Xcode creates are for running on your
own registered devices; notarytool rejects them.

Only the image is stapled. `stapler` accepts app bundles, installer
packages and disk images — a bare Mach-O executable has nowhere to put
the ticket. The executables inside are covered by the notarization
record and validate online, so a user who copies them out while
offline may still see a Gatekeeper prompt.

## Upstream bugs found

Three bugs in 12.1 surfaced while porting, two still present in
upstream master. They are written up in
[UPSTREAM-BUGS.md](UPSTREAM-BUGS.md), most notably a use-after-free in
`fasdump.c` that silently corrupts the primitive table of every FASL
file dumped on macOS 26+.

## License

GPL-2.0-or-later, inherited from MIT/GNU Scheme, with the OpenSSL
linking exception. See [COPYING](COPYING).

Copyright for the original work is held by the Massachusetts Institute
of Technology. The port changes are offered under the same terms.
