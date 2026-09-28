#!/usr/bin/env python3
# MIT Scheme IDE -- a small editor and REPL console for MIT/GNU Scheme.
#
# Copyright (C) 2026 Contributors to the mit-scheme-apple-silicon project
#
# This file is part of MIT/GNU Scheme.
#
# MIT/GNU Scheme is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or (at
# your option) any later version.
#
# MIT/GNU Scheme is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with MIT/GNU Scheme; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301,
# USA.

"""A small IDE for MIT/GNU Scheme: editor + interactive console.

    python3 mit_scheme_ide.py [--scheme PATH] [FILE] [-- SCHEME-ARGS...]

The interpreter is run with ``--emacs``, the same interface Emacs's
xscheme.el uses.  In that mode Scheme sends its prompt, values, errors and
GC notifications as escape sequences instead of text, so the console can
render them itself and always knows whether the REPL is idle, reading or
evaluating.

Code completion is built from the running interpreter: the IDE silently
evaluates an expression that writes every bound name of the REPL
environment chain (with its lambda list or arity) to a file, and refreshes
the user-environment part after every evaluation, so your own definitions
complete too.

Only the Python standard library (with Tk) is required.  The pure-Python
pieces (protocol parser, tokenizer, indenter, completion index, process
driver) sit at the top of this file and do not import Tk, so they can be
unit-tested headlessly.
"""

import argparse
import bisect
import codecs
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

APP_NAME = "MIT Scheme IDE"
VERSION = "0.1"
MANUAL_URL = "https://www.gnu.org/software/mit-scheme/documentation/stable/mit-scheme-ref/"

# ---------------------------------------------------------------------------
# Scheme knowledge used by the highlighter and indenter
# ---------------------------------------------------------------------------

SPECIAL_FORMS = frozenset("""
    define define-syntax define-record-type define-structure define-integrable
    define-values define-library define-macro define-syntax-rule
    lambda named-lambda case-lambda let let* letrec letrec* let-values
    let*-values let-syntax letrec-syntax fluid-let parameterize
    if cond case and or when unless not else => begin do delay delay-force
    make-promise force quasiquote quote unquote unquote-splicing set!
    syntax-rules er-macro-transformer rsc-macro-transformer sc-macro-transformer
    guard raise raise-continuable error assert dynamic-wind
    call-with-current-continuation call/cc call-with-values values
    with-exception-handler the-environment cons-stream declare
    import export include include-ci library
    async await async-let actor task-group with-task-group
""".split())

# Forms whose body is indented two columns past the opening paren, in the
# style of Emacs's scheme-mode.  Anything starting with "define", "let",
# "with-" or "call-with-" is treated the same way.
BODY_FORMS = frozenset("""
    lambda named-lambda case-lambda begin when unless do case cond-expand
    parameterize fluid-let syntax-rules guard receive delay delay-force
    dynamic-wind make-parameter assert cons-stream
    async async-let actor with-task-group
""".split())

DEFINE_FORMS = frozenset("""
    define define-syntax define-record-type define-structure
    define-integrable define-values define-macro define-library
""".split())

# ---------------------------------------------------------------------------
# The --emacs interface protocol
# ---------------------------------------------------------------------------
#
# Scheme writes ESC <type> for a simple signal, or ESC <type> <string> ESC
# for one that carries an argument.  See src/runtime/emacs.scm and
# etc/xscheme.el.  The types that carry a string argument:

STRING_SIGNALS = frozenset("ADEPimnpvw")

_HASHED_VALUE_RE = re.compile(
    r'\(xscheme-write-message-1 xscheme-prompt \(format "(;Value \d+): %s" xscheme-prompt\)\)')
_MESSAGE_RE = re.compile(r'\(message "%s" "((?:[^"\\]|\\.)*)"\)')
_NO_VALUES_RE = re.compile(r'\(xscheme-write-message-1 "\(no values\)" "(;No values)"\)')


def _unescape_scheme_string(s):
    return re.sub(r'\\(.)', lambda m: {'n': '\n', 't': '\t'}.get(m.group(1), m.group(1)), s)


class EmacsProtocolParser:
    """Turns the byte stream of ``mit-scheme --emacs`` into events.

    Each event is a ``(kind, arg)`` tuple.  Kinds: ``output`` (text),
    ``bell``, ``prompt`` ((level, name)), ``ready``, ``read-start``,
    ``read-finish``, ``value`` (repr or "" for unspecified), ``value-text``
    (a complete ";Value 12: ..." line), ``error``, ``gc-start``, ``gc-end``,
    ``cd`` (directory), ``interrupt-ok``, ``expression-prompt`` (prompt
    string), ``confirm`` (prompt string), ``message`` (text), ``debugger``,
    ``read-char``, ``eval`` (unhandled elisp), ``unknown`` (type char).
    """

    def __init__(self):
        self._buf = b""
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._last_p = ""

    def feed(self, data):
        events = []
        buf = self._buf + data
        pos, n = 0, len(buf)
        while pos < n:
            i = buf.find(b"\x1b", pos)
            if i < 0:
                self._text(buf[pos:], events)
                pos = n
                break
            if i > pos:
                self._text(buf[pos:i], events)
            if i + 1 >= n:
                pos = i          # lone ESC at the end: wait for the type byte
                break
            t = chr(buf[i + 1])
            if t in STRING_SIGNALS:
                j = buf.find(b"\x1b", i + 2)
                if j < 0:
                    pos = i      # argument not complete yet
                    break
                arg = buf[i + 2:j].decode("utf-8", "replace")
                pos = j + 1
                self._string_signal(t, arg, events)
            else:
                pos = i + 2
                self._simple_signal(t, events)
        self._buf = buf[pos:]
        return events

    def _text(self, data, events):
        text = self._decoder.decode(data)
        if not text:
            return
        parts = text.split("\x07")
        for k, part in enumerate(parts):
            if k:
                events.append(("bell", None))
            if part:
                events.append(("output", part))

    def _string_signal(self, t, arg, events):
        if t == "p":
            level, _, name = arg.partition(" ")
            try:
                level = int(level)
            except ValueError:
                level = 1
            events.append(("prompt", (level, name)))
        elif t == "v":
            events.append(("value", arg))
        elif t == "w":
            events.append(("cd", arg))
        elif t == "P":
            self._last_p = arg
        elif t == "E" or t == "A":
            m = _HASHED_VALUE_RE.fullmatch(arg)
            if m:
                events.append(("value-text", "%s: %s" % (m.group(1), self._last_p)))
                return
            m = _NO_VALUES_RE.fullmatch(arg)
            if m:
                events.append(("value-text", m.group(1)))
                return
            m = _MESSAGE_RE.fullmatch(arg)
            if m:
                events.append(("message", _unescape_scheme_string(m.group(1))))
                return
            events.append(("eval", arg))
        elif t == "i":
            events.append(("expression-prompt", arg))
        elif t == "n":
            events.append(("confirm", arg))
        elif t == "m":
            events.append(("message", arg))
        elif t == "D":
            events.append(("debugger", arg))

    def _simple_signal(self, t, events):
        kind = {
            "R": "ready", "s": "read-start", "f": "read-finish",
            "b": "gc-start", "e": "gc-end", "g": "interrupt-ok",
            "z": "error", "o": "read-char", "c": "read-char",
        }.get(t)
        events.append((kind, None) if kind else ("unknown", t))


def prompt_text(level, name):
    """The prompt MIT Scheme would print for a ``prompt`` event."""
    if name.startswith("[Debug]"):
        kind = "debug>"
    elif name.startswith("[Where]"):
        kind = "where>"
    elif name.startswith("[Evaluator] "):
        kind = name[len("[Evaluator] "):]
    elif level == 1:
        kind = "]=>"
    else:
        kind = "error>"
    return "%d %s " % (level, kind)


# ---------------------------------------------------------------------------
# The interpreter process
# ---------------------------------------------------------------------------

class SchemeProcess:
    """``mit-scheme --emacs`` behind pipes, with a reader and a writer thread.

    ``read_chunks()`` never blocks; call it from the UI loop and feed the
    bytes to an :class:`EmacsProtocolParser`.  It yields ``None`` once when
    the process has closed its output.
    """

    def __init__(self, executable, extra_args=(), cwd=None):
        self.executable = executable
        self.argv = [executable, "--emacs"] + list(extra_args)
        self.proc = subprocess.Popen(
            self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0, cwd=cwd,
            start_new_session=True)
        self._out = queue.Queue()
        self._in = queue.Queue()
        self._eof = False
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._writer, daemon=True).start()

    @property
    def pid(self):
        return self.proc.pid

    @property
    def alive(self):
        return self.proc.poll() is None

    @property
    def returncode(self):
        return self.proc.poll()

    def _reader(self):
        fd = self.proc.stdout.fileno()
        while True:
            try:
                data = os.read(fd, 65536)
            except OSError:
                data = b""
            if not data:
                self._out.put(None)
                return
            self._out.put(data)

    def _writer(self):
        stdin = self.proc.stdin
        while True:
            item = self._in.get()
            if item is None:
                try:
                    stdin.close()
                except OSError:
                    pass
                return
            try:
                stdin.write(item)
                stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                return

    def read_chunks(self):
        chunks = []
        while True:
            try:
                item = self._out.get_nowait()
            except queue.Empty:
                break
            if item is None:
                self._eof = True
                chunks.append(None)
                break
            chunks.append(item)
        return chunks

    def send(self, text):
        if self.alive:
            self._in.put(text.encode("utf-8"))

    def send_bytes(self, data):
        if self.alive:
            self._in.put(data)

    def interrupt(self):
        """^G: abort to top level, flushing typeahead (what xscheme does)."""
        if not self.alive:
            return

        def go():
            try:
                self.proc.send_signal(signal.SIGINT)
            except OSError:
                return
            time.sleep(0.1)
            self.send_bytes(b"\0")
        threading.Thread(target=go, daemon=True).start()

    def terminate(self):
        self._in.put(None)
        if self.alive:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=1.0)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self.proc.kill()
                except OSError:
                    pass


def find_scheme_executable(explicit=None, preferred=None):
    """Return the command to run, or None.

    Order: explicit argument, ``$MIT_SCHEME_EXE``, the saved preference,
    ``mit-scheme`` on ``$PATH``, a freshly built in-tree ``src/run-build``
    next to this file, then the usual macOS install locations.
    """
    import glob
    home = os.path.expanduser("~")
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [explicit, os.environ.get("MIT_SCHEME_EXE"), preferred, "mit-scheme"]
    run_build = os.path.join(os.path.dirname(here), "src", "run-build")
    if os.path.exists(os.path.join(os.path.dirname(here), "src", "microcode", "scheme")):
        candidates.append(run_build)
    candidates += [
        os.path.join(home, "opt", "mit-scheme", "bin", "mit-scheme"),
        "/opt/homebrew/bin/mit-scheme",
        "/usr/local/bin/mit-scheme",
        "/opt/local/bin/mit-scheme",
    ]
    candidates += sorted(glob.glob("/Applications/mit-scheme*/bin/mit-scheme"), reverse=True)
    candidates += sorted(glob.glob(os.path.join(home, "mit-scheme*", "bin", "mit-scheme")), reverse=True)
    for c in candidates:
        if not c:
            continue
        c = os.path.expanduser(c)
        if os.sep in c:
            if os.path.isfile(c) and os.access(c, os.X_OK):
                return c
        else:
            found = shutil.which(c)
            if found:
                return found
    return None


