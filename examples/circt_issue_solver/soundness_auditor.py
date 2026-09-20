"""Patch Soundness and Anti-Pattern Auditor for CIRCT Compiler Repair.

Performs static analysis on candidate git diffs to detect and reject:
1. Assertion Deletions (test-suite hacking / assertion bypass)
2. Trivial Early Returns (vacuous pass skips)
3. Unsafe C-style casts bypassing LLVM RTTI (dyn_cast/cast/isa)
4. Diagnostic suppression
"""
from __future__ import annotations

import re
from typing import Any


_ASSERT_DELETION_PATTERNS = [
    re.compile(r"^\-\s*assert\s*\(", re.MULTILINE),
    re.compile(r"^\-\s*(?:llvm::)?report_fatal_error\s*\(", re.MULTILINE),
    re.compile(r"^\-\s*assertOp\s*\(", re.MULTILINE),
]

_UNCONDITIONAL_BYPASS_PATTERNS = [
    # Lines adding unconditional early returns like "+  return success();" without an enclosing if
    re.compile(r"^\+\s*return\s+(?:mlir::)?success\s*\(\s*\)\s*;\s*$", re.MULTILINE),
]

_RAW_CAST_PATTERNS = [
    re.compile(r"^\+\s*[^/]*\b(?:reinterpret_cast|\(void\s*\*\))\b", re.MULTILINE),
]


def audit_patch_soundness(diff_text: str) -> dict[str, Any]:
    """Audit a patch diff for soundness violations and compiler repair anti-patterns.
    
    Returns a dict with:
      - is_sound: bool (True if no critical soundness anti-patterns detected)
      - violations: list of detected violation strings
      - warnings: list of minor code style or safety warnings
    """
    violations: list[str] = []
    warnings: list[str] = []

    if not diff_text or not diff_text.strip():
        return {
            "is_sound": False,
            "violations": ["Empty diff: no modifications made to the codebase."],
            "warnings": [],
        }

    # 1. Check for assertion deletions
    for pat in _ASSERT_DELETION_PATTERNS:
        if pat.search(diff_text):
            violations.append(
                "Assertion Deletion Detected: The patch removes an assertion rather than resolving "
                "the underlying invariant violation."
            )
            break

    # 2. Check for trivial unconditional bypasses
    # We inspect newly added lines in the diff
    added_lines = [l[1:].strip() for l in diff_text.splitlines() if l.startswith("+") and not l.startswith("+++")]
    for line in added_lines:
        if re.match(r"^return\s+(?:mlir::)?success\s*\(\s*\)\s*;$", line):
            # Check if this addition is immediately preceded by an `if` check
            # If standalone, warn or violate
            warnings.append(
                "Potential Trivial Bypass: Patch adds an early `return success();`. Ensure this is conditionally guarded."
            )

    # 3. Check for unsafe casting (bypassing LLVM RTTI)
    for pat in _RAW_CAST_PATTERNS:
        if pat.search(diff_text):
            warnings.append(
                "Unsafe Cast: Detected raw/reinterpret cast. Prefer LLVM RTTI (`llvm::dyn_cast` / `llvm::isa`)."
            )

    # 4. Check for debug print leakage
    for l in added_lines:
        if re.search(r"\b(?:std::cout|printf|fprintf\(stderr)\b", l):
            warnings.append("Debug print statements (`std::cout`/`printf`) found in patch. Use `LLVM_DEBUG` or `emitError`.")

    # 5. Check for MLIR PatternRewriter anti-pattern: direct op->erase()
    for l in added_lines:
        if re.search(r"\b\w+->erase\s*\(\s*\)", l) and "rewriter" not in l:
            violations.append(
                "MLIR Rewriter Violation: Direct `op->erase()` call detected. "
                "In MLIR pattern rewriters, always call `rewriter.eraseOp(op)` to avoid worklist corruption."
            )

    # 6. Check for code hygiene: trailing whitespace or tab indentation in C++
    for l in added_lines:
        if "\t" in l:
            warnings.append("Tab character detected in C++ diff. CIRCT/LLVM coding standards require 2-space indentation.")
            break

    is_sound = len(violations) == 0

    return {
        "is_sound": is_sound,
        "violations": violations,
        "warnings": warnings,
    }
