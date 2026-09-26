;;; A small program to try the IDE with.
;;;
;;; Press Run (or F5) to load this file into the console, then call the
;;; procedures interactively: (fact 20), (fib 30), (primes-below 100).

(define (fact n)
  (if (= n 0)
      1
      (* n (fact (- n 1)))))

(define (fib n)
  (let loop ((a 0) (b 1) (i n))
    (if (= i 0)
        a
        (loop b (+ a b) (- i 1)))))

(define (primes-below limit)
  (let ((sieve (make-vector limit #t)))
    (let loop ((i 2) (acc '()))
      (cond ((>= i limit) (reverse acc))
            ((vector-ref sieve i)
             (do ((j (* i i) (+ j i)))
                 ((>= j limit))
               (vector-set! sieve j #f))
             (loop (+ i 1) (cons i acc)))
            (else (loop (+ i 1) acc))))))

(display "10! = ")
(display (fact 10))
(newline)
(display "The primes below 50: ")
(write (primes-below 50))
(newline)
