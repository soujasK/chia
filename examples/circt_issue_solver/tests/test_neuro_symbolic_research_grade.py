"""Research-Grade Neuro-Symbolic CEGIS Test Suite for NEURO-CIRCT.

Validates:
1. TableGen & ODS Semantic Introspection (tablegen_analyzer.py)
2. SSA Provenance & Def-Use Backward Slicing (ssa_provenance_slicer.py)
3. Patch Soundness & Anti-Pattern Auditing (soundness_auditor.py)
4. Adversarial CEGIS Boundary Mutation Generation (cegis_oracle.py)
5. Academic Benchmark Ablation & LaTeX Table Generator (benchmark_ablation.py)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tablegen_analyzer as ta
import ssa_provenance_slicer as sps
import soundness_auditor as sa
import cegis_oracle as co
import benchmark_ablation as ba


# ---------------------------------------------------------------------------
# 1. TableGen & ODS Semantic Introspection Tests
# ---------------------------------------------------------------------------

def test_tablegen_op_parsing(tmp_path):
    dialect_dir = tmp_path / "include" / "circt" / "Dialect" / "FIRRTL"
    dialect_dir.mkdir(parents=True)
    td_file = dialect_dir / "FIRRTLOps.td"

    sample_td = """
def SubfieldOp : FIRRTL_Op<"subfield", [Pure, SameOperandsAndResultType]> {
  let summary = "Extract a subfield from a bundle";
  let description = [{ Extracts field from bundle type }];
  let arguments = (ins FIRRTLType:$input, StrAttr:$fieldname);
  let results = (outs FIRRTLType:$result);
  let hasCustomVerifier = 1;
}
"""
    td_file.write_text(sample_td)

    op_info = ta.parse_tablegen_op("firrtl.subfield", circt_src_dir=str(tmp_path))
    assert op_info is not None
    assert op_info["mnemonic"] == "subfield"
    assert op_info["op_class"] == "SubfieldOp"
    assert "Pure" in op_info["traits"]
    assert "SameOperandsAndResultType" in op_info["traits"]
    assert op_info["has_verifier"] is True
    assert "Extract a subfield" in op_info["summary"]


def test_tablegen_format_context(tmp_path):
    dialect_dir = tmp_path / "include" / "circt" / "Dialect" / "FIRRTL"
    dialect_dir.mkdir(parents=True)
    td_file = dialect_dir / "FIRRTLOps.td"
    td_file.write_text("""
def AsPassiveOp : FIRRTL_Op<"asPassive", [Pure]> {
  let summary = "Cast to passive type";
  let arguments = (ins FIRRTLType:$input);
  let results = (outs FIRRTLType:$result);
}
""")
    md = ta.format_tablegen_context(["firrtl.asPassive"], circt_src_dir=str(tmp_path))
    assert "Formal TableGen / ODS Dialect Specifications" in md
    assert "firrtl.asPassive" in md
    assert "Cast to passive type" in md


# ---------------------------------------------------------------------------
# 2. SSA Provenance & Def-Use Backward Slicing Tests
# ---------------------------------------------------------------------------

def test_ssa_provenance_backward_slice():
    mlir = """firrtl.circuit "Top" {
  firrtl.module @Top(in %clock: !firrtl.clock, in %a: !firrtl.uint<1>, out %b: !firrtl.uint<1>) {
    %unrelated = firrtl.constant 42 : !firrtl.uint<8>
    %0 = firrtl.asPassive %a : (!firrtl.uint<1>) -> !firrtl.uint<1>
    %target = firrtl.not %0 : (!firrtl.uint<1>) -> !firrtl.uint<1>
    firrtl.strictconnect %b, %target : !firrtl.uint<1>
  }
}"""

    # Target is %target line
    sliced = sps.compute_ssa_backward_slice(mlir, target_op_line="%target = firrtl.not")
    assert "%unrelated" not in sliced
    assert "%0 = firrtl.asPassive" in sliced
    assert "%target = firrtl.not" in sliced
    assert 'firrtl.circuit "Top"' in sliced


# ---------------------------------------------------------------------------
# 3. Patch Soundness & Anti-Pattern Auditor Tests
# ---------------------------------------------------------------------------

def test_soundness_auditor_rejects_assertion_deletion():
    bad_diff = """--- a/lib/Conversion/FIRRTLToHW/LowerToHW.cpp
+++ b/lib/Conversion/FIRRTLToHW/LowerToHW.cpp
@@ -100,2 +100,1 @@
-  assert(field && "field must not be null");
   return success();
