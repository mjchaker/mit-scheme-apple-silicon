# LIAR: the native-code compiler, and its AArch64 backend

A developer's guide to `src/compiler`. Paths are relative to `src/`
unless stated otherwise. Line numbers were checked against the tree at
the time of writing and will drift; use them as starting points.

Upstream's own material is `compiler/README` and
`compiler/documentation/` (`porting.guide`, `cmpint.txt`, `cmpaux.txt`,
`facts.txt`, `notes.txt`). This guide does not replace them. It adds the
map of the pipeline, the AArch64 backend specifics, and the
Apple-Silicon shadow-heap conventions, which upstream does not have.

## 1. Directory layout

| Directory | Purpose |
| --- | --- |
| `compiler/base/` | Driver (`toplev.scm`), switches (`switch.scm`), package loader (`make.scm`), control-flow graph (`cfg1-3.scm`, `ctypes.scm`), rvalues/lvalues/blocks, assembler and linker top level (`asstop.scm`), cross-compilation (`crstop.scm`, `crsend.scm`), debug-info generation (`infnew.scm`) |
| `compiler/fggen/` | SCode to flow graph: `fggen.scm`, `canon.scm` (canonicalize SCode), `declar.scm` (declarations) |
| `compiler/fgopt/` | Flow-graph analyses and optimizations: closure analysis, environment optimization, constant folding, operator analysis, side-effect analysis, continuation analysis, subproblem ordering, and so on |
| `compiler/rtlbase/` | RTL data types and register sets |
| `compiler/rtlgen/` | Flow graph to RTL. `opncod.scm` open-codes primitives |
| `compiler/rtlopt/` | RTL optimizations: CSE (`rcse*`), dataflow, lifetime analysis, rewriting, register allocation (`ralloc`), common-suffix merging |
| `compiler/back/` | Back end: LAP generation and linearization, hardware register map, assembler top level and syntaxer |
| `compiler/machines/` | Per-target backends: `aarch64`, `x86-64`, `i386`, `C` (portable C, "LIARC"), `svm` |
| `compiler/documentation/` | Upstream design notes |

`compiler/configure` symlinks `machine` to `machines/<arch>` and
`endian.scm` to `little-endian.scm` or `big-endian.scm`
(`compiler/configure:53-75`). `compiler/choose-machine.sh` maps
`aarch64le|aarch64be` to `machines/aarch64`.

## 2. The pipeline

Everything is driven from `compile-scode/internal` in
`compiler/base/toplev.scm`. Phases run in this order:

1. **FG generation** (`phase/fg-generation`). SCode is canonicalized and
   translated into the flow graph: `rvalue`, `lvalue` and `block`
   objects plus CFG snodes and pnodes.
2. **FG optimization** (`phase/fg-optimization`). In order:
   simulate-application, outer-analysis, fold-constants,
   open-coding-analysis, operator-analysis, environment-optimization,
   identify-closure-limits, setup-block-types, variable-indirection,
   compute-call-graph, side-effect-analysis, continuation-analysis,
   subproblem-analysis, delete-integrated-parameters,
   subproblem-ordering, design-environment-frames,
   connectivity-analysis, compute-node-offsets, return-equivalencing,
   info-generation-1, cleanup. This is where most Scheme-specific
   technology lives.
3. **RTL generation** (`phase/rtl-generation`). Produces the RTL graphs
   (`*rtl-graphs*`, `*rtl-procedures*`, `*rtl-continuations*`,
   `*rtl-root*`).
4. **RTL optimization** (`phase/rtl-optimization`). Dataflow analysis,
   pre-CSE rewriting, CSE (only if `compiler:cse?`), invertible
   expression elimination, post-CSE rewriting, common-suffix merging,
   lifetime analysis, code compression (only if
   `compiler:code-compression?`), linearization analysis, register
   allocation.
