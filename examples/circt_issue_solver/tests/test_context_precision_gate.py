"""Unit tests for the Context-Precision Gate.

Ray-free: only ``context_precision_gate`` (+ ``validation_set``) are imported;
everything that would shell out to circt-opt / circt-reduce / bash is patched at
the ``_safe_run`` boundary, and the LLM step at the injected ``turn_fn``.

These replace the standalone-scaffold control-flow tests (the old
``tests/test_circt_fix_loop.py``): the cascade branch coverage is the same
(fast path / over-budget crash / over-budget miscompile -> stall -> semantic
reduce / verify fallback / verification_failed / no_ir), now exercised through
``run_gate`` + ``gate_for_fix`` (the real seam ``run_issue_remote`` calls) rather
than a parallel ``circt_fix_loop()`` reimplementation.

Run:  python -m pytest chia/examples/circt_issue_solver/tests/ -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import context_precision_gate as g          # noqa: E402
import validation_set as vs                 # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _patch_cascade(monkeypatch, *, slice_res, localize_crash=None,
                   localize_miscompile=None, reduce_res=None, verify=None):
    monkeypatch.setattr(g, "_slice", lambda ir, fp, tb: dict(slice_res))
    monkeypatch.setattr(g, "_run_op_count", lambda ir: len((ir or "").split()))
    monkeypatch.setattr(g, "make_semantic_reduce_tool", lambda **k: None)
    if localize_crash is not None:
        monkeypatch.setattr(g, "_localize_crash",
                            lambda ir, wd, rs="": localize_crash)
    if localize_miscompile is not None:
        monkeypatch.setattr(g, "_localize_miscompile",
                            lambda ir, exp: localize_miscompile)
    if reduce_res is not None:
        monkeypatch.setattr(g, "_reduce",
                            lambda ir, test, wd, tb, timeout=600: dict(reduce_res))
    if verify is not None:
        monkeypatch.setattr(g, "_verify_interestingness", verify)



# ---------------------------------------------------------------------------
# run_gate cascade control flow
# ---------------------------------------------------------------------------

def test_fast_path_skips_localize_and_reduce(monkeypatch, tmp_path):
    _patch_cascade(monkeypatch,
                   slice_res={"sliced_ir": "S", "op_count": 5, "under_budget": True},
                   localize_crash="BOOM", localize_miscompile="BOOM",
                   reduce_res=None,
                   verify=lambda ir, rs, rip: True)
    # _reduce must not be called on the fast path
    monkeypatch.setattr(g, "_reduce", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("reduce called on fast path")))
    r = g.run_gate(ir_dump="raw", failure_type="crash", failure_point="x.op",
                   expected_output=None, repro_script="/x.sh",
                   work_dir=str(tmp_path))
    assert r["status"] == "ok"
    assert r["metrics"]["fast_path"] is True
    assert r["metrics"]["localize_stage"] is None
    assert r["metrics"]["reduce_stalled"] is None
    assert r["payload"]["minimized_ir"] == "S"


def test_over_budget_crash_localizes_then_reduce_converges(monkeypatch, tmp_path):
    _patch_cascade(monkeypatch,
                   slice_res={"sliced_ir": "S", "op_count": 9999, "under_budget": False},
                   localize_crash="TheFailingPass",
                   reduce_res={"reduced_ir": "R", "stalled": False},
                   verify=lambda ir, rs, rip: True)
    r = g.run_gate(ir_dump="raw", failure_type="crash", failure_point="x.op",
                   expected_output=None, repro_script="/x.sh",
                   work_dir=str(tmp_path), repro_input_path=str(tmp_path / "in.mlir"))
    assert r["status"] == "ok"
    assert r["metrics"]["localize_stage"] == "crash"
    assert r["metrics"]["reduce_stalled"] is False
    assert r["metrics"]["semantic_reduce_used"] is False
    assert r["payload"]["implicated_pass"] == "TheFailingPass"
    assert r["payload"]["minimized_ir"] == "R"


def test_over_budget_miscompile_stalls_then_semantic_reduce(monkeypatch, tmp_path):
    _patch_cascade(monkeypatch,
                   slice_res={"sliced_ir": "S", "op_count": 9999, "under_budget": False},
                   localize_miscompile="MiscompilePass",
                   reduce_res={"reduced_ir": "R", "stalled": True},
                   verify=lambda ir, rs, rip: True)
    seen = []

    def turn(phase, prompt, tools):
        seen.append(phase)
        (tmp_path / "reduce_target.mlir").write_text("RR-further")
        return None

    r = g.run_gate(ir_dump="raw", failure_type="miscompilation", failure_point=None,
                   expected_output="expected", repro_script="/x.sh",
                   work_dir=str(tmp_path), repro_input_path=str(tmp_path / "in.mlir"),
                   turn_fn=turn)
    assert r["status"] == "ok"
    assert seen == ["gate_semantic_reduce"]
    assert r["metrics"]["localize_stage"] == "miscompile"
    assert r["metrics"]["reduce_stalled"] is True
    assert r["metrics"]["semantic_reduce_used"] is True
    assert r["payload"]["minimized_ir"] == "RR-further"


def test_stall_without_turn_fn_uses_circt_reduce_output(monkeypatch, tmp_path):
    _patch_cascade(monkeypatch,
                   slice_res={"sliced_ir": "S", "op_count": 9999, "under_budget": False},
                   localize_crash="P",
                   reduce_res={"reduced_ir": "R-stalled", "stalled": True},
                   verify=lambda ir, rs, rip: True)
    r = g.run_gate(ir_dump="raw", failure_type="crash", failure_point="x.op",
                   expected_output=None, repro_script="/x.sh",
                   work_dir=str(tmp_path), turn_fn=None)
    assert r["status"] == "ok"
    assert r["metrics"]["semantic_reduce_used"] is False
    assert r["payload"]["minimized_ir"] == "R-stalled"


def test_verification_falls_back_to_unreduced_slice(monkeypatch, tmp_path):
    seen = []
    _patch_cascade(monkeypatch,
                   slice_res={"sliced_ir": "SLICE", "op_count": 9999, "under_budget": False},
                   localize_crash="P",
                   reduce_res={"reduced_ir": "BAD", "stalled": False},
                   verify=lambda ir, rs, rip: seen.append(ir) or (ir == "SLICE"))
    r = g.run_gate(ir_dump="raw", failure_type="crash", failure_point="x.op",
                   expected_output=None, repro_script="/x.sh", work_dir=str(tmp_path))
    assert r["status"] == "ok"
    assert seen == ["BAD", "SLICE"]
    assert r["payload"]["minimized_ir"] == "SLICE"


def test_verification_failed_when_nothing_reproduces(monkeypatch, tmp_path):
    _patch_cascade(monkeypatch,
                   slice_res={"sliced_ir": "S", "op_count": 9999, "under_budget": False},
                   localize_crash="P",
                   reduce_res={"reduced_ir": "BAD", "stalled": False},
                   verify=lambda ir, rs, rip: False)
    r = g.run_gate(ir_dump="raw", failure_type="crash", failure_point="x.op",
                   expected_output=None, repro_script="/x.sh", work_dir=str(tmp_path))
    assert r["status"] == "verification_failed"
    assert r["payload"] is None
    assert r["gate_prompt_block"] == ""


def test_no_ir_short_circuits(tmp_path):
    r = g.run_gate(ir_dump="", failure_type="crash", failure_point=None,
                   expected_output=None, repro_script="/x.sh", work_dir=str(tmp_path))
    assert r["status"] == "no_ir"
    assert r["payload"] is None


# ---------------------------------------------------------------------------
# gate_for_fix — the seam run_issue_remote calls
# ---------------------------------------------------------------------------

def _cfg(tmp_path, **over):
    c = {"repro_dir": str(tmp_path), "repro_path": str(tmp_path / "repro.sh"),
         "gate_enabled": True, "gate_token_budget": 2000}
    c.update(over)
    return c


def test_gate_for_fix_enabled_runs_cascade(monkeypatch, tmp_path):
    (tmp_path / "input.mlir").write_text("firrtl.circuit {}")
    (tmp_path / "repro.sh").write_text("#!/bin/bash\ncirct-opt input.mlir")
    monkeypatch.setattr(g, "run_gate", lambda **kw: {
        "status": "ok", "payload": {"minimized_ir": "M", "failure_type": "crash"},
        "gate_prompt_block": "<<GATE>>",
        "metrics": dict(g._EMPTY_METRICS, fast_path=True, raw_ir_op_count=3,
                        final_op_count=3)})
    block, outcome = g.gate_for_fix(_cfg(tmp_path), "repro.sh reads input.mlir",
                                    "LLVM ERROR: boom", 1, turn_fn=None)
    assert block == "<<GATE>>"
    assert outcome["gate_enabled"] is True
    assert outcome["status"] == "ok"
    assert outcome["failure_type"] == "crash"
    assert outcome["fast_path"] is True
    # every column db.record_gate.chia_remote reads out of the outcome dict
    # (db._GATE_COLS minus issue_number/created_at) must be present so the
    # INSERT never sees a missing key.
    for col in ("gate_enabled", "status", "failure_type", "raw_ir_op_count",
                "sliced_op_count", "final_op_count", "fast_path", "localize_stage",
                "reduce_stalled", "semantic_reduce_used", "ir_path"):
        assert col in outcome, col


def test_gate_for_fix_disabled_is_baseline(monkeypatch, tmp_path):
    (tmp_path / "input.mlir").write_text("a b c d e")
    monkeypatch.setattr(g, "_run_op_count", lambda ir: len(ir.split()))
    monkeypatch.setattr(g, "run_gate", lambda **kw: (_ for _ in ()).throw(
        AssertionError("run_gate called with gate disabled")))
    block, outcome = g.gate_for_fix(_cfg(tmp_path, gate_enabled=False),
                                    "input.mlir", "wrong output", 1, turn_fn=None)
    assert block == ""
    assert outcome["gate_enabled"] is False
    assert outcome["status"] == "disabled"
    assert outcome["failure_type"] == "miscompilation"
    assert outcome["raw_ir_op_count"] == 5
    assert outcome["final_op_count"] is None


# ---------------------------------------------------------------------------
# classify_failure
# ---------------------------------------------------------------------------

def test_classify_failure_crash_signatures():
    ft, fp = g.classify_failure(
        "LLVM ERROR: x\nsee current operation: %3 = \"firrtl.foo\"()", 1)
    assert ft == "crash"
    assert fp == '%3 = "firrtl.foo"()'


def test_classify_failure_signal_kill_is_crash():
    assert g.classify_failure("terminated", -11)[0] == "crash"


def test_classify_failure_default_is_miscompilation():
    assert g.classify_failure("FileCheck: expected string not found", 1) == \
        ("miscompilation", None)
    assert g.classify_failure("", 1) == ("miscompilation", None)


# ---------------------------------------------------------------------------
# crash-reproducer / verifier-note parsing (formats confirmed from MLIR source)
# ---------------------------------------------------------------------------

def test_parse_failing_pass_local_reproducer_format():
    # mlir/lib/Pass/PassCrashRecovery.cpp formatPassOpReproducerMessage
    err = ("some.mlir:3:8: error: Pipeline failed while executing "
           "`LowerFIRRTLToHW` on 'builtin.module' operation: @Foo: "
           "attempting to use invalidated IR\n")
    assert g._parse_failing_pass(err) == "LowerFIRRTLToHW"


def test_parse_failing_pass_global_reproducer_note_format():
    err = ("error: Failures have been detected while processing an MLIR pass pipeline\n"
           "note: Pipeline failed while executing [`CanonicalizerPass` on 'func.func' "
           "operation: @f, `CSE` on 'func.func' operation]: ...\n")
    assert g._parse_failing_pass(err) == "CanonicalizerPass"


def test_parse_failing_pass_signal():
    assert g._parse_failing_pass(
        "... A signal was caught while processing the MLIR module; marking pass "
        "as failed") == "signal-in-pass"


def test_parse_failing_pass_unknown():
    assert g._parse_failing_pass("nothing useful here") == "unknown-pass"


def test_extract_crash_op_generic_form_note():
    # mlir/lib/IR/Operation.cpp emitError: generic form, single line
    err = ('x.mlir:5:3: error: something bad\n'
           'x.mlir:5:3: note: see current operation: %3 = "firrtl.node"(%a) '
           ': (!firrtl.uint<1>) -> !firrtl.uint<1>\n')
    assert g._extract_crash_op(err) == \
        '%3 = "firrtl.node"(%a) : (!firrtl.uint<1>) -> !firrtl.uint<1>'


def test_extract_crash_op_falls_back_to_reproducer_phrasing():
    err = "error: Pipeline failed while executing `P` on 'hw.module' operation: @m: x"
    assert g._extract_crash_op(err) == "hw.module"


def test_extract_crash_op_none_on_hard_crash():
    assert g._extract_crash_op("LLVM ERROR: out of memory\nStack dump:\n0. ...") is None


def test_localize_crash_extracts_pipeline_from_repro(monkeypatch, tmp_path):
    (tmp_path / "repro.sh").write_text(
        "#!/bin/bash\n"
        "circt-opt --pass-pipeline='builtin.module(firrtl.circuit(firrtl-lower-types))' "
        "$DIR/input.mlir\n")
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return _Proc(1, "", "error: Pipeline failed while executing `LowerTypes` "
                            "on 'firrtl.circuit' operation: x")

    monkeypatch.setattr(g, "_safe_run", fake_run)
    out = g._localize_crash("IR", str(tmp_path), str(tmp_path / "repro.sh"))
    assert out == "LowerTypes"
    assert "--pass-pipeline=builtin.module(firrtl.circuit(firrtl-lower-types))" in seen["cmd"]
    assert "--mlir-pass-pipeline-local-reproducer" in seen["cmd"]


def test_localize_crash_bare_flag_form(monkeypatch, tmp_path):
    (tmp_path / "repro.sh").write_text(
        "#!/bin/bash\ncirct-opt --lower-firrtl-to-hw --export-verilog in.mlir\n")
    monkeypatch.setattr(g, "_safe_run", lambda cmd, **kw: _Proc(
        1, "", "error: Pipeline failed while executing `LowerToHW` on 'x' operation: y"))
    assert g._localize_crash("IR", str(tmp_path), str(tmp_path / "repro.sh")) == "LowerToHW"


def test_localize_crash_no_pipeline_recoverable(tmp_path):
    (tmp_path / "repro.sh").write_text("#!/bin/bash\nfirtool weird.fir\n")
    assert g._localize_crash("IR", str(tmp_path), str(tmp_path / "repro.sh")) == \
        "unknown-pass"


# ---------------------------------------------------------------------------
# find_repro_ir
# ---------------------------------------------------------------------------

def test_find_repro_ir_prefers_named_in_repro(tmp_path):
    (tmp_path / "big.mlir").write_text("x " * 500)
    (tmp_path / "input.fir").write_text("small")
    path, text = g.find_repro_ir(str(tmp_path), "circt-opt input.fir --foo")
    assert os.path.basename(path) == "input.fir"
    assert text == "small"


def test_find_repro_ir_largest_fallback(tmp_path):
    (tmp_path / "a.mlir").write_text("x")
    (tmp_path / "b.mlir").write_text("x " * 100)
    path, _ = g.find_repro_ir(str(tmp_path), "no filename here")
    assert os.path.basename(path) == "b.mlir"


def test_find_repro_ir_empty(tmp_path):
    assert g.find_repro_ir(str(tmp_path)) == ("", "")


# ---------------------------------------------------------------------------
# _make_reduce_test wrapper
# ---------------------------------------------------------------------------

def test_make_reduce_test_wrapper_shape(tmp_path):
    w = g._make_reduce_test("/r/repro.sh", "/circt/.circtissues/in.mlir", str(tmp_path))
    body = open(w).read()
    assert 'cand="${!#}"' in body                       # candidate path is last argv
    assert "cp \"$cand\" '/circt/.circtissues/in.mlir'" in body
    assert "exec bash '/r/repro.sh'" in body
    assert os.access(w, os.X_OK)


# ---------------------------------------------------------------------------
# _run_op_count parsing against a real print-op-count "Readable" sample
# ---------------------------------------------------------------------------

_PRINT_OP_COUNT_SAMPLE = """\
- name: firrtl.circuit
  count: 1
