#!/bin/bash
/workspace/circt/build/bin/circt-opt --lower-firrtl-to-hw /workspace/circt/.circtissues/repro.mlir > /dev/null
