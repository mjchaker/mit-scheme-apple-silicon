/* -*-C-*-

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

*/

/* Compiled code interface for AArch64.  */

#include "cmpint.h"
#include "prims.h"

#ifdef __APPLE__
#  include <libkern/OSCacheControl.h>
#endif

extern void * tospace_to_newspace (void *);
extern void * newspace_to_tospace (void *);

#define TYPE_ARITY_MASK	(UINT32_C (0x0000ffff))
#define TYPE_ARITY_SHIFT 0
#define BLOCK_OFFSET_MASK (UINT32_C (0xffff0000))
#define BLOCK_OFFSET_SHIFT 16

insn_t *
aarch64_entry_pc (insn_t * entry)
{
  insn_t * pc
    = ((insn_t *) (((char *) entry) + (((const int64_t *) entry)[-1])));
  if ((cc_exec_delta != 0) && (!CC_EXEC_SHADOW_P (pc)))
    pc = ((insn_t *) (((char *) pc) + cc_exec_delta));
  return (pc);
}

bool
read_cc_entry_type (cc_entry_type_t * cet, insn_t * address)
{
  uint32_t word = (address[-3]);
  uint16_t type_arity = ((word & TYPE_ARITY_MASK) >> TYPE_ARITY_SHIFT);
  return (decode_old_style_format_word (cet, type_arity));
}

bool
write_cc_entry_type (cc_entry_type_t * cet, insn_t * address)
{
  uint16_t type_arity;
  bool error = (encode_old_style_format_word (cet, (&type_arity)));
  if (error)
    return (error);
  (address[-3]) &=~ TYPE_ARITY_MASK;
  (address[-3]) |= (type_arity << TYPE_ARITY_SHIFT);
  return (false);
}

bool
read_cc_entry_offset (cc_entry_offset_t * ceo, insn_t * address)
{
  const size_t units = ((sizeof (SCHEME_OBJECT)) / (sizeof (insn_t)));
  assert (units == 2);
  uint32_t word = (address[-3]);
  uint16_t n = ((word & BLOCK_OFFSET_MASK) >> BLOCK_OFFSET_SHIFT);
  /* Block offsets are stored in units of Scheme objects.  */
  (ceo->offset) = (units * (n >> 1));
  (ceo->continued_p) = ((n & 1) != 0);
  return (false);
}

bool
write_cc_entry_offset (cc_entry_offset_t * ceo, insn_t * address)
{
  const size_t units = ((sizeof (SCHEME_OBJECT)) / (sizeof (insn_t)));
  assert (units == 2);
  assert (((ceo->offset) % units) == 0);
  if (! ((ceo->offset) < 0x4000))
    return (true);
  (address[-3]) &=~ BLOCK_OFFSET_MASK;
  (address[-3]) |=
    (((((ceo->offset) / units) << 1) | ((ceo->continued_p) ? 1 : 0))
     << BLOCK_OFFSET_SHIFT);
  return (false);
}

static long
sign_extend(long word, unsigned bits)
{
  const long magic = (1L << (bits - 1));
  return ((word ^ magic) - magic);
}

insn_t *
cc_return_address_to_entry_address (insn_t * pc)
{
  insn_t insn = (pc[0]);
  if ((insn & 0xfc000000UL) == 0x14000000UL) /* B */
    return (pc + (sign_extend ((insn & 0x03ffffff), 26)));
  else
    /* XXX What if it got branch-tensioned?  */
    return (pc);
}

/* Compiled closures */

/* start_closure_reloation (scan, ref)

   `scan' points at the manifest of a compiled closure.  Initialize
   `ref' with whatever we need to relocate the entries in it.  */

void
start_closure_relocation (SCHEME_OBJECT * scan, reloc_ref_t * ref)
{
  /* The last element of the block is always the tagged first entry of
     the closure, which tells us where the closure was in oldspace.  */
  (ref->old_addr) = (CC_ENTRY_ADDRESS (* ((CC_BLOCK_ADDR_END (scan)) - 1)));
  /* Find the address of the first entry in newspace.  */
  (ref->new_addr)
    = (tospace_to_newspace
       (compiled_closure_entry (compiled_closure_start (scan + 1))));
}

