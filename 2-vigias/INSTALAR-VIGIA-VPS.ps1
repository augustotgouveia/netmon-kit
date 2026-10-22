# ============================================================================
#  INSTALAR-VIGIA-VPS.ps1 - ativa o vps_monitor.py no servidor do NetMon
#
#  Rodar NO SERVIDOR do NetMon, como administrador, com o vps_monitor.py ja copiado
#  para C:\netmon. Faz, nesta ordem:
#    1. confere python, script, config (token do Telegram) e cacert.pem
#    2. roda o teste: uma medicao de cada coisa + MENSAGEM DE TESTE no Telegram
#       (prova que o servidor consegue te avisar ANTES de virar servico)
#    3. registra a tarefa TI-VigiaVPS como SYSTEM: no boot + todo dia 04:10
#       (rede de seguranca), sem limite de tempo (PT0S), reinicia se cair
#    4. sobe e confere se o processo esta vivo e gravando
#
#  Desfazer:  .\INSTALAR-VIGIA-VPS.ps1 -Remover
#
#  ExecutionTimeLimit PT0S nao e detalhe: o padrao do Windows mata tarefa longa
#  em 72 h -- foi o que derrubava o NetMon a cada 3 dias em julho, e um servico
#  na VPS em agosto.
# ============================================================================
param(
  [string]$Base = 'C:\netmon',
  [switch]$Remover
)
$ErrorActionPreference = 'Stop'
$py     = Join-Path $Base 'python\python.exe'
$script = Join-Path $Base 'vps_monitor.py'
$tarefa = 'TI-VigiaVPS'

function Mata-Processo {
  @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'vps_monitor\.py' }) |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; "  processo $($_.ProcessId) encerrado" }
}

if ($Remover) {
  try { Unregister-ScheduledTask -TaskName $tarefa -Confirm:$false -ErrorAction Stop; "tarefa $tarefa removida" } catch { "tarefa $tarefa nao existia" }
  Mata-Processo
  "Vigia da VPS desligado. Os arquivos ficaram em $Base."
  return
}

"=== 1) conferindo pre-requisitos ==="
foreach ($par in @(@('python', $py), @('vps_monitor.py', $script), @('config.json', (Join-Path $Base 'config.json')))) {
  if (-not (Test-Path $par[1])) { throw "faltando: $($par[0]) em $($par[1])" }
  "  ok  {0}" -f $par[0]
}
"  {0}  cacert.pem (sem ele o TLS do Server 2016 falha com o Telegram)" -f $(if (Test-Path (Join-Path $Base 'cacert.pem')) { 'ok ' } else { 'FALTA' })

"`n=== 2) teste: uma medicao + mensagem de teste no Telegram ==="
$saida = & $py $script --base $Base --teste --telegram 2>&1 | Out-String
$saida -split "`r?`n" | Where-Object { $_.Trim() } | ForEach-Object { "  $_" }
if ($saida -notmatch 'telegram\s+:\s+ENVIADO') {
  throw "o teste NAO conseguiu mandar Telegram - nao instalo um vigia que nao avisa. Ver $Base\vps-monitor.err.log"
}

"`n=== 3) registrando a tarefa $tarefa ==="
try { Unregister-ScheduledTask -TaskName $tarefa -Confirm:$false -ErrorAction SilentlyContinue } catch { }
Mata-Processo
$acao = New-ScheduledTaskAction -Execute $py -Argument "`"$script`" --base `"$Base`"" -WorkingDirectory $Base
$g1   = New-ScheduledTaskTrigger -AtStartup
$g2   = New-ScheduledTaskTrigger -Daily -At '04:10'
$pri  = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$cfg  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
          -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$cfg.ExecutionTimeLimit = 'PT0S'
Register-ScheduledTask -TaskName $tarefa -Action $acao -Trigger $g1,$g2 -Principal $pri -Settings $cfg `
  -Description 'Vigia permanente da VPS de vendas: avisa no Telegram quando cai e quando volta, e conta os tuneis do sistema de vendas' | Out-Null
$t = Get-ScheduledTask -TaskName $tarefa
"  registrada. limite de execucao: {0}  gatilhos: boot + diario 04:10" -f $t.Settings.ExecutionTimeLimit

"`n=== 4) subindo e conferindo ==="
Start-ScheduledTask -TaskName $tarefa
Start-Sleep -Seconds 45
$p = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'vps_monitor\.py' })
"  processo: {0}" -f $(if ($p) { "rodando (pid $($p.ProcessId -join ','))" } else { 'NAO ESTA RODANDO' })
$csv = Join-Path $Base 'vps-monitor.csv'
if (Test-Path $csv) { "  ultimas medicoes:"; Get-Content $csv -Tail 3 | ForEach-Object { "    $_" } }
"`nPronto. Ele so decide estado depois de 5 min de medicoes (janela cheia) - reiniciar o servidor nao gera alarme."
