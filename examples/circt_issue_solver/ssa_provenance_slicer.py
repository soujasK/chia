"""SSA Provenance & Def-Use Backward Slicer for MLIR/CIRCT.

Constructs an SSA dataflow dependency graph and extracts the exact backward
slice originating from the failing operation, isolating the causal dataflow
cone while preserving module structure.
"""
from __future__ import annotations

import re
from typing import Set


_SSA_DEF_PATTERN = re.compile(r"^\s*(%[a-zA-Z0-9_\$#]+(?:\s*,\s*%[a-zA-Z0-9_\$#]+)*)\s*=\s*(.*)$")
_SSA_USE_PATTERN = re.compile(r"(%[a-zA-Z0-9_\$#]+)")
_MODULE_HEADER_PATTERN = re.compile(r"^\s*(?:firrtl\.circuit|firrtl\.module|hw\.module|module)\b")


def parse_ssa_line(line: str) -> tuple[list[str], list[str]]:
    """Return (defined_ssa_values, used_ssa_values) for a single line of MLIR."""
    defs: list[str] = []
    uses: list[str] = []

    m = _SSA_DEF_PATTERN.match(line)
    if m:
        defs_part = m.group(1)
        body = m.group(2)
        defs = [d.strip() for d in defs_part.split(",") if d.strip()]
        uses = _SSA_USE_PATTERN.findall(body)
    else:
        uses = _SSA_USE_PATTERN.findall(line)

    return defs, uses


def compute_ssa_backward_slice(mlir_text: str, target_op_line: str | None = None) -> str:
    """Compute the backward SSA slice from target_op_line in mlir_text.
    
    If target_op_line is None, slices from the last non-terminator operation.
    Keeps enclosing module/circuit boundaries intact.
    """
    if not mlir_text or not mlir_text.strip():
        return mlir_text

    lines = mlir_text.splitlines()
    if len(lines) <= 5:
        return mlir_text

    # 1. Identify target line index
    target_idx = -1
    if target_op_line:
        for idx, l in enumerate(lines):
            if target_op_line.strip() in l:
                target_idx = idx
                break

    if target_idx == -1:
        # Pick the last operation before the final closing braces/output
        for idx in range(len(lines) - 1, -1, -1):
            stripped = lines[idx].strip()
            if stripped and not stripped.startswith("}") and not stripped.startswith("hw.output"):
                target_idx = idx
                break

    if target_idx == -1:
        return mlir_text

    # 2. Build backward def-to-line index and identify initial needed values
    def_to_line_idx: dict[str, int] = {}
    line_uses: dict[int, list[str]] = {}
    line_defs: dict[int, list[str]] = {}

    for idx, line in enumerate(lines):
        d_vals, u_vals = parse_ssa_line(line)
        line_defs[idx] = d_vals
        line_uses[idx] = u_vals
        for d in d_vals:
            def_to_line_idx[d] = idx

    # 3. Transitive backward traversal from target
    needed_lines: Set[int] = {target_idx}
    worklist: list[str] = list(line_uses.get(target_idx, []))
    visited_ssa: Set[str] = set(worklist)

    while worklist:
        val = worklist.pop()
        if val in def_to_line_idx:
            def_line = def_to_line_idx[val]
            if def_line not in needed_lines:
                needed_lines.add(def_line)
                for next_use in line_uses.get(def_line, []):
                    if next_use not in visited_ssa:
                        visited_ssa.add(next_use)
                        worklist.append(next_use)

    # 4. Reconstruct slice while preserving structural lines (headers, braces, terminators)
    sliced_lines: list[str] = []
    for idx, line in enumerate(lines):
        stripped = line.strip()
        # Always preserve headers, module definitions, closing braces, and outputs
        is_structural = (
            _MODULE_HEADER_PATTERN.match(line)
            or stripped.startswith("}")
            or stripped.startswith("hw.output")
            or stripped.startswith("firrtl.strictconnect") and idx in needed_lines
            or stripped.startswith("firrtl.connect") and idx in needed_lines
        )
        if is_structural or idx in needed_lines:
            sliced_lines.append(line)

    return "\n".join(sliced_lines)
