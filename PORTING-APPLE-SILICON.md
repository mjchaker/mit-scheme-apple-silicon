# Porting MIT/GNU Scheme to Apple Silicon (macOS/AArch64)

## Status: working

The native aarch64 build runs.  `src/lib/runtime.com` and
`src/lib/all.com` are built and load; the native LIAR compiler
compiles a file to aarch64 machine code, loads it, runs it, and the
result survives a GC:

```
$ cd src && ./run-build --batch-mode --eval '...(cf "nativetest")...'
;Compiling file: "nativetest.bin" => "nativetest.com"... done
(fib(25)= 75025)  (counter= 3)  (tree-sum(18)= 262144)
(after gc, fib(20)= 6765)
```

Build note: `make` currently stops after the Scheme build, in
`imail`, because `makeinfo` (texinfo) is not installed — a missing
build dependency, unrelated to the port.  `brew install texinfo` or
building only the core subdirectories avoids it.

### Test results

**96 of 97 tests pass**, including the FFI test:

```
$ cd src && FAST=y ./run-build --heap 500000 --batch-mode \
      --load ../tests/check.scm --eval '(%exit)'
...
PASSED: 96
FAILED: "microcode/test-flonum-except"
```

The one failure, and two flags the invocation needs:

- **`microcode/test-flonum-except`** fails because Apple Silicon does
  not trap floating-point exceptions.  This is a hardware limitation
  that upstream already documents: `microcode/floenv.h` enables the
  feget/enable/disableexcept workaround only for `__APPLE__ &&
  __x86_64__`, commenting "the x86 hardware supports it, but aarch64
  hardware generally doesn't".  Not a W^X or codegen issue.
- **`--heap 500000`** is needed: with the default heap the suite dies
  partway through from heap exhaustion.  Worth noting on its own that
  exhaustion presents as a SIGSEGV rather than a clean error, and that
  the `check` target hardcodes no `--heap`.
- **`FAST=y`** is needed in practice: several tests (notably
  `runtime/test-hash-table`, `tests/runtime/test-hash-table.scm:242`)
  run 256 randomized stress iterations instead of 16 without it.  That
  test alone can take over ten minutes unassisted versus ~1.5 seconds
  with `FAST=y`, and its cost varies run to run with the random data,
  which makes it look like a hang.  The harness warns about this at
  startup.

### Build dependencies beyond the base toolchain

Building everything, and running the whole suite, needs some GNU tools
that macOS does not ship (or ships incompatible versions of):

```sh
brew install texinfo autoconf automake libtool
```

- `texinfo` (`makeinfo`) — otherwise `make` stops in `imail` building
  its `.info` manual.
- `autoconf`/`automake` — otherwise `tests/ffi/autobuild.sh` exits 127
  and `ffi/test-ffi` cannot run.
- `libtool` — **GNU** libtool specifically.  macOS's `/usr/bin/libtool`
  is Apple's static-library tool and provides no `libtool.m4`, so
  without the Homebrew one `autoreconf` fails with "undefined or
  overquoted macro: AC_PROG_LIBTOOL" and the FFI test's `configure`
  then reports missing `install-sh`/`compile`.


Status as of 2026-07-18: the microcode builds and runs natively on
arm64 macOS, with a working W^X-compliant executable heap (dual
mapping).  The remaining work is the "delta plumbing" in
fasload/fasdump/GC and bootstrapping a band; both are specified below.

## Distributing on macOS

Building and installing is ordinary:

```sh
cd src
./configure CFLAGS="-g -O2 -fno-strict-aliasing" \
    --enable-native-code=aarch64le --prefix=$HOME/opt/mit-scheme
make && make install
```

Two things about that command are not optional:

- `-fno-strict-aliasing` works around the clang miscompile described
  below; without it the interpreter SIGSEGVs.
- `microcode/` has its **own** `configure`.  The library path is baked
  into the executable via `-DDEFAULT_LIBRARY_PATH` (used only by
  `option.c`), so changing `--prefix` means reconfiguring
  `microcode/` too *and* forcing `option.o` to rebuild.  Deleting only
  the `scheme` binary relinks stale objects and silently keeps the old
  path.

### Code signing (required for notarized distribution)

Compiled Scheme executes out of the heap, so the process creates
executable pages Apple did not sign.  Under the hardened runtime,
which notarization requires, that needs an entitlement:

```sh
src/etc/macos-codesign.sh "Developer ID Application: NAME (TEAMID)" \
    $HOME/opt/mit-scheme/bin
```