/* read_compiled_closure_target (start, ref)

   `start' points to the start of a closure entry in tospace, beginning
   with the format word and block offset.  `ref' was initialized with
   `start_closure_relocation'.  Return the untagged compiled entry
   address in oldspace that the closure entry points to.  */

insn_t *
read_compiled_closure_target (insn_t * start, reloc_ref_t * ref)
{
  insn_t * addr = (start + CC_ENTRY_PADDING_SIZE + CC_ENTRY_HEADER_SIZE);
  insn_t * base = (tospace_to_newspace (addr));
  /* If we're relocating, find where base was in the oldspace.  */
  if (ref)
    base += (ref->old_addr - ref->new_addr);
  char * from_pc = ((char *) base);
  int64_t offset = (((int64_t *) addr)[-1]);
  assert ((offset % (sizeof (insn_t))) == 0);
  assert ((offset % (sizeof (SCHEME_OBJECT))) == 0);
  /* Stored PC offsets are delta-free (see cmpint.h), so this is a
     canonical address, which is what the GC wants.  */
  char * to_pc = (from_pc + offset);
  return ((insn_t *) to_pc);
}

/* write_compiled_closure_target(target, start)

   `target' is an untagged compiled entry address in newspace.  `start'
   points to the start of a closure entry in tospace, beginning with
   the format word and block offset.  Set the closure entry at `start'
   to go to `target'.  */

void
write_compiled_closure_target (insn_t * target, insn_t * start)
{
  insn_t * addr = (start + CC_ENTRY_PADDING_SIZE + CC_ENTRY_HEADER_SIZE);
  char * from_pc = ((char *) (tospace_to_newspace (addr)));
  /* Both are canonical addresses, so the stored offset comes out
     delta-free, as the convention requires (see cmpint.h).  */
  char * to_pc = ((char *) target);
  int64_t offset = (to_pc - from_pc);
  assert ((offset % (sizeof (insn_t))) == 0);
  assert ((offset % (sizeof (SCHEME_OBJECT))) == 0);
  (((int64_t *) addr)[-1]) = offset;
}

unsigned long
compiled_closure_count (SCHEME_OBJECT * block)
{
  /* `block' is a pointer to the first object after the manifest.  The
     first object following it is the entry count.  */
  return ((unsigned long) (* ((uint32_t *) block)));
}

insn_t *
compiled_closure_start (SCHEME_OBJECT * block)
{
  return ((insn_t *) block);
}

insn_t *
compiled_closure_entry (insn_t * start)
{
  return (start + CC_ENTRY_PADDING_SIZE + CC_ENTRY_HEADER_SIZE);
}

insn_t *
compiled_closure_next (insn_t * start)
{
  return (start + CC_ENTRY_PADDING_SIZE + CC_ENTRY_HEADER_SIZE);
}

SCHEME_OBJECT *
skip_compiled_closure_padding (insn_t * start)
{
  return ((SCHEME_OBJECT *) start);
}

SCHEME_OBJECT
compiled_closure_entry_to_target (insn_t * entry)
{
  /* The computed PC is a shadow-view address when cc_exec_delta is
     nonzero; tagged datums must be canonical.  */
  return (MAKE_CC_ENTRY (CC_PC_TO_CANONICAL (CC_ENTRY_ADDRESS_PC (entry))));
}

/* Execution caches (UUO links)

   An execution cache is a region of memory that lives in the
   constants section of a compiled-code block.  It is an indirection
   for calling external procedures that allows the linker to control
   the calling process without having to find and change all the
   places in the compiled code that refer to it.

   Prior to linking, the execution cache has two pieces of
   information: (1) the name of the procedure being called (a symbol),
   and (2) the number of arguments that will be passed to the
   procedure.  `saddr' points to the arity at the beginning of the
   execution cache.  */

SCHEME_OBJECT
read_uuo_symbol (SCHEME_OBJECT * saddr)
{
  return (saddr[0]);
}

unsigned int
read_uuo_frame_size (SCHEME_OBJECT * saddr)
{
#ifdef WORDS_BIGENDIAN
  return ((saddr[2]) & 0xffff);
#else
  return ((saddr[1]) & 0xffff);
#endif
}

insn_t *
read_uuo_target (SCHEME_OBJECT * saddr)
{
  return ((insn_t *) (saddr[0]));
}

insn_t *
read_uuo_target_no_reloc (SCHEME_OBJECT * saddr)
{
  return (read_uuo_target (saddr));
}

