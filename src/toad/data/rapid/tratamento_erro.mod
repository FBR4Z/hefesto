MODULE TratamentoErro
    ! Modelo do Hefesto: espera de sinal com tempo máximo, nova tentativa e
    ! interrupção (TRAP) para um sinal de parada vindo do CLP.

    VAR intnum iParadaClp;
    VAR num nTentativas:=0;
    CONST num nMaxTentativas:=3;

    PROC IniciaInterrupcoes()
        IDelete iParadaClp;
        CONNECT iParadaClp WITH TrataParadaClp;
        ! Troque diParadaClp pelo sinal real.
        ! ISignalDI diParadaClp,1,iParadaClp;
    ENDPROC

    PROC EsperaPecaPresente()
        nTentativas:=0;
        ! Troque diPecaPresente pelo sinal real.
        ! WaitDI diPecaPresente,1\MaxTime:=5;
        WaitTime 0.1;
    ERROR
        IF ERRNO=ERR_WAIT_MAXTIME THEN
            nTentativas:=nTentativas+1;
            IF nTentativas<nMaxTentativas THEN
                TPWrite "Peça não chegou, tentando de novo: "\Num:=nTentativas;
                RETRY;
            ELSE
                TPWrite "Peça não chegou após várias tentativas.";
                RAISE;
            ENDIF
        ENDIF
    ENDPROC

    TRAP TrataParadaClp
        TPWrite "Parada pedida pelo CLP.";
        StopMove;
        ! Espere a liberação do CLP antes de continuar:
        ! WaitDI diParadaClp,0;
        StartMove;
    ENDTRAP
ENDMODULE
