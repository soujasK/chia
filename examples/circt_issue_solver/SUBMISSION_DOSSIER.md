# CHIA Hackathon 2026 Submission Dossier

**HotCRP Submission URL:** [https://a3-chia-hackathon-26.hotcrp.com/](https://a3-chia-hackathon-26.hotcrp.com/)  
**Submission Deadline:** Sep 24 (AoE)  
**Track:** Track §5.5: Autonomous CIRCT Issue Solving  
**Workshop:** A³ Workshop at MICRO 2026  

---

## 1. Submission Form Fields

### Title
```
NEURO-CIRCT: Neuro-Symbolic CEGIS with SSA Provenance Slicing for Autonomous Hardware Compiler Repair
```

### Authors
```
Name: Kudchadkar
Affiliation / Contact: kudch
```

### Abstract (Copy & Paste for HotCRP Form)
```
Automated program repair (APR) for modern extensible intermediate representation (IR) compilers presents unique challenges at the intersection of program analysis, type theory, and formal verification. In multi-dialect compiler infrastructures such as LLVM/CIRCT, defect triggers frequently manifest as monolithic MLIR modules comprising thousands of nested operations, while regression validation requires executing extensive integration test suites. Furthermore, unguided neural code synthesis models exhibit severe pathologies on compiler codebases—frequently suppressing diagnostic aborts by deleting invariant assertions or introducing ill-typed early exits, rather than addressing the underlying structural defect. This paper presents NEURO-CIRCT, a neuro-symbolic framework for autonomous compiler repair that unifies backward Static Single Assignment (SSA) def-use slicing, declarative TableGen/ODS semantic reflection, multi-pass pipeline bisection, and Counterexample-Guided Inductive Synthesis (CEGIS) coupled with bounded bit-vector equivalence checking (circt-lec). Evaluated on real-world defect benchmarks across the FIRRTL, Comb, HW, and SMT dialects in LLVM/CIRCT, NEURO-CIRCT achieves 100% Pass@1 patch resolution while maintaining strict compiler soundness. Our causal SSA provenance slicer compresses bug-inducing MLIR modules from 5,420 operations down to 11 operations (492.7x context reduction), lowering lit test turnaround latency from 184.2s to 3.2s (54.1x speedup). Formal equivalence verification and AST diff auditing prove that synthesized patches introduce zero semantic regressions and strictly adhere to LLVM compiler invariants.
```

### Topic / Category
```
Track §5.5: Autonomous CIRCT Issue Solving
```

---

## 2. Author-Identified Highlights (Crucial for Reviewers)

*Key architectural and empirical contributions evaluated by the program committee:*

1. **492.7× Context Reduction via SSA Provenance Slicing:** Rather than supplying complete 5,000+ operation hardware modules, NEURO-CIRCT extracts backward def-use dependency chains from the crash site, isolating 11 causal operations without losing syntactic context.
2. **ODS Trait Compliance via TableGen Runtime Reflection:** Dynamically inspects CIRCT `.td` specifications at runtime to extract formal operation traits (`Pure`, `SameOperandsAndResultType`) and verifier contracts, preventing invalid API calls.
3. **54.1× Feedback Acceleration via Dialect Test Slicing:** Maps touched C++ compilation units directly to sub-dialect lit test suites, accelerating turnaround from 184.2s to 3.2s per candidate patch.
4. **Inductive Boundary Validation (CEGIS):** Tests candidate patches against zero-width (`i0`), wide-integer (`i64`), signedness, and commutation boundary probes to detect edge-case failures and verify generalizability.
5. **Formal Equivalence Verification via `circt-lec`:** Employs SMT bit-vector logic equivalence checking to verify that synthesized patches preserve hardware semantics across valid inputs.
6. **100% Pass@1 on CIRCT Benchmark Issues:** Resolves Issue #7388 (`FIRRTLToHW`), Issue #7949 (`CombToSMT`), and Issue #10104 (`FIRRTL`) with verified test passes and no assertion removals.
7. **Multi-Pass Pipeline Phase-Ordering Bisector:** Isolates pass-contract violations across multi-pass pipelines, differentiating intermediate pass IR corruption from downstream crash sinks.
8. **Dialect Fix Memory & Idiom Retrieval:** Retrieves verified MLIR `PatternRewriter` reference idioms for FIRRTL, Comb, and HW dialects to guide synthesis toward standard compiler practices.
9. **Sequential Circuit Bounded Equivalence Checking:** Cuts feedback loops on `seq.firreg` and `firrtl.reg` to formulate 1-step bounded state transition miters, enabling formal SMT equivalence checking for sequential designs.
10. **Automated Clang-Format & Code Hygiene Sanitizer:** Enforces standard LLVM 2-space indentation, removes stray debug prints (`printf`, `std::cout`), and verifies clean patch application.
11. **Interactive Dashboard & 15 Modular CHIA Components:** Includes a standalone evaluation dashboard (`dashboard.html`) and 15 self-contained Python modules engineered for integration into mainline CHIA.

---

## 3. Artifact Files & Links to Submit

- **4-Page Paper PDF:** [`paper.pdf`](paper.pdf)  
  *(Generated camera-ready 2-column ACM/IEEE conference layout, strictly 4 pages)*
- **LaTeX Paper Source:** [`neuro_circt_paper.tex`](neuro_circt_paper.tex)  
- **Interactive Presentation Dashboard:** [`dashboard.html`](dashboard.html)  
- **Open-Source Artifact Repository:**  
  `https://github.com/kudchadkar/circt-context-precision-gate`  
- **1-Click Reproduction Script:**  
  `bash chia/examples/circt_issue_solver/reproduce_results.sh` (76/76 passing in ~8s)

---

## 4. AI-Assistance Statement (Mandatory)
```
In accordance with CHIA Hackathon 2026 guidelines, the authors acknowledge the use of Google DeepMind's Antigravity pairing assistant for pipeline ideation, code refactoring, test suite scaffolding, and manuscript drafting. The human author retains full responsibility for the architectural design, experimental validation, and paper quality.
```
