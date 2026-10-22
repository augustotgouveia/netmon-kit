# ============================================================================
#  MATA-ZUMBI.ps1
#
#  Corrige as duas causas do laco de DNS nas estacoes da rede:
#    1. Delivery Optimization P2P  (DODownloadMode = 0)
#    2. Deteccao automatica de proxy / WPAD  (nao ha proxy nesta rede)
#
#  CONTEXTO: tres maquinas faziam 92% de TODO o DNS da rede - ate 1 consulta
#  a cada 0,8 segundo, 24h por dia, inclusive fim de semana. client.wns.
#  windows.com sozinho passava de 30 mil/dia por maquina (o normal e dezenas).
#
#  Rode em CADA maquina, como ADMINISTRADOR. Ele grava um relatorio ao lado
#  do proprio script (no pendrive) - assim voce nao precisa anotar nada.
#
#  USO:
#    .\MATA-ZUMBI.ps1              aplica a correcao
#    .\MATA-ZUMBI.ps1 -Simular     so mostra o que faria, nao muda nada
#    .\MATA-ZUMBI.ps1 -Desfazer    volta ao estado anterior
#    .\MATA-ZUMBI.ps1 -MatarWpad   tambem liga a politica DisableWpad=1 (WinHTTP)
#  NUNCA DESATIVAR O SERVICO WinHttpAutoProxySvc: o WcmSvc depende dele e, sem
#  o WcmSvc, a Wi-Fi (WlanSvc) nao sobe - derrubou a Wi-Fi de um notebook no piloto
#  em 15/out/2026 no 1o boot depois.
#                                  (use so se o wpad continuar depois do 1o passe)
# ============================================================================
param(
  [switch]$Simular,
  [switch]$Desfazer,
  [switch]$MatarWpad,
  # MODO do Delivery Optimization. A escolha importa mais do que parece:
  #
  #   99 = download simples, SEM P2P e **sem contatar a nuvem do DO**.
  #        E o unico que PARA as consultas geo/kv*.prod.do.dsp.mp.microsoft.com.
  #        Use nas maquinas doentes - e o laco de DNS que queremos matar.
  #
  #    1 = HTTP + P2P **apenas dentro da rede local** (mesmo NAT).
  #        As maquinas dividem o update entre si em vez de cada uma baixar
  #        da internet, e NAO redistribuem para fora (que foi o pico de
  #        UPLOAD visto no grafico em julho). Use nas maquinas saudaveis.
  #
  #    0 = HTTP sem P2P, mas AINDA fala com a nuvem do DO.
  #        Mata o P2P e NAO mata o ruido de DNS. Era o que eu tinha posto
  #        aqui por engano.
  #
  #    3 = padrao de fabrica em muitas instalacoes: P2P ate pela INTERNET.
  #        E o que causou o problema original.
  [ValidateSet(0,1,2,3,99,100)]
  [int]$Modo = 99
)

$ErrorActionPreference = 'Stop'
$AQUI = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $AQUI) { $AQUI = (Get-Location).Path }

# Grava TUDO que aparece na tela num log ao lado do script. Sem isso, um erro
# que mate o script antes da pausa final some junto com a janela - foi o que
# aconteceu na primeira execucao num notebook do piloto e nao sobrou nada para ler.
$log = Join-Path $AQUI ("LOG-MATA-ZUMBI-$env:COMPUTERNAME.txt")
try { Start-Transcript -Path $log -Force -ErrorAction Stop | Out-Null } catch { }

function Titulo($t) { Write-Host ""; Write-Host ("=" * 68) -ForegroundColor DarkCyan; Write-Host $t -ForegroundColor Cyan; Write-Host ("=" * 68) -ForegroundColor DarkCyan }
function Ok($t)     { Write-Host "  [OK]   $t" -ForegroundColor Green }
function Info($t)   { Write-Host "  .      $t" -ForegroundColor Gray }
function Aviso($t)  { Write-Host "  [!]    $t" -ForegroundColor Yellow }
function Erro($t)   { Write-Host "  [X]    $t" -ForegroundColor Red }
# Read-Host explode quando nao ha console (execucao remota, tarefa agendada).
# Como as pausas so existem para voce ler a tela, ficam opcionais.
function Pausa($t)  { try { Read-Host $t | Out-Null } catch { Write-Host "  ($t)" -ForegroundColor DarkGray } }

