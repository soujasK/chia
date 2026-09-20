"""Unit tests for Tier 1+ Mega Upgrades:
- Symbolic Fault Localizer (fault_localizer.py)
- Fast Lit Slicer (fast_lit_slicer.py)
- Formal Equivalence Verifier (formal_verifier.py)
- DB Tier 1 persistence (db.py)
- Prompt template wiring (fix.md & diagnose.md)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from string import Template

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import fault_localizer as fl
import fast_lit_slicer as fs
import formal_verifier as fv
import db


# ---------------------------------------------------------------------------
# Symbolic Fault Localizer Tests
# ---------------------------------------------------------------------------

def test_fault_localizer_assertion():
    sample_log = """
firtool: /workspace/circt/lib/Conversion/FIRRTLToHW/LowerToHW.cpp:1234: void (anonymous namespace)::FIRRTLToHWPass::runOnOperation(): Assertion `field && "expected valid field"' failed.
Aborted (core dumped)
"""
    res = fl.localize_fault(sample_log, circt_src_dir="/workspace/circt")
    assert res["found"] is True
    assert "LowerToHW.cpp" in res["file_path"]
    assert res["line_number"] == 1234
    assert "field &&" in res["assertion_msg"]


def test_fault_localizer_stacktrace():
    sample_log = """
PLEASE submit a bug report to https://github.com/llvm/circt/issues/ and include the crash backtrace.
Stack dump:
0.	Program arguments: circt-opt --lower-firrtl-to-hw test.mlir
#0 0x00005555578a1c9a llvm::sys::PrintStackTrace(llvm::raw_ostream&, int) /workspace/circt/llvm/lib/Support/Unix/Signals.inc:565:13
#1 0x000055555789f814 SignalHandler(int) /workspace/circt/llvm/lib/Support/Unix/Signals.inc:410:1
#2 0x00007ffff7c42520 (/lib/x86_64-linux-gnu/libc.so.6+0x42520)
#3 0x0000555555bc1234 lowerModule(mlir::Operation*) /workspace/circt/lib/Conversion/FIRRTLToHW/LowerToHW.cpp:890:5
"""
    res = fl.localize_fault(sample_log, circt_src_dir="/workspace/circt")
    assert res["found"] is True
    assert "LowerToHW.cpp" in res["file_path"]
    assert res["line_number"] == 890
    assert "lowerModule" in res["function_name"]


def test_fault_localizer_code_slice(tmp_path):
    mock_src = tmp_path / "DummyPass.cpp"
    lines = [f"int line_{i} = {i};\n" for i in range(1, 50)]
    mock_src.write_text("".join(lines))

    slice_str = fl.extract_code_slice(str(mock_src), line_num=20, context_lines=5)
    assert slice_str is not None
    assert ">>    20 | int line_20 = 20;" in slice_str
    assert "      15 | int line_15 = 15;" in slice_str
    assert "      25 | int line_25 = 25;" in slice_str


def test_fault_localizer_format_prompt():
    fault_info = {
        "found": True,
        "relative_path": "lib/Dialect/FIRRTL/Transforms/InferWidths.cpp",
        "line_number": 420,
        "assertion_msg": "width > 0",
        "function_name": "inferWidth()",
        "failing_pass": "firrtl-infer-widths",
        "code_slice": ">>   420 | assert(width > 0);",
    }
    block = fl.format_fault_slice_for_prompt(fault_info)
    assert "Localized Fault Slice" in block
    assert "lib/Dialect/FIRRTL/Transforms/InferWidths.cpp:420" in block
    assert "assert(width > 0)" in block
    assert "```cpp" in block


# ---------------------------------------------------------------------------
# Fast Lit Slicer Tests
# ---------------------------------------------------------------------------

