"""Context-Precision Gate for the circt_issue_solver flow.

Inserts a **slice -> budget-gate -> localize -> reduce -> semantic-reduce**
cascade between the REPRODUCE and FIX phases of ``run_issue_remote`` so the Fix
agent receives a right-sized, precisely-scoped MLIR context (plus the implicated
pass / lowering stage) instead of whatever raw dump the reporter attached.

Ships to chia-circt workers via ``runtime_env`` ``py_modules`` alongside
``circt_util.py`` / ``issue_task.py`` -- so the same rules apply: **no head-only
imports** (db / triage), **no file reads at import time**, all config arrives in
the ``cfg`` dict / call args.

All programmatic steps run *locally* on the ``circt`` worker that
``run_issue_remote`` is already pinned to (it holds the whole ``circt`` slot),
exactly as the docs case study says the Gate's programmatic work should. They are
therefore plain functions -- NOT ``@ChiaFunction`` -- unlike ``circt_util``'s
decorate-everything style: nothing here is ever dispatched as its own cluster
task, and keeping them plain lets this module import and unit-test without ray.
The single LLM step (the ``semantic_reduce`` fallback) is dispatched through a
caller-supplied ``turn_fn`` -- ``run_issue_remote._turn`` -- so backend
selection, ``.options(resources={"llm": 1.0})`` pinning, and transcript logging
stay in one place.

-------------------------------------------------------------------------------
Design reasoning (carried over verbatim from the standalone
``src/circt_fix_loop.py`` scaffold this replaces -- it survives regardless of
what the real APIs turned out to be):

  * **Slice always runs first** and is near-free; if the SSA backward slice is
    already under the token budget, skip straight to Fix (the "fast path").
  * **Localize** is cheap for crashes (MLIR's built-in pass-manager crash
    reproducer) and a pipeline bisection for miscompiles (tool exits 0, output
    is just wrong).
  * **Reduce** hands off to ``circt-reduce``; only if that STALLS above budget
    does the LLM-guided **semantic reducer** run -- structure/type-preserving
    edits, never blind deletion (per DuoReduce-style findings that real MLIR
    compilers need joint IR+pass reduction).
  * **verify_interestingness** falls back to the un-reduced slice rather than
    hard-failing the whole issue if a minimized candidate stops reproducing.

-------------------------------------------------------------------------------
Phase-0 corrections applied vs. the scaffold (each has a file:line reason in the
project README / Phase-0 report):

  * ``resources={"circt": 1}``  (scaffold had ``circt_env: 1.0`` -- no such
    resource; the real ``cluster.yaml`` advertises ``circt``).  Only relevant
    for the ``SemanticReduceTool`` MCP server's ``task_options`` here.
  * ``ClaudeCodeQueryResult.result`` (scaffold read ``.text`` -- no such
    attribute; ``QueryResult`` exposes ``.result`` / ``.stream_result`` /
    ``.stderr`` / ``.success``).  The Gate's LLM step reads the file back rather
    than the transcript, so this mostly bites the *other* scaffold nodes, but
    ``_semantic_reduce`` still honors it if it ever inspects the turn result.
  * LLM dispatch via ``turn_fn`` (== ``run_issue_remote._turn``), which itself
    does ``llm.prompt.options(resources={"llm": 1.0}).chia_remote(llm, prompt,
    tools)`` -- matches ``issue_task.py``.
  * ``ChiaTool`` subclasses take an explicit ``name`` and call
    ``super().__post_init__()`` -- the real ``chia.base.tools.ChiaTool``
    contract, not the test stub's ``super().__init__(**kwargs)``.

-------------------------------------------------------------------------------
circt-slice DECISION (Phase-1): there is no ``circt-slice`` tool and no
SSA-slice pass in CIRCT or a release MLIR build at firtool-1.148.0.  We SHIP
without a standalone static slice (``SLICE_IMPL="agent"``): ``circt-reduce``,
which is a delta-debugger driven by the interestingness test, already removes
everything not needed to keep the bug reproducing -- it subsumes a backward
slice for the payload-minimization goal.  The first stage is now just a
``print-op-count`` budget gate on the raw repro (the "fast path" = raw repro
already small enough to skip Reduce).  ``SLICE_IMPL`` keeps ``"pass"`` /
``"cli"`` as upgrade hooks if a real slice ever lands.

STILL OPEN -- needs a real CIRCT checkout / cluster to close (do NOT treat as
confirmed):

  * ``_run_op_count`` (real pass ``print-op-count``, sum of ``count:`` lines)
    and ``_reduce`` (real ``circt-reduce`` positional-file + ``--test-must-fail``
    + wrapper) are corrected from source but UNRUN.  Verify the exact output on
    the pinned tree; commands are in each helper's docstring.
  * ``_parse_failing_pass`` / ``_extract_crash_op`` regexes against the real
    ``--mlir-pass-pipeline-crash-reproducer`` stderr are still INFERRED.
  * ``_passes_within_stage`` / ``_MISCOMPILE_MACRO_STAGES`` pass-flag names are
    UNVERIFIED against the real lowering pipeline.
"""
from __future__ import annotations