- name: firrtl.module
  count: 2
  operands: { 0: 2 }
- name: firrtl.instance
  count: 5
"""


def test_run_op_count_sums_count_lines(monkeypatch):
    monkeypatch.setattr(g, "_safe_run",
                        lambda *a, **k: _Proc(0, _PRINT_OP_COUNT_SAMPLE, ""))
    assert g._run_op_count("whatever") == 1 + 2 + 5


def test_run_op_count_falls_back_to_token_count(monkeypatch):
    monkeypatch.setattr(g, "_safe_run", lambda *a, **k: _Proc(1, "", "tool not found"))
    assert g._run_op_count("a b c d") == 4


# ---------------------------------------------------------------------------
# _reduce uses the real circt-reduce interface
# ---------------------------------------------------------------------------

def test_reduce_invokes_positional_file_and_test_must_fail(monkeypatch, tmp_path):
    calls = {}

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        # emulate circt-reduce writing the -o target
        oi = cmd.index("-o")
        open(cmd[oi + 1], "w").write("REDUCED")
        return _Proc(0, "", "")

    monkeypatch.setattr(g, "_safe_run", fake_run)
    monkeypatch.setattr(g, "_run_op_count", lambda ir: 1)   # under budget
    out = g._reduce("BIG IR", "/tmp/reduce_test.sh", str(tmp_path), token_budget=2000)
    cmd = calls["cmd"]
    assert cmd[0] == g.CIRCT_REDUCE
    assert cmd[1] == str(tmp_path / "reduce_in.mlir")       # positional file, not "-"
    assert "-" not in cmd[1:3]
    assert "--test" in cmd and "/tmp/reduce_test.sh" in cmd
    assert "--test-must-fail" in cmd
    assert out["reduced_ir"] == "REDUCED"
    assert out["stalled"] is False


# ---------------------------------------------------------------------------
# payload rendering
# ---------------------------------------------------------------------------

def test_payload_and_prompt_block_carry_context():
    p = g.construct_payload("module {}", "crash", "LowerFIRRTLToHW", '%3 = "x"()')
    assert p == {"minimized_ir": "module {}", "failure_type": "crash",
                 "implicated_pass": "LowerFIRRTLToHW", "failure_point": '%3 = "x"()'}
    block = g.payload_to_prompt_block(p)
    assert "Implicated pass / lowering stage: `LowerFIRRTLToHW`" in block
    assert "Operation implicated at the crash: `%3 = \"x\"()`" in block
    assert "```mlir\nmodule {}\n```" in block


def test_payload_to_prompt_block_empty():
    assert g.payload_to_prompt_block({}) == ""
    assert g.payload_to_prompt_block(None) == ""


# ---------------------------------------------------------------------------
# validation_set bucket mapping
# ---------------------------------------------------------------------------

def test_bucket_of_status_mapping():
    assert vs.bucket_of({"status": "not_a_bug"}) == "not_a_bug"
    assert vs.bucket_of({"status": "unclear"}) == "fix_unclear"
    assert vs.bucket_of({"status": "no_repro"}) == "already_fixed_upstream"
    assert vs.bucket_of({"status": "fixed"}) == "pr_merged"
    assert vs.bucket_of({"status": "fixed"}, ["good first issue"]) == \
        "fixed_no_pr_good_first_issue"
    assert vs.bucket_of({"status": "attempted"}) == "attempted"


def test_validation_set_is_the_16_from_table_5():
    assert len(vs.VALIDATION_SET) == 16
    assert set(vs.VALIDATION_SET.values()) <= vs.PAPER_BUCKETS
    assert vs.FAST_SET == (7388, 7949, 10104)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
