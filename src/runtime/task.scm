#| -*-Scheme-*-

Copyright (C) 1986, 1987, 1988, 1989, 1990, 1991, 1992, 1993, 1994,
    1995, 1996, 1997, 1998, 1999, 2000, 2001, 2002, 2003, 2004, 2005,
    2006, 2007, 2008, 2009, 2010, 2011, 2012, 2013, 2014, 2015, 2016,
    2017, 2018, 2019, 2020, 2021, 2022 Massachusetts Institute of
    Technology

This file is part of MIT/GNU Scheme.

MIT/GNU Scheme is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 2 of the License, or (at
your option) any later version.

MIT/GNU Scheme is distributed in the hope that it will be useful, but
WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
General Public License for more details.

You should have received a copy of the GNU General Public License
along with MIT/GNU Scheme; if not, write to the Free Software
Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301,
USA.

|#

;;;; Structured concurrency: tasks, task groups, actors, async streams
;;; package: (runtime task)

;;; This module brings the concurrency model of the Swift language to
;;; MIT/GNU Scheme, built on the runtime's threads:
;;;
;;;   Swift                                  Scheme
;;;   -----                                  ------
;;;   Task { ... }                           (async ...), spawn-task
;;;   Task.detached { ... }                  spawn-detached-task
;;;   try await task.value                   (await task)
;;;   task.cancel()                          (cancel-task! task)
;;;   Task.isCancelled                       (task-cancelled?)
;;;   try Task.checkCancellation()           (check-task-cancellation)
;;;   withTaskCancellationHandler            with-task-cancellation-handler
;;;   try await Task.sleep(...)              (task-sleep milliseconds)
;;;   await Task.yield()                     (task-yield)
;;;   async let x = ...                      (async-let ((x ...)) ...)
;;;   withThrowingTaskGroup { group in ... } (with-task-group (lambda (group) ...))
;;;   group.addTask { ... }                  (task-group-add! group thunk)
;;;   try await group.next()                 (task-group-next group)
;;;   try await group.waitForAll()           (task-group-wait-all group)
;;;   group.cancelAll()                      (task-group-cancel-all! group)
;;;   @TaskLocal static var x                (make-task-local default)
;;;   $x.withValue(v) { ... }                (with-task-local x v thunk)
;;;   actor A { ... }; await a.method()      make-actor, (actor-call a thunk)
;;;   AsyncStream { continuation in ... }    (make-async-stream producer)
;;;   continuation.yield(v) / .finish()      async-stream-yield!, async-stream-finish!
;;;   for await v in stream { ... }          async-stream-for-each
;;;   withCheckedThrowingContinuation        with-task-continuation
;;;
;;; A task is a thread plus a result cell, a cancellation flag, a list
;;; of task-local bindings, and a list of structured children.  Every
;;; thread has a task: threads that were not created by this module
;;; (the console thread, for example) are given a root task lazily.
;;;
;;; Structure.  Children created inside WITH-TASK-GROUP or ASYNC-LET
;;; cannot outlive the scope that created them: leaving the scope
;;; waits for them (cancelling them first if the scope is left by an
;;; error or by ASYNC-LET), and cancelling the parent task cancels
;;; them.  Tasks made by SPAWN-TASK are unstructured: they inherit the
;;; parent's task-local values but not its cancellation.
;;;
;;; Cancellation is cooperative, as in Swift.  Cancelling a task sets a
;;; flag, runs any handlers installed by WITH-TASK-CANCELLATION-HANDLER,
;;; and wakes the task if it is blocked in TASK-SLEEP or
;;; ASYNC-STREAM-NEXT; the task must notice by calling
;;; CHECK-TASK-CANCELLATION (which signals CONDITION-TYPE:TASK-CANCELLED)
;;; or TASK-CANCELLED?.
;;;
;;; Errors.  An error signalled inside a task is caught and stored; it
;;; is re-signalled in every task that AWAITs the failed task.  Objects
;;; raised with RAISE are delivered to the awaiting task's handlers
;;; unchanged.
;;;
;;; Actors.  An actor is a lock that serialises the thunks run through
;;; ACTOR-CALL, the way an actor's methods run one at a time in Swift.
;;; As in Swift, actor isolation is released at every suspension point
;;; (AWAIT, TASK-SLEEP, TASK-GROUP-NEXT, ACTOR-CALL on another actor,
;;; ...) and taken back afterwards, so an actor is re-entrant across an
;;; await and two actors calling each other cannot deadlock.  Check
;;; invariants after each suspension point rather than before it.

