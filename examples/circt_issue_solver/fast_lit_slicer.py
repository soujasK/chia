"""Fast Lit Slicer for CIRCT Dialect and Conversion Testing.

Slices the 2000+ CIRCT lit tests down to targeted dialect/conversion test suites
(e.g., test/Conversion/FIRRTLToHW/, test/Dialect/FIRRTL/) based on the implicated pass
and modified source files, reducing turnaround time from minutes to ~3-5 seconds.
"""
from __future__ import annotations

import os
import re
from typing import Sequence


_KNOWN_DIALECTS = [
    "arc", "calyx", "chirrtl", "comb", "emit", "esi", "fsm", "handshake",
    "hw", "hwarith", "ibex", "llhd", "moore", "om", "pipeline", "seq",
    "ssp", "sv", "systemc", "verif", "firrtl"
]

_KNOWN_CONVERSIONS = [
    "CombToSMT", "ExportVerilog", "FIRRTLToHW", "HWToLLHD", "HWToSMT",
    "HWToSystemC", "HandshakeToHW", "LLHDToLLVM", "MooreToCore", "SeqToSV",
    "StandardToHandshake"
]


def map_source_to_test_targets(source_path: str) -> list[str]:
    """Map a C++ source file path (lib/... or include/...) to its corresponding lit test directory."""
    if not source_path:
        return []

    norm = source_path.replace("\\", "/")
    
    # 1. lib/Conversion/X/... -> test/Conversion/X/
    m_conv = re.search(r"(?:lib|include/circt)/Conversion/([^/]+)", norm, re.IGNORECASE)
    if m_conv:
        conv_name = m_conv.group(1)
        return [f"test/Conversion/{conv_name}"]

    # 2. lib/Dialect/X/... -> test/Dialect/X/
    m_dialect = re.search(r"(?:lib|include/circt)/Dialect/([^/]+)", norm, re.IGNORECASE)
    if m_dialect:
        dialect_name = m_dialect.group(1)
        return [f"test/Dialect/{dialect_name}"]

    # 3. tools/firtool/... -> test/firtool/ + test/Dialect/FIRRTL/
    if "firtool" in norm.lower():
        return ["test/firtool", "test/Dialect/FIRRTL"]

    # 4. tools/circt-opt/... -> test/circt-opt/
    if "circt-opt" in norm.lower():
        return ["test/circt-opt"]

    # 5. tools/circt-lec/... -> test/circt-lec/
    if "circt-lec" in norm.lower():
        return ["test/circt-lec"]

    return []


def map_pass_to_test_targets(pass_name: str) -> list[str]:
    """Map a pass name or flag (e.g. 'lower-firrtl-to-hw') to targeted lit test directories."""
    if not pass_name:
        return []

    p = pass_name.lower().strip().lstrip("-")

    # Check conversions
    if "to" in p:
        for conv in _KNOWN_CONVERSIONS:
            if conv.lower() in p.replace("-", ""):
                return [f"test/Conversion/{conv}"]

    # Check dialects
    for d in _KNOWN_DIALECTS:
        if d in p:
            # Capitalize standard dialect names if needed
            d_name = "FIRRTL" if d == "firrtl" else (d.upper() if d in ("hw", "sv", "om", "fsm", "llhd") else d.capitalize())
            return [f"test/Dialect/{d_name}"]

    return []


def get_sliced_lit_targets(
    implicated_pass: str | None = None,
    modified_files: Sequence[str] | None = None,
    default_targets: Sequence[str] | None = None
) -> list[str]:
    """Calculate the minimal set of lit test directories covering the bug and touched files.
    
    Falls back to default_targets if no specific dialect/conversion can be mapped.
    """
    targets: set[str] = set()

    if modified_files:
        for f in modified_files:
            for t in map_source_to_test_targets(f):
                targets.add(t)

    if implicated_pass:
        for t in map_pass_to_test_targets(implicated_pass):
            targets.add(t)

    if targets:
        return sorted(list(targets))

    if default_targets:
        return list(default_targets)

    # Safe fallback
    return ["test/Dialect/FIRRTL"]
