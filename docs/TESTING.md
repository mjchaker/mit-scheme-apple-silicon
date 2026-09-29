# Testing guide

Tests live in `tests/`, driven by `tests/check.scm` and the framework in
`tests/unit-testing.scm`. See also the note in the top-level README on
the expected 96/97 result on Apple Silicon.

## 1. Running

From `src/`, after a build:

```sh
make check                                   # everything
TEST=runtime/test-arith make check           # one test
```

`make check` runs
`./run-build --batch-mode --load ../tests/check.scm --eval '(%exit)'`.
On Apple Silicon use the fuller form, which sets a big-enough heap and
shrinks the stress tests:

```sh
FAST=y ./run-build --heap 500000 --batch-mode \
    --load ../tests/check.scm --eval '(%exit)'
```

- `TEST=` must equal a `known-tests` entry exactly; otherwise you get
  `Unknown test name:` and nothing runs.
- `FAST` is not a `check.scm` switch: `keep-it-fast!?`
  (`unit-testing.scm`) is true iff `FAST` is set and non-empty. Tests such
  as test-promise, test-char-set and test-hash-table use it to shrink
  workloads. Without it a warning says "To avoid long run times, export
  FAST=y."
- `DEBUG=y` starts the debugger on error conditions.
- Output ends with a `Test results: PASSED: / FAILED:` list.

Directories: `tests/{compiler,microcode,runtime,libraries,ffi,sos,star-parser,xml}`.

## 2. The known-tests list

Tests are **not** auto-discovered. Add every new `tests/*/test-*.scm` to
`known-tests` in `tests/check.scm`. Entry forms:

| Form | Meaning |
| --- | --- |
| `"runtime/test-arith"` | Run in a fresh test environment |
| `("runtime/test-char" (runtime))` | Run in the named package's environment |
| `("runtime/test-equals" inline)` | Inline-format file (expression / `'expect-...` pairs, optional leading `(import ...)`) run by `run-inline-tests`; used by the SRFI 1xx tests |
| `"runtime/test-flonum.com"` / `.bin` | A specific compiled variant (`.com` maps to `.so` in C builds) |
| `"microcode/test-flonum-casts.scm"` | An interpreted variant |

`normal-test` compiles the file with `compile-file` first if it has no
file type.

## 3. Writing a test

```scheme
(define-test 'my-feature
  (lambda ()
    (assert-equal (+ 1 2) 3)
    (assert-error (lambda () (car 5)) (list condition-type:wrong-type-argument))))
```

`(define-test 'name thunk ...)` takes a thunk or a nested list of
thunks; sub-tests are numbered `name.0`, `name.1`, and so on.
Test files are loaded into an environment created by
`make-test-environment!`, into which everything defined with
`define-for-tests` is bound.

### Assertions

| Kind | Procedures |
| --- | --- |
| Truth | `assert-true`, `assert-false`, `assert-null`, `assert-pair`, `assert-list`, `assert-non-empty-list` |
| Errors | `assert-error thunk [condition-types . props]`, `assert-simple-error`, `assert-type-error`, `assert-range-error`, `error-assertion` |
| Identity / equality | `assert-eq`, `assert-eqv`, `assert-equal` (negated: `assert-!eq`, `assert-!eqv`, `assert-!equal`) |
| Numeric | `assert-=`, `assert-!=`, `assert-<`, `assert-<=`, `assert->`, `assert->=` |
| Booleans | `assert-boolean=`, `assert-boolean!=` |
| Characters | `assert-char=` `!=` `<` `<=` `>` `>=`, and the `-ci` forms |
| Strings | `assert-string=` `!=` `<` `<=` `>` `>=`, and the `-ci` forms |
| Membership | `assert-memq`, `assert-memv`, `assert-member`, and `!` forms |
| Lists / sets | `assert-list=`, `assert-list!=`, `assert-lset=`, `assert-lset!=` (comparator first) |
| Patterns | `assert-matches`, `assert-!matches` |
| Generic | `value-assert`, `predicate-assertion`, `fail`, `with-test-properties`, `define-comparator` |
| Expected failure | `expect-failure`, `expect-error` (alias of `assert-error`) |
| Timeouts | `carefully` runs a procedure in a thread with a 2000 ms timeout and stack-overflow handling |

### Framework parameters and drivers

`show-passing-results?` prints passes; `throw-test-errors?` disables
failure capture. Drivers: `run-unit-test filename test-name [env]`,
`run-unit-tests filename [env]`, `load-unit-tests`, `register-test`,
`run-inline-tests`. `check.scm` loads `tests/load.scm`, which loads the
framework into the `(mit inline-testing)` library environment
(`src/libraries/inline-testing.sld`) and links the drivers into the
system-global environment.

Failures are `<failure>` records; `report-result` prints
`;name failed N sub-tests out of M`.

## 4. Running one file by hand

```sh
cd tests
../src/run-build --batch-mode --load check.scm --eval '(%exit)'   # with TEST=... exported
```

## 5. Apple Silicon expectations

96 of 97 pass. `microcode/test-flonum-except` fails because AArch64
hardware does not trap floating-point exceptions; upstream's
`microcode/floenv.h` enables the trapping workaround only for
`__APPLE__ && __x86_64__`. It is not a port defect.

The task library has its own suite: `TEST=runtime/test-task make check`.
