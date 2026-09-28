# The microcode: object model, interpreter, memory, primitives

A developer's guide to `src/microcode`, the C core that builds the
`scheme` executable. Paths are relative to `src/microcode` unless stated
otherwise; line numbers are hints and will drift. Values are for the
64-bit build used on Apple Silicon.

See also [COMPILER.md](COMPILER.md) for compiled code,
[PORTING-APPLE-SILICON.md](../PORTING-APPLE-SILICON.md) for the W^X
design rationale, and `../README.txt` for the tree overview.

## 1. Object representation

A `SCHEME_OBJECT` is an `unsigned long`: a 64-bit word with a **6-bit
type code** in the top bits and a **58-bit datum** below it
(`object.h`).

| Constant | Value |
| --- | --- |
| `TYPE_CODE_LENGTH` | 6 (64 type codes) |
| `DATUM_LENGTH` | 58 |
| `DATUM_MASK` | `0x03FFFFFFFFFFFFFF` |
| `FIXNUM_LENGTH` | 57 (plus sign bit) |
| `SMALLEST_FIXNUM` / `BIGGEST_FIXNUM` | -2^57 / 2^57 - 1 |

Core macros: `OBJECT_TYPE(o)` is `o >> DATUM_LENGTH`; `OBJECT_DATUM(o)`
is `o & DATUM_MASK`; `MAKE_OBJECT(type, datum)` combines them;
`MAKE_POINTER_OBJECT(type, addr)` and `OBJECT_ADDRESS(o)` convert to and
from addresses.

**Addresses.** On AArch64 `HEAP_IN_LOW_MEMORY` is defined
(`confshared.h`), so the datum of a pointer object is the raw address.
The heap must therefore be mapped below 2^58, and `ux.c` checks this.
Without that macro the datum is a word offset from `memory_base`.

### Type codes (`types.h`)

| Code | Name | Code | Name |
| --- | --- | --- | --- |
| 00 | `FALSE` / `MANIFEST_VECTOR` | 22 | `BROKEN_HEART` |
| 01 | `LIST` | 23 | `ASSIGNMENT` |
| 02 | `CHARACTER` | 24 | `HUNK3_B` |
| 03 | `SCODE_QUOTE` | 25 | `TAGGED_OBJECT` |
| 04 | `COMPILED_RETURN` | 26 | `COMBINATION` |
| 05 | `UNINTERNED_SYMBOL` | 27 | `MANIFEST_NM_VECTOR` |
| 06 | `BIG_FLONUM` | 28 | `COMPILED_ENTRY` |
| 08 | `CONSTANT` | 29 | `LEXPR` |
| 09 | `EXTENDED_PROCEDURE` | 2B | `EPHEMERON` |
| 0A | `VECTOR` | 2C | `VARIABLE` |
| 0B | `RETURN_CODE` | 2D | `THE_ENVIRONMENT` |
| 0D | `MANIFEST_CLOSURE` | 2E | `SYNTAX_ERROR` |
| 0E | `BIG_FIXNUM` (bignum) | 2F | `VECTOR_1B` (`BIT_STRING`) |
| 0F | `PROCEDURE` | 31 | `VECTOR_16B` |
| 10 | `ENTITY` | 32 | `REFERENCE_TRAP` |
| 11 | `DELAY` | 33 | `BYTEVECTOR` |
| 12 | `ENVIRONMENT` | 34 | `CONDITIONAL` |
| 13 | `DELAYED` | 35 | `DISJUNCTION` |
| 14 | `EXTENDED_LAMBDA` | 36 | `CELL` |
| 15 | `COMMENT` | 37 | `WEAK_CONS` |
| 16 | `NON_MARKED_VECTOR` | 38 | `QUAD` |
| 17 | `LAMBDA` | 39 | `LINKAGE_SECTION` |
| 18 | `PRIMITIVE` | 3A | `RATNUM` |
| 19 | `SEQUENCE` | 3B | `STACK_ENVIRONMENT` |
| 1A | `FIXNUM` | 3C | `COMPLEX` |
| 1B | `UNICODE_STRING` | 3D | `COMPILED_CODE_BLOCK` |
| 1C | `CONTROL_POINT` | 3E | `RECORD` |
| 1D | `INTERNED_SYMBOL` | 20 | `HUNK3_A` |
| 1E | `CHARACTER_STRING` | 21 | `DEFINITION` |
| 1F | `ACCESS` | | |

