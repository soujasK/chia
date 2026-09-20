"""Symbolic Fault Localizer for CIRCT Compiler Issues.

Parses crash logs, stack traces, and assertion failures from CIRCT tools
(circt-opt, firtool, etc.), identifies the failing C++ source file and line,
and extracts a localized code slice around the fault point.
"""
from __future__ import annotations

import os
import re
from typing import Any


# Patterns matching assertion failures and compiler crashes in LLVM/CIRCT
_ASSERTION_PATTERNS = [
    # circt-opt: /path/to/file.cpp:123: void func(): Assertion `cond' failed.
    re.compile(r"(?:[a-zA-Z0-9_\-\.\/]+):\s*([^\s:]+\.(?:cpp|h|inc)):(\d+):.*?Assertion [`']([^']+)[`'] failed", re.IGNORECASE),
    # /path/to/file.cpp:123: fatal error: ...
    re.compile(r"([^\s:]+\.(?:cpp|h|inc)):(\d+):\s*(?:fatal error|error):\s*(.+)", re.IGNORECASE),
    # LLVM ERROR: ... at /path/to/file.cpp:123
    re.compile(r"LLVM ERROR:\s*(.+?)\s+at\s+([^\s:]+\.(?:cpp|h|inc)):(\d+)", re.IGNORECASE),
]

# Stack trace frames: #12 0x... in func() at /path/to/file.cpp:123:45
_STACKTRACE_FRAME_PATTERNS = [
    re.compile(r"#\d+\s+0x[0-9a-fA-F]+\s+(?:in\s+)?([^\(]+(?:\([^\)]*\))?)\s+(?:at\s+)?([^\s:]+\.(?:cpp|h|inc)):(\d+)", re.IGNORECASE),
    re.compile(r"at\s+([^\s:]+\.(?:cpp|h|inc)):(\d+)(?::\d+)?", re.IGNORECASE),
]

# Pass failure indicator: e.g. "Failure running pass 'lower-firrtl-to-hw'" or "pass firrtl-infer-widths failed"
_PASS_FAILURE_PATTERNS = [
    re.compile(r"pass\s+['\"]?([a-zA-Z0-9_\-]+)['\"]?\s+failed", re.IGNORECASE),
    re.compile(r"pipeline:\s*([a-zA-Z0-9_\-]+)", re.IGNORECASE),
    re.compile(r"Running pass\s+['\"]?([a-zA-Z0-9_\-]+)['\"]?", re.IGNORECASE),
    re.compile(r"--([a-zA-Z0-9_\-]+)", re.IGNORECASE),
]


def _normalize_circt_path(raw_path: str, circt_src_dir: str) -> str:
    """Normalize a path from compiler logs to a relative or target path in circt_src_dir."""
    clean = raw_path.strip().strip("'\"()[]")
    
    # Check if it contains CIRCT source tree subpaths
    for marker in ("lib/", "include/circt/", "tools/", "frontends/"):
        idx = clean.find(marker)
        if idx != -1:
            rel = clean[idx:]
            full = os.path.join(circt_src_dir, rel)
            if os.path.exists(full):
                return full
            return rel

    if os.path.isabs(clean):
        if os.path.exists(clean):
            return clean
        # If absolute inside container like /workspace/circt/lib/...
        rel = os.path.relpath(clean, "/workspace/circt") if clean.startswith("/workspace/circt") else clean
        full = os.path.join(circt_src_dir, rel)
        if os.path.exists(full):
            return full
        return clean

    candidate = os.path.join(circt_src_dir, clean)
    if os.path.exists(candidate):
        return candidate
    return clean


def extract_code_slice(file_path: str, line_num: int, context_lines: int = 15) -> str | None:
    """Extract context_lines before and after line_num in file_path with line numbers."""
    if not os.path.exists(file_path) or not os.path.isfile(file_path):
        return None
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return None

    if not lines:
        return None

    total = len(lines)
    # line_num is 1-indexed
    target_idx = line_num - 1
    if target_idx < 0 or target_idx >= total:
        return None

    start_idx = max(0, target_idx - context_lines)
    end_idx = min(total, target_idx + context_lines + 1)

    formatted = []
    for i in range(start_idx, end_idx):
        curr_line_num = i + 1
        marker = ">>" if curr_line_num == line_num else "  "
        line_content = lines[i].rstrip("\r\n")
        formatted.append(f"{marker} {curr_line_num:5d} | {line_content}")

    return "\n".join(formatted)


def find_file_for_pass(pass_name: str, circt_src_dir: str) -> str | None:
    """Heuristic fallback: search for pass implementation files matching pass_name."""
    if not pass_name:
        return None

    pass_clean = pass_name.replace("firrtl-", "").replace("-", "")
    
    # Search common lib directories
    for root, _, files in os.walk(circt_src_dir):
        rel_root = os.path.relpath(root, circt_src_dir)
        if not (rel_root.startswith("lib") or rel_root.startswith("include")):
            continue
        for f in files:
            if not f.endswith((".cpp", ".h")):
                continue
            f_clean = f.lower().replace(".", "").replace("_", "")
            if pass_clean in f_clean or f_clean in pass_clean:
                return os.path.join(root, f)
    return None


