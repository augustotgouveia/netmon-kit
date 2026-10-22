# ============================================================================
#  INSTALAR.ps1 - instala o NetMon Kit num servidor Windows
#
#  Rodar como ADMINISTRADOR, de dentro da pasta do kit:
#     .\INSTALAR.ps1 -Simular        mostra tudo o que faria, sem mudar nada
#     .\INSTALAR.ps1                 instala (ou atualiza)
#     .\INSTALAR.ps1 -Remover        tira tarefas e regras de firewall (os arquivos ficam)
#
#  O que ele faz, nesta ordem:
#     1. confere pre-requisitos (admin, Python, config.json preenchido)
#     2. copia os scripts das pastas 1- a 5- para o destino (guarda copia do que trocar)
#     3. TESTA O TELEGRAM - se a mensagem de teste nao chega, para aqui
#     4. copia a chave de host do roteador (PuTTY) para o perfil do SYSTEM
#     5. cria as tarefas agendadas (SYSTEM, no boot, sem limite de tempo, reinicio automatico)
#     6. libera no firewall so o que precisa, e so para a rede local
#     7. sobe tudo e confere se o painel respondeu
#
#  O que ele NAO faz, de proposito: nao mexe em placa de rede, IP, DNS do
#  Windows nem desliga servico nenhum (licoes 6 e 15 do README).
#
#  Modulos sao ligados conforme o que existir no config/destino:
#     DNS     -> dns.json no destino          Cameras -> go2rtc.exe + go2rtc.yaml
#     HTTPS   -> caddy.exe + Caddyfile         VPS     -> vps.host no config
#     WAN/CRC -> cisco.host + plink            APs     -> lista "aps" no config
# ============================================================================
param(
  [string]$Destino = 'C:\netmon',
  [switch]$Simular,
  [switch]$Remover,
  [switch]$PularTelegram
)
$ErrorActionPreference = 'Stop'
$KIT    = $PSScriptRoot
$GRUPO  = 'NetMon'
$TAREFAS = 'NetMon','NetMon-DNS','NetMon-Caddy','NetMon-Cameras','NetMon-Watchdog',
           'TI-VigiaVPS','TI-SondaWAN2','TI-MedirWAN','TI-VigiaAPs','TI-Syslog'
$LOG = Join-Path $env:TEMP ('netmon-instalador-{0}.log' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))

function Diz($cor, $txt) { Write-Host $txt -ForegroundColor $cor; Add-Content -Path $LOG -Value $txt -Encoding UTF8 }
function Ok($t)    { Diz Green  "  [ok]  $t" }
function Info($t)  { Diz Gray   "        $t" }
function Aviso($t) { Diz Yellow "  [!]   $t" }
function Erro($t)  { Diz Red    "  [X]   $t" }
function Titulo($t){ Diz Cyan   "`n=== $t ===" }
function Faz($descricao, [scriptblock]$bloco) {
  if ($Simular) { Info "faria: $descricao"; return }
  & $bloco; Ok $descricao
}

$ehAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $ehAdmin -and -not $Simular) { Erro 'rode como Administrador (botao direito no PowerShell > Executar como administrador)'; exit 1 }

# ---------------------------------------------------------------- remover
if ($Remover) {
  Titulo 'REMOVENDO tarefas e regras de firewall (arquivos e config ficam)'
  foreach ($n in $TAREFAS) {
    if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {
      Faz "tarefa $n removida" { Stop-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue; Unregister-ScheduledTask -TaskName $n -Confirm:$false }
    }
  }
  Faz "processos do NetMon encerrados" {
    Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -like "*$Destino*" -and $_.Name -match 'python|caddy|go2rtc' } |
      ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  }
  Faz "regras de firewall do grupo $GRUPO removidas" { Get-NetFirewallRule -Group $GRUPO -ErrorAction SilentlyContinue | Remove-NetFirewallRule }
  Info "pasta $Destino mantida. Log: $LOG"
  exit 0
}

# ---------------------------------------------------------------- 1. pre-requisitos
Titulo '1. Pre-requisitos'
$ver = [Environment]::OSVersion.Version
Ok ("Windows {0}.{1} build {2} | PowerShell {3}" -f $ver.Major, $ver.Minor, $ver.Build, $PSVersionTable.PSVersion)
if (-not (Test-Path (Join-Path $KIT '1-nucleo\netmon.py'))) { Erro "rode de dentro da pasta do kit (nao achei 1-nucleo\netmon.py em $KIT)"; exit 1 }