Codes 07, 0C, 2A, 30 and 3F are unused. `const.h` statically asserts
that `FALSE`, `CONSTANT`, `FIXNUM`, `BROKEN_HEART` and
`CHARACTER_STRING` keep the values 0x00, 0x08, 0x1A, 0x22 and 0x1E,
because other code depends on them.

### Immediates

- **Fixnum**: `TC_FIXNUM` with a two's-complement datum
  (`LONG_TO_FIXNUM`, `FIXNUM_TO_LONG`).
- **Character**: `TC_CHARACTER`; datum is `(bucky-bits << 21) | code`.
  Code length is 21 bits, bucky bits are 4 (meta 1, control 2, super 4,
  hyper 8).
- **Constants** (`TC_CONSTANT` unless noted): `#f` is
  `MAKE_OBJECT(TC_FALSE, 0)`, the all-zero word. `#t` is datum 0,
  unspecific is 1, then default-object 7, the empty list 9, eof 6, and
  the lambda-list markers `#!optional` 3, `#!rest` 4.
  `BROKEN_HEART_ZERO` is `(TC_BROKEN_HEART, 0)`.
- The global environment is represented by the type of `#f`
  (`GLOBAL_ENV`).

### Heap-allocated numbers

A flonum is `TC_BIG_FLONUM`: a header word followed by the `double`.
Bignums are `TC_BIG_FIXNUM` (`bignum.c`, `bigprm.c`); ratnums and
complexes are heap tuples.

## 2. The interpreter

`Interpret()` in `interp.c` is written in continuation-passing style;
the comment at the top of the file explains the design. It runs SCode
directly.

**Registers** are a block, `Registers[]` (`extern.h`), with indices in
`const.h`:

| Index | Register | Index | Register |
| --- | --- | --- | --- |
| 0 | `MEMTOP` | 7 | `LEXPR_ACTUALS` |
| 1 | `INT_MASK` | 8 | `PRIMITIVE` |
| 2 | `VAL` | 9 | `CLOSURE_FREE` |
| 3 | `ENV` | 10 | `CLOSURE_SPACE` |
| 4 | `CC_TEMP` | 11 | `STACK_GUARD` |
| 5 | `EXPR` | 12 | `INT_CODE` |
| 6 | `RETURN` | 13 | `REFLECT_TO_INTERFACE` |

The minimum block length is 14. On AArch64 native builds slots 14
(`REGBLOCK_CC_DELTA`) and 15 (`REGBLOCK_CC_SHADOW_START`) are added
(`cmpintmd/aarch64.h`). Compiled code addresses this block through a
dedicated register, so slot numbers are ABI shared with the compiler.

**Stack.** The Scheme stack is separate from the C stack and grows
downward. `STACK_PUSH` pre-decrements `stack_pointer`; `STACK_REF(n)` is
`stack_pointer[n]` (`stack.h`). The guard zone is 4096 words
(`STACK_GUARD_SIZE`); running into it raises `INT_Stack_Overflow`.

**Continuations.** A continuation is two words: a return-code object
(`TC_RETURN_CODE`, an `RC_*` value) and the expression to resume with.
`SAVE_CONT` pushes the expression, then the return code (`interp.h`).

**Apply frames** on the stack are `[header (n_args+1)][procedure][args...]`.

**Control flow.**
- Entry is `setjmp(interpreter_catch_env)` plus a switch on the dispatch
  code (`PRIM_APPLY`, `PRIM_DO_EXPRESSION`, ...). This is how primitives
  and errors return into the interpreter.
