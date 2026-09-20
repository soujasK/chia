"""Ground-truth validation set for the Context-Precision Gate.

The 16 CIRCT issues from Table 5 of the CHIA paper (arXiv:2606.27350, S5.5),
with the bucket the paper's run put each in.  Phase 4 runs the gated pipeline
over these and checks each lands in the same bucket; a mismatch is investigated
(CIRCT has moved on since the paper -- e.g. an issue fixed upstream after
firtool-1.148.0 will now be ``no_repro``).

Relocated verbatim from the standalone ``src/circt_fix_loop.py`` scaffold's
``__main__`` (its ``VALIDATION_SET``), which this example supersedes.
"""
from __future__ import annotations

#: issue number -> paper bucket
VALIDATION_SET: dict[int, str] = {
    2266:  "not_a_bug", 2669:  "not_a_bug", 4396:  "not_a_bug",
    7127:  "not_a_bug", 10571: "not_a_bug",

    4649:  "fix_unclear", 5626: "fix_unclear", 7531: "fix_unclear", 8508: "fix_unclear",

    5789:  "already_fixed_upstream", 6226: "already_fixed_upstream",

    4354:  "fixed_no_pr_good_first_issue", 6740: "fixed_no_pr_good_first_issue",

    7388:  "pr_merged", 7949: "pr_merged", 10104: "pr_merged",
}

#: The 3 simplest confirmed bug -> fixed -> merged issues -- run these first,
#: fast-path only, before trusting the cascade on anything else.
FAST_SET = (7388, 7949, 10104)

PAPER_BUCKETS = {
    "not_a_bug", "fix_unclear", "already_fixed_upstream",
    "fixed_no_pr_good_first_issue", "pr_merged",
}


def bucket_of(res: dict, labels: list[str] | None = None) -> str:
    """Map a ``run_issue_remote`` result dict to a Table-5 bucket.

    run_issue_remote statuses (issue_task.py:274 + the early returns):
      not_a_bug / unclear / no_repro / fixed / attempted / error
    *labels* is the issue's GitHub label list (the Phase-4 runner has the
    GithubIssue) -- used only to split a clean ``fixed`` into the good-first-
    issue carve-out the paper honored (docs/case-studies/circt-issue-solving.rst:
    PRs were NOT submitted for 'good first issue'-labelled issues).
    """
    status = (res or {}).get("status")
    if status == "not_a_bug":
        return "not_a_bug"
    if status == "unclear":
        return "fix_unclear"
    if status == "no_repro":
        # Reproduced-clean on the pinned tree == fixed upstream since the paper.
        return "already_fixed_upstream"
    if status == "fixed":
        low = [l.lower() for l in (labels or [])]
        if "good first issue" in low:
            return "fixed_no_pr_good_first_issue"
        return "pr_merged"
    if status == "attempted":
        return "attempted"          # not a paper bucket -- needs investigation
    return status or "error"


def matches_paper(issue_number: int, res: dict,
                  labels: list[str] | None = None) -> bool:
    """True iff *res* lands in the same bucket the paper reported for the issue."""
    return bucket_of(res, labels) == VALIDATION_SET.get(issue_number)