def localize_fault(repro_output: str, circt_src_dir: str = "/workspace/circt",
                   implicated_pass: str | None = None) -> dict[str, Any]:
    """Extract fault localization info from reproducer log output and source tree."""
    result: dict[str, Any] = {
        "found": False,
        "file_path": None,
        "relative_path": None,
        "line_number": None,
        "assertion_msg": None,
        "function_name": None,
        "failing_pass": implicated_pass,
        "code_slice": None,
        "summary": "No fault location identified.",
    }

    if not repro_output:
        return result

    # 1. Look for explicit assertion failures
    for pat in _ASSERTION_PATTERNS:
        m = pat.search(repro_output)
        if m:
            groups = m.groups()
            if len(groups) == 3 and groups[0].endswith((".cpp", ".h", ".inc")):
                raw_file, raw_line, msg = groups[0], groups[1], groups[2]
            elif len(groups) == 3 and groups[1].endswith((".cpp", ".h", ".inc")):
                msg, raw_file, raw_line = groups[0], groups[1], groups[2]
            else:
                raw_file, raw_line = groups[0], groups[1]
                msg = groups[2] if len(groups) > 2 else "Assertion failure"

            try:
                line_num = int(raw_line)
            except ValueError:
                line_num = None

            norm_path = _normalize_circt_path(raw_file, circt_src_dir)
            rel_path = os.path.relpath(norm_path, circt_src_dir) if os.path.isabs(norm_path) else norm_path

            result.update({
                "found": True,
                "file_path": norm_path,
                "relative_path": rel_path,
                "line_number": line_num,
                "assertion_msg": msg.strip(),
                "summary": f"Assertion `{msg.strip()}` failed in {rel_path}:{line_num}",
            })
            if line_num and os.path.exists(norm_path):
                result["code_slice"] = extract_code_slice(norm_path, line_num)
            return result

    # 2. Look for stack trace frames
    frames = []
    for line in repro_output.splitlines():
        for pat in _STACKTRACE_FRAME_PATTERNS:
            m = pat.search(line)
            if m:
                groups = m.groups()
                if len(groups) == 3:
                    fn, fpath, lnum = groups[0].strip(), groups[1], groups[2]
                elif len(groups) == 2:
                    fn, fpath, lnum = None, groups[0], groups[1]
                else:
                    continue
                try:
                    line_num = int(lnum)
                except ValueError:
                    line_num = None

                norm_path = _normalize_circt_path(fpath, circt_src_dir)
                if "usr/include" in norm_path or "libstdc++" in norm_path:
                    continue
                frames.append((norm_path, line_num, fn))

    for fpath, lnum, fn in reversed(frames):
        if "lib/" in fpath or "include/circt/" in fpath:
            rel_path = os.path.relpath(fpath, circt_src_dir) if os.path.isabs(fpath) else fpath
            result.update({
                "found": True,
                "file_path": fpath,
                "relative_path": rel_path,
                "line_number": lnum,
                "function_name": fn,
                "summary": f"Crash in {rel_path}:{lnum}" + (f" ({fn})" if fn else ""),
            })
            if lnum and os.path.exists(fpath):
                result["code_slice"] = extract_code_slice(fpath, lnum)
            return result

    # 3. Fallback: If implicated pass was given or found, locate pass source file
    pass_name = implicated_pass
    if not pass_name:
        for pat in _PASS_FAILURE_PATTERNS:
            pm = pat.search(repro_output)
            if pm:
                pass_name = pm.group(1)
                break

    if pass_name:
        result["failing_pass"] = pass_name
        pass_file = find_file_for_pass(pass_name, circt_src_dir)
        if pass_file and os.path.exists(pass_file):
            rel_path = os.path.relpath(pass_file, circt_src_dir)
            result.update({
                "found": True,
                "file_path": pass_file,
                "relative_path": rel_path,
                "summary": f"Implicated pass '{pass_name}' mapped to source {rel_path}",
                "code_slice": extract_code_slice(pass_file, 1, context_lines=30),
            })

    return result


def format_fault_slice_for_prompt(fault_info: dict[str, Any]) -> str:
    """Format the localized fault information into a clean prompt block for LLM consumption."""
    if not fault_info or not fault_info.get("found"):
        return ""

    loc = fault_info.get('relative_path') or fault_info.get('file_path') or "unknown"
    if fault_info.get('line_number'):
        loc = f"{loc}:{fault_info['line_number']}"

    lines = [
        "## Localized Fault Slice (High Confidence Root Cause)",
        f"- **Location**: `{loc}`",
    ]
    if fault_info.get("assertion_msg"):
        lines.append(f"- **Failed Assertion / Error**: `{fault_info['assertion_msg']}`")
    if fault_info.get("function_name"):
        lines.append(f"- **Function**: `{fault_info['function_name']}`")
    if fault_info.get("failing_pass"):
        lines.append(f"- **Implicated Pass**: `{fault_info['failing_pass']}`")

    if fault_info.get("code_slice"):
        lines.append("\n```cpp")
        lines.append(fault_info["code_slice"])
        lines.append("```")

    lines.append("\nDirect your fix to this fault location. Avoid scanning irrelevant parts of the codebase.\n")
    return "\n".join(lines)