5. **LAP generation** (`phase/lap-generation`). The machine's
   `define-rule` rules turn RTL into LAP, the "list of assembly
   pseudo-instructions". `phase/lap-linearization` then produces
   `*lap*`, after the machine's `optimize-linear-lap`, which is an
   identity function on AArch64 (`machines/aarch64/lapopt.scm`).
6. **Assemble and link** (`base/asstop.scm`, or `base/crstop.scm` when
   cross-compiling). `phase/assemble` produces `*code-vector*`,
   `*entry-points*` and `*label-bindings*`. `phase/link` builds the
   compiled-code block and entry addresses and calls the microcode
   primitive `declare-compiled-code-block`. `phase/info-generation-2`
   attaches debug info.

### Entry points (`compiler/base/toplev.scm`)

| Name | Role |
| --- | --- |
| `compile-file` | Incremental (dependency-aware). Runs `sf`, then compiles the `.bin`. Honors `compile-file:force?` |
| `cf` | Non-incremental: `sf` then compile |
| `cbf` / `compile-bin-file` | Compile an already-syntaxed `.bin` to `.com` |
| `compile-directory` | Batch |
| `compile-scode`, `compile-procedure` | Compile in memory |
| `compiler:batch-compile` | Batch driver |

### File types

| Extension | Content |
| --- | --- |
| `.scm` | Source |
| `.bin` | SCode produced by `sf` |
| `.com` | Compiled code |
| `.bci` / `.inf` | Debugging information |
| `.moc` | Cross-compiler output, converted to `.com` on the target |
| `.rtl`, `.lap` | Optional listings; see section 6 |

### The compiled-code block

A compiled block is instructions followed by a constants section. On
AArch64 the constants block is built by `generate/constants-block` in
`machines/aarch64/rules3.scm`; it holds interned constants, variable
caches, assignment caches, global links and UUO links. Runtime accessors
are in `runtime/microcode-data.scm` (`compiled-code-block/code-start`,
`code-end`, and so on); debug info is handled in `runtime/infutl.scm`.

## 3. The AArch64 backend (`compiler/machines/aarch64`)

| File | Role |
| --- | --- |
| `machine.scm` | Architecture parameters (64-bit objects, 6-bit type tag, 58-bit datum), closure layout constants, register naming, register-block offsets, CSE cost model. Sets `compiler:open-code-floating-point-arithmetic?` to `#f`, so flonum arithmetic is not open-coded |
| `lapgen.scm` | Register-allocator interface, `push`/`pop`, register-block effective addresses, hook table indices (`define-entries`), `invoke-hook`/`invoke-interface` helpers |
| `rules1.scm` | Data transfers, tagging, constants |
| `rules2.scm` | Predicates |
| `rules3.scm` | Invocations, entries and closures, procedure headers, interrupt checks, constants block |
| `rules4.scm` | Interpreter calls, variable cache traps |
| `rulfix.scm`, `rulflo.scm` | Fixnum and flonum operations |
| `rulrew.scm` | RTL rewrite rules |
| `rgspcm.scm` | Special primitive combinations (RTL-generation side) |
| `lapopt.scm` | No-op LAP optimizer |
| `assmd.scm` | Assembler machine dependencies. Padding is `HLT #0` (`#xd4400000`); block offsets are 16 bits in units of 8 bytes with a continuation bit |
| `insmac.scm`, `coerce.scm`, `insutl.scm` | Instruction-definition syntax, bit-width coercions, field encoders (including logical-immediate encoding) |
| `instr1.scm`, `instr2.scm`, `instrf.scm` | Integer, further integer, and SIMD/FP instructions, ordered by the ARMv8-A Architecture Reference Manual. Branches use `VARIABLE-WIDTH` with an `ADRP`+`ADD`/`BR` fallback for far targets |
| `decls.scm`, `compiler.pkg`, `compiler.sf`, `compiler.cbf`, `make.scm` | Build and package plumbing |
| `little-endian.scm`, `big-endian.scm` | Endianness selection |
| `TODO` | Known gaps: ADR/ADRP assembly, branch-condition verification, fixnum multiply-add |