import glob
import os
import re
import subprocess


# ---------------------------------------------------------------------------
# Hardened subprocess wrapper (ported from the scaffold -- every external call
# in this module goes through it, so a timeout / missing binary / OS error
# degrades into a CompletedProcess with returncode -1 instead of an uncaught
# exception killing the per-issue run).
# ---------------------------------------------------------------------------

def _safe_run(cmd, **kwargs) -> subprocess.CompletedProcess:
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    try:
        return subprocess.run(cmd, **kwargs)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(
            cmd, -1, stdout=(e.stdout or ""),
            stderr=f"TIMEOUT after {e.timeout}s: {e}")
    except FileNotFoundError as e:
        return subprocess.CompletedProcess(cmd, -1, stdout="",
                                           stderr=f"BINARY NOT FOUND: {e}")
    except OSError as e:
        return subprocess.CompletedProcess(cmd, -1, stdout="",
                                           stderr=f"OS ERROR: {e}")


# ---------------------------------------------------------------------------
# Tool names / paths.  circt_util._CIRCT_SOURCE_TREE is /workspace/circt in the
# image; the source-tree build/bin is first on PATH there, so bare tool names
# resolve to the rebuilt binaries.  Kept overridable for tests / non-default
# layouts.
# ---------------------------------------------------------------------------

CIRCT_OPT = os.environ.get("CIRCT_OPT", "circt-opt")
CIRCT_REDUCE = os.environ.get("CIRCT_REDUCE", "circt-reduce")
CIRCT_SLICE = os.environ.get("CIRCT_SLICE", "circt-slice")

#: How the first ("slice") stage narrows the raw repro IR.
#:
#: SHIPPED: ``"agent"`` -- there is no ``circt-slice`` tool and no SSA-slice
#: pass in CIRCT / a release MLIR build (checked against firtool-1.148.0:
#: tools/ has no circt-slice; include/circt/Transforms/Passes.td has no slice
#: pass; mlir::getBackwardSlice is a library, its test pass is not in release
#: builds).  So the standalone static slice is dropped: ``_slice`` just runs
#: the real ``print-op-count`` pass as a budget gate on the raw repro, and
#: ``circt-reduce`` (a delta-debugger driven by the interestingness test, which
#: already removes everything not needed to keep the bug reproducing) does the
#: actual narrowing in the Reduce stage.
#:
#: Upgrade paths, if a real slice ever lands:
#:   "pass" -> express the backward slice as a circt-opt --pass-pipeline
#:             (set CPG_SLICE_PIPELINE); buildable via CHIA's own
#:             rebuild_circt_opt_with_custom_pass() -- no image rebuild.
#:   "cli"  -> shell out to a real ``circt-slice`` binary (new tools/ target +
#:             a chia-circt-build image layer).
SLICE_IMPL = os.environ.get("CPG_SLICE_IMPL", "agent")

DEFAULT_TOKEN_BUDGET = 2000

_MISCOMPILE_MACRO_STAGES = ["-lower-firrtl-to-hw", "-lower-hw-to-sv", "-export-verilog"]


# ---------------------------------------------------------------------------
# Output parsing (INFERRED formats -- see module docstring "STILL OPEN")
# ---------------------------------------------------------------------------

def _run_op_count(ir: str) -> int:
    """Operation count for *ir* via CIRCT's real ``print-op-count`` pass.

    CONFIRMED against source at firtool-1.148.0
    (include/circt/Transforms/Passes.td: ``def PrintOpCount : Pass<"print-op-count">``;
    lib/Transforms/PrintOpCount.cpp):
      * default "Readable" format writes, to ``llvm::outs()`` (stdout), blocks of

            - name: <op>
              count: <N>
              <optional operand-count breakdown>

      * there is NO grand-total line -- sum the ``count:`` values.
      * the pass echoes the (unchanged) IR too, so ``-o <devnull>`` keeps stdout
        to just the report.

    Fallbacks (kept so the cascade always gets a number): a legacy
    "<n> <op-name>" table, then a whitespace-token estimate.
    """
    proc = _safe_run(
        [CIRCT_OPT, "-o", os.devnull, "--pass-pipeline=builtin.module(print-op-count)", "-"],
        input=ir)
    blob = (proc.stdout or "") + "\n" + (proc.stderr or "")
    counts = [int(x) for x in re.findall(r"^\s*count:\s*(\d+)\s*$", blob, re.MULTILINE)]
    if counts:
        return sum(counts)
    m = re.search(r"[Tt]otal\D+(\d+)", blob)
    if m:
        return int(m.group(1))
    counts = [int(x) for x in re.findall(r"^\s*(\d+)\s+[A-Za-z_.]+\s*$", blob, re.MULTILINE)]
    if counts:
        return sum(counts)
    return len(ir.split())