$py = Join-Path $Destino 'python\python.exe'
if (-not (Test-Path $py)) {
  $sys = Get-Command python.exe -ErrorAction SilentlyContinue
  if ($sys -and $sys.Source -notmatch 'WindowsApps') { $py = $sys.Source }
  else { Erro "Python nao encontrado. Ponha o 'Windows embeddable package' do python.org em $Destino\python\ (ou instale o Python para todos os usuarios)"; exit 1 }
}
$pyw = Join-Path (Split-Path $py) 'pythonw.exe'
if (-not (Test-Path $pyw)) { $pyw = $py }
Ok "Python: $py"

$cfgArq = Join-Path $Destino 'config.json'
if (-not (Test-Path $cfgArq)) {
  if ($Simular) { Aviso "sem $cfgArq - na instalacao de verdade o modelo seria copiado e a instalacao pararia para voce preencher" ; $cfg = Get-Content (Join-Path $KIT '0-modelos\config.example.json') -Raw | ConvertFrom-Json }
  else {
    New-Item -ItemType Directory -Force $Destino | Out-Null
    Copy-Item (Join-Path $KIT '0-modelos\config.example.json') $cfgArq
    Aviso "criei $cfgArq a partir do modelo. PREENCHA (no proprio servidor, nunca por chat) e rode de novo."
    exit 2
  }
} else {
  try { $cfg = Get-Content $cfgArq -Raw | ConvertFrom-Json } catch { Erro "config.json invalido: $($_.Exception.Message)"; exit 1 }
  Ok "config.json lido"
}
$cfgTxt = $cfg | ConvertTo-Json -Depth 20
function Preenchido($v) { return ($v -and "$v" -notmatch '<PREENCHER') }
if (-not (Preenchido $cfg.alerts.telegram.token) -or -not (Preenchido $cfg.alerts.telegram.chat_id)) {
  if ($Simular) { Aviso 'Telegram ainda nao preenchido (token/chat_id)' } else { Erro 'preencha alerts.telegram.token e chat_id no config.json'; exit 1 }
}
if (-not (Preenchido $cfg.rede.servidor) -or -not (Preenchido $cfg.rede.lan)) {
  if ($Simular) { Aviso 'rede.servidor / rede.lan nao preenchidos' } else { Erro 'preencha rede.servidor e rede.lan no config.json'; exit 1 }
}
$pendentes = ([regex]::Matches($cfgTxt, '"([^"]+)":\s*"<PREENCHER[^"]*"') | ForEach-Object { $_.Groups[1].Value } | Select-Object -Unique)
if ($pendentes) { Aviso ("ainda com <PREENCHER>: {0} - os modulos que dependem disso ficam desligados" -f ($pendentes -join ', ')) }

# quais modulos ligar
$temCisco = (Preenchido $cfg.cisco.host) -and (Preenchido $cfg.cisco.user) -and (Preenchido $cfg.cisco.password)
$plink = if ($cfg.plink) { $cfg.plink } else { Join-Path $Destino 'plink.exe' }
if ($temCisco -and -not (Test-Path $plink)) { Aviso "roteador configurado mas sem plink.exe em $plink - leitura do roteador desligada"; $temCisco = $false }
$mod = [ordered]@{
  'NetMon'          = $true
  'NetMon-DNS'      = (Test-Path (Join-Path $Destino 'dns.json'))
  'NetMon-Caddy'    = (Test-Path (Join-Path $Destino 'caddy.exe'))
  'NetMon-Cameras'  = (Test-Path (Join-Path $Destino 'go2rtc.exe')) -and (Test-Path (Join-Path $Destino 'go2rtc.yaml'))
  'NetMon-Watchdog' = $true
  'TI-VigiaVPS'     = (Preenchido $cfg.vps.host) -and ("$($cfg.vps.host)" -notmatch '^203\.0\.113\.')
  'TI-SondaWAN2'    = (Preenchido $cfg.rede.modem) -and (Preenchido $cfg.cisco.host)
  'TI-MedirWAN'     = $temCisco
  'TI-VigiaAPs'     = [bool]($cfg.aps | Where-Object { $_.ip })   # @($null).Count vale 1 no PowerShell
  'TI-Syslog'       = $true
}
foreach ($k in $mod.Keys) { Info ("{0,-16} {1}" -f $k, $(if ($mod[$k]) { 'LIGA' } else { 'fica de fora' })) }
if (-not (Test-Path (Join-Path $Destino 'cacert.pem'))) { Aviso "sem $Destino\cacert.pem - no Windows Server 2016 o Telegram falha sem ele (baixe de curl.se/ca/cacert.pem)" }

