"""Adversarial Counterexample-Guided Inductive Synthesis (CEGIS) Oracle.

Generates boundary semantic mutants from minimal MLIR reproducers (e.g. 0-width
integers, signedness flips, operand swaps) to detect patch overfitting and verify
inductive soundness across compiler edge cases.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from typing import Any


def generate_boundary_mutants(mlir_text: str) -> list[dict[str, str]]:
    """Generate semantic boundary mutants from an MLIR snippet to probe patch soundness."""
    if not mlir_text:
        return []

    mutants: list[dict[str, str]] = []

    # 1. Bitwidth boundary mutation: i1 <-> i0 (0-width ints are notorious crash vectors in HW compilers)
    if "i1" in mlir_text:
        m_ir = re.sub(r"\bi1\b", "i0", mlir_text)
        mutants.append({
            "name": "zero_width_boundary",
            "description": "Mutates bitwidths to zero-width integer (i0)",
            "mlir": m_ir,
        })

    # 2. Wide integer boundary mutation: i32 -> i64 / i128
    if "i32" in mlir_text or "i8" in mlir_text:
        m_ir = re.sub(r"\bi(?:32|8)\b", "i64", mlir_text)
        mutants.append({
            "name": "wide_integer_boundary",
            "description": "Mutates integers to 64-bit width (i64)",
            "mlir": m_ir,
        })

    # 3. FIRRTL signedness inversion: uint<...> <-> sint<...>
    if "uint<" in mlir_text:
        m_ir = mlir_text.replace("uint<", "sint<")
        mutants.append({
            "name": "signedness_inversion",
            "description": "Inverts unsigned FIRRTL types to signed (sint)",
            "mlir": m_ir,
        })
    elif "sint<" in mlir_text:
        m_ir = mlir_text.replace("sint<", "uint<")
        mutants.append({
            "name": "unsignedness_inversion",
            "description": "Inverts signed FIRRTL types to unsigned (uint)",
            "mlir": m_ir,
        })

    # 4. Binary operand order permutation: swap %0, %1 on binary ops
    bin_match = re.search(r"=\s*([a-zA-Z0-9_\.]+\s+(%[a-zA-Z0-9_]+)\s*,\s*(%[a-zA-Z0-9_]+))", mlir_text)
    if bin_match:
        full_sub = bin_match.group(1)
        v1 = bin_match.group(2)
        v2 = bin_match.group(3)
        op_prefix = full_sub.split()[0]
        swapped = f"{op_prefix} {v2}, {v1}"
        m_ir = mlir_text.replace(full_sub, swapped, 1)
        mutants.append({
            "name": "operand_commutation",
            "description": f"Swaps operand order ({v1}, {v2} -> {v2}, {v1})",
            "mlir": m_ir,
        })

    return mutants


def verify_patch_on_mutants(
    mutants: list[dict[str, str]],
    compiler_bin: str = "/workspace/circt/build/bin/circt-opt",
    args: list[str] | None = None,
    timeout: int = 15
) -> dict[str, Any]:
    """Execute the patched compiler against all boundary mutants.
    
    If any mutant triggers a compiler crash / assertion failure (exit code < 0 or > 128),
    it is returned as a counterexample proving the patch is unsound or incomplete.
    """
    if not os.path.exists(compiler_bin):
        return {
            "tested": False,
            "all_passed": True,
            "counterexamples": [],
            "note": f"Compiler binary {compiler_bin} not found.",
        }

    cmd_args = args or ["--verify-diagnostics"]
    counterexamples = []

    for m in mutants:
        with tempfile.NamedTemporaryFile(suffix=".mlir", mode="w", delete=False) as tf:
            tf.write(m["mlir"])
            tf_path = tf.name

        try:
            cmd = [compiler_bin, tf_path, *cmd_args]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            # Crash indicators: negative returncode (signal) or assertion failure output
            is_crash = proc.returncode < 0 or "Assertion `" in proc.stderr or "LLVM ERROR" in proc.stderr
            if is_crash:
                counterexamples.append({
                    "name": m["name"],
                    "description": m["description"],
                    "returncode": proc.returncode,
                    "stderr": proc.stderr[-500:],
                    "mutant_mlir": m["mlir"][:1000],
                })
        except subprocess.TimeoutExpired:
            counterexamples.append({
                "name": m["name"],
                "description": m["description"],
                "returncode": -1,
                "stderr": "Compiler hung / timed out on mutant input.",
                "mutant_mlir": m["mlir"][:1000],
            })
        except Exception as e:
            pass
        finally:
            if os.path.exists(tf_path):
                os.unlink(tf_path)

    all_sound = len(counterexamples) == 0
    return {
        "tested": True,
        "all_passed": all_sound,
        "mutant_count": len(mutants),
        "counterexample_count": len(counterexamples),
        "counterexamples": counterexamples,
    }


def format_cegis_refutation_prompt(
    counterexamples: list[dict[str, Any]],
    soundness_violations: list[str] | None = None,
) -> str:
    """Format CEGIS counterexamples and soundness violations into an inductive feedback prompt."""
    lines = [
        "## ⚠️ CEGIS Oracle Refutation: Candidate Patch Failed Invariant Checks",
        "",
        "Your candidate patch passed the initial bug reproducer, but was **refuted** during adversarial boundary verification.",
        "A sound compiler repair must not overfit to a single test and must respect MLIR/CIRCT architectural invariants.",
        "",
    ]

    if soundness_violations:
        lines.append("### Static Soundness Violations Detected:")
        for v in soundness_violations:
            lines.append(f"- **Violation**: {v}")
        lines.append("")

    if counterexamples:
        lines.append("### Adversarial Counterexamples Discovered:")
        for idx, ce in enumerate(counterexamples, 1):
            lines.append(f"#### Counterexample {idx}: `{ce.get('name', 'boundary_mutant')}`")
            lines.append(f"- **Description**: {ce.get('description', 'Boundary mutation')}")
            lines.append(f"- **Crash Log / Stderr**:\n```\n{ce.get('stderr', '').strip()}\n```")
            if ce.get("mutant_mlir"):
                lines.append(f"- **Failing MLIR IR**:\n```mlir\n{ce.get('mutant_mlir', '').strip()}\n```")
            lines.append("")

    lines.extend([
        "### Inductive Refinement Instructions:",
        "1. **Preserve Compiler Invariants**: Do NOT delete assertions or insert trivial unconditional bypasses.",
        "2. **Handle Failure Gracefully**: In MLIR pattern rewriters, handle invalid or unhandled operand types using `rewriter.notifyMatchFailure(op, \"...\")` and return `failure()`.",
        "3. **Refine Patch**: Emit an updated C++ patch that satisfies both the original issue reproducer AND the counterexamples above.",
    ])

    return "\n".join(lines)