There is no AArch64 disassembler. A `dassm` package is commented out in
`compiler.pkg` and the files do not exist.

### Register assignment

Mirrored between `machine.scm` and `microcode/cmpauxmd/aarch64.m4`.
Change both together.

| Register | Role |
| --- | --- |
| `x0` | Value register |
| `x1` | Utility argument 1 and applicand (entry address) |
| `x2`-`x4` | Utility arguments 2-4 |
| `x16` | `scratch-0` (also IP0; branch tensioning) |
| `x17` | `scratch-1` (also IP1); applicand PC and utility index |
| `x18` | Reserved by the platform ABI; never used |
| `x19` | REGS: pointer to the interpreter register block |
| `x20` | FREE: the free pointer |
| `x21` | Dynamic link |
| `x22` | Memtop (commented out in the source: it is still allocatable, and memtop is read from the register block) |
| `x23` | HOOKS: the assembly hook table |
| `x28` | Scheme stack pointer. Scheme has its own stack, separate from the C stack |
| `x29` | C frame pointer, left alone |
| `x30` | Link register |
| `sp` | C stack pointer |

`x0`-`x15`, `x22` and `x24`-`x27` are allocatable general registers;
`v0`-`v31` are float registers. `x19`-`x21` and `x23` are callee-saved
in the C ABI, so the C/Scheme transition needs no save or restore for
them. The stack grows down: `push` is `STR x,[x28,#-8]!` and `pop` is
`LDR x,[x28],#8`.

Register-block slots that compiled code addresses directly:

| Slot | Contents |
| --- | --- |
| 0 | memtop |
| 1 | interrupt mask |
| 2 | value |
| 3 | environment |
| 7 | lexpr primitive arity |
| 11 | stack guard |
| 12 | interrupt code |
| 13 | reflect-to-interface |
| 14 | `cc-delta` (shadow-heap delta; see section 4) |
| 15 | `cc-shadow-start` |

Spill homes start at slot 16, and there are 256 temporaries
(`COMPILER_REGBLOCK_N_TEMPS` in `microcode/cmpintmd/aarch64.h`).

### Calling convention and the microcode interface

- Arguments are pushed on the Scheme stack; the applicand is popped
  into `x1`.
- An unknown call is a `BL` to an `apply_setup_N` hook. The hook checks
  that the object is a compiled entry (`TC_COMPILED_ENTRY`, 40) and that
  the arity in the entry header matches, then returns the target PC in
  `x17` and the caller does `BR x17`. On any mismatch it falls back to
  utility `comutil_apply` in the microcode.
- Return pops the return address into the link register, converts it to
  a real address, adds the shadow delta, and does `RET`. Return
  addresses are tagged objects on the stack.
- Interrupt checks compare FREE against memtop.

Assembly entry points in `microcode/cmpauxmd/aarch64.m4`:

| Entry | Role |
| --- | --- |
| `C_to_interface` | Enter Scheme from C |
| `interface_to_C` | Return to C |
| `scheme_to_interface` | Save VAL, FREE and the Scheme SP, then call `utility_table[x17]` (defined in `microcode/cmpint.c`) and branch to the address the utility returns |
| `interface_to_scheme` | Restore state, `br x17` |
| `interface_to_scheme_return` | `RET` to `x1` |
| `closure_apply`, `fixnum_shift`, `set_interrupt_enables` | Helpers |

The hook table has one 16-byte slot per hook. Index 0 is
`scheme_to_interface`, 1-0x0f are the generic arithmetic hooks, 0x10 is
`fixnum_shift`, 0x11 is `apply_setup`, 0x12-0x19 are `apply_setup_1`
through `_8`, and 0x1a is `set_interrupt_enables`. The compiler-side
table (`define-entries` in `lapgen.scm`) must match. Utility codes
(`code:compiler-*`) index the microcode's `utility_table`.