- `do_expression` switches on `OBJECT_TYPE(GET_EXP)`. Self-evaluating
  types return immediately. SCode cases are `ACCESS`, `ASSIGNMENT`,
  `COMBINATION`, `COMMENT`, `CONDITIONAL`, `DEFINITION`, `DELAY`,
  `DISJUNCTION`, `LAMBDA`/`EXTENDED_LAMBDA`/`LEXPR`, `SCODE_QUOTE`,
  `SEQUENCE`, `THE_ENVIRONMENT`, `VARIABLE`, and `COMPILED_ENTRY`.
  A combination evaluates its operands right to left, pushing them, and
  finishes with `RC_COMB_APPLY_FUNCTION`.
- `pop_return` is the return dispatcher, a switch on `RC_*` codes
  (`RC_COMB_SAVE_VALUE`, `RC_CONDITIONAL_DECIDE`, `RC_INTERNAL_APPLY`,
  `RC_END_OF_COMPUTATION`, ...).
- `internal_apply` first checks for pending interrupts, then dispatches
  on the operator type: `ENTITY`, `RECORD` (applicable records),
  `PROCEDURE`, `CONTROL_POINT` (continuations), `PRIMITIVE` (arity check,
  then `APPLY_PRIMITIVE_FROM_INTERPRETER`), `EXTENDED_PROCEDURE`, and
  `COMPILED_ENTRY` (hands control to native code).

**Interrupts** (`intrpt.h`) are bits in `INT_CODE` filtered by
`INT_MASK`; `PENDING_INTERRUPTS()` is `mask & code`. Stack overflow
0x1, global GC 0x2, GC 0x4, character 0x10, after-GC 0x20, timer 0x40,
suspend 0x100. `setup_interrupt` (`utils.c`) builds a call to the
handler stored in the fixed-objects vector and re-enters apply.

## 3. Memory, garbage collection and bands

### Layout

`setup_memory` (`memmag.c`) allocates **one block** and
`reset_allocator_parameters` carves it into
`[stack][constant space][heap]`. `--heap`, `--stack` and `--constant`
are counted in **1024-word blocks** (8 KiB each on this platform). The
whole allocation must fit under `DATUM_MASK`.

### Collector

The collector is stop-and-copy (`memmag.c`, `gcloop.c`, `gccode.h`).
`GARBAGE-COLLECT` copies live data into a temporary `malloc`ed tospace
and then copies it back over the heap. Roots are the fixed-objects
vector, the history register, the stack, and constant space. Handlers
are a per-type table (`gc_table_t`) built by `initialize_gc_table`;
broken hearts (`TC_BROKEN_HEART`) are forwarding pointers. Weak pairs
and ephemerons get special handling after the main copy. After
collection the runtime's GC daemon runs from
`fixed_objects[GC_DAEMON]`.

`PRIMITIVE-PURIFY` (`purify.c`) copies an object graph into constant
space. The collector treats constant space as roots but never moves it.

### FASL files and bands

`fasl.h` defines the header: 50 words starting with the marker
`0xFAFAFAFAFAFAFAFA`.

| Slot | Contents | Slot | Contents |
| --- | --- | --- | --- |
| 0 | marker | 10 | compiled-code interface version |
| 1 | heap size | 11 | utilities base |
| 2 | heap base | 13 | C code count |
| 3 | dumped object | 14 | C code size |
| 4 | constant size | 15 | `memory_base` |
| 5 | constant base | 16 | stack size |
| 6 | version | 17 | heap reserved |
| 7 | stack start | 18 | ephemeron count |
| 8 | primitive count | 19 | `CC_DELTA` (diagnostic) |
| 9 | primitive table size | | |

A **band** is a FASL file with the band flag set: a raw heap and
constant-space image plus the primitive table. Relevant primitives:

- `DUMP-BAND*` (`fasdump.c`) takes a procedure and a filename. The saved
  band, when loaded, calls that procedure with `#f`.
- `LOAD-BAND` (`fasload.c`) reads and installs a band. It relocates only
  if the load addresses, memory base or primitive numbering differ from
  the dump, and always relocates when `cc_exec_delta != 0`. After the
  point of no return a failure ends in `TERM_DISK_RESTORE`.