The entitlement is `com.apple.security.cs.allow-unsigned-executable-memory`
(`src/etc/macos-entitlements.plist`).  Measured on macOS 27, signing
with `--options runtime`:

| entitlements | result |
|---|---|
| none | process is killed at startup |
| `allow-jit` only | process is killed at startup |
| `allow-unsigned-executable-memory` | runs normally |

`allow-jit` is the wrong entitlement here: it governs `MAP_JIT`
regions, and this port deliberately does not use `MAP_JIT` (see
below).  Notarization itself is the usual `xcrun notarytool submit`
followed by `xcrun stapler staple`; note a bare Unix tree of
executables cannot be stapled, so staple the disk image or installer
you actually ship.

### Making a relocatable binary tarball

`make install` produces a tree that is **not relocatable**: the
library directory is compiled into the executable, so moving the tree
(or unpacking a tarball of it somewhere else) makes it fail with
"searched for file all.com in these directories: &lt;the old path&gt;".
Upstream's binary tarballs sidestep this by being built for, and
unpacked at, `/usr/local`.

To ship something that works wherever it lands, replace `bin/mit-scheme`
with a wrapper that derives the library path from its own location --
the same trick `src/run-build` uses in the build tree:

```sh
#!/bin/sh
HERE=$(cd "$(dirname "${0}")" && pwd)
AUXDIR=$(dirname "${HERE}")/lib/mit-scheme-aarch64le-12.1
if [ -z "${MITSCHEME_LIBRARY_PATH}" ]; then
    MITSCHEME_LIBRARY_PATH=${AUXDIR}; export MITSCHEME_LIBRARY_PATH
fi
exec "${HERE}/mit-scheme-aarch64le-12.1" "${@}"
```

with `scheme` and `mit-scheme-aarch64le` as symlinks to it, and the
real executable left under its versioned name.  Verify relocatability
by moving the original install's `lib` aside and running the unpacked
copy -- otherwise the test passes for the wrong reason, because the
baked-in path still resolves.

Code signatures survive `tar`, so sign before packaging.

### Documentation

The Reference Manual and User's Manual apply to this port unchanged.
The one behavioural difference from other platforms -- floating-point
exceptions do not trap -- is not a documented interface: the manual
does not describe FP trapping procedures, so nothing in it is
contradicted.

## The problem

MIT Scheme's native-code system assumes a heap that is simultaneously
writable and executable:

- `mmap_heap_malloc_try` (`src/microcode/ux.c`) maps the entire Scheme
  heap `PROT_READ|PROT_WRITE|PROT_EXEC` in a single anonymous mapping.
- Compiled-code blocks are first-class heap objects, interleaved with
  data, moved by the copying GC, and executed in place.
- Compiled code allocates data inline (writes through the free
  pointer) *while executing from the same region*, so writability and
  executability are needed at the same instant, not just in the same
  region.

Apple Silicon macOS enforces W^X for all processes.  Measured on this
machine (arm64, macOS 27; probe source in the session scratchpad,
`wxprobe.c`):

| Strategy                                             | Result |
|------------------------------------------------------|--------|
| `mmap` RWX (no `MAP_JIT`)                            | denied (`EPERM`) |
| `mmap` RW → write → `mprotect` RX                    | works |
| `MAP_JIT` + `pthread_jit_write_protect_np` toggle    | works, strictly exclusive |
| `MAP_JIT`, write while in exec mode                  | SIGBUS |
| `MAP_JIT`, exec while in write mode                  | SIGBUS |
| Dual mapping via `mach_vm_remap` (RW view + RX view) | works — simultaneous write (RW view) and execute (RX view) |

The `MAP_JIT` rows kill the obvious approach: the write-protect toggle
applies to the whole `MAP_JIT` region per thread, so a mixed code+data
heap under `MAP_JIT` can never allocate data while running compiled
code.  Also relevant: reading `CTR_EL0` (used by the Linux
cache-flush path in `cmpintmd/aarch64.c`) traps on macOS; the port
must use `sys_icache_invalidate`.

## Chosen strategy: dual-mapped heap

The heap keeps its exact current layout and addresses.  It is mapped
twice:

- **Canonical view** — the primary RW mapping at the addresses used by
  every Scheme object, the GC, and all existing code.  Unchanged.
- **Shadow view** — a read/execute `mach_vm_remap` alias of the same
  physical pages at `cc_exec_delta` bytes away.  All compiled code
  executes here.

