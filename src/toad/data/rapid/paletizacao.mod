MODULE Paletizacao
    ! Modelo do Hefesto: montar um palete em grade (colunas x linhas x camadas).
    ! pOrigem é o canto do palete (primeira peça da primeira camada).

    CONST robtarget pOrigem:=[[0,0,0],[0,1,0,0],[0,0,0,0],[9E9,9E9,9E9,9E9,9E9,9E9]];
    CONST num nColunas:=3;
    CONST num nLinhas:=2;
    CONST num nCamadas:=4;
    CONST num nPassoX:=120;
    CONST num nPassoY:=100;
    CONST num nAlturaPeca:=80;

    PERS num nPeca:=0;
    ! Peças já colocadas: persiste entre ciclos e reinícios do programa.

    FUNC robtarget PosicaoPeca(num indice)
        VAR num coluna;
        VAR num linha;
        VAR num camada;
        camada:=indice DIV (nColunas*nLinhas);
        linha:=(indice MOD (nColunas*nLinhas)) DIV nColunas;
        coluna:=indice MOD nColunas;
        RETURN Offs(pOrigem,coluna*nPassoX,linha*nPassoY,camada*nAlturaPeca);
    ENDFUNC

    FUNC bool PaleteCheio()
        RETURN nPeca>=nColunas*nLinhas*nCamadas;
    ENDFUNC

    PROC ColocaNoPalete()
        IF PaleteCheio() THEN
            TPWrite "Palete cheio: troque o palete e zere nPeca.";
            Stop;
        ENDIF
        ! Coloca PosicaoPeca(nPeca);   ! use o Coloca do modelo pega_e_coloca
        nPeca:=nPeca+1;
    ENDPROC
ENDMODULE