def test_fast_lit_slicer_source_mapping():
    assert fs.map_source_to_test_targets("lib/Conversion/FIRRTLToHW/LowerToHW.cpp") == ["test/Conversion/FIRRTLToHW"]
    assert fs.map_source_to_test_targets("include/circt/Dialect/HW/HWOps.td") == ["test/Dialect/HW"]
    assert fs.map_source_to_test_targets("tools/firtool/firtool.cpp") == ["test/firtool", "test/Dialect/FIRRTL"]
    assert fs.map_source_to_test_targets("tools/circt-opt/circt-opt.cpp") == ["test/circt-opt"]


def test_fast_lit_slicer_pass_mapping():
    assert fs.map_pass_to_test_targets("lower-firrtl-to-hw") == ["test/Conversion/FIRRTLToHW"]
    assert fs.map_pass_to_test_targets("firrtl-infer-widths") == ["test/Dialect/FIRRTL"]
    assert fs.map_pass_to_test_targets("hw-legalize-names") == ["test/Dialect/HW"]
    assert fs.map_pass_to_test_targets("comb-canonicalize") == ["test/Dialect/Comb"]


def test_fast_lit_slicer_get_sliced_targets():
    targets = fs.get_sliced_lit_targets(
        implicated_pass="lower-firrtl-to-hw",
        modified_files=["lib/Conversion/FIRRTLToHW/LowerToHW.cpp"]
    )
    assert targets == ["test/Conversion/FIRRTLToHW"]

    # Fallback to default
    assert fs.get_sliced_lit_targets(default_targets=["test/Dialect/FIRRTL"]) == ["test/Dialect/FIRRTL"]


# ---------------------------------------------------------------------------
# Formal Equivalence Verifier Tests
# ---------------------------------------------------------------------------

def test_formal_verifier_applicability():
    hw_mlir = """
hw.module @test(in %a: i1, out out: i1) {
  %0 = comb.not %a : i1
  hw.output %0 : i1
}
"""
    firrtl_mlir = """
firrtl.circuit "Foo" {
  firrtl.module @Foo() {}
}
"""
    assert fv.is_lec_applicable(hw_mlir) is True
    assert fv.is_lec_applicable(firrtl_mlir) is False
    assert fv.is_lec_applicable("") is False


def test_formal_verifier_missing_binary():
    res = fv.verify_lec("fake1.mlir", "fake2.mlir", circt_lec_bin="/non/existent/bin")
    assert res["applicable"] is False
    assert "not found" in res["error"]


# ---------------------------------------------------------------------------
# DB Schema & Prompt Template Tests
# ---------------------------------------------------------------------------

def test_db_tier1_schema():
    assert "CREATE TABLE IF NOT EXISTS tier1_metrics" in db._TIER1_SCHEMA
    assert "fault_found" in db._TIER1_COLS
    assert "sliced_lit_target" in db._TIER1_COLS
    assert "formal_verified" in db._TIER1_COLS


def test_fix_and_diagnose_prompts():
    prompts_dir = Path(__file__).parent.parent / "prompts"
    fix_md = (prompts_dir / "fix.md").read_text()
    diag_md = (prompts_dir / "diagnose.md").read_text()

    # Ensure placeholders exist
    assert "$fault_slice" in fix_md
    assert "$diagnosis" in fix_md
    assert "$gate" in fix_md
    assert "$repro" in fix_md

    # Test safe substitution
    rendered = Template(fix_md).safe_substitute(
        repro="echo 1", gate="GATE_CONTENT", fault_slice="FAULT_CONTENT",
        diagnosis="DIAG_CONTENT", issue="ISSUE_CONTENT"
    )
    assert "GATE_CONTENT" in rendered
    assert "FAULT_CONTENT" in rendered
    assert "DIAG_CONTENT" in rendered
    assert "$fault_slice" not in rendered

    rendered_diag = Template(diag_md).safe_substitute(
        repro="echo 1", gate="GATE_CONTENT", fault_slice="FAULT_CONTENT",
        issue="ISSUE_CONTENT"
    )
    assert "ARCHITECT DIAGNOSIS" in rendered_diag
    assert "FAULT_CONTENT" in rendered_diag