void
write_uuo_target (insn_t * target, SCHEME_OBJECT * saddr)
{
  insn_t * iaddr;
  int ioff;

  /* Set the target.  */
  (saddr[0]) = ((SCHEME_OBJECT) target);

  /* Determine where the instructions start relative where we store the
     target.  */
#ifdef WORDS_BIGENDIAN
  ioff = 2;
#else
  ioff = 3;
#endif
  iaddr = (((insn_t *) saddr) + ioff);

  /* ldr x1, PC-ioff */
  (iaddr[0]) = (0x58000001UL | ((((unsigned) (-ioff)) & 0x7ffff) << 5));

  /* If the target PC is right after the target offset, then the PC
     requires no further relocation and we can jump to a fixed address.
     But if the target is a compiled closure pointing into a block
     somewhere else, the block may not have been relocated yet and so
     we don't know where the PC will be in the newspace.

     Stored PC offsets are delta-free (see cmpint.h), so a block entry
     whose instructions immediately follow it has offset 0.  */
  {
    int64_t target_off = (((const int64_t *) (newspace_to_tospace (target)))[-1]);
    if (target_off == 0)
    {
      char * from_pc = (tospace_to_newspace ((char *) (&iaddr[1])));
      char * to_pc = ((char *) target);
      ptrdiff_t offset = (to_pc - from_pc);
      assert ((offset % 4) == 0); /* Must be instruction-aligned.  */
      if ((-0x08000000 <= offset) && (offset <= 0x07ffffff))
	{
	  /* Branch takes 26-bit signed instruction (4-byte) offset.  */
	  unsigned imm26 = ((((unsigned) offset) >> 2) & 0x03ffffff);
	  /* b target */
	  (iaddr[1]) = (0x14000000UL | imm26);
	}
      else
	{
	  /* ADRP computes PC - (PC mod 2^12) + 2^12*offset.  We know
	     target - PC, and we want target.  First we add the page
	     offset; then we add target's location in its page.  */
	  uintptr_t from_pg = (((uintptr_t) from_pc) >> 12);
	  uintptr_t to_pg = (((uintptr_t) to_pc) >> 12);
	  ptrdiff_t pgoff = (((intptr_t) to_pg) - ((intptr_t) from_pg));
	  if ((-0x00100000 <= pgoff) && (pgoff <= 0x000fffff))
	    {
	      unsigned lo12 =
		(((uintptr_t) to_pc) - (((uintptr_t) to_pg) << 12));
	      unsigned pglo2 = (pgoff & 3);
	      unsigned pghi19 = ((pgoff & 0x001fffff) >> 2);
	      assert (to_pc == ((char *) (to_pg + lo12)));
	      assert
		(to_pg == (from_pg + (((unsigned long) pghi19 << 2) | pglo2)));
	      /* adrp x17, target */
	      (iaddr[1]) = (0x90000011UL | (pglo2 << 29) | (pghi19 << 5));
	      /* add x17, x17, #off */
	      (iaddr[2]) = (0x91000231UL | (lo12 << 10));
	      /* br x17 */
	      (iaddr[3]) = 0xd61f0220UL;
	    }
	  else
	    /* You have too much memory.  */
	    error_external_return ();
	}
    }
  else
    {
      /* The target is a compiled closure, whose PC we cannot compute
	 until it has been relocated.  Computing it at run time needs
	 more instructions than fit here (the whole cache is 32 bytes,
	 and the W^X delta must be added because stored PC offsets are
	 delta-free), so defer to hook_closure_apply, reached through
	 the hooks register.  x1 already holds the entry address.  */
      unsigned off = (HOOK_CLOSURE_APPLY_INDEX * HOOK_SIZE_IN_BYTES);
      assert (off < 0x1000);	/* must fit ADD's 12-bit immediate */
      /* add ip0, HOOKS, #off */
      (iaddr[1])
	= (0x91000000UL | (off << 10) | (AARCH64_HOOKS_REGNO << 5)
	   | AARCH64_IP0_REGNO);
      /* br ip0 */
      (iaddr[2]) = (0xd61f0000UL | (AARCH64_IP0_REGNO << 5));
    }
  }
}

#define TRAMPOLINE_ENTRY_PADDING_SIZE 1
#define OBJECTS_PER_TRAMPOLINE_ENTRY 4

