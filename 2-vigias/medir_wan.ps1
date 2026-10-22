# ============================================================================
#  medir_wan.ps1 - amostra os contadores da WAN do Cisco a cada execucao
#
#  Responde com numero: o enlace WAN (cabo/porta do modem) esta gerando erro
#  de CRC? No piloto, antes de trocar o cabo: ~23 erros por hora o dia todo.
#  Grava uma linha por amostra em C:\netmon\wan-crc.csv.
# ============================================================================
$ErrorActionPreference = 'Continue'
$csv = 'C:\netmon\wan-crc.csv'
$CFG = Get-Content (Join-Path $PSScriptRoot 'config.json') -Raw | ConvertFrom-Json

# o plink que o NetMon ja usa para falar com o Cisco
$plink = $null
foreach ($p in 'C:\netmon\plink.exe', 'C:\Program Files\PuTTY\plink.exe',
                'C:\Program Files (x86)\PuTTY\plink.exe') {
  if (Test-Path $p) { $plink = $p; break }
}
if (-not $plink) {
  try {
    $cfg = Get-Content 'C:\netmon\config.json' -Raw | ConvertFrom-Json
    if ($cfg.plink -and (Test-Path $cfg.plink)) { $plink = $cfg.plink }
  } catch { }
}
if (-not $plink) { "plink nao encontrado" | Add-Content $csv; exit 1 }

# as duas linhas em branco sao aquecimento: a primeira linha do stdin chega
# corrompida no SSHv1 do 2911
$wanIf = if ($CFG.cisco.wan_if) { $CFG.cisco.wan_if } else { 'GigabitEthernet0/1' }
$saida = ("`n`nshow interfaces $wanIf | include packets input|input errors`n" |
          & $plink -1 -batch -ssh "$($CFG.cisco.user)@$($CFG.cisco.host)" -pw $CFG.cisco.password 2>$null | Out-String)

$pin = [regex]::Match($saida, '(\d+) packets input').Groups[1].Value
$err = [regex]::Match($saida, '(\d+) input errors').Groups[1].Value
$crc = [regex]::Match($saida, '(\d+) CRC').Groups[1].Value
$frm = [regex]::Match($saida, '(\d+) frame').Groups[1].Value
# ANTES AQUI ERA "exit 0" MUDO -- e a tarefa passou 18h "rodando com resultado
# 0" sem escrever UMA linha, porque como SYSTEM o plink nao achava a chave de
# host do Cisco. Falha silenciosa e pior que falha: parece que esta tudo bem.
if (-not $pin) {
  $motivo = if (-not $saida.Trim()) { 'plink nao devolveu nada (chave de host? credencial?)' } else { 'saida veio mas o regex nao casou' }
  "{0} LEITURA FALHOU: {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $motivo |
    Add-Content 'C:\netmon\medir-wan.err.log' -Encoding UTF8
  ($saida -split "`r?`n" | Select-Object -First 6) | ForEach-Object { "    | $_" } |
    Add-Content 'C:\netmon\medir-wan.err.log' -Encoding UTF8
  exit 1
}

if (-not (Test-Path $csv)) {
  'quando,packets,input_errors,crc,frame,crc_delta,pacotes_delta,ppm_intervalo' | Set-Content $csv -Encoding UTF8
}
$ant = @(Import-Csv $csv) | Select-Object -Last 1
$dCrc = if ($ant) { [int64]$crc - [int64]$ant.crc } else { 0 }
$dPin = if ($ant) { [int64]$pin - [int64]$ant.packets } else { 0 }
$ppm  = if ($dPin -gt 0) { [math]::Round($dCrc * 1e6 / $dPin, 2) } else { 0 }

"{0},{1},{2},{3},{4},{5},{6},{7}" -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'),
  $pin, $err, $crc, $frm, $dCrc, $dPin, $ppm | Add-Content $csv -Encoding UTF8
