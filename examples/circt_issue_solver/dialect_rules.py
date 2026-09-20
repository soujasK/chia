"""CIRCT Dialect Invariant Rulebook and Expert C++ Pattern Library.

Provides compiler-specific invariant guidelines and C++ pattern recipes for:
- FIRRTL (connect semantics, passive types, bundle/vector typing)
- HW (module ports, wire legalizations, instance binding)
- Comb (width matching, canonicalizations, signed/unsigned arithmetic)
- SV (always blocks, blocking vs non-blocking assignments, SystemVerilog emissions)
"""
from __future__ import annotations

import re


_RULES = {
    "FIRRTL": [
        "**Strict Connect Invariant**: `firrtl.strictconnect` requires destination and source types to match exactly.",
        "**Passive Types**: Ports and wires must be cast to passive types via `asPassive` when driving an input.",
        "**Bundle Fields**: Accessing fields on bundles must preserve field orientation (flip vs normal).",
        "**Width Propagation**: When creating integer ops, explicitly query or infer widths via `type.getBitWidthOrSentinel()`.",
        "**C++ Idiom**: Prefer `TypeSwitch<Type>` and `firrtl::type_cast<T>()` over unchecked `cast<T>()`."
    ],
    "HW": [
        "**Module Interfaces**: All port types on `hw.module` must be concrete HW types (e.g. `hw::IntType`, `hw::ArrayType`).",
        "**Instance Binding**: Ports must match the target module definition; use `hw::InstanceOp` with matching parameter attributes.",
        "**Wire Invariants**: Avoid circular combinational wire connections without register or pipeline boundaries.",
        "**C++ Idiom**: Use `rewriter.create<hw::ConstantOp>(loc, APInt(width, val))` for hardware constants."
    ],
    "Comb": [
        "**Bitwidth Matching**: Binary comb ops (add, sub, mul, and, or, xor) require both operands to have identical bitwidths.",
        "**Bit Manipulation**: `comb.concat` results in width = sum(operands); `comb.extract` requires `lowBit + bitWidth <= inputWidth`.",
        "**Canonicalization**: Ensure constant foldings return normalized canonical attributes.",
        "**C++ Idiom**: Use `comb::computeBitWidth()` and `mlir::PatternRewriter::replaceOpWithNewOp<comb::...>`."
    ],
    "SV": [
        "**Assignment Semantics**: Use `sv.assign` for continuous assign, `sv.bpassign` for blocking, `sv.passign` (<=) for non-blocking.",
        "**Clock Domains**: Ensure `sv.alwaysff` blocks only contain registers clocked by explicit clock edges.",
        "**C++ Idiom**: Avoid generating naked string literals in SystemVerilog; use structured SV dialect ops."
    ]
}


def detect_dialect_from_context(repro_output: str = "", pass_name: str = "", code_text: str = "") -> str:
    """Infer the primary CIRCT dialect from logs, pass names, or code text."""
    combined = f"{repro_output} {pass_name} {code_text}".lower()

    if "firrtl" in combined or "chirrtl" in combined:
        return "FIRRTL"
    if "comb" in combined:
        return "Comb"
    if "hw" in combined:
        return "HW"
    if "sv" in combined or "verilog" in combined:
        return "SV"

    return "FIRRTL"


def get_dialect_rules(dialect_name: str) -> list[str]:
    """Retrieve expert invariant rules for a given dialect."""
    return _RULES.get(dialect_name.upper(), _RULES["FIRRTL"])


def format_dialect_guidance_for_prompt(dialect_name: str) -> str:
    """Format dialect-specific invariant guidance for injection into LLM prompts."""
    rules = get_dialect_rules(dialect_name)
    lines = [
        f"## Domain-Specific Dialect Invariant Guidance ({dialect_name})",
        "Adhere to these CIRCT compiler architectural invariants when synthesizing your fix:"
    ]
    for r in rules:
        lines.append(f"- {r}")
    lines.append("")
    return "\n".join(lines)
