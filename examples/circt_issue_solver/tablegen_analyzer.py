"""TableGen and ODS Semantic Introspection Engine for CIRCT Dialects.

Parses CIRCT TableGen (.td) files to extract formal operation definitions,
traits (e.g. SameOperandsAndResultType, Pure, Commutative), argument/result types,
and verifier constraints for neuro-symbolic reasoning.
"""
from __future__ import annotations

import os
import re
from typing import Any


_OP_HEADER_PATTERN = re.compile(
    r"def\s+([a-zA-Z0-9_]+)\s*:\s*[a-zA-Z0-9_]*Op<[\"']([^\"']+)[\"'](?:\s*,\s*\[(.*?)\])?\s*>\s*\{",
    re.DOTALL
)

_TRAIT_PATTERN = re.compile(r"\[(.*?)\]", re.DOTALL)
_ARG_PATTERN = re.compile(r"let\s+arguments\s*=\s*\((?:ins)?(.*?)\);", re.DOTALL)
_RES_PATTERN = re.compile(r"let\s+results\s*=\s*\((?:outs)?(.*?)\);", re.DOTALL)
_SUMMARY_PATTERN = re.compile(r"let\s+summary\s*=\s*[\"']([^\"']+)[\"'];")
_VERIFIER_PATTERN = re.compile(r"let\s+has(?:Custom)?Verifier\s*=\s*1;")


def _extract_op_defs(content: str):
    """Extract (op_class, op_mnemonic, traits_raw, body) from TableGen content,
    correctly skipping multiline string blocks [{ ... }] and nested braces.
    """
    for match in _OP_HEADER_PATTERN.finditer(content):
        op_class = match.group(1)
        op_mnemonic = match.group(2)
        traits_raw = match.group(3) or ""
        body_start = match.end()

        # Scan for matching closing brace while ignoring [{ ... }] blocks
        depth = 1
        i = body_start
        length = len(content)
        while i < length and depth > 0:
            if content[i:i+2] == "[{":
                close_bracket = content.find("}]", i + 2)
                if close_bracket != -1:
                    i = close_bracket + 2
                    continue
                else:
                    break
            elif content[i] == "{":
                depth += 1
            elif content[i] == "}":
                depth -= 1
                if depth == 0:
                    body = content[body_start:i]
                    yield op_class, op_mnemonic, traits_raw, body
                    break
            i += 1


def find_tablegen_files(dialect: str, circt_src_dir: str = "/workspace/circt") -> list[str]:
    """Find all .td files for a given dialect or conversion under include/circt/."""
    if not os.path.exists(circt_src_dir):
        return []

    td_files = []
    # Check include/circt/Dialect/<dialect>
    d_dir = os.path.join(circt_src_dir, "include", "circt", "Dialect", dialect)
    if os.path.isdir(d_dir):
        for root, _, files in os.walk(d_dir):
            for f in files:
                if f.endswith(".td"):
                    td_files.append(os.path.join(root, f))

    # Also check case-insensitive match (e.g. firrtl vs FIRRTL)
    if not td_files:
        base_d = os.path.join(circt_src_dir, "include", "circt", "Dialect")
        if os.path.isdir(base_d):
            for d in os.listdir(base_d):
                if d.lower() == dialect.lower():
                    target = os.path.join(base_d, d)
                    for root, _, files in os.walk(target):
                        for f in files:
                            if f.endswith(".td"):
                                td_files.append(os.path.join(root, f))

    return sorted(td_files)


def parse_tablegen_op(full_op_name: str, circt_src_dir: str = "/workspace/circt") -> dict[str, Any] | None:
    """Parse formal TableGen definition for a dialect op (e.g. 'firrtl.subfield' or 'comb.add')."""
    if not full_op_name:
        return None

    if "." in full_op_name:
        dialect, _, mnemonic = full_op_name.partition(".")
    else:
        dialect, mnemonic = "FIRRTL", full_op_name

    td_files = find_tablegen_files(dialect, circt_src_dir)
    if not td_files:
        return None

    for td_file in td_files:
        try:
            with open(td_file, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            continue

        for op_class, op_mnemonic, traits_raw, body in _extract_op_defs(content):
            if op_mnemonic.lower() == mnemonic.lower() or op_class.lower() == (mnemonic + "op").lower():
                traits = [t.strip() for t in traits_raw.split(",") if t.strip()]

                # Parse arguments and results
                arg_match = _ARG_PATTERN.search(body)
                args = [a.strip() for a in arg_match.group(1).split(",") if a.strip()] if arg_match else []

                res_match = _RES_PATTERN.search(body)
                results = [r.strip() for r in res_match.group(1).split(",") if r.strip()] if res_match else []

                sum_match = _SUMMARY_PATTERN.search(body)
                summary = sum_match.group(1).strip() if sum_match else ""

                has_verifier = bool(_VERIFIER_PATTERN.search(body))

                return {
                    "full_name": f"{dialect.lower()}.{op_mnemonic}",
                    "op_class": op_class,
                    "mnemonic": op_mnemonic,
                    "dialect": dialect,
                    "traits": traits,
                    "arguments": args,
                    "results": results,
                    "summary": summary,
                    "has_verifier": has_verifier,
                    "source_file": os.path.relpath(td_file, circt_src_dir),
                }

    return None


def extract_ops_from_mlir(mlir_text: str) -> list[str]:
    """Extract unique dialect operation names occurring in MLIR text."""
    if not mlir_text:
        return []
    # Matches expressions like firrtl.subfield, hw.module, comb.add
    raw_ops = re.findall(r"\b([a-zA-Z][a-zA-Z0-9_]*\.[a-zA-Z][a-zA-Z0-9_]*)\b", mlir_text)
    seen = set()
    ordered = []
    for op in raw_ops:
        if op not in seen:
            seen.add(op)
            ordered.append(op)
    return ordered


def format_tablegen_context(op_names: list[str], circt_src_dir: str = "/workspace/circt",
                            max_ops: int = 4) -> str:
    """Format formal TableGen definitions into a concise markdown section for the architect model."""
    if not op_names:
        return ""

    parsed_ops = []
    for op in op_names[:max_ops]:
        info = parse_tablegen_op(op, circt_src_dir)
        if info:
            parsed_ops.append(info)

    if not parsed_ops:
        return ""

    lines = [
        "## Formal TableGen / ODS Dialect Specifications",
        "The failing IR operations adhere to the following formal CIRCT definitions and invariants:"
    ]

    for op in parsed_ops:
        lines.append(f"\n### Operation `{op['full_name']}` ({op['op_class']})")
        if op.get("summary"):
            lines.append(f"- **Summary**: {op['summary']}")
        if op.get("traits"):
            lines.append(f"- **Semantic Traits**: `{', '.join(op['traits'])}`")
        if op.get("arguments"):
            lines.append(f"- **Arguments**: `({', '.join(op['arguments'])})`")
        if op.get("results"):
            lines.append(f"- **Results**: `({', '.join(op['results'])})`")
        if op.get("has_verifier"):
            lines.append("- **Verification**: Operation enforces custom verifier invariants in C++.")
        lines.append(f"- **Defined in**: `{op['source_file']}`")

    lines.append("\nEnsure your patch preserves these TableGen traits and verification invariants.\n")
    return "\n".join(lines)
