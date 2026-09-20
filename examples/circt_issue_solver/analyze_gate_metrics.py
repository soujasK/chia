"""Phase-5 reporting: turn issues.db into the three numbers the proposal promised.

Read-only, stdlib ``sqlite3`` only (no ray / SQLiteNode) -- run it on the head
after a validation run:

    python analyze_gate_metrics.py [issues.db] [--md]

Reads:
  * gate_metrics  -- one row per issue that reached the Gate (gate_enabled=1),
                     plus one per --no-gate baseline run (gate_enabled=0).
  * attempts      -- status + notes + (antigravity/opencode only) llm_usage,
                     read from the artifact verdict.json when present.

Emits:
  1. Context size delivered to the Fix agent, gated vs. raw baseline, per issue.
  2. Fix-agent cost/latency vs. the paper's ungated Table 6 ($3.01 Fix).
  3. Cascade breakdown: fast-path / Localize / Reduce / Semantic-Reduce shares.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

# Paper Table 6 -- real per-stage $ from the ungated S5.5 run (Fix dominates,
# which is the stage the Gate targets).
PAPER_TABLE6 = {"assess": 0.40, "reproduce": 0.68, "fix": 3.01, "writeup": 0.09}


def _rows(conn, sql, args=()):
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    except sqlite3.OperationalError as e:
        print(f"  (skipped: {e})", file=sys.stderr)
        return []


def _pct(n, d):
    return f"{100.0 * n / d:.0f}%" if d else "n/a"


def _fix_usage_from_artifacts(attempt_row: dict):
    """Pull the 'fix' phase's token/cost usage out of the artifact verdict.json
    (only present for backends that report it: antigravity, opencode)."""
    art = attempt_row.get("artifact_dir") or ""
    vj = os.path.join(art, "verdict.json")
    if not os.path.isfile(vj):
        return None
    try:
        d = json.load(open(vj))
    except Exception:
        return None
    return (d.get("llm_usage") or {}).get("fix")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db", nargs="?", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "issues.db"))
    ap.add_argument("--md", action="store_true", help="emit GitHub-Markdown tables")
    args = ap.parse_args()

    if not os.path.isfile(args.db):
        sys.exit(f"no such db: {args.db} (run the flow first)")
    conn = sqlite3.connect(args.db)

    gm = _rows(conn, "SELECT * FROM gate_metrics ORDER BY issue_number, id")
    at = {r["issue_number"]: r for r in _rows(
        conn, "SELECT * FROM attempts ORDER BY id")}   # last attempt per issue wins

    gated = {r["issue_number"]: r for r in gm if r.get("gate_enabled")}
    baseline = {r["issue_number"]: r for r in gm if not r.get("gate_enabled")}

    if not gm:
        sys.exit("gate_metrics is empty -- no gated run recorded yet.")

    bar = "| --- " if args.md else ""
    H = (lambda *c: print("| " + " | ".join(c) + " |\n|" + "|".join([" --- "] * len(c)) + "|")) \
        if args.md else (lambda *c: print("  ".join(f"{x:>16}" for x in c)))
    R = (lambda *c: print("| " + " | ".join(str(x) for x in c) + " |")) \
        if args.md else (lambda *c: print("  ".join(f"{str(x):>16}" for x in c)))

    # ---- 1. context size, gated vs raw ------------------------------------
    print("\n## 1. Context size delivered to the Fix agent (MLIR op count)\n")
    H("issue", "raw", "gated", "reduction", "vs --no-gate")
    tot_raw = tot_final = 0
    for n in sorted(gated):
        g = gated[n]
        raw = g.get("raw_ir_op_count")
        fin = g.get("final_op_count") if g.get("status") == "ok" else None
        b = baseline.get(n, {}).get("raw_ir_op_count")
        red = _pct(raw - fin, raw) if (raw and fin is not None) else "n/a"
        R(n, raw if raw is not None else "-", fin if fin is not None else "-",
          red, b if b is not None else "-")
        if raw and fin is not None:
            tot_raw += raw
            tot_final += fin
    if tot_raw:
        R("TOTAL", tot_raw, tot_final, _pct(tot_raw - tot_final, tot_raw), "")

    # ---- 2. fix cost/latency vs paper -----------------------------------
    print("\n## 2. Fix-agent cost / latency vs. paper Table 6 (ungated Fix = "
          f"${PAPER_TABLE6['fix']:.2f})\n")
    any_usage = False
    H("issue", "fix $ (gated)", "fix tokens", "note")
    fix_issues = [n for n in sorted(gated)
                  if gated[n].get("status") in ("ok", "verification_failed")]
    for n in fix_issues:
        u = _fix_usage_from_artifacts(at.get(n, {}))
        if u:
            any_usage = True
            R(n, u.get("cost_usd", u.get("total_cost", "?")),
              u.get("total_tokens", u.get("tokens", "?")), "")
        else:
            R(n, "-", "-", "backend reports no per-phase usage")
    if not any_usage:
        print("\n  The Claude backend does not emit per-phase token/cost. Use the "
              "context-size reduction in section 1 as the proxy for Fix-stage "
              "spend: Fix cost scales with prompt+tool context, and the Gate cuts "
              f"that by the section-1 TOTAL % while Table 6 puts ungated Fix at "
              f"${PAPER_TABLE6['fix']:.2f} (vs. Assess ${PAPER_TABLE6['assess']:.2f} "
              f"/ Reproduce ${PAPER_TABLE6['reproduce']:.2f} / Writeup "
              f"${PAPER_TABLE6['writeup']:.2f}). Re-run with --backend antigravity "
              "or opencode for real per-phase $.")

    # ---- 3. cascade breakdown -----------------------------------------
    print("\n## 3. Cascade breakdown (gated issues that reached the Gate)\n")
    reached = [g for g in gated.values() if g.get("status") in ("ok", "verification_failed")]
    d = len(reached) or 1
    fast = sum(1 for g in reached if g.get("fast_path"))
    loc = [g for g in reached if not g.get("fast_path")]
    loc_crash = sum(1 for g in loc if g.get("localize_stage") == "crash")
    loc_misc = sum(1 for g in loc if g.get("localize_stage") == "miscompile")
    loc_skip = sum(1 for g in loc if g.get("localize_stage") == "skipped")
    stalled = sum(1 for g in loc if g.get("reduce_stalled"))
    sem = sum(1 for g in reached if g.get("semantic_reduce_used"))
    vfail = sum(1 for g in gated.values() if g.get("status") == "verification_failed")
    H("stage", "count", "share")
    R("reached Gate", len(reached), "")
    R("fast path (skipped Reduce)", fast, _pct(fast, d))
    R("needed Localize+Reduce", len(loc), _pct(len(loc), d))
    R("  localize: crash", loc_crash, _pct(loc_crash, d))
    R("  localize: miscompile", loc_misc, _pct(loc_misc, d))
    R("  localize: skipped", loc_skip, _pct(loc_skip, d))
    R("circt-reduce stalled -> Semantic-Reduce Agent", sem, _pct(sem, d))
    R("verification_failed (fell back, no repro)", vfail, _pct(vfail, len(gated) or 1))

    # ---- outcome bucket vs paper ------------------------------------------
    print("\n## Outcome vs. paper bucket (needs validation_set.bucket_of at run time)\n")
    for n in sorted(at):
        r = at[n]
        print(f"  #{n:<6} status={r.get('status'):<12} notes={(r.get('notes') or '')[:80]}")


if __name__ == "__main__":
    main()
