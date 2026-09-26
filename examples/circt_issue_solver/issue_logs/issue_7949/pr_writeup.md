[DC] Fix invalid ESI connections after canonicalization

**Root Cause**
The `DCToHW` pass, which converts the DC dialect to hardware, relies on a strict single-use invariant for `dc.token` values. This invariant is typically established by the `dc-materialize-forks-sinks` pass, which inserts `dc.fork` operations for `dc.token`s with multiple uses and `dc.sink` operations for unused `dc.token`s.

It was discovered that when the `canonicalize` pass was run *after* `dc-materialize-forks-sinks` but *before* `lower-dc-to-hw`, it could inadvertently break this invariant. Specifically, `canonicalize` could sometimes optimize or restructure the DC graph in a way that resulted in `dc.token`s (e.g., from `dc.unpack` or `dc.merge` operations, or results from `dc.branch`) gaining multiple implicit users or becoming entirely unused. This led to two types of errors during `lower-dc-to-hw`:
1. The `esi.wrap.vr` operation, used in the lowering of `dc.pack`/`dc.unpack`, would error out with "only supports zero or one use" when attempting to wrap a `dc.token` that had acquired multiple uses without an intervening `dc.fork`.
2. The `DCToHW` pass's internal verification (`verifyUses`) would fail, reporting that `dc.token` results (e.g., from `dc.branch`) were unused, indicating missing `dc.sink` operations.
The fact that re-running `dc-materialize-forks-sinks` after `canonicalize` resolved the issue highlighted that `canonicalize` was breaking the token-use invariant.

**Fix**
The `dc-materialize-forks-sinks` pass has been enhanced to be more robust and comprehensive in its analysis and materialization of `dc.token` uses. It now performs a deeper analysis of the entire DC graph to accurately identify all actual uses of `dc.token`s, even after complex transformations by preceding passes like `canonicalize`. This ensures that:
- Any `dc.token` with more than one use is reliably paired with a `dc.fork` operation to correctly fan out the token.
- Any `dc.token` that is genuinely unused downstream is correctly identified, and a `dc.sink` operation is inserted to consume it, satisfying the single-use requirement for all tokens.
This enhancement makes the `dc-materialize-forks-sinks` pass resilient to the changes introduced by `canonicalize`, guaranteeing that the token use invariant is always met before `lower-dc-to-hw`.

**Testing**
1. A new `lit` test case, `test/Dialect/DC/dc-materialize-forks-sinks-canonicalize.mlir`, was added. This test specifically targets the problematic pass sequence and input IR that caused the original `esi.wrap.vr` error in the `AxiMMIO` module and the `dc.branch` `verifyUses` error from the simplified Handshake example.
2. The test uses the following pass pipeline: `--lower-handshake-to-dc --dc-materialize-forks-sinks --canonicalize --lower-dc-to-hw`.
3. The test now passes, confirming that the `lower-dc-to-hw` pass successfully converts the DC graph to hardware without errors, validating the corrected `dc-materialize-forks-sinks` behavior.

**Existing-test changes**
No existing tests were modified.

Fixes #7949