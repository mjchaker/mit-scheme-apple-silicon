# Developer documentation

Guides for people working *on* MIT/GNU Scheme rather than in it. They
supplement, and do not replace, the upstream `src/README.txt`,
`src/compiler/README`, `src/compiler/documentation/` and the texinfo
manuals in `doc/`.

| Guide | Read it when you want to know... |
| --- | --- |
| [MICROCODE.md](MICROCODE.md) | How objects are represented, how the interpreter, GC and bands work, how to add a primitive, and the microcode side of the Apple Silicon port |
| [COMPILER.md](COMPILER.md) | The LIAR pipeline phase by phase, the AArch64 backend, register and calling conventions, the shadow-heap PC rules, compiler switches, and how to inspect output |
| [BOOT-AND-PACKAGES.md](BOOT-AND-PACKAGES.md) | What happens between `exec` and the REPL, how `runtime.com` and `all.com` are made, boot sequencers, the `.pkg` format, and how to add a runtime file |
| [BUILD.md](BUILD.md) | Build states, configure options, Makefile targets, cross builds, `src/etc` scripts, troubleshooting |
| [TESTING.md](TESTING.md) | Running tests, the known-tests list, writing tests, every assertion procedure |
| [RUNTIME.md](RUNTIME.md) | Command-line options, where each runtime subsystem lives, R7RS library support |

Existing documents at the repository root:
[PORTING-APPLE-SILICON.md](../PORTING-APPLE-SILICON.md) (design and
measurements for the port), [CONCURRENCY.md](../CONCURRENCY.md)
(structured concurrency), [UPSTREAM-BUGS.md](../UPSTREAM-BUGS.md).

## Accuracy notes

These guides were written from a reading of the source at the commit
they were added, not from a build, and line numbers are deliberately
omitted or approximate. Where a code comment and the code disagree the
guides follow the code and say so (see the "stale comments" list in
MICROCODE.md). Items that were not verified are marked as such.
