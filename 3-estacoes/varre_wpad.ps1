# ============================================================================
#  varre_wpad.ps1 - caca o WPAD religado em TODA a frota, por WinRM
#
#  Mesma medida que o NetMon usa (bit 0x08 do byte 8 do blob
#  DefaultConnectionSettings, por perfil de HKU, pulando o da conta de
#  gerencia). Le o cofre DPAPI inteiro e visita maquina por maquina.
#
#  Maquina desligada nao trava a varredura: pinga antes, e se o IP do cofre
#  nao responde tenta achar o IP atual pelo MAC no ARP do Cisco -- o cofre
#  guarda o IP do dia do cadastro e elas sao clientes DHCP.
#
#  USO: .\varre_wpad.ps1            -> so olha
#       .\varre_wpad.ps1 -Aplicar   -> desliga onde achar
# ============================================================================
param(
  [switch]$Aplicar,
  [string]$Cofre = 'C:\netmon\creds'
)
$ErrorActionPreference = 'Continue'
$CFG = Get-Content (Join-Path $PSScriptRoot 'config.json') -Raw | ConvertFrom-Json

# ---- mapa MAC -> IP atual, do ARP do Cisco (o cofre envelhece) -------------
$porMac = @{}
try {
  $plink = 'C:\netmon\plink.exe'
  if (Test-Path $plink) {
    # as duas linhas em branco sao aquecimento: a primeira linha do stdin chega
    # corrompida no SSHv1 do 2911
    $saida = ("`n`nshow ip arp`n" | & $plink -1 -batch -ssh "$($CFG.cisco.user)@$($CFG.cisco.host)" -pw $CFG.cisco.password 2>$null | Out-String)
    foreach ($linha in ($saida -split "`r?`n")) {
      $m = [regex]::Match($linha, '(\d+\.\d+\.\d+\.\d+)\s+\S+\s+([0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4})')
      if (-not $m.Success) { continue }
      $cru = ($m.Groups[2].Value -replace '\.','')
      $mac = ((0..5 | ForEach-Object { $cru.Substring($_*2,2) }) -join '-').ToUpper()
      if (-not $porMac.ContainsKey($mac)) { $porMac[$mac] = $m.Groups[1].Value }
    }
  }
} catch { }
"ARP do Cisco: {0} entradas" -f $porMac.Count

$bloco = {
  param($fazer)
  $res = [ordered]@{ perfis = @(); do_mode = $null; erro = $null; backup = $null }
  try {
    $doKey = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\DeliveryOptimization'
    if (Test-Path $doKey) {
      $res.do_mode = (Get-ItemProperty $doKey -Name DODownloadMode -ErrorAction SilentlyContinue).DODownloadMode
    }
  } catch { }
  try {
    if (-not (Get-PSDrive -Name HKU -ErrorAction SilentlyContinue)) {
      New-PSDrive -Name HKU -PSProvider Registry -Root HKEY_USERS -ErrorAction SilentlyContinue | Out-Null
    }
    $meuSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $backup = @{}
    foreach ($p in @(Get-ChildItem 'HKU:\' -ErrorAction SilentlyContinue |
                     Where-Object { $_.PSChildName -match '^S-1-5-21-[\d-]+$' })) {
      $sid = $p.PSChildName
      if ($sid -eq $meuSid) { continue }
      $cam = "HKU:\$sid\Software\Microsoft\Windows\CurrentVersion\Internet Settings\Connections"
      $b = (Get-ItemProperty $cam -Name DefaultConnectionSettings -ErrorAction SilentlyContinue).DefaultConnectionSettings
      if (-not $b -or $b.Length -le 8) { continue }
      $ligado = [bool]($b[8] -band 0x08)
      if (-not $ligado) { continue }
      $quem = try { ((New-Object Security.Principal.SecurityIdentifier($sid)).Translate([Security.Principal.NTAccount]).Value -split '\\')[-1] } catch { $sid }
      $item = [ordered]@{ nome = $quem; antes = $b[8]; depois = $null; mexi = $false; aindaLigado = $true }
      if ($fazer) {
        $backup[$sid] = ($b -join ',')
        $novo = $b.Clone()
        $novo[8] = [byte]($b[8] -band 0xF7)
        Set-ItemProperty -Path $cam -Name DefaultConnectionSettings -Value $novo -Type Binary
        $conf = (Get-ItemProperty $cam -Name DefaultConnectionSettings).DefaultConnectionSettings
        $item.depois = $conf[8]
        $item.mexi = $true
        $item.aindaLigado = [bool]($conf[8] -band 0x08)
      }
      $res.perfis += [PSCustomObject]$item
    }
    if ($fazer -and $backup.Count) {
      $bk = "C:\Windows\Temp\wpad-backup-$(Get-Date -Format 'yyyyMMdd-HHmmss').json"
      ($backup | ConvertTo-Json -Depth 4) | Set-Content $bk -Encoding UTF8
      $res.backup = $bk
    }
  } catch { $res.erro = $_.Exception.Message }
  [PSCustomObject]$res
}

