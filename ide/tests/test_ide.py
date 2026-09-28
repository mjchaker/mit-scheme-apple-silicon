"""Headless tests for the MIT Scheme IDE.

Run from the repository root:

    python3 -m unittest discover -s ide/tests -v

The last test class drives a real ``mit-scheme`` through the same
process/protocol code the GUI uses; it is skipped when no interpreter is
found (set MIT_SCHEME_EXE to point at one).
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import mit_scheme_ide as ide  # noqa: E402


class ProtocolParserTests(unittest.TestCase):
    BANNER = (b"MIT/GNU Scheme running under GNU/Linux\n\n\x1bw/home/u/proj/\x1b"
              b"Copyright (C) 2022\n\x1bp1 [Evaluator]\x1b\x1bR\x1bs")

    def test_banner(self):
        p = ide.EmacsProtocolParser()
        events = p.feed(self.BANNER)
        self.assertEqual(events, [
            ("output", "MIT/GNU Scheme running under GNU/Linux\n\n"),
            ("cd", "/home/u/proj/"),
            ("output", "Copyright (C) 2022\n"),
            ("prompt", (1, "[Evaluator]")),
            ("ready", None),
            ("read-start", None),
        ])

    def test_split_across_feeds(self):
        p = ide.EmacsProtocolParser()
        events = []
        for i in range(len(self.BANNER)):
            events += p.feed(self.BANNER[i:i + 1])
        merged = []
        for ev in events:                      # coalesce output fragments
            if ev[0] == "output" and merged and merged[-1][0] == "output":
                merged[-1] = ("output", merged[-1][1] + ev[1])
            else:
                merged.append(ev)
        self.assertEqual(merged, ide.EmacsProtocolParser().feed(self.BANNER))

    def test_value_and_error(self):
        p = ide.EmacsProtocolParser()
        self.assertEqual(p.feed(b"\x1bf\x1bv3\x1b"), [("read-finish", None), ("value", "3")])
        self.assertEqual(p.feed(b"\x1bfhi\x1bv\x1b"), [("read-finish", None), ("output", "hi"), ("value", "")])
        events = p.feed(b"\x1bf\n;The object ()\n\x1bz\x07\x1bp2 [Evaluator]\x1b\x1bR\x1bs")
        self.assertEqual(events, [
            ("read-finish", None), ("output", "\n;The object ()\n"), ("error", None),
            ("bell", None), ("prompt", (2, "[Evaluator]")), ("ready", None), ("read-start", None)])

    def test_hashed_value_via_elisp(self):
        p = ide.EmacsProtocolParser()
        events = p.feed(b"\x1bP#[compound-procedure 12 f]\x1b"
                        b'\x1bE(xscheme-write-message-1 xscheme-prompt (format ";Value 12: %s" xscheme-prompt))\x1b')
        self.assertEqual(events, [("value-text", ";Value 12: #[compound-procedure 12 f]")])
        events = p.feed(b'\x1bE(xscheme-write-message-1 "(no values)" ";No values")\x1b')
        self.assertEqual(events, [("value-text", ";No values")])
        events = p.feed(b'\x1bE(message "%s" "Hello \\"there\\"")\x1b')
        self.assertEqual(events, [("message", 'Hello "there"')])

    def test_interrupt_and_gc(self):
        p = ide.EmacsProtocolParser()
        self.assertEqual(p.feed(b"\x1bb\x1be\x07\x1bg"), [
            ("gc-start", None), ("gc-end", None), ("bell", None), ("interrupt-ok", None)])

    def test_utf8_output(self):
        p = ide.EmacsProtocolParser()
        data = "λ→ü".encode("utf-8")
        events = p.feed(data[:2]) + p.feed(data[2:])
        self.assertEqual("".join(e[1] for e in events if e[0] == "output"), "λ→ü")

    def test_prompt_text(self):
        self.assertEqual(ide.prompt_text(1, "[Evaluator]"), "1 ]=> ")
        self.assertEqual(ide.prompt_text(2, "[Evaluator]"), "2 error> ")
        self.assertEqual(ide.prompt_text(3, "[Debug]"), "3 debug> ")
        self.assertEqual(ide.prompt_text(2, "[Evaluator] foo>"), "2 foo> ")


class TokenizerTests(unittest.TestCase):
    def kinds(self, src, classify=None):
        return [(t.kind, t.text) for t in ide.tokenize(src, classify)]

    def test_basic_kinds(self):
        self.assertEqual(self.kinds("(define (fact n) n)"), [
            ("open", "("), ("keyword", "define"), ("open", "("), ("define-name", "fact"),
            ("symbol", "n"), ("close", ")"), ("symbol", "n"), ("close", ")")])
        self.assertEqual(self.kinds("(define x 42)"), [
            ("open", "("), ("keyword", "define"), ("define-name", "x"), ("number", "42"), ("close", ")")])

    def test_literals(self):
        self.assertEqual(self.kinds('"a\\"b" #\\a #\\space #\\( 3.5e-2 1/2 #xFF #t #!default -1 +inf.0'), [
            ("string", '"a\\"b"'), ("char", "#\\a"), ("char", "#\\space"), ("char", "#\\("),
            ("number", "3.5e-2"), ("number", "1/2"), ("number", "#xFF"), ("constant", "#t"),
            ("constant", "#!default"), ("number", "-1"), ("number", "+inf.0")])

    def test_comments(self):
        toks = self.kinds("; line\n(a #| block #| nested |# still |# b) #;(x) c")
        self.assertEqual(toks[0], ("comment", "; line"))
        self.assertEqual(toks[3], ("comment", "#| block #| nested |# still |#"))
        self.assertEqual(toks[6], ("comment", "#;"))
        self.assertEqual(toks[-1], ("symbol", "c"))

    def test_quotes(self):
        self.assertEqual(self.kinds("'foo `(a ,b ,@c)"), [
            ("quote", "'"), ("quoted", "foo"), ("quote", "`"), ("open", "("), ("symbol", "a"),
            ("quote", ","), ("symbol", "b"), ("quote", ",@"), ("symbol", "c"), ("close", ")")])
        self.assertEqual(self.kinds("`sym")[1], ("quoted", "sym"))

    def test_symbols_are_not_numbers(self):
        self.assertEqual(self.kinds("+ - ... 1+ -1+ a1 |odd sym|"), [
            ("symbol", "+"), ("symbol", "-"), ("symbol", "..."), ("symbol", "1+"),
            ("symbol", "-1+"), ("symbol", "a1"), ("symbol", "|odd sym|")])

    def test_classifier(self):
        toks = self.kinds("(map car lst)", lambda n: {"map": "builtin", "car": "builtin"}.get(n))
        self.assertEqual(toks[1:4], [("builtin", "map"), ("builtin", "car"), ("symbol", "lst")])

    def test_unterminated(self):
        for src in ['"abc', '(a "x', "#| never", "(a #| b"]:
            self.assertTrue(ide.unterminated_tail(src, ide.tokenize(src)), src)
        for src in ['"abc"', "#| x |#", "(a)", ""]:
            self.assertFalse(ide.unterminated_tail(src, ide.tokenize(src)), src)

    def test_complete_input(self):
        self.assertTrue(ide.is_complete_input("(+ 1 2)"))
        self.assertTrue(ide.is_complete_input("(+ 1 2) (display 3)"))
        self.assertTrue(ide.is_complete_input("hello there"))
        self.assertTrue(ide.is_complete_input(""))
        self.assertFalse(ide.is_complete_input("(+ 1"))
        self.assertFalse(ide.is_complete_input('(display "a'))
        self.assertFalse(ide.is_complete_input("(+ 1 2))"))

    def test_match_paren(self):
        toks = ide.tokenize("(a (b) c)")
        self.assertEqual(ide.match_paren(toks, 0), 6)
        self.assertEqual(ide.match_paren(toks, 6), 0)
        self.assertEqual(ide.match_paren(toks, 2), 4)
        self.assertIsNone(ide.match_paren(ide.tokenize("(a"), 0))

    def test_toplevel_form_at(self):
        src = "(a b)\n\n(c (d))\n"
        toks = ide.tokenize(src)
        self.assertEqual(ide.toplevel_form_at(toks, 8), (7, 14))   # inside the second form
        self.assertEqual(ide.toplevel_form_at(toks, 6), (0, 5))    # between forms: the previous one
        self.assertEqual(ide.toplevel_form_at(toks, 5), (0, 5))    # right after a form
        self.assertEqual(ide.toplevel_form_at(toks, 0), (0, 5))
        self.assertIsNone(ide.toplevel_form_at(ide.tokenize("  "), 1))

    def test_enclosing_operator(self):
        src = "(map (lambda (x) x) lst"
        self.assertEqual(ide.enclosing_operator(ide.tokenize(src), len(src)), ("map", 2))
        self.assertEqual(ide.enclosing_operator(ide.tokenize(src), 16), ("lambda", 1))
        self.assertEqual(ide.enclosing_operator(ide.tokenize("foo"), 3), (None, 0))
        self.assertEqual(ide.enclosing_operator(ide.tokenize("((a) b"), 6), (None, 0))

    def test_local_names(self):
        names = ide.local_names_from_tokens(ide.tokenize("(define (square x) (* x x)) (square other)"))
        self.assertEqual(names["square"], "defined")
        self.assertEqual(names["other"], "symbol")
        self.assertNotIn("x", names)


class IndentTests(unittest.TestCase):
    def check(self, src, expected):
        self.assertEqual(ide.compute_indent(src), expected, repr(src))

    def test_body_forms(self):
        self.check("(define (f x)", 2)
        self.check("  (define (f x)", 4)
        self.check("(lambda (x)", 2)
        self.check("(let ((a 1))", 2)
        self.check("(let loop ((i 0))", 2)
        self.check("(when x", 2)
        self.check("(with-values thunk", 2)
        self.check("(async-let ((x 1))", 2)

    def test_argument_alignment(self):
        self.check("(list 1", 6)
        self.check("(if (= n 0)", 4)
        self.check("(cond ((a) b)", 6)
        self.check("(+ 1\n   2", 3)
        self.check("(foo", 1)
        self.check("(cond", 1)

    def test_nested(self):
        self.check("(define (f x)\n  (let ((a 1)\n        (b 2))", 4)
        self.check("(define (f x)\n  (let ((a 1)", 8)
        self.check("(define (f x)\n  (if (a)\n      b", 6)
        self.check("(define (f x)\n  (list 1 2))", 0)
        self.check("(a)\n(b)", 0)

    def test_data(self):
        self.check("'(1 2", 2)
        self.check("(quote (a", 8)
        self.check("((a 1)", 1)
        self.check("(let ((a 1)", 6)

    def test_inside_string_or_comment(self):
        self.assertIsNone(ide.compute_indent('(display "hello'))
        self.assertIsNone(ide.compute_indent("(a #| comment"))
        self.check("(a ; comment", 1)

    def test_comments_between_args_are_ignored(self):
        self.check("(list ; c\n 1", 1)
        self.check("(list #| c |# 1", 14)


class CompletionIndexTests(unittest.TestCase):
    DUMP = (";;env\nsquare\tprocedure\t(x)\nmy-var\tvariable\n"
            ";;env\nmap\tprocedure\t2+\nmake-string\tprocedure\t1-2\ndefine\tmacro\n"
            "string-pad-left\tprocedure\t2-3\ncar\tprocedure\t(pair)\n")

    def setUp(self):
        self.idx = ide.CompletionIndex()
        self.idx.load_dump(self.DUMP, full=True)

    def test_fallback_before_load(self):
        idx = ide.CompletionIndex()
        self.assertFalse(idx.loaded)
        self.assertEqual(idx.classify("define"), "keyword")
        self.assertIn("define", [n for n, _, _ in idx.complete("def")])

    def test_prefix_then_infix(self):
        names = [n for n, _, _ in self.idx.complete("ma")]
        self.assertEqual(names[:2], ["map", "make-string"])
        self.assertEqual(names, ["map", "make-string"])
        names = [n for n, _, _ in self.idx.complete("pad")]
        self.assertEqual(names, ["string-pad-left"])
        self.assertEqual(self.idx.complete(""), [])

    def test_kinds_and_describe(self):
        self.assertEqual(self.idx.classify("map"), "builtin")
        self.assertEqual(self.idx.classify("define"), "keyword")
        self.assertIsNone(self.idx.classify("my-var"))
        self.assertEqual(self.idx.describe("square"), "(square x)")
        self.assertEqual(self.idx.describe("car"), "(car pair)")
        self.assertEqual(self.idx.describe("map"), "map: procedure, 2+ arguments")
        self.assertEqual(self.idx.describe("define"), "define: special form")
        self.assertIsNone(self.idx.describe("nope"))

    def test_partial_dump_keeps_global(self):
        self.idx.load_dump(";;env\ncube\tprocedure\t(x)\n", full=False)
        self.assertEqual(self.idx.kind("cube"), "procedure")
        self.assertIsNone(self.idx.kind("square"))
        self.assertEqual(self.idx.kind("map"), "procedure")

    def test_user_definition_shadows_global(self):
        self.idx.load_dump(";;env\nmap\tprocedure\t(f l)\n", full=False)
        self.assertEqual(self.idx.describe("map"), "(map f l)")

    def test_local_names(self):
        self.idx.set_local_names({"helper": "defined", "map": "symbol"})
        self.assertEqual(self.idx.kind("helper"), "defined")
        self.assertEqual(self.idx.kind("map"), "procedure")
        self.assertEqual(self.idx.describe("helper"), "helper: defined in this buffer")

    def test_dump_expression_escapes_path(self):
        expr = ide.dump_expression('/tmp/we"ird\\dir/x.tsv', skip_global=True)
        self.assertIn('"/tmp/we\\"ird\\\\dir/x.tsv"', expr)
        self.assertIn("(skip-global? #t)", expr)
        self.assertTrue(ide.is_complete_input(expr))


class MiscTests(unittest.TestCase):
    def test_scheme_string(self):
        self.assertEqual(ide.scheme_string('a"b\\c'), 'a\\"b\\\\c')

    def test_parse_args(self):
        a = ide.parse_args(["--scheme", "/x/mit-scheme", "f.scm", "--", "--heap", "1000"])
        self.assertEqual((a.scheme, a.file, a.scheme_args), ("/x/mit-scheme", "f.scm", ["--heap", "1000"]))
        a = ide.parse_args([])
        self.assertEqual((a.scheme, a.file, a.scheme_args), (None, None, []))
        a = ide.parse_args(["f.scm", "--scheme", "/x/mit-scheme"])
        self.assertEqual((a.scheme, a.file, a.scheme_args), ("/x/mit-scheme", "f.scm", []))
        a = ide.parse_args(["--", "--band", "x.com"])
        self.assertEqual((a.scheme, a.file, a.scheme_args), (None, None, ["--band", "x.com"]))

    def test_find_executable_explicit_missing(self):
        with tempfile.TemporaryDirectory() as d:
            saved = os.environ.pop("MIT_SCHEME_EXE", None)
            saved_path = os.environ["PATH"]
            try:
                os.environ["PATH"] = d
                self.assertIsNone(ide.find_scheme_executable(os.path.join(d, "nope")))
                exe = os.path.join(d, "mit-scheme")
                with open(exe, "w") as f:
                    f.write("#!/bin/sh\n")
                os.chmod(exe, 0o755)
                self.assertEqual(ide.find_scheme_executable(), exe)
            finally:
                os.environ["PATH"] = saved_path
                if saved is not None:
                    os.environ["MIT_SCHEME_EXE"] = saved


SCHEME = ide.find_scheme_executable()


@unittest.skipUnless(SCHEME, "no mit-scheme executable found")
class LiveInterpreterTests(unittest.TestCase):
    """Drive a real interpreter through SchemeProcess + the parser."""

    def setUp(self):
        self.proc = ide.SchemeProcess(SCHEME)
        self.parser = ide.EmacsProtocolParser()
        self.events = []
        self.cursor = 0
        self.wait_for("ready", timeout=20)

    def tearDown(self):
        self.proc.terminate()

    def pump(self):
        for chunk in self.proc.read_chunks():
            if chunk is None:
                self.events.append(("eof", None))
            else:
                self.events += self.parser.feed(chunk)

    def reset(self):
        self.events = []
        self.cursor = 0

    def wait_for(self, kind, timeout=10):
        """Return the next unconsumed event of ``kind``, pumping the process."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.pump()
            while self.cursor < len(self.events):
                ev = self.events[self.cursor]
                self.cursor += 1
                if ev[0] == kind:
                    return ev
            time.sleep(0.02)
        self.fail("timed out waiting for %s; got %r" % (kind, self.events))

    def output_text(self):
        return "".join(a for k, a in self.events if k == "output")

    def test_eval_value(self):
        self.reset()
        self.proc.send("(+ 1 2)\n")
        self.assertEqual(self.wait_for("value"), ("value", "3"))
        self.assertEqual(self.wait_for("prompt"), ("prompt", (1, "[Evaluator]")))

    def test_output_then_unspecified(self):
        self.reset()
        self.proc.send('(display "hi")\n')
        self.wait_for("ready")
        self.assertEqual(self.output_text(), "hi")
        self.assertIn(("value", ""), self.events)

    def test_error_enters_level_2_and_restart(self):
        self.reset()
        self.proc.send("(car '())\n")
        self.wait_for("error")
        self.assertEqual(self.wait_for("prompt"), ("prompt", (2, "[Evaluator]")))
        self.assertIn("not the correct type", self.output_text())
        self.reset()
        self.proc.send("(restart 1)\n")
        self.assertEqual(self.wait_for("prompt"), ("prompt", (1, "[Evaluator]")))

    def test_interrupt(self):
        self.reset()
        self.proc.send("(let loop () (loop))\n")
        self.wait_for("read-finish")
        time.sleep(0.3)
        self.proc.interrupt()
        self.wait_for("interrupt-ok", timeout=10)
        self.assertEqual(self.wait_for("prompt"), ("prompt", (1, "[Evaluator]")))
        self.assertIn(";Quit!", self.output_text())

    def test_completion_dump(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "dump.tsv")
            self.proc.send("(define (square x) (* x x))\n")
            self.wait_for("ready")
            self.reset()
            self.proc.send(ide.dump_expression(path, skip_global=False) + "\n")
            self.wait_for("ready", timeout=60)
            self.assertEqual(self.output_text(), "", "the dump must print nothing")
            idx = ide.CompletionIndex()
            idx.load_dump_file(path, full=True)
            self.assertGreater(len(idx.global_env), 3000)
            self.assertEqual(idx.describe("square"), "(square x)")
            self.assertEqual(idx.classify("define"), "keyword")
            self.assertEqual(idx.classify("map"), "builtin")
            self.assertEqual(idx.kind("car"), "procedure")
            # a partial dump only re-reads the user environment
            self.proc.send("(define (cube x) (* x x x))\n")
            self.wait_for("ready")
            self.proc.send(ide.dump_expression(path, skip_global=True) + "\n")
            self.wait_for("ready", timeout=30)
            idx.load_dump_file(path, full=False)
            self.assertEqual(idx.describe("cube"), "(cube x)")
            self.assertEqual(idx.kind("map"), "procedure")

    def test_load_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "prog.scm")
            with open(path, "w") as f:
                f.write('(define (twice x) (* 2 x))\n(display (twice 21))\n(newline)\n')
            self.reset()
            self.proc.send('(load "%s")\n' % ide.scheme_string(path))
            self.wait_for("ready", timeout=20)
            self.assertIn("42", self.output_text())


if __name__ == "__main__":
    unittest.main()
