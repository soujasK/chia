"""Automated MLIR Lit Regression Test Synthesizer for CIRCT.

Synthesizes durable LLVM lit regression tests (RUN + FileCheck directives)
from minimized reproducers, ensuring every patch contains a committed regression
guard under test/ adhering to CIRCT testing standards.
"""
from __future__ import annotations

import os
import re
from typing import Any


def synthesize_lit_test(
    minimized_mlir: str,
    failing_pass: str | None = None,
    tool_name: str = "circt-opt",
    issue_number: int | None = None,
    expected_error: str | None = None
) -> str:
    """Synthesize a standard CIRCT lit test from a minimized MLIR snippet."""
    if not minimized_mlir or not minimized_mlir.strip():
        return ""

    pass_flag = f"--{failing_pass.lstrip('-')}" if failing_pass else "--canonicalize"
    issue_comment = f"// Regression test for issue #{issue_number}\n" if issue_number else ""

    if expected_error:
        run_line = f"// RUN: {tool_name} %s {pass_flag} --verify-diagnostics | FileCheck %s"
    else:
        run_line = f"// RUN: {tool_name} %s {pass_flag} | FileCheck %s"

    lines = [
        issue_comment.strip(),
        run_line,
        "",
    ]

    # Generate FileCheck check lines
    for line in minimized_mlir.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append(line)
            continue

        # For top-level modules, generate CHECK-LABEL
        if re.search(r"\b(?:firrtl\.circuit|firrtl\.module|hw\.module|module)\s+@?([a-zA-Z0-9_\$#]+)", stripped):
            m = re.search(r"(@[a-zA-Z0-9_\$#]+)", stripped)
            name = m.group(1) if m else ""
            lines.append(f"// CHECK-LABEL: {stripped.split()[0]} {name}".strip())
            lines.append(line)
        elif "hw.output" in stripped or "firrtl.strictconnect" in stripped:
            lines.append(f"// CHECK: {stripped}")
            lines.append(line)
        else:
            lines.append(line)

    return "\n".join(lines).strip() + "\n"


def infer_lit_test_destination(
    implicated_pass: str | None,
    issue_number: int | None = None,
    circt_src_dir: str = "/workspace/circt"
) -> str:
    """Infer the appropriate directory and filename for a new lit regression test."""
    filename = f"issue_{issue_number}.mlir" if issue_number else "regression_test.mlir"
    p = (implicated_pass or "").lower().strip().lstrip("-")

    if "to" in p:
        # Conversion pass: e.g. lower-firrtl-to-hw -> test/Conversion/FIRRTLToHW
        parts = p.split("to")
        src = parts[0].replace("lower-", "").replace("lower", "").upper()
        dst = parts[1].upper() if len(parts) > 1 else "HW"
        conv_dir = f"{src}To{dst}"
        # Normalize common directory names
        if "FIRRTL" in conv_dir and "HW" in conv_dir:
            conv_dir = "FIRRTLToHW"
        target_dir = os.path.join(circt_src_dir, "test", "Conversion", conv_dir)
    elif "hw" in p:
        target_dir = os.path.join(circt_src_dir, "test", "Dialect", "HW")
    elif "comb" in p:
        target_dir = os.path.join(circt_src_dir, "test", "Dialect", "Comb")
    elif "sv" in p:
        target_dir = os.path.join(circt_src_dir, "test", "Dialect", "SV")
    else:
        # Default dialect
        target_dir = os.path.join(circt_src_dir, "test", "Dialect", "FIRRTL")

    return os.path.join(target_dir, filename)


def has_lit_test_in_diff(diff_text: str) -> bool:
    """True if the diff modifies or creates a test file under test/."""
    if not diff_text:
        return False
    for line in diff_text.splitlines():
        if line.startswith("+++ b/test/") or line.startswith("--- a/test/"):
            return True
    return False