$linhas = @()
foreach ($arq in (Get-ChildItem (Join-Path $Cofre '*.xml') | Sort-Object Name)) {
  $reg = Import-Clixml $arq.FullName
  $nome = $reg.maquina
  $ip = $reg.ip

  # SEM PORTEIRO DE PING OU ARP. Os dois mentem nesta rede:
  #   - algumas estacoes nao respondem ping (bloqueiam ICMP) e
  #     estavam no ar ha 6 dias, com WinRM funcionando;
  #   - a entrada de ARP do Cisco some de maquina viva e persiste horas depois
  #     de desligar, entao nao prova nada nos dois sentidos.
  # O unico teste honesto e abrir a sessao. Timeout curto para maquina
  # desligada nao travar a varredura.
  $op = New-PSSessionOption -OpenTimeout 9000 -OperationTimeout 25000

  function Tenta($alvo, $credencial, $corpo, $arg, $opcoes) {
    try {
      $s = New-PSSession -ComputerName $alvo -Credential $credencial -SessionOption $opcoes -ErrorAction Stop
      try { return @($true, (Invoke-Command -Session $s -ScriptBlock $corpo -ArgumentList $arg)) }
      finally { Remove-PSSession $s -ErrorAction SilentlyContinue }
    } catch {
      return @($false, ($_.Exception.Message -replace '\s+',' '))
    }
  }

  $saida = Tenta $ip $reg.cred $bloco ([bool]$Aplicar) $op
  # IP do cofre pode ter envelhecido: o MAC no ARP da a segunda chance
  if (-not $saida[0]) {
    $noArp = $porMac[([string]$reg.mac).ToUpper()]
    if ($noArp -and $noArp -ne $ip) {
      $tent2 = Tenta $noArp $reg.cred $bloco ([bool]$Aplicar) $op
      if ($tent2[0]) { $ip = $noArp; $saida = $tent2 }
    }
  }

  if (-not $saida[0]) {
    $msg = "sem WinRM: $($saida[1])"
    if ($msg.Length -gt 58) { $msg = $msg.Substring(0, 58) }
    $linhas += [PSCustomObject]@{ maquina = $nome; ip = $ip; estado = $msg; perfis = @(); do_mode = $null }
    continue
  }
  $r = $saida[1]
  $est = if ($r.erro) { "erro: $($r.erro)" } elseif ($r.perfis.Count -eq 0) { 'limpa' } else { "$($r.perfis.Count) perfil(is) com WPAD" }
  $linhas += [PSCustomObject]@{ maquina = $nome; ip = $ip; estado = $est; perfis = $r.perfis; do_mode = $r.do_mode }
}

""
"{0,-18} {1,-15} {2,-8} {3}" -f 'MAQUINA', 'IP', 'DO', 'ESTADO'
"-" * 82
foreach ($l in $linhas) {
  $do = if ($null -eq $l.do_mode) { '-' } elseif ($l.do_mode -in 1, 99) { "$($l.do_mode) ok" } else { "$($l.do_mode) !!" }
  "{0,-18} {1,-15} {2,-8} {3}" -f $l.maquina, $l.ip, $do, $l.estado
  foreach ($p in $l.perfis) {
    $sufixo = if ($p.mexi) { "byte 8: $($p.antes) -> $($p.depois)" } else { 'ainda ligado' }
    "                     perfil '{0}'  {1}" -f $p.nome, $sufixo
  }
}

$comWpad = @($linhas | Where-Object { $_.perfis.Count -gt 0 })
$fora    = @($linhas | Where-Object { $_.estado -notmatch '^limpa|perfil' })
$doRuim  = @($linhas | Where-Object { $null -ne $_.do_mode -and $_.do_mode -notin 1, 99 })
""
"RESUMO: {0} maquina(s) no cofre | {1} com WPAD | {2} com DO fora de 1/99 | {3} nao verificada(s)" -f `
  $linhas.Count, $comWpad.Count, $doRuim.Count, $fora.Count
if (-not $Aplicar -and $comWpad.Count) { "Rode com -Aplicar para desligar." }