def scheme_string(s):
    """Scheme string literal syntax for ``s`` (without the quotes)."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

_MASTER_RE = re.compile(r"""
      (?P<ws>\s+)
    | (?P<comment>;[^\n]*)
    | (?P<bcomment>\#\|)
    | (?P<dcomment>\#;)
    | (?P<string>"(?:[^"\\]|\\.)*(?:"|\Z))
    | (?P<char>\#\\(?:x[0-9a-fA-F]{2,}|[A-Za-z][A-Za-z0-9-]*|.))
    | (?P<open>[(\[{])
    | (?P<close>[)\]}])
    | (?P<quote>,@|[',`])
    | (?P<atom>\|(?:[^|\\]|\\.)*(?:\||\Z)|[^\s()\[\]{}";'`,]+)
""", re.X | re.S)
_BCOMMENT_DELIM_RE = re.compile(r"\#\||\|\#")
_NUMBER_RE = re.compile(
    r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?|[+-]?\d+/\d+|[+-]?inf\.0|[+-]nan\.0|#[xXbBoOdDeEiI].+")
_TERMINATED_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"', re.S)
CONSTANTS = frozenset("#t #f #true #false #!default #!optional #!rest #!eof "
                      "#!unspecific #!aux #!eval #!fold-case #!no-fold-case".split())

IDENT_CHARS_RE = re.compile(r"[^\s()\[\]{}\";'`,]")

# Control (0x4) and Mod1 (0x8: Alt on X11, Command on macOS) in event.state.
MODIFIER_MASK = 0x4 | 0x8
POPUP_NAV_KEYS = frozenset(["Up", "Down", "Prior", "Next", "Return", "KP_Enter", "Tab",
                            "Escape", "Shift_L", "Shift_R", "Control_L", "Control_R",
                            "Alt_L", "Alt_R", "Meta_L", "Meta_R", "Super_L", "Super_R"])


class Token(tuple):
    """(start, end, kind, text).  ``kind`` is one of: comment, string, char,
    number, constant, keyword, builtin, define-name, quoted, symbol, open,
    close, quote."""
    __slots__ = ()
    start = property(lambda t: t[0])
    end = property(lambda t: t[1])
    kind = property(lambda t: t[2])
    text = property(lambda t: t[3])


def tokenize(text, classify=None):
    """Tokenize Scheme source.  ``classify(name)`` may return 'keyword' or
    'builtin' for a symbol known to the running interpreter."""
    tokens = []
    prev1 = prev2 = None      # previous two non-comment tokens
    pos, n = 0, len(text)
    while pos < n:
        m = _MASTER_RE.match(text, pos)
        if not m:            # cannot happen, but never loop forever
            pos += 1
            continue
        kind = m.lastgroup
        start, end = m.span()
        if kind == "ws":
            pos = end
            continue
        if kind == "bcomment":
            depth, i = 1, end
            while depth and i < n:
                d = _BCOMMENT_DELIM_RE.search(text, i)
                if not d:
                    i = n
                    break
                depth += 1 if d.group() == "#|" else -1
                i = d.end()
            end = i
            kind = "comment"
        elif kind == "dcomment":
            kind = "comment"
        tok_text = text[start:end]
        if kind == "atom":
            name = tok_text
            if name in CONSTANTS:
                kind = "constant"
            elif _NUMBER_RE.fullmatch(name):
                kind = "number"
            elif prev1 is not None and prev1[2] == "quote" and prev1[3] in ("'", "`"):
                kind = "quoted"
            elif (prev1 is not None and prev1[2] == "open" and prev2 is not None
                  and prev2[2] == "keyword" and prev2[3] in DEFINE_FORMS):
                kind = "define-name"      # (define (name ...
            elif (prev1 is not None and prev1[2] == "keyword" and prev1[3] in DEFINE_FORMS
                  and prev2 is not None and prev2[2] == "open"):
                kind = "define-name"      # (define name ...
            elif name in SPECIAL_FORMS:
                kind = "keyword"
            else:
                kind = (classify(name) if classify else None) or "symbol"
        tok = Token((start, end, kind, tok_text))
        tokens.append(tok)
        if kind != "comment":
            prev2, prev1 = prev1, tok
        pos = end
    return tokens


def unterminated_tail(text, tokens):
    """True when ``text`` ends inside a string or block comment."""
    if not tokens:
        return False
    t = tokens[-1]
    if t.end != len(text):
        return False
    if t.kind == "string":
        return not _TERMINATED_STRING_RE.fullmatch(t.text)
    if t.kind == "comment" and t.text.startswith("#|"):
        return not t.text.endswith("|#") or len(t.text) < 4
    return False


def paren_depth(tokens):
    """Net paren depth of a token list (negative if over-closed)."""
    depth = 0
    for t in tokens:
        if t.kind == "open":
            depth += 1
        elif t.kind == "close":
            depth -= 1
            if depth < 0:
                return depth
    return depth


def is_complete_input(text):
    """True if ``text`` holds zero or more complete data (balanced parens,
    closed strings/comments)."""
    tokens = tokenize(text)
    if unterminated_tail(text, tokens):
        return False
    return paren_depth(tokens) == 0


def match_paren(tokens, index):
    """Index of the token matching the open/close token at ``index``."""
    t = tokens[index]
    if t.kind == "open":
        depth = 0
        for j in range(index, len(tokens)):
            k = tokens[j].kind
            if k == "open":
                depth += 1
            elif k == "close":
                depth -= 1
                if depth == 0:
                    return j
    elif t.kind == "close":
        depth = 0
        for j in range(index, -1, -1):
            k = tokens[j].kind
            if k == "close":
                depth += 1
            elif k == "open":
                depth -= 1
                if depth == 0:
                    return j
    return None


def token_at(tokens, offset):
    """Index of the token containing ``offset`` (start <= offset < end)."""
    i = bisect.bisect_right(tokens, (offset, float("inf"))) - 1
    if i >= 0 and tokens[i].start <= offset < tokens[i].end:
        return i
    return None


def toplevel_form_at(tokens, offset):
    """(start, end) of the top-level form containing ``offset``, or the one
    just before it when the cursor sits between forms.  None if no form."""
    depth = 0
    form_start = None
    best = None
    for i, t in enumerate(tokens):
        if t.kind == "comment":
            continue
        if depth == 0 and form_start is None:
            form_start = i
        if t.kind == "open":
            depth += 1
        elif t.kind == "close":
            depth = max(0, depth - 1)
        if depth == 0 and t.kind != "quote":
            span = (tokens[form_start].start, t.end)
            form_start = None
            if span[0] <= offset <= span[1]:
                return span
            if span[1] < offset:
                best = span
            else:
                break
    return best


def enclosing_operator(tokens, offset):
    """Name of the operator of the innermost list containing ``offset``, and
    the 0-based index of the argument the cursor is in (0 = operator)."""
    stack = []
    for i, t in enumerate(tokens):
        if t.start >= offset:
            break
        if t.kind == "open":
            stack.append(i)
        elif t.kind == "close" and stack:
            stack.pop()
    if not stack:
        return None, 0
    oi = stack[-1]
    args = 0
    op = None
    depth = 0
    for j in range(oi + 1, len(tokens)):
        t = tokens[j]
        if t.start >= offset:
            break
        if t.kind == "comment":
            continue
        if depth == 0 and t.kind != "quote":
            if op is None:
                op = t
            elif t.kind != "close":
                args += 1
        if t.kind == "open":
            depth += 1
        elif t.kind == "close":
            depth -= 1
    if op is None or op.kind in ("open", "close", "string", "number", "constant"):
        return None, 0
    return op.text, args


# ---------------------------------------------------------------------------
# Indentation
# ---------------------------------------------------------------------------

def _is_body_form(name):
    return (name in BODY_FORMS
            or name.startswith(("define", "let", "with-", "call-with-", "when", "unless")))


def compute_indent(text):
    """Indentation (in columns) for a new line inserted at the end of
    ``text``; None when the end of ``text`` is inside a string or block
    comment (the caller should then not indent at all)."""
    tokens = tokenize(text)
    if unterminated_tail(text, tokens):
        return None
    stack = []
    for i, t in enumerate(tokens):
        if t.kind == "open":
            stack.append(i)
        elif t.kind == "close" and stack:
            stack.pop()
    if not stack:
        return 0
    oi = stack[-1]
    open_tok = tokens[oi]
    line_start = text.rfind("\n", 0, open_tok.start) + 1
    open_col = open_tok.start - line_start
    line_end = text.find("\n", open_tok.start)
    if line_end < 0:
        line_end = len(text)

    def column(off):
        return off - (text.rfind("\n", 0, off) + 1)

    following = [t for t in tokens[oi + 1:oi + 4] if t.kind != "comment"]
    if not following or following[0].start >= line_end:
        return open_col + 1
    first = following[0]
    if first.kind in ("open", "quote", "string", "number", "constant", "char"):
        return column(first.start)
    if _is_body_form(first.text) or first.kind == "define-name":
        return open_col + 2
    if len(following) > 1 and following[1].start < line_end:
        return column(following[1].start)
    return open_col + 1


# ---------------------------------------------------------------------------
# Completion index
# ---------------------------------------------------------------------------

# Evaluated (invisibly) in the REPL.  Writes one line per bound name of the
# REPL environment chain: NAME <TAB> KIND [<TAB> SIGNATURE], with ";;env"
# separating environments (innermost first).  SIGNATURE is the lambda list
# when debugging info is available, else the arity ("2", "1+", "2-3").
DUMP_EXPRESSION = r"""
(let ((out-path "%s") (skip-global? %s))
  (define (safe thunk default)
    (call-with-current-continuation
     (lambda (k)
       (bind-condition-handler (list condition-type:error)
           (lambda (c) (k default))
         thunk))))
  (define (arity-string v)
    (safe (lambda ()
            (let* ((a (procedure-arity v))
                   (lo (procedure-arity-min a))
                   (hi (procedure-arity-max a)))
              (cond ((not hi) (string-append (number->string lo) "+"))
                    ((= lo hi) (number->string lo))
                    (else (string-append (number->string lo) "-"
                                         (number->string hi))))))
          ""))
  (define (lambda-list-string v)
    (safe (lambda ()
            (let ((l (procedure-lambda v)))
              (and l
                   (lambda-components* l
                     (lambda (name req opt rest body)
                       (with-output-to-string
                         (lambda ()
                           (write (append req
                                          (if (null? opt) '() (cons '#!optional opt))
                                          (or rest '()))))))))))
          #f))
  (define (emit env name port)
    (let ((type (safe (lambda () (environment-reference-type env name)) 'unbound)))
      (write-string (symbol->string name) port)
      (write-char #\tab port)
      (case type
        ((macro) (write-string "macro" port))
        ((normal)
         (let ((v (safe (lambda () (environment-lookup env name)) #f)))
           (if (procedure? v)
               (begin
                 (write-string "procedure" port)
                 (write-char #\tab port)
                 (write-string (or (lambda-list-string v) (arity-string v)) port))
               (write-string "variable" port))))
        (else (write-string (symbol->string type) port)))
      (newline port)))
  (call-with-output-file out-path
    (lambda (port)
      (let loop ((env (nearest-repl/environment)))
        (if (not (and skip-global? (eq? env system-global-environment)))
            (begin
              (write-string ";;env" port)
              (newline port)
              (for-each (lambda (name) (emit env name port))
                        (environment-bound-names env))
              (if (environment-has-parent? env)
                  (loop (environment-parent env))))))))
  unspecific)
""".strip()


def dump_expression(path, skip_global):
    return DUMP_EXPRESSION % (scheme_string(path), "#t" if skip_global else "#f")


class CompletionIndex:
    """Names known to the interpreter plus names seen in the editor."""

    def __init__(self):
        self.envs = []          # list of dicts name -> (kind, sig); innermost first
        self.global_env = {}    # the system-global-environment dict, kept across partial dumps
        self.local = {}         # names from the editor buffer: name -> kind
        self._merged = {}
        self._sorted = []
        self._rebuild()

    # -- loading -----------------------------------------------------------

    def parse_dump(self, text):
        envs = []
        for line in text.splitlines():
            if line.startswith(";;env"):
                envs.append({})
                continue
            if not line or line.startswith(";;"):
                continue
            parts = line.split("\t")
            if not envs:
                envs.append({})
            name = parts[0]
            kind = parts[1] if len(parts) > 1 else "variable"
            sig = parts[2] if len(parts) > 2 else ""
            envs[-1].setdefault(name, (kind, sig))
        return envs

    def load_dump(self, text, full):
        envs = self.parse_dump(text)
        if full:
            if envs:
                self.global_env = envs[-1]
                self.envs = envs[:-1]
            else:
                self.envs = []
        else:
            self.envs = envs
        self._rebuild()

    def load_dump_file(self, path, full):
        with open(path, encoding="utf-8", errors="replace") as f:
            self.load_dump(f.read(), full)

    def set_local_names(self, names):
        """``names``: dict name -> 'defined' | 'symbol' from the editor."""
        if names != self.local:
            self.local = dict(names)
            self._rebuild()

    def _rebuild(self):
        merged = {}
        for env in self.envs:
            for name, v in env.items():
                merged.setdefault(name, v)
        for name, v in self.global_env.items():
            merged.setdefault(name, v)
        if not merged:
            for name in SPECIAL_FORMS:
                merged[name] = ("macro", "")
        for name, kind in self.local.items():
            merged.setdefault(name, (kind, ""))
        self._merged = merged
        self._sorted = sorted(merged)

    @property
    def loaded(self):
        return bool(self.envs or self.global_env)

    # -- queries -----------------------------------------------------------

    def kind(self, name):
        v = self._merged.get(name)
        return v[0] if v else None

    def signature(self, name):
        v = self._merged.get(name)
        return v[1] if v else None

    def classify(self, name):
        """Highlight class for a symbol, or None."""
        v = self._merged.get(name)
        if not v:
            return None
        if v[0] == "macro":
            return "keyword"
        if v[0] == "procedure":
            return "builtin"
        return None

    def describe(self, name):
        """A one-line description for the status bar, or None."""
        v = self._merged.get(name)
        if not v:
            return None
        kind, sig = v
        if kind == "macro":
            return "%s: special form" % name
        if kind == "procedure":
            if sig.startswith("("):
                inner = sig[1:-1].strip()
                return "(%s%s)" % (name, " " + inner if inner else "")
            if sig:
                return "%s: procedure, %s argument%s" % (name, sig, "" if sig == "1" else "s")
            return "%s: procedure" % name
        if kind == "defined":
            return "%s: defined in this buffer" % name
        return "%s: %s" % (name, kind)

    def complete(self, prefix, limit=200):
        """Candidates for ``prefix`` as (name, kind, sig), best first."""
        if not prefix:
            return []
        names = self._sorted
        i = bisect.bisect_left(names, prefix)
        exact = []
        while i < len(names) and names[i].startswith(prefix):
            exact.append(names[i])
            i += 1
        exact.sort(key=lambda s: (len(s), s))
        result = exact[:limit]
        if len(result) < limit and len(prefix) >= 2:
            seen = set(result)
            infix = []
            for name in names:
                if name not in seen:
                    k = name.find(prefix)
                    if k > 0:
                        infix.append((k, len(name), name))
            infix.sort()
            result += [n for _, _, n in infix[:limit - len(result)]]
        return [(n,) + self._merged[n] for n in result]


def local_names_from_tokens(tokens, min_length=3):
    names = {}
    for t in tokens:
        if t.kind == "define-name":
            names[t.text] = "defined"
        elif t.kind in ("symbol", "builtin", "quoted") and len(t.text) >= min_length:
            names.setdefault(t.text, "symbol")
    return names


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------

class Prefs(dict):
    DEFAULTS = {
        "scheme_exe": None,
        "font_size": 13,
        "geometry": "1100x800",
        "sash": 0.6,
        "autocomplete": True,
        "dark": False,
        "recent": [],
    }

    @staticmethod
    def path():
        if sys.platform == "darwin":
            base = os.path.expanduser("~/Library/Application Support")
        else:
            base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        return os.path.join(base, "mit-scheme-ide", "prefs.json")

    @classmethod
    def load(cls):
        p = cls(cls.DEFAULTS)
        try:
            with open(cls.path(), encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                p.update(data)
        except (OSError, ValueError):
            pass
        return p

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path()), exist_ok=True)
            with open(self.path(), "w", encoding="utf-8") as f:
                json.dump(self, f, indent=2)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Themes
# ---------------------------------------------------------------------------

LIGHT_THEME = {
    "bg": "#ffffff", "fg": "#24292f", "cursor": "#24292f", "select": "#b6d6fc",
    "gutter_bg": "#f6f8fa", "gutter_fg": "#8c959f",
    "comment": "#6e7781", "string": "#0a3069", "char": "#0a3069",
    "number": "#0550ae", "constant": "#0550ae", "keyword": "#cf222e",
    "builtin": "#8250df", "define-name": "#953800", "quoted": "#116329",
    "paren": "#57606a", "match_bg": "#c8e1ff", "mismatch_bg": "#ffd7d5",
    "found_bg": "#fff8c5",
    "console_bg": "#fbfcfd", "prompt": "#1a7f37", "input": "#0550ae",
    "value": "#1a7f37", "error": "#cf222e", "info": "#6e7781",
    "popup_bg": "#ffffff", "popup_fg": "#24292f", "popup_sel": "#ddf4ff",
}

DARK_THEME = {
    "bg": "#0d1117", "fg": "#e6edf3", "cursor": "#e6edf3", "select": "#264f78",
    "gutter_bg": "#161b22", "gutter_fg": "#6e7681",
    "comment": "#8b949e", "string": "#a5d6ff", "char": "#a5d6ff",
    "number": "#79c0ff", "constant": "#79c0ff", "keyword": "#ff7b72",
    "builtin": "#d2a8ff", "define-name": "#ffa657", "quoted": "#7ee787",
    "paren": "#8b949e", "match_bg": "#1f4b7a", "mismatch_bg": "#6e1a1a",
    "found_bg": "#5a4a00",
    "console_bg": "#0b0f14", "prompt": "#3fb950", "input": "#79c0ff",
    "value": "#3fb950", "error": "#ff7b72", "info": "#8b949e",
    "popup_bg": "#161b22", "popup_fg": "#e6edf3", "popup_sel": "#1f3a5f",
}

TOKEN_TAGS = ("comment", "string", "char", "number", "constant", "keyword",
              "builtin", "define-name", "quoted", "paren")


# ===========================================================================
# GUI
# ===========================================================================

def _gui_imports():
    global tk, ttk, tkfont, filedialog, messagebox
    import tkinter as tk
    from tkinter import ttk, font as tkfont, filedialog, messagebox


class CodeText:
    """Mixin-ish helper: wraps a tk.Text so that every insert/delete/undo
    generates <<TextChanged>> and every scroll <<ViewChanged>>."""

    @staticmethod
    def install(text):
        orig = text._w + "_orig"
        text.tk.call("rename", text._w, orig)

        def proxy(cmd, *args):
            try:
                result = text.tk.call((orig, cmd) + args)
            except tk.TclError:
                if cmd == "edit" and args and args[0] in ("undo", "redo"):
                    return None          # nothing to undo/redo
                raise
            if cmd in ("insert", "delete", "replace") or (
                    cmd == "edit" and args and args[0] in ("undo", "redo")):
                text.event_generate("<<TextChanged>>", when="tail")
                text.event_generate("<<ViewChanged>>", when="tail")
            elif cmd in ("yview", "xview", "see") or (cmd == "mark" and args[:2] == ("set", "insert")):
                text.event_generate("<<ViewChanged>>", when="tail")
            return result
        text.tk.createcommand(text._w, proxy)


def word_before(text, index="insert"):
    """(start_index, word) of the identifier ending at ``index``."""
    line, col = map(int, text.index(index).split("."))
    line_text = text.get("%d.0" % line, index)
    i = len(line_text)
    while i > 0 and IDENT_CHARS_RE.match(line_text[i - 1]):
        i -= 1
    return "%d.%d" % (line, i), line_text[i:]


class CompletionPopup:
    """A drop-down list of completions attached to a Text widget."""

    MAX_ROWS = 10

    def __init__(self, text, index, theme, font, on_status=None):
        self.text = text
        self.index = index
        self.theme = theme
        self.font = font
        self.on_status = on_status or (lambda s: None)
        self.win = None
        self.listbox = None
        self.items = []
        self.word_start = None
        text.bind("<FocusOut>", lambda e: text.after(150, self._focus_check), add="+")
        text.bind("<Configure>", lambda e: self.close(), add="+")
        text.bind("<ButtonPress-1>", lambda e: self.close(), add="+")

    @property
    def active(self):
        return self.win is not None

    def set_theme(self, theme):
        self.theme = theme

    def open(self, explicit=False):
        start, word = word_before(self.text)
        if not word or (not explicit and len(word) < 2):
            self.close()
            return False
        items = [it for it in self.index.complete(word, limit=61)
                 if not (it[0] == word and it[1] == "symbol")]   # not the word itself
        if not items or (len(items) == 1 and items[0][0] == word and not explicit):
            self.close()
            return False
        self.items = items
        self.word_start = start
        if self.win is None:
            self._create()
        self._fill()
        self._place()
        return True

    def refresh(self):
        if self.active:
            self.open(explicit=True)

    def _create(self):
        t = self.theme
        self.win = tk.Toplevel(self.text)
        self.win.overrideredirect(True)
        try:
            self.win.attributes("-topmost", True)
        except tk.TclError:
            pass
        frame = tk.Frame(self.win, bd=1, relief="solid", bg=t["gutter_fg"])
        frame.pack(fill="both", expand=True)
        self.listbox = tk.Listbox(
            frame, font=self.font, activestyle="none", bd=0, highlightthickness=0,
            bg=t["popup_bg"], fg=t["popup_fg"], selectbackground=t["popup_sel"],
            selectforeground=t["popup_fg"], exportselection=False)
        self.listbox.pack(fill="both", expand=True)
        self.listbox.bind("<ButtonRelease-1>", lambda e: self.accept())
        self.listbox.bind("<Motion>", self._on_motion)

    def _focus_check(self):
        if self.active and self.text.focus_get() is not self.text:
            self.close()

    def _on_motion(self, event):
        i = self.listbox.nearest(event.y)
        if i >= 0:
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(i)
            self._status()

    def _fill(self):
        lb = self.listbox
        lb.delete(0, "end")
        width = 20
        for name, kind, sig in self.items:
            label = name
            if kind == "procedure":
                if sig.startswith("("):
                    label += "  " + sig
                elif sig:
                    label += "  %s arg%s" % (sig, "" if sig == "1" else "s")
            elif kind == "macro":
                label += "  syntax"
            elif kind == "defined":
                label += "  (buffer)"
            elif kind == "variable":
                label += "  variable"
            lb.insert("end", label)
            width = max(width, len(label))
        lb.configure(width=min(width + 1, 70), height=min(len(self.items), self.MAX_ROWS))
        lb.selection_set(0)
        lb.see(0)
        self._status()

    def _place(self):
        self.text.update_idletasks()
        bbox = self.text.bbox(self.word_start)
        if not bbox:
            bbox = self.text.bbox("insert") or (0, 0, 0, 0)
        x = self.text.winfo_rootx() + bbox[0]
        y = self.text.winfo_rooty() + bbox[1] + bbox[3] + 2
        self.win.update_idletasks()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        sw, sh = self.text.winfo_screenwidth(), self.text.winfo_screenheight()
        if x + w > sw:
            x = max(0, sw - w)
        if y + h > sh:
            y = self.text.winfo_rooty() + bbox[1] - h - 2
        self.win.geometry("+%d+%d" % (x, y))
        self.win.deiconify()
        self.win.lift()

    def _status(self):
        sel = self.listbox.curselection()
        if sel:
            name = self.items[sel[0]][0]
            self.on_status(self.index.describe(name) or "")

    def move(self, delta):
        if not self.active:
            return
        sel = self.listbox.curselection()
        i = (sel[0] if sel else 0) + delta
        i = max(0, min(len(self.items) - 1, i))
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(i)
        self.listbox.see(i)
        self._status()

    def accept(self):
        if not self.active:
            return False
        sel = self.listbox.curselection()
        if sel:
            name = self.items[sel[0]][0]
            self.text.delete(self.word_start, "insert")
            self.text.insert("insert", name)
        self.close()
        return True

    def close(self):
        if self.win is not None:
            self.win.destroy()
            self.win = None
            self.listbox = None
            self.items = []

    def handle_key(self, event):
        """Called from the Text's <Key> binding.  Returns 'break' when the
        key was consumed by the popup."""
        if not self.active:
            return None
        k = event.keysym
        if k in ("Down", "Up"):
            self.move(1 if k == "Down" else -1)
            return "break"
        if k in ("Next", "Prior"):
            self.move(self.MAX_ROWS if k == "Next" else -self.MAX_ROWS)
            return "break"
        if k in ("Return", "KP_Enter", "Tab"):
            self.accept()
            return "break"
        if k == "Escape":
            self.close()
            return "break"
        if k in ("Left", "Right", "Home", "End"):
            self.close()
        return None


class SchemeTextBase:
    """Behaviour shared by the editor and the console: tag setup, syntax
    highlighting, paren matching, completion popup, auto-indent."""

    def _init_text_common(self, app, text):
        self.app = app
        self.text = text
        self.tokens = []
        self.token_text = ""
        self._hl_job = None
        self.popup = CompletionPopup(text, app.index, app.theme, app.font, app.set_hint)
        for tag in TOKEN_TAGS:
            text.tag_configure(tag)
        text.tag_configure("match")
        text.tag_configure("mismatch")
        text.bind("<Key>", self._on_key_common, add="+")
        text.bind("<KeyRelease>", self._on_key_release_common, add="+")
        text.bind("<ButtonRelease-1>", lambda e: self.text.after_idle(self.update_match), add="+")
        text.bind("<Control-space>", self._complete_explicit)

    def apply_theme(self, theme, console=False):
        t = self.text
        t.configure(bg=theme["console_bg"] if console else theme["bg"], fg=theme["fg"],
                    insertbackground=theme["cursor"], selectbackground=theme["select"],
                    selectforeground=theme["fg"], inactiveselectbackground=theme["select"])
        for tag in TOKEN_TAGS:
            t.tag_configure(tag, foreground=theme[tag])
        t.tag_configure("comment", font=self.app.italic_font)
        t.tag_configure("keyword", font=self.app.bold_font)
        t.tag_configure("match", background=theme["match_bg"])
        t.tag_configure("mismatch", background=theme["mismatch_bg"])
        self.popup.set_theme(theme)
        t.tag_raise("sel")

    # -- highlighting ------------------------------------------------------

    def highlight_region(self, first, last):
        """Re-tokenize and re-tag the text between the two indices."""
        text = self.text.get(first, last)
        tokens = tokenize(text, self.app.index.classify)
        starts = [0]
        for m in re.finditer("\n", text):
            starts.append(m.end())
        base_line, base_col = map(int, self.text.index(first).split("."))

        def idx(off):
            line = bisect.bisect_right(starts, off)
            col = off - starts[line - 1]
            if line == 1:
                col += base_col
            return "%d.%d" % (base_line + line - 1, col)

        ranges = {tag: [] for tag in TOKEN_TAGS}
        for t in tokens:
            kind = t.kind
            if kind in ("open", "close"):
                kind = "paren"
            lst = ranges.get(kind)
            if lst is not None:
                lst.append(idx(t.start))
                lst.append(idx(t.end))
        for tag in TOKEN_TAGS:
            self.text.tag_remove(tag, first, last)
            if ranges[tag]:
                self.text.tag_add(tag, *ranges[tag])
        return text, tokens

    def schedule_highlight(self, delay=40):
        if self._hl_job is None:
            self._hl_job = self.text.after(delay, self._run_highlight)

    def _run_highlight(self):
        self._hl_job = None
        self.rehighlight()

    def rehighlight(self):
        raise NotImplementedError

    # -- paren matching ----------------------------------------------------

    def update_match(self):
        t = self.text
        t.tag_remove("match", "1.0", "end")
        t.tag_remove("mismatch", "1.0", "end")
        tokens, base = self._tokens_for_match()
        if not tokens:
            return
        off = self._offset_of("insert") - base
        cand = None
        i = token_at(tokens, off - 1)
        if i is not None and tokens[i].kind == "close":
            cand = i
        else:
            i = token_at(tokens, off)
            if i is not None and tokens[i].kind == "open":
                cand = i
        if cand is None:
            return
        j = match_paren(tokens, cand)
        a = self._index_of(tokens[cand].start + base)
        if j is None:
            t.tag_add("mismatch", a, a + "+1c")
            return
        b = self._index_of(tokens[j].start + base)
        t.tag_add("match", a, a + "+1c")
        t.tag_add("match", b, b + "+1c")

    def _tokens_for_match(self):
        return self.tokens, 0

    def _offset_of(self, index):
        return int(self.text.count("1.0", index, "chars")[0]) if self.text.compare(index, ">", "1.0") else 0

    def _index_of(self, offset):
        return self.text.index("1.0+%dc" % offset)

    # -- keys --------------------------------------------------------------

    def _on_key_common(self, event):
        r = self.popup.handle_key(event)
        if r:
            return r
        return None

    def _on_key_release_common(self, event):
        chord = event.state & MODIFIER_MASK
        if self.popup.active:
            if event.keysym not in POPUP_NAV_KEYS and not chord:
                self.popup.refresh()      # closes itself when no word is left
        elif (self.app.prefs.get("autocomplete", True) and event.char
              and IDENT_CHARS_RE.match(event.char) and not chord):
            self.popup.open(explicit=False)
        self.text.after_idle(self.update_match)

    def _complete_explicit(self, event=None):
        if not self.popup.open(explicit=True):
            _, word = word_before(self.text)
            self.app.set_hint("No completions for %r" % word if word else "")
        return "break"

    def insert_newline_and_indent(self, region_start="1.0"):
        t = self.text
        before = t.get(region_start, "insert")
        indent = compute_indent(before)
        # remove trailing whitespace on the line being left
        line_start = t.index("insert linestart")
        seg = t.get(line_start, "insert")
        stripped = seg.rstrip()
        if len(stripped) != len(seg) and t.compare(line_start, ">=", region_start):
            t.delete("insert-%dc" % (len(seg) - len(stripped)), "insert")
        t.insert("insert", "\n" + (" " * indent if indent else ""))
        t.see("insert")

    def reindent_line(self, region_start="1.0"):
        """Re-indent the current line; returns True if it changed."""
        t = self.text
        line_start = t.index("insert linestart")
        if t.compare(line_start, "<", region_start):
            return False
        before = t.get(region_start, line_start)
        indent = compute_indent(before)
        if indent is None:
            return False
        line = t.get(line_start, "%s lineend" % line_start)
        current = len(line) - len(line.lstrip(" \t"))
        if current == indent:
            # put the cursor after the indentation if it is inside it
            col = int(t.index("insert").split(".")[1])
            if col < indent:
                t.mark_set("insert", "%s+%dc" % (line_start, indent))
            return False
        t.delete(line_start, "%s+%dc" % (line_start, current))
        t.insert(line_start, " " * indent)
        col = int(t.index("insert").split(".")[1])
        if col < indent:
            t.mark_set("insert", "%s+%dc" % (line_start, indent))
        return True


class Editor(SchemeTextBase):
    """The source editor pane."""

    def __init__(self, app, master):
        self.frame = ttk.Frame(master)
        self.path = None
        self._last_local_names = None

        self.findbar = ttk.Frame(self.frame)
        ttk.Label(self.findbar, text="Find:").pack(side="left", padx=(6, 2))
        self.find_var = tk.StringVar()
        self.find_entry = ttk.Entry(self.findbar, textvariable=self.find_var, width=30)
        self.find_entry.pack(side="left", padx=2, pady=3)
        ttk.Button(self.findbar, text="Next", command=lambda: self.find_next(1)).pack(side="left", padx=2)
        ttk.Button(self.findbar, text="Previous", command=lambda: self.find_next(-1)).pack(side="left", padx=2)
        self.find_status = ttk.Label(self.findbar, text="")
        self.find_status.pack(side="left", padx=8)
        ttk.Button(self.findbar, text="Close", command=self.hide_find).pack(side="right", padx=4)
        self.find_entry.bind("<Return>", lambda e: self.find_next(1))
        self.find_entry.bind("<Shift-Return>", lambda e: self.find_next(-1))
        self.find_entry.bind("<Escape>", lambda e: self.hide_find())
        self.find_entry.bind("<KeyRelease>", self._find_incremental)

        body = ttk.Frame(self.frame)
        body.pack(fill="both", expand=True)
        self.gutter = tk.Canvas(body, width=48, highlightthickness=0, bd=0)
        self.gutter.pack(side="left", fill="y")
        text = tk.Text(body, wrap="none", undo=True, autoseparators=True, maxundo=-1,
                       font=app.font, bd=0, padx=6, pady=4, highlightthickness=0,
                       insertwidth=2, tabs=("2c",))
        vs = ttk.Scrollbar(body, orient="vertical", command=text.yview)
        hs = ttk.Scrollbar(self.frame, orient="horizontal", command=text.xview)
        text.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        vs.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        hs.pack(fill="x")
        CodeText.install(text)
        self._init_text_common(app, text)
        text.tag_configure("found")

        text.bind("<<TextChanged>>", self._on_changed)
        text.bind("<<ViewChanged>>", self._on_view_changed)
        text.bind("<<Modified>>", self._on_modified_flag)
        text.bind("<Return>", self._on_return)
        text.bind("<KP_Enter>", self._on_return)
        text.bind("<Tab>", self._on_tab)
        text.bind("<ISO_Left_Tab>", self._on_shift_tab)
        text.bind("<Shift-Tab>", self._on_shift_tab)
        text.bind("<BackSpace>", self._on_backspace)
        text.bind("<<Paste>>", lambda e: text.after_idle(self.schedule_highlight), add="+")
        text.bind("<Configure>", lambda e: text.after_idle(self.draw_line_numbers), add="+")
        text.focus_set()

    # -- file handling -----------------------------------------------------

    def get_text(self):
        return self.text.get("1.0", "end-1c")

    def set_text(self, content, path=None):
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)
        self.text.edit_reset()
        self.text.edit_modified(False)
        self.path = path
        self.text.mark_set("insert", "1.0")
        self.text.see("1.0")
        self.rehighlight()

    @property
    def modified(self):
        return bool(self.text.edit_modified())

    def mark_saved(self):
        self.text.edit_modified(False)

    # -- events ------------------------------------------------------------

    def _on_changed(self, event=None):
        self.schedule_highlight()

    def _on_view_changed(self, event=None):
        self.text.after_idle(self.draw_line_numbers)
        self.text.after_idle(self.app.update_cursor_status)

    def _on_modified_flag(self, event=None):
        self.app.update_title()

    def _on_return(self, event):
        if self.popup.active:
            return self.popup.handle_key(event)
        if self.text.tag_ranges("sel"):
            self.text.delete("sel.first", "sel.last")
        self.insert_newline_and_indent()
        return "break"

    def _on_tab(self, event):
        if self.popup.active:
            return self.popup.handle_key(event)
        if self.text.tag_ranges("sel"):
            self.indent_region()
            return "break"
        changed = self.reindent_line()
        if not changed:
            _, word = word_before(self.text)
            if word:
                self.popup.open(explicit=True)
        return "break"

    def _on_shift_tab(self, event):
        self.indent_region(dedent=True)
        return "break"

    def _on_backspace(self, event):
        if self.text.tag_ranges("sel"):
            return None
        # in leading whitespace, delete back to the previous indent stop
        line_start = self.text.index("insert linestart")
        seg = self.text.get(line_start, "insert")
        if seg and seg.strip() == "" and len(seg) > 1:
            n = len(seg) % 2 or 2
            self.text.delete("insert-%dc" % n, "insert")
            return "break"
        return None

    # -- highlighting ------------------------------------------------------

    def rehighlight(self):
        self.token_text, self.tokens = self.highlight_region("1.0", "end-1c")
        self.text.tag_raise("found")
        self.text.tag_raise("sel")
        names = local_names_from_tokens(self.tokens)
        if names != self._last_local_names:
            self._last_local_names = names
            self.app.index.set_local_names(names)
        self.update_match()
        self.draw_line_numbers()
        self.app.update_cursor_status()

    def draw_line_numbers(self):
        c = self.gutter
        c.delete("all")
        t = self.text
        i = t.index("@0,0")
        last = int(t.index("end-1c").split(".")[0])
        width = max(3, len(str(last)))
        c.configure(width=self.app.font.measure("0" * width) + 14)
        cur = int(t.index("insert").split(".")[0])
        linespace = self.app.font.metrics("linespace")
        descent = self.app.font.metrics("descent")
        while True:
            info = t.dlineinfo(i)      # (x, y, width, height, baseline)
            if info is None:
                break
            line = int(i.split(".")[0])
            if info[3] >= linespace:   # skip lines clipped at the top or bottom
                y = info[1] + info[4] + descent   # bottom of the glyph box
                c.create_text(c.winfo_width() - 7, y, anchor="se", text=str(line),
                              font=self.app.font,
                              fill=self.app.theme["fg"] if line == cur else self.app.theme["gutter_fg"])
            elif info[1] > 0:
                break                  # clipped at the bottom: nothing further is visible
            i = t.index("%s+1line" % i)
            if int(i.split(".")[0]) > last:
                break

    def apply_theme(self, theme):
        super().apply_theme(theme)
        self.gutter.configure(bg=theme["gutter_bg"])
        self.text.tag_configure("found", background=theme["found_bg"])
        self.draw_line_numbers()

    # -- editing commands --------------------------------------------------

    def _selected_lines(self):
        t = self.text
        if t.tag_ranges("sel"):
            first = int(t.index("sel.first").split(".")[0])
            last_idx = t.index("sel.last")
            last = int(last_idx.split(".")[0])
            if last_idx.endswith(".0") and last > first:
                last -= 1
        else:
            first = last = int(t.index("insert").split(".")[0])
        return first, last

    def indent_region(self, dedent=False):
        t = self.text
        first, last = self._selected_lines()
        t.edit_separator()
        for line in range(first, last + 1):
            ls = "%d.0" % line
            if dedent:
                seg = t.get(ls, "%s lineend" % ls)
                n = min(2, len(seg) - len(seg.lstrip(" ")))
                if n:
                    t.delete(ls, "%s+%dc" % (ls, n))
            else:
                t.mark_set("insert", ls)
                self.reindent_line()
        t.tag_add("sel", "%d.0" % first, "%d.0 lineend" % last)
        t.edit_separator()

    def toggle_comment(self):
        t = self.text
        first, last = self._selected_lines()
        lines = [t.get("%d.0" % n, "%d.0 lineend" % n) for n in range(first, last + 1)]
        nonblank = [l for l in lines if l.strip()]
        all_commented = bool(nonblank) and all(l.lstrip().startswith(";") for l in nonblank)
        t.edit_separator()
        if all_commented:
            for k, line in enumerate(lines):
                if not line.strip():
                    continue
                lead = len(line) - len(line.lstrip())
                m = re.match(r";+ ?", line[lead:])
                t.delete("%d.%d" % (first + k, lead), "%d.%d" % (first + k, lead + m.end()))
        else:
            indent = min((len(l) - len(l.lstrip()) for l in nonblank), default=0)
            for k, line in enumerate(lines):
                if not line.strip():
                    continue
                t.insert("%d.%d" % (first + k, indent), ";; ")
        t.edit_separator()
        t.tag_add("sel", "%d.0" % first, "%d.0 lineend" % last)

    def undo(self):
        self.text.edit_undo()

    def redo(self):
        self.text.edit_redo()

    def goto_line(self, line):
        self.text.mark_set("insert", "%d.0" % line)
        self.text.see("insert")

    # -- find --------------------------------------------------------------

    def show_find(self):
        self.findbar.pack(fill="x", before=self.frame.winfo_children()[1])
        if self.text.tag_ranges("sel"):
            sel = self.text.get("sel.first", "sel.last")
            if "\n" not in sel:
                self.find_var.set(sel)
        self.find_entry.focus_set()
        self.find_entry.select_range(0, "end")

    def hide_find(self):
        self.findbar.pack_forget()
        self.text.tag_remove("found", "1.0", "end")
        self.text.focus_set()

    def _find_incremental(self, event):
        if event.keysym in ("Return", "Escape", "Shift_L", "Shift_R", "Tab"):
            return
        self.find_next(1, from_index="insert", incremental=True)

    def find_next(self, direction=1, from_index=None, incremental=False):
        needle = self.find_var.get()
        t = self.text
        t.tag_remove("found", "1.0", "end")
        if not needle:
            self.find_status.configure(text="")
            return
        if from_index is None:
            from_index = "insert" if direction < 0 else "insert+1c"
            if incremental:
                from_index = "insert"
        if incremental:
            start = t.index("sel.first") if t.tag_ranges("sel") else t.index("insert")
        else:
            start = from_index
        kw = dict(nocase=True, backwards=direction < 0)
        pos = t.search(needle, start, stopindex="1.0" if direction < 0 else "end", **kw)
        if not pos:
            pos = t.search(needle, "end" if direction < 0 else "1.0", **kw)
            wrapped = True
        else:
            wrapped = False
        if not pos:
            self.find_status.configure(text="Not found")
            return
        end = "%s+%dc" % (pos, len(needle))
        t.tag_remove("sel", "1.0", "end")
        t.tag_add("sel", pos, end)
        t.tag_add("found", pos, end)
        t.mark_set("insert", end if direction > 0 and not incremental else pos)
        t.see(pos)
        self.find_status.configure(text="Wrapped" if wrapped else "")

    # -- what to run -------------------------------------------------------

    def form_at_cursor(self):
        """Selected text, or the top-level form around the cursor."""
        t = self.text
        if t.tag_ranges("sel"):
            return t.get("sel.first", "sel.last")
        if self._hl_job is not None:
            self.text.after_cancel(self._hl_job)
            self._hl_job = None
            self.rehighlight()
        span = toplevel_form_at(self.tokens, self._offset_of("insert"))
        if span is None:
            return None
        a, b = self._index_of(span[0]), self._index_of(span[1])
        t.tag_remove("sel", "1.0", "end")
        t.tag_add("found", a, b)
        t.after(400, lambda: t.tag_remove("found", "1.0", "end"))
        return self.token_text[span[0]:span[1]]


class Console(SchemeTextBase):
    """The interactive REPL pane."""

    def __init__(self, app, master):
        self.frame = ttk.Frame(master)
        text = tk.Text(self.frame, wrap="word", undo=False, font=app.font, bd=0, padx=6,
                       pady=4, highlightthickness=0, insertwidth=2)
        vs = ttk.Scrollbar(self.frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=vs.set)
        vs.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        self._init_text_common(app, text)
        for tag in ("prompt", "input", "value", "error", "info"):
            text.tag_configure(tag)
        text.mark_set("input_start", "end-1c")
        text.mark_gravity("input_start", "left")
        text.mark_set("prompt_start", "end-1c")
        text.mark_gravity("prompt_start", "left")
        self.history = []
        self.history_pos = 0
        self.history_draft = ""

        text.bind("<Key>", self._on_key)
        text.bind("<KeyRelease>", lambda e: self.schedule_highlight(20), add="+")
        text.bind("<Return>", self._on_return)
        text.bind("<KP_Enter>", self._on_return)
        text.bind("<Shift-Return>", lambda e: self._on_return(e, force=True))
        text.bind("<Tab>", self._on_tab)
        text.bind("<BackSpace>", self._on_backspace)
        text.bind("<Delete>", self._on_delete)
        text.bind("<Up>", lambda e: self._history_key(e, -1))
        text.bind("<Down>", lambda e: self._history_key(e, 1))
        text.bind("<Alt-p>", lambda e: self.history_step(-1))
        text.bind("<Alt-n>", lambda e: self.history_step(1))
        text.bind("<Control-p>", lambda e: self.history_step(-1))
        text.bind("<Control-n>", lambda e: self.history_step(1))
        text.bind("<Control-c>", self._on_control_c)
        text.bind("<Control-l>", lambda e: (self.clear(), "break")[1])
        text.bind("<<Paste>>", self._on_paste)
        text.bind("<Home>", self._on_home)

    def apply_theme(self, theme):
        super().apply_theme(theme, console=True)
        t = self.text
        t.tag_configure("prompt", foreground=theme["prompt"], font=self.app.bold_font)
        t.tag_configure("input", foreground=theme["input"])
        t.tag_configure("value", foreground=theme["value"])
        t.tag_configure("error", foreground=theme["error"])
        t.tag_configure("info", foreground=theme["info"], font=self.app.italic_font)
        t.tag_raise("sel")

    # -- output ------------------------------------------------------------

    def write(self, s, tag=None):
        """Insert output before the (uncommitted) input region."""
        if not s:
            return
        t = self.text
        idx = t.index("input_start")
        t.insert(idx, s, tag or "output")
        t.mark_set("input_start", "%s+%dc" % (idx, len(s)))
        t.see("end")
        self._trim()

    MAX_LINES = 20000

    def _trim(self):
        t = self.text
        last = int(t.index("end-1c").split(".")[0])
        if last > self.MAX_LINES:
            t.delete("1.0", "%d.0" % (last - self.MAX_LINES + 1000))

    def at_line_start(self):
        return self.text.compare("input_start", "==", "1.0") or \
            self.text.get("input_start-1c", "input_start") == "\n"

    def fresh_line(self):
        if not self.at_line_start():
            self.write("\n")

    def show_prompt(self, text):
        self.fresh_line()
        self.write("\n")
        self.text.mark_set("prompt_start", "input_start")
        self.write(text, "prompt")
        self.text.see("end")

    def tag_error_lines(self):
        """Colour the ";..." lines Scheme printed since the last prompt."""
        t = self.text
        start = t.index("prompt_start")
        end = t.index("input_start")
        idx = start
        while t.compare(idx, "<", end):
            line_end = t.index("%s lineend" % idx)
            if t.get(idx, "%s+1c" % idx) == ";":
                t.tag_add("error", idx, line_end)
            idx = t.index("%s+1line linestart" % idx)
            if t.compare(idx, "<=", start):
                break
            start = idx

    def note(self, s):
        self.fresh_line()
        self.write(s + "\n", "info")

    def clear(self):
        t = self.text
        t.delete("1.0", "prompt_start")

    # -- input -------------------------------------------------------------

    def input_text(self):
        return self.text.get("input_start", "end-1c")

    def replace_input(self, s):
        self.text.delete("input_start", "end-1c")
        self.text.insert("input_start", s, "input")
        self.text.mark_set("insert", "end-1c")
        self.text.see("end")
        self.schedule_highlight(0)

    def submit(self, s, echo=True):
        """Send ``s`` as if typed (echoing it after the prompt)."""
        if echo:
            self.write(s + "\n", "input")
        if s.strip():
            self._remember(s)
        self.app.send(s + "\n")

    def commit_input(self):
        t = self.text
        s = self.input_text()
        t.mark_set("insert", "end-1c")
        t.insert("end-1c", "\n")
        t.tag_add("input", "input_start", "end-1c")
        for tag in TOKEN_TAGS:
            t.tag_remove(tag, "input_start", "end-1c")
        t.mark_set("input_start", "end-1c")
        t.see("end")
        if s.strip():
            self._remember(s)
        self.app.send(s + "\n")

    def _remember(self, s):
        if not self.history or self.history[-1] != s:
            self.history.append(s)
            del self.history[:-500]
        self.history_pos = len(self.history)

    def _on_return(self, event, force=False):
        if self.popup.active and not force:
            return self.popup.handle_key(event)
        s = self.input_text()
        if force or is_complete_input(s):
            self.commit_input()
        else:
            self._ensure_in_input()
            self.insert_newline_and_indent(region_start="input_start")
        return "break"

    def _on_tab(self, event):
        if self.popup.active:
            return self.popup.handle_key(event)
        self._ensure_in_input()
        if not self.reindent_line(region_start="input_start"):
            _, word = word_before(self.text)
            if word:
                self.popup.open(explicit=True)
        return "break"

    def _in_input(self, index="insert"):
        return self.text.compare(index, ">=", "input_start")

    def _ensure_in_input(self):
        if not self._in_input():
            self.text.mark_set("insert", "end-1c")

    def _on_key(self, event):
        r = self._on_key_common(event)
        if r:
            return r
        if event.keysym in ("Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L",
                            "Alt_R", "Meta_L", "Meta_R", "Super_L", "Super_R"):
            return None
        if not event.char or event.state & MODIFIER_MASK:
            return None
        if not self._in_input():
            # typing outside the input region: jump to the input
            self.text.tag_remove("sel", "1.0", "end")
            self.text.mark_set("insert", "end-1c")
        if self.text.tag_ranges("sel") and \
                self.text.compare("sel.first", "<", "input_start"):
            self.text.tag_remove("sel", "1.0", "end")
            self.text.mark_set("insert", "end-1c")
        return None

    def _on_backspace(self, event):
        if self.text.tag_ranges("sel"):
            if self.text.compare("sel.first", "<", "input_start"):
                return "break"
            return None
        if not self._in_input() or self.text.compare("insert", "==", "input_start"):
            return "break"
        return None

    def _on_delete(self, event):
        if self.text.tag_ranges("sel") and self.text.compare("sel.first", "<", "input_start"):
            return "break"
        if not self._in_input():
            return "break"
        return None

    def _on_home(self, event):
        if self._in_input() and self.text.compare("insert linestart", "<=", "input_start"):
            self.text.mark_set("insert", "input_start")
            return "break"
        return None

    def _on_paste(self, event):
        self._ensure_in_input()
        self.text.after_idle(lambda: self.schedule_highlight(0))
        return None

    def _on_control_c(self, event):
        if self.text.tag_ranges("sel"):
            return None       # let the default copy binding run
        self.app.interrupt()
        return "break"

    def _history_key(self, event, direction):
        if self.popup.active:
            return self.popup.handle_key(event)
        if not self._in_input():
            return None
        cur = int(self.text.index("insert").split(".")[0])
        first = int(self.text.index("input_start").split(".")[0])
        last = int(self.text.index("end-1c").split(".")[0])
        if (direction < 0 and cur == first) or (direction > 0 and cur == last):
            self.history_step(direction)
            return "break"
        return None

    def history_step(self, direction):
        if not self.history:
            return "break"
        if self.history_pos == len(self.history):
            self.history_draft = self.input_text()
        pos = self.history_pos + direction
        if pos < 0:
            return "break"
        if pos >= len(self.history):
            self.history_pos = len(self.history)
            self.replace_input(self.history_draft)
            return "break"
        self.history_pos = pos
        self.replace_input(self.history[pos])
        return "break"

    # -- highlighting of the input region ----------------------------------

    def rehighlight(self):
        self.token_text, self.tokens = self.highlight_region("input_start", "end-1c")
        self.text.tag_raise("sel")
        self.update_match()

    def _tokens_for_match(self):
        return self.tokens, self._offset_of("input_start")


class IDE:
    """The application window."""

    def __init__(self, root, args):
        self.root = root
        self.args = args
        self.prefs = Prefs.load()
        if args.dark:
            self.prefs["dark"] = True
        if args.font_size:
            self.prefs["font_size"] = args.font_size
        self.theme = DARK_THEME if self.prefs["dark"] else LIGHT_THEME
        self.mac = root.tk.call("tk", "windowingsystem") == "aqua"
        self.mod = "Command" if self.mac else "Control"
        self.mod_label = "\u2318" if self.mac else "Ctrl+"
        self.tmpdir = tempfile.mkdtemp(prefix="mit-scheme-ide-")
        self.dump_path = os.path.join(self.tmpdir, "bindings.tsv")
        self.index = CompletionIndex()
        self.parser = EmacsProtocolParser()
        self.proc = None
        self.level = 1
        self.prompt_name = "[Evaluator]"
        self.repl_ready = False
        self.hidden_active = False
        self.hidden_queue = []       # (expression, callback)
        self.pending_sends = []
        self.output_since_prompt = False
        self.refresh_wanted = False
        self.first_dump_done = False
        self.cwd = os.getcwd()
        self.scheme_args = list(args.scheme_args or [])

        self._make_fonts()
        self._make_widgets()
        self._make_menus()
        self._bind_keys()
        self.apply_theme()
        root.protocol("WM_DELETE_WINDOW", self.on_quit)
        if self.mac:
            try:
                root.createcommand("tk::mac::Quit", self.on_quit)
                root.createcommand("::tk::mac::OpenDocument", self.open_paths)
            except tk.TclError:
                pass
        root.geometry(self.prefs.get("geometry") or "1100x800")
        root.after(100, self._restore_sash)

        if args.file:
            self.open_file(args.file)
        else:
            self.editor.set_text(
                ";; MIT/GNU Scheme -- %sR or F5 runs this buffer in the console below;\n"
                ";; %s\u21a9 sends the definition at the cursor.  Tab indents, Ctrl-Space completes.\n\n"
                % (self.mod_label, self.mod_label))
        self.update_title()
        self.start_scheme()
        root.after(30, self.poll)

    # -- construction ------------------------------------------------------

    def _make_fonts(self):
        size = int(self.prefs.get("font_size", 13))
        base = tkfont.nametofont("TkFixedFont")
        family = base.actual("family")
        if self.mac:
            family = "Menlo"
        else:
            for cand in ("DejaVu Sans Mono", "Liberation Mono", "Noto Sans Mono", family):
                if cand in tkfont.families():
                    family = cand
                    break
        self.font = tkfont.Font(family=family, size=size)
        self.bold_font = tkfont.Font(family=family, size=size, weight="bold")
        self.italic_font = tkfont.Font(family=family, size=size, slant="italic")

    def _make_widgets(self):
        root = self.root
        root.title(APP_NAME)
        self.toolbar = ttk.Frame(root, padding=(4, 3))
        self.toolbar.pack(fill="x")
        self.run_button = ttk.Button(self.toolbar, text="\u25b6 Run", command=self.run_buffer, width=8)
        self.run_button.pack(side="left", padx=2)
        self.stop_button = ttk.Button(self.toolbar, text="\u25a0 Stop", command=self.interrupt, width=8)
        self.stop_button.pack(side="left", padx=2)
        ttk.Button(self.toolbar, text="\u21bb Restart", command=self.restart_scheme, width=10).pack(side="left", padx=2)
        ttk.Separator(self.toolbar, orient="vertical").pack(side="left", fill="y", padx=6, pady=2)
        ttk.Button(self.toolbar, text="Open", command=self.open_dialog, width=6).pack(side="left", padx=2)
        ttk.Button(self.toolbar, text="Save", command=self.save, width=6).pack(side="left", padx=2)
        self.repl_state = ttk.Label(self.toolbar, text="Starting\u2026", anchor="e")
        self.repl_state.pack(side="right", padx=6)

        self.paned = ttk.PanedWindow(root, orient="vertical")
        self.paned.pack(fill="both", expand=True)
        self.editor = Editor(self, self.paned)
        self.console = Console(self, self.paned)
        self.paned.add(self.editor.frame, weight=3)
        self.paned.add(self.console.frame, weight=2)

        self.status = ttk.Frame(root, padding=(6, 2))
        self.status.pack(fill="x")
        self.hint_label = ttk.Label(self.status, text="", anchor="w")
        self.hint_label.pack(side="left", fill="x", expand=True)
        self.pos_label = ttk.Label(self.status, text="Ln 1, Col 1", anchor="e", width=16)
        self.pos_label.pack(side="right")
        self.exe_label = ttk.Label(self.status, text="", anchor="e")
        self.exe_label.pack(side="right", padx=12)

    def _restore_sash(self):
        try:
            frac = float(self.prefs.get("sash", 0.6))
            h = self.paned.winfo_height()
            if h > 100:
                self.paned.sashpos(0, int(h * frac))
        except (tk.TclError, ValueError):
            pass

    def _acc(self, key, shift=False):
        if self.mac:
            return ("\u21e7" if shift else "") + "\u2318" + key.upper()
        return "Ctrl+" + ("Shift+" if shift else "") + key.upper()

    def _make_menus(self):
        root = self.root
        menubar = tk.Menu(root)
        m = self.mod
        A = self._acc

        filem = tk.Menu(menubar, tearoff=0)
        filem.add_command(label="New", accelerator=A("n"), command=self.new_file)
        filem.add_command(label="Open\u2026", accelerator=A("o"), command=self.open_dialog)
        self.recent_menu = tk.Menu(filem, tearoff=0)
        filem.add_cascade(label="Open Recent", menu=self.recent_menu)
        filem.add_separator()
        filem.add_command(label="Save", accelerator=A("s"), command=self.save)
        filem.add_command(label="Save As\u2026", accelerator=A("s", True), command=self.save_as)
        filem.add_separator()
        filem.add_command(label="Quit", accelerator=A("q"), command=self.on_quit)
        menubar.add_cascade(label="File", menu=filem)

        editm = tk.Menu(menubar, tearoff=0)
        editm.add_command(label="Undo", accelerator=A("z"), command=self.editor.undo)
        editm.add_command(label="Redo", accelerator=A("z", True) if self.mac else "Ctrl+Y",
                          command=self.editor.redo)
        editm.add_separator()
        editm.add_command(label="Cut", accelerator=A("x"), command=lambda: self._focused_event("<<Cut>>"))
        editm.add_command(label="Copy", accelerator=A("c"), command=lambda: self._focused_event("<<Copy>>"))
        editm.add_command(label="Paste", accelerator=A("v"), command=lambda: self._focused_event("<<Paste>>"))
        editm.add_command(label="Select All", accelerator=A("a"), command=self.select_all)
        editm.add_separator()
        editm.add_command(label="Find\u2026", accelerator=A("f"), command=self.editor.show_find)
        editm.add_command(label="Find Next", accelerator=A("g"), command=lambda: self.editor.find_next(1))
        editm.add_command(label="Find Previous", accelerator=A("g", True), command=lambda: self.editor.find_next(-1))
        editm.add_separator()
        editm.add_command(label="Toggle Comment", accelerator=self.mod_label + "/", command=self.editor.toggle_comment)
        editm.add_command(label="Indent Region", accelerator="Tab", command=self.editor.indent_region)
        editm.add_command(label="Complete Symbol", accelerator="Ctrl+Space", command=self.editor._complete_explicit)
        menubar.add_cascade(label="Edit", menu=editm)

        runm = tk.Menu(menubar, tearoff=0)
        runm.add_command(label="Run Buffer", accelerator=A("r") + " / F5", command=self.run_buffer)
        runm.add_command(label="Send Selection or Definition", accelerator=self.mod_label + "\u21a9",
                         command=self.send_form)
        runm.add_command(label="Load File\u2026", accelerator=A("l", True), command=self.load_dialog)
        runm.add_separator()
        runm.add_command(label="Interrupt (^G)", accelerator="\u2318." if self.mac else "Ctrl+C in console",
                         command=self.interrupt)
        runm.add_command(label="Restart Scheme", accelerator=A("r", True), command=self.restart_scheme)
        runm.add_command(label="Clear Console", accelerator=A("k"), command=self.console.clear)
        runm.add_separator()
        runm.add_command(label="Choose Interpreter\u2026", command=self.choose_interpreter)
        menubar.add_cascade(label="Run", menu=runm)

        viewm = tk.Menu(menubar, tearoff=0)
        self.autocomplete_var = tk.BooleanVar(value=bool(self.prefs.get("autocomplete", True)))
        self.dark_var = tk.BooleanVar(value=bool(self.prefs.get("dark", False)))
        viewm.add_checkbutton(label="Complete While Typing", variable=self.autocomplete_var,
                              command=self._toggle_autocomplete)
        viewm.add_checkbutton(label="Dark Theme", variable=self.dark_var, command=self._toggle_dark)
        viewm.add_separator()
        viewm.add_command(label="Bigger Text", accelerator=A("="), command=lambda: self.zoom(1))
        viewm.add_command(label="Smaller Text", accelerator=A("-"), command=lambda: self.zoom(-1))
        viewm.add_command(label="Reset Text Size", accelerator=A("0"), command=lambda: self.zoom(0))
        viewm.add_separator()
        viewm.add_command(label="Focus Editor", accelerator=A("1"), command=lambda: self.editor.text.focus_set())
        viewm.add_command(label="Focus Console", accelerator=A("2"), command=lambda: self.console.text.focus_set())
        menubar.add_cascade(label="View", menu=viewm)

        helpm = tk.Menu(menubar, tearoff=0)
        helpm.add_command(label="Keyboard Shortcuts", command=self.show_shortcuts)
        helpm.add_command(label="MIT/GNU Scheme Reference Manual", command=self.open_manual)
        helpm.add_command(label="About " + APP_NAME, command=self.show_about)
        menubar.add_cascade(label="Help", menu=helpm)
        root.config(menu=menubar)
        self._rebuild_recent_menu()

    def _bind_keys(self):
        root = self.root
        m = self.mod

        texts = (self.editor.text, self.console.text)

        def bind(seq, fn):
            handler = lambda e, fn=fn: (fn(), "break")[1]
            for t in texts:
                t.bind(seq, handler)
            root.bind_all(seq, handler)

        bind("<%s-n>" % m, self.new_file)
        bind("<%s-o>" % m, self.open_dialog)
        bind("<%s-s>" % m, self.save)
        bind("<Shift-%s-S>" % m, self.save_as)
        bind("<%s-q>" % m, self.on_quit)
        bind("<%s-f>" % m, self.editor.show_find)
        bind("<%s-g>" % m, lambda: self.editor.find_next(1))
        bind("<Shift-%s-G>" % m, lambda: self.editor.find_next(-1))
        bind("<F3>", lambda: self.editor.find_next(1))
        bind("<Shift-F3>", lambda: self.editor.find_next(-1))
        bind("<%s-slash>" % m, self.editor.toggle_comment)
        bind("<%s-r>" % m, self.run_buffer)
        bind("<F5>", self.run_buffer)
        bind("<%s-Return>" % m, self.send_form)
        bind("<Shift-%s-L>" % m, self.load_dialog)
        bind("<Shift-%s-R>" % m, self.restart_scheme)
        bind("<%s-k>" % m, self.console.clear)
        bind("<%s-equal>" % m, lambda: self.zoom(1))
        bind("<%s-plus>" % m, lambda: self.zoom(1))
        bind("<%s-minus>" % m, lambda: self.zoom(-1))
        bind("<%s-0>" % m, lambda: self.zoom(0))
        bind("<%s-1>" % m, lambda: self.editor.text.focus_set())
        bind("<%s-2>" % m, lambda: self.console.text.focus_set())
        if self.mac:
            bind("<Command-period>", self.interrupt)
            self.editor.text.bind("<Command-z>", lambda e: (self.editor.undo(), "break")[1])
            self.editor.text.bind("<Shift-Command-Z>", lambda e: (self.editor.redo(), "break")[1])
        else:
            self.editor.text.bind("<Control-z>", lambda e: (self.editor.undo(), "break")[1])
            self.editor.text.bind("<Control-y>", lambda e: (self.editor.redo(), "break")[1])
            self.editor.text.bind("<Shift-Control-Z>", lambda e: (self.editor.redo(), "break")[1])
        bind("<%s-a>" % m, self.select_all)
        self.editor.text.bind("<Escape>", lambda e: self.editor.popup.close())
        self.console.text.bind("<Escape>", lambda e: self.console.popup.close())

    def _focused_event(self, virtual):
        w = self.root.focus_get()
        if w is not None:
            w.event_generate(virtual)

    def select_all(self):
        w = self.root.focus_get()
        if isinstance(w, tk.Text):
            w.tag_add("sel", "1.0", "end-1c")
        elif isinstance(w, (tk.Entry, ttk.Entry)):
            w.select_range(0, "end")

    # -- theme / fonts -----------------------------------------------------

    def apply_theme(self):
        self.editor.apply_theme(self.theme)
        self.console.apply_theme(self.theme)

    def _toggle_dark(self):
        self.prefs["dark"] = bool(self.dark_var.get())
        self.theme = DARK_THEME if self.prefs["dark"] else LIGHT_THEME
        self.apply_theme()
        self.prefs.save()

    def _toggle_autocomplete(self):
        self.prefs["autocomplete"] = bool(self.autocomplete_var.get())
        self.prefs.save()

    def zoom(self, delta):
        size = Prefs.DEFAULTS["font_size"] if delta == 0 else max(7, self.font.actual("size") + delta)
        for f in (self.font, self.bold_font, self.italic_font):
            f.configure(size=size)
        self.prefs["font_size"] = size
        self.prefs.save()
        self.editor.draw_line_numbers()

    # -- status ------------------------------------------------------------

    def set_hint(self, text):
        self.hint_label.configure(text=text or "")

    def set_state(self, text):
        self.repl_state.configure(text=text)

    def update_cursor_status(self):
        try:
            line, col = self.editor.text.index("insert").split(".")
        except tk.TclError:
            return
        self.pos_label.configure(text="Ln %s, Col %d" % (line, int(col) + 1))
        # signature hint for the enclosing call
        if self.root.focus_get() is self.editor.text and not self.editor.popup.active:
            name, _ = enclosing_operator(self.editor.tokens, self.editor._offset_of("insert"))
            self.set_hint(self.index.describe(name) if name else "")

    def update_title(self):
        name = os.path.basename(self.editor.path) if self.editor.path else "Untitled"
        star = " \u2022" if self.editor.modified else ""
        self.root.title("%s%s \u2014 %s" % (name, star, APP_NAME))

    # -- files -------------------------------------------------------------

    def _confirm_discard(self):
        if not self.editor.modified:
            return True
        ans = messagebox.askyesnocancel(APP_NAME, "Save changes to %s?" %
                                        (os.path.basename(self.editor.path) if self.editor.path else "Untitled"))
        if ans is None:
            return False
        if ans:
            return self.save()
        return True

    def new_file(self):
        if not self._confirm_discard():
            return
        self.editor.set_text("")
        self.update_title()

    def open_dialog(self):
        if not self._confirm_discard():
            return
        path = filedialog.askopenfilename(
            title="Open Scheme file", initialdir=self._initial_dir(),
            filetypes=[("Scheme files", "*.scm *.sld *.ss *.sls *.pkg"), ("All files", "*")])
        if path:
            self.open_file(path)

    def open_paths(self, *paths):
        for p in paths:
            if self._confirm_discard():
                self.open_file(p)

    def _initial_dir(self):
        if self.editor.path:
            return os.path.dirname(self.editor.path)
        return self.cwd

    def open_file(self, path):
        path = os.path.abspath(path)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError as e:
            if not os.path.exists(path):
                content = ""       # new file to be created on save
            else:
                messagebox.showerror(APP_NAME, "Cannot open %s:\n%s" % (path, e))
                return
        self.editor.set_text(content, path)
        self._add_recent(path)
        self.update_title()
        self.editor.text.focus_set()

    def save(self):
        if not self.editor.path:
            return self.save_as()
        return self._write(self.editor.path)

    def save_as(self):
        path = filedialog.asksaveasfilename(
            title="Save Scheme file", initialdir=self._initial_dir(),
            initialfile=os.path.basename(self.editor.path) if self.editor.path else "untitled.scm",
            defaultextension=".scm", filetypes=[("Scheme files", "*.scm"), ("All files", "*")])
        if not path:
            return False
        return self._write(path)

    def _write(self, path):
        content = self.editor.get_text()
        if content and not content.endswith("\n"):
            content += "\n"
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as e:
            messagebox.showerror(APP_NAME, "Cannot save %s:\n%s" % (path, e))
            return False
        self.editor.path = path
        self.editor.mark_saved()
        self._add_recent(path)
        self.update_title()
        return True

    def _add_recent(self, path):
        recent = [p for p in self.prefs.get("recent", []) if p != path]
        recent.insert(0, path)
        self.prefs["recent"] = recent[:10]
        self.prefs.save()
        self._rebuild_recent_menu()

    def _rebuild_recent_menu(self):
        m = self.recent_menu
        m.delete(0, "end")
        for p in self.prefs.get("recent", []):
            m.add_command(label=p, command=lambda p=p: self._confirm_discard() and self.open_file(p))
        if not self.prefs.get("recent"):
            m.add_command(label="(empty)", state="disabled")

    # -- the interpreter ---------------------------------------------------

    def start_scheme(self):
        exe = find_scheme_executable(self.args.scheme, self.prefs.get("scheme_exe"))
        if not exe:
            self.exe_label.configure(text="no interpreter")
            self.set_state("No interpreter")
            self.console.note("No mit-scheme executable found.  Use Run \u2192 Choose Interpreter\u2026, "
                              "pass --scheme PATH, or set MIT_SCHEME_EXE.")
            return False
        self.parser = EmacsProtocolParser()
        self.repl_ready = False
        self.hidden_active = False
        self.hidden_queue = []
        self.first_dump_done = False
        self.output_since_prompt = False
        try:
            self.proc = SchemeProcess(exe, self.scheme_args, cwd=self.cwd)
        except OSError as e:
            self.proc = None
            self.set_state("Failed to start")
            self.console.note("Cannot start %s: %s" % (exe, e))
            return False
        self.exe_label.configure(text=exe)
        self.set_state("Starting\u2026")
        self.console.note("Started %s (pid %d)" % (" ".join(self.proc.argv), self.proc.pid))
        self.request_hidden(dump_expression(self.dump_path, skip_global=False),
                            lambda: self._load_dump(full=True))
        return True

    def restart_scheme(self):
        if self.proc is not None:
            self.proc.terminate()
            self.proc = None
        self.pending_sends = []
        self.console.fresh_line()
        self.start_scheme()

    def choose_interpreter(self):
        path = filedialog.askopenfilename(title="Choose the mit-scheme executable",
                                          initialdir=os.path.dirname(self.proc.executable) if self.proc else "/")
        if not path:
            return
        self.prefs["scheme_exe"] = path
        self.prefs.save()
        self.args.scheme = path
        self.restart_scheme()

    def interrupt(self):
        if self.proc is not None and self.proc.alive:
            self.proc.interrupt()
            self.set_state("Interrupting\u2026")

    def send(self, text):
        """Send user input to the REPL (queued behind an invisible eval)."""
        if self.proc is None or not self.proc.alive:
            if not self.start_scheme():
                return
            self.pending_sends.append(text)
            return
        if self.hidden_active or (not self.first_dump_done and self.hidden_queue):
            self.pending_sends.append(text)
            return
        self._send_now(text)

    def _send_now(self, text):
        self.repl_ready = False
        self.refresh_wanted = True
        self.proc.send(text)

    def request_hidden(self, expression, callback):
        self.hidden_queue.append((expression, callback))
        self._dispatch()

    def _dispatch(self):
        if self.proc is None or not self.proc.alive or self.hidden_active:
            return
        if not self.repl_ready:
            return
        if self.pending_sends:
            sends, self.pending_sends = self.pending_sends, []
            for s in sends:
                self._send_now(s)
            return
        if self.hidden_queue:
            expr, cb = self.hidden_queue.pop(0)
            self.hidden_active = True
            self.hidden_callback = cb
            self.repl_ready = False
            self.output_since_prompt = False
            self.proc.send(expr + "\n")
            return
        if self.refresh_wanted and self.first_dump_done:
            self.refresh_wanted = False
            self.request_hidden(dump_expression(self.dump_path, skip_global=True),
                                lambda: self._load_dump(full=False))

    def _load_dump(self, full):
        try:
            self.index.load_dump_file(self.dump_path, full)
        except OSError:
            return
        if full:
            self.first_dump_done = True
        self.editor.rehighlight()
        self.console.rehighlight()
        if full:
            n = len(self.index._sorted)
            self.set_hint("Completion index: %d names from the interpreter" % n)

    # -- the event loop ----------------------------------------------------

    def poll(self):
        if self.proc is not None:
            for chunk in self.proc.read_chunks():
                if chunk is None:
                    self._on_exit()
                    break
                for ev in self.parser.feed(chunk):
                    self.handle_event(ev)
        self.root.after(30, self.poll)

    def _on_exit(self):
        code = self.proc.returncode
        self.proc.terminate()
        self.proc = None
        self.hidden_active = False
        self.repl_ready = False
        self.console.note("Scheme exited%s.  Run or Restart starts it again." %
                          ("" if code in (0, None) else " with code %s" % code))
        self.set_state("Exited")
        self.editor.text.focus_set()

    def handle_event(self, ev):
        kind, arg = ev
        c = self.console
        if kind == "output":
            self.output_since_prompt = True
            c.write(arg)
        elif kind == "bell":
            self.root.bell()
        elif kind == "prompt":
            self.level, self.prompt_name = arg
            if not self.hidden_active or self.output_since_prompt:
                c.show_prompt(prompt_text(self.level, self.prompt_name))
                self.output_since_prompt = False
        elif kind == "ready":
            self.repl_ready = True
            if self.hidden_active:
                self.hidden_active = False
                cb, self.hidden_callback = self.hidden_callback, None
                if cb:
                    cb()
            self.set_state("Ready  %s" % prompt_text(self.level, self.prompt_name).strip())
            self._dispatch()
        elif kind == "read-start":
            pass
        elif kind == "read-finish":
            if not self.hidden_active:
                self.set_state("Evaluating\u2026")
        elif kind == "value":
            if not self.hidden_active:
                c.fresh_line()
                c.write((";Value: %s" % arg if arg else ";Unspecified return value") + "\n", "value")
        elif kind == "value-text":
            if not self.hidden_active:
                c.fresh_line()
                c.write(arg + "\n", "value")
        elif kind == "error":
            self.output_since_prompt = True
            c.tag_error_lines()
            self.set_state("Error")
        elif kind == "gc-start":
            if not self.hidden_active:
                self.set_state("Garbage collecting\u2026")
        elif kind == "gc-end":
            if not self.hidden_active:
                self.set_state("Evaluating\u2026")
        elif kind == "cd":
            self.cwd = arg
        elif kind == "message":
            self.set_hint(arg)
        elif kind in ("expression-prompt", "confirm"):
            self.output_since_prompt = True
            c.fresh_line()
            c.write(arg, "prompt")
        elif kind == "interrupt-ok":
            self.set_state("Interrupted")
        # debugger, read-char, eval, unknown: nothing to do

    # -- running code ------------------------------------------------------

    def run_buffer(self):
        path = self.editor.path
        if path:
            if self.editor.modified and not self.save():
                return
        else:
            path = os.path.join(self.tmpdir, "untitled.scm")
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self.editor.get_text() + "\n")
            except OSError as e:
                messagebox.showerror(APP_NAME, "Cannot write %s:\n%s" % (path, e))
                return
        self.console.submit('(load "%s")' % scheme_string(path))
        self.console.text.see("end")

    def send_form(self):
        w = self.root.focus_get()
        if w is self.console.text:
            self.console._on_return(None, force=True)
            return
        form = self.editor.form_at_cursor()
        if not form:
            self.set_hint("No expression at the cursor")
            return
        self.console.submit(form.strip())

    def load_dialog(self):
        path = filedialog.askopenfilename(title="Load Scheme file into the REPL",
                                          initialdir=self._initial_dir(),
                                          filetypes=[("Scheme files", "*.scm *.sld *.bin *.com"), ("All files", "*")])
        if path:
            self.console.submit('(load "%s")' % scheme_string(path))

    # -- help --------------------------------------------------------------

    def show_shortcuts(self):
        m = self.mod_label
        text = "\n".join([
            "%sR, F5\tRun the editor buffer (saves it first)" % m,
            "%s\u21a9\tSend selection or definition at cursor to the REPL" % m,
            "\u21e7%sL\tLoad a file into the REPL" % m,
            ("\u2318." if self.mac else "Ctrl+C") + "\tInterrupt Scheme (^G) -- in the console",
            "\u21e7%sR\tRestart Scheme" % m,
            "%sK\tClear the console" % m,
            "",
            "Tab\tRe-indent line / complete symbol",
            "Ctrl+Space\tComplete symbol",
            "%s/\tComment or uncomment lines" % m,
            "%sF, %sG\tFind, find next" % (m, m),
            "%s1, %s2\tFocus editor, focus console" % (m, m),
            "",
            "In the console: \u21a9 sends the input when its parentheses balance,",
            "otherwise it starts an indented continuation line; \u21e7\u21a9 sends anyway.",
            "\u2191/\u2193 on the first/last input line walk the history.",
        ])
        messagebox.showinfo("Keyboard Shortcuts", text)

    def open_manual(self):
        import webbrowser
        webbrowser.open(MANUAL_URL)

    def show_about(self):
        exe = self.proc.executable if self.proc else "(none)"
        messagebox.showinfo("About " + APP_NAME,
                            "%s %s\n\nA small editor and console for MIT/GNU Scheme.\n\n"
                            "Interpreter: %s\nPreferences: %s" % (APP_NAME, VERSION, exe, Prefs.path()))

    # -- shutdown ----------------------------------------------------------

    def on_quit(self):
        if not self._confirm_discard():
            return
        try:
            self.prefs["geometry"] = self.root.geometry()
            h = self.paned.winfo_height()
            if h > 0:
                self.prefs["sash"] = self.paned.sashpos(0) / h
        except tk.TclError:
            pass
        self.prefs.save()
        if self.proc is not None:
            self.proc.terminate()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        self.root.destroy()


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="mit_scheme_ide.py",
        usage="%(prog)s [options] [FILE] [-- SCHEME-ARGS...]",
        description="A small IDE for MIT/GNU Scheme.  Arguments after -- go to mit-scheme.")
    p.add_argument("--scheme", metavar="PATH", help="mit-scheme executable (default: $MIT_SCHEME_EXE, then PATH)")
    p.add_argument("--dark", action="store_true", help="start with the dark theme")
    p.add_argument("--font-size", type=int, metavar="N", help="editor font size")
    p.add_argument("file", nargs="?", help="Scheme file to open")
    argv = list(argv)
    scheme_args = []
    if "--" in argv:
        k = argv.index("--")
        argv, scheme_args = argv[:k], argv[k + 1:]
    args = p.parse_args(argv)
    args.scheme_args = scheme_args
    return args


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        _gui_imports()
    except ImportError:
        sys.stderr.write("This program needs Python's tkinter module.  On macOS use the python.org\n"
                         "installer (it bundles Tk) or 'brew install python-tk'; on Debian/Ubuntu\n"
                         "'apt install python3-tk'.\n")
        return 1
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling")
    except tk.TclError:
        pass
    IDE(root, args)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
