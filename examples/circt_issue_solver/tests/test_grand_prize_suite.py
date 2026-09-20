"""Grand Prize Test Suite for NEURO-CIRCT.

Validates:
1. Automated Lit Test Synthesizer (lit_test_synthesizer.py)
2. CIRCT Dialect Invariant Rulebook (dialect_rules.py)
3. Maintainer-Grade PR & Commit Polisher (pr_polish_agent.py)
4. Aesthetic Dashboard Generator (report_dashboard.py)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import lit_test_synthesizer as lts
import dialect_rules as dr
import pr_polish_agent as pr
import report_dashboard as rd


# ---------------------------------------------------------------------------
# 1. Automated Lit Test Synthesizer Tests
# ---------------------------------------------------------------------------

def test_lit_test_synthesizer():
    mlir = """firrtl.circuit "MyTop" {
  firrtl.module @MyTop(out %out: !firrtl.uint<1>) {
    %c = firrtl.constant 1 : !firrtl.uint<1>
    firrtl.strictconnect %out, %c : !firrtl.uint<1>
  }
}"""
    test_src = lts.synthesize_lit_test(
        mlir, failing_pass="lower-firrtl-to-hw", issue_number=7388
    )
    assert "// RUN: circt-opt %s --lower-firrtl-to-hw | FileCheck %s" in test_src
    assert "// CHECK-LABEL: firrtl.module @MyTop" in test_src
    assert "// Regression test for issue #7388" in test_src


def test_lit_test_destination_inference():
    dest = lts.infer_lit_test_destination("lower-firrtl-to-hw", issue_number=7388, circt_src_dir="/workspace/circt")
    assert "test/Conversion/FIRRTLToHW/issue_7388.mlir" in dest.replace("\\", "/")

    dest_hw = lts.infer_lit_test_destination("hw-cleanup", issue_number=999, circt_src_dir="/workspace/circt")
    assert "test/Dialect/HW/issue_999.mlir" in dest_hw.replace("\\", "/")


def test_has_lit_test_in_diff():
    diff_with_test = """--- a/lib/Conversion/Lower.cpp
+++ b/lib/Conversion/Lower.cpp
--- /dev/null
+++ b/test/Dialect/FIRRTL/issue_test.mlir
@@ -0,0 +1,5 @@
"""
    diff_without_test = """--- a/lib/Conversion/Lower.cpp
+++ b/lib/Conversion/Lower.cpp
@@ -10,1 +10,1 @@
"""
    assert lts.has_lit_test_in_diff(diff_with_test) is True
    assert lts.has_lit_test_in_diff(diff_without_test) is False


# ---------------------------------------------------------------------------
# 2. CIRCT Dialect Invariant Rulebook Tests
# ---------------------------------------------------------------------------

def test_dialect_detection():
    assert dr.detect_dialect_from_context(repro_output="firrtl.module @Foo") == "FIRRTL"
    assert dr.detect_dialect_from_context(pass_name="lower-comb-to-smt") == "Comb"
    assert dr.detect_dialect_from_context(code_text="hw.module @Top") == "HW"
    assert dr.detect_dialect_from_context(pass_name="export-verilog") == "SV"


def test_dialect_guidance_formatting():
    guidance = dr.format_dialect_guidance_for_prompt("FIRRTL")
    assert "Strict Connect Invariant" in guidance
    assert "Passive Types" in guidance
    assert "Domain-Specific Dialect Invariant Guidance (FIRRTL)" in guidance


# ---------------------------------------------------------------------------
# 3. Maintainer-Grade PR & Commit Polisher Tests
# ---------------------------------------------------------------------------

def test_commit_message_formatting():
    msg = pr.format_commit_message(
        issue_number=7388,
        dialect="FIRRTLToHW",
        summary="fix memory type crash",
        root_cause="Null type dereference on invalid memory op",
        test_path="test/Conversion/FIRRTLToHW/issue_7388.mlir"
    )
    assert "[FIRRTLToHW] fix memory type crash (fixes #7388)" in msg
    assert "Root Cause:\nNull type dereference" in msg
    assert "Adds durable regression test at `test/Conversion/FIRRTLToHW/issue_7388.mlir`" in msg


def test_pr_body_formatting():
    body = pr.format_pr_body(
        issue_number=7388,
        issue_title="Memory lowering crash",
        root_cause="Missing type check",
        diff_summary="Added null guard before type retrieval",
        test_path="test/Conversion/FIRRTLToHW/issue_7388.mlir",
        formal_verified=True,
        cegis_passed=True
    )
    assert "## Summary of Changes (Fixes #7388)" in body
    assert "Formally Verified" in body
    assert "CEGIS Mutation" in body


def test_gh_pr_command():
    cmd = pr.generate_gh_pr_command("fix/7388", "Fix memory crash", "PR body text")
    assert 'gh pr create --head "fix/7388"' in cmd


# ---------------------------------------------------------------------------
# 4. Aesthetic Dashboard Generator Tests
# ---------------------------------------------------------------------------

def test_dashboard_html_generation(tmp_path):
    out_file = tmp_path / "dashboard.html"
    rd.generate_dashboard_html([], str(out_file))
    assert out_file.exists()
    content = out_file.read_text()
    assert "NEURO-CIRCT" in content
    assert "CHIA Hackathon 2026" in content
    assert "tailwindcss.min.js" in content
    assert "Context Reduction" in content
    assert "Pass@1 Success" in content
    assert "Copy PR Command" in content

    # Also persist to artifact directory
    art_dir = Path("/mnt/c/Users/kudch/.gemini/antigravity/brain/1c82ba39-1cd6-4f2c-8c89-a52aa80ad077")
    if art_dir.exists():
        rd.generate_dashboard_html([], str(art_dir / "dashboard.html"))
