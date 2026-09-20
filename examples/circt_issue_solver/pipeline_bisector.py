"""Multi-Pass Pipeline Phase-Ordering Bisector for CIRCT Compilers.

In MLIR and CIRCT, bugs are frequently caused by subtle phase-ordering contract
violations: Pass A emits non-canonical or malformed intermediate IR, which passes
unnoticed until Pass D crashes on it.

This module bisects the compiler pass pipeline to isolate:
1. The exact originating pass that produced invalid intermediate IR.
2. The minimal failing pipeline prefix.
3. Whether the bug is an 'execution sink crash' or a 'phase-ordering violation'.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from typing import Any, Sequence


def parse_pipeline_flags(cmd_or_pipeline: str) -> list[str]:
    """Extract individual compiler pass flags from a pipeline string or CLI invocation."""
    if not cmd_or_pipeline:
        return []

    # Find flags like --lower-firrtl-to-hw, -firrtl-infer-widths, --pass-name
    tokens = re.findall(r"(?:--?[a-zA-Z0-9_\-]+(?:=[a-zA-Z0-9_\-]+)?)", cmd_or_pipeline)
    # Filter out generic flags like -o, --split-input-file, etc.
    non_pass_flags = {
        "-o", "--split-input-file", "--verify-diagnostics", "-v", "--help",
        "--mlir-print-op-generic", "--allow-unregistered-dialect",
    }
    return [t for t in tokens if t not in non_pass_flags and not t.startswith("-o=")]


def bisect_pipeline(
    passes: Sequence[str],
    mlir_text: str,
    compiler_bin: str = "/workspace/circt/build/bin/circt-opt",
    timeout: int = 15,
) -> dict[str, Any]:
    """Bisect a multi-pass pipeline to find the minimal failing pass prefix and root pass.
    
    Returns:
      - is_pipeline: bool (True if > 1 pass in pipeline)
      - total_passes: int
      - minimal_failing_prefix: list[str]
      - root_originating_pass: str (the pass where failure first manifests)
      - is_phase_ordering_bug: bool
      - intermediate_verifications: list of per-step outcomes
    """
    pass_list = list(passes)
    if len(pass_list) <= 1:
        return {
            "is_pipeline": False,
            "total_passes": len(pass_list),
            "minimal_failing_prefix": pass_list,
            "root_originating_pass": pass_list[0] if pass_list else "unknown",
            "is_phase_ordering_bug": False,
            "intermediate_verifications": [],
            "diagnosis": "Single-pass execution; crash originates within this pass.",
        }

    with tempfile.NamedTemporaryFile(suffix=".mlir", mode="w", delete=False) as tf:
        tf.write(mlir_text)
        tf_path = tf.name

    verifications: list[dict[str, Any]] = []
    first_failure_idx = -1

    try:
        # Step through prefix pipelines: 1..N
        for k in range(1, len(pass_list) + 1):
            prefix = pass_list[:k]
            cmd = [compiler_bin, tf_path, *prefix, "--verify-each"]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
                crashed = proc.returncode != 0 or "Assertion `" in proc.stderr or "LLVM ERROR" in proc.stderr
                verifications.append({
                    "step": k,
                    "pass_added": pass_list[k - 1],
                    "prefix": prefix,
                    "returncode": proc.returncode,
                    "failed": crashed,
                    "stderr_summary": proc.stderr[-300:] if crashed else "",
                })
                if crashed and first_failure_idx == -1:
                    first_failure_idx = k - 1
                    break
            except subprocess.TimeoutExpired:
                verifications.append({
                    "step": k,
                    "pass_added": pass_list[k - 1],
                    "prefix": prefix,
                    "returncode": -1,
                    "failed": True,
                    "stderr_summary": "Pass timed out / infinite loop.",
                })
                if first_failure_idx == -1:
                    first_failure_idx = k - 1
                    break
    except Exception as e:
        verifications.append({"error": str(e)})
    finally:
        import os
        if os.path.exists(tf_path):
            os.unlink(tf_path)

    if first_failure_idx != -1:
        minimal_prefix = pass_list[: first_failure_idx + 1]
        originating_pass = pass_list[first_failure_idx]
        is_phase_ordering = first_failure_idx < (len(pass_list) - 1)
        diagnosis = (
            f"Phase-ordering violation: Pass {originating_pass} (step {first_failure_idx + 1} of {len(pass_list)}) "
            f"triggers invalid IR state or crash before the full pipeline completes."
            if is_phase_ordering
            else f"Pipeline sink crash: Failure manifests at final pass {originating_pass}."
        )
    else:
        minimal_prefix = pass_list
        originating_pass = pass_list[-1]
        is_phase_ordering = False
        diagnosis = "All intermediate prefix passes succeeded with --verify-each."

    return {
        "is_pipeline": True,
        "total_passes": len(pass_list),
        "minimal_failing_prefix": minimal_prefix,
        "root_originating_pass": originating_pass,
        "is_phase_ordering_bug": is_phase_ordering,
        "intermediate_verifications": verifications,
        "diagnosis": diagnosis,
    }


def format_pipeline_bisection_context(bisection_res: dict[str, Any]) -> str:
    """Format pipeline bisection results for injection into the Architect diagnosis prompt."""
    if not bisection_res.get("is_pipeline"):
        return ""

    lines = [
        "### 🔍 Compiler Pipeline Bisection Analysis",
        f"- **Total Pipeline Passes**: {bisection_res.get('total_passes')}",
        f"- **Root Implicated Pass**: `{bisection_res.get('root_originating_pass')}`",
        f"- **Phase-Ordering Bug**: {'YES (earlier pass corrupted IR)' if bisection_res.get('is_phase_ordering_bug') else 'NO (crash sink in final pass)'}",
        f"- **Diagnosis**: {bisection_res.get('diagnosis')}",
        f"- **Minimal Failing Pass Prefix**: `{' '.join(bisection_res.get('minimal_failing_prefix', []))}`",
        "",
    ]
    return "\n".join(lines)