(declare (usual-integrations))

;;; The condition type below refers to CONDITION-TYPE:ERROR, which is
;;; itself initialised when (RUNTIME ERROR-HANDLER) boots.
(add-boot-deps! '(runtime error-handler))

;;;; Waiting

;;; Every blocking operation in this module goes through %WAIT.  A
;;; waiting thread registers itself with the object it waits on and
;;; suspends; whoever changes the object's state wakes the registered
;;; threads with %WAKE-THREADS!, which delivers an empty thread event,
;;; the same mechanism the runtime uses to wake threads blocked on a
;;; mutex.  Thread events are blocked around the whole wait so that a
;;; wake-up arriving between the state check and the suspension is
;;; queued rather than lost; SUSPEND-CURRENT-THREAD returns at once if
;;; an event is pending.

(define %unavailable (list 'unavailable))
(define %cancelled (list 'cancelled))

(define (%wake-threads! threads)
  (for-each (lambda (thread)
	      (signal-thread-event thread #f #t))
	    threads))

(define (%wait mutex try add-waiter! remove-waiter! task)
  ;; Block until TRY, called with MUTEX held, returns something other
  ;; than %UNAVAILABLE, and return that.  If TASK is given and is
  ;; cancelled while waiting, return %CANCELLED instead.
  (let ((self (current-thread)))
    (%with-actor-released
     (lambda ()
       (with-thread-events-blocked
	(lambda ()
	  (let loop ()
	    (let ((result
		   (with-thread-mutex-lock mutex
		     (lambda ()
		       (let ((result (try)))
			 (if (eq? result %unavailable)
			     (add-waiter! self))
			 result)))))
	      (cond ((not (eq? result %unavailable))
		     result)
		    ((and task (%task-cancelled? task))
		     (with-thread-mutex-lock mutex
		       (lambda ()
			 (remove-waiter! self)))
		     %cancelled)
		    (else
		     (suspend-current-thread)
		     (with-thread-mutex-lock mutex
		       (lambda ()
			 (remove-waiter! self)))
		     (loop)))))))))))

(define (%call-catching-errors thunk)
  ;; Returns (COMPLETED . value) or (FAILED . condition).
  (call-with-current-continuation
   (lambda (k)
     (bind-condition-handler (list condition-type:error)
	 (lambda (condition)
	   (k (cons 'failed condition)))
       (lambda ()
	 (cons 'completed (thunk)))))))

(define (%capture-error message . irritants)
  (cdr (%call-catching-errors
	(lambda ()
	  (apply error message irritants)))))

(define (%reraise object)
  (if (condition? object)
      (error object)
      (raise object)))

(define (%optional-name name)
  (if (default-object? name) #f name))

;;;; Tasks

(define-record-type <task>
    (%make-task name thread parent finish-hook mutex state result
		cancelled? cancel-handlers children locals waiters)
    task?
  (name task-name)
  (thread task-thread set-task-thread!)
  (parent task-parent)			;structured parent task, or #f
  (finish-hook task-finish-hook)	;called with the task when it finishes
  (mutex task-mutex)
  (state task-state set-task-state!)	;running, completed, or failed
  (result task-result set-task-result!)	;value, or condition when failed
  (cancelled? %task-cancelled? set-task-cancelled?!)
  (cancel-handlers task-cancel-handlers set-task-cancel-handlers!)
  (children task-children set-task-children!) ;structured children
  (locals task-locals set-task-locals!)	;alist of task-local bindings
  (waiters task-waiters set-task-waiters!)) ;threads blocked in AWAIT

(define-print-method task?
  (standard-print-method 'task
    (lambda (task)
      (let ((name (task-name task)))
	(if name
	    (list name (task-state task))
	    (list (task-state task)))))))

(define (%new-task name thread parent finish-hook locals)
  (%make-task name thread parent finish-hook (make-thread-mutex)
	      'running #f #f '() '() locals '()))

(define %task-key (list 'task))

(define (current-task)
  (let ((thread (current-thread)))
    (or (thread-get thread %task-key)
	(let ((task (%new-task #f thread #f #f '())))
	  (thread-put! thread %task-key task)
	  task))))

(define (task-done? task)
  (guarantee task? task 'task-done?)
  (%task-done? task))

(define (%task-done? task)
  (not (eq? 'running (task-state task))))

(define (spawn-task thunk #!optional name)
  (guarantee procedure? thunk 'spawn-task)
  (%spawn thunk (%optional-name name) (task-locals (current-task)) #f #f))

(define (spawn-detached-task thunk #!optional name)
  (guarantee procedure? thunk 'spawn-detached-task)
  (%spawn thunk (%optional-name name) '() #f #f))

(define-syntax async
  (syntax-rules ()
    ((_ body0 body1 ...)
     (spawn-task (lambda () body0 body1 ...)))))

(define (%spawn thunk name locals parent finish-hook)
  (let ((task (%new-task name #f parent finish-hook locals)))
    (if (and parent (not (%add-child! parent task)))
	;; A child of a cancelled parent starts out cancelled.
	(set-task-cancelled?! task #t))
    (let ((body
	   (lambda ()
	     (let ((thread (current-thread)))
	       (set-task-thread! task thread)
	       (thread-put! thread %task-key task))
	     (%run-task task thunk))))
      (set-task-thread! task
			(if name
			    (create-thread #f body name)
			    (create-thread #f body))))
    task))

(define (%run-task task thunk)
  (dynamic-wind
   (lambda () unspecific)
   (lambda ()
     (let ((outcome (%call-catching-errors thunk)))
       (%finish-task! task (car outcome) (cdr outcome))))
   (lambda ()
     ;; The thread left through EXIT-CURRENT-THREAD or an escaping
     ;; continuation; do not leave awaiting tasks hanging.
     (if (not (%task-done? task))
	 (%finish-task! task 'failed
			(%capture-error "Task exited abnormally:" task))))))

(define (%finish-task! task state result)
  (let ((waiters
	 (with-thread-mutex-lock (task-mutex task)
	   (lambda ()
	     (set-task-state! task state)
	     (set-task-result! task result)
	     (set-task-cancel-handlers! task '())
	     (let ((waiters (task-waiters task)))
	       (set-task-waiters! task '())
	       waiters)))))
    (let ((parent (task-parent task)))
      (if parent
	  (%remove-child! parent task)))
    (let ((hook (task-finish-hook task)))
      (if hook
	  (hook task)))
    (%wake-threads! waiters)))

(define (%add-child! parent child)
  ;; Returns #F, without adding CHILD, if PARENT is already cancelled.
  (with-thread-mutex-lock (task-mutex parent)
    (lambda ()
      (and (not (%task-cancelled? parent))
	   (begin
	     (set-task-children! parent (cons child (task-children parent)))
	     #t)))))

(define (%remove-child! parent child)
  (with-thread-mutex-lock (task-mutex parent)
    (lambda ()
      (set-task-children! parent (delq! child (task-children parent))))))

(define (await task)
  (guarantee task? task 'await)
  (if (eq? task (current-task))
      (error "A task cannot await itself:" task))
  (%wait (task-mutex task)
	 (lambda ()
	   (if (%task-done? task) #t %unavailable))
	 (lambda (thread)
	   (set-task-waiters! task (cons thread (task-waiters task))))
	 (lambda (thread)
	   (set-task-waiters! task (delq! thread (task-waiters task))))
	 #f)
  (%task-value task))

(define (%task-value task)
  (if (eq? 'failed (task-state task))
      (%reraise (task-result task))
      (task-result task)))

(define (task-yield)
  (%with-actor-released yield-current-thread))

;;;; Cancellation

(define (cancel-task! task)
  (guarantee task? task 'cancel-task!)
  (let ((work
	 (with-thread-mutex-lock (task-mutex task)
	   (lambda ()
	     (and (not (%task-cancelled? task))
		  (begin
		    (set-task-cancelled?! task #t)
		    (let ((handlers (task-cancel-handlers task))
			  (children (task-children task)))
		      (set-task-cancel-handlers! task '())
		      (cons handlers (list-copy children)))))))))
    (if work
	(begin
	  (%wake-task! task)
	  ;; Handlers run on the cancelling thread, innermost first,
	  ;; and cancellation propagates to structured children.
	  (for-each (lambda (handler) (handler)) (car work))
	  (for-each cancel-task! (cdr work))))
    unspecific))

(define (%wake-task! task)
  (let ((thread (task-thread task)))
    (if thread
	(signal-thread-event thread #f #t))))

(define (task-cancelled? #!optional task)
  (%task-cancelled?
   (if (default-object? task)
       (current-task)
       (begin
	 (guarantee task? task 'task-cancelled?)
	 task))))

(define (check-task-cancellation)
  (let ((task (current-task)))
    (if (%task-cancelled? task)
	(signal-task-cancelled task))))

(define (with-task-cancellation-handler on-cancel thunk)
  (guarantee procedure? on-cancel 'with-task-cancellation-handler)
  (guarantee procedure? thunk 'with-task-cancellation-handler)
  (let ((task (current-task))
	(handler (lambda () (on-cancel))))	;a fresh object per installation
    (if (with-thread-mutex-lock (task-mutex task)
	  (lambda ()
	    (or (%task-cancelled? task)
		(begin
		  (set-task-cancel-handlers!
		   task
		   (cons handler (task-cancel-handlers task)))
		  #f))))
	;; Already cancelled: the handler runs immediately, as in Swift.
	(begin
	  (handler)
	  (thunk))
	(dynamic-wind
	 (lambda () unspecific)
	 thunk
	 (lambda ()
	   (with-thread-mutex-lock (task-mutex task)
	     (lambda ()
	       (set-task-cancel-handlers!
		task
		(delq! handler (task-cancel-handlers task))))))))))

(define (task-sleep interval)
  ;; INTERVAL is in milliseconds.  Signals CONDITION-TYPE:TASK-CANCELLED
  ;; if the current task is, or becomes, cancelled.
  (guarantee real? interval 'task-sleep)
  (let ((task (current-task)))
    (if (or (%task-cancelled? task)
	    (%with-actor-released
	     (lambda ()
	       (with-thread-events-blocked
		(lambda ()
		  (let ((done? #f))
		    (let ((registration
			   (register-timer-event interval
			     (lambda ()
			       (set! done? #t)
			       unspecific))))
		      (let loop ()
			(cond (done? #f)
			      ((%task-cancelled? task)
			       (deregister-timer-event registration)
			       #t)
			      (else
			       (suspend-current-thread)
			       (loop)))))))))))
	(signal-task-cancelled task))
    unspecific))

(define-deferred condition-type:task-cancelled
  (make-condition-type 'task-cancelled condition-type:error '(task)
    (lambda (condition port)
      (write-string "The task " port)
      (write (access-condition condition 'task) port)
      (write-string " was cancelled." port))))

(define-deferred signal-task-cancelled
  (condition-signaller condition-type:task-cancelled
		       '(task)
		       standard-error-handler))

(define-deferred cancellation-error?
  (condition-predicate condition-type:task-cancelled))

(define-deferred cancellation-error/task
  (condition-accessor condition-type:task-cancelled 'task))

;;;; Task-local values

;;; A task-local value is bound for the dynamic extent of
;;; WITH-TASK-LOCAL in the current task, and every task spawned within
;;; that extent inherits a snapshot of the bindings in force.

(define-record-type <task-local>
    (%make-task-local default name)
    task-local?
  (default task-local-default)
  (name task-local-name))

(define-print-method task-local?
  (standard-print-method 'task-local
    (lambda (local)
      (let ((name (task-local-name local)))
	(if name (list name) '())))))

(define (make-task-local #!optional default name)
  (%make-task-local (if (default-object? default) #f default)
		    (%optional-name name)))

(define (task-local-ref local)
  (guarantee task-local? local 'task-local-ref)
  (let ((binding (assq local (task-locals (current-task)))))
    (if binding
	(cdr binding)
	(task-local-default local))))

(define (with-task-local local value thunk)
  (guarantee task-local? local 'with-task-local)
  (guarantee procedure? thunk 'with-task-local)
  (let ((task (current-task)))
    (let ((outer (task-locals task)))
      (let ((inner (cons (cons local value) outer)))
	(dynamic-wind
	 (lambda () (set-task-locals! task inner))
	 thunk
	 (lambda () (set-task-locals! task outer)))))))

;;;; Task groups

(define-record-type <task-group>
    (%make-task-group owner mutex open? cancelled? pending finished waiters)
    task-group?
  (owner task-group-owner)		;the task that created the group
  (mutex task-group-mutex)
  (open? task-group-open? set-task-group-open?!)
  (cancelled? task-group-cancelled? set-task-group-cancelled?!)
  (pending task-group-pending set-task-group-pending!) ;running children
  (finished task-group-finished)	;queue of finished, uncollected children
  (waiters task-group-waiters set-task-group-waiters!))

(define-print-method task-group?
  (standard-print-method 'task-group))

(define (with-task-group procedure)
  ;; Calls PROCEDURE with a fresh group.  Children added to the group
  ;; run concurrently; when PROCEDURE returns, the remaining children
  ;; are waited for and their results discarded -- a child's error is
  ;; seen only through TASK-GROUP-NEXT or TASK-GROUP-WAIT-ALL, as in
  ;; Swift.  If PROCEDURE signals an error, the children are
  ;; cancelled, waited for, and the error re-signalled.
  (guarantee procedure? procedure 'with-task-group)
  (%with-task-group procedure #f))

(define (%with-task-group procedure cancel-on-exit?)
  (let ((group
	 (%make-task-group (current-task) (make-thread-mutex) #t #f
			   '() (make-queue) '())))
    (dynamic-wind
     (lambda () unspecific)
     (lambda ()
       (let ((outcome
	      (%call-catching-errors (lambda () (procedure group)))))
	 (%close-task-group! group)
	 (if (eq? 'completed (car outcome))
	     (begin
	       (if cancel-on-exit?
		   (task-group-cancel-all! group))
	       (%task-group-discard-all group)
	       (cdr outcome))
	     (begin
	       (task-group-cancel-all! group)
	       (%task-group-discard-all group)
	       (%reraise (cdr outcome))))))
     (lambda ()
       ;; A non-local exit from the body must not leave children
       ;; running unsupervised: cancel them and wait for them.
       (if (task-group-open? group)
	   (begin
	     (%close-task-group! group)
	     (task-group-cancel-all! group)
	     (%task-group-discard-all group)))))))

(define (%close-task-group! group)
  (with-thread-mutex-lock (task-group-mutex group)
    (lambda ()
      (set-task-group-open?! group #f))))

(define (task-group-add! group thunk #!optional name)
  (guarantee task-group? group 'task-group-add!)
  (guarantee procedure? thunk 'task-group-add!)
  (%task-group-add group thunk (%optional-name name) #f))

(define (task-group-add-unless-cancelled! group thunk #!optional name)
  ;; Like TASK-GROUP-ADD!, but returns #F without adding a child if
  ;; the group or its owner has been cancelled.
  (guarantee task-group? group 'task-group-add-unless-cancelled!)
  (guarantee procedure? thunk 'task-group-add-unless-cancelled!)
  (%task-group-add group thunk (%optional-name name) #t))

(define (%task-group-add group thunk name unless-cancelled?)
  (let ((locals (task-locals (current-task)))
	(owner (task-group-owner group)))
    (let ((task+cancel?
	   (with-thread-mutex-lock (task-group-mutex group)
	     (lambda ()
	       (if (not (task-group-open? group))
		   (error "Task group no longer accepts children:" group))
	       (and (not (and unless-cancelled?
			      (or (task-group-cancelled? group)
				  (%task-cancelled? owner))))
		    (let ((task
			   (%spawn thunk name locals owner
				   (%task-group-finish-hook group))))
		      (set-task-group-pending!
		       group
		       (cons task (task-group-pending group)))
		      (cons task (task-group-cancelled? group))))))))
      (and task+cancel?
	   (let ((task (car task+cancel?)))
	     (if (cdr task+cancel?)
		 (cancel-task! task))
	     task)))))

(define (%task-group-finish-hook group)
  (lambda (task)
    (%wake-threads!
     (with-thread-mutex-lock (task-group-mutex group)
       (lambda ()
	 (set-task-group-pending! group (delq! task (task-group-pending group)))
	 (enqueue!/unsafe (task-group-finished group) task)
	 (let ((waiters (task-group-waiters group)))
	   (set-task-group-waiters! group '())
	   waiters))))))

(define (%task-group-next-finished group)
  ;; Returns the next child to finish, or the EOF object if none remain.
  (%wait (task-group-mutex group)
	 (lambda ()
	   (let ((finished (task-group-finished group)))
	     (cond ((not (queue-empty? finished)) (dequeue!/unsafe finished))
		   ((null? (task-group-pending group)) (eof-object))
		   (else %unavailable))))
	 (lambda (thread)
	   (set-task-group-waiters! group (cons thread (task-group-waiters group))))
	 (lambda (thread)
	   (set-task-group-waiters! group (delq! thread (task-group-waiters group))))
	 #f))

(define (task-group-next group)
  ;; Waits for the next child to finish and returns its value, or
  ;; re-signals its error.  Returns the EOF object when no children
  ;; remain.
  (guarantee task-group? group 'task-group-next)
  (let ((task (%task-group-next-finished group)))
    (if (eof-object? task)
	task
	(%task-value task))))

(define (task-group-wait-all group)
  ;; Waits for every remaining child.  If one of them fails, the others
  ;; are cancelled, still waited for, and then the first failure is
  ;; re-signalled.
  (guarantee task-group? group 'task-group-wait-all)
  (let loop ((failure #f))
    (let ((task (%task-group-next-finished group)))
      (cond ((not (eof-object? task))
	     (if (and (not failure)
		      (eq? 'failed (task-state task)))
		 (begin
		   (task-group-cancel-all! group)
		   (loop (task-result task)))
		 (loop failure)))
	    (failure (%reraise failure))
	    (else unspecific)))))

(define (%task-group-discard-all group)
  (let loop ()
    (if (not (eof-object? (%task-group-next-finished group)))
	(loop))))

(define (task-group-for-each procedure group)
  (guarantee procedure? procedure 'task-group-for-each)
  (guarantee task-group? group 'task-group-for-each)
  (let loop ()
    (let ((value (task-group-next group)))
      (if (not (eof-object? value))
	  (begin
	    (procedure value)
	    (loop))))))

(define (task-group->list group)
  ;; The children's values, in the order they finished.
  (guarantee task-group? group 'task-group->list)
  (let loop ((results '()))
    (let ((value (task-group-next group)))
      (if (eof-object? value)
	  (reverse! results)
	  (loop (cons value results))))))

(define (task-group-empty? group)
  (guarantee task-group? group 'task-group-empty?)
  (with-thread-mutex-lock (task-group-mutex group)
    (lambda ()
      (and (null? (task-group-pending group))
	   (queue-empty? (task-group-finished group))))))

(define (task-group-cancel-all! group)
  (guarantee task-group? group 'task-group-cancel-all!)
  (for-each cancel-task!
	    (with-thread-mutex-lock (task-group-mutex group)
	      (lambda ()
		(set-task-group-cancelled?! group #t)
		(list-copy (task-group-pending group))))))

;;; (async-let ((name expr) ...) body ...) starts each EXPR as a child
;;; task and binds each NAME to that task, so the body can (await name)
;;; wherever it likes.  When the body is left, children that were not
;;; awaited are cancelled and waited for, as Swift's async let does.

(define-syntax async-let
  (syntax-rules ()
    ((_ ((name expr) ...) body0 body1 ...)
     (%async-let (list (lambda () expr) ...)
		 (lambda (name ...) body0 body1 ...)))))

(define (%async-let thunks receiver)
  (%with-task-group
   (lambda (group)
     (apply receiver
	    (map (lambda (thunk) (task-group-add! group thunk)) thunks)))
   #t))

;;;; Actors

(define-record-type <actor>
    (%make-actor name mutex)
    actor?
  (name actor-name)
  (mutex actor-mutex))

(define-print-method actor?
  (standard-print-method 'actor
    (lambda (actor)
      (let ((name (actor-name actor)))
	(if name (list name) '())))))

(define (make-actor #!optional name)
  (%make-actor (%optional-name name) (make-thread-mutex)))

(define %actor-key (list 'actor))

(define (current-actor)
  (thread-get (current-thread) %actor-key))

(define (actor-isolated? actor)
  (guarantee actor? actor 'actor-isolated?)
  (eq? actor (current-actor)))

(define (actor-call actor thunk)
  ;; Runs THUNK isolated to ACTOR -- no other thunk runs on ACTOR at
  ;; the same time -- and returns its value.  From code already
  ;; isolated to ACTOR the call is synchronous, as in Swift.
  (guarantee actor? actor 'actor-call)
  (guarantee procedure? thunk 'actor-call)
  (if (eq? actor (current-actor))
      (thunk)
      (%with-actor-released
       (lambda ()
	 (with-thread-mutex-lock (actor-mutex actor)
	   (lambda ()
	     (%with-current-actor actor thunk)))))))

(define (actor-async actor thunk #!optional name)
  ;; Like ACTOR-CALL, but in a new task; returns the task.
  (guarantee actor? actor 'actor-async)
  (guarantee procedure? thunk 'actor-async)
  (spawn-task (lambda () (actor-call actor thunk)) name))

(define (%with-current-actor actor thunk)
  (let ((thread (current-thread)))
    (let ((outer (thread-get thread %actor-key)))
      (dynamic-wind
       (lambda () (thread-put! thread %actor-key actor))
       thunk
       (lambda () (thread-put! thread %actor-key outer))))))

(define (%with-actor-released thunk)
  ;; A suspension point: give up the current actor's isolation while
  ;; THUNK runs and take it back afterwards.  A thread holds at most
  ;; one actor's lock at a time, that of (CURRENT-ACTOR).
  (let ((actor (current-actor)))
    (if actor
	(without-thread-mutex-lock (actor-mutex actor)
	  (lambda ()
	    (%with-current-actor #f thunk)))
	(thunk))))

;;;; Async streams

;;; An async stream is a queue of values produced by one task and
;;; consumed by another, with an end.  ASYNC-STREAM-NEXT blocks until a
;;; value is available; it returns the EOF object once the stream has
;;; finished and been drained, or when the consuming task is
;;; cancelled.  A stream finished with a failure re-signals it to the
;;; consumer after the remaining values have been delivered.

(define-record-type <async-stream>
    (%make-async-stream name mutex items state failure waiters)
    async-stream?
  (name async-stream-name)
  (mutex async-stream-mutex)
  (items async-stream-items)		;queue of undelivered values
  (state async-stream-state set-async-stream-state!) ;open or finished
  (failure async-stream-failure set-async-stream-failure!)
  (waiters async-stream-waiters set-async-stream-waiters!))

(define-print-method async-stream?
  (standard-print-method 'async-stream
    (lambda (stream)
      (let ((name (async-stream-name stream)))
	(if name (list name) '())))))

(define (make-async-stream #!optional producer name)
  ;; If PRODUCER is given, it is run in a new task with the stream as
  ;; its argument, and the stream finishes when it returns (with the
  ;; producer's error, if it signals one).
  (let ((stream
	 (%make-async-stream (%optional-name name) (make-thread-mutex)
			     (make-queue) 'open #f '())))
    (if (and (not (default-object? producer)) producer)
	(begin
	  (guarantee procedure? producer 'make-async-stream)
	  (spawn-task
	   (lambda ()
	     (let ((outcome
		    (%call-catching-errors (lambda () (producer stream)))))
	       (if (eq? 'failed (car outcome))
		   (async-stream-finish! stream (cdr outcome))
		   (async-stream-finish! stream))))
	   name)))
    stream))

(define (async-stream-finished? stream)
  (guarantee async-stream? stream 'async-stream-finished?)
  (eq? 'finished (async-stream-state stream)))

(define (%async-stream-update! stream update)
  ;; Runs UPDATE with the stream locked; if it returns true, wakes the
  ;; waiting consumers and returns #T.
  (let ((waiters
	 (with-thread-mutex-lock (async-stream-mutex stream)
	   (lambda ()
	     (and (update)
		  (let ((waiters (async-stream-waiters stream)))
		    (set-async-stream-waiters! stream '())
		    (cons 'updated waiters)))))))
    (if waiters
	(begin
	  (%wake-threads! (cdr waiters))
	  #t)
	#f)))

(define (async-stream-yield! stream value)
  ;; Returns #T if VALUE was accepted, #F if the stream had finished.
  (guarantee async-stream? stream 'async-stream-yield!)
  (%async-stream-update! stream
    (lambda ()
      (and (eq? 'open (async-stream-state stream))
	   (begin
	     (enqueue!/unsafe (async-stream-items stream) value)
	     #t)))))

(define (async-stream-finish! stream #!optional failure)
  ;; Returns #T if this call finished the stream, #F if it already was.
  (guarantee async-stream? stream 'async-stream-finish!)
  (let ((failure (if (default-object? failure) #f failure)))
    (%async-stream-update! stream
      (lambda ()
	(and (eq? 'open (async-stream-state stream))
	     (begin
	       (set-async-stream-state! stream 'finished)
	       (set-async-stream-failure! stream failure)
	       #t))))))

(define (async-stream-next stream)
  (guarantee async-stream? stream 'async-stream-next)
  (let ((result
	 (%wait (async-stream-mutex stream)
		(lambda ()
		  (let ((items (async-stream-items stream)))
		    (cond ((not (queue-empty? items))
			   (list 'value (dequeue!/unsafe items)))
			  ((eq? 'finished (async-stream-state stream))
			   (let ((failure (async-stream-failure stream)))
			     (set-async-stream-failure! stream #f)
			     (if failure
				 (list 'failed failure)
				 (list 'done))))
			  (else %unavailable))))
		(lambda (thread)
		  (set-async-stream-waiters!
		   stream
		   (cons thread (async-stream-waiters stream))))
		(lambda (thread)
		  (set-async-stream-waiters!
		   stream
		   (delq! thread (async-stream-waiters stream))))
		(current-task))))
    (cond ((eq? result %cancelled) (eof-object))
	  ((eq? 'value (car result)) (cadr result))
	  ((eq? 'failed (car result)) (%reraise (cadr result)))
	  (else (eof-object)))))

(define (async-stream-for-each procedure stream)
  (guarantee procedure? procedure 'async-stream-for-each)
  (guarantee async-stream? stream 'async-stream-for-each)
  (let loop ()
    (let ((value (async-stream-next stream)))
      (if (not (eof-object? value))
	  (begin
	    (procedure value)
	    (loop))))))

(define (async-stream->list stream)
  (guarantee async-stream? stream 'async-stream->list)
  (let loop ((results '()))
    (let ((value (async-stream-next stream)))
      (if (eof-object? value)
	  (reverse! results)
	  (loop (cons value results))))))

;;;; Task continuations

;;; WITH-TASK-CONTINUATION bridges callback-style code into a task, as
;;; Swift's withCheckedThrowingContinuation does: RECEIVER is called at
;;; once with a one-shot continuation object, and the current task
;;; then blocks until some thread resumes it with a value or fails it.

(define-record-type <task-continuation>
    (%make-task-continuation mutex state value waiters)
    task-continuation?
  (mutex task-continuation-mutex)
  (state task-continuation-state set-task-continuation-state!) ;pending, completed, or failed
  (value task-continuation-value set-task-continuation-value!)
  (waiters task-continuation-waiters set-task-continuation-waiters!))

(define-print-method task-continuation?
  (standard-print-method 'task-continuation
    (lambda (continuation)
      (list (task-continuation-state continuation)))))

(define (with-task-continuation receiver)
  (guarantee procedure? receiver 'with-task-continuation)
  (let ((continuation
	 (%make-task-continuation (make-thread-mutex) 'pending #f '())))
    (receiver continuation)
    (%wait (task-continuation-mutex continuation)
	   (lambda ()
	     (if (eq? 'pending (task-continuation-state continuation))
		 %unavailable
		 #t))
	   (lambda (thread)
	     (set-task-continuation-waiters!
	      continuation
	      (cons thread (task-continuation-waiters continuation))))
	   (lambda (thread)
	     (set-task-continuation-waiters!
	      continuation
	      (delq! thread (task-continuation-waiters continuation))))
	   #f)
    (if (eq? 'failed (task-continuation-state continuation))
	(%reraise (task-continuation-value continuation))
	(task-continuation-value continuation))))

(define (task-continuation-resume! continuation #!optional value)
  (%resume-task-continuation! continuation 'completed
			      (if (default-object? value) unspecific value)
			      'task-continuation-resume!))

(define (task-continuation-fail! continuation failure)
  ;; FAILURE is a condition, or any object to deliver with RAISE.
  (%resume-task-continuation! continuation 'failed failure
			      'task-continuation-fail!))

(define (%resume-task-continuation! continuation state value caller)
  (guarantee task-continuation? continuation caller)
  (%wake-threads!
   (with-thread-mutex-lock (task-continuation-mutex continuation)
     (lambda ()
       (if (not (eq? 'pending (task-continuation-state continuation)))
	   (error "Task continuation already resumed:" continuation))
       (set-task-continuation-state! continuation state)
       (set-task-continuation-value! continuation value)
       (let ((waiters (task-continuation-waiters continuation)))
	 (set-task-continuation-waiters! continuation '())
	 waiters)))))