# ---------------------------------------------------------------- 2. arquivos
Titulo "2. Arquivos -> $Destino"
$bk = Join-Path $Destino ('backup-instalador-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
$copiados = 0; $trocados = 0
foreach ($pasta in Get-ChildItem $KIT -Directory | Where-Object { $_.Name -match '^[1-5]-' }) {
  foreach ($a in Get-ChildItem $pasta.FullName -File) {
    $alvo = Join-Path $Destino $a.Name
    if ((Test-Path $alvo) -and ((Get-FileHash $alvo).Hash -eq (Get-FileHash $a.FullName).Hash)) { continue }
    if (Test-Path $alvo) { $trocados++; if (-not $Simular) { New-Item -ItemType Directory -Force $bk | Out-Null; Copy-Item $alvo $bk } }
    if (-not $Simular) { New-Item -ItemType Directory -Force $Destino | Out-Null; Copy-Item $a.FullName $alvo -Force }
    $copiados++
  }
}
if ($Simular) { Info "faria: copiar $copiados arquivos ($trocados ja existiam e iriam para backup)" }
else { Ok "$copiados arquivos copiados$(if ($trocados) { " ($trocados versoes antigas em $bk)" })" }

# ---------------------------------------------------------------- 3. Telegram
Titulo '3. Teste do Telegram'
if ($PularTelegram -or $Simular) { Info $(if ($Simular) { 'faria: mandar mensagem de teste (a instalacao para se nao chegar)' } else { 'pulado (-PularTelegram)' }) }
else {
  $teste = Join-Path $env:TEMP 'netmon_teste_tg.py'
  @'
import io, json, os, ssl, sys, urllib.request, socket
base = sys.argv[1]
cfg = json.load(io.open(os.path.join(base, "config.json"), encoding="utf-8-sig"))
tg = cfg["alerts"]["telegram"]
ca = os.path.join(base, "cacert.pem")
ctx = ssl.create_default_context(cafile=ca) if os.path.exists(ca) else ssl.create_default_context()
txt = "\u2705 <b>NetMon instalado</b> em %s (%s).\nSe esta mensagem chegou, o servidor consegue te avisar." % (socket.gethostname(), cfg.get("site_name", ""))
r = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % tg["token"],
                           data=json.dumps({"chat_id": tg["chat_id"], "text": txt, "parse_mode": "HTML"}).encode(),
                           headers={"Content-Type": "application/json"})
try:
    print("ENVIADO" if json.load(urllib.request.urlopen(r, timeout=25, context=ctx)).get("ok") else "RECUSADO")
except Exception as e:
    print("FALHOU: %s" % type(e).__name__)
'@ | Set-Content $teste -Encoding UTF8
  $res = (& $py $teste $Destino 2>&1 | Out-String).Trim()
  Remove-Item $teste -ErrorAction SilentlyContinue
  if ($res -ne 'ENVIADO') { Erro "Telegram: $res. Confira token, chat_id, cacert.pem e a saida para a internet. Nada foi instalado alem dos arquivos."; exit 1 }
  Ok 'mensagem de teste enviada - confira no Telegram'
}

# ---------------------------------------------------------------- 4. chave do roteador para o SYSTEM
Titulo '4. Chave de host do roteador (PuTTY) para o SYSTEM'
$origem = 'HKCU:\Software\SimonTatham\PuTTY\SshHostKeys'
$dest   = 'Registry::HKEY_USERS\S-1-5-18\Software\SimonTatham\PuTTY\SshHostKeys'
if (-not $mod['TI-MedirWAN']) { Info 'sem roteador configurado - pulado' }
elseif (-not (Test-Path $origem)) {
  Aviso "nenhuma chave salva para este usuario. Rode UMA vez, aceite a chave e rode o instalador de novo:"
  Info  ("& '{0}' -1 -ssh {1}@{2}" -f $plink, $cfg.cisco.user, $cfg.cisco.host)
} else {
  Faz 'chaves copiadas para o perfil do SYSTEM (sem isso o plink da tarefa falha calado)' {
    New-Item -Path $dest -Force | Out-Null
    (Get-Item $origem).Property | ForEach-Object { Set-ItemProperty -Path $dest -Name $_ -Value (Get-ItemPropertyValue -Path $origem -Name $_) }
  }
}

