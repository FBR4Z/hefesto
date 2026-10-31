MODULE HandshakeClp
    ! Modelo do Hefesto: troca de sinais com o CLP (pedido, confirmação, fim).
    ! O CLP pede um programa (grupo de entradas), o robô confirma, executa e avisa.
    ! Troque os nomes pelos sinais da célula (veja /io no Hefesto).

    VAR num nPrograma:=0;

    PROC AtendeClp()
        ! WaitDI diPedido,1;            ! CLP pede
        ! nPrograma:=GInput(giPrograma); ! qual programa
        ! SetDO doConfirma,1;           ! robô confirma
        ! WaitDI diPedido,0;            ! CLP baixa o pedido
        ! SetDO doConfirma,0;
        TEST nPrograma
        CASE 1:
            ! Programa1;
        CASE 2:
            ! Programa2;
        DEFAULT:
            TPWrite "Programa desconhecido pedido pelo CLP: "\Num:=nPrograma;
        ENDTEST
        ! PulseDO\PLength:=0.5,doFim;   ! avisa o fim
    ENDPROC
ENDMODULE
