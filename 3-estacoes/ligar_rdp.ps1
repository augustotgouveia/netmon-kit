# ============================================================================
#  ligar_rdp.ps1 - liga a Area de Trabalho Remota nas estacoes Pro
#
#  Script separado de proposito: reexecutar o ABRE-GERENCIA geraria uma SENHA
#  NOVA para o gtiadmin e invalidaria o cofre inteiro.
#
#  Liga so onde da: Windows Home NAO tem servidor RDP (nao e configuracao,
#  e edicao). Nas Home ele nem tenta.
#
#  Regra de firewall restrita a rede local (-Rede, padrao 192.168.0.0/24) e NLA obrigatorio.
#  -Desligar reverte.
# ============================================================================
param(
  [string]$Maquina,
  [switch]$Todas,
  [switch]$Desligar,
  [string]$Rede  = '192.168.0.0/24',
  [string]$Cofre = 'C:\netmon\creds'
)
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false) } catch { }

$alvos = @()
if ($Todas)       { $alvos = @(Get-ChildItem (Join-Path $Cofre '*.xml') -ErrorAction SilentlyContinue) }
elseif ($Maquina) { $alvos = @(Get-Item (Join-Path $Cofre "$Maquina.xml") -ErrorAction SilentlyContinue) }
else { "use -Todas ou -Maquina <nome>"; exit 1 }

$TRAB = {
  param($rede, $desligar)
  $os = Get-CimInstance Win32_OperatingSystem
  if ($os.Caption -match '(?i)home|single language|core') {
    return "  PULADA: $($os.Caption) - edicao Home nao tem servidor RDP"
  }
  $ts = 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server'
  $antes = (Get-ItemProperty $ts -Name fDenyTSConnections -ErrorAction SilentlyContinue).fDenyTSConnections

  if ($desligar) {
    Set-ItemProperty $ts -Name fDenyTSConnections -Value 1
    try { Disable-NetFirewallRule -Group (Get-NetFirewallRule -Name 'RemoteDesktop-UserMode-In-TCP').Group -EA Stop } catch { }
    return "  RDP DESLIGADO (fDenyTSConnections: $antes -> 1)"
  }

  Set-ItemProperty $ts -Name fDenyTSConnections -Value 0
  # NLA: autentica ANTES de desenhar a tela. Sem isso qualquer um chega na
  # tela de login pela rede.
  Set-ItemProperty "$ts\WinStations\RDP-Tcp" -Name UserAuthentication -Value 1 -ErrorAction SilentlyContinue
  Set-ItemProperty "$ts\WinStations\RDP-Tcp" -Name SecurityLayer -Value 2 -ErrorAction SilentlyContinue

  $n = 0
  $anc = Get-NetFirewallRule -Name 'RemoteDesktop-UserMode-In-TCP' -ErrorAction SilentlyContinue
  if ($anc) {
    foreach ($x in @(Get-NetFirewallRule -Group $anc.Group -Direction Inbound -ErrorAction SilentlyContinue)) {
      try {
        Enable-NetFirewallRule -Name $x.Name -ErrorAction Stop
        # sem esta linha a regra vale para QUALQUER origem
        Set-NetFirewallRule -Name $x.Name -RemoteAddress $rede -ErrorAction Stop
        $n++
      } catch { }
    }
  }
  $dep = (Get-ItemProperty $ts -Name fDenyTSConnections).fDenyTSConnections
  $nla = (Get-ItemProperty "$ts\WinStations\RDP-Tcp" -Name UserAuthentication -EA SilentlyContinue).UserAuthentication
  $esc = [bool](Get-NetTCPConnection -State Listen -LocalPort 3389 -ErrorAction SilentlyContinue)
  "  RDP: $antes -> $dep   NLA=$nla   $n regra(s) limitadas a $rede   escutando 3389: $esc"
}

$opt = New-PSSessionOption -OpenTimeout 8000 -OperationTimeout 120000
foreach ($f in $alvos) {
  $r = Import-Clixml $f.FullName
  "$($r.maquina)  ($($r.ip))"
  try {
    Invoke-Command -ComputerName $r.ip -Credential $r.cred -SessionOption $opt -ScriptBlock $TRAB -ArgumentList $Rede, $Desligar.IsPresent -ErrorAction Stop
  } catch { "  FALHOU: $($_.Exception.Message.Split([char]10)[0])" }
}
