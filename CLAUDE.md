# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

MIT/GNU Scheme 12.1 source distribution: a Scheme interpreter/runtime written in C and Scheme, plus LIAR, a native-code compiler written in Scheme. MIT Scheme is self-hosting — **building it requires an already-installed compatible mit-scheme binary release** (the "host scheme"). If the host scheme is installed somewhere unusual, set `MIT_SCHEME_EXE` to its command name.

## Build commands

All build commands run in `src/`:

```sh
cd src
./Setup.sh      # fresh -> distribution state (only needed for git checkouts; already done in a source tarball)
./configure     # distribution -> configured
make            # configured -> compiled
make install
```

Cleaning: `make clean` (back to configured), `make distclean` (back to distribution), `make maintainer-clean` (back to fresh).

Useful intermediate targets: `make compile-runtime`, `make compile-sf`, `make compile-compiler`, `make toolchain` (see `src/Makefile.in`).

## Tests

From `src/` after a successful build:

```sh
make check                          # runs all tests via ../tests/check.scm
TEST=runtime/test-arith make check  # run a single test (name from the known-tests list)
```

The test harness is `tests/check.scm` + `tests/unit-testing.scm` (a Scheme unit-test framework with `define-test`). Tests are **not** auto-discovered: a new `tests/*/test-*.scm` file must be added to the `known-tests` list in `tests/check.scm`. Entries there are either a bare string (run in default environment) or `(name (package))` to run in a specific package environment.

`src/run-build` is a wrapper script that runs the freshly built `src/microcode/scheme` with the in-tree library path; the test target uses it, so tests exercise the just-built tree, not the installed scheme.

## Architecture

Detailed description in `src/README.txt`; compiler internals in `src/compiler/README`.

### Core (under `src/`)

- **microcode/** — the C core. Compiles to the `scheme` executable: interpreter, GC, primitives, OS interface.
- **runtime/** — the runtime library in Scheme (most of what the reference manual documents). Package/module structure is declared in `runtime/runtime.pkg`.
- **sf/** — "Scheme->SCode" translator with optimizations (beta substitution, early binding). SCode is the interpreter's internal representation.
- **compiler/** — LIAR, the native-code compiler (SCode → machine code). Pipeline: `fggen` (SCode → Flow Graph) → `fgopt` (FG optimization; most Scheme-specific technology) → `rtlgen` (FG → RTL) → `rtlopt` (CSE, lifetime analysis, register allocation) → `back` (RTL → assembly). `base/toplev.scm` is the top-level driver; `machines/` holds per-target backends.
- **cref/** — cross-reference tool that also implements the module ("package") system driven by `*.pkg` files found throughout the tree.

### Extensions and plugins

- **sos/** (object system), **star-parser/** (parser language), **xml/**, **ffi/** (C FFI syntax).
- C FFI plugins that replace microcode-module packages: **gdbm/**, **blowfish/**, **pgsql/**, **x11/**.
- **edwin/** (Emacs-like editor in Scheme) and **imail/** (mail reader for Edwin).
- **libraries/** — R7RS library support.

### Conventions

- Scheme sources start with the copyright block comment and commonly `(declare (usual-integrations))`.
- Each buildable subdirectory has its own `Setup.sh`, `Clean.sh`, `Makefile-fragment` or `Makefile.in`; the top-level `src/Makefile.in` orchestrates them via `SUBDIRS`.
- Per-directory `.pkg` files define package boundaries and exports; changing what a file exports usually means editing the corresponding `.pkg` file.

### Other top-level directories

- **doc/** — texinfo manuals (`ref-manual/`, `user-manual/`); has its own configure/Makefile.
- **tests/** — test suite (see above).
- **dist/**, **etc/** — release/build machinery.

## Gotchas

- If compiler data structures have changed incompatibly, `make` cannot rebuild the compiler directly; see "Building an incompatible compiler" in `src/README.txt` for the manual band-building procedure.
- `./Setup.sh` needs a C compiler supporting `-M` for dependency generation.