Nothing about heap layout, bands, purify, or the copying GC's
allocation changes.  What changes is the interpretation of *PC
values*.

### Why this is cheap on the AArch64 backend specifically

The aarch64 backend (unlike i386/x86-64) was designed so that:

1. **Closures contain no instructions.**  A closure stores a 64-bit
   *PC offset* as data; calls compute `PC = entry_address + pc_offset`
   at the call site (`apply_setup` in `cmpauxmd/aarch64.m4`,
   `CC_ENTRY_ADDRESS_PC` in `cmpintmd/aarch64.h`).
2. **Compiled code never writes machine code.**  Closure consing
   (`generate-closure-entry`, `compiler/machines/aarch64/rules3.scm`)
   writes only data; all code patching (UUO linking, trampolines, GC
   transport, fasload relocation) is done by C, and every such site
   already ends with `FLUSH_I_CACHE_REGION`.
3. **PC-relative code is delta-invariant.**  `B` displacements and
   (with a 4KiB-aligned delta; `mach_vm_remap` gives 16KiB alignment)
   `ADRP` page arithmetic compute the same bits in either view, so
   `write_uuo_target`'s generated branches work unchanged.

### The conventions (also documented in `src/microcode/cmpint.h`)

With `cc_exec_delta != 0`:

- **Tagged compiled-entry datums stored in Scheme objects are
  canonical addresses.**  The GC relocates them exactly as today.
- **Raw PCs are shadow addresses.**  This includes return addresses
  pushed on the Scheme stack by compiled code (returns are a bare
  `ret` to the popped value) and trap-time PCs from signal contexts.
  They must be normalized with `CC_PC_TO_CANONICAL` (an unambiguous
  address-range test — the shadow range is disjoint from the heap)
  before being treated as heap pointers, and re-biased after
  relocation.
- **Every PC-offset field includes `cc_exec_delta`**, so
  `PC = entry + pc_offset` always lands in the shadow view:
  - *Closure PC offsets*: automatic.  Compiled code computes them at
    run time as `ADR`-derived PC (shadow) minus free-pointer address
    (canonical), so the delta is baked in with **zero changes** to
    the compiler backend or to `apply_setup`.
  - *Trampoline PC offsets*: set to `cc_exec_delta` at creation
    (`store_trampoline_insns`, done).
  - *Code-block entry PC offsets*: compiled in as small in-block
    distances; must have the delta **added at load time** (fasload /
    band restore) and **subtracted at dump time** (fasdump).  Not yet
    implemented — see below.

Consequences that fall out for free: `interface_to_scheme_return`'s
`ret` works because stored continuations are shadow PCs;
`C_to_interface` works because `CC_ENTRY_ADDRESS_PC` picks up the
delta from the offset field; reads of code-block headers through
either view work because the shadow is readable.

## Implemented and verified (this machine)

- `src/microcode/ux.c` — heap mapped RW (no `PROT_EXEC` on
  Darwin/arm64); `setup_cc_exec_shadow` creates the RX alias with
  `mach_vm_remap` + `mprotect` and publishes
  `cc_exec_base/size/delta`.  Verified live in lldb: heap
  `[0x10b000000,0x113000000) rw-`, shadow
  `[0x114000000,0x11c000000) r-x`, and a byte written through the
  canonical view is immediately visible in the shadow.
- `src/microcode/cmpint.h` / `cmpint.c` — `cc_exec_delta`,
  `cc_exec_base`, `cc_exec_size` globals (zero on all other systems,
  so every change is a no-op elsewhere) and the
  `CC_EXEC_SHADOW_P` / `CC_PC_TO_CANONICAL` / `CC_CANONICAL_TO_PC`
  macros.
- `src/microcode/cmpintmd/aarch64.c` —
  `aarch64_flush_i_cache_region` uses `sys_icache_invalidate` on the
  shadow addresses under `__APPLE__` (the `mrs ctr_el0` path traps on
  macOS); `store_trampoline_insns` stores the delta as the trampoline
  PC offset; `aarch64_reset_hook` documented.
- Build system: `./configure --enable-native-code=aarch64le` and the
  microcode build work out of the box on arm64 macOS (the
  `cmpauxmd/aarch64.m4` glue already had `__APPLE__` support).  The
  resulting interpreter boots to the band-loading stage.

Build-system gotcha: `cmpintmd.c`/`cmpintmd.h`/`cmpauxmd.m4` in the
build directory are configure-time symlinks into `cmpintmd/` and
`cmpauxmd/`; make does not always notice edits behind them, so after
editing the aarch64 machine files, `rm -f cmpintmd.o` (etc.) or make
clean to be safe.

