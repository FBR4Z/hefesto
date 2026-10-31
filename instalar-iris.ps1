<#
.SYNOPSIS
    Instala a Íris (e, opcionalmente, os agentes e a voz local) no Windows.

.DESCRIPTION
    - Instala o que faltar: uv, Node.js e git (via winget).
    - Instala a Íris como comando global `iris`.
    - Instala os agentes escolhidos (Claude Code, Codex CLI, Gemini CLI) e seus adaptadores ACP.
    - Detecta GPU NVIDIA e memória e instala a voz na versão adequada:
        com GPU  -> Kokoro + Whisper large-v3-turbo na GPU
        sem GPU  -> Piper + Whisper small no processador (sem as bibliotecas CUDA, ~3 GB a menos)
    - Grava a configuração de voz escolhida em ~/.config/toad/toad.json.

    Pode rodar de dentro do repositório clonado:
        powershell -ExecutionPolicy Bypass -File .\instalar-iris.ps1
    ou direto da internet (aceita os mesmos parâmetros depois do bloco):
        & ([scriptblock]::Create((irm https://raw.githubusercontent.com/FBR4Z/iris/main/instalar-iris.ps1)))

.PARAMETER Agentes
    Lista separada por vírgula: claude, codex, gemini. Vazio pergunta.

.PARAMETER Voz
    desligada, avisos, ditado, ditado_avisos, completa ou pular. Vazio pergunta.

.PARAMETER Modelos
    Pasta com os modelos de voz (pendrive, rede) para instalar sem internet. Vazio baixa
    da release "modelos-voz-v1" do GitHub da Íris (e só em último caso das fontes originais).

.PARAMETER Pasta
    Onde clonar a Íris se o script não estiver dentro do repositório. Padrão: ~\iris

.PARAMETER Simular
    Mostra o que seria feito, sem instalar nada.

.EXAMPLE
    .\instalar-iris.ps1
    .\instalar-iris.ps1 -Agentes claude,gemini -Voz avisos
    .\instalar-iris.ps1 -Simular
    .\instalar-iris.ps1 -Voz avisos -Modelos E:\modelos-voz-v1
#>
param(
    [string]$Agentes = "",
    [string]$Voz = "",
    [string]$Modelos = "",
    [string]$Pasta = (Join-Path $HOME "iris"),
    [switch]$Simular
)

$ErrorActionPreference = "Stop"
$RepoUrl = "https://github.com/FBR4Z/iris.git"

function Titulo([string]$texto) { Write-Host "`n== $texto ==" -ForegroundColor Cyan }
function Info([string]$texto) { Write-Host "   $texto" }
function Ok([string]$texto) { Write-Host "   ✓ $texto" -ForegroundColor Green }
function Aviso([string]$texto) { Write-Host "   ! $texto" -ForegroundColor Yellow }

function Executar([string]$descricao, [scriptblock]$comando) {
    if ($Simular) {
        Write-Host "   [simulação] $descricao" -ForegroundColor DarkGray
        return
    }
    Info $descricao
    $global:LASTEXITCODE = 0
    & $comando
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) {
        throw "Falhou: $descricao (código $LASTEXITCODE)"
    }
}

function Existe([string]$comando) {
    return [bool](Get-Command $comando -ErrorAction SilentlyContinue)
}

function Atualizar-Path {
    $maquina = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $usuario = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$maquina;$usuario;$HOME\.local\bin"
}

function Perguntar([string]$pergunta, [string]$padrao) {
    $resposta = Read-Host "$pergunta [$padrao]"
    if ([string]::IsNullOrWhiteSpace($resposta)) { return $padrao }
    return $resposta.Trim()
}

function Winget([string]$id, [string]$nome) {
    if (-not (Existe "winget")) {
        throw "$nome não encontrado e o winget não está disponível. Instale $nome manualmente e rode de novo."
    }
    Executar "Instalando $nome (winget)" {
        winget install --id $id -e --accept-source-agreements --accept-package-agreements --silent
    }
    Atualizar-Path
}

# ---------------------------------------------------------------- pré-requisitos

Titulo "Pré-requisitos"
Atualizar-Path

if (Existe "uv") { Ok "uv $((uv --version) -replace 'uv ', '')" }
else {
    Executar "Instalando uv" {
        powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    }
    Atualizar-Path
}

if (Existe "node") { Ok "Node.js $(node --version)" }
else { Winget "OpenJS.NodeJS.LTS" "Node.js" }

if (Existe "git") { Ok "git" }
else { Winget "Git.Git" "git" }

# ---------------------------------------------------------------------- repositório

Titulo "Íris"
$Repo = $null
if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot "voz\pyproject.toml"))) {
    $Repo = $PSScriptRoot
    Ok "Usando o repositório em $Repo"
}
elseif (Test-Path (Join-Path $Pasta "voz\pyproject.toml")) {
    $Repo = $Pasta
    Executar "Atualizando $Repo" { git -C $Repo pull --ff-only }
}
else {
    $Repo = $Pasta
    Executar "Baixando a Íris em $Repo" { git clone $RepoUrl $Repo }
}

