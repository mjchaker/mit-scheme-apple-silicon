# Boot sequence, bands and the package system

How `mit-scheme` gets from `exec` to a REPL, how `runtime.com` and
`all.com` are made, and how `.pkg` files turn into runtime packages.
Paths are relative to the repository root; line numbers are hints.

Companion guides: [MICROCODE.md](MICROCODE.md), [BUILD.md](BUILD.md),
[RUNTIME.md](RUNTIME.md).

## 1. Starting up from a band

### Microcode (`src/microcode/boot.c`)

`main` runs, in order:

1. `read_command_line_options` — parses only the microcode's own
   options; the rest are saved for the runtime.
2. `setup_memory` — sizes come from `--heap`, `--stack`, `--constant`
   (1024-word blocks).
3. `initialize_primitives`, `compiler_initialize`, `OS_initialize`,
   then `start_scheme`.

`start_scheme` prints the banner (unless batch mode, `--version` or
`--help`), initializes the fixed-objects vector, and builds the first
expression to evaluate:

- With `--fasl FILE` (a *cold load*): `(SCODE-EVAL (BINARY-FASLOAD file)
  GLOBAL-ENV)`. This is how `runtime/make.com` builds `runtime.com`.
- Otherwise: `(LOAD-BAND <band>)`.

It then enters the interpreter. `LOAD-BAND` (`fasload.c`) installs the
band, clears `fixed_objects`, and continues at the band's saved restart
continuation.

**Band selection** (`option.c`): `--fasl` and `--band` conflict. With no
`--band`, the first file found on the library path from `all.com`,
`runtime.com`, `mechanics.com`, `edwin-mechanics.com`.
`MITSCHEME_BAND`, `MITSCHEME_LIBRARY_PATH`, `MITSCHEME_HEAP_SIZE` and
`MITSCHEME_STACK_SIZE` override defaults.

### Runtime restart (`src/runtime/savres.scm`)

After a band loads, the restart code runs `event:after-restore`, starts
the thread timer, then `abort->top-level`, which prints the world id
(unless batch mode) and runs `event:after-restart`.
`process-command-line` (`command-line.scm`) is registered on
`event:after-restart`; that is where `--load`, `--eval`,
`--no-init-file` and friends take effect.

`disk-save` (`savres.scm`) dumps a new band at runtime through the
`DUMP-BAND*` primitive.

## 2. Cold load: how `runtime.com` is built (`src/runtime/make.scm`)

`make.scm` runs under `--fasl` in a bare microcode with no runtime.
Everything must be bootstrapped by hand:

| Lines (approx.) | Step |
| --- | --- |
| 31-56 | Disable interrupts, define `define-multiple` and `*make-environment` |
| 57-128 | Alias low-level primitives; define `tty-write-string`, `fatal-error` |
| 130-152 | Install GC, stack-overflow and hardware-trap handlers; enable interrupts |
| 154-260 | Boot helpers: `fasload`, `file->object` (tries `.so` via `initialize-c-compiled-block`, then `.com`, then `.bin`), a minimal `eval`, package-init helpers |
| ~301 | Load `packag` into a private environment and call its `initialize-package!` to create the root package; export selected names globally |
| 330-341 | Load `runtime-unx.pkd` and `construct-packages-from-file` to build every package and link |
| 351-436 | Hand-ordered boot in three file groups (below) |
| 444-464 | Bulk load of every remaining file of every package; trigger `seq:after-files-loaded` to start the initialization cascade |
| 467-475 | `finish-host-library-db!`, `initialize-error-hooks!`, optional `site` file |
| 477-531 | Link a few names, `gc-clean`, `purify`, `initialize-synthetic-libraries!` |
| 535-540 | Add `(user)` package, start the thread timer, enter `initial-top-level-repl` |

The three hand-ordered groups:

- `files0`: gcdemn, gc, boot-seq, boot, generator, weak-pair, queue,
  equals, vector, procedure, list, primitive-arithmetic, srfi-1,
  thread-low.
- `files1`: msort, string, bytevector-low, symbol, random,
  dispatch-tag, poplat, prop1d, record.
- `files2`: bundle, syntax-low, thread, wind, events, gcfinal.

If a new runtime file must exist before the rest of the system can
initialize, it belongs in one of these lists. Otherwise it does not.

## 3. Boot sequencing (`src/runtime/boot-seq.scm`)

Load order is not initialization order. Each package has a
*boot sequencer* (dependency-driven action graph; operators `add-before!`,
`add-action!`, `trigger!`, `inert?`).

- `(add-boot-init! thunk)` registers initialization work. During cold
  load it is queued on the current package's sequencer; after boot it
  runs immediately. About 60 runtime files use it.
- `(add-boot-deps! '(runtime foo) ...)` at the top of a file declares
  that its initialization must follow those packages'.
- Fixed sequencers such as `seq:after-printer`, `seq:after-record` and
  `seq:after-microcode-tables` are tied to specific packages.
- A handful of older files still define `(define (initialize-package!)
  ...)`: chrsyn, equals, format, gc, gcdemn, lambda, packag, process.
  These are called explicitly (from `make.scm`, or via a load option's
  `standard-option-loader`).

`src/runtime/boot.scm` defines the `boot-definitions` package (`%record`,
`%make-record`, interrupt bit constants, `define-print-method`).

## 4. Load options

`(load-option 'name)` (`src/runtime/option.scm`) searches a chain of
`optiondb.scm` files, starting from `MITSCHEME_LOAD_OPTIONS` or the first
`optiondb` on the library path and following a parent link.

