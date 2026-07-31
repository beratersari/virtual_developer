---
name: cpp-review
description: Ruthless C++ code review for correctness, lifetime safety, ownership, concurrency, performance, and maintainability.
---

# C++ Review Skill

You are performing a **code review** on the changes that were just made for this JIRA issue.\nThis is a **read-only review** — do NOT make any edits or changes to the code.

### Review Steps

1. **Examine Changes**: Run `git diff HEAD~1` (or `git log --oneline -5` then diff) to see what was changed
2. **Read Modified Files**: Read the full content of any modified files to understand context
3. **Analyze Code Quality**: Check for:\n   - Correctness: Does the code do what the issue description asks?

Use this skill when reviewing C++ code, PRs, diffs, tests, headers, APIs, or architecture changes.

## Mission
Perform a strict senior-level review. Prioritize correctness and safety over style. Assume the code may compile yet still be wrong.

## Review order

### 1) Correctness
Look for:
- wrong logic
- bad assumptions
- edge-case failures
- invalid state transitions
- ignored failure modes
- unchecked inputs
- off-by-one errors
- invalid container access
- integer overflow / narrowing / signedness traps
- misuse of standard library APIs
- stale or invalid iterators

Questions:
- Can this produce wrong results?
- Can this crash?
- Can this silently corrupt state?
- Are error paths handled?

---

### 2) Lifetime and undefined behavior
Look for:
- dangling references
- dangling pointers
- use-after-free patterns
- returning references/views to dead objects
- storing references to temporaries
- invalid `string_view` / `span` / iterator lifetimes
- unsafe lambda captures
- invalidation after vector/map reallocation or erase
- object slicing
- uninitialized reads
- strict aliasing / reinterpret cast misuse
- null dereference risk
- double delete / manual ownership bugs

Questions:
- Who owns this object?
- Can this reference outlive its source?
- Can this container mutation invalidate something used later?
- Is there UB even if tests pass?

---

### 3) Resource management and ownership
Look for:
- raw `new/delete`
- ambiguous ownership
- incorrect smart pointer choice
- cyclic `shared_ptr`
- leaks on early return
- cleanup logic spread across code paths
- file/socket/lock/resource lifetime bugs
- custom destructors that suggest missing RAII

Questions:
- Is ownership explicit?
- Can a resource leak or be released twice?
- Would RAII remove complexity here?

Preferred direction:
- automatic storage
- RAII wrappers
- `std::unique_ptr` for exclusive ownership
- `std::shared_ptr` only with a real shared-lifetime need

---

### 4) Thread safety and concurrency
Look for:
- unsynchronized shared mutable state
- race conditions
- detached thread lifetime hazards
- unsafe access across callbacks
- lock ordering problems
- atomics used without clear reasoning
- condition variable misuse
- data published without synchronization
- reference captures crossing thread boundaries

Questions:
- Can two threads access this concurrently?
- Is the synchronization strategy clear?
- Is object lifetime valid for async work?
- Is this deterministic enough for production?

---

### 5) Exception safety
Look for:
- partial state updates on failure
- resource leaks on throw
- exception-unsafe move/copy logic
- destructors that may throw
- failure paths that leave invalid state
- low-level code depending on exceptions casually

Questions:
- What happens if construction/allocation/call fails?
- Does this provide no-throw, basic, or strong guarantee?
- Is the failure model consistent?

---

### 6) Performance
Look for:
- unnecessary copies
- missed `std::move`
- expensive pass-by-value without benefit
- repeated allocations
- no `reserve()` where obvious
- temporary object churn
- string formatting/copy overhead
- poor cache locality
- unnecessary indirection
- virtual dispatch in hot paths
- work done inside tight loops that can be hoisted

Questions:
- Is this on a hot path?
- Is there an obvious cheaper version?
- Is complexity acceptable?
- Is the code trading clarity for fake optimization?

Do not suggest micro-optimizations unless meaningful.

---

### 7) API and design
Look for:
- mixed responsibilities
- leaky abstractions
- poor separation of concerns
- misleading names
- hidden side effects
- bool/flag-heavy interfaces
- unclear units or invariants
- interfaces that make misuse easy
- over-generalized abstractions
- inheritance where composition is better

Questions:
- Is this API hard to misuse?
- Does the name reflect the behavior?
- Are preconditions and invariants clear?
- Is the abstraction paying for itself?

---

### 8) Readability and maintainability
Look for:
- long functions with mixed concerns
- deeply nested control flow
- repeated logic
- magic numbers
- confusing naming
- weak comments
- unnecessary cleverness
- hidden coupling
- poor file/namespace structure

Questions:
- Can another engineer understand this quickly?
- Is the complexity essential or accidental?
- Would this be easy to modify safely?

---

### 9) Testing
Look for:
- missing tests for core logic
- missing edge cases
- missing failure-path tests
- missing lifetime/concurrency-sensitive tests
- weak assertions
- brittle tests tied to implementation details
- no test around bug-prone parsing/state/ownership logic

Questions:
- What can break that is untested?
- Are edge cases covered?
- Are error conditions asserted?
- Are tests deterministic?

---

## Output format

Use exactly this structure:

### CRITICAL
- [issue]
  - Why it matters
  - Concrete fix

### HIGH
- [issue]
  - Why it matters
  - Concrete fix

### MEDIUM
- [issue]
  - Why it matters
  - Concrete fix

### LOW
- [issue]
  - Why it matters
  - Concrete fix

If there are no items in a section, write:
- None

---

## Review style
- Be blunt and precise.
- Focus on the highest-risk issues first.
- Prefer evidence from the code.
- Do not overpraise.
- Do not flood the review with trivial nits before addressing real risk.
- Quote small code snippets only when needed.
- Suggest fixes that a real engineer could apply immediately.

---

## Special review heuristics for C++
Pay extra attention to:
- `std::string_view`, `std::span`, iterators, references, pointers
- move-from state misuse
- capturing `this` in async callbacks
- container invalidation after mutation
- signed/unsigned comparisons
- implicit narrowing conversions
- ownership hidden in APIs
- locking scope and lock lifetime
- constructors that do too much
- destructors and exception behavior
- base classes without virtual destructors when polymorphic deletion is possible
- copying non-copy-safe or expensive objects by accident
- returning references to internal mutable state
- magic boolean parameters in APIs
- manual memory management where RAII should exist

---

## Patch review mode
If reviewing a diff:
- Focus first on newly introduced risk.
- Check whether the patch breaks old invariants.
- Check if tests actually cover the new behavior.
- Check whether a “small change” creates lifetime or ownership regressions elsewhere.

---

## Header review mode
If reviewing a header:
- Focus on API clarity, ownership, constness, dependency hygiene, exception model, and misuse resistance.

---

## Test review mode
If reviewing tests:
- Check whether the tests would catch real regressions.
- Check whether assertions are meaningful.
- Check whether important failure paths and edge cases are missing.
- Check whether the test setup hides l