unsigned long
trampoline_entry_size (unsigned long n_entries)
{
  return (n_entries * OBJECTS_PER_TRAMPOLINE_ENTRY);
}

insn_t *
trampoline_entry_addr (SCHEME_OBJECT * block, unsigned long index)
{
  return (((insn_t *) (block + 2 + (index * OBJECTS_PER_TRAMPOLINE_ENTRY)))
	  + TRAMPOLINE_ENTRY_PADDING_SIZE + CC_ENTRY_HEADER_SIZE);
}

insn_t *
trampoline_return_addr (SCHEME_OBJECT * block, unsigned long index)
{
  return (trampoline_entry_addr (block, index));
}

bool
store_trampoline_insns (insn_t * entry, uint8_t code)
{
  /* Stored PC offsets are delta-free (see cmpint.h); the trampoline's
     instructions begin at the entry, so the offset is zero.  Use sites
     add the W^X delta.  */
  (((int64_t *) entry)[-1]) = 0;
  /* movz x17, #code */
  (entry[0]) = (0xd2800011UL | (((unsigned) code) << 5));
  /* adr x1, storage (pc + 12) */
  (entry[1]) = 0x10000061UL;
  /* br x23 (scheme-to-interface) */
  (entry[2]) = 0xd61f02e0UL;
  return (false);		/* no error */
}

void
aarch64_reset_hook (void)
{
  /* On W^X systems (cc_exec_delta != 0) the heap is mapped writable
     with a separate executable shadow view, set up when the heap was
     allocated; nothing to do here.  Note that after a band restore the
     restored code blocks' entry PC offsets must include cc_exec_delta;
     that adjustment belongs in the fasload relocation sweep, not here,
     because this hook has no way to enumerate the blocks.  */
}

#ifdef __APPLE__

void
aarch64_flush_i_cache_region (SCHEME_OBJECT * start, size_t nwords)
{
  /* Instructions are fetched through the executable shadow view, so
     invalidate at the shadow addresses.  sys_icache_invalidate also
     pushes the data cache, which is what we need after writing
     instructions through the writable view.  */
  size_t nbytes = (nwords * (sizeof (SCHEME_OBJECT)));
  char * q = ((char *) start);
  if ((cc_exec_delta != 0)
      && ((((unsigned long) q) - cc_exec_base) < cc_exec_size))
    q += cc_exec_delta;
  sys_icache_invalidate (q, nbytes);
}

#else /* !__APPLE__ */

/* Reading CTR_EL0 is allowed on Linux and the BSDs but traps on macOS,
   which is one reason the macOS path above uses the system call.  */
static inline void
aarch64_cache_line_sizes (unsigned *dsizep, unsigned *isizep)
{
  uint64_t ctr_el0, dsize, isize;

  asm volatile ("\n\
    mrs %0, ctr_el0\n\
    ubfx %1, %0, #16, #4\n\
    ubfx %2, %0, #0, #4\n\
  " : "=r"(ctr_el0), "=r"(dsize), "=r"(isize));
  (*dsizep) = dsize;
  (*isizep) = isize;
}

void
aarch64_flush_i_cache_region (SCHEME_OBJECT * start, size_t nwords)
{
  unsigned dsize, isize;
  size_t nbytes = (nwords * (sizeof (SCHEME_OBJECT)));
  char * p;
  size_t n;

  /* Get the cache line sizes.  */
  aarch64_cache_line_sizes ((&dsize), (&isize));

  /* Flush the data cache lines.  */
  n = ((nbytes + (dsize - 1)) / dsize);
  for (p = ((char *) start); n --> 0; p += dsize)
    asm volatile ("dc cvau, %0" : : "r"(p));

  /* All data writes must complete before any following data reads.  */
  asm volatile ("dsb ish");

  /* Flush the instruction cache lines.  */
  n = ((nbytes + (isize - 1)) / isize);
  for (p = ((char *) start); n --> 0; p += isize)
    asm volatile ("ic ivau, %0" : : "r"(p));

  /* All cache flushes happen before any following instruction fetches.  */
  asm volatile ("isb");
}

#endif /* !__APPLE__ */

void
aarch64_flush_i_cache (void)
{
  /* Can't do `ic iallu' because that's privileged.  */
  aarch64_flush_i_cache_region (constant_start, Free - constant_start);
}
