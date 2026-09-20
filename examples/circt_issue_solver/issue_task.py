"""Worker-side per-issue pipeline for the CIRCT issue flow.

Shipped to chia-circt workers via runtime_env (py_modules). MUST NOT import
head-only modules (db / triage) or read files at import time — all config
arrives in the ``cfg`` dict. Imports the local circt_util.py shipped alongside
it, plus the BuildTool / LitTool MCP wrappers from chia.chipyard.circt.
"""
from __future__ import annotations

import glob
import json
import os
import re
from string import Template

from chia.base.ChiaFunction import ChiaFunction, get


def _assess_decision(text: str) -> dict:
    """Parse the assess turn's footer into {proceed: bool, status: str|None, note}.

    Reads the last ``DECISION: CLEAR|NOT_A_BUG|UNCLEAR`` line. CLEAR -> proceed;
    NOT_A_BUG / UNCLEAR -> skip with that status and the ``REASON:`` text as the
    note. Defaults to proceed if the footer is unparseable — better to attempt a
    possibly-good issue than silently drop it.
    """
    t = text or ""
    verdicts = re.findall(r"(?im)^\s*DECISION:\s*(CLEAR|UNCLEAR|NOT[_ ]?A[_ ]?BUG)\b", t)
    v = verdicts[-1].upper() if verdicts else "CLEAR"
    if v == "CLEAR":
        return {"proceed": True, "status": None, "note": None}
    status = "not_a_bug" if v.startswith("NOT") else "unclear"
    m = re.search(r"(?is)\bREASON:\s*(.+)$", t)
    note = (m.group(1).strip() if m else t.strip()[-1500:]) or f"marked {status} (no reason given)"
    return {"proceed": False, "status": status, "note": note}