if (Get-Process -Name "iris", "iris-voz" -ErrorAction SilentlyContinue) {
    $mensagem = "A Íris está aberta. Feche-a antes de instalar (o Windows não deixa substituir programas em uso)."
    if ($Simular) { Aviso $mensagem } else { throw $mensagem }
}

Executar "Instalando o comando iris" { uv tool install --editable $Repo --force }
Executar "Colocando ~\.local\bin no PATH" { uv tool update-shell }
Atualizar-Path

# --------------------------------------------------------------------------- agentes

Titulo "Agentes"
if (-not $Agentes) {
    Info "Quais agentes instalar? (claude, codex, gemini — separados por vírgula, ou 'nenhum')"
    $Agentes = Perguntar "Agentes" "claude,gemini"
}
$lista = $Agentes.ToLower().Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ -and $_ -ne "nenhum" }

foreach ($agente in $lista) {
    switch ($agente) {
        "claude" {
            if (Existe "claude") { Ok "Claude Code já instalado" }
            else { Executar "Instalando Claude Code" { powershell -ExecutionPolicy ByPass -c "irm https://claude.ai/install.ps1 | iex" } }
            Executar "Instalando adaptador ACP do Claude" { npm install -g @agentclientprotocol/claude-agent-acp }
        }
        "codex" {
            if (Existe "codex") { Ok "Codex CLI já instalado" }
            else { Executar "Instalando Codex CLI" { npm install -g @openai/codex } }
        }
        "gemini" {
            if (Existe "gemini") { Ok "Gemini CLI já instalado" }
            else { Executar "Instalando Gemini CLI" { npm install -g @google/gemini-cli } }
        }
        default { Aviso "Agente desconhecido: $agente (ignorado)" }
    }
}
Atualizar-Path

# ------------------------------------------------------------------------------ voz

Titulo "Voz local"
$gpu = $null
if (Existe "nvidia-smi") {
    try { $gpu = (nvidia-smi "--query-gpu=name,memory.total" "--format=csv,noheader" 2>$null | Select-Object -First 1) } catch { }
}
$ramGb = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
if ($gpu) { Ok "GPU: $gpu" } else { Info "GPU NVIDIA: não encontrada (a voz usará o processador)" }
Info "Memória: $ramGb GB"

if ($gpu) { $sugestao = "ditado_avisos" }
elseif ($ramGb -ge 8) { $sugestao = "avisos" }
else { $sugestao = "desligada" }

if (-not $Voz) {
    Info "Modos: desligada | avisos | ditado | ditado_avisos | completa | pular (não instala)"
    Info "Sugestão para esta máquina: $sugestao"
    $Voz = Perguntar "Modo da voz" $sugestao
}
$Voz = $Voz.ToLower()

if ($Voz -ne "pular" -and $Voz -ne "desligada") {
    $excludes = if ($gpu) { "excludes-gpu.txt" } else { "excludes-cpu.txt" }
    $variante = if ($gpu) { "com GPU (~3,5 GB)" } else { "leve, só processador (~0,5 GB)" }
    Executar "Instalando o serviço de voz $variante" {
        uv tool install --editable (Join-Path $Repo "voz") --python 3.13 --excludes (Join-Path $Repo "voz\$excludes") --force
    }
    Atualizar-Path
    $baixar = @("baixar")
    if (-not $gpu) { $baixar += "--cpu"; if ($ramGb -lt 8) { $baixar += @("--stt", "base") } }
    if ($Modelos) { $baixar += @("--de", $Modelos) }
    $origem = if ($Modelos) { $Modelos } else { "GitHub" }
    Executar "Baixando os modelos de voz ($origem)" { iris-voz @baixar }
}

if ($Voz -ne "pular") {
    $config = Join-Path $HOME ".config\toad\toad.json"
    $dados = @{}
    if (Test-Path $config) {
        $json = Get-Content $config -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($propriedade in $json.PSObject.Properties) { $dados[$propriedade.Name] = $propriedade.Value }
    }
    $vozConfig = @{}
    if ($dados.ContainsKey("voz")) {
        foreach ($propriedade in $dados["voz"].PSObject.Properties) { $vozConfig[$propriedade.Name] = $propriedade.Value }
    }
    $vozConfig["modo"] = $Voz
    if (-not $gpu) {
        $vozConfig["motor"] = "piper"
        $vozConfig["ditado_modelo"] = if ($ramGb -ge 8) { "small" } else { "base" }
    }
    $dados["voz"] = $vozConfig
    Executar "Gravando configuração de voz ($Voz) em $config" {
        New-Item -ItemType Directory -Force (Split-Path $config) | Out-Null
        $texto = $dados | ConvertTo-Json -Depth 10
        [IO.File]::WriteAllText($config, $texto, (New-Object Text.UTF8Encoding $false))
    }
}

# ---------------------------------------------------------------------------- fim

Titulo "Pronto"
Info "Reabra o terminal e, se ainda não fez login nos agentes, rode uma vez:"
foreach ($agente in $lista) { Info "   $agente" }
Info ""
Info "Depois, dentro da pasta de um projeto:"
Info "   iris"
if ($Simular) { Aviso "Isto foi uma simulação: nada foi instalado." }
