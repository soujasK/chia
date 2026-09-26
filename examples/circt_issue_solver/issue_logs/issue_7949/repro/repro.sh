#!/bin/bash
/workspace/circt/build/bin/circt-opt /workspace/circt/.circtissues/test_minimal.mlir --lower-dc-to-hw > /dev/null
