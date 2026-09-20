"""Dialect Fix Memory and Canonical Compiler Idiom Knowledge Base.

Stores verified, maintainer-grade C++ repair patterns for MLIR/CIRCT dialects.
When diagnosing an issue, this module retrieves the matching canonical C++ idiom
and injects it as an in-context exemplar to guide synthesis toward idiomatic,
zero-regression compiler fixes.
"""
from __future__ import annotations

import re
from typing import Any


DIALECT_IDIOMS: dict[str, list[dict[str, Any]]] = {
    "FIRRTL": [
        {
            "id": "firrtl_unhandled_type_guard",
            "trigger_keywords": ["unhandled", "null", "elemtype", "memory", "subfield", "cast"],
            "pattern_name": "Safe Pattern Match Failure on Unhandled Types",
            "explanation": "In FIRRTL lowering, guard dereferences of nested/aggregate types and notify match failure cleanly.",
            "cpp_snippet": """// Canonical Idiom: Guard unhandled or null element types
auto memType = op.getType();
if (!memType || !memType.getElementType())
  return rewriter.notifyMatchFailure(op, "unhandled or invalid memory element type");
auto elemType = memType.getElementType();
assert(elemType && "expected valid element type");
rewriter.replaceOpWithNewOp<hw::WireOp>(op, elemType);""",
        },
        {
            "id": "firrtl_cycle_break_recursion",
            "trigger_keywords": ["recursion", "circular", "cycle", "infer", "width", "stack"],
            "pattern_name": "Visited Set Cycle Termination with UnknownWidthType",
            "explanation": "To prevent infinite recursion on self-referential cyclic wires, track visited nodes and return UnknownWidthType.",
            "cpp_snippet": """// Canonical Idiom: Cycle detection using llvm::DenseSet
if (!val || visited.count(val)) {
  val.setType(firrtl::UnknownWidthType::get(op.getContext()));
  return failure();
}
visited.insert(val);""",
        },
    ],
    "Comb": [
        {
            "id": "comb_bitwidth_alignment_concat",
            "trigger_keywords": ["width", "concat", "mismatch", "bitwidth", "assert", "size"],
            "pattern_name": "Pre-Condition Width Validation Before SMT Concat",
            "explanation": "Before emitting SMT or hardware bit-vector operations, validate operand width equality.",
            "cpp_snippet": """// Canonical Idiom: Width validation before SMT concatenation
auto lhsWidth = lhs.getType().getIntOrFloatBitWidth();
auto rhsWidth = rhs.getType().getIntOrFloatBitWidth();
if (lhsWidth != rhsWidth)
  return rewriter.notifyMatchFailure(op, "operand bitwidths must match for concat");
assert(lhsWidth == rhsWidth && "concat bitwidth mismatch");
rewriter.replaceOpWithNewOp<smt::BVConcatOp>(op, lhs, rhs);""",
        },
    ],
    "HW": [
        {
            "id": "hw_wire_port_sanitization",
            "trigger_keywords": ["wire", "port", "module", "direction", "interface"],
            "pattern_name": "Hardware Wire Replacement with Valid Inferred Type",
            "explanation": "Construct new hardware wire ops ensuring port types are fully resolved.",
            "cpp_snippet": """// Canonical Idiom: Safe WireOp creation
Type resType = op.getResult().getType();
if (!hw::type_isa<hw::TypeAliasType>(resType) && !resType.isInteger())
  return rewriter.notifyMatchFailure(op, "unsupported hardware wire type");
rewriter.replaceOpWithNewOp<hw::WireOp>(op, resType, op.getNameAttr());""",
        },
    ],
}


def retrieve_dialect_idioms(dialect: str, context_text: str = "", max_idioms: int = 2) -> list[dict[str, Any]]:
    """Retrieve relevant canonical compiler fix idioms matching the dialect and symptom."""
    d_clean = dialect.upper().replace("TO", "").replace("DIALECT", "")
    
    # Map dialect keywords
    matched_key = None
    for k in DIALECT_IDIOMS:
        if k.upper() in d_clean:
            matched_key = k
            break

    if not matched_key:
        matched_key = "FIRRTL"  # Default to FIRRTL idioms

    candidates = DIALECT_IDIOMS.get(matched_key, [])
    if not context_text:
        return candidates[:max_idioms]

    text_lower = context_text.lower()
    scored = []
    for c in candidates:
        score = sum(1 for kw in c["trigger_keywords"] if kw in text_lower)
        scored.append((score, c))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored[:max_idioms]]


def format_idioms_for_prompt(dialect: str, context_text: str = "") -> str:
    """Format canonical compiler idioms into Markdown for injection into the C++ synthesis prompt."""
    idioms = retrieve_dialect_idioms(dialect, context_text)
    if not idioms:
        return ""

    lines = [
        "### 🏛️ Canonical LLVM/CIRCT C++ Fix Idioms (Maintainer Best Practices)",
        "Follow these established MLIR pattern rewriter idioms for this dialect:",
        "",
    ]
    for idx, item in enumerate(idioms, 1):
        lines.append(f"#### Idiom {idx}: {item['pattern_name']}")
        lines.append(f"*{item['explanation']}*")
        lines.append(f"```cpp\n{item['cpp_snippet'].strip()}\n```")
        lines.append("")

    return "\n".join(lines)