"""
    audit = sa.audit_patch_soundness(bad_diff)
    assert audit["is_sound"] is False
    assert any("Assertion Deletion" in v for v in audit["violations"])


def test_soundness_auditor_warns_on_unconditional_bypass():
    bypass_diff = """--- a/lib/Dialect/FIRRTL/Transforms/InferWidths.cpp
+++ b/lib/Dialect/FIRRTL/Transforms/InferWidths.cpp
@@ -50,3 +50,4 @@
 void InferWidthsPass::runOnOperation() {
+  return success();
   auto mod = getOperation();
"""
    audit = sa.audit_patch_soundness(bypass_diff)
    assert any("Trivial Bypass" in w for w in audit["warnings"])


def test_soundness_auditor_accepts_clean_patch():
    sound_diff = """--- a/lib/Conversion/FIRRTLToHW/LowerToHW.cpp
+++ b/lib/Conversion/FIRRTLToHW/LowerToHW.cpp
@@ -100,3 +100,5 @@
+  if (!field)
+    return rewriter.notifyMatchFailure(op, "missing field");
   assert(field && "field must not be null");
"""
    audit = sa.audit_patch_soundness(sound_diff)
    assert audit["is_sound"] is True
    assert len(audit["violations"]) == 0


# ---------------------------------------------------------------------------
# 4. Adversarial CEGIS Mutation Oracle Tests
# ---------------------------------------------------------------------------

def test_cegis_boundary_mutants():
    mlir = """
hw.module @test(in %a: i1, in %b: i32, out out: i1) {
  %0 = comb.add %a, %b : i1
  hw.output %0 : i1
}
"""
    mutants = co.generate_boundary_mutants(mlir)
    assert len(mutants) >= 2
    names = [m["name"] for m in mutants]
    assert "zero_width_boundary" in names
    assert "wide_integer_boundary" in names

    # Check 0-width mutation
    zero_m = next(m for m in mutants if m["name"] == "zero_width_boundary")
    assert "i0" in zero_m["mlir"]


# ---------------------------------------------------------------------------
# 5. Academic Benchmark Ablation & LaTeX Table Tests
# ---------------------------------------------------------------------------

def test_benchmark_ablation_metrics():
    runs = [
        {"status": "fixed", "lit_ok": True, "added": 5, "removed": 2, "gate": {"raw_ir_op_count": 500, "final_op_count": 10}, "tier1": {"sound": True}},
        {"status": "fixed", "lit_ok": True, "added": 12, "removed": 4, "gate": {"raw_ir_op_count": 200, "final_op_count": 20}, "tier1": {"sound": True}},
        {"status": "attempted", "lit_ok": False, "added": 0, "removed": 0, "gate": None, "tier1": {"sound": False}},
    ]
    res = ba.aggregate_research_metrics(runs)
    assert res["total_attempts"] == 3
    assert res["fixed_count"] == 2
    assert res["pass_at_1"] == 66.67
    assert res["soundness_ratio"] == 100.0
    assert res["avg_diff_added"] == 8.5
    assert res["avg_context_reduction"] == 30.0  # (50 + 10) / 2


def test_benchmark_ablation_latex():
    data = {
        "Direct Prompting": {"pass_at_1": 25.0, "soundness_ratio": 50.0, "avg_diff_added": 45, "avg_diff_removed": 20, "avg_context_reduction": 1.0},
        "Context-Precision Gate": {"pass_at_1": 62.5, "soundness_ratio": 75.0, "avg_diff_added": 15, "avg_diff_removed": 5, "avg_context_reduction": 18.4},
        "NEURO-CIRCT (Ours)": {"pass_at_1": 87.5, "soundness_ratio": 100.0, "avg_diff_added": 6, "avg_diff_removed": 2, "avg_context_reduction": 24.8},
    }
    latex = ba.generate_latex_table(data)
    assert r"\begin{table}" in latex
    assert "NEURO-CIRCT (Ours)" in latex
    assert "87.5\\%" in latex
    assert r"\end{table}" in latex


def test_benchmark_failure_taxonomy():
    tax = ba.get_failure_taxonomy()
    assert "ASSERTION_ERASURE" in tax
    assert "API_HALLUCINATION" in tax
    assert "TRIVIAL_BYPASS" in tax
    assert "CONTEXT_SATURATION" in tax
    assert "LIT_TIMEOUT" in tax
    assert "CEGIS_OVERFITTING" in tax

    latex_tax = ba.generate_failure_taxonomy_latex()
    assert r"\begin{table*}" in latex_tax
    assert "Assertion Erasure" in latex_tax
    assert "tablegen_analyzer.py" in latex_tax
    assert r"\end{table*}" in latex_tax

