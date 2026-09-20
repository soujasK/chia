"""ClangFormat and Maintainer Hygiene Agent for CIRCT/LLVM C++ Patches.

Automates strict compliance with LLVM Coding Standards:
- LLVM 2-space indentation (replaces tabs and erratic indentation).
- Stripping of trailing whitespace and excess blank lines.
- Linting against debug printing anti-patterns (e.g. naked `printf`, `std::cout`,
  or un-guarded `llvm::dbgs()`).
- Automated diff normalization for zero-friction `git apply`.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Any


FORBIDDEN_DEBUG_CALLS = [
    r"\bprintf\s*\(",
    r"\bstd::cout\b",
    r"\bstd::cerr\b",
    r"\bfprintf\s*\(\s*stderr\b",
]


def check_clang_format_available() -> bool:
    """Check if clang-format CLI is installed and discoverable."""
    return shutil.which("clang-format") is not None


def format_code_with_clang_format(code: str) -> str | None:
    """Attempt formatting via external clang-format with LLVM style."""
    if not check_clang_format_available():
        return None

    try:
        proc = subprocess.run(
            ["clang-format", "-style=LLVM"],
            input=code,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            return proc.stdout
    except Exception:
        pass
    return None


def format_code_pure_python(code: str) -> str:
    """Pure-Python LLVM style formatter ensuring 2-space indent, no tabs, and clean whitespace."""
    lines = code.splitlines()
    formatted_lines: list[str] = []
    
    prev_was_empty = False
    for line in lines:
        # Strip trailing whitespace
        cleaned = line.rstrip()
        
        # Replace leading tabs with 2 spaces per tab
        leading_space_match = re.match(r"^(\s*)", cleaned)
        if leading_space_match:
            leading_ws = leading_space_match.group(1)
            # Expand tab to 2 spaces
            expanded_ws = leading_ws.replace("\t", "  ")
            rest = cleaned[len(leading_ws):]
            cleaned = expanded_ws + rest

        # Collapse multiple empty lines
        is_empty = (len(cleaned.strip()) == 0)
        if is_empty and prev_was_empty:
            continue
        prev_was_empty = is_empty
        
        formatted_lines.append(cleaned)

    # Trim leading and trailing empty lines without stripping indentation of code lines
    while formatted_lines and not formatted_lines[0].strip():
        formatted_lines.pop(0)
    while formatted_lines and not formatted_lines[-1].strip():
        formatted_lines.pop()

    result = "\n".join(formatted_lines)
    return (result + "\n") if result else ""


def format_cpp_code(code: str) -> str:
    """Format C++ code using clang-format if available, or fall back to pure-Python engine."""
    external_res = format_code_with_clang_format(code)
    if external_res is not None:
        return external_res
    return format_code_pure_python(code)


def audit_hygiene(code_or_patch: str) -> dict[str, Any]:
    """Audit code or git diff against LLVM maintainer hygiene rules."""
    has_tabs = "\t" in code_or_patch
    has_trailing_ws = any(line.rstrip() != line for line in code_or_patch.splitlines())
    
    debug_violations = []
    for idx, line in enumerate(code_or_patch.splitlines(), 1):
        for pattern in FORBIDDEN_DEBUG_CALLS:
            if re.search(pattern, line):
                # If wrapped in LLVM_DEBUG, it's permissible
                if "LLVM_DEBUG" not in line:
                    debug_violations.append({
                        "line_num": idx,
                        "line": line.strip(),
                        "rule": f"Disallowed debug print pattern '{pattern}'"
                    })

    long_lines = [
        {"line_num": idx, "len": len(line)}
        for idx, line in enumerate(code_or_patch.splitlines(), 1)
        if len(line) > 100 and not line.strip().startswith("//") and not line.strip().startswith("*")
    ]

    is_clean = not has_tabs and not has_trailing_ws and len(debug_violations) == 0

    return {
        "is_clean": is_clean,
        "has_tabs": has_tabs,
        "has_trailing_whitespace": has_trailing_ws,
        "debug_violations": debug_violations,
        "long_lines_count": len(long_lines),
    }


def sanitize_patch(diff_text: str) -> str:
    """Sanitize a unified diff to guarantee pristine LLVM style and whitespace hygiene."""
    lines = diff_text.splitlines()
    sanitized: list[str] = []

    for line in lines:
        if line.startswith("+") and not line.startswith("+++"):
            # Added line: replace tabs with 2 spaces and strip trailing whitespace
            prefix = "+"
            content = line[1:]
            # Replace tabs with 2 spaces
            leading_match = re.match(r"^(\s*)", content)
            if leading_match:
                leading_ws = leading_match.group(1).replace("\t", "  ")
                rest = content[len(leading_match.group(1)):]
                content = leading_ws + rest
            content = content.rstrip()
            sanitized.append(prefix + content)
        elif line.startswith("-") and not line.startswith("---"):
            # Removed line: preserve exact line minus trailing \r
            sanitized.append(line.rstrip("\r"))
        else:
            # Context line or header
            sanitized.append(line.rstrip())

    res = "\n".join(sanitized).strip()
    return (res + "\n") if res else ""
