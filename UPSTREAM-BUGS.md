# Bugs found in MIT/GNU Scheme 12.1 while porting to Apple Silicon

Found 2026-07-18 while bootstrapping a cross-build on macOS 27
(arm64, host = official 12.1 x86-64 binary dist under Rosetta 2,
Apple clang 21).  All verified present in 12.1; items 1 and 2
verified still present in master.  Draft report for bug-mit-scheme.

## 1. Use-after-free in fasdump corrupts every dumped primitive table

`src/microcode/fasdump.c` (`prim_dump`): `make_prim_renumber`
registers `free_prim_renumber` with `tat_always`, so
`transaction_commit ()  /* 2 */` frees `current_pr`.  The header
initialization then reads `(current_pr->next_code)` to set
`FASLHDR_N_PRIMITIVES`.  This benign-looking stale read returned the
right value for years; macOS 26+'s zero-on-free allocator makes it
read 0.  Result: every dumped FASL has a populated primitive-table
*body* but declares 0 entries, so on load `new_prim_table` is used
uninitialized and primitive references resolve to garbage.  First
visible symptom in a build: `syntax-runtime` dies on `srfi-1.scm`
with an unprintable `bad-range-argument` from
`primitive-procedure-arity` (the datum is a primitive with a corrupt
name read from an `.ext` file).

Fix: capture `next_code` before the commit.  (One-line hoist; patch
available.)

## 2. x86-64 compiler emits illegal ternary IMUL for checked left shifts >= 32

`src/compiler/machines/x86-64/rulfix.scm`, `load-fixnum-constant`'s
shift case: a left shift with overflow detection is open-coded as
IMUL by `2^n` (comment: "SHL fails to set the overflow flag for
n>1").  The ternary IMUL immediate must fit in 32 bits; for `n >=
32`, `with-unsigned-immediate-operand` materializes the constant in
a temporary register, producing `(IMUL Q r r r)`, which the
assembler rejects: `illegal instruction syntax (imul q (r 1) (r 1)
(r 0))`.

Trigger: compiling `machines/aarch64/rules3.scm`
(`make-closure-padded-format` does `(shift-left word 32)`), i.e. any
x86-64-hosted cross-build of the aarch64 compiler.  The multiply
case a few lines up already handles the fallback correctly with
binary `(IMUL Q target temp)`; the shift case should do the same.

## 3. Tools band dump runs out of heap with a misleading error

`Makefile.tools`'s `tools/compiler.com` / `tools/syntaxer.com`
recipes invoke the host Scheme without `$(HOST_COMPILER_HEAP)` (and
configure leaves that variable empty for most hosts).  Loading the
full compiled compiler into `tools/runtime.com` to `disk-save`
exhausts the default heap partway through, and the failure surfaces
as: `Attempt to read binary file .../fgopt/delint.com failed: either
it's not binary or the wrong version` — the file is fine and loads
standalone, which makes this look like fasl corruption rather than
allocation failure.  Suggested: pass `$(HOST_COMPILER_HEAP)` in
those recipes, default it non-empty, and make the fasload error
distinguish out-of-heap from bad-format.