# ---------------------------------------------------------------- admin?
$eAdmin = ([Security.Principal.WindowsPrincipal] `
  [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $eAdmin) {
  Erro "Este script precisa rodar como ADMINISTRADOR."
  Write-Host ""
  Write-Host "  Feche esta janela, clique com o botao DIREITO no PowerShell" -ForegroundColor Yellow
  Write-Host "  e escolha 'Executar como administrador'. Depois rode de novo." -ForegroundColor Yellow
  Write-Host ""
  Pausa "  Enter para sair"
  exit 1
}

# ---------------------------------------------------------------- identidade
Titulo "QUEM E ESTA MAQUINA"
$cs  = Get-CimInstance Win32_ComputerSystem
$os  = Get-CimInstance Win32_OperatingSystem
$bios= Get-CimInstance Win32_BIOS
$nome = $env:COMPUTERNAME
$redes = Get-NetIPConfiguration | Where-Object { $_.IPv4Address -and $_.NetAdapter.Status -eq 'Up' }

Info "nome        : $nome"
Info "fabricante  : $($cs.Manufacturer) / $($cs.Model)"
Info "serial      : $($bios.SerialNumber)"
Info "Windows     : $($os.Caption) build $($os.BuildNumber)"
Info "usuario logado: $($cs.UserName)"
foreach ($r in $redes) {
  Info ("rede        : {0} = {1}  (MAC {2})" -f $r.InterfaceAlias, $r.IPv4Address.IPAddress, $r.NetAdapter.MacAddress)
}

# ---------------------------------------------------------------- Warsaw?
# Hipotese de trabalho: TODAS as maquinas no laco tem o modulo bancario
# Warsaw/Topaz instalado (5 de 5). Nao esta provado que ele e a causa -
# ha maquinas com Warsaw e sem laco - mas vale registrar em cada visita.
$warsaw = @()
# Ler a lista de programas de uma vez so quebra quando UMA entrada do registro
# esta malformada: o Get-ItemProperty lanca InvalidCastException, que e
# TERMINANTE e passa por cima do -ErrorAction SilentlyContinue. Foi assim que
# este script morreu num notebook do piloto - sem escrever nada, antes da pausa,
# deixando so uma tela preta que fechou sozinha.
# Por isso agora: uma chave por vez, cada uma dentro do seu proprio try.
foreach ($raiz in 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
                  'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall') {
  foreach ($sub in @(Get-ChildItem $raiz -ErrorAction SilentlyContinue)) {
    try {
      $dn = (Get-ItemProperty $sub.PSPath -Name DisplayName -ErrorAction SilentlyContinue).DisplayName
      if ($dn -and $dn -match 'Warsaw|Topaz|GAS Tecnologia') { $warsaw += $dn }
    } catch { }   # entrada podre no registro: ignora essa, segue as outras
  }
}
$topaz = Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { $_.InterfaceDescription -match 'Topaz' }
if ($warsaw) { Aviso ("Warsaw/Topaz instalado: " + ($warsaw -join ', ')) } else { Info "Warsaw/Topaz: nao encontrado" }
if ($topaz)  { Aviso ("adaptador Topaz presente: " + ($topaz.Name -join ', ')) }

# ---------------------------------------------------------------- estado atual
Titulo "ESTADO ANTES"
$doKey = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\DeliveryOptimization'
$doAntes = $null
if (Test-Path $doKey) { $doAntes = (Get-ItemProperty $doKey -Name DODownloadMode -ErrorAction SilentlyContinue).DODownloadMode }
Info ("DODownloadMode : " + $(if ($null -ne $doAntes) { $doAntes } else { "(nao definido = padrao, P2P LIGADO)" }))

# WPAD e POR USUARIO. Se voce elevar com OUTRA conta, o HKCU seria o do admin
# e a correcao nao pegaria no usuario de verdade. Por isso varremos TODOS os
# perfis carregados em HKEY_USERS, e nao so o HKCU.
if (-not (Get-PSDrive -Name HKU -ErrorAction SilentlyContinue)) {
  New-PSDrive -Name HKU -PSProvider Registry -Root HKEY_USERS -Scope Script | Out-Null
}
$perfis = Get-ChildItem 'HKU:\' -ErrorAction SilentlyContinue |
          Where-Object { $_.PSChildName -match '^S-1-5-21-[\d-]+$' }
Info ("perfis de usuario carregados: " + $perfis.Count)

$estadoWpad = @()
foreach ($p in $perfis) {
  $cx = "HKU:\$($p.PSChildName)\Software\Microsoft\Windows\CurrentVersion\Internet Settings\Connections"
  $isr= "HKU:\$($p.PSChildName)\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
  $dcs = $null; $auto = $null
  if (Test-Path $cx)  { $dcs  = (Get-ItemProperty $cx  -Name DefaultConnectionSettings -ErrorAction SilentlyContinue).DefaultConnectionSettings }
  if (Test-Path $isr) { $auto = (Get-ItemProperty $isr -Name AutoDetect -ErrorAction SilentlyContinue).AutoDetect }
  # byte 8 do blob: bit 0x08 = "Detectar configuracoes automaticamente"
  $ligado = if ($dcs -and $dcs.Length -gt 8) { [bool]($dcs[8] -band 0x08) } else { $null }
  $estadoWpad += [PSCustomObject]@{
    sid = $p.PSChildName; caminho = $cx; blob = $dcs; autoDetect = $auto; wpadLigado = $ligado
  }
  Info ("  perfil {0}: autodetect={1} AutoDetect={2}" -f $p.PSChildName.Substring(0,[Math]::Min(20,$p.PSChildName.Length)),
        $(if ($null -eq $ligado) { "sem config" } elseif ($ligado) { "LIGADO" } else { "desligado" }),
        $(if ($null -ne $auto) { $auto } else { "-" }))
}
$svc = Get-Service WinHttpAutoProxySvc -ErrorAction SilentlyContinue
if ($svc) { Info ("servico WinHttpAutoProxySvc: {0} / {1}" -f $svc.Status, $svc.StartType) }

# ---------------------------------------------------------------- desfazer
$arqUndo = Join-Path $AQUI ("DESFAZER-$nome.json")

if ($Desfazer) {
  Titulo "DESFAZENDO"
  if (-not (Test-Path $arqUndo)) { Erro "Nao achei $arqUndo. Nada a desfazer nesta maquina."; Pausa "  Enter"; exit 1 }
  $u = Get-Content $arqUndo -Raw | ConvertFrom-Json
  if ($null -eq $u.doAntes) {
    if (Test-Path $doKey) { Remove-ItemProperty $doKey -Name DODownloadMode -ErrorAction SilentlyContinue; Ok "DODownloadMode removido (volta ao padrao)" }
  } else {
    New-ItemProperty $doKey -Name DODownloadMode -Value ([int]$u.doAntes) -PropertyType DWord -Force | Out-Null
    Ok "DODownloadMode restaurado para $($u.doAntes)"
  }
  foreach ($w in $u.wpad) {
    if ($w.blob) {
      Set-ItemProperty -Path $w.caminho -Name DefaultConnectionSettings -Value ([byte[]]$w.blob)
      Ok "WPAD restaurado em $($w.sid)"
    }
  }
  if ($u.servicoAntes) {
    Set-Service WinHttpAutoProxySvc -StartupType $u.servicoAntes -ErrorAction SilentlyContinue
    Ok "servico WinHttpAutoProxySvc restaurado para $($u.servicoAntes)"
  }
  Write-Host ""; Ok "Desfeito. Reinicie a maquina."
  Pausa "  Enter"
  exit 0
}

# ---------------------------------------------------------------- aplicar
Titulo $(if ($Simular) { "SIMULACAO - nada sera alterado" } else { "APLICANDO" })

if (-not $Simular) {
  # guarda o estado para poder voltar
  @{
    maquina = $nome; quando = (Get-Date).ToString('o')
    doAntes = $doAntes
    servicoAntes = $(if ($svc) { "$($svc.StartType)" } else { $null })
    wpad = @($estadoWpad | ForEach-Object { @{ sid = $_.sid; caminho = $_.caminho; blob = $(if ($_.blob) { [int[]]$_.blob } else { $null }) } })
  } | ConvertTo-Json -Depth 6 | Set-Content $arqUndo -Encoding UTF8
  Info "estado anterior salvo em $arqUndo"
}

# --- 1. Delivery Optimization ---
$explica = switch ($Modo) {
  99 { "sem P2P e SEM contatar a nuvem do DO - para o ruido de DNS" }
  1  { "P2P so na rede local - economiza internet, nao redistribui para fora" }
  0  { "sem P2P, mas AINDA fala com a nuvem do DO (nao para o ruido de DNS)" }
  2  { "P2P em grupo privado" }
  3  { "P2P ate pela internet (era o problema original)" }
  100{ "DO em bypass" }
}
if ($Simular) {
  Info "faria: DODownloadMode = $Modo em $doKey  ($explica)"
} else {
  if (-not (Test-Path $doKey)) { New-Item -Path $doKey -Force | Out-Null }
  New-ItemProperty -Path $doKey -Name DODownloadMode -Value $Modo -PropertyType DWord -Force | Out-Null
  $conf = (Get-ItemProperty $doKey -Name DODownloadMode).DODownloadMode
  if ($conf -eq $Modo) { Ok "DODownloadMode = $Modo  ($explica)" } else { Erro "gravou $Modo mas leu de volta '$conf'" }
}

# --- 2. WPAD, em todos os perfis ---
foreach ($w in $estadoWpad) {
  if ($null -eq $w.blob) { Info "perfil $($w.sid): sem DefaultConnectionSettings, pulando"; continue }
  if ($w.wpadLigado -eq $false) { Info "perfil $($w.sid): autodetect ja estava desligado" ; continue }
  if ($Simular) { Info "faria: desligar autodetect no perfil $($w.sid)"; continue }
  $novo = [byte[]]$w.blob.Clone()
  $novo[8] = $novo[8] -band (-bnot 0x08)   # limpa o bit do auto-detect
  # o contador nos bytes 4..7 precisa subir para o Windows reler
  $cnt = [BitConverter]::ToUInt32($novo, 4) + 1
  [Array]::Copy([BitConverter]::GetBytes($cnt), 0, $novo, 4, 4)
  Set-ItemProperty -Path $w.caminho -Name DefaultConnectionSettings -Value $novo
  $isr = $w.caminho -replace '\\Connections$', ''
  New-ItemProperty -Path $isr -Name AutoDetect -Value 0 -PropertyType DWord -Force | Out-Null
  $chk = (Get-ItemProperty $w.caminho -Name DefaultConnectionSettings).DefaultConnectionSettings
  if (-not ([bool]($chk[8] -band 0x08))) { Ok "autodetect desligado no perfil $($w.sid)" }
  else { Erro "nao consegui desligar o autodetect em $($w.sid)" }
}

# --- 3. servico do WPAD (opcional) ---
if ($MatarWpad) {
  # NAO desativar o servico (o WcmSvc e a Wi-Fi dependem dele) - so a politica.
  $polKey = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WinHttp'
  if ($Simular) { Info "faria: DisableWpad = 1 em $polKey (servico continua ligado)" }
  else {
    if (-not (Test-Path $polKey)) { New-Item -Path $polKey -Force | Out-Null }
    New-ItemProperty -Path $polKey -Name DisableWpad -Value 1 -PropertyType DWord -Force | Out-Null
    Ok "politica DisableWpad = 1 (o WinHTTP para de procurar wpad; servico mantido)"
    Aviso "se algum dia houver proxy nesta rede, apagar DisableWpad em $polKey"
  }
} else {
  Info "servico WinHttpAutoProxySvc: MANTIDO (use -MatarWpad para ligar a politica DisableWpad)"
}

# ---------------------------------------------------------------- relatorio
$rel = Join-Path $AQUI ("RELATORIO-$nome-$(Get-Date -Format 'yyyyMMdd-HHmm').txt")
$linhas = @(
  "MATA-ZUMBI - relatorio"
  "quando      : $(Get-Date -Format 'dd/MM/yyyy HH:mm:ss')"
  "maquina     : $nome"
  "fabricante  : $($cs.Manufacturer) / $($cs.Model)"
  "serial      : $($bios.SerialNumber)"
  "windows     : $($os.Caption) build $($os.BuildNumber)"
  "usuario     : $($cs.UserName)"
)
foreach ($r in $redes) { $linhas += "rede        : $($r.InterfaceAlias) = $($r.IPv4Address.IPAddress) MAC $($r.NetAdapter.MacAddress)" }
$linhas += "warsaw      : $(if ($warsaw) { $warsaw -join ', ' } else { 'nao' })"
$linhas += "DO antes    : $(if ($null -ne $doAntes) { $doAntes } else { 'nao definido' })"
$linhas += "DO depois   : $(if ($Simular) { 'SIMULACAO' } else { (Get-ItemProperty $doKey -Name DODownloadMode -ErrorAction SilentlyContinue).DODownloadMode })"
foreach ($w in $estadoWpad) { $linhas += "wpad $($w.sid) : antes=$($w.wpadLigado)" }
$linhas += "servico wpad: $(if ($MatarWpad) { 'DESLIGADO' } else { 'mantido' })"
$linhas | Set-Content $rel -Encoding UTF8

Titulo "PRONTO"
Ok "relatorio salvo em: $rel"
if (-not $Simular) { Info "para desfazer nesta maquina: .\MATA-ZUMBI.ps1 -Desfazer" }
Write-Host ""
Write-Host "  >>> REINICIE ESTA MAQUINA <<<" -ForegroundColor Yellow
Write-Host "  O servico de notificacoes so refaz o handshake do zero no boot." -ForegroundColor Gray
Write-Host "  Sem reiniciar, o laco antigo continua rodando ate a proxima vez." -ForegroundColor Gray
Write-Host ""
Pausa "  Enter para fechar"