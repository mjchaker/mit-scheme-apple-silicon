# Structured concurrency for MIT/GNU Scheme

This port adds a concurrency library modelled on the Swift language's
concurrency system (async/await, structured task groups, cancellation,
task-local values, actors, async streams and checked continuations),
built on the MIT/GNU Scheme runtime's threads.

Source: [`src/runtime/task.scm`](src/runtime/task.scm), package
`(runtime task)`. Tests: [`tests/runtime/test-task.scm`](tests/runtime/test-task.scm)
(`TEST=runtime/test-task make check`).

## Swift to Scheme

| Swift | Scheme |
|---|---|
| `Task { ... }` | `(async body ...)`, `(spawn-task thunk [name])` |
| `Task.detached { ... }` | `(spawn-detached-task thunk [name])` |
| `try await task.value` | `(await task)` |
| `task.cancel()` | `(cancel-task! task)` |
| `Task.isCancelled`, `task.isCancelled` | `(task-cancelled?)`, `(task-cancelled? task)` |
| `try Task.checkCancellation()` | `(check-task-cancellation)` |
| `CancellationError` | `condition-type:task-cancelled`, `cancellation-error?` |
| `withTaskCancellationHandler(operation:onCancel:)` | `(with-task-cancellation-handler on-cancel thunk)` |
| `try await Task.sleep(for: ...)` | `(task-sleep milliseconds)` |
| `await Task.yield()` | `(task-yield)` |
| `async let x = e` | `(async-let ((x e) ...) body ...)` |
| `try await withThrowingTaskGroup { group in ... }` | `(with-task-group (lambda (group) ...))` |
| `group.addTask { ... }` | `(task-group-add! group thunk [name])` |
| `group.addTaskUnlessCancelled { ... }` | `(task-group-add-unless-cancelled! group thunk [name])` |
| `try await group.next()` | `(task-group-next group)` |
| `try await group.waitForAll()` | `(task-group-wait-all group)` |
| `for try await v in group { ... }` | `(task-group-for-each proc group)`, `(task-group->list group)` |
| `group.cancelAll()`, `group.isCancelled`, `group.isEmpty` | `task-group-cancel-all!`, `task-group-cancelled?`, `task-group-empty?` |
| `@TaskLocal static var x = d` | `(make-task-local default [name])` |
| `$x.withValue(v) { ... }`, `x` | `(with-task-local x v thunk)`, `(task-local-ref x)` |
| `actor A { ... }` | `(make-actor [name])` |
| `await a.method()` | `(actor-call a thunk)` |
| `Task { await a.method() }` | `(actor-async a thunk [name])` |
| `#isolation`, `isolated` | `(current-actor)`, `(actor-isolated? a)` |
| `AsyncThrowingStream { continuation in ... }` | `(make-async-stream producer [name])`, `(make-async-stream)` |
| `continuation.yield(v)` | `(async-stream-yield! stream v)` |
| `continuation.finish()`, `.finish(throwing: e)` | `(async-stream-finish! stream)`, `(async-stream-finish! stream condition)` |
| `try await iterator.next()` | `(async-stream-next stream)` |
| `for try await v in stream { ... }` | `(async-stream-for-each proc stream)`, `(async-stream->list stream)` |
| `try await withCheckedThrowingContinuation { k in ... }` | `(with-task-continuation (lambda (k) ...))` |
| `k.resume(returning: v)`, `k.resume(throwing: e)` | `(task-continuation-resume! k v)`, `(task-continuation-fail! k e)` |

Also: `task?`, `task-name`, `task-state` (`running`, `completed` or
`failed`), `task-done?`, `task-thread`, `current-task`,
`cancellation-error/task`, `condition-type:task-cancelled`,
`task-local?`, `task-group?`, `actor?`, `actor-name`, `async-stream?`,
`async-stream-finished?`, `task-continuation?`.

## Semantics

**Tasks.** A task is a thread together with a result cell, a
cancellation flag, its task-local bindings and its structured
children. `await` blocks the calling task until the awaited task has
finished and returns its value. Every thread has a task: threads not
created by this library (the console thread, for instance) get a root
task the first time `current-task` is called in them, so all of the
operations below work from the REPL.

**Errors.** An error signalled inside a task is caught and stored in
the task; every `await` of that task re-signals it in the awaiting
task. Objects raised with `raise` are delivered unchanged, so a
`guard` around `await` sees what the task raised. A task's thread
never drops into a nested error REPL.

**Structure.** Children created with `task-group-add!` or `async-let`
cannot outlive the scope that created them. When the body of
`with-task-group` returns normally, the remaining children are waited
for and their results discarded; a child's error is seen only through
`task-group-next`, which re-signals it, or `task-group-wait-all`,
which on the first failure cancels the remaining children, waits for
them, and then re-signals that failure. If the body signals an error,
or is left through a continuation, all children are cancelled, waited
for, and the error re-signalled.
`async-let` binds each name to a child task; children that were not
awaited by the time the body is left are cancelled and waited for, as
in Swift. Cancelling a task cancels its structured children. A child
added to a cancelled group or under a cancelled parent starts out
cancelled. Tasks made with `spawn-task` are unstructured: they inherit
task-local values but not cancellation; `spawn-detached-task` inherits
nothing.

