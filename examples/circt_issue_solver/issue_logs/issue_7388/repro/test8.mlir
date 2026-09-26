firrtl.circuit "MatchInline" {
  firrtl.module @MatchInline() {
    %c0_ui8 = firrtl.constant 0 : !firrtl.uint<8>
    %1 = firrtl.enumcreate Some(%c0_ui8) : (!firrtl.uint<8>) -> !firrtl.enum<Some: uint<8>, None: uint<0>>
  }
}
