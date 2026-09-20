"""Unit tests for the God-Tier CIRCT Issue Solver Architecture.

Covers:
1. Pipeline Bisector (phase-ordering isolation, flag parsing, context formatting)
2. Dialect Fix Memory (compiler idiom retrieval, trigger keyword matching)
3. Sequential Verifier (register/clock detection, 1-step transition miter synthesis)
4. ClangFormat Agent (pure-Python LLVM 2-space formatting, hygiene audit, diff sanitization)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Add parent directory to sys.path so modules can be imported directly
SOLVER_DIR = Path(__file__).resolve().parent.parent
if str(SOLVER_DIR) not in sys.path:
    sys.path.insert(0, str(SOLVER_DIR))

import pipeline_bisector
import dialect_fix_memory
import sequential_verifier
import clang_format_agent


# --------------------------------------------------------------------------- #
# 1. Pipeline Bisector Tests
# --------------------------------------------------------------------------- #

def test_parse_pipeline_flags():
    cmd = (
        "circt-opt repro.mlir --lower-firrtl-to-hw -firrtl-infer-widths "
        "-o out.mlir --split-input-file --verify-diagnostics"
    )
    passes = pipeline_bisector.parse_pipeline_flags(cmd)
    assert "--lower-firrtl-to-hw" in passes
    assert "-firrtl-infer-widths" in passes
    # Non-pass flags must be excluded
    assert "-o" not in passes
    assert "--split-input-file" not in passes
    assert "--verify-diagnostics" not in passes


def test_bisect_pipeline_single_pass():
    res = pipeline_bisector.bisect_pipeline(["--pass-one"], "module {}")
    assert not res["is_pipeline"]
    assert res["total_passes"] == 1
    assert res["root_originating_pass"] == "--pass-one"
    assert not res["is_phase_ordering_bug"]


def test_format_pipeline_bisection_context():
    sample_res = {
        "is_pipeline": True,
        "total_passes": 3,
        "root_originating_pass": "--lower-firrtl-to-hw",
        "is_phase_ordering_bug": True,
        "diagnosis": "Phase-ordering violation: Pass --lower-firrtl-to-hw corrupted intermediate IR.",
        "minimal_failing_prefix": ["--firrtl-infer-widths", "--lower-firrtl-to-hw"],
    }
    context = pipeline_bisector.format_pipeline_bisection_context(sample_res)
    assert "Compiler Pipeline Bisection Analysis" in context
    assert "YES (earlier pass corrupted IR)" in context
    assert "--lower-firrtl-to-hw" in context


# --------------------------------------------------------------------------- #
# 2. Dialect Fix Memory Tests
# --------------------------------------------------------------------------- #

def test_dialect_fix_memory_retrieval_firrtl():
    idioms = dialect_fix_memory.retrieve_dialect_idioms(
        "FIRRTL", context_text="Crash with unhandled null element type in memory subfield"
    )
    assert len(idioms) > 0
    assert any(i["id"] == "firrtl_unhandled_type_guard" for i in idioms)
    assert "notifyMatchFailure" in idioms[0]["cpp_snippet"]


def test_dialect_fix_memory_retrieval_comb():
    idioms = dialect_fix_memory.retrieve_dialect_idioms(
        "Comb", context_text="Assertion bitwidth mismatch concat failed"
    )
    assert len(idioms) > 0
    assert any("concat" in i["id"] for i in idioms)


def test_format_idioms_for_prompt():
    prompt_str = dialect_fix_memory.format_idioms_for_prompt(
        "FIRRTL", "recursion cycle in width inference"
    )
    assert "Canonical LLVM/CIRCT C++ Fix Idioms" in prompt_str
    assert "```cpp" in prompt_str


# --------------------------------------------------------------------------- #
# 3. Sequential Verifier Tests
# --------------------------------------------------------------------------- #

SAMPLE_SEQUENTIAL_MLIR = """
hw.module @Top(in %clock : !seq.clock, in %reset : i1, in %in : i32, out out : i32) {
  %reg0 = seq.firreg %in clock %clock : i32
  %reg1 = firrtl.reg %clock : i32
  %sum = comb.add %reg0, %in : i32
  hw.output %sum : i32
}
"""

SAMPLE_COMBINATIONAL_MLIR = """
hw.module @Add(in %a : i32, in %b : i32, out out : i32) {
  %res = comb.add %a, %b : i32
  hw.output %res : i32
}
"""

def test_detect_sequential_elements():
    seq_res = sequential_verifier.detect_sequential_elements(SAMPLE_SEQUENTIAL_MLIR)
    assert seq_res["is_sequential"] is True
    assert seq_res["reg_count"] >= 2
    assert "seq.firreg" in seq_res["sequential_ops"]
    assert "firrtl.reg" in seq_res["sequential_ops"]

    comb_res = sequential_verifier.detect_sequential_elements(SAMPLE_COMBINATIONAL_MLIR)
    assert comb_res["is_sequential"] is False
    assert comb_res["reg_count"] == 0


def test_synthesize_transition_miter():
    miter = sequential_verifier.synthesize_transition_miter(SAMPLE_SEQUENTIAL_MLIR)
    # Register feedback definitions should be replaced with open-loop constant state anchors
    assert "State transition cut:" in miter
    assert "hw.constant 0" in miter
    # Neither seq.firreg nor firrtl.reg should be instantiated as active cyclic feedback
    assert "seq.firreg %in clock" not in miter


def test_format_sequential_context():
    seq_info = sequential_verifier.detect_sequential_elements(SAMPLE_SEQUENTIAL_MLIR)
    context = sequential_verifier.format_sequential_context(seq_info)
    assert "Sequential Circuit State Analysis" in context
    assert "Registers Detected" in context


# --------------------------------------------------------------------------- #
# 4. ClangFormat Agent Tests
# --------------------------------------------------------------------------- #

def test_format_code_pure_python_tabs_and_spaces():
    raw_code = "\t\tvoid foo() {\n\t\t\tint a = 1;   \n\n\n\t\t\treturn;\n\t\t}\n"
    formatted = clang_format_agent.format_code_pure_python(raw_code)
    # Tabs converted to 2 spaces per tab
    assert "\t" not in formatted
    assert "    void foo() {" in formatted
    # Trailing spaces stripped
    assert "int a = 1;   " not in formatted
    assert "      int a = 1;" in formatted


def test_audit_hygiene_clean():
    clean_code = "  // Valid LLVM C++ code\n  if (!val)\n    return failure();\n"
    audit = clang_format_agent.audit_hygiene(clean_code)
    assert audit["is_clean"] is True
    assert audit["has_tabs"] is False
    assert audit["has_trailing_whitespace"] is False
    assert len(audit["debug_violations"]) == 0


def test_audit_hygiene_disallowed_debug():
    dirty_code = (
        "  printf(\"debug value: %d\\n\", x);\n"
        "  std::cout << \"hit line\" << std::endl;\n"
    )
    audit = clang_format_agent.audit_hygiene(dirty_code)
    assert audit["is_clean"] is False
    assert len(audit["debug_violations"]) == 2


def test_sanitize_patch():
    raw_diff = (
        "--- a/lib/Dialect/FIRRTL/Transforms/Pass.cpp\n"
        "+++ b/lib/Dialect/FIRRTL/Transforms/Pass.cpp\n"
        "@@ -10,3 +10,4 @@\n"
        " context;\n"
        "+\t\tauto elem = op.getType();   \n"
        "+\t\treturn rewriter.notifyMatchFailure(op, \"err\");\n"
    )
    sanitized = clang_format_agent.sanitize_patch(raw_diff)
    # Tabs stripped on added lines and trailing whitespace stripped
    assert "\t" not in sanitized
    assert "+    auto elem = op.getType();" in sanitized
    assert not sanitized.endswith("   \n")
