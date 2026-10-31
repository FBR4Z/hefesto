MODULE PegaEColoca
    ! Modelo do Hefesto: pegar uma peça e colocar em outro lugar.
    ! Ajuste nomes de sinais, ferramenta, obra, alvos e velocidades à célula.
    ! Alvos: grave com /ponto <nome> no Hefesto, com o robô na posição.

    PERS tooldata tGarra:=[TRUE,[[0,0,150],[1,0,0,0]],[1.5,[0,0,60],[1,0,0,0],0,0,0]];
    PERS loaddata lPeca:=[0.8,[0,0,30],[1,0,0,0],0,0,0];
    TASK PERS wobjdata wMesa:=[FALSE,TRUE,"",[[600,0,0],[1,0,0,0]],[[0,0,0],[1,0,0,0]]];

    CONST robtarget pPega:=[[0,0,0],[0,1,0,0],[0,0,0,0],[9E9,9E9,9E9,9E9,9E9,9E9]];
    CONST robtarget pColoca:=[[200,0,0],[0,1,0,0],[0,0,0,0],[9E9,9E9,9E9,9E9,9E9,9E9]];

    CONST speeddata vAproxima:=v1000;
    CONST speeddata vTrabalho:=v200;
    CONST num nAltura:=100;

    PROC CicloPegaEColoca()
        Pega pPega;
        Coloca pColoca;
    ENDPROC

    PROC Pega(robtarget alvo)
        AbreGarra;
        MoveJ Offs(alvo,0,0,nAltura),vAproxima,z50,tGarra\WObj:=wMesa;
        MoveL alvo,vTrabalho,fine,tGarra\WObj:=wMesa;
        FechaGarra;
        GripLoad lPeca;
        MoveL Offs(alvo,0,0,nAltura),vTrabalho,z10,tGarra\WObj:=wMesa;
    ENDPROC

    PROC Coloca(robtarget alvo)
        MoveJ Offs(alvo,0,0,nAltura),vAproxima,z50,tGarra\WObj:=wMesa;
        MoveL alvo,vTrabalho,fine,tGarra\WObj:=wMesa;
        AbreGarra;
        GripLoad load0;
        MoveL Offs(alvo,0,0,nAltura),vTrabalho,z10,tGarra\WObj:=wMesa;
    ENDPROC

    PROC AbreGarra()
        ! Troque pelos sinais da garra (ex.: SetDO doGarraFecha,0;).
        WaitTime 0.3;
    ENDPROC

    PROC FechaGarra()
        ! Troque pelos sinais da garra (ex.: SetDO doGarraFecha,1; WaitDI diGarraFechada,1\MaxTime:=2;).
        WaitTime 0.3;
    ENDPROC
ENDMODULE
