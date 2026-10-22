# ============================================================================
#  vigia_aps.ps1 - registra TODA queda e volta dos APs, com duracao
#
#  POR QUE EXISTE: saber se a queda de um AP foi evento isolado ou padrao --
#  e a resposta so vem com dias de observacao, com hora e duracao.
#
#  Roda no SERVIDOR porque ele e com fio: um notebook no Wi-Fi do proprio AP
#  cai junto com ele e nao serve de observador.
#
#  Grava SO TRANSICAO (caiu / voltou) + um batimento de 6 h pra provar que
#  estava vivo. Tres falhas seguidas (45 s) antes de declarar queda, para nao
#  registrar pacote perdido.
#
#  ExecutionTimeLimit da tarefa TEM que ser PT0S: o padrao do Windows mata
#  tarefa longa em 72 h -- foi o que derrubava o NetMon a cada 3 dias.
# ============================================================================
param(
  [int]$Segundos = 15,
  [int]$Horas = 8760          # quem decide quando parar e a tarefa, nao o script
)
$ErrorActionPreference = 'Continue'
$csv = 'C:\netmon\aps-quedas.csv'

$CFG = Get-Content (Join-Path $PSScriptRoot 'config.json') -Raw | ConvertFrom-Json
$APS = @{}
foreach ($a in $CFG.aps) { $APS[$a.ip] = $a.nome }   # lista em config.json -> aps

if (-not (Test-Path $csv)) {
  'quando,ap,ip,evento,segundos_fora' | Set-Content $csv -Encoding UTF8
}

function Grava($ap, $ip, $evento, $seg) {
  "{0},{1},{2},{3},{4}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $ap, $ip, $evento, $seg |
    Add-Content $csv -Encoding UTF8
}

# estado inicial: mede antes de comecar, para nao registrar queda falsa de quem
# ja estava fora quando o vigia subiu
$estado = @{}
$desde  = @{}
$falhas = @{}
foreach ($ip in $APS.Keys) {
  $vivo = [bool](Test-Connection $ip -Count 2 -Quiet -ErrorAction SilentlyContinue)
  $estado[$ip] = $vivo
  $desde[$ip] = Get-Date
  $falhas[$ip] = 0
  Grava $APS[$ip] $ip $(if ($vivo) { 'inicio-no-ar' } else { 'inicio-fora' }) ''
}

$fim = (Get-Date).AddHours($Horas)
$ultimoBatimento = Get-Date

while ((Get-Date) -lt $fim) {
  foreach ($ip in @($APS.Keys)) {
    $vivo = [bool](Test-Connection $ip -Count 1 -Quiet -ErrorAction SilentlyContinue)

    if (-not $vivo) {
      $falhas[$ip]++
      # 3 falhas seguidas = 45 s sem resposta: ai sim e queda
      if ($falhas[$ip] -eq 3 -and $estado[$ip]) {
        $estado[$ip] = $false
        $desde[$ip] = (Get-Date).AddSeconds(-3 * $Segundos)
        Grava $APS[$ip] $ip 'caiu' ''
      }
    } else {
      if (-not $estado[$ip]) {
        $seg = [int]((Get-Date) - $desde[$ip]).TotalSeconds
        $estado[$ip] = $true
        $desde[$ip] = Get-Date
        Grava $APS[$ip] $ip 'voltou' $seg
      }
      $falhas[$ip] = 0
    }
  }

  if (((Get-Date) - $ultimoBatimento).TotalHours -ge 6) {
    foreach ($ip in @($APS.Keys)) {
      Grava $APS[$ip] $ip $(if ($estado[$ip]) { 'batimento-no-ar' } else { 'batimento-fora' }) ''
    }
    $ultimoBatimento = Get-Date
  }
  Start-Sleep -Seconds $Segundos
}

