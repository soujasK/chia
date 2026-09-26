hw.module @test(in %arg0: !dc.token, in %arg1: !dc.token, in %clk: !seq.clock {dc.clock}, in %rst: i1 {dc.reset}, out out0: !dc.value<i32>, out out1: !dc.value<i32>) {
  %0 = dc.merge %arg0, %arg1
  %token, %val = dc.unpack %0 : !dc.value<i1>
  %1 = hw.constant 0 : i32
  %2 = dc.pack %token, %1 : i32
  %token2, %val2 = dc.unpack %2 : !dc.value<i32>
  %3:2 = dc.fork [2] %token2
  %4 = dc.pack %3#0, %val2 : i32
  %5 = dc.pack %3#1, %val2 : i32
  hw.output %4, %5 : !dc.value<i32>, !dc.value<i32>
}