def _parse_failing_pass(stderr: str) -> str:
    """Pull the implicated pass name out of a pass-manager crash-reproducer run.

    CONFIRMED against MLIR source (mlir/lib/Pass/PassCrashRecovery.cpp):
      * local-reproducer mode (what _localize_crash requests) emits
            error: Pipeline failed while executing `<PassName>` on '<op>' operation[: @<sym>]: <desc>
      * global mode emits
            error: Failures have been detected while processing an MLIR pass pipeline
            note:  Pipeline failed while executing [`<PassA>` on '<op>' operation, `<PassB>` ...]: <desc>
      * a caught signal (segv/abort under CrashRecoveryContext) emits
            ... A signal was caught while processing the MLIR module<desc>; marking pass as failed
        with no pass name -> return "signal-in-pass".
      * <PassName> is the pass's C++ display name (Pass::getName()), e.g.
        "LowerFIRRTLToHW", NOT the -flag form.
    NOTE: the emitted reproducer .mlir has NO header comment (config rides in
    resource attributes) -- do not try to parse it.
    """
    m = re.search(r"Pipeline failed while executing[^`\n]*`([^`]+)`", stderr)
    if m:
        return m.group(1).strip()
    if "A signal was caught while processing the MLIR module" in stderr:
        return "signal-in-pass"
    # Older/other surfaces, kept as a fallback.
    for pat in (r"Pass ['`\"](.+?)['`\"] failed",
                r"while running pass ['`\"]?([\w-]+)"):
        m = re.search(pat, stderr)
        if m:
            return m.group(1)
    return "unknown-pass"


def _extract_crash_op(stderr: str) -> str | None:
    """The operation MLIR was processing when the diagnostic fired.

    CONFIRMED against MLIR source (mlir/lib/IR/Operation.cpp, Operation::emitError):

        diag.attachNote(getLoc())
            .append("see current operation: ")
            .appendOp(*this, OpPrintingFlags().printGenericOpForm());

    guarded by ``shouldPrintOpOnDiagnostic()`` (default ON).  So on a verifier
    error / emitError (the common CIRCT "pass produced invalid IR" case) stderr
    carries a single-line, GENERIC-form note:
        note: see current operation: %0 = "firrtl.foo"(%a) : (i1) -> i1
    A hard crash (LLVM ERROR / signal) has no such line -> return None and the
    caller leaves failure_point unset.  Falls back to the crash-recovery
    "`Pass` on '<op>' operation" phrasing when present.
    """
    m = re.search(r"see current operation:\s*(.+)", stderr)
    if m:
        return m.group(1).strip()
    m = re.search(r"Pipeline failed while executing[^\n]*?on '([^']+)' operation", stderr)
    if m:
        return m.group(1).strip()
    return None


# ---------------------------------------------------------------------------
# Stage groupings for miscompile localization.
#
# NOT WIRED in the current integration: run_gate is called with
# expected_output=None (the reproduce phase doesn't capture a FileCheck
# expected-correct output), so _localize_miscompile / _bisect_pipeline never
# run -- miscompile issues take the `localize_stage = "skipped"` branch and go
# straight to circt-reduce.  Wiring this needs a reproduce.md change to emit the
# expected output; until then these pass names are a documented starting point,
# UNVERIFIED against CIRCT's real FIRRTL/HW pipeline on firtool-1.148.0 (flag
# names + the textual-pipeline "firrtl.circuit(...)" nesting both differ).
# ---------------------------------------------------------------------------

_STAGE_PASSES = {
    "-lower-firrtl-to-hw": [
        "-firrtl-lower-types", "-firrtl-expand-whens", "-firrtl-infer-widths",
        "-firrtl-imconstprop", "-firrtl-imdeadcodeelim", "-lower-firrtl-to-hw",
    ],
    "-lower-hw-to-sv": ["-hw-cleanup", "-hw-legalize-modules", "-lower-hw-to-sv"],
    "-export-verilog": ["-prepare-for-emission", "-export-verilog"],
}


def _passes_within_stage(stage: str) -> list:
    return _STAGE_PASSES.get(stage, [stage])


def _bisect_pipeline(ir: str, pipeline: list, expected: str) -> str:
    """Return the first pass in *pipeline* after which circt-opt's output stops
    matching *expected* -- i.e. the pass that introduced the miscompile."""
    lo, hi = 0, len(pipeline)
    while lo < hi:
        mid = (lo + hi) // 2
        out = _safe_run(
            [CIRCT_OPT, "-", f"--pass-pipeline={','.join(pipeline[:mid])}"],
            input=ir).stdout
        lo, hi = (mid + 1, hi) if out == expected else (lo, mid)
    return pipeline[lo - 1] if lo > 0 else (pipeline[0] if pipeline else "")


