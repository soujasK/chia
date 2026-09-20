"""Formal Verification Helper for CIRCT using circt-lec (Logical Equivalence Checking).

Verifies whether pre-patch and post-patch MLIR modules are logically equivalent,
or verifies semantic correctness of transformations using CIRCT's formal tools.
"""
from __future__ import annotations

import os
import subprocess
from typing import Any


CIRCT_LEC_DEFAULT = "/workspace/circt/build/bin/circt-lec"


def is_lec_applicable(mlir_content: str) -> bool:
    """Check if MLIR code contains combinational/hardware modules suitable for circt-lec."""
    if not mlir_content:
        return False
    # circt-lec operates on HW/Comb dialects (e.g., hw.module, comb.add, etc.)
    has_hw = "hw.module" in mlir_content or "comb." in mlir_content
    return has_hw


def verify_lec(
    file_first: str,
    file_second: str,
    module_first: str | None = None,
    module_second: str | None = None,
    circt_lec_bin: str = CIRCT_LEC_DEFAULT,
    timeout: int = 60
) -> dict[str, Any]:
    """Run circt-lec between two files / modules to verify logical equivalence."""
    result: dict[str, Any] = {
        "applicable": True,
        "verified": False,
        "equivalent": None,
        "returncode": -1,
        "log": "",
        "error": None
    }

    if not os.path.exists(circt_lec_bin):
        result["applicable"] = False
        result["error"] = f"circt-lec binary not found at {circt_lec_bin}"
        return result

    if not os.path.exists(file_first) or not os.path.exists(file_second):
        result["applicable"] = False
        result["error"] = "One or both input files do not exist."
        return result

    cmd = [circt_lec_bin, file_first, file_second]
    if module_first:
        cmd.extend(["--c1", module_first])
    if module_second:
        cmd.extend(["--c2", module_second])

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        result["returncode"] = proc.returncode
        result["log"] = (proc.stdout + "\n" + proc.stderr).strip()

        if proc.returncode == 0:
            result["verified"] = True
            result["equivalent"] = True
        else:
            # Check if it was a counterexample or tool crash
            if "counterexample" in result["log"].lower() or "not equivalent" in result["log"].lower():
                result["verified"] = True
                result["equivalent"] = False
            else:
                result["verified"] = False
                result["error"] = f"circt-lec exited with code {proc.returncode}"
    except subprocess.TimeoutExpired:
        result["error"] = f"circt-lec timed out after {timeout}s"
    except Exception as e:
        result["error"] = str(e)

    return result