The microcode-side machine header is `microcode/cmpintmd/aarch64.h`. Its
opening comment documents the execute-cache (UUO) layout, the closure
format and the trampoline encoding, and is the best single reference
when changing any of them. The C support is in
`microcode/cmpintmd/aarch64.c`, and the machine-independent interface is
`microcode/cmpint.c`.

## 4. Apple Silicon: the shadow-mapped heap

Design rationale is in [PORTING-APPLE-SILICON.md](../PORTING-APPLE-SILICON.md).
This section is a code map.

The heap is mapped twice. The primary mapping is writable and never
executable; a read/execute alias is created with `mach_vm_remap`, and
compiled code runs in the alias. `cc_exec_delta = shadow - primary`.

- Setup: `microcode/ux.c`, `setup_cc_exec_shadow`, compiled only when
  `CC_IS_NATIVE`, `__APPLE__` and `__aarch64__` all hold
  (`USE_CC_EXEC_SHADOW`).
- Globals and macros: `cc_exec_delta`, `cc_exec_base`, `cc_exec_size`,
  `CC_EXEC_SHADOW_P`, `CC_PC_TO_CANONICAL`, `CC_CANONICAL_TO_PC`, in
  `microcode/cmpint.h` and `cmpint.c`. They are zero or identity on
  other platforms.
- The delta and shadow start are published to compiled code through
  register-block slots 14 and 15 (`cmpint.c`).
- `microcode/uxtrap.c` normalizes trap-time PCs.

Conventions (`cmpint.h` is authoritative):

- Tagged compiled-entry and return datums hold **canonical**
  (writable-view) addresses.
- Raw PCs are **shadow** addresses.
- Every stored PC-offset field is **delta-free**, so a dumped band loads
  under whatever delta the next process gets. fasload and fasdump
  rebase nothing.
- The delta is added, by a "if not already in the shadow, add" range
  test, at three sites: `apply_setup` in `aarch64.m4`,
  `aarch64_entry_pc` in `cmpintmd/aarch64.c`, and `entry->pc` in
  `rules3.scm`.
- The delta is subtracted where an ADR-derived shadow PC becomes a
  stored offset: `generate-closure-entry` in `rules3.scm`,
  `store_trampoline_insns` in `aarch64.c`, and `canonicalize-rlr` for
  `BL` return addresses.
- Cache flushing on macOS uses `sys_icache_invalidate` at shadow
  addresses (`aarch64_flush_i_cache_region`), because reading `CTR_EL0`
  traps there. The non-Apple path uses `dc cvau`, `dsb ish`, `ic ivau`
  and `isb`.

Note: parts of `PORTING-APPLE-SILICON.md` that describe fasload adding
the delta predate the delta-free representation. Trust `cmpint.h` and
the code where they disagree.

## 5. Building the compiler

Prerequisites and flags are in the top-level README. The compiler
specific points:

- `./configure --enable-native-code=aarch64le` selects this backend.
  Other accepted values include `x86-64`, `c` and `svm1-*`.
  `--with-compiler-target` and `--enable-cross-compiling` are described
  in `configure.ac`.
- With `--enable-cross-compiling` (required when bootstrapping from the
  x86-64 host under Rosetta), the Makefile builds tool bands
  (`tools/compiler.com`, `tools/syntaxer.com`, `tools/runtime.com`),
  runs the compiler with `compiler:cross-compiling?` set, and converts
  host output to target format (`etc/crossbin`, then
  `finish-cross-compilation`).
- Cross compilation writes `.moc`/`.inf` via `portable-fasdump` with
  `(target-fasl-format)`, which `machines/aarch64/machine.scm` defines as
  `fasl-format:aarch64le` or `fasl-format:aarch64be`.
- Useful targets: `make compile-compiler`, `make toolchain`. From a
  REPL, `(load "compiler/machines/aarch64/make")` loads the compiler
  package set.
- Incompatible changes to compiler data structures need the manual
  procedure in `README.txt` ("Building an incompatible compiler").