# ---------------------------------------------------------------------------
# Cascade steps
# ---------------------------------------------------------------------------

def _slice(ir_dump: str, failure_point: str | None, token_budget: int) -> dict:
    """First cascade stage: narrow *ir_dump* toward *failure_point*.

    Returns ``{sliced_ir, op_count, under_budget[, slice_error]}``.  On any
    failure the ORIGINAL ir is returned with ``under_budget=False`` so the
    cascade proceeds to Localize/Reduce rather than aborting.

    SHIPPED (``SLICE_IMPL="agent"``): NO static slice -- ``circt-slice`` and an
    SSA-slice pass do not exist (see module docstring).  This is just a
    ``print-op-count`` budget gate on the raw repro; ``circt-reduce`` in the
    Reduce stage does the real narrowing.  ``"pass"`` / ``"cli"`` are upgrade
    hooks.
    """
    if not ir_dump:
        return {"sliced_ir": "", "op_count": 0, "under_budget": True}

    if SLICE_IMPL == "agent":
        # No static slice; circt-reduce (+ semantic-reduce) does all narrowing.
        oc = _run_op_count(ir_dump)
        return {"sliced_ir": ir_dump, "op_count": oc, "under_budget": oc <= token_budget}

    if SLICE_IMPL == "pass":
        # Express the backward slice as a circt-opt pass pipeline.  The concrete
        # pipeline string is filled in once Phase 1 confirms a usable pass.
        pipeline = os.environ.get("CPG_SLICE_PIPELINE", "")
        if not pipeline:
            oc = _run_op_count(ir_dump)
            return {"sliced_ir": ir_dump, "op_count": oc,
                    "under_budget": oc <= token_budget,
                    "slice_error": "SLICE_IMPL=pass but CPG_SLICE_PIPELINE unset"}
        proc = _safe_run([CIRCT_OPT, "-", f"--pass-pipeline={pipeline}"], input=ir_dump)
        if proc.returncode != 0:
            oc = _run_op_count(ir_dump)
            return {"sliced_ir": ir_dump, "op_count": oc, "under_budget": False,
                    "slice_error": proc.stderr[-2000:]}
        oc = _run_op_count(proc.stdout)
        return {"sliced_ir": proc.stdout, "op_count": oc, "under_budget": oc <= token_budget}

    # SLICE_IMPL == "cli": shell out to a real circt-slice binary.
    cmd = [CIRCT_SLICE, "-"]
    if failure_point:
        cmd = [CIRCT_SLICE, "--failure-op", failure_point, "-"]
    proc = _safe_run(cmd, input=ir_dump)
    if proc.returncode != 0:
        oc = _run_op_count(ir_dump)
        return {"sliced_ir": ir_dump, "op_count": oc, "under_budget": False,
                "slice_error": proc.stderr[-2000:]}
    oc = _run_op_count(proc.stdout)
    return {"sliced_ir": proc.stdout, "op_count": oc, "under_budget": oc <= token_budget}


_PIPELINE_RE = re.compile(
    r"""--?pass-pipeline\s*[=\s]\s*['"]?([^'"\n]+?)['"]?(?:\s|$)"""
    r"""|(?:^|\s)(--[a-z][\w-]*(?:\s+--[a-z][\w-]*)*)\s+[^\s|]*\.mlir""",
    re.MULTILINE)


def _localize_crash(ir: str, work_dir: str, repro_script: str = "") -> str:
    """Name the pass that crashes on *ir*.

    The crash reproducer only fires if circt-opt actually runs the failing
    pipeline -- which lives in repro.sh, not here.  So: read the
    ``--pass-pipeline=...`` (or a run of ``--foo-pass`` flags before the input)
    out of *repro_script*, re-run circt-opt with it + the local crash
    reproducer, and parse the ``Pipeline failed while executing `Pass` ...``
    line (see _parse_failing_pass).  Best-effort: ``"unknown-pass"`` when the
    pipeline can't be recovered or the re-run doesn't crash -- non-fatal, the
    Fix agent still gets the reduced IR.
    """
    pipeline = ""
    if repro_script:
        try:
            txt = open(repro_script, errors="replace").read()
        except OSError:
            txt = ""
        m = _PIPELINE_RE.search(txt)
        if m:
            pipeline = (m.group(1) or m.group(2) or "").strip()
    if not pipeline:
        return "unknown-pass"

    crash_repro_path = os.path.join(work_dir, "crash_repro.mlir")
    # group 1 == a pipeline string (wrap in --pass-pipeline=); group 2 == a run
    # of bare --foo-pass flags (pass through as-is).
    pipe_args = pipeline.split() if pipeline.startswith("--") \
        else [f"--pass-pipeline={pipeline}"]
    proc = _safe_run(
        [CIRCT_OPT, "-", *pipe_args,
         f"--mlir-pass-pipeline-crash-reproducer={crash_repro_path}",
         "--mlir-pass-pipeline-local-reproducer"],
        input=ir)
    return _parse_failing_pass(proc.stderr or "")


