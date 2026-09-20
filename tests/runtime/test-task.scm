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

;;;; Tests of structured concurrency (tasks, groups, actors, streams)

(declare (usual-integrations))

(define (spin-until-cancelled value)
  ;; Busy-waits, yielding, until the current task is cancelled.
  (let loop ()
    (if (task-cancelled?)
	value
	(begin
	  (task-yield)
	  (loop)))))

(define (catch-error thunk)
  ;; Returns the condition signalled by THUNK, or #F if none.
  (call-with-current-continuation
   (lambda (k)
     (bind-condition-handler (list condition-type:error) k
       (lambda ()
	 (thunk)
	 #f)))))

(define (elapsed-time thunk)
  ;; Returns the value of THUNK and the milliseconds it took.
  (let ((start (real-time-clock)))
    (let ((value (thunk)))
      (values value (- (real-time-clock) start)))))

;;;; Tasks

(define-test 'spawn-and-await
  (lambda ()
    (assert-equal (await (async (+ 1 2))) 3)
    (assert-equal (await (spawn-task (lambda () 'spawned))) 'spawned)
    (assert-equal (await (spawn-detached-task (lambda () 'detached)))
		  'detached)
    (let ((task (async 'done)))
      (await task)
      (assert-true (task-done? task))
      (assert-eq (task-state task) 'completed)
      ;; Awaiting a finished task again returns the same value.
      (assert-eq (await task) 'done))))

(define-test 'await-many-in-order
  (lambda ()
    (let ((tasks
	   (map (lambda (i)
		  (spawn-task (lambda ()
				(task-sleep (modulo (* 7 i) 5))
				(* i i))
			      i))
		(iota 20))))
      (assert-equal (map await tasks) (map (lambda (i) (* i i)) (iota 20)))
      (assert-equal (map task-name tasks) (iota 20)))))

(define-test 'current-task
  (lambda ()
    (assert-true (task? (current-task)))
    (assert-eq (current-task) (current-task))
    (assert-false (task-done? (current-task)))
    (assert-error (lambda () (await (current-task))))
    (let ((task (async (current-task))))
      (assert-eq (await task) task)
      (assert-!eq task (current-task)))))

(define-test 'error-propagation
  (lambda ()
    (let ((task (async (error "boom:" 42))))
      (let ((condition (catch-error (lambda () (await task)))))
	(assert-true (condition? condition))
	(assert-equal (condition/report-string condition) "boom: 42"))
      (assert-eq (task-state task) 'failed)
      ;; Every await re-signals it.
      (assert-error (lambda () (await task))))
    ;; Non-condition objects raised with RAISE arrive unchanged.
    (assert-eq (guard (e (#t e))
		 (await (async (raise 'oops))))
	       'oops)))

;;;; Cancellation

(define-test 'cancellation-flag
  (lambda ()
    (let ((task (async (spin-until-cancelled 'noticed))))
      (assert-false (task-cancelled? task))
      (cancel-task! task)
      (assert-true (task-cancelled? task))
      (assert-eq (await task) 'noticed)
      ;; Cancelling again is harmless.
      (cancel-task! task))
    (assert-false (task-cancelled?))))

(define-test 'check-task-cancellation
  (lambda ()
    (let ((task
	   (async
	    (let loop ()
	      (check-task-cancellation)
	      (task-yield)
	      (loop)))))
      (cancel-task! task)
      (let ((condition (catch-error (lambda () (await task)))))
	(assert-true (cancellation-error? condition))
	(assert-eq (cancellation-error/task condition) task))
      (assert-error (lambda () (await task))
		    (list condition-type:task-cancelled)))))

(define-test 'cancel-sleeping-task
  (lambda ()
    (let ((task (async (task-sleep 60000) 'overslept)))
      (task-sleep 20)
      (cancel-task! task)
      (receive (condition elapsed)
	  (elapsed-time (lambda () (catch-error (lambda () (await task)))))
	(assert-true (cancellation-error? condition))
	(assert-true (< elapsed 30000))))
    ;; Sleeping in an already-cancelled task signals at once.
    (let ((task (async (spin-until-cancelled #t) (task-sleep 60000))))
      (cancel-task! task)
      (assert-error (lambda () (await task))
		    (list condition-type:task-cancelled)))))

(define-test 'cancellation-handler
  (lambda ()
    (let ((log '()))
      (let ((task
	     (async
	      (with-task-cancellation-handler
	       (lambda () (set! log (cons 'outer log)))
	       (lambda ()
		 (with-task-cancellation-handler
		  (lambda () (set! log (cons 'inner log)))
		  (lambda () (spin-until-cancelled 'finished))))))))
	(task-sleep 20)
	(cancel-task! task)
	;; Handlers run before CANCEL-TASK! returns, innermost first.
	(assert-equal log '(outer inner))
	(assert-eq (await task) 'finished)))
    ;; A handler installed after cancellation runs immediately.
    (let ((gate (make-async-stream))
	  (ran? #f))
      (let ((task
	     (async
	      (async-stream-next gate)
	      (with-task-cancellation-handler
	       (lambda () (set! ran? #t))
	       (lambda () ran?)))))
	(task-sleep 20)
	(cancel-task! task)
	(assert-true (await task))))
    ;; A handler that has gone out of scope does not run.
    (let ((ran? #f))
      (let ((task
	     (async
	      (with-task-cancellation-handler (lambda () (set! ran? #t))
					      (lambda () 'ignored))
	      (spin-until-cancelled 'finished))))
	(task-sleep 20)
	(cancel-task! task)
	(assert-eq (await task) 'finished)
	(assert-false ran?)))))

;;;; Task-local values

(define-test 'task-local-values
  (lambda ()
    (let ((local (make-task-local 'default 'local)))
      (assert-eq (task-local-ref local) 'default)
      (assert-equal
       (with-task-local local 'bound
	 (lambda ()
	   (list (task-local-ref local)
		 ;; Inherited by unstructured children ...
		 (await (async (task-local-ref local)))
		 ;; ... and by structured ones ...
		 (async-let ((x (task-local-ref local))) (await x))
		 ;; ... but not by detached tasks.
		 (await (spawn-detached-task
			 (lambda () (task-local-ref local))))
		 ;; Inner bindings shadow outer ones.
		 (with-task-local local 'inner
		   (lambda () (await (async (task-local-ref local))))))))
       '(bound bound bound default inner))
      (assert-eq (task-local-ref local) 'default))))

;;;; Task groups

(define-test 'task-group-basic
  (lambda ()
    (assert-equal
     (sort (with-task-group
	    (lambda (group)
	      (for-each (lambda (i)
			  (task-group-add! group (lambda () (* i i))))
			(iota 5))
	      (task-group->list group)))
	   <)
     '(0 1 4 9 16))
    (with-task-group
     (lambda (group)
       (assert-true (task-group-empty? group))
       (assert-true (eof-object? (task-group-next group)))
       (task-group-add! group (lambda () 'only))
       (assert-false (task-group-empty? group))
       (assert-eq (task-group-next group) 'only)
       (assert-true (eof-object? (task-group-next group)))
       (let ((sum 0))
	 (for-each (lambda (i) (task-group-add! group (lambda () i)))
		   (iota 10))
	 (task-group-for-each (lambda (i) (set! sum (+ sum i))) group)
	 (assert-equal sum 45))))))

(define-test 'task-group-waits-for-children
  (lambda ()
    (let ((cells (make-vector 5 #f)))
      (with-task-group
       (lambda (group)
	 (for-each (lambda (i)
		     (task-group-add! group
				      (lambda ()
					(task-sleep (* 10 i))
					(vector-set! cells i #t))))
		   (iota 5))))
      (assert-equal (vector->list cells) '(#t #t #t #t #t)))))

(define-test 'task-group-closed-after-scope
  (lambda ()
    (let ((escaped
	   (with-task-group (lambda (group) group))))
      (assert-error
       (lambda () (task-group-add! escaped (lambda () 'late)))))))

(define-test 'task-group-body-error-cancels-children
  (lambda ()
    (let ((child #f))
      (let ((condition
	     (catch-error
	      (lambda ()
		(with-task-group
		 (lambda (group)
		   (set! child
			 (task-group-add! group
					  (lambda () (task-sleep 60000))))
		   (error "body failed")))))))
	(assert-equal (condition/report-string condition) "body failed")
	(assert-true (task-cancelled? child))
	(assert-true (task-done? child))))))

(define-test 'task-group-child-error-cancels-siblings
  (lambda ()
    (let ((sibling #f))
      (receive (condition elapsed)
	  (elapsed-time
	   (lambda ()
	     (catch-error
	      (lambda ()
		(with-task-group
		 (lambda (group)
		   (set! sibling
			 (task-group-add! group
					  (lambda () (task-sleep 60000))))
		   (task-group-add! group (lambda () (error "child failed")))
		   (task-group-wait-all group)))))))
	(assert-equal (condition/report-string condition) "child failed")
	(assert-true (task-cancelled? sibling))
	(assert-true (task-done? sibling))
	(assert-true (< elapsed 30000))))
    ;; The same through an explicit NEXT.
    (assert-error
     (lambda ()
       (with-task-group
	(lambda (group)
	  (task-group-add! group (lambda () (error "child failed")))
	  (task-group-next group)))))))

(define-test 'task-group-implicit-wait-discards-errors
  (lambda ()
    ;; A body that returns without collecting still waits for its
    ;; children, but their errors are dropped, as in Swift.
    (let ((sibling #f)
	  (finished? #f))
      (assert-eq (with-task-group
		  (lambda (group)
		    (set! sibling
			  (task-group-add! group
					   (lambda ()
					     (task-sleep 30)
					     (set! finished? #t))))
		    (task-group-add! group (lambda () (error "ignored")))
		    'body-value))
		 'body-value)
      (assert-true finished?)
      (assert-false (task-cancelled? sibling))
      (assert-true (task-done? sibling)))))

(define-test 'task-group-cancel-all
  (lambda ()
    (with-task-group
     (lambda (group)
       (for-each (lambda (i)
		   (task-group-add! group
				    (lambda () (spin-until-cancelled i))))
		 (iota 4))
       (assert-false (task-group-cancelled? group))
       (task-group-cancel-all! group)
       (assert-true (task-group-cancelled? group))
       (assert-equal (sort (task-group->list group) <) '(0 1 2 3))
       ;; Children added afterwards start out cancelled ...
       (assert-eq (task-group-next
		   (begin
		     (task-group-add! group (lambda () (task-cancelled?)))
		     group))
		  #t)
       ;; ... unless the caller asks not to add them.
       (assert-false
	(task-group-add-unless-cancelled! group (lambda () 'never)))))))

(define-test 'parent-cancellation-propagates
  (lambda ()
    (let ((outer
	   (async
	    (with-task-group
	     (lambda (group)
	       (task-group-add! group
				(lambda () (spin-until-cancelled 'child)))
	       (task-group-next group))))))
      (task-sleep 20)
      (cancel-task! outer)
      (assert-eq (await outer) 'child))
    ;; Unstructured children are not cancelled with their parent.
    (let ((inner #f))
      (let ((outer
	     (async
	      (set! inner (async (task-sleep 20) 'independent))
	      (spin-until-cancelled 'outer))))
	(task-sleep 10)
	(cancel-task! outer)
	(assert-eq (await outer) 'outer)
	(assert-false (task-cancelled? inner))
	(assert-eq (await inner) 'independent)))))

(define-test 'async-let
  (lambda ()
    (assert-equal (async-let ((a (+ 1 1))
			      (b (* 2 3)))
		    (+ (await a) (await b)))
		  8)
    ;; Children not awaited by the body are cancelled at scope exit.
    (let ((escaped #f))
      (assert-eq (async-let ((slow (spin-until-cancelled 'cancelled)))
		   (set! escaped slow)
		   'body)
		 'body)
      (assert-true (task-cancelled? escaped))
      (assert-true (task-done? escaped)))
    ;; Errors in an awaited child propagate.
    (assert-error
     (lambda ()
       (async-let ((bad (error "bad child")))
	 (await bad))))))

;;;; Actors

(define-test 'actor-serialises-access
  (lambda ()
    (let ((counter (make-actor 'counter))
	  (count 0)
	  (isolated? #t)
	  (seen-actor #f))
      (let ((increment!
	     (lambda ()
	       (actor-call counter
			   (lambda ()
			     (set! isolated?
				   (and isolated? (actor-isolated? counter)))
			     (set! seen-actor (current-actor))
			     (let ((n count))
			       ;; Block the thread, inviting the others
			       ;; to run, without a task suspension
			       ;; point (which would release the actor).
			       (sleep-current-thread 1)
			       (set! count (+ n 1))))))))
	(assert-false (actor-isolated? counter))
	(assert-false (current-actor))
	(with-task-group
	 (lambda (group)
	   (for-each (lambda (i)
		       (task-group-add! group
					(lambda ()
					  (do ((j 0 (+ j 1)))
					      ((= j 50))
					    (increment!)))))
		     (iota 10))))
	(assert-equal count 500)
	(assert-true isolated?)
	(assert-eq seen-actor counter)
	(assert-false (current-actor))))))

(define-test 'actor-reentrancy
  (lambda ()
    (let ((a (make-actor 'a))
	  (b (make-actor 'b)))
      ;; A synchronous call from inside the actor.
      (assert-eq (actor-call a (lambda () (actor-call a (lambda () 'inner))))
		 'inner)
      ;; Isolation is released while awaiting, so another task may
      ;; enter the actor in the meantime.
      (assert-eq (actor-call a
			     (lambda ()
			       (await (actor-async a (lambda () 'nested)))))
		 'nested)
      (assert-false (current-actor))
      ;; Two actors calling each other do not deadlock.
      (assert-eq (actor-call a
			     (lambda ()
			       (actor-call b
					   (lambda ()
					     (assert-eq (current-actor) b)
					     (actor-call a (lambda () 'ok))))))
		 'ok)
      (assert-false (current-actor))
      ;; An error inside the actor releases it.
      (assert-error (lambda () (actor-call a (lambda () (error "inside")))))
      (assert-eq (await (actor-async a (lambda () (current-actor)))) a))))

;;;; Async streams

(define-test 'async-stream
  (lambda ()
    (let ((stream
	   (make-async-stream
	    (lambda (stream)
	      (for-each (lambda (i)
			  (task-sleep 1)
			  (async-stream-yield! stream i))
			(iota 5))))))
      (assert-equal (async-stream->list stream) '(0 1 2 3 4))
      (assert-true (async-stream-finished? stream))
      (assert-false (async-stream-yield! stream 'late))
      (assert-true (eof-object? (async-stream-next stream))))
    ;; Hand-driven stream, consumed from a task.
    (let ((stream (make-async-stream))
	  (seen '()))
      (let ((consumer
	     (async (async-stream-for-each
		     (lambda (v) (set! seen (cons v seen)))
		     stream)
		    'consumed)))
	(assert-true (async-stream-yield! stream 'a))
	(assert-true (async-stream-yield! stream 'b))
	(task-sleep 20)
	(assert-true (async-stream-yield! stream 'c))
	(assert-true (async-stream-finish! stream))
	(assert-false (async-stream-finish! stream))
	(assert-eq (await consumer) 'consumed)
	(assert-equal (reverse seen) '(a b c))))))

(define-test 'async-stream-failure
  (lambda ()
    (let ((stream
	   (make-async-stream
	    (lambda (stream)
	      (async-stream-yield! stream 1)
	      (async-stream-yield! stream 2)
	      (error "producer failed")))))
      (assert-equal (async-stream-next stream) 1)
      (assert-equal (async-stream-next stream) 2)
      (let ((condition (catch-error (lambda () (async-stream-next stream)))))
	(assert-equal (condition/report-string condition) "producer failed"))
      ;; The failure is delivered once.
      (assert-true (eof-object? (async-stream-next stream))))))

(define-test 'async-stream-consumer-cancelled
  (lambda ()
    (let ((stream (make-async-stream)))
      (let ((consumer
	     (async (let ((v (async-stream-next stream)))
		      (if (eof-object? v) 'ended v)))))
	(task-sleep 20)
	(cancel-task! consumer)
	(assert-eq (await consumer) 'ended)))))

;;;; Task continuations

(define-test 'task-continuation
  (lambda ()
    (assert-equal
     (with-task-continuation
      (lambda (k)
	(async (task-sleep 10) (task-continuation-resume! k 42))))
     42)
    (let ((condition
	   (catch-error
	    (lambda ()
	      (with-task-continuation
	       (lambda (k)
		 (async (task-continuation-fail!
			 k
			 (catch-error (lambda () (error "resumed with error")))))))))))
      (assert-equal (condition/report-string condition) "resumed with error"))
    (assert-eq (guard (e (#t e))
		 (with-task-continuation
		  (lambda (k) (task-continuation-fail! k 'plain-object))))
	       'plain-object)
    ;; Resuming synchronously, and only once.
    (assert-equal
     (with-task-continuation
      (lambda (k)
	(task-continuation-resume! k 'first)
	(assert-error (lambda () (task-continuation-resume! k 'second)))))
     'first)))
