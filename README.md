# ⚙ Hefesto

Claude, Codex e Gemini no chão de fábrica, com os robôs ABB no contexto.

O Hefesto é um fork da [Íris](https://github.com/FBR4Z/iris) (que é fork do [Toad](https://github.com/batrachianai/toad)) voltado para o ambiente industrial:

- **Robôs ABB no contexto desde o primeiro pedido.** Ao iniciar, o Hefesto lê cada controlador cadastrado (só leitura) e conta para a IA o modo, o estado dos motores, o programa, a velocidade, os eixos e os últimos eventos de cada um.
- **Um servidor MCP por controlador** (`abb-<apelido>`), para a IA nunca mandar um comando para o robô errado. Robôs **reais começam em somente leitura**, e mesmo liberados a IA precisa pedir confirmação para cada ação que mexe na máquina.
- **IRC5 e OmniCore, reais e virtuais**, ao mesmo tempo: cada célula (projeto) tem os seus controladores.
- **Animação robótica** no lugar do olho: braço de 6 eixos que copia a pose do robô real, torre de sinalização (andon) com o estado de cada controlador, flange, radar de eventos, IHM/FlexPendant e wireframe 3D.
- **Voz local** (nada sai da máquina), com a palavra de ativação **"Vulcano"**, que funciona também em máquinas sem placa de vídeo e em redes que bloqueiam o HuggingFace.

Todo o resto (agentes, atalhos, comandos `/`, projetos, shell embutido, uso da assinatura, voz) funciona como na Íris: veja o [README da Íris](https://github.com/FBR4Z/iris#readme).

## Instalação (Windows)

```powershell
git clone https://github.com/FBR4Z/hefesto.git
cd hefesto
powershell -ExecutionPolicy Bypass -File .\instalar-hefesto.ps1 -Robos
```

O instalador:

- instala o que faltar (uv, Node.js, git), o comando `hefesto` e os agentes escolhidos;
- instala a voz na versão certa para a máquina (GPU: Kokoro + Whisper turbo; só processador: Piper + Whisper small) e já baixa os modelos da release [`modelos-voz-v1`](https://github.com/FBR4Z/iris/releases/tag/modelos-voz-v1) da Íris;
- com `-Robos`, baixa e **compila** o [`abbctl`](https://github.com/FBR4Z/abbctl) e o `abbctl-mcp` em `~\abbctl`. Eles são compilados na própria máquina porque usam as DLLs do PC SDK que vêm com o **RobotStudio 2026+**, e a licença da ABB não permite redistribuí-las.

Sem internet: leve a pasta com os modelos de voz num pendrive e use `-Modelos E:\modelos-voz-v1` (veja o README da Íris).

## Robôs

### Requisitos por controlador

| Controlador | Como o Hefesto conversa | Opção necessária no controlador real |
|---|---|---|
| IRC5 (RobotWare 6) | PC SDK (`abbctl`) | **616-1 PC Interface** |
| OmniCore (RobotWare 7) | PC SDK (`abbctl`) | **3119-1 RobotStudio Connect** |
| Virtual (RobotStudio) | PC SDK (`abbctl`) | nenhuma |

### Cadastro

O cadastro fica **só nesta máquina**, em `~\.config\hefesto\hefesto-controladores.json`, nunca no repositório.

```powershell
hefesto robo procurar                              # IRC5 e OmniCore na rede, reais e virtuais
hefesto robo adicionar celula3-solda 192.168.125.1 --real -d "IRB 6700, solda ponto"
hefesto robo adicionar bancada CRB1100_4_47 --virtual   # virtuais: use o nome do sistema
hefesto robo testar celula3-solda                  # o que a IA vai receber (só leitura)
hefesto robo listar
hefesto robo escrita celula3-solda liberar         # pede confirmação; "bloquear" volta atrás
hefesto projeto controladores Celula3 celula3-solda bancada
```

- **Real ou virtual é declarado por você** (`--real`/`--virtual` é obrigatório). A detecção pela rede pode se enganar (VPN, túnel), então o cadastro manda. Se um controlador cadastrado como real disser que é virtual, a IA é avisada.
- Vários controladores virtuais no mesmo PC respondem em `127.0.0.1`. Por isso, cadastre-os pelo **nome do sistema** (o que aparece no `hefesto robo procurar`).
- Um **projeto** (célula) lista os seus controladores. Sem essa lista, vale o cadastro inteiro.

### Na sessão

- **Contexto:** o primeiro pedido leva um resumo de cada controlador da célula. Exemplo:
  `celula3-solda · REAL · IRC5 RobotWare 6.15 · Auto · MotorsOn · programa rodando · velocidade 80%`
- **MCP:** cada controlador ganha o próprio servidor, `abb-<apelido>` (o [`abbctl-mcp`](https://github.com/FBR4Z/abbctl#mcp-server-ai-clients), preso àquele controlador). As regras valem no próprio servidor, não só no que a IA é instruída a fazer:
  - real sem escrita liberada: toda escrita é recusada (parar o programa continua permitido);
  - real com escrita liberada: cada escrita precisa de `confirm=true`, que a IA só manda depois de perguntar a você;
  - virtual: liberado.
- **Painel "Robôs"** na barra lateral: luz, apelido, REAL/VIRTUAL, modo, motores, programa e velocidade de cada controlador, atualizados por eventos (`abbctl monitor`).

## Programar os robôs

### Editar RAPID com segurança

```text
/rapid editar celula3-solda MainModule ia    a IA edita; você revisa e aprova
/rapid editar celula3-solda MainModule eu    você edita (abre no editor); a IA revisa e confirma
/rapid revisar                               diff, estrutura e o que muda o movimento
/rapid aplicar                               gêmeo virtual primeiro, depois o controlador, com confirmação
/rapid voltar [versão]                       carrega uma versão anterior
/rapid historico · /rapid modelo [nome] · /rapid cancelar
```

1. **Editar:** o Hefesto lê o módulo do controlador, guarda no **histórico** e cria uma cópia de trabalho.
   - Em `ia`, o pedido para a IA já vem pronto, e ela é instruída a mexer só na cópia.
   - Em `eu`, a cópia abre no editor (`HEFESTO_EDITOR`, ou o Bloco de Notas).
2. **Revisar:** mostra o diff e aponta erros de estrutura (blocos sem `END...`, parênteses e aspas sem fechar). Também marca o que muda o movimento ou a segurança:
   - 🔴 velocidades e zonas, alvos (com o deslocamento em mm), ferramenta e obra, movimentos novos ou removidos, `AccSet`/`VelSet`/`ConfL`...
   - 🟡 I/O e esperas.

   No modo `eu`, a revisão vai para a IA, que precisa terminar com `VEREDITO: APROVADO` para liberar o passo seguinte.
3. **Aplicar:** exige revisão da versão atual da cópia. Se o controlador tiver **gêmeo virtual**, o módulo vai para ele primeiro. Depois, uma janela de confirmação com os riscos e o carregamento (`prog load --replace`). O programa precisa estar parado.
4. **Desfazer:** `/rapid voltar` lista as versões e carrega a escolhida.

O histórico é um **git local e privado** por controlador, em `~\.local\share\hefesto\rapid\`, que nunca é enviado para lugar nenhum.

### Gêmeo virtual

Restaure o backup do robô real numa estação do RobotStudio, cadastre-a como virtual e ligue as duas:

```powershell
hefesto robo adicionar celula3-gemeo CELULA3_SISTEMA --virtual
hefesto robo gemeo celula3-solda celula3-gemeo
```

### Mais comandos

| Comando | O que faz |
|---|---|
| `/ponto pPega` | grava a posição atual do robô (TCP na obra ativa, com tool e wobj) como `CONST robtarget`. Com uma edição aberta, entra direto na cópia de trabalho; sem ela, vai para a área de transferência |
| `/io celula3 DO_` | sinais e valores, com filtro |
| `/explicar` | a IA explica o último erro, com o texto do próprio controlador (causas e ações) e o trecho do programa onde o ponteiro parou |
| `/ciclo` | último ciclo, média, desvio e um minigráfico; avisa quando o último ciclo foge da média |
| `/rapid modelo` | modelos validados: pega e coloca, paletização, tratamento de erro (`ERROR`, `TRAP`), handshake com o CLP |

```powershell
hefesto robo sinais celula3-solda DO_GARRA DI_PECA   # acompanhados ao vivo
hefesto robo ciclo celula3-solda DO_CICLO            # subida do sinal = novo ciclo (sem sinal: início → fim do programa)
```

O painel **Robôs** mostra também o ponteiro do programa. Paradas de emergência e erros de qualquer controlador aparecem como alerta na tela e são falados, se a voz estiver em modo de avisos. Por voz, só leitura: "Vulcano, status da célula 3", "Vulcano, qual foi o último erro?", "Vulcano, tempo de ciclo".

## Animação

Em **F2 → Hefesto → Animação**:

| Tema | O que mostra |
|---|---|
| **Braço + torre de sinalização** (padrão) | o braço mostra a IA; a torre, o robô (uma torre por controlador, até três) |
| Braço 6 eixos | repouso, pensando, pega e solta a peça, aguardando você, falha; com robô conectado, copia os eixos 2, 3 e 5 |
| Torre de sinalização (andon) | vermelho: emergência ou erro; amarelo: manual ou motores desligados; verde: programa rodando; azul: a IA aguarda você |
| Flange da ferramenta | gira com a atividade; o anel de fora mostra a velocidade do robô (%) |
| Radar de eventos | cada evento do controlador vira um ponto (vermelho: erro; amarelo: aviso) |
| IHM / FlexPendant | a tela mostra a voz e a atividade |
| Braço em wireframe 3D | o braço gira (com robô conectado, pelo eixo 1) |

Os olhos e o arco da Íris continuam disponíveis. Prévia em PNG: `uv run --with pillow python tools/preview_orb.py 36 10 --tema combinado,andon --png temas.png`.

## Voz: "Vulcano"

Diga **"Vulcano"** e o comando ou o texto (por exemplo, "Vulcano, status", "Vulcano, abre o Claude"). Ative em **F2 → Voz → Palavra de ativação**. "Hefesto" não serve como palavra de ativação porque o vigia leve (Vosk) não conhece a palavra; "Vulcano" é o nome romano do mesmo deus.

## Dados da fábrica

O código é público; os dados da fábrica não. Ficam **fora do repositório**, em `~\.config\hefesto` e `~\.local\state\hefesto`:

- `hefesto-controladores.json` (endereços, apelidos, descrições);
- `iris-projetos.json` (células, pastas, descrições);
- logs das sessões.

Programas RAPID, backups e logs de controlador devem ficar nas pastas dos projetos, fora deste repositório.

## Atualizando a partir da Íris

```powershell
git remote add iris https://github.com/FBR4Z/iris.git   # uma vez
git fetch iris
git merge iris/main
```

O que é do Hefesto fica em arquivos próprios (`hefesto_robot.py`, `cli_hefesto.py`, `widgets/hefesto_*.py`), para os merges serem simples.

## Testes

```powershell
uv run pytest
```

Os testes usam um agente falso e um monitor falso (não precisam de RobotStudio nem de cota de modelo).

## Licença e créditos

AGPL-3.0, como a Íris e o Toad. Baseado no [Toad](https://github.com/batrachianai/toad), de Will McGugan, e na [Íris](https://github.com/FBR4Z/iris). ABB, IRC5, OmniCore, RobotStudio e RobotWare são marcas da ABB; este projeto não é afiliado à ABB.