def _localize_miscompile(ir: str, expected_output: str) -> str:
    """Bisect the lowering pipeline to the pass that first diverges from
    *expected_output*."""
    stage = _bisect_pipeline(ir, _MISCOMPILE_MACRO_STAGES, expected_output)
    return _bisect_pipeline(ir, _passes_within_stage(stage), expected_output)


def _reduce(ir: str, interestingness_test: str, work_dir: str, token_budget: int,
            timeout: int = 600) -> dict:
    """Hand *ir* to ``circt-reduce`` with *interestingness_test* as the test.
    Returns ``{reduced_ir, stalled}`` where ``stalled`` means the minimized
    result is still over budget.

    CONFIRMED against source at firtool-1.148.0
    (tools/circt-reduce/circt-reduce.cpp, lib/Reduce/Tester.cpp):
      * input is a REQUIRED POSITIONAL FILENAME -- not stdin ``-``.
      * ``--test <cmd>`` is required; extra fixed args via repeated ``--test-arg``.
      * the test is invoked as ``<cmd> <test-args...> <candidate.mlir>`` -- i.e.
        circt-reduce writes each candidate to a temp file and appends its PATH
        as the final argv.  So *interestingness_test* MUST accept the IR file as
        its last positional argument (``$1``), unlike the repro.sh contract
        which runs a hard-coded input.  ``run_gate`` synthesizes a wrapper for
        this (see ``_make_reduce_test``).
      * interesting == ``result == 0`` by default; ``--test-must-fail`` flips it
        to ``result > 0``.  Our wrapper exits non-zero while the bug is present,
        so we pass ``--test-must-fail``.
      * ``-o <file>`` writes the reduced case (default stdout); ``--keep-best``
        keeps overwriting with better reductions.
    """
    in_path = os.path.join(work_dir, "reduce_in.mlir")
    out_path = os.path.join(work_dir, "reduce_out.mlir")
    with open(in_path, "w") as f:
        f.write(ir)
    proc = _safe_run(
        [CIRCT_REDUCE, in_path, "--test", interestingness_test,
         "--test-must-fail", "--keep-best", "-o", out_path],
        timeout=timeout)
    reduced = ir
    if os.path.exists(out_path):
        try:
            txt = open(out_path, errors="replace").read()
            if txt.strip():
                reduced = txt
        except OSError:
            pass
    elif proc.returncode == 0 and (proc.stdout or "").strip():
        reduced = proc.stdout
    return {"reduced_ir": reduced, "stalled": _run_op_count(reduced) > token_budget}


def _semantic_reduce(ir: str, repro_script: str, work_dir: str, cfg: dict,
                     turn_fn) -> str:
    """LLM-guided reduction fallback when ``circt-reduce`` stalls above budget.

    *turn_fn* is ``run_issue_remote._turn`` -- ``turn_fn(phase, prompt, tools)``.
    The agent edits ``<work_dir>/reduce_target.mlir`` in place and must call the
    interestingness checks after every edit.  Returns the reduced IR text.
    """
    reduce_target = os.path.join(work_dir, "reduce_target.mlir")
    with open(reduce_target, "w") as f:
        f.write(ir)

    prompt = cfg.get("gate_semantic_reduce_prompt") or _DEFAULT_SEMANTIC_REDUCE_PROMPT
    prompt = prompt.replace("$reduce_target", reduce_target).replace(
        "$repro_script", repro_script)

    tool = None
    try:
        tool = make_semantic_reduce_tool(
            name=f"cpg_reduce_{os.path.basename(work_dir)}",
            ir_path=reduce_target, repro_script=repro_script,
            task_options={"resources": {"circt": 1}})
    except Exception:
        # ray / mcp not importable (unit test, or non-cluster) -- the agent can
        # still edit the file + run the repro through whatever bash tool the
        # caller's turn_fn already exposes.
        tool = None

    try:
        turn_fn("gate_semantic_reduce", prompt, [tool] if tool is not None else [])
    finally:
        if tool is not None:
            try:
                tool.stop()
            except Exception:
                pass
    try:
        return open(reduce_target).read()
    except OSError:
        return ir


