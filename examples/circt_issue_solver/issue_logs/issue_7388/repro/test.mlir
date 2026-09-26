firrtl.circuit "MatchInline" {
  firrtl.module @MatchInline(in %i: !firrtl.enum<Some: uint<8>, None: uint<0>>, out %o: !firrtl.uint<8>) attributes {convention = #firrtl<convention scalarized>} {
    %c255_ui8 = firrtl.constant 255 : !firrtl.uint<8> {name = "c_out"}
    %c0_ui8 = firrtl.constant 0 : !firrtl.uint<8> {name = "c_in"}
    %c0_ui0 = firrtl.constant 0 : !firrtl.uint<0>
    %0 = firrtl.enumcreate Some(%c0_ui8) : (!firrtl.uint<8>) -> !firrtl.enum<Some: uint<8>, None: uint<0>>
    %1 = firrtl.enumcreate None(%c0_ui0) : (!firrtl.uint<0>) -> !firrtl.enum<Some: uint<8>, None: uint<0>>
    %2 = firrtl.istag %i Some : !firrtl.enum<Some: uint<8>, None: uint<0>>
    %c_out = firrtl.wire : !firrtl.uint<8>
    %3 = firrtl.mux(%2, %c_out, %c255_ui8) : (!firrtl.uint<1>, !firrtl.uint<8>, !firrtl.uint<8>) -> !firrtl.uint<8>
    firrtl.matchingconnect %o, %3 : !firrtl.uint<8>
  }
}