# ---------------------------------------------------------------- 5. tarefas
Titulo '5. Tarefas agendadas (SYSTEM, sem limite de tempo)'
$quem = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
function Tarefa($nome, $exe, $argumentos, $gatilhos, $limite, $reinicios) {
  if (-not $mod[$nome]) { return }
  $cfgT = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -StartWhenAvailable
  if ($reinicios -gt 0) { $cfgT.RestartCount = $reinicios; $cfgT.RestartInterval = 'PT1M' }
  $cfgT.ExecutionTimeLimit = $limite          # 'PT0S' = sem limite (o padrao mata em 72 h - licao 3)
  $acaoTarefa = New-ScheduledTaskAction -Execute $exe -Argument $argumentos -WorkingDirectory $Destino
  Faz "$nome" {
    Unregister-ScheduledTask -TaskName $nome -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $nome -Action $acaoTarefa -Trigger $gatilhos -Principal $quem -Settings $cfgT -Description 'NetMon Kit' | Out-Null
  }
}
$boot = New-ScheduledTaskTrigger -AtStartup
function Diario($hhmm) { New-ScheduledTaskTrigger -Daily -At $hhmm }
function Cada($min) { $t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $min); $t }
$ps = 'powershell.exe'
Tarefa 'NetMon'          $pyw "`"$Destino\netmon.py`""                                   $boot 'PT0S' 3
Tarefa 'NetMon-DNS'      $pyw "`"$Destino\dns.py`""                                      $boot 'PT0S' 3
Tarefa 'NetMon-Caddy'    "$Destino\caddy.exe" "run --config `"$Destino\Caddyfile`" --adapter caddyfile" $boot 'PT0S' 3
Tarefa 'NetMon-Cameras'  "$Destino\go2rtc.exe" "-config `"$Destino\go2rtc.yaml`""         $boot 'PT0S' 5
Tarefa 'NetMon-Watchdog' $ps "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Destino\watchdog.ps1`"" (Cada 5) 'PT3M' 0
Tarefa 'TI-VigiaVPS'     $py "`"$Destino\vps_monitor.py`" --base `"$Destino`""          @($boot, (Diario '04:10')) 'PT0S' 3
Tarefa 'TI-SondaWAN2'    $ps "-NoProfile -ExecutionPolicy Bypass -File `"$Destino\sonda_wan2.ps1`" -Segundos 10 -Horas 8760" @($boot, (Diario '04:00')) 'PT0S' 3
Tarefa 'TI-MedirWAN'     $ps "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Destino\medir_wan.ps1`"" (Cada 10) 'PT3M' 0
Tarefa 'TI-VigiaAPs'     $ps "-NoProfile -ExecutionPolicy Bypass -File `"$Destino\vigia_aps.ps1`"" @($boot, (Diario '04:05')) 'PT0S' 3
Tarefa 'TI-Syslog'       $pyw "`"$Destino\syslog_rx.py`""                                $boot 'PT0S' 3

# ---------------------------------------------------------------- 6. firewall
Titulo "6. Firewall (so da rede $($cfg.rede.lan))"
function Regra($nome, $proto, $portas, $liga) {
  if (-not $liga) { return }
  Faz "$nome ($proto $($portas -join ','))" {
    Get-NetFirewallRule -DisplayName $nome -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName $nome -Group $GRUPO -Direction Inbound -Action Allow -Protocol $proto -LocalPort $portas -RemoteAddress $cfg.rede.lan -Profile Any | Out-Null
  }
}
Regra 'NetMon painel'        TCP @(8787)       $true
Regra 'NetMon DNS (UDP)'     UDP @(53)         $mod['NetMon-DNS']
Regra 'NetMon DNS (TCP)'     TCP @(53)         $mod['NetMon-DNS']
Regra 'NetMon HTTPS'         TCP @(443)        $mod['NetMon-Caddy']
Regra 'NetMon cameras'       TCP @(1984, 8555) $mod['NetMon-Cameras']
Regra 'NetMon cameras (UDP)' UDP @(8555)       $mod['NetMon-Cameras']
Regra 'NetMon syslog'        UDP @(514)        $mod['TI-Syslog']

# ---------------------------------------------------------------- 7. sobe e confere
Titulo '7. Subindo e conferindo'
if ($Simular) { Info 'faria: iniciar as tarefas e testar http://127.0.0.1:8787/'; Diz Cyan "`nSIMULACAO terminada - nada foi alterado. Log: $LOG"; exit 0 }
foreach ($n in $mod.Keys) { if ($mod[$n]) { Start-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue } }
Start-Sleep -Seconds 20
try { $r = Invoke-WebRequest 'http://127.0.0.1:8787/' -UseBasicParsing -TimeoutSec 15; Ok "painel respondendo (HTTP $($r.StatusCode)) em http://$($cfg.rede.servidor):8787" }
catch { Erro "painel nao respondeu: veja $Destino\netmon.err.log" }
foreach ($n in $mod.Keys) {
  if (-not $mod[$n]) { continue }
  $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
  $i = Get-ScheduledTaskInfo -TaskName $n -ErrorAction SilentlyContinue
  Info ("{0,-16} {1,-8} ultimo resultado {2}" -f $n, $t.State, $i.LastTaskResult)
}
Diz Cyan "`nPronto. Primeira semana: ajuste os limites de alerta pelo que e normal nesta rede. Log: $LOG"