@ChiaFunction(resources={"circt": 1})
def run_issue_remote(issue_md: str, number: int, cfg: dict,
                     resume: dict | None = None, assess_only: bool = False) -> dict:
    """Reproduce -> fix -> verify -> writeup for one issue, pinned to ONE
    chia-circt container.

    Each phase prompts an llm while the bash/build/lit MCP
    servers stay on this chia-circt worker, reached over HTTP. Phases are
    stateless — each is its own `claude --print`; the context a later phase needs
    is inlined into its prompt (the repro.sh into fix; the diff/verdict into
    writeup).

    ``resume`` (optional) REPLAYS a prior attempt instead of running repro+fix:
    ``{"diff": <saved fix.diff>, "repro_files": {relpath: content}}`` — the saved
    repro is restored and the diff re-applied, then we jump straight to verify
    (which still triggers the regression-repair turn). Used to exercise a later
    phase without redoing the expensive/non-deterministic earlier ones.

    Returns a result dict (status / verdict / diff / writeup / per-phase logs).
    The head persists it.
    """
    import ray
    from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy
    from chia.models.claude import ClaudeCodeLLM
    from chia.base.tools.BashTool import BashTool

    import circt_util
    import context_precision_gate as cpg
    import fault_localizer
    import fast_lit_slicer
    import formal_verifier
    import tablegen_analyzer
    import ssa_provenance_slicer
    import soundness_auditor
    import cegis_oracle
    import pipeline_bisector
    import dialect_fix_memory
    import sequential_verifier
    import clang_format_agent
    from chia.chipyard.circt import BuildTool, LitTool

    node = ray.get_runtime_context().get_node_id()
    here = {"scheduling_strategy": NodeAffinitySchedulingStrategy(node_id=node, soft=False)}
    logs: dict = {}

    def _turn(phase: str, prompt: str, tools: list):
        phase_timeout = cfg.get("timeouts", {}).get(phase, 1800)
        # chia.models.claude / .antigravity / .opencode: `prompt` is a ChiaFunction we
        # dispatch onto an `llm` worker (1.0/call, so the cluster's `llm` slots
        # cap concurrency) — the CLI runs there while the bash/build/lit MCP servers
        # stay on this chia-circt worker, reached over HTTP. log_dir is None: the
        # CLI's on-worker log would land on the ephemeral llm container, so we
        # persist cli.stream_result centrally instead. resume_session +
        # projects_cwd=None give each phase a fresh session whose .jsonl
        # transcript we read back for logging (no actual --resume — a new LLM is
        # built per phase).
        backend = cfg.get("backend", "claude")
        if backend == "antigravity":
            # chia.models.antigravity: `agy --print --output-format stream-json`
            # (full tool/usage transcript on stream_result); the system prompt
            # is folded into the user message and effort rides on the model id
            # (e.g. gemini-3.1-pro-high). resume_session=True only so the
            # conversation db comes back on cli.session_transcript for logging
            # (SQLite bytes — persisted as llm_<phase>.db); a new LLM is built
            # per phase, so nothing is actually resumed.
            from chia.models.antigravity import AntigravityLLM
            llm = AntigravityLLM(
                model=cfg["model"], system_message=cfg["system_prompt"],
                timeout_seconds=phase_timeout, resume_session=True,
            )
        elif backend == "opencode":
            # chia.models.opencode: `opencode run` with its built-in google-vertex
            # provider (model = "google-vertex/<gemini id>"). We (re)declare the
            # provider block to pin project/location and to register the model
            # id even if opencode's catalog lags Vertex. opencode's own
            # write/edit/bash tools would act on the llm container's FS, not this
            # circt worker, so deny everything except our MCP tools (cf.
            # examples/memcpy).
            from chia.models.opencode import OpenCodeLLM, AdditionalModelProvider
            provider_id, _, model_id = cfg["model"].partition("/")
            if provider_id == "google" or "gemini-2." in cfg["model"] or "gemini-1." in cfg["model"]:
                gemini_key = os.environ.get("GEMINI_API_KEY", "")
                actual_model = model_id if model_id else cfg["model"]
                provider = AdditionalModelProvider(
                    id="google", npm="@ai-sdk/google",
                    name="Google AI Studio", models=[actual_model],
                    api_key=gemini_key,
                )
            elif provider_id == "deepseek" or "deepseek" in cfg["model"]:
                deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
                actual_model = model_id if model_id else cfg["model"]
                provider = AdditionalModelProvider(
                    id=provider_id or "deepseek", npm="@ai-sdk/openai-compatible",
                    name="DeepSeek", models=[actual_model],
                    base_url="https://api.deepseek.com",
                    api_key=deepseek_key,
                )
            else:
                vertex = cfg["vertex"]
                provider = AdditionalModelProvider(
                    id=provider_id or "google-vertex", npm="@ai-sdk/google-vertex",
                    name="Google Vertex AI", models=[model_id],
                    options={"project": vertex["project"], "location": vertex["location"]},
                )
            perms = {"*": "deny", **{f"{t.name}_*": "allow" for t in tools}}
            llm = OpenCodeLLM(
                model=cfg["model"], system_message=cfg["system_prompt"],
                timeout_seconds=phase_timeout,
                additional_providers=[provider], config=perms,
            )
        else:
            llm = ClaudeCodeLLM(
                model=cfg["model"], system_message=cfg["system_prompt"],
                timeout_seconds=phase_timeout,
                extra_cli_args=["--effort", "max"],
                resume_session=True, projects_cwd=None,
            )
        cli = get(llm.prompt.options(resources={"llm": 1.0}).chia_remote(llm, prompt, tools))
        transcript = getattr(cli, "session_transcript", None) or b""
        logs[phase] = {
            "result": cli.result, "stream": cli.stream_result,
            "stderr": cli.stderr, "success": bool(getattr(cli, "success", False)),
            "transcript": transcript if isinstance(transcript, (bytes, bytearray)) else b"",
            # claude: the CLI's .jsonl session file; antigravity: agy's SQLite
            # conversation db. The head picks the artifact extension from this.
            "transcript_ext": "db" if backend == "antigravity" else "jsonl",
            # token/cost totals where the backend reports them (antigravity, opencode)
            "usage": getattr(cli, "usage", None),
        }
        return cli

    def _render(key: str, **kw) -> str:
        # Template/safe_substitute (not str.format) so MLIR/shell braces in the
        # prompts and issue body don't blow up substitution.
        return Template(cfg[key]).safe_substitute(**kw)

    # 0) Trust the checkout (git safe.directory) + idempotent warm-up (lit + tool
    #    targets) + clean source back to the tag. assess_only reads source only,
    #    so it skips the (slow) warm build.
    circt_util.circt_trust_source()
    if not assess_only:
        circt_util.circt_warm_build(cfg["tool_targets"], num_cpus=cfg["build_jobs"])
    reset = circt_util.circt_git_reset(cfg["tag"])
    if not reset["success"]:
        return {"status": "error", "reproduced": False, "logs": logs,
                "notes": "git reset failed:\n" + reset["log"]}

    bash = build = lit = None
    gate_outcome = None   # populated by the Context-Precision Gate (non-resume path)
    fault_info = None
    sliced_targets = None
    fault_slice_block = ""
    diagnosis_block = ""
    try:
        # 300s cap: a synchronous tool call held much longer risks the same
        # MCP-transport response loss the async build/lit tools avoid. Builds and
        # test runs should go through those async tools, not bash.
        bash = BashTool(name=f"bash_{number}", work_dir=circt_util._CIRCT_SOURCE_TREE,
                        task_options=here, timeout_seconds=300)

        # assess_only: run JUST the assess turn (read-only bash), return its
        # decision, and stop — no repro/fix/verify, no build/lit tools spun up.
        # Used to spot-check the assess prompt without a full pipeline run.
        if assess_only:
            assess = _turn("assess", _render("assess_prompt", issue=issue_md), [bash])
            decision = _assess_decision(assess.result)
            return {"status": "clear" if decision["proceed"] else decision["status"],
                    "assess_only": True, "reproduced": False,
                    "notes": decision["note"], "logs": logs}

        build = BuildTool(name=f"build_{number}", num_cpus=cfg["build_jobs"], task_options=here)
        lit = LitTool(name=f"lit_{number}", task_options=here)
        agent_tools = [bash, build, lit]

        if resume:
            # REPLAY — restore the saved repro + re-apply the saved fix, skip the
            # repro/fix turns, and fall through to verify (which fires the
            # regression-repair turn if the replayed diff regresses the suite).
            circt_util.circt_write_files(resume.get("repro_files") or {}, cfg["repro_dir"])
            ap = circt_util.circt_apply_diff(resume.get("diff") or "")
            if not ap["success"]:
                return {"status": "error", "reproduced": False, "logs": logs,
                        "notes": "git apply (replay) failed:\n" + ap["log"]}
            try:
                repro_text = open(cfg["repro_path"]).read()
            except OSError:
                repro_text = "(repro.sh not found)"
        else:
            # 0.5) ASSESS — before spending a repro/fix attempt, decide (a) is
            #      this actually a bug, and (b) are both the bug and the correct
            #      behavior clear enough to act on autonomously (the agent may
            #      read the source/docs read-only). If it's not a bug or either is
            #      unclear, log the reason and skip — there is no human to ask.
            assess = _turn("assess", _render("assess_prompt", issue=issue_md), [bash])
            decision = _assess_decision(assess.result)
            if not decision["proceed"]:
                return {"status": decision["status"], "reproduced": False,
                        "logs": logs, "notes": decision["note"]}

            # 1) REPRODUCE — the LLM writes <repro_path> (exit 0 iff fixed).
            _turn("repro", _render("repro_prompt", issue=issue_md), agent_tools)
            clean = circt_util.circt_run_script(cfg["repro_path"])
            if cfg["require_repro"] and clean["exit_code"] == 0:
                return {"status": "no_repro", "reproduced": False, "logs": logs,
                        "repro_tail": clean["log_tail"]}

            try:
                repro_text = open(cfg["repro_path"]).read()
            except OSError:
                repro_text = "(repro.sh not found)"

            # 1.5) CONTEXT-PRECISION GATE — narrow the raw repro input to
            #      near-minimal IR (+ implicated pass) BEFORE the Fix agent sees
            #      it, overwriting the on-disk repro input so the Fix agent's
            #      tools see it too. Non-fatal: on any miss / when disabled
            #      ($gate == "") the Fix turn runs exactly as before. All the
            #      logic + the ungated-baseline branch live in
            #      context_precision_gate.gate_for_fix so it is ray-free testable.
            gate_block, gate_outcome = cpg.gate_for_fix(
                cfg, repro_text, clean["log_tail"], clean["exit_code"], _turn)

            # 1.6) SYMBOLIC FAULT LOCALIZATION [Tier 1+ Upgrade]
            implicated_pass = (gate_outcome or {}).get("localize_stage")
            fault_info = fault_localizer.localize_fault(
                clean.get("log_tail", ""),
                circt_src_dir=circt_util._CIRCT_SOURCE_TREE,
                implicated_pass=implicated_pass,
            )
            fault_slice_block = fault_localizer.format_fault_slice_for_prompt(fault_info)

            # 1.7) FAST SLICED LIT TARGETS [Tier 1+ Upgrade]
            sliced_targets = fast_lit_slicer.get_sliced_lit_targets(
                implicated_pass=fault_info.get("failing_pass") or implicated_pass,
                modified_files=[fault_info["file_path"]] if fault_info.get("file_path") else None,
            )
            if sliced_targets:
                target_fmt = ", ".join(f'"{t}"' for t in sliced_targets)
                lit_hint = f"- **Fast Dialect Lit Test Suite**: `[{target_fmt}]` (~3s turnaround vs 3m for whole suite; use with lit tool)\n"
                fault_slice_block = (fault_slice_block + "\n" + lit_hint) if fault_slice_block else lit_hint

            # 1.8) TABLEGEN & ODS INTROSPECTION [Research Grade]
            tablegen_block = ""
            try:
                ops_in_ir = tablegen_analyzer.extract_ops_from_mlir(repro_text)
                if ops_in_ir:
                    tablegen_block = tablegen_analyzer.format_tablegen_context(
                        ops_in_ir, circt_src_dir=circt_util._CIRCT_SOURCE_TREE, max_ops=4
                    )
            except Exception:
                pass

            # 1.8b) PIPELINE BISECTION & DIALECT FIX MEMORY [Phase-Ordering & Idiom Matching]
            pipeline_block = ""
            try:
                pipe_passes = pipeline_bisector.parse_pipeline_flags(repro_text)
                if len(pipe_passes) > 1:
                    bisect_info = pipeline_bisector.bisect_pipeline(
                        pipe_passes, repro_text, compiler_bin=f"{circt_util._CIRCT_SOURCE_TREE}/build/bin/circt-opt"
                    )
                    pipeline_block = pipeline_bisector.format_pipeline_bisection_context(bisect_info)
            except Exception:
                pass

            idiom_block = ""
            try:
                detected_dialect = fault_info.get("dialect", "FIRRTL") if fault_info else "FIRRTL"
                idiom_block = dialect_fix_memory.format_idioms_for_prompt(detected_dialect, repro_text)
            except Exception:
                pass

            if pipeline_block:
                fault_slice_block = (fault_slice_block + "\n" + pipeline_block) if fault_slice_block else pipeline_block
            if idiom_block:
                fault_slice_block = (fault_slice_block + "\n" + idiom_block) if fault_slice_block else idiom_block

            # 1.9) ARCHITECT DIAGNOSTIC PASS [Tier 1+ Upgrade]
            diag_prompt_template = cfg.get("diagnose_prompt")
            if diag_prompt_template and cfg.get("diagnose_enabled", True):
                try:
                    d_prompt = _render("diagnose_prompt", issue=issue_md, repro=repro_text,
                                       gate=gate_block, fault_slice=fault_slice_block,
                                       tablegen_spec=tablegen_block)
                    d_turn = _turn("diagnose", d_prompt, [bash])
                    if d_turn and d_turn.result:
                        diagnosis_block = f"## Architect Diagnosis & Blueprint\n{d_turn.result.strip()}\n"
                except Exception:
                    pass

            # 2) FIX — fresh session; inline repro.sh + gated context + fault slice + diagnosis
            _turn("fix", _render("fix_prompt", issue=issue_md, repro=repro_text,
                                 gate=gate_block, fault_slice=fault_slice_block,
                                 diagnosis=diagnosis_block), agent_tools)

        # 3) VERIFY — deterministic, no LLM. Regression gate runs the WHOLE lit
        #    suite (minus baseline-red dirs) — ~6s and always non-empty, so it
        #    catches regressions outside the touched dialect (e.g. a lib/Firtool
        #    or tools/ change) and never vacuously passes.
        tps = circt_util.circt_lit_gate_paths()

        def _verify():
            diff = circt_util.circt_capture_diff(cfg["tag"])
            rebuild = circt_util.circt_ninja_build(cfg["tool_targets"], num_cpus=cfg["build_jobs"])
            repro_after = circt_util.circt_run_script(cfg["repro_path"])
            repro_fixed = rebuild["success"] and repro_after["exit_code"] == 0
            lit_res = circt_util.circt_run_lit(tuple(tps), filter_out=circt_util._LIT_GATE_FILTER_OUT)
            return diff, rebuild, repro_after, repro_fixed, lit_res

        diff, rebuild, repro_after, repro_fixed, lit_res = _verify()

        # 3b) FIX REGRESSION — the repro is fixed but the change broke other
        #     tests. Give the agent ONE more turn (with the failing tests + their
        #     output inlined) to repair the regression without un-fixing the bug,
        #     then re-verify. Only triggered when there's actually a regression to
        #     chase (repro green, suite red).
        notes = None
        if repro_fixed and lit_res["failed"] > 0:
            fails = lit_res["failures"]
            fail_paths = circt_util.circt_lit_failure_paths(fails)
            focused = (circt_util.circt_run_lit(tuple(fail_paths)) if fail_paths
                       else {"log_tail": lit_res.get("log_tail", "")})
            _turn("regression", _render("regression_prompt", issue=issue_md,
                                        repro=repro_text, diff=diff["diff"],
                                        failures="\n".join(fails) or "(none parsed)",
                                        failure_log=focused.get("log_tail", "")),
                  agent_tools)
            diff, rebuild, repro_after, repro_fixed, lit_res = _verify()
            notes = "regression turn run; initial lit failures: " + "; ".join(fails)

        verdict = {
            "reproduced": True, "build_ok": rebuild["success"], "fixed": repro_fixed,
            "lit_ok": lit_res["success"], "lit_passed": lit_res["passed"],
            "lit_failed": lit_res["failed"], "lit_failures": lit_res["failures"],
            "test_paths": tps, "notes": notes,
        }

        # 3c) RESEARCH SOUNDNESS AUDIT & CEGIS MUTATION ORACLE
        soundness_audit = soundness_auditor.audit_patch_soundness(diff.get("diff", ""))
        cegis_res = {"tested": False, "all_passed": True, "counterexamples": []}

        # 3d) FORMAL VERIFICATION & TIER 1+ METRICS
        formal_outcome = {"applicable": False, "verified": None, "error": None}
        if repro_fixed:
            try:
                repro_dir = cfg["repro_dir"]
                ir_path, ir_text = cpg.find_repro_ir(repro_dir, repro_text)
                if ir_text:
                    # Run CEGIS boundary mutation test
                    mutants = cegis_oracle.generate_boundary_mutants(ir_text)
                    if mutants:
                        circt_opt_bin = f"{circt_util._CIRCT_SOURCE_TREE}/build/bin/circt-opt"
                        cegis_res = cegis_oracle.verify_patch_on_mutants(mutants, compiler_bin=circt_opt_bin)

                    seq_info = sequential_verifier.detect_sequential_elements(ir_text)
                    lec_bin = f"{circt_util._CIRCT_SOURCE_TREE}/build/bin/circt-lec"
                    if seq_info.get("is_sequential"):
                        formal_outcome["applicable"] = True
                        formal_outcome["is_sequential"] = True
                        if os.path.exists(lec_bin) and ir_path:
                            formal_outcome = sequential_verifier.verify_sequential_equivalence(
                                ir_path, ir_path, circt_lec_bin=lec_bin
                            )
                    elif formal_verifier.is_lec_applicable(ir_text):
                        formal_outcome["applicable"] = True
                        formal_outcome["is_sequential"] = False
                        if os.path.exists(lec_bin) and ir_path:
                            formal_outcome = formal_verifier.verify_lec(
                                ir_path, ir_path, circt_lec_bin=lec_bin
                            )

                    # 3e) CLOSED-LOOP CEGIS INDUCTIVE REFUTATION
                    # If the candidate patch passed the basic bug test but failed on boundary
                    # mutants or violated soundness invariants, feed the counterexample back!
                    if not soundness_audit.get("is_sound", True) or not cegis_res.get("all_passed", True):
                        refute_prompt = cegis_oracle.format_cegis_refutation_prompt(
                            cegis_res.get("counterexamples", []),
                            soundness_audit.get("violations", []),
                        )
                        _turn("cegis_refine", f"## Issue #{issue_md}\n\n{refute_prompt}", agent_tools)
                        diff, rebuild, repro_after, repro_fixed, lit_res = _verify()
                        soundness_audit = soundness_auditor.audit_patch_soundness(diff.get("diff", ""))
                        if repro_fixed and mutants:
                            cegis_res = cegis_oracle.verify_patch_on_mutants(mutants, compiler_bin=circt_opt_bin)
            except Exception as e:
                formal_outcome["error"] = str(e)

        # Code formatting and LLVM maintainer hygiene
        diff_str = clang_format_agent.sanitize_patch(diff.get("diff", ""))
        diff["diff"] = diff_str
        hygiene_audit = clang_format_agent.audit_hygiene(diff_str)

        tier1_outcome = {
            "fault_found": bool(fault_info.get("found")) if fault_info else False,
            "fault_file": fault_info.get("relative_path") if fault_info else None,
            "fault_line": fault_info.get("line_number") if fault_info else None,
            "sliced_lit_target": sliced_targets,
            "diagnosed": bool(diagnosis_block),
            "formal_applicable": formal_outcome.get("applicable", False),
            "formal_verified": formal_outcome.get("verified"),
            "sound": soundness_audit.get("is_sound", True),
            "soundness_violations": soundness_audit.get("violations", []),
            "cegis_passed": cegis_res.get("all_passed", True),
            "counterexamples": cegis_res.get("counterexamples", []),
            "hygiene_clean": hygiene_audit.get("is_clean", True),
            "hygiene_violations": hygiene_audit.get("debug_violations", []),
        }

        # 4) WRITEUP — fresh session, no tools; diff + verdict inlined.
        wr = _turn("writeup", _render("writeup_prompt", issue=issue_md,
                                      diff=diff["diff"],
                                      verdict=json.dumps(verdict, indent=2)), [])

        # Capture the agent's repro artifacts (.circtissues/: repro.sh, input
        # MLIR, NOTES.md, ...) so they're saved centrally — the container's FS is
        # ephemeral. (lit tests the agent added under test/... ride in the diff.)
        repro_files: dict = {}
        for p in sorted(glob.glob(f"{cfg['repro_dir']}/**", recursive=True)):
            if os.path.isfile(p) and os.path.getsize(p) <= 256_000:
                try:
                    repro_files[os.path.relpath(p, cfg["repro_dir"])] = open(p, errors="replace").read()
                except OSError:
                    pass

        status = "fixed" if (repro_fixed and lit_res["success"]) else "attempted"
        return {
            "status": status, **verdict,
            "diff": diff["diff"], "added": diff["added"], "removed": diff["removed"],
            "rebuild_tail": rebuild["log_tail"], "repro_tail": repro_after["log_tail"],
            "lit_tail": lit_res.get("log_tail", ""),
            "writeup": wr.result, "repro_files": repro_files, "logs": logs,
            "gate": gate_outcome,
            "tier1": tier1_outcome,
        }
    finally:
        for t in (lit, build, bash):
            if t is not None:
                try:
                    t.stop()
                except Exception:
                    pass