- `BINARY-FASLOAD` loads ordinary FASL files.

Primitive tables are stored **by name and arity**, so primitive numbers
may differ between builds and fasload renumbers references.

## 4. Primitives

### Writing one

```c
DEFINE_PRIMITIVE ("MY-PRIM", Prim_my_prim, 2, 2,
  "(A B)\nDocumentation string, or 0.")
{
  PRIMITIVE_HEADER (2);
  CHECK_ARG (1, FIXNUM_P);
  long a = arg_integer (2);
  PRIMITIVE_RETURN (LONG_TO_FIXNUM (a));
}
```

- Arity is `min, max`; use `LEXPR` for variable arity.
- `PRIMITIVE_HEADER(n)` comes first. Arguments are `ARG_REF(i)`,
  **1-based**. Helpers: `CHECK_ARG`, `arg_integer`, `arg_index_integer`,
  `arg_ulong_integer`, `STRING_ARG`.
- Errors: `error_wrong_type_arg(n)`, `error_bad_range_arg(n)`.
- Return with `PRIMITIVE_RETURN(v)`; `PRIMITIVE_ABORT(PRIM_*)` for
  non-standard returns. If you allocate, call
  `Primitive_GC_If_Needed(n)` first.

### How primitives get registered

There is no registration call. At build time the host program
`findprim.c` textually scans every source in `STD_SOURCES` for
`DEFINE_PRIMITIVE` and writes `usrdef.c`: the static name, arity,
function and documentation tables (`makegen/Makefile.in.in`, rule
`usrdef.c: $(STD_SOURCES) findprim`). `initialize_primitives`
(`primutl.c`) installs them at startup. A primitive object is
`MAKE_OBJECT(TC_PRIMITIVE, index)`.

### Adding one

1. Put it in an existing file in the right group, or create one. Groups:
   `prosXXX.c` (OS-independent OS primitives), `pruxXXX.c` (Unix), and
   core files such as `list.c`, `vector.c`, `string.c`.
2. For a new file, add its base name to the appropriate list in
   `makegen/` (`files-core.scm`, `files-os-prim.scm`, `files-unix.scm`
   or `files-optional.scm`) and regenerate the Makefile. `Setup.sh` and
   `configure` do this; the exact minimal regeneration command was not
   verified, so if in doubt re-run them.
3. Rebuild the microcode. `usrdef.c` is regenerated by the Makefile.
   Do not edit `prims.h`; it holds macros only.
4. Call it from Scheme as `((ucode-primitive my-prim 2) a b)`.

Older bands keep loading on a newer microcode because of name-based
primitive tables.

## 5. Apple Silicon specifics

