[FIRRTLToHW] Fix crash lowering aggregate creation with zero-width elements

**Root cause**
During FIRRTL to HW lowering, zero-width values often do not have an associated lowered `Value`, causing `getLoweredValue()` to return null. The lowering implementations for aggregate creation operations (`firrtl.enumcreate`, `firrtl.bundlecreate`, `firrtl.vectorcreate`) assumed that all operands would successfully lower to a valid SSA `Value`. When an operand was zero-width (e.g., an enum variant payload with 0 bits), passing the null `Value` into HW aggregate builders like `hw::UnionCreateOp::create` or `hw::StructCreateOp::create` triggered a crash in MLIR's `IROperand` initialization.

**Fix**
- For `BundleCreateOp` and `VectorCreateOp`, if the entire resulting aggregate is zero-width, we now safely short-circuit and return a null `Value` (which correctly propagates the elision of the zero-width value).
- For `BundleCreateOp`, `VectorCreateOp`, and `FEnumCreateOp`, if any individual operand's lowered value is null (because it is zero-width but part of a non-zero-width aggregate), we explicitly materialize a dummy `hw.constant 0 : i0` at the operation's location to satisfy the HW dialect's requirement that all elements have valid SSA values.
- Formatted a few adjacent lines in `LowerToHW.cpp` using `clang-format`.

**Testing**
The original reproduction using a 0-width enum variant now compiles successfully instead of crashing. Added specific lit tests in `test/Conversion/FIRRTLToHW/zero-width.mlir` to verify that `firrtl.enumcreate`, `firrtl.bundlecreate`, and `firrtl.vectorcreate` operations properly construct their aggregates when given zero-width elements (expecting a materialized `i0` constant).

**Existing-test changes**
No existing tests were modified.

Fixes #7388