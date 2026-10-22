# ============================================================================
#  sonda_wan2.ps1 - de quem e a culpa quando a internet cai (v2)
#
#  O QUE MUDOU DA v1, E POR QUE:
#  A v1 testava 8.8.8.8 e 1.1.1.1. Os DOIS sao anycast e respondem de dentro
#  do Brasil. Em 01/10/2026 o Telegram ficou inalcancavel (4 faixas diferentes)
#  enquanto google.com:443 abria e 8.8.8.8 pingava normal: quebrou o TRANSITO
#  INTERNACIONAL, e a v1 teria escrito "ok" durante o evento inteiro.
#
#  Agora ha um degrau a mais: destino comprovadamente fora do Brasil (~200 ms).
#
#  Escada de culpa, do mais perto pro mais longe:
#     Cisco fora          -> problema NOSSO, dentro de casa
#     modem fora          -> NOSSO LADO (cabo/porta/modem)
#     nacional fora       -> OPERADORA
#     so internacional    -> TRANSITO INTERNACIONAL (operadora tb, mas outro assunto
#                            e outro papo com o suporte deles)
#
#  Grava em sonda-wan2.csv (arquivo novo de proposito: a v1 tem outra coluna).
# ============================================================================
param([int]$Segundos = 10, [int]$Horas = 24)

$ErrorActionPreference = 'Continue'
$csv   = 'C:\netmon\sonda-wan2.csv'
$CFG = Get-Content (Join-Path $PSScriptRoot 'config.json') -Raw | ConvertFrom-Json
$MODEM = $CFG.rede.modem      # modem da operadora, antes do caminho dela
$GW    = $CFG.cisco.host      # nosso roteador, separa problema interno
$NAC1  = '8.8.8.8'            # anycast: responde DE DENTRO do Brasil
$NAC2  = '1.1.1.1'            # idem
# CUIDADO AO MEXER NESTES NOMES: PowerShell NAO diferencia maiuscula em nome de
# variavel. A primeira versao disto usava $INTL pro IP e $intl pro resultado --
# eram a MESMA variavel. O primeiro ciclo media certo, o resultado $true
# sobrescrevia o IP, e dali em diante a sonda tentava conectar num host chamado
# "True" e acusava queda internacional pra sempre. O booleano agora e $intlOk.
$INTL  = '149.154.167.51'     # Telegram DC2 (Europa), ~210 ms, sem cache no Brasil
$INTL2 = '91.108.56.130'      # Telegram CDN, outro prefixo: segunda opiniao
# Os dois sao do Telegram de proposito: e exatamente o caminho que os alertas
# precisam. "Internacional fora" aqui significa "o alerta nao sairia agora".

if (-not (Test-Path $csv)) {
  'quando,cisco,modem,nacional,internacional,ms_intl,veredito' | Set-Content $csv -Encoding UTF8
}

function Responde($ip) {
  try { [bool](Test-Connection -ComputerName $ip -Count 1 -Quiet -ErrorAction SilentlyContinue) }
  catch { $false }
}

# Internacional e medido por TCP 443, nao por ping: muita coisa la fora descarta
# ICMP mas atende TCP. Devolve os ms pra dar pra ver degradacao antes da queda.
function TcpOk($ip, $porta = 443, $timeoutMs = 4000) {
  $sw = [Diagnostics.Stopwatch]::StartNew()
  $cli = New-Object Net.Sockets.TcpClient
  try {
    $iar = $cli.BeginConnect($ip, $porta, $null, $null)
    if (-not $iar.AsyncWaitHandle.WaitOne($timeoutMs, $false)) { return @($false, -1) }
    $cli.EndConnect($iar)
    return @($true, [int]$sw.ElapsedMilliseconds)
  } catch { return @($false, -1) }
  finally { try { $cli.Close() } catch { } }
}

$fim = (Get-Date).AddHours($Horas)
$anterior = ''
$ultimoRegistro = [datetime]::MinValue

while ((Get-Date) -lt $fim) {
  $c = Responde $GW
  $m = Responde $MODEM
  $nac = (Responde $NAC1) -or (Responde $NAC2)

  $r1 = TcpOk $INTL
  $intlOk = $r1[0]; $msIntl = $r1[1]
  if (-not $intlOk) {
    # segunda opiniao antes de acusar: um destino so pode estar fora por conta propria
    $r2 = TcpOk $INTL2
    $intlOk = $r2[0]; if ($r2[0]) { $msIntl = $r2[1] }
  }

  $veredito =
    if (-not $c)        { 'REDE INTERNA - nem o Cisco responde' }
    elseif (-not $m)    { 'NOSSO LADO - o modem da operadora nao responde' }
    elseif (-not $nac)  { 'OPERADORA - modem responde mas a internet nao' }
    elseif (-not $intlOk) { 'INTERNACIONAL - Brasil ok, trajeto de fora caiu' }
    else                { 'ok' }

  $estado = "$c|$m|$nac|$intlOk"
  $agora = Get-Date
  if ($estado -ne $anterior -or ($agora - $ultimoRegistro).TotalMinutes -ge 5) {
    "{0},{1},{2},{3},{4},{5},{6}" -f $agora.ToString('yyyy-MM-dd HH:mm:ss'),
      $c, $m, $nac, $intlOk, $msIntl, $veredito | Add-Content $csv -Encoding UTF8
    $anterior = $estado
    $ultimoRegistro = $agora
  }
  Start-Sleep -Seconds $Segundos
}

