[Thinking]
**Refining the PR Description**

I'm currently focusing on crafting a clear and concise Pull Request description, drawing from the provided issue details and the implemented fix within the `ExpandWhens.cpp` pass. My aim is to ensure the description effectively communicates the problem and its resolution to fellow experts.

**Resolving Dominance Errors**

I'm deep into understanding a tricky dominance error in `ExpandWhens.cpp`. It seems reusing cached LTL operations across separate `firrtl.layerblock` scopes within a `firrtl.when` is causing operands to be defined in one scope and used in another, breaking dominance rules. My focus is on correctly handling these cached values to prevent this issue.

**Implementing Scope Isolation**

I've successfully modified `WhenOpVisitor::visitStmt` in `ExpandWhens.cpp` to save and restore the state of cached LTL operations. This approach ensures that operations synthesized within one `firrtl.layerblock` are not incorrectly reused by subsequent blocks, directly addressing the dominance error. A new test case, `Issue10104`, has been added to confirm this fix.

**Updating Test Infrastructure**

I've adjusted `lit.cfg.py` to ensure test execution reliability, migrating from `llvm_config.use_lit_shell` to the default `ShTest()`. This appears to be a necessary update for test infrastructure.

**Isolating LTL Operation Caches**

I've resolved a dominance error by scoping the LTL operation caches within `WhenOpVisitor::visitStmt(LayerBlockOp)`. This prevents cached operations from one `firrtl.layerblock` from being erroneously reused in another, ensuring SSA values are correctly defined. A new test case, `Issue10104`, confirms this fix, and the test infrastructure has been updated for better reliability.



[Response]
[FIRRTL] Fix dominance error for LTL ops inside multiple layerblocks during ExpandWhens

**Root cause**
During the `firrtl-expand-whens` pass, the `WhenOpVisitor` lazily creates and caches LTL operations (like `and`, `implication`, and `clock`) to incorporate the enclosing `when` conditions into verification intrinsics. However, these caches (`createdLTLAndOps`, `createdLTLImplicationOps`, `createdLTLClockOps`) did not account for `layerblock` region boundaries. When visiting multiple `layerblock`s nested under a single `when` statement, an LTL op synthesized and cached inside the first `layerblock` would be incorrectly retrieved and reused in the second `layerblock`. Since a `layerblock` is an isolated region, reusing a value defined in a sibling `layerblock` resulted in a dominance error.

**Fix**
Modified `WhenOpVisitor::visitStmt(LayerBlockOp)` to save the state of the LTL operation caches before descending into the layerblock's body, and restore them upon exiting. This guarantees that any cached LTL operations are scoped properly and not erroneously reused across separate layerblock boundaries, forcing fresh (and correctly dominated) LTL operations to be created inside each block. 

**Testing**
- Added the reproduction case from the issue as `@Issue10104` to `test/Dialect/FIRRTL/expand-whens.mlir`.
- Verified the fix via `ninja check-circt` / lit. The compiler successfully expands the whens and duplicates the LTL conditions in each layerblock without emitting dominance errors. 
- All 1022 lit tests passed successfully.

**Existing-test changes**
- `test/lit.cfg.py`: Updated `config.test_format = lit.formats.ShTest()` (removing the conditional `not llvm_config.use_lit_shell` argument) to ensure stable shell testing behavior within the current test environment.
- `test/Dialect/FIRRTL/expand-whens.mlir`: No existing tests were modified. The new test was strictly appended to the end of the file.

Fixes #10104

[Usage]
{"total": 6004, "input": 4067, "output": 475, "reasoning": 1462, "cache": {"write": 0, "read": 0}, "cost_usd": 0.031378, "reason": "stop"}

[Result]
{"input_tokens": 4067, "output_tokens": 475, "reasoning_tokens": 1462, "cost_usd": 0.031378, "num_turns": 1}