_DEFAULT_SEMANTIC_REDUCE_PROMPT = (
    "The MLIR at $reduce_target still exceeds the Fix agent's context budget "
    "after circt-reduce. Shrink it further using ONLY structure- and "
    "type-preserving edits -- rename/renumber SSA values, drop operations and "
    "operands that are provably irrelevant to the failure, collapse wide "
    "constants -- never blind text deletion that breaks the IR. After EVERY "
    "edit, call the verify_ir_valid and still_reproduces_bug tools; keep an "
    "edit only if both still pass. Stop as soon as the IR is minimal and both "
    "checks pass. Write the final IR back to $reduce_target. The interestingness "
    "test is $repro_script (exits non-zero while the bug is present)."
)


# ---------------------------------------------------------------------------
# verify + package (ported from VerifyAndPackage)
# ---------------------------------------------------------------------------

def _make_reduce_test(repro_script: str, repro_input_path: str,
                      work_dir: str) -> str:
    """Synthesize a circt-reduce ``--test`` wrapper.

    circt-reduce appends each candidate's file path as the last argv; the
    reproduce phase's ``repro.sh`` instead runs a hard-coded input.  This
    wrapper copies the candidate ($1, or $-last) over the repro input, runs
    ``repro.sh``, and forwards its exit code -- so ``circt-reduce
    --test-must-fail`` keeps candidates that still reproduce.  Returns the
    wrapper's path.
    """
    wrapper = os.path.join(work_dir, "reduce_test.sh")
    with open(wrapper, "w") as f:
        f.write(
            "#!/usr/bin/env bash\n"
            "# auto-generated by context_precision_gate._make_reduce_test\n"
            "set -u\n"
            'cand="${!#}"           # circt-reduce appends the candidate path last\n'
            f'cp "$cand" {repro_input_path!r}\n'
            f'exec bash {repro_script!r}\n')
    os.chmod(wrapper, 0o755)
    return wrapper


def _verify_interestingness(ir: str, repro_script: str, repro_input_path: str) -> bool:
    """True iff *ir*, written to the repro's real input file, still makes
    *repro_script* exit non-zero (bug still reproduces)."""
    if repro_input_path:
        try:
            with open(repro_input_path, "w") as f:
                f.write(ir)
        except OSError:
            pass
    return _safe_run(["bash", repro_script]).returncode != 0


_CRASH_SIGNS = ("LLVM ERROR", "PLEASE submit a bug report", "Assertion",
                "UNREACHABLE", "Segmentation fault", "stack dump")


def classify_failure(log_tail: str, exit_code: int) -> tuple[str, str | None]:
    """Classify a reproduced failure from the repro run's combined output.

    ``("crash", <failing op or None>)`` when the output carries a crash/assert
    signature or the process was signal-killed (exit_code < 0); otherwise
    ``("miscompilation", None)`` -- the tool exited non-zero on wrong output
    (e.g. a FileCheck mismatch inside repro.sh).  Ported from the scaffold's
    ReproduceIssueAgent._classify_failure.
    """
    blob = log_tail or ""
    crashed = exit_code < 0 or any(s in blob for s in _CRASH_SIGNS)
    if crashed:
        return "crash", _extract_crash_op(blob)
    return "miscompilation", None


def construct_payload(ir: str, failure_type: str,
                      implicated_pass: str | None = None,
                      failure_point: str | None = None) -> dict:
    """The object handed to the Fix phase in place of the raw repro dump."""
    payload = {"minimized_ir": ir, "failure_type": failure_type}
    if implicated_pass:
        payload["implicated_pass"] = implicated_pass
    if failure_point:
        payload["failure_point"] = failure_point
    return payload