The W^X design is in `PORTING-APPLE-SILICON.md`; the code map is in
[COMPILER.md section 4](COMPILER.md#4-apple-silicon-the-shadow-mapped-heap).
Microcode-side summary:

- `ux.c` `setup_cc_exec_shadow` builds the shadow with `mach_vm_remap`,
  first requesting the address heap + 4 GiB (`VM_FLAGS_FIXED`), falling
  back to `VM_FLAGS_ANYWHERE`. It then `mprotect`s the shadow
  read/execute, and `mmap_heap_malloc_try` leaves `PROT_EXEC` off the
  primary mapping. Failure prints
  "unable to map executable shadow of heap -- native code will fail".
- `cmpint.h` documents the conventions (canonical tagged addresses,
  shadow raw PCs, delta-free stored offsets) and defines
  `CC_PC_TO_CANONICAL` / `CC_CANONICAL_TO_PC`.
- `cmpintmd/aarch64.c` flushes the instruction cache through
  `sys_icache_invalidate` on macOS.
- `uxtrap.c` normalizes trap PCs through `CC_PC_TO_CANONICAL`.
- The FASL header carries `cc_exec_delta` for diagnostics only; the
  loader never consults it.

Other Darwin adaptations: `configure.ac` sets the SDK/sysroot and
`-DSIGNAL_HANDLERS_CAN_USE_SCHEME_STACK`, passes `-P __APPLE__,1` to m4
for symbol prefixes, and builds the `macosx-starter` helper. `ux.h`
undefines `HAVE_POLL` and forces `fork` over `vfork`; `uxterm.c` disables
`posix_openpt`; `uxsig.c` sets `SA_64REGSET`. `floenv.h` enables the
`fegetexcept` workaround only for `__APPLE__ && __x86_64__`, which is
why `microcode/test-flonum-except` fails on Apple Silicon.

### Known stale comments

Some comments predate the delta-free-offset design and describe fasload
biasing PC offsets. They are wrong; `cmpint.h` and the code are
authoritative:

- `fasload.c` near the relocation sweep ("biases the compiled entries' PC
  offsets").
- `cmpintmd/aarch64.c` in the cache-flush comments ("belongs in the
  fasload relocation sweep").
- `cmpintmd/aarch64.h` near `CC_ENTRY_ADDRESS_PC`.

### Possible gap

`uxtrap.h` defines the `SIGCONTEXT_*` accessors for Darwin only under
`__IA32__` and `__x86_64__`. No AArch64 Darwin branch was found, so how
hardware-trap PC/SP decoding behaves on Apple Silicon has not been
traced. Worth checking before relying on trap-driven behavior.

## 6. Command line and environment (`option.c`)

Options are case-insensitive, accept `-` or `--`, and must precede the
band's own options.

| Option | Meaning |
| --- | --- |
| `--library PATH` / `--prepend-library DIR` | Library search path |
| `--band FILE` | Band to load |
| `--fasl FILE` | Cold-load a FASL file (conflicts with `--band`) |
| `--heap N`, `--stack N`, `--constant N` | Sizes in 1024-word blocks |
| `--batch-mode`, `--quiet`, `--silent` | Suppress banner and prompts (all set `option_batch_mode`) |
| `--emacs`, `--interactive`, `--nocore` | Interface modes |
| `--macosx-application` | (Apple) prepend the app bundle directory to the library path |
| `--option-summary`, `--help`, `--version` | Informational |

| Variable | Meaning and default |
| --- | --- |
| `MITSCHEME_LIBRARY_PATH` | Colon-separated; default `/usr/local/lib/mit-scheme` |
| `MITSCHEME_BAND` | Default band. Otherwise `all.com`, then first found of `runtime.com`, `mechanics.com`, `edwin-mechanics.com` |
| `MITSCHEME_HEAP_SIZE` | Blocks; default 16384 (128 MiB) on 64-bit |
| `MITSCHEME_STACK_SIZE` | Blocks; default 1024 |

There is no variable for constant space (default 1024 blocks, or the
band's own size).

**The effective heap is the requested heap plus the band's own heap.**
When the band header is readable, `option_heap_size + band_heap_size` is
used. So `--heap 500000` (as used for the test suite) is *on top of* the
band.

## 7. Unix OS interface

| Layer | Files |
| --- | --- |
| Core wrappers | `ux.c` (`UX_*`, `OS_malloc`, heap mapping), `uxtop.c`, `uxenv.c`, `uxutil.c` |
| I/O | `uxio.c` (channels, select/poll), `uxfile.c`, `uxfs.c`, `uxsock.c`, `uxentropy.c` |
| Processes | `uxproc.c` (subprocess table, job-control status, death hook) |
| Terminals | `uxtty.c`, `uxctty.c`, `uxterm.c` (including pty allocation) |
| Signals and traps | `uxsig.c`, `uxtrap.c` (per-OS `SIGCONTEXT_*`) |
| Portable OS interface | `os.h`, `osio.h`, `osproc.h`, `osfs.h`, ... |
| OS-independent primitives | `prosenv.c`, `prosfile.c`, `prosfs.c`, `prosio.c`, `prosproc.c`, `prospty.c`, `prosterm.c`, `prostty.c` |
| Unix primitives | `pruxenv.c`, `pruxfs.c`, `pruxio.c` (its header notes it should be called `pruxproc.c`), `pruxsock.c` |
| Dynamic loading and FFI | `pruxdld.c`, `pruxffi.c` |

Windows equivalents are the `nt*.c` files.