## Delta plumbing (implemented 2026-07-18)

The convention refined during implementation: **every tagged
compiled-entry/return datum is canonical, everywhere** — including
continuations on the Scheme stack.  This leaves the GC's compiled-entry
relocation (`gc_cc_entry` / `gc_cc_return` in gcloop.c) completely
untouched.  The delta lives in PC-offset fields and is applied/removed
at exactly these places:

- **Producing a PC from a datum**: `PC = datum + pc_offset` (unchanged
  in both `apply_setup` and `CC_ENTRY_ADDRESS_PC`) — in-memory offsets
  always include the delta; `pop-return` and
  `interface_to_scheme_return` (via `CC_RETURN_ADDRESS_PTR`, now
  `CC_CANONICAL_TO_PC`) add it explicitly since returns bypass the
  offset field.
- **Producing a datum from a PC**: compiled code subtracts the delta
  when materializing `ENTRY:PROCEDURE` / `ENTRY:CONTINUATION`
  (rules1.scm); C normalizes with `CC_PC_TO_CANONICAL` in
  `compiler_interrupt_common`, `classify_pc` (uxtrap.c), and
  `compiled_closure_entry_to_target`.
- **On-disk format is delta-free**: fasload's relocation scan (now
  forced whenever `cc_exec_delta != 0`, even for address-matching
  bands) biases every referenced entry's PC offset; fasdump unbiases
  the dumped copies and writes closure offsets plain
  (`cc_dumping_p`).  All fixups are idempotent via an address-range
  test (a biased offset lands the PC in the disjoint shadow range), so
  reaching the same entry through many references is safe.
