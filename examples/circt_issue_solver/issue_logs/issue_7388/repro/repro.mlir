firrtl.circuit "MatchInline" {
  firrtl.module @MatchInline() {
    %c0_ui0 = firrtl.constant 0 : !firrtl.uint<0>
    %1 = firrtl.enumcreate None(%c0_ui0) : (!firrtl.uint<0>) -> !firrtl.enum<Some: uint<8>, None: uint<0>>
  }
}