def payload_to_prompt_block(payload: dict) -> str:
    """Render *payload* as the ``$gate`` block spliced into the fix prompt."""
    if not payload:
        return ""
    lines = ["## Context-Precision Gate output",
             "",
             "The reproduction input below has been reduced (circt-reduce, then "
             "an LLM structure-preserving pass if it stalled) to near-minimal "
             "IR that still triggers the bug. Work from THIS, not the raw issue "
             "attachment.",
             ""]
    if payload.get("implicated_pass"):
        lines += [f"- Implicated pass / lowering stage: `{payload['implicated_pass']}`"]
    if payload.get("failure_point"):
        lines += [f"- Operation implicated at the crash: `{payload['failure_point']}`"]
    lines += [f"- Failure type: {payload.get('failure_type', 'unknown')}",
              "",
              "```mlir",
              payload.get("minimized_ir", "").rstrip(),
              "```"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Repro-input discovery
# ---------------------------------------------------------------------------

_IR_SUFFIXES = (".mlir", ".fir", ".ll", ".sv")


def find_repro_ir(repro_dir: str, repro_text: str = "") -> tuple[str, str]:
    """Locate the reproduction's input IR under *repro_dir*.

    Prefers a file *named* in *repro_text* (the repro.sh contents); else the
    largest IR-suffixed file that is not the script itself.  Returns
    ``(path, text)`` -- ``("", "")`` if nothing looks like IR.
    """
    candidates = []
    for suf in _IR_SUFFIXES:
        candidates += glob.glob(os.path.join(repro_dir, "**", f"*{suf}"),
                                recursive=True)
    candidates = [c for c in candidates if os.path.isfile(c)]
    if not candidates:
        return "", ""

    if repro_text:
        for c in candidates:
            if os.path.basename(c) in repro_text:
                try:
                    return c, open(c, errors="replace").read()
                except OSError:
                    pass
    biggest = max(candidates, key=lambda p: os.path.getsize(p))
    try:
        return biggest, open(biggest, errors="replace").read()
    except OSError:
        return "", ""


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def run_gate(*, ir_dump: str, failure_type: str, failure_point: str | None,
             expected_output: str | None, repro_script: str, work_dir: str,
             repro_input_path: str = "", cfg: dict | None = None, turn_fn=None,
             token_budget: int = DEFAULT_TOKEN_BUDGET) -> dict:
    """Run the full cascade and return a result dict:

        {
          "status": "ok" | "verification_failed" | "no_ir",
          "payload": {minimized_ir, failure_type[, implicated_pass]} | None,
          "gate_prompt_block": str,              # "" unless status == "ok"
          "metrics": {                           # flattened into db.record_gate
             raw_ir_op_count, sliced_op_count, fast_path, localize_stage,
             reduce_stalled, semantic_reduce_used, final_op_count,
          },
        }

    Cascade: budget-gate the raw repro -> if under budget, done (fast path) ->
    else localize (crash reproducer / pipeline bisect) + circt-reduce -> if
    reduce stalled over budget, LLM semantic-reduce -> verify interestingness,
    falling back to the un-reduced input if a candidate stopped reproducing,
    then package. (This is the design from the accepted proposal / the removed
    src/circt_fix_loop.py scaffold, minus the static slice -- see module docstring.)
    """
    cfg = cfg or {}
    os.makedirs(work_dir, exist_ok=True)
    metrics = {
        "raw_ir_op_count": None, "sliced_op_count": None, "fast_path": None,
        "localize_stage": None, "reduce_stalled": None,
        "semantic_reduce_used": False, "final_op_count": None,
    }

    if not ir_dump or not ir_dump.strip():
        return {"status": "no_ir", "payload": None, "gate_prompt_block": "",
                "metrics": metrics}

    metrics["raw_ir_op_count"] = _run_op_count(ir_dump)

    slice_result = _slice(ir_dump, failure_point, token_budget)
    sliced_ir = slice_result["sliced_ir"]
    metrics["sliced_op_count"] = slice_result["op_count"]
    metrics["fast_path"] = bool(slice_result["under_budget"])

    implicated_pass = None
    if slice_result["under_budget"]:
        payload_ir = sliced_ir
    else:
        if failure_type == "crash":
            metrics["localize_stage"] = "crash"
            implicated_pass = _localize_crash(sliced_ir, work_dir, repro_script)
        elif failure_type == "miscompilation" and expected_output:
            metrics["localize_stage"] = "miscompile"
            implicated_pass = _localize_miscompile(sliced_ir, expected_output)
        else:
            metrics["localize_stage"] = "skipped"

        reduce_test = _make_reduce_test(repro_script, repro_input_path, work_dir) \
            if repro_input_path else repro_script
        reduce_result = _reduce(sliced_ir, reduce_test, work_dir, token_budget)
        metrics["reduce_stalled"] = bool(reduce_result["stalled"])
        if reduce_result["stalled"] and turn_fn is not None:
            metrics["semantic_reduce_used"] = True
            payload_ir = _semantic_reduce(reduce_result["reduced_ir"],
                                          repro_script, work_dir, cfg, turn_fn)
        else:
            payload_ir = reduce_result["reduced_ir"]

    ok = _verify_interestingness(payload_ir, repro_script, repro_input_path)
    if not ok:
        # Minimized IR stopped reproducing -- fall back to the un-reduced slice
        # rather than failing the whole issue on one over-eager reduction.
        payload_ir = sliced_ir
        ok = _verify_interestingness(payload_ir, repro_script, repro_input_path)
        if not ok:
            return {"status": "verification_failed", "payload": None,
                    "gate_prompt_block": "", "metrics": metrics}

    metrics["final_op_count"] = _run_op_count(payload_ir)
    payload = construct_payload(payload_ir, failure_type, implicated_pass,
                                failure_point)
    return {"status": "ok", "payload": payload,
            "gate_prompt_block": payload_to_prompt_block(payload),
            "metrics": metrics}


# ---------------------------------------------------------------------------
# The seam: what run_issue_remote calls between REPRODUCE and FIX.
# Kept here (not inline in issue_task.py) so it is testable without ray -- it
# takes the repro run's outputs + an injected turn_fn and returns exactly what
# the FIX turn needs.
# ---------------------------------------------------------------------------

_EMPTY_METRICS = {
    "raw_ir_op_count": None, "sliced_op_count": None, "fast_path": None,
    "localize_stage": None, "reduce_stalled": None,
    "semantic_reduce_used": False, "final_op_count": None,
}


def gate_for_fix(cfg: dict, repro_text: str, repro_log_tail: str,
                 repro_exit_code: int, turn_fn) -> tuple[str, dict]:
    """Run the Gate for one issue's reproduced failure.

    Returns ``(gate_block, gate_outcome)``:
      * ``gate_block`` -- the ``$gate`` text spliced into the fix prompt
        (``""`` on any miss / when disabled, so FIX runs exactly as before).
      * ``gate_outcome`` -- one flat dict for ``db.record_gate`` (always has
        ``gate_enabled`` / ``status`` / ``failure_type`` / the metric keys).

    ``cfg["gate_enabled"]`` False == the ungated §5.5 baseline: no cascade,
    but still measure ``raw_ir_op_count`` so the comparison has both sides.
    """
    repro_dir = cfg["repro_dir"]
    ir_path, ir_text = find_repro_ir(repro_dir, repro_text)
    ftype, fpoint = classify_failure(repro_log_tail, repro_exit_code)

    if not cfg.get("gate_enabled", True):
        return "", {"gate_enabled": False, "failure_type": ftype,
                    "ir_path": ir_path, "status": "disabled",
                    **_EMPTY_METRICS,
                    "raw_ir_op_count": _run_op_count(ir_text) if ir_text else None}

    res = run_gate(
        ir_dump=ir_text, failure_type=ftype, failure_point=fpoint,
        expected_output=None, repro_script=cfg["repro_path"],
        repro_input_path=ir_path,
        work_dir=os.path.join(repro_dir, "_gate"),
        cfg=cfg, turn_fn=turn_fn,
        token_budget=cfg.get("gate_token_budget", DEFAULT_TOKEN_BUDGET))
    outcome = {"gate_enabled": True, "failure_type": ftype,
               "ir_path": ir_path, "status": res["status"], **res["metrics"]}
    return res.get("gate_prompt_block", ""), outcome


# ---------------------------------------------------------------------------
# MCP tool for the semantic-reduce agent (ported from SemanticReduceTool,
# rebased on the real chia.base.tools.ChiaTool contract:
#   - explicit `name` positional
#   - register via self.mcp.add_tool(self.method, name=f"{name}_...")
#   - call super().__post_init__() to start the server actor
# ).
#
# Built by a factory so `from chia.base.tools.ChiaTool import ChiaTool` (which
# pulls in ray + mcp) is deferred to call time -- this module then imports and
# unit-tests without a cluster.  issue_task.py already uses this deferred-import
# pattern for BuildTool / LitTool.
# ---------------------------------------------------------------------------

def make_semantic_reduce_tool(*, name: str, ir_path: str, repro_script: str,
                              task_options: dict | None = None):
    """Return a live ``SemanticReduceTool`` MCP server pinned via *task_options*.

    Tools registered: ``<name>_read_ir`` / ``<name>_write_ir`` /
    ``<name>_verify_valid`` / ``<name>_still_reproduces`` -- crisp bool signals
    instead of bash-output scraping (same rationale as ``BuildTool`` /
    ``LitTool`` in ``chia.chipyard.circt``).
    """
    from chia.base.tools.ChiaTool import ChiaTool

    class SemanticReduceTool(ChiaTool):
        def __init__(self):
            super().__init__(name, task_options=task_options)
            self._ir_path = ir_path
            self._repro_script = repro_script
            self.mcp.add_tool(self.read_ir, name=f"{name}_read_ir")
            self.mcp.add_tool(self.write_ir, name=f"{name}_write_ir")
            self.mcp.add_tool(self.verify_ir_valid, name=f"{name}_verify_valid")
            self.mcp.add_tool(self.still_reproduces_bug,
                              name=f"{name}_still_reproduces")
            super().__post_init__()

        def read_ir(self) -> str:
            """Return the current scratch IR text."""
            return open(self._ir_path, errors="replace").read()

        def write_ir(self, contents: str) -> str:
            """Overwrite the scratch IR with *contents*."""
            with open(self._ir_path, "w") as f:
                f.write(contents)
            return f"wrote {len(contents)} bytes"

        def verify_ir_valid(self) -> bool:
            """True iff circt-opt parses + verifies the current scratch IR."""
            ir = open(self._ir_path, errors="replace").read()
            return _safe_run([CIRCT_OPT, "--verify-diagnostics", "-"],
                             input=ir).returncode == 0

        def still_reproduces_bug(self) -> bool:
            """True iff the repro script still exits non-zero on the current IR."""
            return _safe_run(["bash", self._repro_script]).returncode != 0

    return SemanticReduceTool()