Runtime options in `src/runtime/optiondb.scm`: `arithmetic-interface`,
`compress`, `format`, `mime-codec`, `ordered-vector`, `stepper`,
`subprocess`, `synchronous-subprocess`, `regular-expression`, plus the
dummies `hash-table`, `rb-tree`, `wt-tree`. Other subsystems ship their
own (`src/ffi/optiondb.scm`, and so on). The optional `.com` files
installed are listed in `RUNOPTS` in `src/runtime/Makefile-fragment`.

## 5. Bands and the `lib/` layout

Two bands matter:

- **`lib/runtime.com`** — the bare runtime. Built by running the
  microcode with `--fasl make.com` (or `make.so` in a C build) and
  evaluating `(disk-save "../lib/runtime.com")`
  (`src/Makefile.in`; `get_fasl_file` in `src/etc/functions.sh` picks
  the file).
- **`lib/all.com`** — `runtime.com` plus the compiler and SF:
  start from `--band runtime.com`, `(load-option 'compiler)`,
  `(load-option 'sf)`, `(disk-save "lib/all.com")`. It is the default
  band.

`src/Setup.sh` creates `lib/` as symlinks (`include`, `mit-scheme.h`,
`optiondb.scm`, and one per subsystem); the build adds the `.com` bands.

Installed layout: `$(libdir)/mit-scheme-ARCH-VERSION` holds
`optiondb.scm`, `plugins.scm`, `*.com`, and directories `runtime/`
(`*.pkd`, `*.bci`, option `.com` files), `libraries/` (`*.comld`,
`*.bcild`) and one directory per subsystem.

`src/run-build` sets `MIT_SCHEME_EXE`, `MITSCHEME_INF_DIRECTORY=$HERE`
and `MITSCHEME_LIBRARY_PATH=$HERE/lib`, then execs
`microcode/scheme "$@"`. That is how tests and build steps use the
just-built tree rather than an installed Scheme.

## 6. The package system

Packages are declared in `.pkg` files scattered through the tree
(`src/runtime/runtime.pkg` is the big one) and processed by `cref`
(`src/cref/redpkg.scm`).

### Forms

- `(define-package NAME option ...)`
- `(extend-package NAME option ...)`
- `(global-definitions "file" ...)`
- `(os-type-case ((nt) ...) ((unix) ...) (else ...))`
- `(include "other.pkg")`

### Package options

| Option | Meaning |
| --- | --- |
| `(files "a" "b")` | Source files, no extension |
| `(file-case KEY ((val ...) "f" ...) (else))` | Conditional files. The runtime uses `options` (`load` / `no-load`, so option files are skipped at cold load) and `os-type` |
| `(parent (runtime))` | Parent package (optional if the name's prefix implies it) |
| `(export TARGET sym \| (local exported) ...)` | Export bindings. Target `()` is the global environment. `(export deprecated ...)` marks them deprecated |
| `(import SOURCE sym ...)` | Bind a name from another package |
| `(initialization (expr))`, `(finalization (expr))` | Exactly one expression each; used mostly by Edwin, since the runtime uses boot sequencers |

Comments such as `;(scheme base)` after exported names record R7RS
library membership; `library-standard.scm` has the authoritative lists.

### Example (`runtime.pkg`)

```scheme
(define-package (runtime weak-pair)
  (files "weak-pair")
  (parent (runtime))
  (export ()
          clean-weak-alist!
          ...))
```

A non-global export target and an import look like:

```scheme
(export (runtime) get-fixed-objects-vector)
(import (runtime thread) with-obarray-lock)
```

### What cref produces

`(cref/generate-constructors "runtime")` (last line of `runtime.sf`)
writes:

- **`runtime-unx.pkd`** (or `-w32.pkd`; type `dkp` when
  cross-compiling) — a fasdumped vector
  `#(package-descriptions version descriptions loads)`. Naming comes from
  `package-set-pathname` in `src/runtime/packag.scm`.
- a **`.crf`** cross-reference listing.

There are no `*-pkg.scm` or `*-model` files; "model" is only an
internal name in `redpkg.scm`.

At runtime, `construct-packages-from-file` builds the packages,
`load-packages-from-file` loads each package's files, and
`initialize/finalize` evaluates `initialization` / `finalization`
expressions.

## 7. Adding a runtime file or package

1. Create `src/runtime/foo.scm`: copyright block, `;;;; Title`,
   `;;; package: (runtime foo)`, `(declare (usual-integrations))`.
2. Add to `runtime.pkg`:
   ```scheme
   (define-package (runtime foo)
     (files "foo")
     (parent (runtime))
     (export () my-proc))
   ```
   For a new file in an existing package, extend its `files` list.
3. If it needs ordering, add `(add-boot-deps! ...)` and
   `(add-boot-init! (lambda () ...))` in the file.
4. `runtime.sf` needs no per-file entry (it syntaxes the whole
   directory). Syntax order is not load order; `runtime.pkg` and
   `make.scm` decide that. Edit `files0/1/2` in `make.scm` only for
   early-boot files.
5. `make compile-runtime` (regenerates `runtime-unx.pkd`), then
   `make lib/runtime.com lib/all.com`.
6. **Optional (non-boot) file:** use `(file-case options ((load) "foo")
   (else))`, add a `define-load-option` in `optiondb.scm`, and add the
   name to `RUNOPTS` in `Makefile-fragment`.
7. For R7RS visibility, add names to the right `define-standard-library`
   list in `library-standard.scm`.