**Cancellation is cooperative.** `cancel-task!` sets the task's flag,
runs the handlers installed with `with-task-cancellation-handler`
(innermost first, on the cancelling thread; a handler installed after
cancellation runs at once), wakes the task if it is blocked in
`task-sleep` or `async-stream-next`, then cancels its structured
children. The task must notice: `check-task-cancellation` signals
`condition-type:task-cancelled`, `task-sleep` signals it when the task
is or becomes cancelled, and `async-stream-next` returns the EOF
object. An `await` is not interrupted by cancellation, exactly as in
Swift: cancel the awaited task if that is what you mean.

**Task-local values** are bound for the dynamic extent of
`with-task-local` in the current task, and every task spawned within
that extent inherits a snapshot of the bindings in force. They are
distinct from `parameterize`: in MIT/GNU Scheme 12.1 a parameter
binding is not visible from a thread created inside it.

**Actors** serialise the thunks run through `actor-call`: no two of
them run on the same actor at the same time, and inside one
`(current-actor)` is the actor. A call from code already isolated to
the actor runs synchronously. As in Swift, isolation is *released at
every suspension point* (`await`, `task-sleep`, `task-yield`,
`task-group-next`, `async-stream-next`, `with-task-continuation` and
`actor-call` on another actor) and taken back afterwards. So an actor
is re-entrant across an await, two actors may call each other
without deadlock, and a thread never holds more than one actor at a
time. The Swift rule applies: re-check invariants after a suspension
point, since other calls may have run on the actor in the meantime.
Blocking thread-level operations (`sleep-current-thread`,
`thread-queue/dequeue!`, ...) are not suspension points and keep the
actor held.

**Async streams** carry values from a producer to a consumer.
`async-stream-next` blocks until a value is available and returns the
EOF object when the stream has finished and been drained, or when the
consuming task is cancelled. A stream finished with a condition
re-signals it to the consumer once, after the buffered values. Values
are buffered without bound; `async-stream-yield!` returns `#f` once
the stream has finished.

**Task continuations** bridge callback-style code into a task:
`with-task-continuation` calls its argument at once with a one-shot
continuation object and blocks until some thread resumes or fails it.
Resuming twice is an error.

## Examples

```scheme
;; Fan out, then collect: the group waits for every child.
(with-task-group
  (lambda (group)
    (for-each (lambda (url)
                (task-group-add! group (lambda () (fetch url))))
              urls)
    (task-group->list group)))          ; results in completion order

;; async let: start two things, use both.
(async-let ((left  (expensive-1))
            (right (expensive-2)))
  (combine (await left) (await right)))

;; A timeout: whichever finishes first wins, the other is cancelled.
(define (with-timeout milliseconds thunk)
  (with-task-group
    (lambda (group)
      (task-group-add! group thunk)
      (task-group-add! group
        (lambda () (task-sleep milliseconds) (error "Timed out")))
      (let ((first (task-group-next group)))
        (task-group-cancel-all! group)
        first))))

;; Cooperative cancellation.
(define worker
  (async
    (let loop ((i 0))
      (check-task-cancellation)         ; signals if cancelled
      (task-sleep 100)                  ; likewise
      (loop (+ i 1)))))
(cancel-task! worker)
(guard (e ((cancellation-error? e) 'stopped))
  (await worker))

;; An actor guarding mutable state.
(define account (make-actor 'account))
(define balance 0)
(define (deposit! amount)
  (actor-call account (lambda () (set! balance (+ balance amount)))))

;; A stream produced by one task, consumed by another.
(define ticks
  (make-async-stream
    (lambda (stream)
      (do ((i 0 (+ i 1))) ((= i 10))
        (task-sleep 50)
        (async-stream-yield! stream i)))))
(async-stream-for-each display ticks)
```

## Notes and limitations

* MIT/GNU Scheme threads are green threads scheduled within one OS
  thread, so this is concurrency, not parallelism. The scheduler
  preempts on a timer, so critical sections still need an actor or a
  mutex.
* There are no task priorities; the `priority` arguments of the Swift
  APIs have no counterpart. `Sendable` is a static property of Swift
  types and has no runtime counterpart.
* Times are in milliseconds, as for `sleep-current-thread`.
* `task-group-next` and `async-stream-next` use the EOF object as
  their end marker, so do not produce the EOF object as a value.
* Structured scopes wait for their children even when left through an
  escaping continuation; a child that ignores cancellation therefore
  keeps the scope from returning, as it would in Swift.

## For readers of SICP

An actor is the *serializer* of section 3.4.2 given a name:
`actor-call` is what `(make-serializer)` returns, applied to a thunk,
with the twist that the serializer is released at each `await`. An
async stream is the section 3.5 stream whose `cons-stream` is forced
by another process rather than by delay. A task is a procedure whose
evaluation has been handed to another agent, and `await` is how one
agent's value flows into another's environment. Cancellation is a
piece of state in the task's environment that the task consults, not
a signal that interrupts it.

## Trying it without rebuilding

An installed MIT/GNU Scheme 12.1 can load the module directly:

```scheme
(define task-env (extend-top-level-environment (->environment '(runtime))))
(eval '(define (add-boot-deps! . deps) unspecific) task-env)
(load "src/runtime/task.scm" task-env)
(load "tests/unit-testing.scm")
(run-unit-tests "tests/runtime/test-task.scm" task-env)
```

In a full build the package is part of the runtime and its names are
available everywhere.