### Bringing up another backend

Mirror `machines/aarch64`: `machine.scm`, `lapgen.scm`, `rules*.scm`,
`rulfix`, `rulflo`, `rulrew`, `rgspcm`, `assmd`, `insmac`, `instr*.scm`,
`coerce`, `decls.scm`, `compiler.pkg`, `.sf`, `.cbf`, `make.scm`, and
endianness files. On the microcode side add
`microcode/cmpintmd/<arch>.{h,c}`, `<arch>-config.h` and
`microcode/cmpauxmd/<arch>.m4`. Hook it up in
`compiler/choose-machine.sh`, `compiler/configure`,
`microcode/aclocal.m4` (`MIT_SCHEME_ARCHITECTURE`),
`microcode/configure.ac`, and give `fasl.h` a format code. Read
`compiler/documentation/porting.guide` first.

## 6. Switches, declarations and inspecting output

### Declarations

- `(declare (usual-integrations))` is handled by SF
  (`sf/pardec.scm`; expansion table `usual-integrations/expansion-alist`
  in `sf/usiexp.scm`). `integrate-operator` and `integrate-external`
  are SF declarations too.
- Compiler declarations are processed in `compiler/fggen/declar.scm`.
  `UUO-LINK` is on by default for all (`compiler:default-top-level-declarations`
  in `switch.scm`). `NO-TYPE-CHECKS` and `NO-RANGE-CHECKS` are
  pre-declarations that turn off generated checks in the affected block.
- Primitive open-coding is done in `rtlgen/opncod.scm` (see
  `define-open-coder/*`) and gated by `compiler:open-code-primitives?`.
  `compiler:primitives-with-no-open-coding` in `machine.scm` is a
  per-machine blacklist. This tree has no `integrate-primitive-procedures`
  declaration.

### Switches (`compiler/base/switch.scm`; defaults in parentheses)

Output and inspection: `compiler:noisy?` (#t), `compiler:show-phases?`
(#f), `compiler:show-subphases?` (#f), `compiler:show-time-reports?`
(#f), `compiler:show-procedures?` (#f),
`compiler:generate-rtl-files?` (#f), `compiler:generate-lap-files?`
(#f), `compiler:intersperse-rtl-in-lap?` (#t),
`compiler:preserve-data-structures?` (#f), `compiler:phase-wrapper`
(#f).

Optimization: `compiler:cse?` (#t), `compiler:code-compression?` (#t),
`compiler:open-code-primitives?` (#t), `compiler:optimize-environments?`
(#t), `compiler:analyze-side-effects?` (#t),
`compiler:cache-free-variables?` (#t), `compiler:implicit-self-static?`
(#t), `compiler:enable-integration-declarations?` (#t),
`compiler:compile-by-procedures?` (#t), `compiler:use-multiclosures?`
(#f).

Safety: `compiler:generate-type-checks?` (#t),
`compiler:generate-range-checks?` (#t),
`compiler:generate-stack-checks?` (#t),
`compiler:open-code-flonum-checks?` (#f), `compiler:assume-safe-fixnums?`
(#t).

Other: `compiler:cross-compiling?` (#f), `compiler:avoid-scode?` (#t),
`compiler:compress-top-level?` (#f; the AArch64 `make.scm` sets it #t),
`compiler:package-optimization-level` (`'HYBRID`).

### Looking at what the compiler produced

```scheme
(set! compiler:generate-rtl-files? #t)
(set! compiler:generate-lap-files? #t)
(cf "foo")          ; writes foo.com plus foo.rtl and foo.lap
```

The `.lap` file has RTL interspersed with the generated assembly
pseudo-instructions, printed in hex. To trace phase progress set
`compiler:show-phases?` and `compiler:show-subphases?`. To poke at the
intermediate data structures from the REPL set
`compiler:preserve-data-structures?` and call `compiler:reset!` when
done. Without an AArch64 disassembler, the `.lap` listing is the
practical way to read generated code.