- **Trampolines**: `store_trampoline_insns` writes the delta as the PC
  offset, and `compiler_reset` re-stores the trampolines on every band
  restore (a band carries the dumping session's stale delta) and
  publishes the delta in `Registers[REGBLOCK_CC_DELTA]` (slot 14) for
  compiled code (`reg:cc-delta`).
- **Closures**: runtime-created closure offsets include the delta
  automatically (shadow `ADR` PC minus canonical free pointer);
  `read_compiled_closure_target` normalizes, and
  `write_compiled_closure_target` re-biases (or writes plain during a
  dump).  `write_uuo_target` accepts both 0 and delta as the
  "instructions immediately follow the entry" offset.

Additions from first native runs (the finish-cross-compilation step
was the first time compiled aarch64 code executed under the dual
mapping):

- **Block prologues write through ADR-derived addresses**:
  `generate/quotation-header` stores the environment into the block
  through an `ADR`-derived (shadow) pointer — SIGBUS.  Fixed in
  rules3.scm by subtracting `reg:cc-delta` before the store.
- **Utilities receive shadow addresses**: every microcode utility arg
  that arrives as a raw address from compiled code (`comutil_link`'s
  return/block/constants addresses, the `*_trap` family's
  `ret_addr`/`cache_addr`, trampoline `TRAMP_store`, lexpr-apply's
  entry address, breakpoint entries) is now normalized with
  `CC_PC_TO_CANONICAL` at entry — idempotent by range test, so
  already-canonical values pass through.
- **Bands are raw heap images** (`DUMP-BAND*` writes memory
  verbatim), so their PC offsets carry the dumping process's delta on
  disk.  New header word `FASL_OFFSET_CC_DELTA` (19; old files have
  `#F` there, which decodes as 0) records it, and the fasload rebase
  computes `offset - stale + current`.  Additionally the shadow is
  now requested at a *fixed* delta (heap base + 4 GiB, kernel-chosen
  fallback), so same-machine bands normally rebase as a no-op.

Further additions from driving the finish step (each fixed a distinct
SIGBUS/SIGSEGV, found by lldb on the raw fault PC):

- **Run-time entry materialization**: entries built from in-block
  labels (not fasload-biased, not closure-intrinsic) had plain
  offsets, so `PC = entry + offset` landed in the canonical (RW,
  non-executable) view.  `CC_ENTRY_ADDRESS_PC` is now
  `aarch64_entry_pc`, which biases into the shadow when the offset
  didn't already; the assembly twin is a range test in `apply_setup`
  using a new register-block slot `REGBLOCK_CC_SHADOW_START` (15)
  holding the shadow base.  The shadow is required to sit strictly
  above the heap for the test (enforced in `setup_cc_exec_shadow`).
- **Subproblem continuations**: the pushed-continuation idiom
  (`BL .+8; B target; ORR x30,#tag; STP x30,...`) leaves a shadow PC
  in x30; `ENTRY:CONTINUATION`/`ENTRY:PROCEDURE` subtract
  `reg:cc-delta` so the tagged datum on the stack is canonical (GC
  relocates it; `pop-return` re-adds the delta).  Confirmed in the
  emitted LAP.

**Key architectural correction (learned from the finish-step
crashes): the shadow bias belongs at PC-*use* sites, not baked into
stored offsets.**  An externally-referenced entry can have its stored
PC offset adjusted at fasload, but a compiled block also contains many
*internal* entries (continuation entries, computed-jump targets) whose
PC offsets are fixed at compile time and are never seen by fasload.
So any site that turns a (canonical) entry datum into an executable PC
must apply a *range test* — "if entry+offset is below the shadow, add
the delta" — which is idempotent across all three offset provenances
(plain internal, fasload-adjusted external, closure-intrinsic).  The
three use sites:
  - `apply_setup` (cmpauxmd/aarch64.m4) — unknown-procedure apply;
  - `aarch64_entry_pc` (CC_ENTRY_ADDRESS_PC, cmpintmd/aarch64.c) —
    every microcode entry into compiled code;
  - `entry->pc` (rules3.scm) — inline `INVOCATION:COMPUTED-JUMP`.
All three use `reg:cc-shadow-start` / `REGBLOCK_CC_SHADOW_START` (slot
15) for the compare.  Continuation *datums* are a separate story: they
are normalized to canonical when pushed (ENTRY:CONTINUATION,
with-stack-marker, with-interrupts all subtract the delta), so
`pop-return` adds it back unconditionally.  Given the range test,
the fasload offset bias is redundant (but harmless — kept for now).

Debugging note: a stale cross-compiled `.com` (built by a compiler
band predating a backend fix) produces exactly the same crash as a
missing fix.  When a fault's disassembly shows a backend fix absent
that the current `compiler.com` provably emits (check by
`cf`-ing a probe file with `compiler:generate-lap-files?` set and
reading the `.lap`), purge and recompile the affected stage rather
than re-patching.

Compiler-backend changes (machine.scm, lapgen.scm, rules1.scm,
rules3.scm) are no-ops when the delta slot holds zero, so one compiler
serves Linux and macOS; but old bands (built by an unmodified
compiler) will not run where the delta is nonzero.

## Remaining work

1. **Bootstrap and validate**: install a host Scheme (see below),
   rebuild the compiler with the backend changes, build a band, and
   run `make check` — the compiler tests (`compiler/test-*`) exercise
   exactly the uuo-link/closure/trampoline machinery this port
   touches.  The delta plumbing compiles and boots but cannot be
   exercised further without a band.
2. **Audit residual PC/datum conversions**: entry objects constructed
   by introspection primitives (comutl.c) from block + offset
   arithmetic; any remaining utility that receives a raw PC from
   compiled code.  Also verify compiled code never *stores* through a
   PC-derived (shadow) address into its own block — reads are fine,
   writes would fault.
3. **Shadow placement hardening**: `mach_vm_remap` currently lets the
   kernel choose the shadow address; requesting a deterministic spot
   far from plausible heap bases (e.g. heap base + 2^40) would remove
   any chance of the idempotency range test colliding with stale
   addresses from a foreign FASL file.

## Bootstrap findings (2026-07-18, Rosetta host)

The official `mit-scheme-12.1-x86-64.tar.gz` binary dist builds and
runs under Rosetta 2 (whose emulated processes are still allowed RWX
mappings), but two latent 12.1 bugs bite on modern macOS:

1. **clang 21 at `-O3` miscompiles the microcode** — the interpreter
   SIGSEGVs executing band code.  The old C leans on strict-aliasing
   violations (`struct cc_entry` even jokes about it).  Building with
   `CFLAGS="-g -O2 -fno-strict-aliasing"` fixes it; we use the same
   flags for the native aarch64 microcode as prophylaxis.
2. **Use-after-free in fasdump** (`src/microcode/fasdump.c`, present
   upstream through master as of this writing): `make_prim_renumber`
   registers `free_prim_renumber` with `tat_always`, so
   `transaction_commit()` frees `current_pr`; the code then reads
   `current_pr->next_code` to fill `FASLHDR_N_PRIMITIVES`.  For years
   the stale read returned the right value; macOS 26+'s zero-on-free
   allocator makes it read 0, so every dumped FASL claims an
   empty-but-present primitive table.  Loading such a file leaves
   `new_prim_table` uninitialized and resolves primitives to garbage
   (first symptom: `syntax-runtime` dies on `srfi-1.scm` with an
   unprintable `bad-range-argument` from `primitive-procedure-arity`).
   Fixed here (in both the host tree and this tree) by capturing
   `next_code` before the commit.  **Worth reporting upstream.**
3. **x86-64 LIAR emits an illegal ternary IMUL for checked left
   shifts by n >= 32** (`machines/x86-64/rulfix.scm`,
   `load-fixnum-constant`'s shift case; still present in master): the
   shift is open-coded as IMUL by `2^n` to get overflow detection,
   but the ternary IMUL immediate must fit in 32 bits, and for larger
   constants `with-unsigned-immediate-operand` materializes a
   register, producing `(IMUL Q r r r)` which the assembler rejects
   ("illegal instruction syntax").  First triggered by
   `(shift-left word 32)` in this tree's
   `machines/aarch64/rules3.scm` (`make-closure-padded-format`);
   worked around there by splitting into two shifts by 16 (the
   arithmetic must stay generic/checked because the packed word can
   exceed fixnum range).  The real fix belongs in the x86-64 backend:
   use the binary `(IMUL Q target temp)` form when the constant does
   not fit, as `load-fixnum-constant`'s multiply case already does.
   **Also worth reporting upstream.**
4. **Default host heap too small for the tools band dump**, with a
   maximally misleading symptom: loading the compiled compiler into
   the tools runtime band to `disk-save` `tools/compiler.com` runs
   out of heap partway through and surfaces as `Attempt to read
   binary file .../delint.com failed: either it's not binary or the
   wrong version` (the file is fine; it loads standalone).  configure
   only sets `HOST_COMPILER_HEAP` for some host types.  Fixed here by
   setting `HOST_COMPILER_HEAP = --heap 200000` in `Makefile.tools`
   and `TOOL_COMPILER_HEAP` in `Makefile` (note these are generated;
   re-running configure loses the setting unless `HOST_COMPILER_HEAP`
   is exported to it).

Host setup that works: extract the x86-64 binary dist, then in its
`src/`: `./configure CFLAGS="-g -O2 -fno-strict-aliasing"
--enable-native-code=x86-64 --prefix=$HOME/opt/mit-scheme-host`,
`make`, `make install`, apply the fasdump fix, rebuild.  Then in this
tree: `MIT_SCHEME_EXE=$HOME/opt/mit-scheme-host/bin/mit-scheme
./configure CFLAGS="-g -O2 -fno-strict-aliasing"
--enable-native-code=aarch64le --enable-cross-compiling` and `make`.
If a broken host ever ran the build, `make toolclean && rm -f
stamp_toolchain` before retrying — the tools stage caches corrupt
`.bin`/`.ext` files.

## Bootstrapping a host Scheme (no mit-scheme on this machine)

MIT Scheme builds itself; options on an Apple Silicon Mac:

- **Rosetta 2 host (recommended)**: install the official 12.1 x86-64
  macOS binary; it runs under Rosetta (which permits its RWX heap).
  Use it as `MIT_SCHEME_EXE` to cross-compile:
  `./configure --enable-native-code=aarch64le
  --enable-cross-compiling` (see `--enable-compiler-target` handling
  in `src/configure.ac`), then `make` in `src/`.
- **Portable C ("liarc") distribution**: build a host anywhere from
  the liarc tarball, no Scheme needed (see node "Portable C
  Installation" in the user manual).
- **svm1**: `--enable-native-code=svm1` builds a bytecode-VM Scheme
  with no W^X issues at all — useful both as a working Scheme on
  macOS/arm64 today and as a host for the native build.

## Distribution / hardened-runtime caveat

The dual mapping relies on `mach_vm_remap` + `mprotect(RX)` of
anonymous memory, which works for plain (ad-hoc-signed) binaries.
Under the hardened runtime (notarized distribution) this requires the
`com.apple.security.cs.allow-unsigned-executable-memory` entitlement.
The Apple-sanctioned alternative (`MAP_JIT` +
`com.apple.security.cs.allow-jit`) is *not* compatible with the
dual-mapping design (the toggle is per-thread, per-region-set, and
exclusive); adopting it would require segregating compiled code into
its own arena, a much larger change to the GC and loader.
